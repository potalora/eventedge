from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from tradingagents.dashboard import data_loaders
from tradingagents.strategies.metrics.service import MetricsService
from scripts.generate_daily_report import render_generation_report


def test_generation_loader_delegates_to_read_only_metrics_service(monkeypatch) -> None:
    service = Mock(spec=MetricsService)
    expected = {"metric_schema_version": 2, "headline_books": {}}
    service.generation_report.return_value = expected
    ledger = Mock()
    monkeypatch.setattr(
        data_loaders, "_open_metrics_service", lambda _path: (service, (ledger,))
    )

    data_loaders.load_generation_metrics.clear()
    assert data_loaders.load_generation_metrics("gen_004", "/tmp/gen_004") == expected
    service.generation_report.assert_called_once_with()
    ledger.close.assert_called_once_with()


def test_generation_loader_rejects_mismatched_generation_identity(monkeypatch) -> None:
    service = Mock(spec=MetricsService)
    service.generation_report.return_value = {
        "metric_schema_version": 2,
        "epoch": {"epoch_id": "epoch-1", "generation_id": "gen_actual"},
    }
    ledger = Mock()
    monkeypatch.setattr(
        data_loaders, "_open_metrics_service", lambda _path: (service, (ledger,))
    )

    data_loaders.load_generation_metrics.clear()
    with pytest.raises(ValueError, match="generation identity mismatch"):
        data_loaders.load_generation_metrics("gen_claimed", "/tmp/gen_claimed")
    ledger.close.assert_called_once_with()


def test_dashboard_loader_contains_no_network_price_fetching() -> None:
    source = Path(data_loaders.__file__).read_text()
    assert "yf.download" not in source
    assert "import yfinance" not in source
    assert "load_current_prices" not in source
    assert "paper_trades" not in source


def test_trade_compatibility_loader_returns_no_ungoverned_positions(
    monkeypatch,
) -> None:
    monkeypatch.setattr(data_loaders, "load_generation_metrics", lambda *_args: {})
    data_loaders.load_all_trades.clear()
    assert data_loaders.load_all_trades("gen_004", "/tmp/gen_004") == []


def test_compatibility_loaders_project_generation_report(monkeypatch) -> None:
    report = {
        "metric_schema_version": 2,
        "headline_books": {
            "horizon_30d_size_100k": {
                "fills": 3,
                "strategy_decisions": 7,
                "total_return": 0.02,
            },
        },
        "stress_tests": {
            "horizon_30d_size_50k": {
                "strategy_decisions": 999,
                "total_return": 0.01,
            },
        },
        "cohort_series": {
            "horizon_30d_size_100k": {
                "net_equity_history": [{"net_equity": 102000.0}],
            }
        },
    }
    monkeypatch.setattr(data_loaders, "load_generation_metrics", lambda *_args: report)

    assert data_loaders.load_cohort_metrics("gen_004", "/tmp/gen_004") is report
    assert data_loaders.load_cohort_heatmap("gen_004", "/tmp/gen_004", "fills") == {
        "30d": {"5k": None, "10k": None, "50k": None, "100k": 3.0},
        "3m": {"5k": None, "10k": None, "50k": None, "100k": None},
        "6m": {"5k": None, "10k": None, "50k": None, "100k": None},
        "1y": {"5k": None, "10k": None, "50k": None, "100k": None},
    }
    assert data_loaders.load_equity_history("gen_004", "/tmp/gen_004") == {
        "horizon_30d_size_100k": [{"net_equity": 102000.0}],
    }
    assert (
        data_loaders.load_signal_stats("gen_004", "/tmp/gen_004")["total_signals"] == 7
    )


def test_report_discloses_epoch_quality_counts_and_costs() -> None:
    rendered = render_generation_report(
        "2026-08-31",
        {"gen_id": "gen_004"},
        {"epoch": {"epoch_id": "epoch-1"}, "headline_books": {}, "stress_tests": {}},
    )
    required = (
        "Metric epoch",
        "Schema v2",
        "Valuation timestamp",
        "Benchmark timestamp",
        "Valid sessions",
        "Unique catalysts",
        "Strategy decisions",
        "Fills",
        "Closed trades",
        "Missing/stale marks",
        "Gross exposure",
        "Net exposure",
        "Costs",
    )
    assert all(label in rendered for label in required)
