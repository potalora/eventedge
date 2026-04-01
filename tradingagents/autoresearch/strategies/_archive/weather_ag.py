from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from .base import Candidate

logger = logging.getLogger(__name__)

# Agricultural commodity and agribusiness ETFs/stocks
AG_TICKERS = {
    "dba": "DBA",   # Invesco DB Agriculture Fund
    "weat": "WEAT", # Teucrium Wheat Fund
    "corn": "CORN", # Teucrium Corn Fund
    "moo": "MOO",   # VanEck Agribusiness ETF
}


class WeatherAgStrategy:
    """Trade agricultural ETFs based on seasonal patterns and momentum.

    Academic basis: "USDA Reports Affect the Stock Market, Too" (2024,
    J. Commodity Markets) and Energy Economics (2021) showing temperature
    anomalies significantly affect ag futures returns. Weather explains
    ~33% of crop yield variability.

    Signal logic:
    1. During growing season (April-September), monitor ag ETF momentum.
    2. Strong positive momentum = supply concerns driving prices up.
    3. Go long top ag ETFs during growing season.
    4. Outside season, stay flat.

    For backtesting: uses seasonal momentum in ag ETFs. True weather-driven
    trading requires NOAA/USDA data (Phase 2+).
    """

    name = "weather_ag"
    track = "backtest"
    data_sources = ["yfinance"]

    def get_param_space(self) -> dict[str, tuple]:
        return {
            "lookback_days": (10, 60),
            "season_start_month": (3, 5),
            "season_end_month": (8, 10),
            "hold_days": (10, 45),
            "min_return": (-0.05, 0.05),
        }

    def get_default_params(self) -> dict[str, Any]:
        return {
            "lookback_days": 21,
            "season_start_month": 4,
            "season_end_month": 9,
            "hold_days": 21,
            "min_return": 0.0,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for agricultural opportunities during growing season."""
        prices = data.get("yfinance", {}).get("prices", {})
        lookback = params.get("lookback_days", 21)
        season_start = params.get("season_start_month", 4)
        season_end = params.get("season_end_month", 9)
        min_return = params.get("min_return", 0.0)

        # Check if we're in growing season
        try:
            current_month = pd.Timestamp(date).month
        except ValueError:
            return []

        if not (season_start <= current_month <= season_end):
            return []

        ag_returns: list[tuple[str, str, float]] = []

        for name, ticker in AG_TICKERS.items():
            df = prices.get(ticker)
            if df is None or df.empty:
                continue

            df = df.loc[:date]
            if len(df) < lookback:
                continue

            close = df["Close"]
            trailing_return = (close.iloc[-1] / close.iloc[-lookback]) - 1.0
            ag_returns.append((name, ticker, trailing_return))

        if not ag_returns:
            return []

        # Sort by trailing return (strongest momentum first)
        ag_returns.sort(key=lambda x: x[2], reverse=True)

        candidates = []
        for name, ticker, ret in ag_returns[:2]:
            if ret < min_return:
                continue
            candidates.append(
                Candidate(
                    ticker=ticker,
                    date=date,
                    direction="long",
                    score=ret,
                    metadata={"commodity": name, "trailing_return": ret},
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
        hold_days = params.get("hold_days", 21)
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

        return f"""You are optimizing a Weather/Agriculture strategy that trades ag ETFs
(DBA, WEAT, CORN, MOO) during the US growing season (April-September)
based on momentum.

Current parameters: {current}

Parameter ranges:
- lookback_days: 10-60 (momentum window)
- season_start_month: 3-5 (growing season start)
- season_end_month: 8-10 (growing season end)
- hold_days: 10-45 (holding period)
- min_return: -0.05 to 0.05 (minimum momentum threshold)

Recent backtest results:
{results_text or '  No results yet.'}

Suggest 3 new parameter combinations. Consider:
- Agricultural prices are seasonal — planting (Apr-May) vs harvest (Aug-Sep)
- Weather events create sharp spikes — shorter lookbacks may capture better
- DBA is diversified; WEAT/CORN are concentrated single-commodity bets

Return JSON array of 3 param dicts."""
