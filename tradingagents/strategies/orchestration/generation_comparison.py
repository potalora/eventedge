"""Cross-generation comparison tool for paper trading trials.

Compares metrics across multiple paper trading generations, where each
generation has control/ and adaptive/ cohort subdirectories.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tradingagents.strategies.learning.signal_journal import SignalJournal
from tradingagents.strategies.state.state import StateManager

logger = logging.getLogger(__name__)

COHORT_NAMES = ("control", "adaptive")


@dataclass
class GenerationInfo:
    """Minimal info needed for comparison (mirrors generation_manager.GenerationInfo)."""

    gen_id: str
    state_dir: str
    description: str
    created_at: str
    status: str
    git_commit: str = ""


class GenerationComparison:
    """Compare metrics across multiple paper trading generations."""

    def __init__(self, generations: list[GenerationInfo]) -> None:
        self._generations = generations

    # ------------------------------------------------------------------
    # Core comparison
    # ------------------------------------------------------------------

    def compare(self) -> dict:
        """Per-generation, per-cohort metrics.

        Returns:
            {
                "generations": {
                    "gen_001": {
                        "description": "...",
                        "created_at": "...",
                        "status": "active",
                        "git_commit": "abc123",
                        "cohorts": {
                            "control": { ... },
                            "adaptive": { ... }
                        }
                    },
                    ...
                }
            }
        """
        result: dict[str, dict[str, Any]] = {}

        for gen in self._generations:
            gen_data: dict[str, Any] = {
                "description": gen.description,
                "created_at": gen.created_at,
                "status": gen.status,
                "git_commit": gen.git_commit,
                "cohorts": {},
            }

            for cohort_name in COHORT_NAMES:
                cohort_dir = str(Path(gen.state_dir) / cohort_name)
                metrics = _cohort_metrics(cohort_dir)
                if metrics is not None:
                    gen_data["cohorts"][cohort_name] = metrics

            result[gen.gen_id] = gen_data

        return {"generations": result}

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def format_report(self) -> str:
        """Human-readable comparison table."""
        data = self.compare()
        lines: list[str] = []
        lines.append("=== Generation Comparison ===")
        lines.append("")

        gens = data.get("generations", {})
        if not gens:
            lines.append("No generations to compare.")
            return "\n".join(lines)

        for gen_id, info in gens.items():
            status = info.get("status", "?")
            desc = info.get("description", "")
            commit = info.get("git_commit", "")
            commit_tag = f" [{commit[:7]}]" if commit else ""

            lines.append(
                f'{gen_id} ({status}) \u2014 "{desc}"{commit_tag}'
            )

            created = info.get("created_at", "?")
            # Compute max trading days across cohorts
            max_days = 0
            for cm in info.get("cohorts", {}).values():
                max_days = max(max_days, cm.get("num_trading_days", 0))
            lines.append(f"  Created: {created} | Days: {max_days}")

            cohorts = info.get("cohorts", {})
            if not cohorts:
                lines.append("  (no cohort data)")
            else:
                for cname in COHORT_NAMES:
                    cm = cohorts.get(cname)
                    if cm is None:
                        continue
                    trades = cm.get("total_trades", 0)
                    hit = _fmt_pct(cm.get("hit_rate"))
                    sharpe = (
                        f"{cm['sharpe']:.2f}"
                        if cm.get("sharpe") is not None
                        else "N/A"
                    )
                    ret = _fmt_signed_pct(cm.get("total_return"))
                    label = f"{cname.capitalize()}"
                    lines.append(
                        f"  {label + ':':10s} {trades} trades | "
                        f"Hit: {hit} | Sharpe: {sharpe} | Return: {ret}"
                    )

            lines.append("")

        return "\n".join(lines)


# ======================================================================
# Internal helpers
# ======================================================================


def _cohort_metrics(cohort_dir: str) -> dict[str, Any] | None:
    """Build metrics dict for a single cohort directory.

    Returns None if the directory does not exist.
    """
    path = Path(cohort_dir)
    if not path.exists():
        return None

    journal = SignalJournal(cohort_dir)
    state = StateManager(cohort_dir)

    entries = journal.get_entries()
    trades = state.load_paper_trades()
    closed = [t for t in trades if t.get("status") == "closed"]
    open_trades = [t for t in trades if t.get("status") == "open"]

    # Date range from trade entry_date fields
    date_range = _date_range(trades)
    num_days = _trading_days(trades)

    # Per-strategy breakdown
    strat_entries: dict[str, list[dict]] = {}
    for e in entries:
        strat_entries.setdefault(e.get("strategy", ""), []).append(e)

    strat_trades: dict[str, list[dict]] = {}
    for t in trades:
        strat_trades.setdefault(t.get("strategy", ""), []).append(t)

    per_strategy: dict[str, dict[str, Any]] = {}
    all_strategies = set(strat_entries) | set(strat_trades)
    for strat in sorted(all_strategies):
        if not strat:
            continue
        s_entries = strat_entries.get(strat, [])
        s_closed = [
            t
            for t in strat_trades.get(strat, [])
            if t.get("status") == "closed"
        ]
        per_strategy[strat] = {
            "signals": len(s_entries),
            "trades": len(strat_trades.get(strat, [])),
            "hit_rate": _hit_rate(s_entries),
        }

    return {
        "date_range": date_range,
        "num_trading_days": num_days,
        "total_signals": len(entries),
        "total_trades": len(trades),
        "open_trades": len(open_trades),
        "closed_trades": len(closed),
        "hit_rate": _hit_rate(entries),
        "avg_pnl_pct": _avg_pnl_pct(closed),
        "sharpe": _sharpe(closed),
        "total_return": _total_return(closed),
        "per_strategy": per_strategy,
    }


def _hit_rate(entries: list[dict]) -> float | None:
    """Fraction of signals where direction matched 5d return sign."""
    eligible = [e for e in entries if e.get("return_5d") is not None]
    if not eligible:
        return None
    hits = 0
    for e in eligible:
        ret = e["return_5d"]
        direction = e.get("direction", "long")
        if (direction == "long" and ret > 0) or (direction == "short" and ret < 0):
            hits += 1
    return hits / len(eligible)


def _avg_pnl_pct(closed_trades: list[dict]) -> float | None:
    """Mean PnL percentage of closed trades."""
    pcts = [
        t["pnl_pct"]
        for t in closed_trades
        if "pnl_pct" in t and t["pnl_pct"] is not None
    ]
    if not pcts:
        return None
    return statistics.mean(pcts)


def _sharpe(closed_trades: list[dict]) -> float | None:
    """Sharpe ratio from closed trade PnL pct values (simple, not annualized)."""
    pcts = [
        t["pnl_pct"]
        for t in closed_trades
        if "pnl_pct" in t and t["pnl_pct"] is not None
    ]
    if len(pcts) < 2:
        return None
    sd = statistics.stdev(pcts)
    if sd == 0:
        return None
    return statistics.mean(pcts) / sd


def _total_return(closed_trades: list[dict]) -> float | None:
    """Sum of PnL pct for closed trades."""
    pcts = [
        t["pnl_pct"]
        for t in closed_trades
        if "pnl_pct" in t and t["pnl_pct"] is not None
    ]
    if not pcts:
        return None
    return sum(pcts)


def _date_range(trades: list[dict]) -> list[str]:
    """[earliest_entry_date, latest_entry_date] or empty list."""
    dates = [t["entry_date"] for t in trades if t.get("entry_date")]
    if not dates:
        return []
    return [min(dates), max(dates)]


def _trading_days(trades: list[dict]) -> int:
    """Count unique entry_date values across trades."""
    dates = {t["entry_date"] for t in trades if t.get("entry_date")}
    return len(dates)


def _fmt_pct(value: float | None) -> str:
    """Format a float as percentage string, or N/A."""
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%"


def _fmt_signed_pct(value: float | None) -> str:
    """Format a float as signed percentage string, or N/A."""
    if value is None:
        return "N/A"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value * 100:.1f}%"
