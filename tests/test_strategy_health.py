from datetime import date

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
        {"finnhub": {"error": "timeout"}},
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
