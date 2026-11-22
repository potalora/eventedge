"""Source-company disruption screening with explicit peer context.

Peer relationships do not establish supplier/customer edges or multihop impact.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .base import Candidate
from .admission import admit_candidates

logger = logging.getLogger(__name__)

# Keywords that indicate supply chain disruption
DISRUPTION_KEYWORDS = [
    "supply chain",
    "shortage",
    "disruption",
    "recall",
    "force majeure",
    "factory shutdown",
    "port closure",
    "embargo",
    "tariff",
    "sanctions",
    "logistics",
    "backlog",
    "inventory shortage",
    "chip shortage",
    "port congestion",
    "inventory",
    "sanction",
    "trade restriction",
    "export ban",
    "import duty",
    "raw material",
]


# Broader liquid industrial/tech/semiconductor/logistics/supply-chain bellwethers.
# Configurable via strategy params["supply_chain_universe"].
DEFAULT_SUPPLY_CHAIN_UNIVERSE: tuple[str, ...] = (
    "AAPL",
    "TSLA",
    "NVDA",
    "AMZN",
    "BA",
    "CAT",
    "DE",
    "AVGO",
    "AMD",
    "TSM",
    "INTC",
    "QCOM",
    "TXN",
    "HON",
    "GE",
    "UNP",
    "UPS",
    "FDX",
    "F",
    "GM",
    "LMT",
    "RTX",
    "XOM",
    "FCX",
    "NUE",
)


def get_supply_chain_universe(params: dict[str, Any] | None = None) -> list[str]:
    """Return configured supply-chain monitoring tickers (or the default bellwether universe)."""
    configured = (params or {}).get("supply_chain_universe")
    if isinstance(configured, (list, tuple)):
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in configured:
            ticker = str(raw or "").strip().upper()
            if ticker and ticker not in seen:
                seen.add(ticker)
                cleaned.append(ticker)
        if cleaned:
            return cleaned
    return list(DEFAULT_SUPPLY_CHAIN_UNIVERSE)


class SupplyChainStrategy:
    """Paper-trade strategy for supply chain disruption detection."""

    name = "supply_chain"
    track = "paper_trade"
    data_sources = ["finnhub", "yfinance"]

    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            HORIZON_PARAMS,
        )

        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_range"],
            "max_positions": (2, 6),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            HORIZON_PARAMS,
        )

        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_default"],
            "max_positions": 4,
            "supply_chain_universe": list(DEFAULT_SUPPLY_CHAIN_UNIVERSE),
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

        allowed_universe = (
            set(get_supply_chain_universe(params))
            if "supply_chain_universe" in params
            else None
        )

        candidates = []
        for article in news:
            symbol = str(article.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            if allowed_universe is not None and symbol not in allowed_universe:
                continue
            headline = article.get("headline", "").lower()
            summary = article.get("summary", "").lower()

            # Check for disruption keywords
            text = headline + " " + summary
            is_disruption = any(kw in text for kw in DISRUPTION_KEYWORDS)
            if not is_disruption:
                continue

            article_id = article.get("id") or article.get("article_id", "")
            article_url = article.get("url", "")
            published = article.get("published_at") or article.get("datetime")
            if isinstance(published, (int, float)):
                published = datetime.fromtimestamp(
                    published, tz=timezone.utc
                ).isoformat()
            elif isinstance(published, str) and published.isdigit():
                published = datetime.fromtimestamp(
                    int(published), tz=timezone.utc
                ).isoformat()
            if not (article_id or article_url) and not (
                article.get("source") and article.get("headline") and published
            ):
                continue

            # Get downstream peers/customers that may be affected
            peers = chains.get(symbol, [])

            candidates.append(
                Candidate(
                    ticker=symbol,
                    date=date,
                    direction="short",  # Disruption = short affected companies
                    score=0.6,
                    raw_signal_score=0.6,
                    metadata={
                        "headline": article.get("headline", ""),
                        "article_id": article_id,
                        "url": article_url,
                        **({"published_at": published} if published else {}),
                        "summary": article.get("summary", "")[:1000],
                        "source": article.get("source", ""),
                        "affected_peers": peers[:10],
                        "needs_llm_analysis": True,
                        "analysis_type": "supply_chain",
                    },
                )
            )

        return admit_candidates(
            self.name,
            candidates,
            params.get("analysis_budget", params.get("max_positions", 4)),
        )

    def check_exit(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        holding_days: int,
        params: dict,
        data: dict,
        direction: str = "long",
    ) -> tuple[bool, str]:
        hold_days = params.get("hold_days", 22)
        if holding_days >= hold_days:
            return True, "hold_period"
        take_profit_pct = float(params.get("take_profit_pct", 0.08))
        stop_loss_pct = float(params.get("stop_loss_pct", 0.08))
        if entry_price > 0:
            pnl_pct = (
                (1 if direction == "long" else -1)
                * (current_price - entry_price)
                / entry_price
            )
            if pnl_pct >= take_profit_pct:
                return True, "take_profit"
            if pnl_pct <= -stop_loss_pct:
                return True, "stop_loss"
        return False, ""

    def build_propose_prompt(self, context: dict) -> str:
        current = context.get("current_params", self.get_default_params())
        return f"""You are optimizing a Supply Chain Disruption strategy that detects
disruption events for the source company with explicitly labeled peer context.

Investment horizon: 30 days. Supply disruptions take weeks to price across
the chain. The initial reaction captures only part of the move.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (target ~22-25 days for disruption propagation)
- max_positions: 2-6

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
