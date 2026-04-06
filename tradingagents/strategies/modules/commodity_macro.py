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
    "gold", "silver", "copper", "crude", "oil", "natural gas", "lng",
    "mining", "metals", "energy", "opec", "pipeline", "refinery",
    "tariff", "sanctions", "embargo", "supply chain", "commodity",
]


class CommodityMacroStrategy:
    """Trade non-ag commodity ETFs based on COT positioning extremes."""

    name = "commodity_macro"
    track = "paper_trade"
    data_sources = ["yfinance", "cftc", "fred", "regulations", "finnhub"]

    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "cot_extreme_pct": (75, 95),
            "cot_lookback_weeks": (26, 104),
            "hold_days": hp["hold_days_range"],
            "macro_veto_enabled": (True, False),
            "catalyst_boost": (0.0, 0.3),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "cot_extreme_pct": 85,
            "cot_lookback_weeks": 52,
            "hold_days": hp["hold_days_default"],
            "macro_veto_enabled": True,
            "catalyst_boost": 0.15,
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
        candidates = []

        for commodity, cot in cot_data.items():
            if isinstance(cot, str):
                continue

            percentile = cot.get("percentile", 0.5)
            direction = cot.get("direction_signal", "neutral")

            if direction == "neutral":
                continue

            if not (percentile >= cot_extreme_pct or percentile <= (1.0 - cot_extreme_pct)):
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
                        "analysis_type": "commodity_macro",
                    },
                )
            )

        return candidates

    def check_exit(self, ticker, entry_price, current_price, holding_days, params, data):
        hold_days = params.get("hold_days", 90)
        if holding_days >= hold_days:
            return True, "hold_period"

        cot_data = data.get("cftc", {})
        if cot_data:
            for commodity, etf in _COMMODITY_TO_ETF.items():
                if etf == ticker or _LONG_SUBSTITUTIONS.get(etf) == ticker:
                    cot = cot_data.get(commodity, {})
                    if isinstance(cot, dict):
                        pctl = cot.get("percentile", 0.5)
                        if 0.30 <= pctl <= 0.70:
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
- cot_lookback_weeks: 26-104 (lookback for percentile calculation)
- hold_days: horizon-dependent (holding period)
- macro_veto_enabled: True/False (whether macro confirmation is required)
- catalyst_boost: 0.0-0.3 (score boost when catalyst present)

Recent results:
{results_text or '  No results yet.'}

Suggest 3 new parameter combinations. Return JSON array of 3 param dicts."""

    @staticmethod
    def _macro_vetoes(commodity, direction, fred_data):
        if not fred_data:
            return False

        fed_funds = _latest_value(fred_data.get("FEDFUNDS", {}))
        cpi_values = fred_data.get("CPIAUCSL", {})
        vix = _latest_value(fred_data.get("VIXCLS", {}))

        cpi_sorted = sorted(cpi_values.items()) if isinstance(cpi_values, dict) else []
        if len(cpi_sorted) >= 2:
            cpi_latest = cpi_sorted[-1][1]
            cpi_3m_ago = cpi_sorted[0][1]
            cpi_momentum = cpi_latest - cpi_3m_ago
        else:
            cpi_latest = _latest_value(cpi_values)
            cpi_momentum = 0.0

        real_rate_now = (fed_funds or 0) - (cpi_latest or 0)

        if commodity in ("gold", "silver") and direction == "long":
            if len(cpi_sorted) >= 2:
                real_rate_3m = (fed_funds or 0) - cpi_3m_ago
                real_rate_delta = real_rate_now - real_rate_3m
                if real_rate_delta >= 0.5:
                    return True

        if commodity in ("crude_oil", "nat_gas") and direction == "long":
            if cpi_momentum < 0:
                return True

        if direction == "short" and vix is not None and vix < 15:
            return True

        return False

    @staticmethod
    def _scan_catalysts(commodity, data):
        relevant_keywords = [k for k in _CATALYST_KEYWORDS if k in commodity or commodity in k]
        relevant_keywords.extend(["mining", "energy", "opec", "tariff", "sanctions"])

        regs = data.get("regulations", {})
        if isinstance(regs, dict):
            results = regs.get("results", [])
            if isinstance(results, list):
                for reg in results:
                    title = str(reg.get("title", "")).lower()
                    if any(kw in title for kw in relevant_keywords):
                        return True

        finnhub = data.get("finnhub", {})
        if isinstance(finnhub, dict):
            news = finnhub.get("news", [])
            if isinstance(news, list):
                for item in news:
                    headline = str(item.get("headline", "")).lower()
                    if any(kw in headline for kw in relevant_keywords):
                        return True

        return False


def _latest_value(series_data):
    if not series_data:
        return None
    if isinstance(series_data, dict):
        if not series_data:
            return None
        sorted_items = sorted(series_data.items())
        return sorted_items[-1][1]
    try:
        return float(series_data.iloc[-1])
    except (IndexError, TypeError):
        return None
