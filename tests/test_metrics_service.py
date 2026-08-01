from __future__ import annotations

import ast
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace

import pytest

from tradingagents.strategies.execution.models import (
    BenchmarkObservation,
    SignalRecord,
)
from tradingagents.strategies.metrics.models import (
    METRIC_SCHEMA_VERSION,
    MetricEpoch,
    PortfolioMetrics,
    SignalMetricRecord,
)
from tradingagents.strategies.metrics.service import MetricsService
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.orchestration.cohort_comparison import CohortComparison
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


SESSIONS = (date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 6))
NOW = datetime(2026, 8, 6, 21, tzinfo=UTC)


@pytest.fixture
def ledger_factory(tmp_path, request):
    ledgers: list[PortfolioLedger] = []

    def build(cohort_id: str, root: Path | None = None) -> PortfolioLedger:
        target = root or tmp_path
        ledger = PortfolioLedger(
            target / cohort_id / "portfolio.db",
            cohort_id,
            Decimal("100000"),
        )
        ledgers.append(ledger)
        return ledger

    def close() -> None:
        for ledger in ledgers:
            ledger.close()

    request.addfinalizer(close)
    return build


def _epoch(epoch_id: str = "epoch-1") -> MetricEpoch:
    return MetricEpoch(
        epoch_id=epoch_id,
        generation_id="gen-1",
        generation_commit="abc123",
        behavior_hash="behavior",
        config_hash="config",
        metric_schema_version=METRIC_SCHEMA_VERSION,
        execution_clock_version="next-open-v1",
        pricing_version="raw-v1",
        cost_model_version="cost-v1",
        start_session=SESSIONS[0],
        end_session=None,
        status="open",
        boundary_reason="initial",
    )


def _signal(
    signal_id: str = "signal-1",
    *,
    direction: str = "long",
    epoch_id: str = "epoch-1",
) -> SignalRecord:
    return SignalRecord(
        signal_id=signal_id,
        epoch_id=epoch_id,
        policy_id="policy-1",
        event_key="event-1",
        strategy="litigation",
        ticker="AAPL",
        direction=direction,
        event_at=None,
        observed_at=NOW,
        reference_session=SESSIONS[0],
        reference_close=Decimal("100"),
        decision_at=NOW,
        evidence_hash="evidence",
    )


def _record_window(
    ledger: PortfolioLedger,
    epoch_id: str,
    sessions: tuple[date, ...],
) -> None:
    for offset, session in enumerate(sessions):
        observed_at = NOW.replace(day=session.day)
        ledger.mark(session, {}, epoch_id, observed_at)
        for symbol, close in (("SPY", 100 + offset), ("BIL", 100 + offset / 10)):
            ledger.record_benchmark_observation(
                BenchmarkObservation(
                    observation_id=f"{ledger.cohort_id}-{epoch_id}-{symbol}-{session}",
                    cohort_id=ledger.cohort_id,
                    epoch_id=epoch_id,
                    session=session,
                    symbol=symbol,
                    close=Decimal(str(close)),
                    return_basis="total_return_adjusted",
                    source="fixture",
                    observed_at=observed_at,
                    valid=True,
                    invalid_reason="",
                )
            )


def _portfolio(cohort_id: str, epoch_id: str, total_return: float) -> PortfolioMetrics:
    return PortfolioMetrics(
        cohort_id=cohort_id,
        epoch_id=epoch_id,
        metric_schema_version=2,
        start_session=SESSIONS[0],
        end_session=SESSIONS[1],
        valuation_at=NOW,
        benchmark_at=NOW,
        valid_sessions=2,
        total_return=total_return,
        gross_return=total_return,
        matched_benchmark_return=0.0,
        matched_excess_return=total_return,
        annualized_daily_net_sharpe=None,
        sharpe_return_count=1,
        annualized_matched_information_ratio=None,
        information_ratio_return_count=1,
        max_drawdown=0.0,
        long_weight=0.0,
        short_weight=0.0,
        gross_weight=0.0,
        net_weight=0.0,
        cash_weight=1.0,
        cumulative_costs={
            "slippage": 0.0,
            "commission": 0.0,
            "other_fees": 0.0,
            "borrow": 0.0,
            "financing": 0.0,
        },
        unique_catalysts=0,
        strategy_decisions=0,
        fills=0,
        closed_trades=0,
        missing_mark_count=0,
        stale_mark_count=0,
    )


