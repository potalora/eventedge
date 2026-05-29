"""Human-readable rendering of an EventStudyResult."""
from __future__ import annotations

from tradingagents.strategies.validation.models import EventStudyResult


def format_report(result: EventStudyResult) -> str:
    lines: list[str] = []
    if not result.aggregates:
        lines.append("No events with sufficient data.")
    for agg in result.aggregates:
        lines.append(f"{agg.group}   (n={agg.n_events} events)")
        lines.append("  window     mean_CAR    t       p       95% CI")
        for w in agg.windows:
            ci = f"[{w.ci.lower * 100:+.2f}%, {w.ci.upper * 100:+.2f}%]"
            lines.append(
                f"  {w.window:<9} {w.mean_car * 100:>+7.2f}%  "
                f"{w.t_stat:>5.2f}  {w.p_value:>5.3f}  {ci}"
            )
        lines.append("")
    if result.skipped_tickers:
        lines.append(f"Skipped (insufficient data): {', '.join(result.skipped_tickers)}")
    return "\n".join(lines)
