"""Commodity Macro strategy — trades non-ag commodity ETFs on COT positioning extremes.

Signal logic:
1. CFTC COT gate: extreme speculative positioning (contrarian).
2. Macro confirmation: FRED data veto for contradicting macro regimes.
3. Catalyst scan: regulatory/supply chain news for optional boost.
4. Emit ETF candidates via FUTURES_TO_ETF_MAP.
"""

from __future__ import annotations

import logging
from typing import Any
import math
import pandas as pd

from .base import Candidate

logger = logging.getLogger(__name__)

# --- Instrument Constants ---

FUTURES_TO_ETF_MAP = {
    "GC=F": "GLD",
    "SI=F": "SLV",
    "CL=F": "USO",
    "NG=F": "UNG",
    "HG=F": "COPX",
}

ETF_TO_FUTURES_UNDERLYING = {
    "GLD": "GC",
    "SLV": "SI",
    "USO": "CL",
    "UNG": "NG",
    "COPX": "HG",
    "PDBC": None,
    "XLE": "CL",
}

SHORT_ONLY_ETFS = {"USO", "UNG"}
SAFE_HAVEN_ETFS = {"GLD", "SLV"}
COMMODITY_ETFS = {"GLD", "SLV", "PDBC", "COPX", "XLE", "USO", "UNG"}

_COMMODITY_TO_ETF = {
    "gold": "GLD",
    "silver": "SLV",
    "crude_oil": "USO",
    "nat_gas": "UNG",
    "copper": "COPX",
}

_LONG_SUBSTITUTIONS = {
    "USO": "XLE",
    "UNG": None,
}

_CATALYST_KEYWORDS = [
    "gold",
    "silver",
    "copper",
    "crude",
    "oil",
    "natural gas",
    "lng",
    "mining",
    "metals",
    "energy",
    "opec",
    "pipeline",
    "refinery",
    "tariff",
    "sanctions",
    "embargo",
    "supply chain",
    "commodity",
]