def test_cohort_comparison_delegates_to_metrics_service() -> None:
    service = Mock(spec=MetricsService)
    service.generation_report.return_value = {"metric_schema_version": 2}
    comparison = CohortComparison(metrics_service=service)

    assert comparison.compare(epoch_id="epoch-1") == {"metric_schema_version": 2}
    service.generation_report.assert_called_once_with(epoch_id="epoch-1")


def test_constructor_requires_exact_immutable_unique_database_bindings(
    tmp_path, ledger_factory
) -> None:
    ledger = ledger_factory("cohort-a")
    with pytest.raises(ValueError, match="exactly match"):
        MetricsService(tmp_path, {"declared": ledger})

    bindings = {"cohort-a": ledger}
    service = MetricsService(tmp_path, bindings)
    bindings.clear()
    assert service.cohort_ids == ("cohort-a",)

    reopened = PortfolioLedger.open_existing(ledger.path)
    try:
        reopened.cohort_id = "cohort-b"
        with pytest.raises(ValueError, match="same ledger database"):
            MetricsService(
                tmp_path / "duplicate",
                {"cohort-a": ledger, "cohort-b": reopened},
            )
    finally:
        reopened.close()


def test_metric_store_read_only_open_blocks_mutation(tmp_path) -> None:
    path = tmp_path / "metrics_v2.sqlite3"
    writable = MetricStore(path)
    writable.save_epoch(_epoch())

    read_only = MetricStore.open_existing(path)

    assert read_only.read_only is True
    assert read_only.load_epoch("epoch-1") == _epoch()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        read_only.save_epoch(_epoch("epoch-2"))


def test_cohort_report_reads_once_bounds_fills_and_aggregates_once(
    tmp_path, ledger_factory, monkeypatch
) -> None:
    import tradingagents.strategies.metrics.service as service_module

    ledger = ledger_factory("cohort-a")
    _record_window(ledger, "epoch-1", SESSIONS[:3])
    signal = _signal()
    ledger.record_signal(signal)
    service = MetricsService(tmp_path, {"cohort-a": ledger})
    service.store.save_epoch(_epoch())

    counts = {"snapshots": 0, "benchmarks": 0, "signals": 0, "fills": 0, "aggregate": 0}
    original_snapshots = ledger.read_snapshots
    original_benchmarks = ledger.read_benchmark_observations
    original_fills = ledger.read_fills
    original_aggregate = service_module.portfolio_metrics

    def read_snapshots(*args, **kwargs):
        counts["snapshots"] += 1
        return original_snapshots(*args, **kwargs)

    def read_benchmarks(*args, **kwargs):
        counts["benchmarks"] += 1
        return original_benchmarks(*args, **kwargs)

    def read_signals(*args, **kwargs):
        counts["signals"] += 1
        return [signal, signal]

    def read_fills(*args, **kwargs):
        counts["fills"] += 1
        assert args == (SESSIONS[0], SESSIONS[2])
        assert kwargs == {"epoch_id": "epoch-1"}
        return original_fills(*args, **kwargs)

    def aggregate(**kwargs):
        counts["aggregate"] += 1
        rows = tuple(kwargs["signals"])
        assert rows == (
            SignalMetricRecord(
                event_key=signal.event_key,
                signal_id=signal.signal_id,
                epoch_id=signal.epoch_id,
                policy_id=signal.policy_id,
                strategy=signal.strategy,
                ticker=signal.ticker,
                direction=signal.direction,
                decision_at=signal.decision_at,
                reference_session=signal.reference_session,
            ),
        )
        kwargs["signals"] = rows
        return original_aggregate(**kwargs)

    monkeypatch.setattr(ledger, "read_snapshots", read_snapshots)
    monkeypatch.setattr(ledger, "read_benchmark_observations", read_benchmarks)
    monkeypatch.setattr(ledger, "read_signals", read_signals)
    monkeypatch.setattr(ledger, "read_fills", read_fills)
    monkeypatch.setattr(service_module, "portfolio_metrics", aggregate)

    report = service.cohort_report("cohort-a", "epoch-1")

    assert report.valid_sessions == 3
    assert counts == {
        "snapshots": 1,
        "benchmarks": 1,
        "signals": 1,
        "fills": 1,
        "aggregate": 1,
    }


