"""Paper trade recorder and P&L tracker.

Records paper trades from paper-trade strategies, tracks them against
real prices, and computes performance metrics.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime
from typing import Any

import pandas as pd

from tradingagents.autoresearch.state import StateManager

logger = logging.getLogger(__name__)


class PaperTrader:
    """Records paper trades, tracks P&L against real prices."""

    def __init__(self, state: StateManager) -> None:
        self.state = state

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
        """Open a new paper trade and return its trade_id."""
        trade = {
            "strategy": strategy,
            "ticker": ticker,
            "direction": direction,
            "entry_price": entry_price,
            "entry_date": entry_date,
            "shares": shares,
            "position_value": position_value,
            "rationale": rationale,
            "params": params or {},
            "metadata": metadata or {},
            "status": "open",
            "vintage_id": vintage_id,
            "is_exploration": is_exploration,
        }
        self.state.save_paper_trade(trade)
        # Return the trade_id that was assigned
        trades = self.state.load_paper_trades(strategy=strategy, status="open")
        for t in reversed(trades):
            if t.get("ticker") == ticker and t.get("entry_date") == entry_date:
                return t["trade_id"]
        return ""

    def check_exits(
        self,
        strategies: dict[str, Any],
        price_cache: dict[str, pd.DataFrame],
        current_date: str | None = None,
    ) -> list[dict]:
        """Check all open paper trades against current prices and exit rules.

        Args:
            strategies: Dict of strategy_name -> strategy module instance.
            price_cache: Dict of ticker -> price DataFrame.
            current_date: Date to evaluate (default: today).

        Returns:
            List of trades that were closed.
        """
        if current_date is None:
            current_date = datetime.now().strftime("%Y-%m-%d")

        open_trades = self.state.load_paper_trades(status="open")
        closed = []

        for trade in open_trades:
            ticker = trade.get("ticker", "")
            strategy_name = trade.get("strategy", "")
            entry_price = trade.get("entry_price", 0)
            entry_date = trade.get("entry_date", "")

            # Get current price
            price_df = price_cache.get(ticker)
            if price_df is None or price_df.empty:
                continue

            try:
                current_row = price_df.loc[:current_date]
                if current_row.empty:
                    continue
                current_price = float(current_row["Close"].iloc[-1])
            except (KeyError, IndexError):
                continue

            # Calculate holding days
            try:
                holding_days = (
                    pd.Timestamp(current_date) - pd.Timestamp(entry_date)
                ).days
            except (ValueError, TypeError):
                holding_days = 0

            # Check exit rules via strategy module
            strategy = strategies.get(strategy_name)
            if strategy is None:
                continue

            params = trade.get("params", strategy.get_default_params())
            should_exit, reason = strategy.check_exit(
                ticker=ticker,
                entry_price=entry_price,
                current_price=current_price,
                holding_days=holding_days,
                params=params,
                data={},
            )

            if should_exit:
                self.close_trade(
                    trade_id=trade["trade_id"],
                    exit_price=current_price,
                    exit_date=current_date,
                    exit_reason=reason,
                )
                closed.append({
                    "trade_id": trade["trade_id"],
                    "strategy": strategy_name,
                    "ticker": ticker,
                    "exit_reason": reason,
                    "pnl_pct": (current_price - entry_price) / entry_price
                    if entry_price > 0
                    else 0,
                })

        return closed

    def close_trade(
        self,
        trade_id: str,
        exit_price: float,
        exit_date: str,
        exit_reason: str,
    ) -> None:
        """Close a paper trade, computing PnL fields for the learning loop."""
        # Look up the trade to compute PnL
        all_trades = self.state.load_paper_trades(status="open")
        trade = next((t for t in all_trades if t.get("trade_id") == trade_id), None)

        pnl_pct = 0.0
        pnl = 0.0
        if trade:
            entry_price = trade.get("entry_price", 0)
            shares = trade.get("shares", 0)
            direction = trade.get("direction", "long")
            if entry_price > 0:
                raw_pct = (exit_price - entry_price) / entry_price
                pnl_pct = -raw_pct if direction == "short" else raw_pct
                pnl = pnl_pct * entry_price * shares

        self.state.update_paper_trade(
            trade_id,
            {
                "status": "closed",
                "exit_price": exit_price,
                "exit_date": exit_date,
                "exit_reason": exit_reason,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 6),
                "closed_at": datetime.now().isoformat(),
            },
        )
        logger.info(
            "Closed paper trade %s: reason=%s pnl=%.2f (%.2f%%)",
            trade_id, exit_reason, pnl, pnl_pct * 100,
        )

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
            "avg_holding_days": statistics.mean(holding_days_list) if holding_days_list else 0.0,
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

            win_rate = sum(1 for r in returns if r > 0) / len(returns) if returns else 0.0
            mean_r = statistics.mean(returns) if returns else 0.0
            std_r = statistics.stdev(returns) if len(returns) > 1 else 0.0

            # Get created_at from the earliest trade's opened_at
            created_at = min(
                (t.get("opened_at", "") for t in trades), default=""
            )

            summaries.append({
                "vintage_id": vid,
                "strategy": strategy,
                "num_trades": len(trades),
                "num_completed": len(closed),
                "win_rate": win_rate,
                "sharpe": mean_r / std_r if std_r > 0 else 0.0,
                "avg_pnl_pct": mean_r,
                "created_at": created_at,
                "is_exploration": trades[0].get("is_exploration", False),
            })

        summaries.sort(key=lambda s: s["created_at"], reverse=True)
        return summaries
