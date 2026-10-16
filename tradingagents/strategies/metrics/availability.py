"""Reader-facing explanations from governed ratio availability evidence."""
from __future__ import annotations
from typing import Mapping

RATIO_REQUIREMENTS = "Ratios require 30 valid daily returns (31 snapshots); zero variance remains undefined."


def ratio_display(book: Mapping[str, object], metric: str) -> str:
    value = book.get(metric)
    if value is not None:
        return f"{float(value):.2f}"
    prefix = "sharpe" if metric == "annualized_daily_net_sharpe" else "information_ratio"
    reason = book.get(f"{prefix}_unavailable_reason") or book.get("unavailable_reason")
    count = book.get(f"{prefix}_return_count")
    if count is None and isinstance(book.get("valid_sessions"), int):
        count = max(int(book["valid_sessions"]) - 1, 0)
    if reason == "zero_variance":
        return "Undefined: zero variance in daily excess returns"
    if reason in {"insufficient_history", "insufficient_return_count"} or (not reason and count is not None and int(count) < 30):
        return f"Insufficient history ({count if count is not None else 0}/30 returns; 31 snapshots required)"
    if reason:
        return "Unavailable: " + str(reason).replace("_", " ")
    return "Unavailable: reason not supplied"