def test_cohort_report_fails_conflicting_signals_and_gapped_window_before_fills(
    tmp_path, ledger_factory, monkeypatch
) -> None:
    ledger = ledger_factory("cohort-a")
    _record_window(ledger, "epoch-1", SESSIONS[:2])
    service = MetricsService(tmp_path, {"cohort-a": ledger})
    service.store.save_epoch(_epoch())
    monkeypatch.setattr(
        ledger,
        "read_signals",
        lambda **_kwargs: [
            _signal("long", direction="long"),
            _signal("short", direction="short"),
        ],
    )
    with pytest.raises(ValueError, match="conflicting signal identities"):
        service.cohort_report("cohort-a", "epoch-1")

    gap_ledger = ledger_factory("cohort-gap")
    _record_window(gap_ledger, "epoch-1", (SESSIONS[0], SESSIONS[2]))
    gap_service = MetricsService(tmp_path / "gap", {"cohort-gap": gap_ledger})
    gap_service.store.save_epoch(_epoch())
    fills = Mock(
        side_effect=AssertionError("fills must not be read for an invalid window")
    )
    monkeypatch.setattr(gap_ledger, "read_fills", fills)
    with pytest.raises(ValueError, match="contiguous"):
        gap_service.cohort_report("cohort-gap", "epoch-1")
    fills.assert_not_called()


def test_exact_epoch_status_allows_closed_and_rejects_missing_or_invalid(
    tmp_path, ledger_factory
) -> None:
    ledger = ledger_factory("cohort-a")
    _record_window(ledger, "closed", SESSIONS[:2])
    service = MetricsService(tmp_path, {"cohort-a": ledger})
    service.store.save_epoch(_epoch("closed"))
    service.store.close_epoch("closed", SESSIONS[1], "historical")
    assert service.cohort_report("cohort-a", "closed").epoch_id == "closed"

    service.store.save_epoch(_epoch("invalid"))
    service.store.invalidate_epoch("invalid", SESSIONS[1], "gap")
    with pytest.raises(ValueError, match="invalid"):
        service.cohort_report("cohort-a", "invalid")
    with pytest.raises(KeyError):
        service.cohort_report("cohort-a", "missing")
    with pytest.raises(KeyError, match="unknown cohort"):
        service.cohort_report("cohort-b", "closed")


