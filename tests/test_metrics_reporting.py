from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from tradingagents.dashboard import data_loaders
from tradingagents.strategies.metrics.service import MetricsService


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


def test_dashboard_loader_contains_no_network_price_fetching() -> None:
    source = Path(data_loaders.__file__).read_text()
    assert "yf.download" not in source
    assert "import yfinance" not in source
    assert "load_current_prices" not in source


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
