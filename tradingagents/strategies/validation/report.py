"""Human-readable rendering of an EventStudyResult."""
from __future__ import annotations

from tradingagents.strategies.validation.models import EventStudyResult


def format_report(result: EventStudyResult) -> str:
    lines: list[str] = [
        "Descriptive total-return catalyst study (market-model CAR).",
        "Day 0 includes prior-close to catalyst-session-close returns, including moves before an after-close decision.",
        "Not executable alpha. Events can overlap and share market shocks; IID significance and confidence intervals are withheld.",
        "",
    ]
    if not result.aggregates:
        lines.append("No events with sufficient data.")
    for agg in result.aggregates:
        lines.append(f"{agg.group}   (n={agg.n_events} events)")
        lines.append("  window     n     mean_CAR    dispersion")
        for w in agg.windows:
            lines.append(
                f"  {w.window:<9} {w.n_events:>4}  {w.mean_car * 100:>+7.2f}%  "
                f"{w.std_car * 100:>7.2f}%"
            )
        lines.append("")
    if result.skipped_tickers:
        lines.append(f"Skipped (insufficient data): {', '.join(result.skipped_tickers)}")
    return "\n".join(lines)