def test_generation_report_empty_current_historical_and_panel_rules(
    tmp_path, ledger_factory, monkeypatch
) -> None:
    empty_ledger = ledger_factory("horizon_30d_size_100k", tmp_path / "empty-ledger")
    empty = MetricsService(tmp_path / "empty", {empty_ledger.cohort_id: empty_ledger})
    assert empty.generation_report() == {
        "metric_schema_version": 2,
        "epoch": None,
        "headline_books": {},
        "scenario_panel": None,
        "scenario_panel_available": False,
        "scenario_panel_unavailable_reason": "no_current_epoch",
        "missing_headline_books": [
            "horizon_1y_size_100k",
            "horizon_30d_size_100k",
            "horizon_3m_size_100k",
            "horizon_6m_size_100k",
        ],
        "stress_tests": {},
        "cohort_series": {},
        "dependent_scenarios": True,
    }

    names = [
        "horizon_30d_size_100k",
        "horizon_3m_size_100k",
        "horizon_6m_size_100k",
        "horizon_1y_size_100k",
        "horizon_30d_size_50k",
    ]
    ledgers = {name: ledger_factory(name, tmp_path / "full-ledgers") for name in names}
    service = MetricsService(tmp_path / "full", ledgers)
    service.store.save_epoch(_epoch("historical"))
    service.store.close_epoch("historical", SESSIONS[1], "closed")
    values = {name: index / 100 for index, name in enumerate(names, start=1)}
    monkeypatch.setattr(
        service,
        "cohort_report",
        lambda cohort_id, epoch_id: _portfolio(cohort_id, epoch_id, values[cohort_id]),
    )

    report = service.generation_report(epoch_id="historical")

    assert report["scenario_panel_available"] is True
    assert report["scenario_panel_unavailable_reason"] is None
    assert report["missing_headline_books"] == []
    assert report["scenario_panel"]["total_return"] == pytest.approx(0.025)
    assert set(report["headline_books"]) == set(names[:4])
    assert set(report["stress_tests"]) == {"horizon_30d_size_50k"}
    assert report["epoch"]["status"] == "closed"

    monkeypatch.setattr(
        service,
        "cohort_report",
        lambda cohort_id, epoch_id: replace(
            _portfolio(cohort_id, epoch_id, values[cohort_id]),
            start_session=(
                SESSIONS[1] if cohort_id == "horizon_1y_size_100k" else SESSIONS[0]
            ),
            valid_sessions=(1 if cohort_id == "horizon_1y_size_100k" else 2),
        ),
    )
    mismatched = service.generation_report(epoch_id="historical")
    assert mismatched["scenario_panel"] is None
    assert mismatched["scenario_panel_available"] is False
    assert mismatched["scenario_panel_unavailable_reason"] == (
        "mismatched_headline_windows"
    )

    partial_ledgers = dict(list(ledgers.items())[:3]) | {
        "horizon_30d_size_50k": ledgers["horizon_30d_size_50k"]
    }
    partial = MetricsService(tmp_path / "partial", partial_ledgers)
    partial.store.save_epoch(_epoch("partial"))
    monkeypatch.setattr(
        partial,
        "cohort_report",
        lambda cohort_id, epoch_id: _portfolio(cohort_id, epoch_id, values[cohort_id]),
    )
    partial_report = partial.generation_report(epoch_id="partial")
    assert partial_report["scenario_panel"] is None
    assert partial_report["scenario_panel_available"] is False
    assert partial_report["scenario_panel_unavailable_reason"] == (
        "missing_headline_books"
    )
    assert partial_report["missing_headline_books"] == ["horizon_1y_size_100k"]
    assert set(partial_report["stress_tests"]) == {"horizon_30d_size_50k"}


def test_generation_report_projects_persisted_series_without_network(
    tmp_path, ledger_factory
) -> None:
    ledger = ledger_factory("horizon_30d_size_100k")
    _record_window(ledger, "epoch-1", SESSIONS)
    service = MetricsService(tmp_path, {ledger.cohort_id: ledger})
    service.store.save_epoch(_epoch())

    series = service.generation_report()["cohort_series"][ledger.cohort_id]

    assert [row["net_equity"] for row in series["net_equity_history"]] == [
        100000.0,
        100000.0,
        100000.0,
        100000.0,
    ]
    assert [row["session"] for row in series["benchmarks"]["SPY"]] == [
        session.isoformat() for session in SESSIONS
    ]
    assert [row["return"] for row in series["matched_benchmark_returns"]] == [
        pytest.approx(0.001),
        pytest.approx(1 / 1001),
        pytest.approx(1 / 1002),
    ]


def test_compare_uses_exact_reports_and_contiguous_common_daily_returns(
    tmp_path, ledger_factory
) -> None:
    candidate_ledger = ledger_factory("candidate", tmp_path / "candidate-ledger")
    baseline_ledger = ledger_factory("baseline", tmp_path / "baseline-ledger")
    _record_window(candidate_ledger, "candidate-epoch", SESSIONS[:3])
    _record_window(baseline_ledger, "baseline-epoch", SESSIONS[1:])
    candidate = MetricsService(tmp_path / "candidate", {"candidate": candidate_ledger})
    baseline = MetricsService(tmp_path / "baseline", {"baseline": baseline_ledger})
    candidate.store.save_epoch(_epoch("candidate-epoch"))
    baseline.store.save_epoch(_epoch("baseline-epoch"))

    comparison = candidate.compare(
        "candidate", "candidate-epoch", baseline, "baseline", "baseline-epoch"
    )
    assert comparison.common_sessions == (SESSIONS[2],)

    no_common_ledger = ledger_factory("no-common", tmp_path / "no-common-ledger")
    _record_window(no_common_ledger, "no-common-epoch", SESSIONS[2:])
    no_common = MetricsService(tmp_path / "no-common", {"no-common": no_common_ledger})
    no_common.store.save_epoch(_epoch("no-common-epoch"))
    with pytest.raises(ValueError, match="common session"):
        candidate.compare(
            "candidate", "candidate-epoch", no_common, "no-common", "no-common-epoch"
        )


