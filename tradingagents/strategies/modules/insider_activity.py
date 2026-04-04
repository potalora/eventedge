from __future__ import annotations

import logging
from typing import Any

from .base import Candidate

logger = logging.getLogger(__name__)


class InsiderActivityStrategy:
    """Unified insider activity strategy (merges P4 insider_combo + P7 insider_10b5_1).

    Monitors EDGAR Form 4 filings for both buy clusters (bullish) and
    sell patterns / 10b5-1 red flags (bearish).

    Academic basis:
    - Lakonishok & Lee (2001, RFS): insider purchases predict returns.
    - Cohen et al. (2012, JoF): opportunistic insider buys predict 8.2%
      annual outperformance.
    - Jagolinzer (2009, Accounting Review), Henderson et al. (2023, JAR):
      10b5-1 plans are frequently manipulated; red flags predict negative returns.
    """

    name = "insider_activity"
    track = "paper_trade"
    data_sources = ["edgar", "yfinance", "openbb"]

    def get_param_space(self) -> dict[str, tuple]:
        return {
            "hold_days": (20, 45),
            "min_cluster_size": (2, 5),
            "min_sell_threshold": (2, 5),
            "min_conviction": (0.3, 0.8),
            "max_positions": (2, 5),
        }

    def get_default_params(self) -> dict[str, Any]:
        return {
            "hold_days": 25,
            "min_cluster_size": 2,
            "min_sell_threshold": 2,
            "min_conviction": 0.5,
            "max_positions": 3,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen Form 4 filings for insider buy clusters and sell red flags.

        Uses parsed transaction types from Form 4 XML to separate buys from
        sells deterministically. LLM provides context analysis only.
        """
        edgar_data = data.get("edgar", {})
        form4s = edgar_data.get("form4", {})

        if not form4s:
            return []

        candidates = []
        min_cluster = params.get("min_cluster_size", 3)
        min_sell = params.get("min_sell_threshold", 2)

        for ticker, filings in form4s.items():
            if not filings:
                continue

            # Separate buys from sells using parsed transaction_type
            buys = [f for f in filings if f.get("transaction_type") == "buy"]
            sells = [f for f in filings if f.get("transaction_type") == "sell"]

            # Skip if no parsed transaction data — metadata-only filings
            # aren't actionable (LLM can't distinguish buys from awards)
            if not buys and not sells:
                continue

            # Buy cluster signal: multiple insiders buying
            if len(buys) >= min_cluster:
                unique_buyers = {f.get("owner_name", "") for f in buys if f.get("owner_name")}
                officer_buys = [f for f in buys if f.get("is_officer")]
                total_shares = sum(f.get("shares", 0) for f in buys)
                total_value = sum(f.get("shares", 0) * f.get("price_per_share", 0) for f in buys)

                # Score: cluster size × officer bonus × open-market premium
                open_market = [f for f in buys if f.get("transaction_code") == "P"]
                score = len(buys) * (1.5 if officer_buys else 1.0) * (2.0 if open_market else 1.0)

                candidates.append(
                    Candidate(
                        ticker=ticker,
                        date=date,
                        direction="long",
                        score=float(score),
                        metadata={
                            "cluster_size": len(buys),
                            "unique_buyers": len(unique_buyers),
                            "officer_buys": len(officer_buys),
                            "total_shares": total_shares,
                            "total_value": round(total_value, 2),
                            "open_market_buys": len(open_market),
                            "filings": buys[:5],
                            "needs_llm_analysis": True,
                            "analysis_type": "insider_activity",
                            "cluster_type": "buy_cluster",
                        },
                    )
                )

            # Sell pattern / 10b5-1 red flag signal
            if len(sells) >= min_sell:
                unique_sellers = {f.get("owner_name", "") for f in sells if f.get("owner_name")}
                officer_sells = [f for f in sells if f.get("is_officer")]
                total_sell_shares = sum(f.get("shares", 0) for f in sells)

                score = len(sells) * (1.5 if officer_sells else 1.0)

                candidates.append(
                    Candidate(
                        ticker=ticker,
                        date=date,
                        direction="short",
                        score=float(score),
                        metadata={
                            "sell_count": len(sells),
                            "unique_sellers": len(unique_sellers),
                            "officer_sells": len(officer_sells),
                            "total_sell_shares": total_sell_shares,
                            "filings": sells[:5],
                            "needs_llm_analysis": True,
                            "analysis_type": "insider_activity",
                            "cluster_type": "sell_pattern",
                        },
                    )
                )

        # Enrich with OpenBB insider data for officer titles and sector
        openbb_data = data.get("openbb", {})
        insider_data = openbb_data.get("insider_trading", {})
        profile_data = openbb_data.get("profile", {})
        if isinstance(insider_data, dict) or isinstance(profile_data, dict):
            for candidate in candidates:
                ticker = candidate.ticker
                # Officer title weighting
                if isinstance(insider_data, dict) and ticker in insider_data:
                    for obb_trade in insider_data[ticker].get("trades", []):
                        title = (obb_trade.get("title", "") or "").upper()
                        if any(t in title for t in ["CEO", "CFO", "COO", "CTO", "PRESIDENT"]):
                            candidate.score = min(candidate.score * 1.3, 1.0)
                            candidate.metadata["officer_title"] = obb_trade.get("title", "")
                            break
                        elif "DIRECTOR" in title:
                            candidate.metadata["officer_title"] = obb_trade.get("title", "")
                            break
                # Sector context
                if isinstance(profile_data, dict) and ticker in profile_data:
                    candidate.metadata["sector"] = profile_data[ticker].get("sector", "")

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
        """Exit on hold period or stop loss (10%)."""
        hold_days = params.get("hold_days", 25)
        if holding_days >= hold_days:
            return True, "hold_period"

        if entry_price > 0:
            pnl_pct = (current_price - entry_price) / entry_price
            if pnl_pct <= -0.10:
                return True, "stop_loss"

        return False, ""

    def build_propose_prompt(self, context: dict) -> str:
        current = context.get("current_params", self.get_default_params())
        return f"""You are optimizing an Insider Activity strategy that monitors Form 4
filings for two signal types:
1. Buy clusters: multiple insiders buying the same stock (bullish)
2. Sell patterns / 10b5-1 red flags: suspicious insider selling (bearish)

Investment horizon: 30 days. Insider signal decay is ~30 days in academic
literature (Lakonishok & Lee 2001). Target 25-30 day holds.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (target ~25-30 days)
- min_cluster_size: 2-5 (minimum insiders buying for long signal)
- min_sell_threshold: 2-5 (minimum insider sells for short signal)
- min_conviction: 0.3-0.8
- max_positions: 2-5

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
