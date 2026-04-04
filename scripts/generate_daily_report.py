#!/usr/bin/env python3
"""Generate a daily markdown report for all active paper trading generations.

Usage:
    python scripts/generate_daily_report.py [--date 2026-04-03]

Saves to: docs/reports/YYYY-MM-DD-daily-report.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("daily_report")


def _repo_root() -> str:
    return str(Path(__file__).resolve().parent.parent)


def _load_trades(state_dir: str) -> list[dict]:
    path = Path(state_dir) / "paper_trades.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _load_signals(state_dir: str) -> list[dict]:
    path = Path(state_dir) / "signal_journal.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().strip().split("\n") if line.strip()]


def _load_regimes(state_dir: str) -> list[dict]:
    path = Path(state_dir) / "regime_snapshots.json"
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _fmt(val: float | None, fmt: str = ".2f", suffix: str = "") -> str:
    if val is None:
        return "N/A"
    return f"{val:{fmt}}{suffix}"


def _capital_deployed(trades: list[dict]) -> float:
    return sum(t.get("position_value", 0) for t in trades if t.get("status") == "open")


def _generate_report(date: str, gens: list[dict]) -> str:
    lines: list[str] = []
    lines.append(f"# Daily Report: {date}")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"**Active generations:** {len(gens)}")
    lines.append("")

    # --- Summary table ---
    lines.append("## Summary")
    lines.append("")
    header = "| | " + " | ".join(g["gen_id"] for g in gens) + " |"
    sep = "|---|" + "|".join("---" for _ in gens) + "|"
    lines.append(header)
    lines.append(sep)

    def row(label: str, values: list[str]) -> str:
        return f"| **{label}** | " + " | ".join(values) + " |"

    lines.append(row("Description", [g["description"][:60] for g in gens]))
    lines.append(row("Commit", [g["git_commit"][:12] for g in gens]))
    lines.append(row("Days run", [str(g["days_run"]) for g in gens]))

    for cohort in ["control", "adaptive"]:
        signals = [str(g["cohorts"][cohort]["total_signals"]) for g in gens]
        trades = [str(g["cohorts"][cohort]["total_trades"]) for g in gens]
        open_t = [str(g["cohorts"][cohort]["open_trades"]) for g in gens]
        closed_t = [str(g["cohorts"][cohort]["closed_trades"]) for g in gens]
        deployed = [f"${g['cohorts'][cohort]['capital_deployed']:,.0f}" for g in gens]
        hit = [_fmt(g["cohorts"][cohort].get("hit_rate"), ".0%") for g in gens]
        sharpe = [_fmt(g["cohorts"][cohort].get("sharpe")) for g in gens]
        ret = [_fmt(g["cohorts"][cohort].get("total_return"), "+.2%") for g in gens]

        label = cohort.capitalize()
        lines.append(row(f"{label} signals", signals))
        lines.append(row(f"{label} trades (open/closed)", [f"{o}/{c}" for o, c in zip(open_t, closed_t)]))
        lines.append(row(f"{label} capital deployed", deployed))
        lines.append(row(f"{label} hit rate", hit))
        lines.append(row(f"{label} Sharpe", sharpe))
        lines.append(row(f"{label} return", ret))

    lines.append("")

    # --- Per-generation detail ---
    for g in gens:
        lines.append(f"## {g['gen_id']}: {g['description'][:80]}")
        lines.append("")

        for cohort in ["control", "adaptive"]:
            cd = g["cohorts"][cohort]
            lines.append(f"### {cohort.capitalize()} Cohort")
            lines.append("")

            # Open positions table
            open_trades = cd["open_trades_detail"]
            if open_trades:
                lines.append(f"**Open positions** — ${cd['capital_deployed']:,.0f} deployed")
                lines.append("")
                lines.append("| Ticker | Strategy | Dir | Entry | Shares | Value | Entry Date |")
                lines.append("|--------|----------|-----|-------|--------|-------|------------|")
                for t in open_trades:
                    lines.append(
                        f"| {t['ticker']} | {t['strategy']} | {t['direction']} "
                        f"| ${t['entry_price']:.2f} | {t['shares']} "
                        f"| ${t['position_value']:.2f} | {t['entry_date']} |"
                    )
                lines.append("")

            # Closed trades
            closed_trades = cd["closed_trades_detail"]
            if closed_trades:
                lines.append(f"**Closed trades** — {len(closed_trades)} completed")
                lines.append("")
                lines.append("| Ticker | Strategy | Dir | Entry | Exit | PnL % | Reason | Hold Days |")
                lines.append("|--------|----------|-----|-------|------|-------|--------|-----------|")
                for t in closed_trades:
                    pnl = _fmt(t.get("pnl_pct"), "+.2%")
                    lines.append(
                        f"| {t['ticker']} | {t['strategy']} | {t['direction']} "
                        f"| ${t.get('entry_price', 0):.2f} | ${t.get('exit_price', 0):.2f} "
                        f"| {pnl} | {t.get('exit_reason', '?')} | {t.get('holding_days', '?')} |"
                    )
                lines.append("")

            # Today's signals
            today_signals = cd["today_signals"]
            if today_signals:
                traded_count = sum(1 for s in today_signals if s.get("traded"))
                lines.append(f"**Today's signals** — {len(today_signals)} signals, {traded_count} traded")
                lines.append("")
                lines.append("| | Ticker | Strategy | Dir | Score |")
                lines.append("|---|--------|----------|-----|-------|")
                for s in today_signals:
                    mark = "\\*" if s.get("traded") else " "
                    lines.append(
                        f"| {mark} | {s.get('ticker', '?')} | {s.get('strategy', '?')} "
                        f"| {s.get('direction', '?')} | {s.get('score', 0):.2f} |"
                    )
                lines.append("")

            # Strategy breakdown
            strat_counts = cd["strategy_signal_counts"]
            if strat_counts:
                lines.append("**Strategy breakdown**")
                lines.append("")
                for strat, count in sorted(strat_counts.items(), key=lambda x: -x[1]):
                    traded = cd["strategy_trade_counts"].get(strat, 0)
                    lines.append(f"- `{strat}`: {count} signals, {traded} trades")
                lines.append("")

    # --- Regime ---
    lines.append("## Regime Context")
    lines.append("")
    # Use first generation's regime data
    regimes = gens[0]["regimes"]
    if regimes:
        lines.append("| Date | Overall | VIX | Credit | Yield Curve |")
        lines.append("|------|---------|-----|--------|-------------|")
        for r in regimes[-5:]:
            lines.append(
                f"| {r.get('date', '?')} | {r.get('overall_regime', '?')} "
                f"| {r.get('vix_regime', '?')} | {r.get('credit_regime', '?')} "
                f"| {r.get('yield_curve_regime', '?')} |"
            )
    else:
        lines.append("No regime data available.")
    lines.append("")

    return "\n".join(lines)


def _collect_generation_data(gen_info: dict, date: str) -> dict:
    """Collect all data for a single generation."""
    state_dir = gen_info["state_dir"]

    cohorts = {}
    for cohort_name in ["control", "adaptive"]:
        cohort_dir = str(Path(state_dir) / cohort_name)

        trades = _load_trades(cohort_dir)
        signals = _load_signals(cohort_dir)

        open_trades = [t for t in trades if t.get("status") == "open"]
        closed_trades = [t for t in trades if t.get("status") == "closed"]

        # Today's signals
        today_signals = [s for s in signals if s.get("timestamp", "")[:10] == date]

        # Strategy breakdowns
        strat_signal_counts = Counter(s.get("strategy", "?") for s in signals)
        strat_trade_counts = Counter(t.get("strategy", "?") for t in trades)

        # Hit rate from journal entries with return_5d
        eligible = [s for s in signals if s.get("return_5d") is not None]
        hit_rate = None
        if eligible:
            hits = sum(
                1 for e in eligible
                if (e.get("direction") == "long" and e["return_5d"] > 0)
                or (e.get("direction") == "short" and e["return_5d"] < 0)
            )
            hit_rate = hits / len(eligible)

        # Sharpe and return from closed trades
        import statistics
        pnl_pcts = [t["pnl_pct"] for t in closed_trades if t.get("pnl_pct") is not None]
        sharpe = None
        total_return = None
        if len(pnl_pcts) >= 2:
            sd = statistics.stdev(pnl_pcts)
            if sd > 0:
                sharpe = statistics.mean(pnl_pcts) / sd
        if pnl_pcts:
            total_return = sum(pnl_pcts)

        cohorts[cohort_name] = {
            "total_signals": len(signals),
            "total_trades": len(trades),
            "open_trades": len(open_trades),
            "closed_trades": len(closed_trades),
            "capital_deployed": _capital_deployed(trades),
            "hit_rate": hit_rate,
            "sharpe": sharpe,
            "total_return": total_return,
            "open_trades_detail": open_trades,
            "closed_trades_detail": closed_trades,
            "today_signals": today_signals,
            "strategy_signal_counts": dict(strat_signal_counts),
            "strategy_trade_counts": dict(strat_trade_counts),
        }

    # Regime from control (same for both)
    regimes = _load_regimes(str(Path(state_dir) / "control"))

    # Count unique trading days
    all_signals = _load_signals(str(Path(state_dir) / "control"))
    days = sorted({s.get("timestamp", "")[:10] for s in all_signals if s.get("timestamp")})

    return {
        "gen_id": gen_info["gen_id"],
        "description": gen_info["description"],
        "git_commit": gen_info.get("git_commit", ""),
        "days_run": len(days),
        "cohorts": cohorts,
        "regimes": regimes,
    }


def main():
    parser = argparse.ArgumentParser(description="Generate daily trading report")
    parser.add_argument("--date", default=None, help="Report date (YYYY-MM-DD)")
    args = parser.parse_args()

    date = args.date or datetime.now().strftime("%Y-%m-%d")

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    from tradingagents.autoresearch.generation_manager import GenerationManager

    repo = _repo_root()
    manager = GenerationManager(repo)
    all_gens = manager.list_generations()
    active_gens = [g for g in all_gens if g.status == "active"]

    if not active_gens:
        print("No active generations found.")
        return

    # Collect data for each generation
    gen_data = []
    for g in active_gens:
        data = _collect_generation_data(
            {
                "gen_id": g.gen_id,
                "state_dir": g.state_dir,
                "description": g.description,
                "git_commit": g.git_commit,
            },
            date,
        )
        gen_data.append(data)

    # Generate report
    report = _generate_report(date, gen_data)

    # Save
    report_dir = Path(repo) / "docs" / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{date}-daily-report.md"
    report_path.write_text(report)
    print(f"Report saved to {report_path}")


if __name__ == "__main__":
    main()