def test_metrics_modules_and_consumers_have_no_learning_or_local_formulas() -> None:
    imported: list[str] = []
    for path in Path("tradingagents/strategies/metrics").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
    assert not any("learning" in name.split(".") for name in imported)

    paths = (
        Path("tradingagents/strategies/orchestration/cohort_comparison.py"),
        Path("tradingagents/strategies/orchestration/generation_comparison.py"),
    )
    forbidden_names = {
        "_hit_rate",
        "_hit_rate_5d",
        "_sharpe",
        "_total_return",
        "_max_drawdown",
        "_win_rate",
        "_avg_pnl",
        "_avg_return_5d",
    }
    for path in paths:
        tree = ast.parse(path.read_text())
        assert (
            not {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            & forbidden_names
        )
    assert "sum(" not in Path("tradingagents/strategies/metrics/service.py").read_text()
    engine_source = Path(
        "tradingagents/strategies/orchestration/multi_strategy_engine.py"
    ).read_text()
    assert "statistics.stdev" not in engine_source
    assert "load_paper_trades" not in engine_source


def test_dashboard_adapter_exposes_only_v2_metric_books() -> None:
    from tradingagents.dashboard.data_loaders import cohort_metric_books

    report = {
        "headline_books": {"horizon_30d_size_100k": {"fills": 2}},
        "stress_tests": {"horizon_30d_size_5k": {"fills": 1}},
    }

    assert cohort_metric_books(report) == {
        "horizon_30d_size_100k": {"fills": 2},
        "horizon_30d_size_5k": {"fills": 1},
    }
    with pytest.raises(ValueError, match="duplicate cohort"):
        cohort_metric_books(
            {
                "headline_books": {"same": {}},
                "stress_tests": {"same": {}},
            }
        )


def test_generation_cli_pair_parser_and_read_only_ledgers_close(
    tmp_path, monkeypatch
) -> None:
    from scripts.run_generations import (
        _parse_comparison_pair,
        _run_explicit_comparison,
    )
    from tradingagents.strategies.metrics.store import MetricStore
    from tradingagents.strategies.orchestration.generation_comparison import (
        GenerationComparison,
    )

    pair = _parse_comparison_pair("gen-a:cohort-a:epoch-a,gen-b:cohort-b:epoch-b")
    with pytest.raises(Exception, match="GEN:COHORT:EPOCH"):
        _parse_comparison_pair("gen-a:cohort-a,gen-b:cohort-b:epoch-b")

    generations = []
    for generation_id, cohort_id, epoch_id in (
        ("gen-a", "cohort-a", "epoch-a"),
        ("gen-b", "cohort-b", "epoch-b"),
    ):
        root = tmp_path / generation_id
        ledger = PortfolioLedger(
            root / cohort_id / "portfolio.db", cohort_id, Decimal("100")
        )
        ledger.close()
        store = MetricStore(root / "metrics_v2.sqlite3")
        store.save_epoch(replace(_epoch(epoch_id), generation_id=generation_id))
        generations.append(SimpleNamespace(gen_id=generation_id, state_dir=str(root)))

    opened = []
    original_open = PortfolioLedger.open_existing

    def capture(path):
        ledger = original_open(path)
        opened.append(ledger)
        return ledger

    monkeypatch.setattr(PortfolioLedger, "open_existing", staticmethod(capture))
    observed_read_only: list[bool] = []

    def compare_services(self, pairs):
        observed_read_only.extend(
            service.store.read_only for service in self._services.values()
        )
        return {"metric_schema_version": 2, "comparisons": []}

    monkeypatch.setattr(
        GenerationComparison,
        "compare",
        compare_services,
    )
    manager = SimpleNamespace(list_generations=lambda: generations)

    assert _run_explicit_comparison(manager, (pair,))["metric_schema_version"] == 2
    assert observed_read_only == [True, True]
    assert len(opened) == 2
    for ledger in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            ledger.connection.execute("SELECT 1")
