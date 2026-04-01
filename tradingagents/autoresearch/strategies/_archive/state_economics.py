from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from .base import Candidate

logger = logging.getLogger(__name__)

# Regional bank / retailer ETFs as proxies for state economic conditions
REGIONAL_ETFS = {
    "regional_banks": "KRE",      # SPDR S&P Regional Banking ETF
    "small_cap_value": "IWN",     # iShares Russell 2000 Value ETF
    "retail": "XRT",              # SPDR S&P Retail ETF
    "real_estate": "IYR",         # iShares US Real Estate ETF
    "homebuilders": "XHB",        # SPDR S&P Homebuilders ETF
}


class StateEconomicsStrategy:
    """Trade regional ETFs using cross-sector momentum as a proxy for
    state-level economic divergence.

    Academic basis: Korniotis & Kumar (2013, JoF) show state economic
    conditions predict local stock returns. Addoum et al. (2017) find
    predictability strongest for difficult-to-arbitrage firms. Composite
    leading indicator quintile spread: 1.43%/month.

    Signal logic (backtest proxy):
    1. Track regional bank, retail, homebuilder ETF momentum.
    2. Regional economic strength → local companies outperform.
    3. Go long top-performing regional ETFs.
    4. Rebalance periodically.

    True state-level indicator trading requires FRED state data (Phase 2+).
    """

    name = "state_economics"
    track = "backtest"
    data_sources = ["yfinance"]

    def get_param_space(self) -> dict[str, tuple]:
        return {
            "lookback_days": (10, 60),
            "top_n": (1, 4),
            "rebalance_days": (5, 30),
            "min_return": (-0.05, 0.05),
        }

    def get_default_params(self) -> dict[str, Any]:
        return {
            "lookback_days": 21,
            "top_n": 2,
            "rebalance_days": 14,
            "min_return": 0.0,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for top-performing regional ETFs by trailing return."""
        prices = data.get("yfinance", {}).get("prices", {})
        lookback = params.get("lookback_days", 21)
        top_n = params.get("top_n", 2)
        min_return = params.get("min_return", 0.0)

        regional_returns: list[tuple[str, str, float]] = []

        for name, ticker in REGIONAL_ETFS.items():
            df = prices.get(ticker)
            if df is None or df.empty:
                continue

            df = df.loc[:date]
            if len(df) < lookback:
                continue

            close = df["Close"]
            trailing_return = (close.iloc[-1] / close.iloc[-lookback]) - 1.0
            regional_returns.append((name, ticker, trailing_return))

        if not regional_returns:
            return []

        regional_returns.sort(key=lambda x: x[2], reverse=True)

        candidates = []
        for name, ticker, ret in regional_returns[:top_n]:
            if ret < min_return:
                continue
            candidates.append(
                Candidate(
                    ticker=ticker,
                    date=date,
                    direction="long",
                    score=ret,
                    metadata={"regional_sector": name, "trailing_return": ret},
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
        """Exit on rebalance schedule."""
        rebalance_days = params.get("rebalance_days", 14)
        if holding_days >= rebalance_days:
            return True, "rebalance"
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

        return f"""You are optimizing a State Economics strategy that rotates among
regional ETFs (KRE, IWN, XRT, IYR, XHB) based on trailing momentum
as a proxy for state-level economic conditions.

Current parameters: {current}

Parameter ranges:
- lookback_days: 10-60 (trailing return window)
- top_n: 1-4 (number of top ETFs to hold)
- rebalance_days: 5-30 (rebalance frequency)
- min_return: -0.05 to 0.05 (minimum trailing return threshold)

Recent backtest results:
{results_text or '  No results yet.'}

Suggest 3 new parameter combinations. Consider:
- Regional banks (KRE) are highly sensitive to local economic conditions
- Homebuilders (XHB) lead the cycle — housing data is forward-looking
- Shorter lookbacks capture recent economic shifts
- More concentrated bets (fewer ETFs) amplify the signal

Return JSON array of 3 param dicts."""
