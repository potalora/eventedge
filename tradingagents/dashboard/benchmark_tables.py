"""Shared read-only presentation of fixed, source-backed ETF comparisons."""
from __future__ import annotations

import math


ETF_COMPARISON_TITLE = "Fixed ETF comparisons by $100k book"
ETF_COMPARISON_DISCLOSURE = (
    "SPY is primary with a fixed 5 percentage point annualized hurdle. "
    "BIL, VTI and VT are fixed diagnostics; paired intervals are descriptive "
    "and do not select a winning benchmark. Books share evidence and are not pooled."
)
ETF_COMPARISON_COLUMNS = (
    "Book", "Benchmark", "Role", "Total excess", "Annualized excess",
    "Paired 95% intervals", "Evidence", "Research decision",
)
_ROLES = {
    "SPY": "Primary S&P 500; 5 pp annualized hurdle",
    "BIL": "Cash diagnostic",
    "VTI": "Secondary US total market; descriptive",
    "VT": "Secondary global equities; descriptive",
}


def _points(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "Unavailable"
    return f"{value * 100:+.2f} pp"


def _intervals(comparison: dict) -> str:
    rows = []
    for block in comparison.get("block_lengths", []):
        bounds = block.get("annualized_excess_confidence_interval")
        prefix = f"{block.get('block_length')} sessions: "
        if block.get("status") == "available" and isinstance(bounds, (tuple, list)) and len(bounds) == 2:
            rows.append(prefix + f"[{_points(bounds[0])}, {_points(bounds[1])}]")
        else:
            rows.append(prefix + "unavailable (" + str(block.get("reason", "insufficient evidence")) + ")")
    return "; ".join(rows) if rows else "Unavailable"


def etf_comparison_rows(report: dict) -> list[dict[str, str]]:
    """Format retained research diagnostics; never fetch or recompute returns."""
    rows = []
    for cohort, book in sorted((report.get("headline_books") or {}).items()):
        comparisons = (book.get("research_diagnostics") or {}).get("benchmark_excess_uncertainty") or {}
        for symbol, role in _ROLES.items():
            comparison = comparisons.get(symbol) or {}
            available = comparison.get("status") == "available"
            evidence = (f"{comparison.get('return_count')} paired returns" if available else
                        "Insufficient evidence: " + str(comparison.get("reason", "research_diagnostics_unavailable")))
            decision = (str(comparison.get("decision", "inconclusive")).replace("_", " ")
                        if symbol == "SPY" else "Descriptive only")
            if symbol == "SPY" and comparison.get("decision_reason"):
                decision += ": " + str(comparison["decision_reason"]).replace("_", " ")
            rows.append({
                "Book": cohort, "Benchmark": symbol, "Role": role,
                "Total excess": _points(comparison.get("total_excess_return")) if available else "Unavailable",
                "Annualized excess": _points(comparison.get("annualized_excess_return")) if available else "Unavailable",
                "Paired 95% intervals": _intervals(comparison) if available else "Unavailable",
                "Evidence": evidence, "Research decision": decision,
            })
    return rows
