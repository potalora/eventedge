#!/usr/bin/env python3
"""Generate a metric-v2 daily markdown report without rebuilding performance."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from tradingagents.dashboard.data_loaders import load_generation_metrics

HEADLINE_TITLE = "Four $100k horizon books"
PANEL_LABEL = "Equal-weighted scenario panel"
DEPENDENCE_DISCLOSURE = "Dependent scenario portfolios: shared signals and market data mean the books are not independent observations and are not combined fund AUM."
STRESS_TEST_LABEL = "$5k/$10k/$50k concentration stress tests"
SHARPE_LABEL = "Annualized daily net Sharpe"
INFORMATION_RATIO_LABEL = "Annualized matched-benchmark information ratio"
ACCURACY_LABEL = "Directional accuracy (5 XNYS sessions)"
INSUFFICIENT_HISTORY = "Insufficient history (<30 valid sessions)"


def _pct(value: object) -> str:
    return "N/A" if value is None else f"{float(value):+.2%}"


def _metric(value: object) -> str:
    return INSUFFICIENT_HISTORY if value is None else f"{float(value):.2f}"


def _book_table(books: dict[str, object]) -> list[str]:
    lines = [
        f"| Book | Net return | {SHARPE_LABEL} | {INFORMATION_RATIO_LABEL} | {ACCURACY_LABEL} | Valid sessions |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, value in sorted(books.items()):
        book = value if isinstance(value, dict) else {}
        lines.append(
            f"| {name} | {_pct(book.get('total_return'))} | {_metric(book.get('annualized_daily_net_sharpe'))} | {_metric(book.get('annualized_matched_information_ratio'))} | {_pct(book.get('directional_accuracy_5d'))} | {book.get('valid_sessions', 'N/A')} |"
        )
    return (
        lines
        if len(lines) > 2
        else lines + ["| No governed metric books available. | — | — | — | — | — |"]
    )


def render_generation_report(
    date: str, gen: dict[str, object], report: dict[str, object]
) -> str:
    epoch = report.get("epoch") if isinstance(report.get("epoch"), dict) else {}
    headline = (
        report.get("headline_books")
        if isinstance(report.get("headline_books"), dict)
        else {}
    )
    stress = (
        report.get("stress_tests")
        if isinstance(report.get("stress_tests"), dict)
        else {}
    )
    panel = (
        report.get("scenario_panel")
        if isinstance(report.get("scenario_panel"), dict)
        else None
    )
    panel_value = _pct(panel.get("total_return")) if panel else "Unavailable"
    lines = [
        f"# Daily Report: {date}",
        "",
        f"## {gen.get('gen_id', '?')}",
        "",
        f"Metric epoch: {epoch.get('epoch_id', 'No available metric epoch')}",
        "Schema v2",
        f"{PANEL_LABEL}: {panel_value}",
        DEPENDENCE_DISCLOSURE,
        "",
        f"## {HEADLINE_TITLE}",
        "",
    ]
    lines.extend(_book_table(headline))
    lines += ["", f"## {STRESS_TEST_LABEL}", ""]
    lines.extend(_book_table(stress))
    lines += [
        "",
        "## Evidence and data quality",
        "",
        "| Book | Valuation timestamp | Benchmark timestamp | Valid sessions | Unique catalysts | Strategy decisions | Fills | Closed trades | Missing/stale marks | Gross exposure | Net exposure | Costs |",
        "|---|---|---|---:|---:|---:|---:|---:|---|---:|---:|---|",
    ]
    for name, value in sorted({**headline, **stress}.items()):
        book = value if isinstance(value, dict) else {}
        missing_stale = f"{book.get('missing_mark_count', 'N/A')}/{book.get('stale_mark_count', 'N/A')}"
        lines.append(
            f"| {name} | {book.get('valuation_at', 'N/A')} | {book.get('benchmark_at', 'N/A')} | {book.get('valid_sessions', 'N/A')} | {book.get('unique_catalysts', 'N/A')} | {book.get('strategy_decisions', 'N/A')} | {book.get('fills', 'N/A')} | {book.get('closed_trades', 'N/A')} | {missing_stale} | {book.get('gross_weight', 'N/A')} | {book.get('net_weight', 'N/A')} | {book.get('cumulative_costs', 'N/A')} |"
        )
    if not headline and not stress:
        lines.append(
            "| No governed metric books available. | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate metric-v2 daily report")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    args = parser.parse_args()
    from tradingagents.strategies.orchestration.generation_manager import (
        GenerationManager,
    )

    root = Path(__file__).resolve().parent.parent
    active = [
        g
        for g in GenerationManager(str(root)).list_generations()
        if g.status == "active"
    ]
    if not active:
        print("No active generations found.")
        return
    reports = []
    for gen in active:
        metadata = {"gen_id": gen.gen_id, "state_dir": gen.state_dir}
        reports.append(
            render_generation_report(
                args.date, metadata, load_generation_metrics(gen.gen_id, gen.state_dir)
            )
        )
    out = root / "docs" / "reports" / f"{args.date}-daily-report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(reports))
    print(f"Report saved to {out}")


if __name__ == "__main__":
    main()
