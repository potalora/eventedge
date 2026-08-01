from datetime import date
from time import sleep

from tradingagents.strategies.metrics.health import classify_strategy_run
from tradingagents.strategies.metrics.models import MetricEpoch
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.modules import get_paper_trade_strategies
from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    CohortOrchestrator,
)
from tradingagents.strategies.orchestration.multi_strategy_engine import (
    MultiStrategyEngine,
    _gather_with_timeout,
)


class _ScreenStrategy:
    def __init__(
        self,
        name: str,
        result: list[Candidate] | Exception,
        *,
        data_sources: tuple[str, ...] = ("finnhub",),
    ) -> None:
        self.name = name
        self.data_sources = list(data_sources)
        self._result = result

    def get_default_params(self, *, horizon: str) -> dict:
        return {}

    def screen(self, data: dict, trading_date: str, params: dict) -> list[Candidate]:
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _DefaultParamsFailureStrategy(_ScreenStrategy):
    def get_default_params(self, *, horizon: str) -> dict:
        raise ValueError("bad strategy parameters")


class _OpenBBSource:
    name = "openbb"

    def __init__(self, available: bool) -> None:
        self._available = available

    def is_available(self) -> bool:
        return self._available


class _OpenBBRegistry:
    def __init__(self, available: bool) -> None:
        self.source = _OpenBBSource(available)

    def get(self, name: str):
        return self.source if name == "openbb" else None

    def available_sources(self) -> list[str]:
        return ["openbb"] if self.source.is_available() else []


def test_zero_candidates_with_healthy_sources_is_legitimate_no_event() -> None:
    record = classify_strategy_run(
        epoch_id="epoch-1",
        session=date(2026, 8, 3),
        policy_id="30d",
        strategy="earnings_call",
        data_sources=("finnhub", "yfinance"),
        candidates=[],
        provider_errors={},
        exception=None,
    )

    assert record.status == "legitimate_no_event"
    assert record.evidence["candidate_count"] == 0


def test_provider_error_is_data_failure() -> None:
    record = classify_strategy_run(
        epoch_id="epoch-1",
        session=date(2026, 8, 3),
        policy_id="30d",
        strategy="earnings_call",
        data_sources=("finnhub",),
        candidates=[],
        provider_errors={"finnhub": "timeout"},
        exception=None,
    )

    assert record.status == "data_failure"
    assert record.evidence["provider_errors"] == {"finnhub": "timeout"}


def test_exception_is_strategy_defect() -> None:
    record = classify_strategy_run(
        epoch_id="epoch-1",
        session=date(2026, 8, 3),
        policy_id="30d",
        strategy="filing_analysis",
        data_sources=("edgar",),
        candidates=[],
        provider_errors={},
        exception=ValueError("bad filing"),
    )

    assert record.status == "strategy_defect"
    assert record.evidence["error_type"] == "ValueError"


