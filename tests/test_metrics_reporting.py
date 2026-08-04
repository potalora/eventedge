from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from tradingagents.dashboard import data_loaders
from tradingagents.dashboard.pages import overview
from tradingagents.dashboard.charts import make_cohort_heatmap, make_equity_curves_facet
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


def test_report_discloses_candidate_recovery_and_quarantine_evidence() -> None:
    rendered = render_generation_report(
        "2026-08-31",
        {"gen_id": "gen_004"},
        {
            "epoch": {"epoch_id": "epoch-1"},
            "headline_books": {},
            "stress_tests": {},
            "candidate_bar_recoveries": [
                {
                    "session": "2026-08-31",
                    "ticker": "ALX",
                    "outcome": "quarantined",
                    "attempts": [{"attempt": 1}, {"attempt": 2}],
                    "signal_identities": [
                        {"event_key": "event-alx", "strategy": "litigation"}
                    ],
                }
            ],
        },
    )

    assert "Candidate market-data recovery" in rendered
    assert "execution-valid degraded" in rendered
    assert "| 2026-08-31 | ALX | quarantined | 2 | 1 |" in rendered


class _OverviewColumn:
    def __init__(self, metrics):
        self._metrics = metrics

    def metric(self, label, value):
        self._metrics.append((label, value))


class _OverviewStreamlit:
    def __init__(self):
        self.metrics = []
        self.captions = []

    def markdown(self, *_args, **_kwargs):
        pass

    def caption(self, value):
        self.captions.append(value)

    def columns(self, count):
        return [_OverviewColumn(self.metrics) for _ in range(count)]


def _render_overview_card(monkeypatch, metrics):
    fake_st = _OverviewStreamlit()
    monkeypatch.setattr(overview, "st", fake_st)
    monkeypatch.setattr(overview, "load_generation_metrics", lambda *_args: metrics)
    overview._render_gen_card(
        {
            "gen_id": "gen_004",
            "state_dir": "/tmp/gen_004",
            "created_at": "2026-08-01",
            "git_commit": "abcdef123",
            "description": "candidate recovery",
            "run_history": [
                {
                    "date": "2026-08-31",
                    "success": False,
                    "degraded": True,
                    "execution_valid": True,
                    "candidate_bar_quarantines": ["ALX"],
                }
            ],
        }
    )
    return fake_st


def test_overview_counts_execution_valid_degraded_run_as_trading_day(monkeypatch):
    rendered = _render_overview_card(
        monkeypatch,
        {"metric_schema_version": 2, "headline_books": {}},
    )

    assert ("Trading Days", 1) in rendered.metrics


def test_overview_discloses_candidate_recovery_and_quarantine(monkeypatch):
    rendered = _render_overview_card(
        monkeypatch,
        {
            "metric_schema_version": 2,
            "headline_books": {},
            "candidate_bar_recoveries": [
                {
                    "session": "2026-08-31",
                    "ticker": "ALX",
                    "outcome": "quarantined",
                    "attempts": [{"attempt": 1}, {"attempt": 2}],
                    "signal_identities": [
                        {"event_key": "event-alx", "strategy": "litigation"}
                    ],
                }
            ],
        },
    )

    candidate_captions = " ".join(rendered.captions)
    assert "Candidate market-data recovery" in candidate_captions
    assert "ALX" in candidate_captions
    assert "quarantined" in candidate_captions
    assert "execution-valid degraded" in candidate_captions


