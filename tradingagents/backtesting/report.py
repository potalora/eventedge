import json
from pathlib import Path
from typing import Any, Dict

import pandas as pd


def generate_backtest_report(result: Dict[str, Any], output_dir: str) -> str:
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    metrics = result["metrics"]
    trade_log = result["trade_log"]
    equity_curve = result["equity_curve"]

    lines = [
        "# Backtest Report\n",
        "## Performance Metrics\n",
        "| Metric | Value |",
        "|--------|-------|",
    ]
    for k, v in metrics.items():
        if isinstance(v, float):
            lines.append(f"| {k} | {v:.4f} |")
        else:
            lines.append(f"| {k} | {v} |")

    lines.append(f"\n## Trade Log ({len(trade_log)} trades)\n")
    if trade_log:
        lines.append("| Date | Ticker | Action | Price | Qty |")
        lines.append("|------|--------|--------|-------|-----|")
        for t in trade_log:
            lines.append(
                f"| {t['date']} | {t['ticker']} | {t['action']} | "
                f"${t['fill_price']:.2f} | {t['quantity']} |"
            )

    report_md = "\n".join(lines)
    report_path = Path(output_dir) / "backtest_report.md"
    report_path.write_text(report_md)

    if isinstance(equity_curve, pd.DataFrame) and not equity_curve.empty:
        equity_curve.to_csv(Path(output_dir) / "equity_curve.csv", index=False)

    if trade_log:
        pd.DataFrame(trade_log).to_csv(
            Path(output_dir) / "trade_log.csv", index=False
        )

    return str(report_path)
