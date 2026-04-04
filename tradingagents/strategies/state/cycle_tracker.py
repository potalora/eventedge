"""30-day cycle evaluation tracker for portfolio performance.

Observation-only component that produces snapshots at 30-day boundaries
aligned to generation start date. Does not force exits or modify strategy
behavior.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

CYCLE_LENGTH = 30


class CycleTracker:
    """Track portfolio performance in 30-day cycles aligned to generation start."""

    def __init__(self, gen_start_date: str, state_dir: str) -> None:
        self._gen_start = pd.Timestamp(gen_start_date)
        self._state_dir = state_dir
        self._cycles_dir = os.path.join(state_dir, "cycles")
        os.makedirs(self._cycles_dir, exist_ok=True)

        # Running metrics for the current cycle
        self._daily_values: list[tuple[str, float]] = []
        self._cycle_start_value: float | None = None

    def current_cycle(self, trading_date: str) -> int:
        """Return the current cycle number (1-indexed)."""
        days = (pd.Timestamp(trading_date) - self._gen_start).days
        return days // CYCLE_LENGTH + 1

    def days_remaining(self, trading_date: str) -> int:
        """Days remaining in the current cycle."""
        days = (pd.Timestamp(trading_date) - self._gen_start).days
        days_into_cycle = days % CYCLE_LENGTH
        return CYCLE_LENGTH - days_into_cycle

    def is_boundary(self, trading_date: str) -> bool:
        """True if trading_date is the last day of a cycle."""
        return self.days_remaining(trading_date) == 1

    def update_daily(
        self, trading_date: str, positions: list, portfolio_value: float
    ) -> None:
        """Called after each trading day. Updates running metrics."""
        if self._cycle_start_value is None:
            self._cycle_start_value = portfolio_value
        self._daily_values.append((trading_date, portfolio_value))

    def snapshot_cycle(
        self,
        cycle_number: int,
        positions: list,
        closed_trades: list,
        portfolio_value: float,
    ) -> dict[str, Any]:
        """Generate full cycle evaluation and persist to disk."""
        start_value = self._cycle_start_value or portfolio_value
        cycle_start_date = self._gen_start + pd.Timedelta(days=(cycle_number - 1) * CYCLE_LENGTH)
        cycle_end_date = self._gen_start + pd.Timedelta(days=cycle_number * CYCLE_LENGTH - 1)

        realized_pnl = sum(t.get("pnl", 0.0) for t in closed_trades)
        unrealized_pnl = portfolio_value - start_value - realized_pnl

        # Capital utilization from daily values
        daily_vals = [v for _, v in self._daily_values]
        avg_util = 0.0
        peak_util = 0.0
        if daily_vals and start_value > 0:
            deployed = [start_value - v for v in daily_vals]
            # Rough proxy: deviation from starting capital
            avg_util = sum(abs(d) for d in deployed) / len(deployed) / start_value * 100
            peak_util = max(abs(d) for d in deployed) / start_value * 100

        # Strategy breakdown
        strategy_stats: dict[str, dict] = defaultdict(
            lambda: {"signals": 0, "traded": 0, "pnl": 0.0, "hold_days": []}
        )
        for t in closed_trades:
            s = t.get("strategy", "unknown")
            strategy_stats[s]["traded"] += 1
            strategy_stats[s]["pnl"] += t.get("pnl", 0.0)
            if "holding_days" in t:
                strategy_stats[s]["hold_days"].append(t["holding_days"])

        breakdown = {}
        for strat, stats in strategy_stats.items():
            hold_days_list = stats["hold_days"]
            avg_hold = sum(hold_days_list) / len(hold_days_list) if hold_days_list else 0
            hit_rate = (
                sum(1 for t in closed_trades if t.get("strategy") == strat and t.get("pnl", 0) > 0)
                / stats["traded"]
                if stats["traded"] > 0
                else 0.0
            )
            breakdown[strat] = {
                "signals": stats["signals"],
                "traded": stats["traded"],
                "pnl": round(stats["pnl"], 2),
                "hit_rate": round(hit_rate, 2),
                "avg_hold_days": round(avg_hold, 1),
            }

        snapshot = {
            "cycle": cycle_number,
            "start_date": cycle_start_date.strftime("%Y-%m-%d"),
            "end_date": cycle_end_date.strftime("%Y-%m-%d"),
            "portfolio_value_start": round(start_value, 2),
            "portfolio_value_end": round(portfolio_value, 2),
            "cycle_return_pct": round(
                (portfolio_value - start_value) / start_value * 100
                if start_value > 0
                else 0.0,
                2,
            ),
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "positions_opened": len(closed_trades) + len(positions),
            "positions_closed": len(closed_trades),
            "positions_open_at_end": len(positions),
            "capital_utilization_avg_pct": round(avg_util, 1),
            "capital_utilization_peak_pct": round(peak_util, 1),
            "strategy_breakdown": breakdown,
        }

        # Persist
        cycle_path = os.path.join(self._cycles_dir, f"cycle_{cycle_number:03d}.json")
        with open(cycle_path, "w") as f:
            json.dump(snapshot, f, indent=2)
        logger.info("Cycle %d snapshot saved to %s", cycle_number, cycle_path)

        # Reset running metrics for next cycle
        self._daily_values = []
        self._cycle_start_value = portfolio_value

        return snapshot
