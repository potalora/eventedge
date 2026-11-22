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
    assert "Insufficient history (" in html
    assert "/30 returns; 31 snapshots required)" in html
    for evidence in (
        "Persisted benchmark observations (SPY/BIL/VTI/VT)",
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


def test_dashboard_pruned_unused_charts_and_noop_loaders() -> None:
    from tradingagents.dashboard import charts, data_loaders
    from tradingagents.dashboard.pages import (
        cohort_matrix,
        overview,
        positions,
        returns,
        strategies,
    )

    for removed_chart in (
        "make_capital_bars",
        "make_strategy_bars",
        "make_gen_comparison",
        "make_drawdown_chart",
        "make_strategy_pnl_chart",
        "make_winners_losers_bars",
        "make_position_treemap",
    ):
        assert not hasattr(charts, removed_chart)

    for live_chart in (
        "make_cohort_heatmap",
        "make_regime_timeline",
        "make_equity_curves_facet",
    ):
        assert hasattr(charts, live_chart)

    for removed_loader in (
        "load_all_trades",
        "load_position_pnl",
        "load_strategy_pnl",
    ):
        assert not hasattr(data_loaders, removed_loader)

    for page in (cohort_matrix, overview, positions, returns, strategies):
        assert callable(page.render)


def test_dashboard_reads_regime_and_metrics_from_generation_db(tmp_path) -> None:
    from decimal import Decimal
    from tradingagents.dashboard import data_loaders
    from tradingagents.strategies.state.generation_db import GenerationDB
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    from tradingagents.strategies.state.state import StateManager

    db = GenerationDB.for_state_dir(tmp_path)
    mgr = StateManager(tmp_path, db=db)
    mgr.save_regime_snapshot(
        {
            "session": "2026-08-31",
            "overall_regime": "normal",
            "vix_level": 16.4,
            "credit_spread_bps": 320.0,
            "yield_curve_slope": 0.45,
        }
    )
    ledger = PortfolioLedger(
        db, cohort_id="horizon_30d_size_100k", initial_cash=Decimal("100000")
    )
    ledger.close()

    history = data_loaders.load_regime_history.__wrapped__("gen_003", str(tmp_path))
    assert len(history) == 1
    assert history[0]["overall_regime"] == "normal"
    assert history[0]["vix_level"] == 16.4

    service, ledgers = data_loaders._open_metrics_service(str(tmp_path))
    assert service is not None
    assert len(ledgers) == 1
    try:
        report = service.generation_report()
        assert report["metric_schema_version"] == 2
    finally:
        for item in ledgers:
            item.close()