class CommodityMacroStrategy:
    """Trade non-ag commodity ETFs based on COT positioning extremes."""

    name = "commodity_macro"
    track = "paper_trade"
    data_sources = ["yfinance", "cftc", "fred", "regulations", "finnhub"]

    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            HORIZON_PARAMS,
        )

        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "cot_extreme_pct": (75, 95),
            "hold_days": hp["hold_days_range"],
            "macro_veto_enabled": (True, False),
            "catalyst_boost": (0.0, 0.3),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            HORIZON_PARAMS,
        )

        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        if not hp.get("commodity_eligible", False):
            eligible = []
        else:
            eligible = hp.get("commodity_instruments_override", list(COMMODITY_ETFS))
        return {
            "cot_extreme_pct": 85,
            "hold_days": hp["hold_days_default"],
            "macro_veto_enabled": True,
            "catalyst_boost": 0.15,
            "eligible_instruments": eligible,
            "commodity_eligible": hp.get("commodity_eligible", False),
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        eligible = params.get("eligible_instruments", [])
        if not eligible:
            return []

        cot_data = data.get("cftc", {})
        if not cot_data or "error" in cot_data:
            return []

        cot_extreme_pct = params.get("cot_extreme_pct", 85) / 100.0
        macro_veto = params.get("macro_veto_enabled", True)
        catalyst_boost_val = params.get("catalyst_boost", 0.15)
        fred_data = data.get("fred", {})
        # Bound observations even when the accepted bundle spans later sessions.
        fred_data = {key: {str(when): value for when, value in values.items() if pd.Timestamp(when).date().isoformat() <= date}
                     for key, values in fred_data.items() if key in {"FEDFUNDS", "CPIAUCSL", "VIXCLS"} and hasattr(values, "items")}
        candidates = []

        for commodity, cot in cot_data.items():
            if commodity not in _COMMODITY_TO_ETF or not isinstance(cot, dict):
                continue

            percentile = cot.get("percentile")
            if isinstance(percentile, bool) or not isinstance(percentile, (float, int)) or not math.isfinite(percentile) or not 0 <= percentile <= 1:
                continue
            direction = cot.get("direction_signal", "neutral")
            report_id = cot.get("report_id")
            window_end = cot.get("window_end")

            if direction not in {"long", "short"} or not report_id or not window_end:
                continue
            if macro_veto and not self._macro_inputs_available(commodity, direction, fred_data):
                continue

            if not (
                percentile >= cot_extreme_pct or percentile <= (1.0 - cot_extreme_pct)
            ):
                continue

            if macro_veto and self._macro_vetoes(commodity, direction, fred_data):
                logger.info("Macro veto: %s %s", commodity, direction)
                continue

            etf = _COMMODITY_TO_ETF.get(commodity)
            if etf is None:
                continue

            if etf in SHORT_ONLY_ETFS and direction == "long":
                substitute = _LONG_SUBSTITUTIONS.get(etf)
                if substitute is None:
                    continue
                etf = substitute

            if etf not in eligible:
                continue

            base_score = 0.5
            catalyst_found = self._scan_catalysts(commodity, data)
            if catalyst_found:
                base_score += catalyst_boost_val

            candidates.append(
                Candidate(
                    ticker=etf,
                    date=date,
                    direction=direction,
                    score=base_score,
                    metadata={
                        "commodity": commodity,
                        "cot_percentile": percentile,
                        "cot_net_position": cot.get("net_position", 0),
                        "catalyst_found": catalyst_found,
                        "needs_llm_analysis": True,
                        "deterministic_evidence_complete": True,
                        "cot_evidence": cot,
                        "macro_evidence": {k: {str(when): float(value) for when, value in v.items()} if hasattr(v, "items") else v for k, v in fred_data.items() if k in {"FEDFUNDS", "CPIAUCSL", "VIXCLS"}},
                        **({"available_at": cot["available_at"]} if cot.get("available_at") else {}),
                        "analysis_type": "commodity_macro",
                        "report_id": report_id,
                        "window_end": window_end,
                    },
                )
            )

        return candidates

    def check_exit(
        self, ticker, entry_price, current_price, holding_days, params, data, direction="long"
    ):
        hold_days = params.get("hold_days", 90)
        if holding_days >= hold_days:
            return True, "hold_period"

        cot_data = data.get("cftc", {})
        if cot_data and "error" not in cot_data:
            for commodity, etf in _COMMODITY_TO_ETF.items():
                if etf == ticker or _LONG_SUBSTITUTIONS.get(etf) == ticker:
                    cot = cot_data.get(commodity, {})
                    if isinstance(cot, dict):
                        pctl = cot.get("percentile")
                        if cot.get("report_id") and cot.get("window_end") and isinstance(pctl, (int, float)) and not isinstance(pctl, bool) and math.isfinite(pctl) and 0.30 <= pctl <= 0.70:
                            return True, "cot_normalized"

        return False, ""

    def build_propose_prompt(self, context: dict) -> str:
        current = context.get("current_params", self.get_default_params())
        results = context.get("recent_results", [])
        results_text = ""
        if results:
            for r in results[-5:]:
                results_text += (
                    f"  params={r.get('params', {})}, "
                    f"sharpe={r.get('sharpe', 0):.2f}, "
                    f"return={r.get('total_return', 0):.2%}, "
                    f"trades={r.get('num_trades', 0)}\n"
                )
        return f"""You are optimizing a Commodity Macro strategy that trades
non-agricultural commodity ETFs (GLD, SLV, USO, UNG, COPX, XLE, PDBC)
based on CFTC Commitments of Traders positioning extremes with macro
confirmation.

Current parameters: {current}

Parameter ranges:
- cot_extreme_pct: 75-95 (percentile threshold for extreme positioning)
- hold_days: horizon-dependent (holding period)
- macro_veto_enabled: True/False (whether macro confirmation is required)
- catalyst_boost: 0.0-0.3 (score boost when catalyst present)

Recent results:
{results_text or "  No results yet."}

Suggest 3 new parameter combinations. Return JSON array of 3 param dicts."""

    @staticmethod
    def _real_rate_points(fred_data):
        """Policy rate less CPI year-over-year percent inflation, date aligned.

        CPIAUCSL is an index. Compare three-month change in ex-post real
        policy rate using matched CPI/FEDFUNDS dates, never index points.
        """
        cpi = _observations(fred_data.get("CPIAUCSL"))
        fed = _observations(fred_data.get("FEDFUNDS"))
        points = []
        for when, level in cpi:
            year_before = when - pd.DateOffset(years=1)
            previous = [(d, v) for d, v in cpi if d <= year_before]
            rate = [(d, v) for d, v in fed if d <= when]
            if (previous and rate and level > 0 and previous[-1][1] > 0
                    and previous[-1][0].to_period("M") == when.to_period("M") - 12
                    and rate[-1][0].to_period("M") == when.to_period("M")):
                inflation = 100 * (level / previous[-1][1] - 1)
                points.append((when, rate[-1][1] - inflation))
        return points

    @classmethod
    def _macro_inputs_available(cls, commodity, direction, fred_data):
        if direction == "short":
            return _latest_value(fred_data.get("VIXCLS")) is not None
        cpi = _observations(fred_data.get("CPIAUCSL"))
        if commodity in {"gold", "silver"}:
            points = cls._real_rate_points(fred_data)
            return bool(points and any(d.to_period("M") == points[-1][0].to_period("M") - 3 for d, _ in points))
        if commodity in {"crude_oil", "nat_gas"}:
            return bool(cpi and any(d.to_period("M") == cpi[-1][0].to_period("M") - 3 for d, _ in cpi))
        return True

    @classmethod
    def _macro_vetoes(cls, commodity, direction, fred_data):
        if direction == "short":
            vix = _latest_value(fred_data.get("VIXCLS"))
            return vix is not None and vix < 15
        if commodity in {"gold", "silver"}:
            points = cls._real_rate_points(fred_data)
            if points:
                prior = [v for d, v in points if d.to_period("M") == points[-1][0].to_period("M") - 3]
                return bool(prior and points[-1][1] - prior[-1] >= .5)
        if commodity in {"crude_oil", "nat_gas"}:
            cpi = _observations(fred_data.get("CPIAUCSL"))
            if cpi:
                prior = [v for d, v in cpi if d.to_period("M") == cpi[-1][0].to_period("M") - 3]
                return bool(prior and cpi[-1][1] < prior[-1])
        return False

    @staticmethod
    def _scan_catalysts(commodity, data):
        relevant_keywords = {
            "gold": ("gold",), "silver": ("silver",), "copper": ("copper",),
            "crude_oil": ("crude", "oil", "opec", "refinery"),
            "nat_gas": ("natural gas", "lng", "gas pipeline"),
        }.get(commodity, (commodity,))

        regs = data.get("regulations", {})
        if isinstance(regs, dict):
            results = regs.get("proposed_rules", regs.get("results", []))
            if isinstance(results, list):
                for reg in results:
                    title = str(reg.get("title", "")).lower()
                    if any(kw in title for kw in relevant_keywords):
                        return True

        finnhub = data.get("finnhub", {})
        if isinstance(finnhub, dict):
            news = [*finnhub.get("news", []), *finnhub.get("disruption_news", []), *finnhub.get("pqc_news", [])]
            if isinstance(news, list):
                for item in news:
                    headline = str(item.get("headline", "")).lower()
                    if any(kw in headline for kw in relevant_keywords):
                        return True

        return False


def _observations(values):
    if values is None:
        return []
    items = values.items() if hasattr(values, "items") else []
    result = []
    for when, value in items:
        try:
            observed, number = pd.Timestamp(when), float(value)
            if not pd.isna(observed) and math.isfinite(number):
                result.append((observed, number))
        except (ValueError, TypeError):
            continue
    return sorted(result)


def _latest_value(series_data):
    observations = _observations(series_data)
    return observations[-1][1] if observations else None
