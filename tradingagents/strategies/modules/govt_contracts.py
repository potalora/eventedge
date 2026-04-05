from __future__ import annotations

import logging
from typing import Any

from .base import Candidate

logger = logging.getLogger(__name__)

# Known government contractor tickers for backtesting
# Maps common recipient keywords → tickers
CONTRACTOR_TICKERS = {
    "lockheed": "LMT",
    "raytheon": "RTX",
    "northrop": "NOC",
    "general dynamics": "GD",
    "boeing": "BA",
    "l3harris": "LHX",
    "bae systems": "BAESY",
    "leidos": "LDOS",
    "saic": "SAIC",
    "booz allen": "BAH",
    "parsons": "PSN",
    "kratos": "KTOS",
    "palantir": "PLTR",
    "caci": "CACI",
    # Defense tech/cloud
    "snowflake": "SNOW",
    "mongodb": "MDB",
    # Defense components
    "bwx": "BWX",
    "heico": "HEI",
    "transdigm": "TDG",
}

EXPANSION_INDUSTRIES = ["Aerospace & Defense", "Information Technology Services"]


class GovtContractsStrategy:
    """Buy small/mid-cap stocks winning large federal contracts.

    Academic basis: "Trading on Government Contracts" (2025, Economics Letters)
    shows positive cumulative returns following large contract announcements.
    TenderAlpha/FactSet find 5.4-7.1% annual alpha from the "Unexpected
    Government Receivables" signal. USAspending.gov publishes awards with a
    4-6 day lag, giving retail investors a window.

    Signal logic:
    1. Screen USAspending for recent large contracts.
    2. Resolve recipient names to tickers (using keyword matching).
    3. Filter by contract materiality (amount > threshold).
    4. Go long, hold 30-60 days for market to price in the revenue impact.

    For backtesting: uses the defense/govt contractor universe with yfinance
    prices, simulating entries on random dates since we don't have historical
    USAspending data in the backtest window.
    """

    name = "govt_contracts"
    track = "paper_trade"
    data_sources = ["yfinance", "usaspending", "openbb"]

    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_range"],
            "stop_loss_pct": (0.05, 0.15),
            "profit_target_pct": (0.05, 0.25),
            "max_positions": (2, 5),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_default"],
            "stop_loss_pct": 0.08,
            "profit_target_pct": 0.15,
            "max_positions": 3,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for government contractor opportunities.

        Uses USASpending contract data when available, falls back to
        defense contractor momentum. Enriches with OpenBB profile/estimates.
        """
        candidates = []

        # Try USASpending contract data first
        usaspending = data.get("usaspending", {})
        contracts = usaspending.get("data", {}).get("contracts", [])

        if contracts:
            for contract in contracts:
                recipient = (contract.get("recipient", "") or "").lower()
                amount = contract.get("amount", 0) or 0

                # Resolve recipient to ticker
                ticker = None
                for keyword, t in CONTRACTOR_TICKERS.items():
                    if keyword in recipient:
                        ticker = t
                        break

                if not ticker or amount < 10_000_000:  # $10M minimum
                    continue

                score = min(amount / 1_000_000_000, 1.0)  # Scale by $1B
                candidates.append(
                    Candidate(
                        ticker=ticker,
                        date=date,
                        direction="long",
                        score=score,
                        metadata={
                            "contractor": recipient,
                            "contract_amount": amount,
                            "source": "usaspending",
                        },
                    )
                )
        else:
            # Fallback: all defense contractors with available price data
            prices = data.get("yfinance", {}).get("prices", {})
            if prices:
                for name, ticker in CONTRACTOR_TICKERS.items():
                    df = prices.get(ticker)
                    if df is None or df.empty:
                        continue
                    df = df.loc[:date]
                    if len(df) < 30:
                        continue
                    close = df["Close"]
                    momentum = (close.iloc[-1] / close.iloc[-30]) - 1.0
                    candidates.append(
                        Candidate(
                            ticker=ticker,
                            date=date,
                            direction="long",
                            score=max(momentum, 0.01),  # Floor score at 1%
                            metadata={
                                "contractor": name,
                                "momentum_30d": momentum,
                                "source": "momentum_fallback",
                            },
                        )
                    )

        # Enrich with OpenBB data
        openbb_data = data.get("openbb", {})
        profile = openbb_data.get("profile", {})
        estimates = openbb_data.get("estimates", {})
        for c in candidates:
            if isinstance(profile, dict) and c.ticker in profile:
                c.metadata["sector"] = profile[c.ticker].get("sector", "")
            if isinstance(estimates, dict) and c.ticker in estimates:
                c.metadata["price_target_mean"] = estimates[c.ticker].get("price_target_mean")

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[: params.get("max_positions", 3)]

    def check_exit(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        holding_days: int,
        params: dict,
        data: dict,
    ) -> tuple[bool, str]:
        """Exit on hold period, profit target, or stop loss."""
        hold_days = params.get("hold_days", 30)
        stop_loss = params.get("stop_loss_pct", 0.08)
        profit_target = params.get("profit_target_pct", 0.15)

        if entry_price > 0:
            pnl_pct = (current_price - entry_price) / entry_price
            if pnl_pct >= profit_target:
                return True, "profit_target"
            if pnl_pct <= -stop_loss:
                return True, "stop_loss"

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

        return f"""You are optimizing a Government Contract Awards strategy that buys
defense/government contractor stocks after large contract announcements.

Investment horizon: 30 days. Government contract repricing takes 30-60 days
for mid/small-caps. The market underreacts to award announcements.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (holding period, target ~30 days)
- stop_loss_pct: 0.05-0.15 (stop loss percentage)
- profit_target_pct: 0.05-0.25 (take profit percentage)
- max_positions: 2-5 (max concurrent positions)

Recent backtest results:
{results_text or '  No results yet.'}

Suggest 3 new parameter combinations. Return JSON array of 3 param dicts."""

    def get_universe(self, openbb_source=None) -> dict[str, str]:
        """Return contractor universe, optionally expanded via OpenBB."""
        universe = dict(CONTRACTOR_TICKERS)
        if openbb_source and openbb_source.is_available():
            for industry in EXPANSION_INDUSTRIES:
                result = openbb_source.fetch({
                    "method": "sector_tickers",
                    "industry": industry,
                })
                tickers = result.get("tickers", [])
                for t in tickers:
                    universe[t.lower()] = t
        return universe
