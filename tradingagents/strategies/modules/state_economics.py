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
    "homebuilders_focused": "ITB",  # iShares US Home Construction ETF
    "broad_reit": "VNQ",          # Vanguard Real Estate ETF
    "semiconductors": "SOXX",     # iShares Semiconductor ETF
    "industrials": "XLI",         # Industrial Select Sector SPDR
    "real_estate_sector": "XLRE", # Real Estate Select Sector SPDR
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
    track = "paper_trade"
    data_sources = ["yfinance", "fred", "openbb"]

    def get_param_space(self) -> dict[str, tuple]:
        return {
            "lookback_days": (10, 60),
            "top_n": (1, 4),
            "rebalance_days": (20, 45),
        }

    def get_default_params(self) -> dict[str, Any]:
        return {
            "lookback_days": 21,
            "top_n": 2,
            "rebalance_days": 30,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for regional ETFs using economic indicators + momentum composite.

        Combines FRED economic indicators with ETF momentum for a
        composite signal. Falls back to pure momentum if FRED unavailable.
        """
        prices = data.get("yfinance", {}).get("prices", {})
        lookback = params.get("lookback_days", 21)
        top_n = params.get("top_n", 2)

        # Compute momentum scores
        etf_scores: list[tuple[str, str, float]] = []
        for name, ticker in REGIONAL_ETFS.items():
            df = prices.get(ticker)
            if df is None or df.empty:
                continue
            df = df.loc[:date]
            if len(df) < lookback:
                continue
            close = df["Close"]
            momentum = (close.iloc[-1] / close.iloc[-lookback]) - 1.0
            etf_scores.append((name, ticker, momentum))

        if not etf_scores:
            return []

        # Enrich with FRED economic indicator context
        fred_data = data.get("fred", {})
        fred_indicators = fred_data.get("data", {})
        econ_boost = {}
        if fred_indicators:
            # Check if unemployment is declining (bullish for regionals)
            unemployment = fred_indicators.get("UNRATE", {})
            if unemployment:
                values = sorted(unemployment.items())
                if len(values) >= 2:
                    recent = values[-1][1]
                    prior = values[-2][1]
                    if recent < prior:  # Declining unemployment
                        econ_boost["KRE"] = 0.02  # Boost regional banks
                        econ_boost["XRT"] = 0.01  # Boost retail
                        econ_boost["XHB"] = 0.01  # Boost homebuilders

            # Check initial claims (leading indicator)
            claims = fred_indicators.get("ICSA", {})
            if claims:
                values = sorted(claims.items())
                if len(values) >= 2:
                    recent = values[-1][1]
                    prior = values[-2][1]
                    if recent < prior:  # Declining claims = bullish
                        econ_boost["IWN"] = 0.02  # Small-cap value benefits most

        # Combine momentum + economic boost
        combined_scores = []
        for name, ticker, momentum in etf_scores:
            boost = econ_boost.get(ticker, 0.0)
            combined = momentum + boost
            combined_scores.append((name, ticker, combined, momentum, boost))

        combined_scores.sort(key=lambda x: x[2], reverse=True)

        candidates = []
        for name, ticker, combined, momentum, boost in combined_scores[:top_n]:
            # No hard min_return gate — let LLM judge context
            metadata = {
                "regional_sector": name,
                "trailing_return": momentum,
                "econ_boost": boost,
                "composite_score": combined,
            }

            # Add factor context from OpenBB
            openbb_data = data.get("openbb", {})
            factors = openbb_data.get("factors_fama_french", {})
            if isinstance(factors, dict) and "factors" in factors:
                metadata["fama_french"] = factors["factors"]

            candidates.append(
                Candidate(
                    ticker=ticker,
                    date=date,
                    direction="long",
                    score=combined,
                    metadata=metadata,
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
        rebalance_days = params.get("rebalance_days", 30)
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
regional ETFs (KRE, IWN, XRT, IYR, XHB, ITB, VNQ, SOXX, XLI, XLRE)
based on trailing momentum as a proxy for state-level economic conditions.

Investment horizon: 30 days. Rebalance window aligns with the portfolio
evaluation cycle. Regional economic divergence is persistent over 1-3 months.

Current parameters: {current}

Parameter ranges:
- lookback_days: 10-60 (trailing return window)
- top_n: 1-4 (number of top ETFs to hold)
- rebalance_days: 20-45 (rebalance frequency, target ~30 days)

Recent backtest results:
{results_text or '  No results yet.'}

Suggest 3 new parameter combinations. Return JSON array of 3 param dicts."""
