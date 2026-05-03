"""Cross-cohort metrics comparison for paper trading trials.

Compares signal accuracy, trade performance, and strategy-level metrics
across two (or more) cohorts running in parallel with different configs.
"""
from __future__ import annotations

import json
import logging
import statistics
from pathlib import Path
from typing import Any

from tradingagents.strategies.learning.signal_journal import SignalJournal
from tradingagents.strategies.state.state import StateManager

logger = logging.getLogger(__name__)


class CohortComparison:
    """Cross-cohort metrics from signal journals and trade records."""

    def __init__(self, cohort_state_dirs: dict[str, str]) -> None:
        """
        Args:
            cohort_state_dirs: {cohort_name: state_dir_path}
                e.g. {"control": "data/state/control", "adaptive": "data/state/adaptive"}
        """
        self._dirs = cohort_state_dirs
        self._journals: dict[str, SignalJournal] = {
            name: SignalJournal(path) for name, path in cohort_state_dirs.items()
        }
        self._states: dict[str, StateManager] = {
            name: StateManager(path) for name, path in cohort_state_dirs.items()
        }

    # ------------------------------------------------------------------
    # Core comparison
    # ------------------------------------------------------------------

    def compare(self) -> dict:
        """Generate comparison metrics across cohorts.

        Returns dict with:
        - "cohorts": per-cohort aggregate metrics
        - "per_strategy": per-strategy per-cohort metrics
        - "confidence_evolution": adaptive cohort confidence values
        - "prompt_trials": adaptive cohort prompt trial metadata
        """
        cohorts: dict[str, dict[str, Any]] = {}
        per_strategy: dict[str, dict[str, Any]] = {}

        for name in self._dirs:
            entries = self._journals[name].get_entries()
            trades = self._states[name].load_paper_trades()
            closed = [t for t in trades if t.get("status") == "closed"]
            open_trades = [t for t in trades if t.get("status") == "open"]

            cohorts[name] = {
                "total_signals": len(entries),
                "total_trades": len(trades),
                "open_trades": len(open_trades),
                "closed_trades": len(closed),
                "hit_rate_5d": _hit_rate_5d(entries),
                "avg_return_5d": _avg_return_5d(entries),
                "sharpe": _sharpe(closed),
                "max_drawdown_estimate": _max_drawdown(closed),
                "win_rate": _win_rate(closed),
                "avg_pnl": _avg_pnl(closed),
            }

            # Per-strategy breakdown
            strat_entries: dict[str, list[dict]] = {}
            for e in entries:
                strat_entries.setdefault(e.get("strategy", ""), []).append(e)

            strat_trades: dict[str, list[dict]] = {}
            for t in trades:
                strat_trades.setdefault(t.get("strategy", ""), []).append(t)

            all_strategies = set(strat_entries) | set(strat_trades)
            for strat in all_strategies:
                if not strat:
                    continue
                if strat not in per_strategy:
                    per_strategy[strat] = {}
                s_entries = strat_entries.get(strat, [])
                s_trades = strat_trades.get(strat, [])
                per_strategy[strat][name] = {
                    "signal_count": len(s_entries),
                    "trade_count": len(s_trades),
                    "hit_rate_5d": _hit_rate_5d(s_entries),
                }

        # Adaptive-only metrics
        confidence_evolution = self._confidence_evolution()
        prompt_trials = self._prompt_trials()

        return {
            "cohorts": cohorts,
            "per_strategy": per_strategy,
            "confidence_evolution": confidence_evolution,
            "prompt_trials": prompt_trials,
        }

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def format_report(self) -> str:
        """Human-readable text summary of comparison."""
        data = self.compare()
        lines: list[str] = []

        lines.append("=== Cohort Comparison Report ===")
        lines.append("")

        # Header
        header = (
            f"{'Cohort':<12} | {'Signals':>7} | {'Trades':>6} | {'Hit Rate':>8} "
            f"| {'Avg Ret':>10} | {'Sharpe':>6} | {'Win Rate':>8} | {'Avg PnL':>10} "
            f"| {'Max DD':>10}"
        )
        sep = "-" * len(header)
        lines.append(header)
        lines.append(sep)

        for name, m in data["cohorts"].items():
            hit = _fmt_pct(m["hit_rate_5d"])
            avg_ret = _fmt_pct(m["avg_return_5d"])
            sharpe = f"{m['sharpe']:.2f}" if m["sharpe"] is not None else "N/A"
            win = _fmt_pct(m["win_rate"])
            avg_pnl = f"${m['avg_pnl']:.2f}" if m["avg_pnl"] is not None else "N/A"
            dd = f"${m['max_drawdown_estimate']:.2f}" if m["max_drawdown_estimate"] is not None else "N/A"
            lines.append(
                f"{name:<12} | {m['total_signals']:>7} | {m['total_trades']:>6} | {hit:>8} "
                f"| {avg_ret:>10} | {sharpe:>6} | {win:>8} | {avg_pnl:>10} "
                f"| {dd:>10}"
            )

        # Per-strategy hit rates
        if data["per_strategy"]:
            lines.append("")
            lines.append("Per-Strategy Hit Rates:")
            cohort_names = list(data["cohorts"].keys())
            strat_header = f"  {'Strategy':<30}"
            for cn in cohort_names:
                strat_header += f" | {cn:>12}"
            lines.append(strat_header)
            lines.append("  " + "-" * (len(strat_header) - 2))

            for strat in sorted(data["per_strategy"]):
                row = f"  {strat:<30}"
                for cn in cohort_names:
                    sm = data["per_strategy"][strat].get(cn)
                    if sm:
                        val = _fmt_pct(sm["hit_rate_5d"])
                        row += f" | {val:>12}"
                    else:
                        row += f" | {'N/A':>12}"
                lines.append(row)

        # Confidence evolution
        if data["confidence_evolution"]:
            lines.append("")
            lines.append("Confidence Evolution (adaptive cohort):")
            for strat, conf in sorted(data["confidence_evolution"].items()):
                lines.append(f"  {strat:<30}  {conf:.3f}")

        # Prompt trials
        if data["prompt_trials"]:
            lines.append("")
            lines.append("Prompt Trials (adaptive cohort):")
            for tid, info in data["prompt_trials"].items():
                status = info.get("status", "unknown")
                strategy = info.get("strategy", "?")
                start = info.get("start_date", "?")
                lines.append(f"  {tid}  strategy={strategy}  status={status}  started={start}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Slice & dice helpers
    # ------------------------------------------------------------------

    def compare_by_horizon(self, size: str) -> dict:
        """Compare cohorts across horizons for a fixed portfolio size.

        Args:
            size: Size profile key (e.g., "5k", "10k").

        Returns:
            Same structure as compare(), filtered to matching cohorts.
        """
        matching = {
            name for name in self._dirs
            if f"_size_{size}" in name
        }
        return self._filtered_compare(matching)

    def compare_by_size(self, horizon: str) -> dict:
        """Compare cohorts across sizes for a fixed horizon.

        Args:
            horizon: Horizon key (e.g., "30d", "3m").

        Returns:
            Same structure as compare(), filtered to matching cohorts.
        """
        matching = {
            name for name in self._dirs
            if f"horizon_{horizon}_" in name
        }
        return self._filtered_compare(matching)

    def heatmap(self, metric: str = "sharpe") -> dict[str, dict[str, float | None]]:
        """Return horizon x size matrix of a single metric.

        Args:
            metric: Key from per-cohort metrics (e.g., "sharpe", "hit_rate_5d",
                    "win_rate", "avg_pnl").

        Returns:
            {horizon: {size: metric_value}} e.g. {"30d": {"5k": 1.2, "10k": 0.8}}
        """
        data = self.compare()
        result: dict[str, dict[str, float | None]] = {}

        for name, metrics in data["cohorts"].items():
            # Parse horizon and size from name: "horizon_30d_size_5k"
            parts = name.split("_")
            if len(parts) >= 4 and parts[0] == "horizon" and parts[2] == "size":
                horizon = parts[1]
                size = parts[3]
                result.setdefault(horizon, {})[size] = metrics.get(metric)

        return result

    def _filtered_compare(self, names: set[str]) -> dict:
        """Run compare() and filter to only the named cohorts."""
        full = self.compare()
        filtered_cohorts = {
            k: v for k, v in full["cohorts"].items() if k in names
        }
        filtered_strategies = {}
        for strat, cohort_data in full["per_strategy"].items():
            filtered = {k: v for k, v in cohort_data.items() if k in names}
            if filtered:
                filtered_strategies[strat] = filtered

        return {
            "cohorts": filtered_cohorts,
            "per_strategy": filtered_strategies,
            "confidence_evolution": full.get("confidence_evolution", {}),
            "prompt_trials": full.get("prompt_trials", {}),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _confidence_evolution(self) -> dict[str, float]:
        """Compute per-strategy confidence for the adaptive cohort."""
        if "adaptive" not in self._journals:
            return {}

        entries = self._journals["adaptive"].get_entries()
        by_strat: dict[str, list[dict]] = {}
        for e in entries:
            by_strat.setdefault(e.get("strategy", ""), []).append(e)

        result: dict[str, float] = {}
        for strat, strat_entries in by_strat.items():
            if not strat:
                continue
            hr = _hit_rate_5d(strat_entries)
            if hr is None:
                result[strat] = 0.2
            else:
                result[strat] = max(0.2, min(0.9, (hr - 0.3) / 0.4 * 0.7 + 0.2))
        return result

    def _prompt_trials(self) -> dict[str, dict]:
        """Load prompt trials from the adaptive cohort state dir."""
        if "adaptive" not in self._dirs:
            return {}

        trials_path = Path(self._dirs["adaptive"]) / "prompt_trials.json"
        if not trials_path.exists():
            return {}
        try:
            data = json.loads(trials_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load prompt trials: %s", e)
            return {}

        # Extract summary fields per trial
        result: dict[str, dict] = {}
        for tid, trial in data.items():
            result[tid] = {
                "strategy": trial.get("strategy", ""),
                "status": trial.get("status", ""),
                "start_date": trial.get("start_date", ""),
            }
        return result


# ======================================================================
# Pure metric helpers
# ======================================================================


def _hit_rate_5d(entries: list[dict]) -> float | None:
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


def _avg_return_5d(entries: list[dict]) -> float | None:
    """Mean absolute 5d return across entries with data."""
    returns = [e["return_5d"] for e in entries if e.get("return_5d") is not None]
    if not returns:
        return None
    return statistics.mean(returns)


def _sharpe(closed_trades: list[dict]) -> float | None:
    """Sharpe ratio from closed trade PnL values."""
    import math
    pnls = [
        t["pnl"] for t in closed_trades
        if "pnl" in t
        and t["pnl"] is not None
        and isinstance(t["pnl"], (int, float))
        and not (isinstance(t["pnl"], float) and (math.isnan(t["pnl"]) or math.isinf(t["pnl"])))
    ]
    if len(pnls) < 2:
        return None
    sd = statistics.stdev(pnls)
    if sd == 0:
        return None
    return statistics.mean(pnls) / sd


def _win_rate(closed_trades: list[dict]) -> float | None:
    """Fraction of closed trades with positive PnL."""
    pnls = [t["pnl"] for t in closed_trades if "pnl" in t and t["pnl"] is not None]
    if not pnls:
        return None
    return sum(1 for p in pnls if p > 0) / len(pnls)


def _avg_pnl(closed_trades: list[dict]) -> float | None:
    """Mean PnL of closed trades."""
    pnls = [t["pnl"] for t in closed_trades if "pnl" in t and t["pnl"] is not None]
    if not pnls:
        return None
    return statistics.mean(pnls)


def _max_drawdown(closed_trades: list[dict]) -> float | None:
    """Max peak-to-trough drawdown from cumulative closed-trade PnL."""
    pnls = [t["pnl"] for t in closed_trades if "pnl" in t and t["pnl"] is not None]
    if not pnls:
        return None

    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _fmt_pct(value: float | None) -> str:
    """Format a float as percentage string, or N/A."""
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%"
