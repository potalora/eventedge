"""Paper trade recorder and P&L tracker.

Records paper trades from paper-trade strategies, tracks them against
real prices, and computes performance metrics.
"""

from __future__ import annotations

import logging
import statistics
from typing import Any

import pandas as pd

from tradingagents.strategies.state.state import StateManager

logger = logging.getLogger(__name__)


class PaperTrader:
    """Records paper trades, tracks P&L against real prices."""

    def __init__(self, state: StateManager) -> None:
        self.state = state

    @staticmethod
    def _read_only_error() -> RuntimeError:
        return RuntimeError(
            "PaperTrader is read-only; accounting mutations must use PortfolioLedger"
        )

    def project(self) -> list[dict]:
        """Return the current ledger-backed compatibility trade view."""
        return self.state.load_paper_trades()

    def open_trade(
        self,
        strategy: str,
        ticker: str,
        direction: str,
        entry_price: float,
        entry_date: str,
        shares: int = 0,
        position_value: float = 0.0,
        rationale: str = "",
        params: dict | None = None,
        metadata: dict | None = None,
        vintage_id: str | None = None,
        is_exploration: bool = False,
    ) -> str:
        """Reject legacy trade creation; execution accounting is ledger-owned."""
        raise self._read_only_error()

    def check_exits(
        self,
        strategies: dict[str, Any],
        price_cache: dict[str, pd.DataFrame],
        current_date: str | None = None,
    ) -> list[dict]:
        """Reject legacy exit mutation; execution accounting is ledger-owned."""
        raise self._read_only_error()

    def close_trade(
        self,
        trade_id: str,
        exit_price: float,
        exit_date: str,
        exit_reason: str,
    ) -> None:
        """Reject legacy trade closure; execution accounting is ledger-owned."""
        raise self._read_only_error()

    def get_performance(self, strategy: str | None = None) -> dict:
        """Compute performance metrics for closed paper trades.

        Returns:
            Dict with win_rate, avg_pnl, sharpe, total_return, num_trades.
        """
        trades = self.state.load_paper_trades(strategy=strategy, status="closed")

        if not trades:
            return {
                "win_rate": 0.0,
                "avg_pnl": 0.0,
                "sharpe": 0.0,
                "total_return": 0.0,
                "num_trades": 0,
            }

        returns = []
        for t in trades:
            entry = t.get("entry_price", 0)
            exit_ = t.get("exit_price", 0)
            if entry > 0:
                pnl_pct = (exit_ - entry) / entry
                if t.get("direction") == "short":
                    pnl_pct = -pnl_pct
                returns.append(pnl_pct)

        if not returns:
            return {
                "win_rate": 0.0,
                "avg_pnl": 0.0,
                "sharpe": 0.0,
                "total_return": 0.0,
                "num_trades": len(trades),
            }

        winners = sum(1 for r in returns if r > 0)
        mean_r = statistics.mean(returns)
        std_r = statistics.stdev(returns) if len(returns) > 1 else 1.0

        return {
            "win_rate": winners / len(returns),
            "avg_pnl": mean_r,
            "sharpe": mean_r / std_r if std_r > 0 else 0.0,
            "total_return": sum(returns),
            "num_trades": len(trades),
        }

    def get_open_positions(self, strategy: str | None = None) -> list[dict]:
        """Get all open paper trade positions."""
        return self.state.load_paper_trades(strategy=strategy, status="open")

    def get_vintage_performance(self, vintage_id: str) -> dict:
        """Compute performance metrics for a specific vintage's completed trades.

        Returns dict with: vintage_id, num_trades, num_completed, win_rate,
        avg_pnl_pct, sharpe, total_return, avg_holding_days.
        """
        all_trades = self.state.load_paper_trades()
        vintage_trades = [t for t in all_trades if t.get("vintage_id") == vintage_id]
        closed = [t for t in vintage_trades if t.get("status") == "closed"]

        if not closed:
            return {
                "vintage_id": vintage_id,
                "num_trades": len(vintage_trades),
                "num_completed": 0,
                "win_rate": 0.0,
                "avg_pnl_pct": 0.0,
                "sharpe": 0.0,
                "total_return": 0.0,
                "avg_holding_days": 0.0,
            }

        returns = []
        holding_days_list = []
        for t in closed:
            if "pnl_pct" in t:
                pnl_pct = t["pnl_pct"]
            else:
                entry = t.get("entry_price", 0)
                exit_ = t.get("exit_price", 0)
                pnl_pct = (exit_ - entry) / entry if entry > 0 else 0.0
                if t.get("direction") == "short":
                    pnl_pct = -pnl_pct
            returns.append(pnl_pct)

            try:
                days = (
                    pd.Timestamp(t.get("exit_date", ""))
                    - pd.Timestamp(t.get("entry_date", ""))
                ).days
            except (ValueError, TypeError):
                days = 0
            holding_days_list.append(days)

        winners = sum(1 for r in returns if r > 0)
        mean_r = statistics.mean(returns)
        std_r = statistics.stdev(returns) if len(returns) > 1 else 0.0

        return {
            "vintage_id": vintage_id,
            "num_trades": len(vintage_trades),
            "num_completed": len(closed),
            "win_rate": winners / len(returns),
            "avg_pnl_pct": mean_r,
            "sharpe": mean_r / std_r if std_r > 0 else 0.0,
            "total_return": sum(returns),
            "avg_holding_days": statistics.mean(holding_days_list)
            if holding_days_list
            else 0.0,
        }

    def get_strategy_vintage_summary(self, strategy: str) -> list[dict]:
        """Return per-vintage statistics for a strategy.

        Returns list of dicts sorted by created_at descending (newest first).
        """
        all_trades = self.state.load_paper_trades(strategy=strategy)

        # Group trades by vintage_id
        by_vintage: dict[str, list[dict]] = {}
        for t in all_trades:
            vid = t.get("vintage_id")
            if vid is not None:
                by_vintage.setdefault(vid, []).append(t)

        summaries = []
        for vid, trades in by_vintage.items():
            closed = [t for t in trades if t.get("status") == "closed"]
            returns = []
            for t in closed:
                if "pnl_pct" in t:
                    returns.append(t["pnl_pct"])
                else:
                    entry = t.get("entry_price", 0)
                    exit_ = t.get("exit_price", 0)
                    pnl_pct = (exit_ - entry) / entry if entry > 0 else 0.0
                    if t.get("direction") == "short":
                        pnl_pct = -pnl_pct
                    returns.append(pnl_pct)

            win_rate = (
                sum(1 for r in returns if r > 0) / len(returns) if returns else 0.0
            )
            mean_r = statistics.mean(returns) if returns else 0.0
            std_r = statistics.stdev(returns) if len(returns) > 1 else 0.0

            # Get created_at from the earliest trade's opened_at
            created_at = min((t.get("opened_at", "") for t in trades), default="")

            summaries.append(
                {
                    "vintage_id": vid,
                    "strategy": strategy,
                    "num_trades": len(trades),
                    "num_completed": len(closed),
                    "win_rate": win_rate,
                    "sharpe": mean_r / std_r if std_r > 0 else 0.0,
                    "avg_pnl_pct": mean_r,
                    "created_at": created_at,
                    "is_exploration": trades[0].get("is_exploration", False),
                }
            )

        summaries.sort(key=lambda s: s["created_at"], reverse=True)
        return summaries