def test_report_and_dashboard_disclose_candidate_recovery_truncation(monkeypatch):
    recovery = {
        "session": "2026-08-31",
        "ticker": "ALX",
        "outcome": "quarantined",
        "attempts": [{"attempt": 1}, {"attempt": 2}],
        "signal_identities": [
            {"event_key": "event-alx", "strategy": "litigation"}
        ],
    }
    metrics = {
        "epoch": {"epoch_id": "epoch-1"},
        "headline_books": {},
        "stress_tests": {},
        "candidate_bar_recoveries": [recovery],
        "candidate_bar_recovery_scope": {
            "total_records": 1_001,
            "returned_records": 1_000,
            "truncated": True,
            "order": "newest_first",
        },
    }

    rendered_report = render_generation_report(
        "2026-08-31", {"gen_id": "gen_004"}, metrics
    )
    rendered_dashboard = _render_overview_card(monkeypatch, metrics)

    disclosure = "Showing newest 1,000 of 1,001 persisted recovery records"
    assert disclosure in rendered_report
    assert disclosure in " ".join(rendered_dashboard.captions)


def test_dashboard_surfaces_have_no_legacy_accuracy_or_local_aggregation() -> None:
    dashboard = Path("tradingagents/dashboard")
    sources = "\n".join(path.read_text() for path in dashboard.rglob("*.py"))
    for forbidden in (
        "hit_rate_5d",
        "return_5d",
        "capital_weighted",
        "cur_peak",
    ):
        assert forbidden not in sources


def test_matrix_uses_exact_governed_labels_and_unavailable_copy() -> None:
    source = Path("tradingagents/dashboard/pages/cohort_matrix.py").read_text()
    for required in (
        "Annualized daily net Sharpe",
        "Annualized matched-benchmark information ratio",
        "Insufficient history (<30 valid sessions)",
        "concentration stress tests",
        "Dependent scenario portfolios",
    ):
        assert required in source


@pytest.mark.parametrize(
    ("metric_name", "value", "expected"),
    (
        ("Net Total Return", 0.01, "1.0%"),
        ("Gross Weight", 0.80, "80.0%"),
        ("Cash Weight", 0.20, "20.0%"),
        ("Net Max Drawdown", -0.10, "-10.0%"),
        ("Annualized daily net Sharpe", 0.75, "0.75"),
    ),
)
def test_heatmap_formats_governed_ratio_metrics_truthfully(
    metric_name: str, value: float, expected: str
) -> None:
    figure = make_cohort_heatmap(
        {"30d": {"5k": value}},
        metric_name,
    )

    assert figure.data[0].text[0][0] == expected


def test_equity_chart_consumes_governed_metric_v2_series() -> None:
    figure = make_equity_curves_facet(
        {
            "horizon_30d_size_100k": [
                {"session": "2026-08-03", "net_equity": 100_000.0, "total_return": 0.0},
                {
                    "session": "2026-08-04",
                    "net_equity": 101_000.0,
                    "total_return": 0.01,
                },
            ]
        }
    )

    assert tuple(figure.data[0].x) == ("2026-08-03", "2026-08-04")
    assert tuple(figure.data[0].y) == (0.0, 0.01)


def test_overview_discloses_governed_headline_data_quality() -> None:
    source = Path("tradingagents/dashboard/pages/overview.py").read_text()

    assert "valid sessions" in source
    assert "missing/stale marks" in source


def test_reporting_discloses_policy_audit_as_governance_not_alpha() -> None:
    overview = Path("tradingagents/dashboard/pages/overview.py").read_text()
    readme = Path("README.md").read_text()
    architecture = Path("AUTORESEARCH_ARCHITECTURE_MAP.md").read_text()

    assert "governance evidence only, not alpha validation" in overview
    assert "Accepted/trimmed/rejected are recommendation decisions" in overview
    assert "journal-only/consumed/cutoff/committee-not-selected are signals" in overview
    assert "not alpha validation" in readme
    assert "30/60/90-session gates" in architecture
    assert "premium, assignment, expiry, and contract-mark" in readme


def test_returns_page_renders_the_governed_equity_series() -> None:
    source = Path("tradingagents/dashboard/pages/returns.py").read_text()

    assert "make_equity_curves_facet" in source
    assert "cohort_series" in source
    assert "Persisted net-equity history" in source