def test_store_keeps_exactly_one_health_record_per_active_strategy(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    session = date(2026, 8, 3)
    strategies = get_paper_trade_strategies()
    records = [
        classify_strategy_run(
            epoch_id="epoch-1",
            session=session,
            policy_id="foundation-30d",
            strategy=strategy.name,
            data_sources=strategy.data_sources,
            candidates=[],
            provider_errors={},
            exception=None,
        )
        for strategy in strategies
    ]

    assert len({record.strategy for record in records}) == 12
    for record in records:
        store.save_strategy_health(record)
        store.save_strategy_health(record)

    stored = store.read_strategy_health(epoch_id="epoch-1", session=session)
    assert {record.strategy for record in stored} == {
        record.strategy for record in records
    }
    assert len(stored) == 12


def test_screen_and_enrich_returns_health_for_exception_zero_and_signals(
    tmp_path,
) -> None:
    signal = Candidate(
        ticker="AAPL",
        date="2026-08-03",
        direction="long",
        score=0.8,
        metadata={},
    )
    engine = MultiStrategyEngine(
        config={"autoresearch": {"state_dir": str(tmp_path)}},
        strategies=[
            _ScreenStrategy("signals", [signal], data_sources=("yfinance",)),
            _ScreenStrategy("zero", [], data_sources=("yfinance",)),
            _ScreenStrategy("provider_failure", [], data_sources=("finnhub",)),
            _ScreenStrategy(
                "broken", ValueError("bad filing"), data_sources=("edgar",)
            ),
        ],
    )
    engine._build_regime_model = lambda data: {}
    signals, regime, health = engine.screen_and_enrich(
        "2026-08-03",
        {"yfinance": {}, "finnhub": {"error": "timeout"}},
        horizon="30d",
        epoch_id="epoch-1",
        policy_id="policy-1",
    )

    assert "timestamp" in regime
    assert [signal["strategy"] for signal in signals] == ["signals"]
    assert {record.strategy: record.status for record in health} == {
        "signals": "signals",
        "zero": "legitimate_no_event",
        "provider_failure": "data_failure",
        "broken": "strategy_defect",
    }


def test_absent_required_source_is_data_failure(tmp_path) -> None:
    engine = MultiStrategyEngine(
        config={"autoresearch": {"state_dir": str(tmp_path)}},
        strategies=[_ScreenStrategy("missing_source", [], data_sources=("edgar",))],
    )
    engine._build_regime_model = lambda data: {}

    _, _, health = engine.screen_and_enrich(
        "2026-08-03", {"finnhub": {}}, epoch_id="epoch-1", policy_id="policy-1"
    )

    assert health[0].status == "data_failure"
    assert health[0].evidence["provider_errors"] == {
        "edgar": "missing from shared data"
    }


def test_unavailable_required_source_is_explicit_data_failure(tmp_path) -> None:
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry

    engine = MultiStrategyEngine(
        config={"autoresearch": {"state_dir": str(tmp_path)}},
        strategies=[_ScreenStrategy("unavailable_source", [], data_sources=("edgar",))],
        registry=DataSourceRegistry(),
    )
    engine._build_regime_model = lambda data: {}
    data = engine._fetch_all_data("2026-07-01", "2026-08-03")

    _, _, health = engine.screen_and_enrich(
        "2026-08-03", data, epoch_id="epoch-1", policy_id="policy-1"
    )

    assert data["edgar"]["error"] == "source unavailable or skipped"
    assert health[0].status == "data_failure"


def test_available_openbb_enrichment_source_is_not_missing_data_failure(
    tmp_path,
) -> None:
    engine = MultiStrategyEngine(
        config={"autoresearch": {"state_dir": str(tmp_path)}},
        strategies=[_ScreenStrategy("openbb_enriched", [], data_sources=("openbb",))],
        registry=_OpenBBRegistry(available=True),
    )
    engine._build_regime_model = lambda data: {}
    data = engine._fetch_all_data("2026-07-01", "2026-08-03")

    _, _, health = engine.screen_and_enrich(
        "2026-08-03", data, epoch_id="epoch-1", policy_id="policy-1"
    )

    assert data["openbb"] == {"enrichment_only": True}
    assert health[0].status == "legitimate_no_event"


def test_unavailable_openbb_enrichment_source_is_explicit_data_failure(
    tmp_path,
) -> None:
    engine = MultiStrategyEngine(
        config={"autoresearch": {"state_dir": str(tmp_path)}},
        strategies=[_ScreenStrategy("openbb_enriched", [], data_sources=("openbb",))],
        registry=_OpenBBRegistry(available=False),
    )
    engine._build_regime_model = lambda data: {}
    data = engine._fetch_all_data("2026-07-01", "2026-08-03")

    _, _, health = engine.screen_and_enrich(
        "2026-08-03", data, epoch_id="epoch-1", policy_id="policy-1"
    )

    assert data["openbb"]["error"] == "source unavailable or skipped"
    assert health[0].status == "data_failure"


def test_fetch_exception_is_retained_and_classified_as_data_failure(tmp_path) -> None:
    def failing_fetch() -> dict:
        raise RuntimeError("upstream unavailable")

    data = _gather_with_timeout({"finnhub": (failing_fetch, ())}, timeout_s=1)
    engine = MultiStrategyEngine(
        config={"autoresearch": {"state_dir": str(tmp_path)}},
        strategies=[_ScreenStrategy("provider_failure", [])],
    )
    engine._build_regime_model = lambda data: {}

    _, _, health = engine.screen_and_enrich(
        "2026-08-03",
        data,
        epoch_id="epoch-1",
        policy_id="policy-1",
    )

    assert data["finnhub"]["error"] == "RuntimeError: upstream unavailable"
    assert health[0].status == "data_failure"


def test_timed_out_fetch_is_retained_and_classified_as_data_failure(tmp_path) -> None:
    def slow_fetch() -> dict:
        sleep(0.05)
        return {}

    data = _gather_with_timeout({"finnhub": (slow_fetch, ())}, timeout_s=0.001)
    engine = MultiStrategyEngine(
        config={"autoresearch": {"state_dir": str(tmp_path)}},
        strategies=[_ScreenStrategy("provider_timeout", [])],
    )
    engine._build_regime_model = lambda data: {}

    _, _, health = engine.screen_and_enrich(
        "2026-08-03",
        data,
        epoch_id="epoch-1",
        policy_id="policy-1",
    )

    assert data["finnhub"]["error"] == "timeout after 0.001s"
    assert health[0].status == "data_failure"


def test_default_params_exception_is_strategy_defect_and_other_health_survives(
    tmp_path,
) -> None:
    engine = MultiStrategyEngine(
        config={"autoresearch": {"state_dir": str(tmp_path)}},
        strategies=[
            _DefaultParamsFailureStrategy("bad_params", []),
            _ScreenStrategy("zero", []),
        ],
    )
    engine._build_regime_model = lambda data: {}

    _, _, health = engine.screen_and_enrich(
        "2026-08-03",
        {"finnhub": {}},
        epoch_id="epoch-1",
        policy_id="policy-1",
    )

    assert {record.strategy: record.status for record in health} == {
        "bad_params": "strategy_defect",
        "zero": "legitimate_no_event",
    }


def test_incomplete_strategy_health_invalidates_current_epoch(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    session = date(2026, 8, 3)
    store.save_epoch(
        MetricEpoch(
            epoch_id="epoch-1",
            generation_id="gen-004",
            generation_commit="a" * 40,
            behavior_hash="behavior",
            config_hash="config",
            metric_schema_version=2,
            execution_clock_version="close-v1",
            pricing_version="price-v1",
            cost_model_version="cost-v1",
            start_session=session,
            end_session=None,
            status="open",
            boundary_reason="started",
        )
    )
    orchestrator = CohortOrchestrator.__new__(CohortOrchestrator)
    orchestrator._metric_store = store
    orchestrator._epoch_id = "epoch-1"
    orchestrator._active_strategy_names = frozenset(
        strategy.name for strategy in get_paper_trade_strategies()
    )
    record = classify_strategy_run(
        epoch_id="epoch-1",
        session=session,
        policy_id="foundation-30d",
        strategy="earnings_call",
        data_sources=("finnhub",),
        candidates=[],
        provider_errors={},
        exception=None,
    )

    assert (
        orchestrator._persist_horizon_health([record], session, "foundation-30d")
        is False
    )
    epoch = store.current_epoch()
    assert epoch is not None
    assert epoch.status == "invalid"
    assert epoch.boundary_reason == "unclassified_strategy_silence"


def test_impostor_strategy_name_invalidates_current_epoch(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    session = date(2026, 8, 3)
    store.save_epoch(
        MetricEpoch(
            epoch_id="epoch-1",
            generation_id="gen-004",
            generation_commit="a" * 40,
            behavior_hash="behavior",
            config_hash="config",
            metric_schema_version=2,
            execution_clock_version="close-v1",
            pricing_version="price-v1",
            cost_model_version="cost-v1",
            start_session=session,
            end_session=None,
            status="open",
            boundary_reason="started",
        )
    )
    active_names = [strategy.name for strategy in get_paper_trade_strategies()]
    records = [
        classify_strategy_run(
            epoch_id="epoch-1",
            session=session,
            policy_id="foundation-30d",
            strategy=("impostor" if index == 0 else name),
            data_sources=(),
            candidates=[],
            provider_errors={},
            exception=None,
        )
        for index, name in enumerate(active_names)
    ]
    orchestrator = CohortOrchestrator.__new__(CohortOrchestrator)
    orchestrator._metric_store = store
    orchestrator._epoch_id = "epoch-1"
    orchestrator._active_strategy_names = frozenset(active_names)

    assert (
        orchestrator._persist_horizon_health(records, session, "foundation-30d")
        is False
    )
    assert store.current_epoch().boundary_reason == "unclassified_strategy_silence"


def test_invalid_health_batch_writes_nothing_and_invalidates_exact_epoch(
    tmp_path,
) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    session = date(2026, 8, 3)
    epochs = [
        MetricEpoch(
            epoch_id=epoch_id,
            generation_id="gen-004",
            generation_commit="a" * 40,
            behavior_hash="behavior",
            config_hash="config",
            metric_schema_version=2,
            execution_clock_version="close-v1",
            pricing_version="price-v1",
            cost_model_version="cost-v1",
            start_session=session,
            end_session=None,
            status="open",
            boundary_reason="started",
        )
        for epoch_id in ("epoch-1", "epoch-2")
    ]
    for epoch in epochs:
        store.save_epoch(epoch)
    active_names = [strategy.name for strategy in get_paper_trade_strategies()]
    records = [
        classify_strategy_run(
            epoch_id=("wrong-epoch" if index == 0 else "epoch-1"),
            session=session,
            policy_id="foundation-30d",
            strategy=name,
            data_sources=(),
            candidates=[],
            provider_errors={},
            exception=None,
        )
        for index, name in enumerate(active_names)
    ]
    orchestrator = CohortOrchestrator.__new__(CohortOrchestrator)
    orchestrator._metric_store = store
    orchestrator._epoch_id = "epoch-1"
    orchestrator._active_strategy_names = frozenset(active_names)

    assert (
        orchestrator._persist_horizon_health(records, session, "foundation-30d")
        is False
    )
    assert store.read_strategy_health(epoch_id="epoch-1", session=session) == ()
    assert store.load_epoch("epoch-1").status == "invalid"
    assert store.load_epoch("epoch-2").status == "open"


def test_configured_health_policy_is_horizon_scoped_without_conflicts(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics.sqlite3")
    session = date(2026, 8, 3)
    store.save_epoch(
        MetricEpoch(
            epoch_id="epoch-1",
            generation_id="gen-004",
            generation_commit="a" * 40,
            behavior_hash="behavior",
            config_hash="config",
            metric_schema_version=2,
            execution_clock_version="close-v1",
            pricing_version="price-v1",
            cost_model_version="cost-v1",
            start_session=session,
            end_session=None,
            status="open",
            boundary_reason="started",
        )
    )
    active_names = [strategy.name for strategy in get_paper_trade_strategies()]
    orchestrator = CohortOrchestrator.__new__(CohortOrchestrator)
    orchestrator._metric_store = store
    orchestrator._epoch_id = "epoch-1"
    orchestrator._active_strategy_names = frozenset(active_names)
    orchestrator._base_config = {
        "autoresearch": {"paper_ledger": {"policy_id": "configured-policy"}}
    }

    policies = [
        orchestrator._policy_id_for_horizon(horizon) for horizon in ("30d", "3m")
    ]
    for policy_id in policies:
        records = [
            classify_strategy_run(
                epoch_id="epoch-1",
                session=session,
                policy_id=policy_id,
                strategy=name,
                data_sources=(),
                candidates=[],
                provider_errors={},
                exception=None,
            )
            for name in active_names
        ]
        assert orchestrator._persist_horizon_health(records, session, policy_id)

    stored = store.read_strategy_health(epoch_id="epoch-1", session=session)
    assert len(stored) == 24
    assert set(policies) == {record.policy_id for record in stored}
