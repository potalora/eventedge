"""Weather/Agriculture strategy — trades ag instruments on supply disruption signals.

Academic basis: "USDA Reports Affect the Stock Market, Too" (2024,
J. Commodity Markets) and Energy Economics (2021) showing temperature
anomalies significantly affect ag futures returns. Weather explains
~33% of crop yield variability.

Signal logic:
1. Gather data from NOAA (weather), USDA (crop conditions), Drought Monitor.
2. Rule-based gate: is anything interesting happening?
3. If yes, bundle all raw ag data into metadata and delegate to LLM for scoring.
4. Year-round operation with seasonal ticker filtering.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from .base import Candidate

logger = logging.getLogger(__name__)

# Full agricultural ticker universe (ETFs + stocks)
AG_TICKERS_FULL = {
    # ETFs — direct commodity exposure
    "dba": "DBA",    # Invesco DB Agriculture Fund
    "weat": "WEAT",  # Teucrium Wheat Fund
    "corn": "CORN",  # Teucrium Corn Fund
    "moo": "MOO",    # VanEck Agribusiness ETF
    "soyb": "SOYB",  # Teucrium Soybean Fund
    # Stocks — agribusiness companies
    "adm": "ADM",    # Archer-Daniels-Midland
    "bg": "BG",      # Bunge Global
    "ctva": "CTVA",  # Corteva Agriscience
    "de": "DE",      # Deere & Company
    "fmc": "FMC",    # FMC Corporation
    # Food/beverage — weather-sensitive demand
    "pep": "PEP",    # PepsiCo
    "ko": "KO",      # Coca-Cola
    "gis": "GIS",    # General Mills
    "mdlz": "MDLZ",  # Mondelez International
    # Fertilizer/crop chemicals — input cost sensitivity
    "mos": "MOS",    # Mosaic Company
    "ntr": "NTR",    # Nutrien
}

# Winter subset (Oct-Mar): skip corn/soy-specific + seasonal-only instruments
AG_TICKERS_WINTER = {"weat", "dba", "moo", "adm", "bg", "pep", "ko", "gis", "mdlz"}

# Industries for dynamic OpenBB expansion
AG_EXPANSION_INDUSTRIES = [
    "Agricultural Products",
    "Packaged Foods",
    "Farm & Heavy Construction Machinery",
    "Agricultural Inputs",
]


class WeatherAgStrategy:
    """Trade agricultural instruments based on multi-source supply disruption signals."""

    name = "weather_ag"
    track = "paper_trade"
    data_sources = ["yfinance", "noaa", "usda", "drought_monitor", "openbb"]

    def get_param_space(self) -> dict[str, tuple]:
        return {
            "lookback_days": (10, 60),
            "hold_days": (20, 45),
            "min_return": (-0.05, 0.05),
            "heat_stress_threshold": (2, 15),
            "precip_deficit_threshold": (-50, -10),
            "drought_min_score": (0.3, 2.0),
            "crop_decline_threshold": (1, 5),
        }

    def get_default_params(self) -> dict[str, Any]:
        return {
            "lookback_days": 21,
            "hold_days": 25,
            "min_return": 0.0,
            "heat_stress_threshold": 2,
            "precip_deficit_threshold": -10,
            "drought_min_score": 0.3,
            "crop_decline_threshold": 1,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for agricultural opportunities using multi-source disruption signals."""
        prices = data.get("yfinance", {}).get("prices", {})
        lookback = params.get("lookback_days", 21)
        heat_threshold = params.get("heat_stress_threshold", 5)
        precip_threshold = params.get("precip_deficit_threshold", -25)
        drought_min = params.get("drought_min_score", 1.0)
        crop_decline_min = params.get("crop_decline_threshold", 2)

        try:
            current_month = pd.Timestamp(date).month
        except ValueError:
            return []

        # Determine season and eligible tickers
        is_growing_season = 4 <= current_month <= 9
        if is_growing_season:
            eligible_tickers = AG_TICKERS_FULL
        else:
            eligible_tickers = {k: v for k, v in AG_TICKERS_FULL.items() if k in AG_TICKERS_WINTER}

        # --- Gather ag context from all sources ---
        noaa_data = data.get("noaa", {})
        drought_data = data.get("drought_monitor", {})
        usda_data = data.get("usda", {})

        # --- Gate check: is anything interesting happening? ---
        gate_triggered = False
        gate_reasons = []

        # Drought gate
        drought_score = 0.0
        if isinstance(drought_data, dict):
            drought_score = drought_data.get("composite_score", 0.0)
        if drought_score >= drought_min:
            gate_triggered = True
            gate_reasons.append(f"drought={drought_score:.1f}")

        # USDA crop condition gate (week-over-week decline in Good+Excellent)
        crop_decline = self._check_crop_decline(usda_data)
        if crop_decline > crop_decline_min:
            gate_triggered = True
            gate_reasons.append(f"crop_decline={crop_decline}pp")

        # NOAA weather gate (growing season only)
        if is_growing_season and noaa_data and "error" not in noaa_data:
            heat_days = noaa_data.get("heat_stress_days", 0)
            precip_deficit = noaa_data.get("precip_deficit_pct", 0)
            frost_events = noaa_data.get("frost_events", 0)
            if heat_days >= heat_threshold:
                gate_triggered = True
                gate_reasons.append(f"heat={heat_days}d")
            if precip_deficit < precip_threshold:
                gate_triggered = True
                gate_reasons.append(f"precip={precip_deficit:.0f}%")
            if frost_events > 0:
                gate_triggered = True
                gate_reasons.append(f"frost={frost_events}")

        # Momentum gate: any ag ticker trailing return > 2%
        ag_returns: list[tuple[str, str, float]] = []
        for name, ticker in eligible_tickers.items():
            df = prices.get(ticker)
            if df is None or df.empty:
                continue
            df = df.loc[:date]
            if len(df) < lookback:
                continue
            close = df["Close"]
            trailing_return = (close.iloc[-1] / close.iloc[-lookback]) - 1.0
            ag_returns.append((name, ticker, trailing_return))
            if trailing_return > 0.02:
                gate_triggered = True
                gate_reasons.append(f"momentum_{ticker}={trailing_return:.1%}")

        if not gate_triggered or not ag_returns:
            return []

        logger.info("Ag gate triggered: %s", ", ".join(gate_reasons))

        # --- Build candidates with LLM metadata ---
        ag_returns.sort(key=lambda x: x[2], reverse=True)

        # Bundle all raw ag data for LLM analysis
        ag_context = {
            "drought_score": drought_score,
            "drought_states": drought_data.get("states", {}) if isinstance(drought_data, dict) else {},
            "noaa_data": {k: v for k, v in noaa_data.items() if k != "error"} if noaa_data else {},
            "usda_data": usda_data if usda_data else {},
            "is_growing_season": is_growing_season,
            "gate_reasons": gate_reasons,
        }

        candidates = []
        for name, ticker, ret in ag_returns[:3]:
            candidates.append(
                Candidate(
                    ticker=ticker,
                    date=date,
                    direction="long",
                    score=0.5,  # LLM will adjust
                    metadata={
                        "commodity": name,
                        "trailing_return": round(ret, 4),
                        "needs_llm_analysis": True,
                        "analysis_type": "ag_weather",
                        **ag_context,
                    },
                )
            )

        return candidates

    def check_exit(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        holding_days: int,
        params: dict,
        data: dict,
    ) -> tuple[bool, str]:
        """Exit on hold period."""
        hold_days = params.get("hold_days", 25)
        if holding_days >= hold_days:
            return True, "hold_period"
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

        return f"""You are optimizing a Weather/Agriculture strategy that trades ag
ETFs and agribusiness stocks based on NOAA weather anomalies, USDA crop
condition declines, and US Drought Monitor data.

Investment horizon: 30 days. Every signal must answer "why will this move
price within 30 days?" Crop/weather impacts take 3-4 weeks to flow into
commodity prices and earnings expectations.

Current parameters: {current}

Parameter ranges:
- lookback_days: 10-60 (momentum window)
- hold_days: 20-45 (holding period, target ~25-30 days)
- min_return: -0.05 to 0.05 (minimum momentum for fallback)
- heat_stress_threshold: 2-15 (min heat days to trigger)
- precip_deficit_threshold: -50 to -10 (% below normal precipitation)
- drought_min_score: 0.3-2.0 (min composite drought score to trigger)
- crop_decline_threshold: 1-5 (min weekly Good+Excellent decline in pp)

Recent results:
{results_text or '  No results yet.'}

Suggest 3 new parameter combinations. Return JSON array of 3 param dicts."""

    def get_universe(self, openbb_source=None) -> dict[str, str]:
        """Return eligible ticker universe, optionally expanded via OpenBB."""
        universe = dict(AG_TICKERS_FULL)
        if openbb_source and openbb_source.is_available():
            for industry in AG_EXPANSION_INDUSTRIES:
                result = openbb_source.fetch({
                    "method": "sector_tickers",
                    "industry": industry,
                })
                tickers = result.get("tickers", [])
                for t in tickers:
                    universe[t.lower()] = t
        blocked = set()  # Populated from config at engine level
        return {k: v for k, v in universe.items() if v not in blocked}

    @staticmethod
    def _check_crop_decline(usda_data: dict) -> float:
        """Check for week-over-week Good+Excellent decline across commodities.

        Returns the maximum decline in percentage points across all commodities.
        """
        if not usda_data:
            return 0.0

        crop_progress = usda_data.get("crop_progress", {})
        if not crop_progress:
            return 0.0

        max_decline = 0.0
        for commodity, weeks in crop_progress.items():
            if not isinstance(weeks, list) or len(weeks) < 2:
                continue
            # Compare last two weeks (most recent data)
            latest = weeks[-1]
            prior = weeks[-2]
            latest_ge = latest.get("good_pct", 0) + latest.get("excellent_pct", 0)
            prior_ge = prior.get("good_pct", 0) + prior.get("excellent_pct", 0)
            decline = prior_ge - latest_ge
            if decline > max_decline:
                max_decline = decline

        return max_decline
