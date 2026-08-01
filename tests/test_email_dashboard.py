"""Email rendering consumes injected metric-v2 reports only."""

from __future__ import annotations

from unittest.mock import patch

from tradingagents.dashboard.email_export import render_dashboard_html


def _report() -> dict:
    books = {
        f"horizon_{h}_size_100k": {
            "total_return": 0.01,
            "valid_sessions": 2,
            "annualized_daily_net_sharpe": None,
            "annualized_matched_information_ratio": None,
            "directional_accuracy_5d": None,
            "valuation_at": "2026-08-31T20:00:00+00:00",
            "benchmark_at": "2026-08-31T20:01:00+00:00",
            "matched_benchmark_return": 0.005,
            "gross_weight": 0.8,
            "net_weight": 0.6,
            "cumulative_costs": {"commission": 1.5},
        }
        for h in ("30d", "3m", "6m", "1y")
    }
    series = {
        name: {
            "benchmarks": {
                "SPY": [{"observed_at": "2026-08-31T20:01:00+00:00", "close": 600.0}],
                "BIL": [{"observed_at": "2026-08-31T20:01:00+00:00", "close": 91.0}],
            }
        }
        for name in books
    }
    return {
        "metric_schema_version": 2,
        "epoch": {"epoch_id": "epoch-1"},
        "headline_books": books,
        "stress_tests": {},
        "cohort_series": series,
        "scenario_panel": {"total_return": 0.01},
        "dependent_scenarios": True,
    }


@patch("yfinance.download", side_effect=AssertionError("network forbidden"))
def test_email_uses_persisted_metric_report_only(_download) -> None:
    html = render_dashboard_html(
        [{"gen_id": "gen_test", "metric_report": _report()}], "2026-08-31"
    )
    assert "Dependent scenario portfolios" in html
    assert "Equal-weighted scenario panel" in html
    assert "Fund AUM" not in html
    assert "Insufficient history (<30 valid sessions)" in html
    for evidence in (
        "Persisted SPY/BIL observations",
        "2026-08-31T20:00:00+00:00",
        "2026-08-31T20:01:00+00:00",
        "Matched benchmark return",
        "Gross exposure",
        "Net exposure",
        "commission",
    ):
        assert evidence in html


def test_no_generations_message() -> None:
    assert "No active generations" in render_dashboard_html([], "2026-08-31")
