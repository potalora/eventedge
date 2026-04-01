"""P6: Supply Chain Disruption -- multi-hop impact assessment.

Detects supply chain disruptions from news and maps impact to affected
companies via peer/supplier/customer relationships.

Academic basis: Cohen & Frazzini (2008, JoF) show that supply chain
links create predictable return momentum. Disruptions propagate with
a 1-3 week delay to downstream firms.

Data sources: Finnhub (company news + peer relationships).
"""
from __future__ import annotations

import logging
from typing import Any

from .base import Candidate

logger = logging.getLogger(__name__)

# Keywords that indicate supply chain disruption
DISRUPTION_KEYWORDS = [
    "supply chain", "shortage", "disruption", "recall", "force majeure",
    "factory shutdown", "port closure", "embargo", "tariff", "sanctions",
    "logistics", "backlog", "inventory shortage", "chip shortage",
]


class SupplyChainStrategy:
    """Paper-trade strategy for supply chain disruption detection."""

    name = "supply_chain"
    track = "paper_trade"
    data_sources = ["finnhub", "yfinance"]

    def get_param_space(self) -> dict[str, tuple]:
        return {
            "hold_days": (10, 45),
            "min_conviction": (0.3, 0.8),
            "max_positions": (2, 6),
            "news_lookback_days": (3, 14),
            "hop_depth": (1, 3),
        }

    def get_default_params(self) -> dict[str, Any]:
        return {
            "hold_days": 10,
            "min_conviction": 0.5,
            "max_positions": 4,
            "news_lookback_days": 7,
            "hop_depth": 2,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for supply chain disruption signals.

        Expects data["finnhub"]["disruption_news"] = list of dicts with
        {symbol, headline, summary, ...} and
        data["finnhub"]["supply_chains"] = dict of {symbol: [peer_tickers]}.
        """
        finnhub_data = data.get("finnhub", {})
        news = finnhub_data.get("disruption_news", [])
        chains = finnhub_data.get("supply_chains", {})

        if not news:
            return []

        candidates = []
        for article in news:
            symbol = article.get("symbol", "")
            headline = article.get("headline", "").lower()
            summary = article.get("summary", "").lower()

            # Check for disruption keywords
            text = headline + " " + summary
            is_disruption = any(kw in text for kw in DISRUPTION_KEYWORDS)
            if not is_disruption:
                continue

            # Get downstream peers/customers that may be affected
            peers = chains.get(symbol, [])

            candidates.append(
                Candidate(
                    ticker=symbol,
                    date=date,
                    direction="short",  # Disruption = short affected companies
                    score=0.6,
                    metadata={
                        "headline": article.get("headline", ""),
                        "summary": article.get("summary", "")[:1000],
                        "source": article.get("source", ""),
                        "affected_peers": peers[:10],
                        "needs_llm_analysis": True,
                        "analysis_type": "supply_chain",
                    },
                )
            )

        return candidates[: params.get("max_positions", 4)]

    def check_exit(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        holding_days: int,
        params: dict,
        data: dict,
    ) -> tuple[bool, str]:
        hold_days = params.get("hold_days", 20)
        if holding_days >= hold_days:
            return True, "hold_period"
        # Take profit at 8%
        pnl_pct = (current_price - entry_price) / entry_price
        if abs(pnl_pct) > 0.08:
            return True, "take_profit"
        return False, ""

    def build_propose_prompt(self, context: dict) -> str:
        current = context.get("current_params", self.get_default_params())
        return f"""You are optimizing a Supply Chain Disruption strategy that detects
disruption events and maps multi-hop impacts to affected companies.

Current parameters: {current}

Parameter ranges:
- hold_days: 10-45 (supply chain effects propagate 1-3 weeks)
- min_conviction: 0.3-0.8
- max_positions: 2-6
- news_lookback_days: 3-14
- hop_depth: 1-3 (how many supply chain hops to trace)

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
