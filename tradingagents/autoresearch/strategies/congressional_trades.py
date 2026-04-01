from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from .base import Candidate

logger = logging.getLogger(__name__)

# Dollar amount buckets from congressional disclosures (ascending)
AMOUNT_BUCKETS = [
    "$1,001 - $15,000",
    "$15,001 - $50,000",
    "$50,001 - $100,000",
    "$100,001 - $250,000",
    "$250,001 - $500,000",
    "$500,001 - $1,000,000",
    "$1,000,001 - $5,000,000",
    "$5,000,001 - $25,000,000",
    "$25,000,001 - $50,000,000",
]

# Map bucket string to a 1-based tier for scoring
BUCKET_TIER = {b: i + 1 for i, b in enumerate(AMOUNT_BUCKETS)}


class CongressionalTradesStrategy:
    """P11: Follow congressional stock purchases.

    Academic basis: Eggers & Hainmueller (2014, APSR) show members of
    Congress earn abnormal returns of 6-9% annually. Informational
    advantages stem from committee assignments, legislative oversight,
    and advance knowledge of policy changes. Insider purchases are more
    informative than sales (Ziobrowski et al. 2004, JFP&QA).

    Signal logic:
    1. Monitor congressional trade disclosures for purchases.
    2. Score by dollar amount bucket (higher = stronger conviction).
    3. Cluster signal: multiple members buying the same stock.
    4. Go long on high-conviction cluster buys.
    """

    name = "congressional_trades"
    track = "paper_trade"
    data_sources = ["congress", "yfinance"]

    def get_param_space(self) -> dict[str, tuple]:
        return {
            "hold_days": (20, 60),
            "min_amount_bucket": (1, 4),
            "max_positions": (2, 5),
            "min_members": (1, 3),
        }

    def get_default_params(self) -> dict[str, Any]:
        return {
            "hold_days": 25,
            "min_amount_bucket": 2,
            "max_positions": 3,
            "min_members": 1,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen congressional trade disclosures for purchase clusters."""
        congress_data = data.get("congress", {})
        trades = congress_data.get("recent_trades", [])

        if not trades:
            return []

        min_bucket = params.get("min_amount_bucket", 2)
        min_members = params.get("min_members", 1)
        max_positions = params.get("max_positions", 3)

        # Group purchases by ticker
        ticker_buys: dict[str, list[dict]] = defaultdict(list)

        for trade in trades:
            tx_type = (trade.get("transaction_type") or "").lower()
            if tx_type not in ("buy", "purchase") and "purchase" not in tx_type:
                continue

            ticker = (trade.get("ticker") or "").upper().strip()
            if not ticker or ticker == "--":
                continue

            amount = trade.get("amount", "")
            tier = BUCKET_TIER.get(amount, 0)
            if tier < min_bucket:
                continue

            ticker_buys[ticker].append({
                "member": trade.get("representative") or trade.get("senator", "Unknown"),
                "chamber": trade.get("chamber", "unknown"),
                "amount": amount,
                "tier": tier,
                "date": trade.get("transaction_date", ""),
            })

        # Build candidates from tickers with enough unique members
        candidates = []
        for ticker, buys in ticker_buys.items():
            unique_members = {b["member"] for b in buys}
            if len(unique_members) < min_members:
                continue

            # Score: sum of tiers * cluster multiplier
            total_tier = sum(b["tier"] for b in buys)
            cluster_bonus = len(unique_members)
            score = total_tier * cluster_bonus

            candidates.append(
                Candidate(
                    ticker=ticker,
                    date=date,
                    direction="long",
                    score=float(score),
                    metadata={
                        "num_members": len(unique_members),
                        "num_trades": len(buys),
                        "members": list(unique_members)[:5],
                        "max_tier": max(b["tier"] for b in buys),
                        "needs_llm_analysis": False,
                    },
                )
            )

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:max_positions]

    def check_exit(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        holding_days: int,
        params: dict,
        data: dict,
    ) -> tuple[bool, str]:
        """Exit on hold period or stop loss."""
        hold_days = params.get("hold_days", 30)
        if holding_days >= hold_days:
            return True, "hold_period"

        if entry_price > 0:
            pnl_pct = (current_price - entry_price) / entry_price
            if pnl_pct <= -0.08:
                return True, "stop_loss"

        return False, ""

    def build_propose_prompt(self, context: dict) -> str:
        current = context.get("current_params", self.get_default_params())
        return f"""You are optimizing a Congressional Stock Trades strategy that follows
purchase disclosures from US Congress members.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-60 (holding period after entry)
- min_amount_bucket: 1-4 (minimum dollar tier, 1=$1K-$15K, 4=$100K-$250K)
- max_positions: 2-5 (maximum concurrent positions)
- min_members: 1-3 (minimum unique members buying same stock)

Suggest 3 parameter combinations. Consider:
- Higher min_amount_bucket = fewer but higher-conviction signals
- Cluster buys (min_members >= 2) are the strongest signal
- Congress members often trade ahead of legislation by 30-60 days

Return JSON array of 3 param dicts."""
