"""30-day simulation harness for the paper trading pipeline.

Validates the entire pipeline end-to-end with synthetic data:
1. Broker reconstruction across days
2. Exact XNYS-session outcomes with raw next-open and close prices
3. Idempotency (double-run same date produces no duplicates)
4. Full 30-day lifecycle: open, hold, exit, back-fill, learn
5. 2-cohort divergence over 30 days

All LLM and external API calls are mocked. Deterministic via seeded RNG.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import sqlite3
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from tradingagents.strategies.execution import (
    CorporateAction,
    MarketBar,
    OrderIntent,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.execution.price_source import AdjustedClose

from tradingagents.strategies.orchestration.cohort_orchestrator import (
    CohortConfig,
    CohortOrchestrator,
)
from tradingagents.strategies.orchestration.multi_strategy_engine import (
    MultiStrategyEngine,
)
from tradingagents.strategies.orchestration.session_executor import (
    CorporateActionBatchError,
    PHASES,
    SessionExecutor,
)
from tradingagents.strategies.orchestration.trading_calendar import (
    next_session,
    previous_session,
    session_close,
)
from tradingagents.strategies.trading.portfolio_committee import (
    TradeRecommendation,
)
from tradingagents.strategies.metrics.models import SignalMetricRecord
from tradingagents.strategies.metrics.outcomes import OutcomeCalculator
from tradingagents.strategies.state.state import StateManager
from tradingagents.strategies.modules.base import Candidate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TICKERS = ["AAPL", "MSFT", "AMZN", "TSLA", "NVDA"]
BASE_DATE = datetime(2026, 4, 1)
RNG = np.random.RandomState(42)


def _make_price_df(
    ticker: str,
    base_price: float,
    start: str = "2026-03-01",
    days: int = 60,
    seed: int = 42,
) -> pd.DataFrame:
    """Deterministic price series with slight daily drift."""
    rng = np.random.RandomState(seed + hash(ticker) % 10000)
    dates = pd.bdate_range(start=start, periods=days)
    returns = rng.normal(0.001, 0.01, size=days)
    prices = [base_price]
    for r in returns[1:]:
        prices.append(prices[-1] * (1 + r))
    return pd.DataFrame(
        {"Close": prices[:days], "Volume": [1_000_000] * days},
        index=dates[:days],
    )


def _build_price_cache(days: int = 60) -> dict[str, pd.DataFrame]:
    """Build deterministic price cache for all test tickers."""
    bases = {"AAPL": 170.0, "MSFT": 420.0, "AMZN": 190.0, "TSLA": 250.0, "NVDA": 130.0}
    return {
        ticker: _make_price_df(ticker, bp, days=days) for ticker, bp in bases.items()
    }


class FakeStrategy:
    """Minimal strategy for testing."""

    track = "paper_trade"
    data_sources = ["yfinance"]

    def __init__(self, name: str = "fake_strat", hold_days: int = 5):
        self.name = name
        self._hold_days = hold_days

    def get_param_space(self, horizon: str = "30d"):
        return {"hold_days": (3, 30)}

    def get_default_params(self, horizon: str = "30d"):
        return {"hold_days": self._hold_days}

    def screen(self, data, date, params):
        return [
            Candidate(
                ticker="AAPL",
                date=date,
                direction="long",
                score=5.0,
                metadata={
                    "source": "test",
                    "event_key": f"{self.name}:{date}",
                    "observed_at": f"{date}T19:00:00+00:00",
                },
            )
        ]

    def check_exit(
        self, ticker, entry_price, current_price, holding_days, params, data
    ):
        hold = params.get("hold_days", self._hold_days)
        if holding_days >= hold:
            return True, "holding_period"
        return False, ""

    def build_propose_prompt(self, context):
        return "test"


class FakeStrategy2(FakeStrategy):
    def __init__(self):
        super().__init__(name="fake_strat_2", hold_days=5)

    def screen(self, data, date, params):
        return [
            Candidate(
                ticker="MSFT",
                date=date,
                direction="long",
                score=4.0,
                metadata={
                    "source": "test",
                    "event_key": f"{self.name}:{date}",
                    "observed_at": f"{date}T19:00:00+00:00",
                },
            )
        ]


class _HealthPaddingStrategy:
    """Deterministic no-event strategy used only to satisfy health coverage."""

    track = "paper_trade"
    data_sources: list[str] = []

    def __init__(self, name: str) -> None:
        self.name = name

    def get_default_params(self, horizon: str = "30d") -> dict:
        return {}

    def screen(self, data, date, params) -> list[Candidate]:
        return []

    def check_exit(
        self, ticker, entry_price, current_price, holding_days, params, data
    ):
        return False, ""

    def build_propose_prompt(self, context):
        return "fixture health padding"


def _with_health_padding(strategies: list[object]) -> list[object]:
    """Pad test strategies to production's exact 12-strategy health contract."""
    names = {strategy.name for strategy in strategies}
    if len(names) != len(strategies):
        raise ValueError("authoritative fixture strategies must have unique names")
    if len(strategies) > 12:
        raise ValueError("authoritative fixture supports at most 12 strategies")
    index = 1
    while len(strategies) < 12:
        name = f"fixture_health_padding_{index}"
        index += 1
        if name not in names:
            strategies.append(_HealthPaddingStrategy(name))
            names.add(name)
    return strategies


def _base_config(state_dir: str) -> dict:
    """Minimal config for test isolation."""
    return {
        "autoresearch": {
            "state_dir": state_dir,
            "total_capital": 5000,
            "paper_trade": {
                "min_trades_for_evaluation": 2,
                "portfolio_committee_enabled": False,
            },
        },
        "execution": {"mode": "paper"},
    }


def _build_engine(
    state_dir: str,
    strategies=None,
    adaptive: bool = False,
) -> tuple[MultiStrategyEngine, StateManager]:
    config = _base_config(state_dir)
    state = StateManager(state_dir)
    strategies = strategies or [FakeStrategy(), FakeStrategy2()]
    engine = MultiStrategyEngine(
        config=config,
        strategies=strategies,
        state_manager=state,
        use_llm=False,
        adaptive_confidence=adaptive,
    )
    return engine, state


def _make_fake_committee(max_per_day: int = 3):
    """Return a side_effect function for mocked PortfolioCommittee.synthesize."""

    def fake_synthesize(
        signals,
        regime_context=None,
        strategy_confidence=None,
        current_positions=None,
        total_capital=None,
        **kwargs,
    ):
        recs = []
        seen: set[str] = set()
        for s in signals[:max_per_day]:
            if s["ticker"] not in seen:
                recs.append(
                    TradeRecommendation(
                        ticker=s["ticker"],
                        direction=s["direction"],
                        position_size_pct=0.08,
                        confidence=0.6,
                        rationale="test recommendation",
                        contributing_strategies=[s["strategy"]],
                    )
                )
                seen.add(s["ticker"])
        return recs

    return fake_synthesize


# ===========================================================================
# 2. TestExactSessionOutcomePrices
# ===========================================================================


class TestExactSessionOutcomePrices:
    """Verify v2 outcomes use exact raw XNYS bars and one short sign flip."""

    @staticmethod
    def _signal(direction: str) -> SignalMetricRecord:
        return SignalMetricRecord(
            event_key="event-aapl",
            signal_id=f"signal-{direction}",
            epoch_id="epoch",
            policy_id="30d",
            strategy="test_strat",
            ticker="AAPL",
            direction=direction,
            decision_at=datetime(2026, 8, 3, 20, tzinfo=timezone.utc),
            reference_session=date(2026, 8, 3),
        )

    @staticmethod
    def _bars(entry_open: str, exit_close: str) -> dict[tuple[str, date], MarketBar]:
        fetched_at = datetime(2026, 8, 10, 22, tzinfo=timezone.utc)
        return {
            ("AAPL", date(2026, 8, 4)): MarketBar(
                "AAPL",
                date(2026, 8, 4),
                Decimal(entry_open),
                Decimal("101"),
                Decimal("99"),
                Decimal("100"),
                "fixture",
                fetched_at,
                False,
            ),
            ("AAPL", date(2026, 8, 10)): MarketBar(
                "AAPL",
                date(2026, 8, 10),
                Decimal("105"),
                Decimal("111"),
                Decimal("104"),
                Decimal(exit_close),
                "fixture",
                fetched_at,
                False,
            ),
        }

    def test_five_sessions_uses_exact_next_open_and_exit_close(self):
        outcome = OutcomeCalculator().build(
            self._signal("long"), 5, self._bars("100", "110")
        )

        assert outcome.entry_price == Decimal("100")
        assert outcome.exit_price == Decimal("110")
        assert outcome.raw_return == Decimal("0.1")

    def test_short_negates_raw_return_once(self):
        outcome = OutcomeCalculator().build(
            self._signal("short"), 5, self._bars("100", "110")
        )

        assert outcome.raw_return == Decimal("0.1")
        assert outcome.signed_return == Decimal("-0.1")


# ===========================================================================
# 3. TestIdempotencyDoubleRun
# ===========================================================================


class AuthoritativePriceSource:
    """Exact raw/action/adjusted fixture with call accounting."""

    def __init__(self):
        self.raw_calls: list[tuple[tuple[str, ...], date]] = []
        self.action_calls: list[tuple[tuple[str, ...], date]] = []
        self.benchmark_calls: list[date] = []

    def get_daily_bars(
        self, tickers, start_session, end_session_inclusive, adjusted=False
    ):
        assert start_session == end_session_inclusive
        assert adjusted is False
        session = start_session
        self.raw_calls.append((tuple(sorted(tickers)), session))
        fetched_at = datetime.now(timezone.utc)
        return {
            (ticker, session): MarketBar(
                ticker,
                session,
                Decimal("100"),
                Decimal("102"),
                Decimal("99"),
                Decimal("101"),
                "fixture-raw",
                fetched_at,
                False,
            )
            for ticker in tickers
        }

    def get_corporate_actions(self, tickers, session):
        self.action_calls.append((tuple(sorted(tickers)), session))
        return []

    def get_total_return_closes(self, symbols, start_session, end_session_inclusive):
        assert start_session == end_session_inclusive
        session = start_session
        self.benchmark_calls.append(session)
        fetched_at = datetime.now(timezone.utc)
        return {
            (symbol, session): AdjustedClose(
                symbol,
                session,
                Decimal("650") if symbol == "SPY" else Decimal("91"),
                "fixture-adjusted",
                fetched_at,
            )
            for symbol in symbols
        }


def _cohort_config(tmp_path, name: str, state_name: str | None = None) -> CohortConfig:
    return CohortConfig(
        name=name,
        state_dir=str(tmp_path / (state_name or name)),
        horizon="30d",
        size_profile="5k",
        use_llm=False,
    )


def _authoritative_orchestrator(
    tmp_path,
    cohorts=1,
    hold_days=2,
    strategy_modules=None,
    model="sonnet",
    *,
    cohort_configs=None,
    source=None,
):
    source = source or AuthoritativePriceSource()
    configs = cohort_configs or [
        _cohort_config(tmp_path, f"cohort_{index}") for index in range(cohorts)
    ]
    config = {
        "execution": {"mode": "paper"},
        "autoresearch": {
            "state_dir": str(tmp_path / "base"),
            "autoresearch_model": model,
            "total_capital": 5000,
            "paper_trade": {"portfolio_committee_enabled": False},
            "paper_ledger": {
                "benchmark_symbols": ["SPY", "BIL"],
                "bar_max_age_hours": 24,
            },
            "risk_gate": {
                "min_position_value": 1,
                "max_position_pct": 0.5,
                "max_positions": 5,
            },
        },
    }
    active_strategies = _with_health_padding(
        list(
            strategy_modules
            if strategy_modules is not None
            else [FakeStrategy(hold_days=hold_days), FakeStrategy2()]
        )
    )
    with patch(
        "tradingagents.strategies.modules.get_paper_trade_strategies",
        return_value=active_strategies,
    ):
        orchestrator = CohortOrchestrator(
            configs,
            config,
            generation_id="gen_test",
            generation_commit="test-commit",
            price_source=source,
        )
    for cohort in orchestrator.cohorts:
        cohort["engine"]._fetch_all_data = lambda start, end: {}
    orchestrator._fetch_openbb_enrichment = lambda signals: {}
    return orchestrator, source


def _authoritative_committee(signals, **kwargs):
    if not signals:
        return []
    signal = signals[0]
    return [
        TradeRecommendation(
            ticker=signal["ticker"],
            direction=signal["direction"],
            position_size_pct=0.20,
            confidence=0.6,
            rationale="fixture",
            contributing_strategies=[signal["strategy"]],
        )
    ]


def _unrepresentable_corporate_gap(
    kind: str, session: date
) -> CorporateActionBatchError:
    normal = CorporateAction(
        "bad-scope-action",
        "MSFT",
        session,
        "split",
        Decimal("2"),
        None,
        "fixture",
        datetime.now(timezone.utc),
        True,
    )
    if kind == "overlong_field":
        actions = (replace(normal, action_id="x" * 257),)
        errors = ("invalid corporate action",)
    elif kind == "overlong_error":
        actions = (normal,)
        errors = ("e" * 4097,)
    elif kind == "item_overflow":
        actions = tuple(
            replace(normal, action_id=f"overflow-{index}") for index in range(2049)
        )
        errors = ("too many actions",)
    elif kind == "byte_overflow":
        huge_decimal = Decimal("9" * 256)
        actions = tuple(
            replace(
                normal,
                action_id=f"{index:04d}-" + "x" * 250,
                ticker="T" * 256,
                ratio=huge_decimal,
                cash_per_share=huge_decimal,
                source="S" * 256,
            )
            for index in range(1900)
        )
        errors = ("oversized canonical audit",)
    else:  # pragma: no cover - parametrization owns this invariant.
        raise AssertionError(kind)
    return CorporateActionBatchError(actions, errors)


def _corrupt_persisted_market_bundle(ledger, session: date) -> None:
    context = ledger.session_execution_context(session)
    assert context is not None
    economic = json.loads(context["economic_inputs_json"])
    raw_bars = economic["market"]["raw_bars"]
    assert raw_bars
    raw_bars[0].pop("open")
    economic_json = json.dumps(economic, sort_keys=True, separators=(",", ":"))
    ledger.connection.execute(
        """
        UPDATE session_execution_contexts
        SET economic_inputs_json = ?, input_digest = ?, market_digest = ?
        WHERE cohort_id = ? AND session = ?
        """,
        (
            economic_json,
            stable_id("session_economic_inputs", economic),
            stable_id("session_market_inputs", economic["market"]),
            ledger.cohort_id,
            session.isoformat(),
        ),
    )
    ledger.connection.commit()


class TestIdempotencyDoubleRun:
    def test_registered_metric_epoch_is_shared_p0_and_v2_identity(self, tmp_path):
        from tradingagents.strategies.metrics.identity import signal_id

        orchestrator, _ = _authoritative_orchestrator(tmp_path, cohorts=2)
        sessions = [date(2026, 3, 30)]
        for _ in range(5):
            sessions.append(next_session(sessions[-1]))

        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            for session in sessions:
                result = orchestrator.run_daily(session.isoformat())

        assert all(not row["error"] for row in result.values())
        stores = {
            id(cohort["executor"].metric_store) for cohort in orchestrator.cohorts
        }
        assert len(stores) == 1
        store = orchestrator.cohorts[0]["executor"].metric_store
        epoch = store.current_epoch()
        assert epoch is not None
        assert epoch.epoch_id == orchestrator._epoch_id
        for cohort in orchestrator.cohorts:
            ledger = cohort["ledger"]
            snapshots = ledger.read_snapshots(sessions[0], sessions[-1])
            benchmarks = ledger.read_benchmark_observations(sessions[0], sessions[-1])
            signals = ledger.read_signals(sessions[0], sessions[-1])
            assert snapshots and benchmarks and signals
            assert {row.epoch_id for row in snapshots} == {epoch.epoch_id}
            assert {row.epoch_id for row in benchmarks} == {epoch.epoch_id}
            assert {row.epoch_id for row in signals} == {epoch.epoch_id}
            for row in signals:
                assert row.signal_id == signal_id(
                    epoch.epoch_id,
                    row.strategy,
                    row.policy_id,
                    row.direction,
                    row.event_key,
                )
        outcomes = store.read_outcomes(epoch.epoch_id)
        assert outcomes
        assert {row.epoch_id for row in outcomes} == {epoch.epoch_id}

    def test_later_model_change_rotates_before_new_ledger_write(self, tmp_path):
        first, _ = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()], model="sonnet"
        )
        first_session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            first.run_daily(first_session.isoformat())
        store = first.cohorts[0]["executor"].metric_store
        old_epoch = store.current_epoch()
        assert old_epoch is not None
        for cohort in first.cohorts:
            cohort["ledger"].close()

        changed, _ = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()], model="opus"
        )
        next_day = next_session(first_session)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            result = changed.run_daily(next_day.isoformat())

        assert not result["cohort_0"]["error"]
        new_epoch = store.current_epoch()
        assert new_epoch is not None
        assert new_epoch.epoch_id != old_epoch.epoch_id
        assert new_epoch.start_session == next_day
        assert store.load_epoch(old_epoch.epoch_id).end_session == first_session
        snapshots = changed.cohorts[0]["ledger"].read_snapshots(next_day, next_day)
        assert {row.epoch_id for row in snapshots} == {new_epoch.epoch_id}

    @pytest.mark.parametrize(
        ("gap", "expected_reason"),
        (("missing", "missing_exit_bar"), ("stale", "stale_exit_bar")),
    )
    def test_due_exit_gap_persists_invalid_then_invalidates_and_replays_read_only(
        self, tmp_path, gap, expected_reason
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        sessions = [date(2026, 3, 30)]
        for _ in range(5):
            sessions.append(next_session(sessions[-1]))
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            for session in sessions[:-1]:
                orchestrator.run_daily(session.isoformat())

            original_get = source.get_daily_bars

            def gap_bars(tickers, start, end, adjusted=False):
                bars = original_get(tickers, start, end, adjusted=adjusted)
                if start == sessions[-1] and ("AAPL", start) in bars:
                    if gap == "missing":
                        bars.pop(("AAPL", start))
                    else:
                        bars[("AAPL", start)] = replace(
                            bars[("AAPL", start)],
                            fetched_at=datetime.now(timezone.utc) - timedelta(hours=48),
                        )
                return bars

            source.get_daily_bars = gap_bars
            calls_before = (
                len(source.raw_calls),
                len(source.action_calls),
                len(source.benchmark_calls),
            )
            result = orchestrator.run_daily(sessions[-1].isoformat())
            calls_after = (
                len(source.raw_calls),
                len(source.action_calls),
                len(source.benchmark_calls),
            )
            replay = orchestrator.run_daily(sessions[-1].isoformat())

        ledger = orchestrator.cohorts[0]["ledger"]
        store = orchestrator.cohorts[0]["executor"].metric_store
        invalid_epoch = store.current_epoch()
        assert result["cohort_0"]["error"]
        assert ledger.session_invalid_reason(sessions[-1])
        assert invalid_epoch is not None and invalid_epoch.status == "invalid"
        rows = store.read_outcomes(invalid_epoch.epoch_id)
        assert len(rows) == 1
        assert rows[0].status == "invalid"
        assert rows[0].entry_price is not None
        assert rows[0].exit_price is None
        assert rows[0].signed_return is None
        assert rows[0].invalid_reason == expected_reason
        assert calls_after == (
            calls_before[0] + 1,
            calls_before[1] + 1,
            calls_before[2] + 1,
        )
        assert replay["cohort_0"]["error"]
        assert (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        ) == calls_after
        assert store.current_epoch() == invalid_epoch
        assert store.read_outcomes(invalid_epoch.epoch_id) == rows

        original_context = orchestrator._metric_epoch_context
        orchestrator._metric_epoch_context = replace(
            original_context, config_hash="same-session-conflict"
        )
        with pytest.raises(ValueError, match="invalidated session context conflict"):
            orchestrator.run_daily(sessions[-1].isoformat())
        assert (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        ) == calls_after
        assert store.current_epoch() == invalid_epoch
        orchestrator._metric_epoch_context = original_context

        source.get_daily_bars = original_get
        next_day = next_session(sessions[-1])
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            clean = orchestrator.run_daily(next_day.isoformat())
        replacement = store.current_epoch()
        assert not clean["cohort_0"]["error"]
        assert replacement is not None
        assert replacement.status == "open"
        assert replacement.epoch_id != invalid_epoch.epoch_id
        assert replacement.start_session == next_day

    @pytest.mark.parametrize(
        "crash_hook",
        (
            "_after_gap_marker",
            "_after_gap_p0_invalidation",
            "_after_gap_metric_invalidation",
        ),
    )
    def test_pending_critical_gap_recovers_each_crash_boundary_without_refetch(
        self, tmp_path, crash_hook
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        sessions = [date(2026, 3, 30)]
        for _ in range(5):
            sessions.append(next_session(sessions[-1]))
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            for session in sessions[:-1]:
                orchestrator.run_daily(session.isoformat())
            original_get = source.get_daily_bars

            def missing_exit(tickers, start, end, adjusted=False):
                bars = original_get(tickers, start, end, adjusted=adjusted)
                bars.pop(("AAPL", start), None)
                return bars

            source.get_daily_bars = missing_exit
            setattr(
                orchestrator,
                crash_hook,
                lambda marker: (_ for _ in ()).throw(
                    RuntimeError(f"crash at {crash_hook}")
                ),
            )
            with pytest.raises(RuntimeError, match=f"crash at {crash_hook}"):
                orchestrator.run_daily(sessions[-1].isoformat())

            store = orchestrator.cohorts[0]["executor"].metric_store
            original_epoch_id = orchestrator._epoch_id
            pending = store.pending_critical_gap()
            assert pending is not None
            assert pending.epoch_id == original_epoch_id
            assert pending.gap_session == sessions[-1]
            assert pending.reason == "critical_market_data_gap"
            assert pending.cohort_invalid_reasons == {
                "cohort_0": {"AAPL": "missing_exit_bar"}
            }
            gap_ledger = orchestrator.cohorts[0]["ledger"]
            if crash_hook == "_after_gap_marker":
                assert gap_ledger.session_invalid_reason(sessions[-1]) == ""
            else:
                assert gap_ledger.session_invalid_reason(sessions[-1])
            if crash_hook == "_after_gap_metric_invalidation":
                assert store.load_epoch(original_epoch_id).status == "invalid"
            else:
                assert store.load_epoch(original_epoch_id).status == "open"

            calls_before_recovery = (
                len(source.raw_calls),
                len(source.action_calls),
                len(source.benchmark_calls),
            )
            setattr(orchestrator, crash_hook, lambda marker: None)
            replay = orchestrator.run_daily(sessions[-1].isoformat())

            assert replay["cohort_0"]["error"]
            assert (
                len(source.raw_calls),
                len(source.action_calls),
                len(source.benchmark_calls),
            ) == calls_before_recovery
            assert store.pending_critical_gap() is None
            invalid = store.load_epoch(original_epoch_id)
            assert invalid.status == "invalid"
            assert invalid.end_session == sessions[-1]
            assert len(store.read_outcomes(original_epoch_id)) == 1

            source.get_daily_bars = original_get
            later = next_session(sessions[-1])
            clean = orchestrator.run_daily(later.isoformat())

        replacement = store.current_epoch()
        assert not clean["cohort_0"]["error"]
        assert replacement is not None and replacement.status == "open"
        assert replacement.epoch_id != original_epoch_id
        assert replacement.start_session == later

    def test_later_session_recovers_pending_gap_before_opening_replacement(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        sessions = [date(2026, 3, 30)]
        for _ in range(5):
            sessions.append(next_session(sessions[-1]))
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            for session in sessions[:-1]:
                orchestrator.run_daily(session.isoformat())
            original_get = source.get_daily_bars

            def missing_exit(tickers, start, end, adjusted=False):
                bars = original_get(tickers, start, end, adjusted=adjusted)
                bars.pop(("AAPL", start), None)
                return bars

            source.get_daily_bars = missing_exit
            orchestrator._after_gap_p0_invalidation = lambda marker: (
                _ for _ in ()
            ).throw(RuntimeError("crash before epoch invalidation"))
            with pytest.raises(RuntimeError, match="crash before epoch invalidation"):
                orchestrator.run_daily(sessions[-1].isoformat())

            store = orchestrator.cohorts[0]["executor"].metric_store
            executor = orchestrator.cohorts[0]["executor"]
            original_epoch_id = orchestrator._epoch_id
            original_complete = store.complete_critical_gap
            original_ensure = executor.ensure_metric_epoch
            events = []

            def recording_complete(marker_id):
                events.append("complete_gap")
                return original_complete(marker_id)

            def recording_ensure(context, session):
                events.append("ensure_epoch")
                return original_ensure(context, session)

            store.complete_critical_gap = recording_complete
            executor.ensure_metric_epoch = recording_ensure
            orchestrator._after_gap_p0_invalidation = lambda marker: None
            source.get_daily_bars = original_get
            calls_before = len(source.raw_calls)
            later = next_session(sessions[-1])
            clean = orchestrator.run_daily(later.isoformat())

        assert events[:2] == ["complete_gap", "ensure_epoch"]
        assert store.load_epoch(original_epoch_id).status == "invalid"
        replacement = store.current_epoch()
        assert replacement is not None and replacement.epoch_id != original_epoch_id
        assert replacement.start_session == later
        assert not clean["cohort_0"]["error"]
        assert all(call[1] == later for call in source.raw_calls[calls_before:])

    def test_multi_cohort_due_gap_writes_before_one_shared_invalidation(self, tmp_path):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, cohorts=2, strategy_modules=[FakeStrategy()]
        )
        sessions = [date(2026, 3, 30)]
        for _ in range(5):
            sessions.append(next_session(sessions[-1]))
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            for session in sessions[:-1]:
                orchestrator.run_daily(session.isoformat())
            original_get = source.get_daily_bars

            def missing_bars(tickers, start, end, adjusted=False):
                bars = original_get(tickers, start, end, adjusted=adjusted)
                bars.pop(("AAPL", start), None)
                return bars

            source.get_daily_bars = missing_bars
            store = orchestrator.cohorts[0]["executor"].metric_store
            original_upsert = store.upsert_outcome
            original_invalidate = orchestrator.cohorts[0][
                "executor"
            ].invalidate_metric_epoch
            events = []

            def recording_upsert(outcome):
                events.append(("outcome", outcome.signal_id))
                return original_upsert(outcome)

            def recording_invalidate(*args, **kwargs):
                events.append(("invalidate", "shared"))
                return original_invalidate(*args, **kwargs)

            store.upsert_outcome = recording_upsert
            orchestrator.cohorts[0][
                "executor"
            ].invalidate_metric_epoch = recording_invalidate
            orchestrator._screen_for_horizon = lambda *args, **kwargs: (
                _ for _ in ()
            ).throw(AssertionError("screening must stop after critical gap"))
            result = orchestrator.run_daily(sessions[-1].isoformat())

        assert all(row["error"] for row in result.values())
        assert [event[0] for event in events] == ["outcome", "invalidate"]
        assert len(store.read_outcomes(orchestrator._epoch_id)) == 1

    def test_shared_fetch_failure_without_due_outcomes_invalidates_and_stops(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        session = date(2026, 3, 30)
        executor = orchestrator.cohorts[0]["executor"]
        invalidations = []
        original_invalidate = executor.invalidate_metric_epoch

        def recording_invalidate(*args, **kwargs):
            invalidations.append(args[0])
            return original_invalidate(*args, **kwargs)

        executor.invalidate_metric_epoch = recording_invalidate
        source.get_total_return_closes = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("benchmark fetch failed")
        )
        orchestrator._screen_for_horizon = lambda *args, **kwargs: (
            _ for _ in ()
        ).throw(AssertionError("screening must stop after shared fetch failure"))

        result = orchestrator.run_daily(session.isoformat())

        epoch = executor.metric_store.current_epoch()
        assert result["cohort_0"]["error"]
        assert invalidations == [session]
        assert epoch is not None and epoch.status == "invalid"
        assert executor.metric_store.read_outcomes(epoch.epoch_id) == ()

    def test_candidate_reference_gap_closes_epoch_without_refetch_or_staging(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        session = date(2026, 3, 30)
        cohort = orchestrator.cohorts[0]
        store = cohort["executor"].metric_store
        original_stage = cohort["engine"].screen_and_stage
        cohort["engine"].screen_and_stage = lambda *args, **kwargs: (
            _ for _ in ()
        ).throw(AssertionError("staging must stop after candidate reference gap"))

        with (
            patch(
                "tradingagents.strategies.orchestration.session_executor.ensure_reference_bars",
                side_effect=RuntimeError("deterministic candidate reference gap"),
            ),
            pytest.raises(RuntimeError, match="deterministic candidate reference gap"),
        ):
            orchestrator.run_daily(session.isoformat())

        epoch_id = orchestrator._epoch_id
        epoch = store.load_epoch(epoch_id)
        assert epoch.status == "invalid" and epoch.end_session == session
        assert store.pending_critical_gap() is None
        with sqlite3.connect(store.path) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM critical_gap_markers WHERE status = 'completed'"
                ).fetchone()[0]
                == 1
            )
        snapshot = cohort["ledger"].read_snapshots(
            session, session, epoch_id=epoch_id, valid_only=True
        )
        assert len(snapshot) == 1
        assert cohort["ledger"].session_invalid_reason(session) == ""
        assert (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        ) == (0, 0, 1)

        cohort["engine"].screen_and_stage = original_stage
        later_session = next_session(session)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            clean = orchestrator.run_daily(later_session.isoformat())
        assert not clean["cohort_0"]["error"]
        assert orchestrator._epoch_id != epoch_id

    def test_execution_only_missing_required_bar_invalidates_shared_epoch(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        first_session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
        execution_session = next_session(first_session)
        original_get = source.get_daily_bars

        def missing_required(tickers, start, end, adjusted=False):
            bars = original_get(tickers, start, end, adjusted=adjusted)
            bars.pop(("AAPL", start), None)
            return bars

        source.get_daily_bars = missing_required
        orchestrator._screen_for_horizon = lambda *args, **kwargs: (
            _ for _ in ()
        ).throw(AssertionError("screening must stop after execution bundle failure"))
        result = orchestrator.run_daily(execution_session.isoformat())

        epoch = orchestrator.cohorts[0]["executor"].metric_store.current_epoch()
        assert result["cohort_0"]["error"]
        assert epoch is not None and epoch.status == "invalid"
        assert epoch.end_session == execution_session

    def test_corporate_action_batch_error_invalidates_and_stops(self, tmp_path):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        first_session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
        execution_session = next_session(first_session)
        invalid_action = CorporateAction(
            "bad-scope-action",
            "MSFT",
            execution_session,
            "split",
            Decimal("2"),
            None,
            "fixture",
            datetime.now(timezone.utc),
            True,
        )
        source.get_corporate_actions = lambda tickers, session: (invalid_action,)
        cohort = orchestrator.cohorts[0]
        store = cohort["executor"].metric_store
        events = []
        original_begin_gap = store.begin_critical_gap
        original_reject = cohort["ledger"].reject_corporate_action_batch

        def recording_begin(marker):
            events.append("marker")
            return original_begin_gap(marker)

        def recording_reject(*args, **kwargs):
            events.append("p0_rejection")
            return original_reject(*args, **kwargs)

        store.begin_critical_gap = recording_begin
        cohort["ledger"].reject_corporate_action_batch = recording_reject
        orchestrator._screen_for_horizon = lambda *args, **kwargs: (
            _ for _ in ()
        ).throw(AssertionError("screening must stop after corporate action gap"))

        result = orchestrator.run_daily(execution_session.isoformat())

        epoch = orchestrator.cohorts[0]["executor"].metric_store.current_epoch()
        assert result["cohort_0"]["error"]
        assert epoch is not None and epoch.status == "invalid"
        assert epoch.end_session == execution_session
        assert events == ["marker", "p0_rejection"]
        rejection_count = (
            cohort["ledger"]
            .connection.execute(
                "SELECT COUNT(*) FROM corporate_action_batch_rejections"
            )
            .fetchone()[0]
        )
        assert rejection_count == 1

    def test_corporate_action_audit_failure_stays_pending_and_replays_without_fetch(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        first_session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
        execution_session = next_session(first_session)
        invalid_action = CorporateAction(
            "bad-scope-action",
            "MSFT",
            execution_session,
            "split",
            Decimal("2"),
            None,
            "fixture",
            datetime.now(timezone.utc),
            True,
        )
        source.get_corporate_actions = lambda tickers, session: (invalid_action,)
        cohort = orchestrator.cohorts[0]
        store = cohort["executor"].metric_store
        original_reject = cohort["ledger"].reject_corporate_action_batch

        cohort["ledger"].reject_corporate_action_batch = lambda *args, **kwargs: (
            _ for _ in ()
        ).throw(RuntimeError("audit write failed"))
        with pytest.raises(RuntimeError, match="audit write failed"):
            orchestrator.run_daily(execution_session.isoformat())

        assert store.pending_critical_gap() is not None
        assert (
            cohort["ledger"]
            .connection.execute(
                "SELECT COUNT(*) FROM corporate_action_batch_rejections"
            )
            .fetchone()[0]
            == 0
        )
        calls_before_replay = (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        )

        cohort["ledger"].reject_corporate_action_batch = original_reject
        replay = orchestrator.run_daily(execution_session.isoformat())

        assert replay["cohort_0"]["error"]
        assert store.pending_critical_gap() is None
        assert (
            cohort["ledger"]
            .connection.execute(
                "SELECT COUNT(*) FROM corporate_action_batch_rejections"
            )
            .fetchone()[0]
            == 1
        )
        assert (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        ) == calls_before_replay

    @pytest.mark.parametrize(
        ("detail_kind", "expected_error"),
        (
            ("overlong_field", "corporate action text is invalid"),
            ("overlong_error", "corporate action error is invalid"),
            ("item_overflow", "item count exceeds bound"),
            ("byte_overflow", "payload exceeds byte bound"),
        ),
    )
    def test_unrepresentable_corporate_detail_keeps_minimal_blocker_and_closes_epoch(
        self, tmp_path, detail_kind, expected_error
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        first_session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
        gap_session = next_session(first_session)
        gap_error = _unrepresentable_corporate_gap(detail_kind, gap_session)
        source.get_corporate_actions = lambda tickers, session: gap_error.actions
        store = orchestrator.cohorts[0]["executor"].metric_store
        epoch_id = orchestrator._epoch_id

        with patch.object(
            SessionExecutor,
            "validate_shared_action_response",
            side_effect=gap_error,
        ):
            with pytest.raises(ValueError, match=expected_error):
                orchestrator.run_daily(gap_session.isoformat())

        pending = store.pending_critical_gap()
        assert pending is not None
        assert pending.detail_status == "minimal"
        assert pending.affected_cohorts.keys() == {"cohort_0"}
        assert pending.corporate_action_rejections == {}
        assert store.load_epoch(epoch_id).status == "invalid"
        calls_before = (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        )
        source.get_corporate_actions = lambda tickers, session: ()

        for replay_session in (gap_session, next_session(gap_session)):
            with pytest.raises(ValueError, match="critical gap recovery detail"):
                orchestrator.run_daily(replay_session.isoformat())

            assert store.pending_critical_gap() == pending
            assert (
                len(source.raw_calls),
                len(source.action_calls),
                len(source.benchmark_calls),
            ) == calls_before

    def test_marker_persistence_failure_directly_invalidates_exact_epoch(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        first_session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
        gap_session = next_session(first_session)
        store = orchestrator.cohorts[0]["executor"].metric_store
        epoch_id = orchestrator._epoch_id
        original_begin = store.begin_critical_gap
        store.begin_critical_gap = lambda marker: (_ for _ in ()).throw(
            RuntimeError("marker persistence failed")
        )
        source.get_total_return_closes = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("benchmark gap")
        )

        with pytest.raises(RuntimeError, match="marker persistence failed"):
            orchestrator.run_daily(gap_session.isoformat())

        assert store.pending_critical_gap() is None
        invalid = store.load_epoch(epoch_id)
        assert invalid.status == "invalid" and invalid.end_session == gap_session
        store.begin_critical_gap = original_begin
        source.get_total_return_closes = (
            AuthoritativePriceSource().get_total_return_closes
        )
        later = next_session(gap_session)
        clean = orchestrator.run_daily(later.isoformat())
        assert not clean["cohort_0"]["error"]
        assert store.current_epoch().epoch_id != epoch_id

    def test_binding_derivation_failure_directly_invalidates_exact_epoch(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        first_session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
        gap_session = next_session(first_session)
        cohort = orchestrator.cohorts[0]
        store = cohort["executor"].metric_store
        epoch_id = orchestrator._epoch_id
        original_binding = cohort["ledger"].recovery_binding_id
        cohort["ledger"].recovery_binding_id = lambda: (_ for _ in ()).throw(
            RuntimeError("binding derivation failed")
        )
        source.get_total_return_closes = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("benchmark gap")
        )

        with pytest.raises(RuntimeError, match="binding derivation failed"):
            orchestrator.run_daily(gap_session.isoformat())

        assert store.pending_critical_gap() is None
        invalid = store.load_epoch(epoch_id)
        assert invalid.status == "invalid" and invalid.end_session == gap_session
        cohort["ledger"].recovery_binding_id = original_binding
        source.get_total_return_closes = (
            AuthoritativePriceSource().get_total_return_closes
        )
        later_session = next_session(gap_session)
        clean = orchestrator.run_daily(later_session.isoformat())
        assert not clean["cohort_0"]["error"]
        assert orchestrator._epoch_id != epoch_id

    def test_no_due_shared_gap_marker_names_every_affected_ledger(self, tmp_path):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, cohorts=2, strategy_modules=[FakeStrategy()]
        )
        first_session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
        gap_session = next_session(first_session)
        source.get_total_return_closes = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("shared benchmark gap")
        )
        orchestrator._after_gap_marker = lambda marker: (_ for _ in ()).throw(
            RuntimeError("crash after minimal marker")
        )

        with pytest.raises(RuntimeError, match="crash after minimal marker"):
            orchestrator.run_daily(gap_session.isoformat())

        marker = orchestrator.cohorts[0]["executor"].metric_store.pending_critical_gap()
        assert marker is not None
        assert marker.cohort_invalid_reasons == {}
        assert marker.corporate_action_rejections == {}
        assert marker.affected_cohorts.keys() == {"cohort_0", "cohort_1"}
        assert len(set(marker.affected_cohorts.values())) == 2
        assert all(
            binding.startswith("ledger_recovery_binding_")
            for binding in marker.affected_cohorts.values()
        )
        assert str(tmp_path) not in json.dumps(
            marker.__dict__, sort_keys=True, default=str
        )

    def test_crash_after_minimal_blocker_closes_epoch_and_cannot_be_bypassed(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        first_session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
        gap_session = next_session(first_session)
        epoch_id = orchestrator._epoch_id
        store = orchestrator.cohorts[0]["executor"].metric_store
        source.get_total_return_closes = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("shared benchmark gap")
        )
        orchestrator._after_gap_blocker = lambda marker: (_ for _ in ()).throw(
            RuntimeError("crash after minimal blocker")
        )

        with pytest.raises(RuntimeError, match="crash after minimal blocker"):
            orchestrator.run_daily(gap_session.isoformat())

        pending = store.pending_critical_gap()
        assert pending is not None and pending.detail_status == "minimal"
        invalid = store.load_epoch(epoch_id)
        assert invalid.status == "invalid" and invalid.end_session == gap_session
        orchestrator._after_gap_blocker = lambda marker: None
        source.get_total_return_closes = (
            AuthoritativePriceSource().get_total_return_closes
        )
        calls_before = (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        )

        for replay_session in (gap_session, next_session(gap_session)):
            with pytest.raises(ValueError, match="critical gap recovery detail"):
                orchestrator.run_daily(replay_session.isoformat())

            assert store.pending_critical_gap() == pending
            assert (
                len(source.raw_calls),
                len(source.action_calls),
                len(source.benchmark_calls),
            ) == calls_before

    @pytest.mark.parametrize(
        "topology",
        ("removed", "renamed", "changed_state_path", "duplicate"),
    )
    def test_restart_requires_every_original_ledger_binding(self, tmp_path, topology):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, cohorts=2, strategy_modules=[FakeStrategy()]
        )
        first_session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
        gap_session = next_session(first_session)
        source.get_total_return_closes = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("shared benchmark gap")
        )
        orchestrator._after_gap_marker = lambda marker: (_ for _ in ()).throw(
            RuntimeError("restart boundary")
        )
        with pytest.raises(RuntimeError, match="restart boundary"):
            orchestrator.run_daily(gap_session.isoformat())
        store = orchestrator.cohorts[0]["executor"].metric_store
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()

        configs = [_cohort_config(tmp_path, "cohort_0")]
        if topology == "renamed":
            configs.append(_cohort_config(tmp_path, "renamed", "renamed_state"))
        elif topology == "changed_state_path":
            configs.append(_cohort_config(tmp_path, "cohort_1", "fresh_cohort_1"))
        elif topology == "duplicate":
            configs.append(_cohort_config(tmp_path, "cohort_1"))
        restarted, restarted_source = _authoritative_orchestrator(
            tmp_path,
            strategy_modules=[FakeStrategy()],
            cohort_configs=configs,
        )
        if topology == "duplicate":
            restarted.cohorts.append(restarted.cohorts[0])
        calls_before = (
            len(restarted_source.raw_calls),
            len(restarted_source.action_calls),
            len(restarted_source.benchmark_calls),
        )

        with pytest.raises(ValueError, match="critical gap cohort binding"):
            restarted.run_daily(gap_session.isoformat())

        assert store.pending_critical_gap() is not None
        assert (
            len(restarted_source.raw_calls),
            len(restarted_source.action_calls),
            len(restarted_source.benchmark_calls),
        ) == calls_before

    def test_restart_missing_corporate_audit_cohort_stays_pending(self, tmp_path):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, cohorts=2, strategy_modules=[FakeStrategy()]
        )
        first_session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
        gap_session = next_session(first_session)
        invalid_action = CorporateAction(
            "bad-scope-action",
            "MSFT",
            gap_session,
            "split",
            Decimal("2"),
            None,
            "fixture",
            datetime.now(timezone.utc),
            True,
        )
        source.get_corporate_actions = lambda tickers, session: (invalid_action,)
        orchestrator._after_gap_marker = lambda marker: (_ for _ in ()).throw(
            RuntimeError("audit restart boundary")
        )
        with pytest.raises(RuntimeError, match="audit restart boundary"):
            orchestrator.run_daily(gap_session.isoformat())
        store = orchestrator.cohorts[0]["executor"].metric_store
        marker = store.pending_critical_gap()
        assert marker is not None
        assert marker.corporate_action_rejections.keys() == {"cohort_0", "cohort_1"}
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()

        restarted, _ = _authoritative_orchestrator(
            tmp_path,
            strategy_modules=[FakeStrategy()],
            cohort_configs=[_cohort_config(tmp_path, "cohort_0")],
        )
        with pytest.raises(ValueError, match="critical gap cohort binding"):
            restarted.run_daily(gap_session.isoformat())

        assert store.pending_critical_gap() is not None
        with sqlite3.connect(tmp_path / "cohort_1" / "portfolio.db") as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM corporate_action_batch_rejections"
                ).fetchone()[0]
                == 0
            )

    def test_committed_interleaved_runner_still_persists_required_audit(self, tmp_path):
        marker_owner, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        first_session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            marker_owner.run_daily(first_session.isoformat())
        gap_session = next_session(first_session)
        epoch_id = marker_owner._epoch_id
        interleaved, clean_source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        interleaved_executor = interleaved.cohorts[0]["executor"]
        interleaved_epoch = interleaved_executor.ensure_metric_epoch(
            interleaved._metric_epoch_context, gap_session
        )
        assert interleaved_epoch.epoch_id == epoch_id
        required = interleaved_executor.required_tickers(gap_session, epoch_id)
        clean_bundle = SessionExecutor.fetch_input_bundle(
            gap_session, required, clean_source
        )

        invalid_action = CorporateAction(
            "bad-scope-action",
            "MSFT",
            gap_session,
            "split",
            Decimal("2"),
            None,
            "fixture",
            datetime.now(timezone.utc),
            True,
        )
        source.get_corporate_actions = lambda tickers, session: (invalid_action,)
        marker_owner._after_gap_marker = lambda marker: (_ for _ in ()).throw(
            RuntimeError("runner paused after ready marker")
        )
        with pytest.raises(RuntimeError, match="runner paused after ready marker"):
            marker_owner.run_daily(gap_session.isoformat())

        store = marker_owner.cohorts[0]["executor"].metric_store
        pending = store.pending_critical_gap()
        assert pending is not None and pending.detail_status == "ready"
        processed_at = datetime.now(timezone.utc)
        committed_result = interleaved_executor.execute_open_and_mark(
            gap_session,
            epoch_id,
            clean_bundle,
            {},
            processed_at,
        )
        assert committed_result.valid and committed_result.snapshot is not None
        committed_ledger = marker_owner.cohorts[0]["ledger"]
        snapshot_before = committed_ledger.read_snapshots(
            gap_session, gap_session, epoch_id=epoch_id, valid_only=True
        )[0]
        assert all(
            committed_ledger.phase_completed(gap_session, phase) for phase in PHASES
        )
        original_reject = committed_ledger.reject_corporate_action_batch
        committed_ledger.reject_corporate_action_batch = lambda *args, **kwargs: (
            _ for _ in ()
        ).throw(RuntimeError("committed audit write failed"))

        with pytest.raises(RuntimeError, match="committed audit write failed"):
            marker_owner.run_daily(gap_session.isoformat())

        assert store.pending_critical_gap() == pending
        assert store.load_epoch(epoch_id).status == "invalid"
        assert (
            committed_ledger.connection.execute(
                "SELECT COUNT(*) FROM corporate_action_batch_rejections"
            ).fetchone()[0]
            == 0
        )
        committed_ledger.reject_corporate_action_batch = original_reject
        marker_owner._after_gap_metric_invalidation = lambda marker: (
            _ for _ in ()
        ).throw(RuntimeError("crash after committed audit"))

        with pytest.raises(RuntimeError, match="crash after committed audit"):
            marker_owner.run_daily(gap_session.isoformat())

        assert store.pending_critical_gap() is not None
        assert (
            committed_ledger.connection.execute(
                "SELECT COUNT(*) FROM corporate_action_batch_rejections"
            ).fetchone()[0]
            == 1
        )
        marker_owner._after_gap_metric_invalidation = lambda marker: None
        recovered = marker_owner.run_daily(gap_session.isoformat())

        assert recovered["cohort_0"]["error"]
        assert store.pending_critical_gap() is None
        assert (
            committed_ledger.connection.execute(
                "SELECT COUNT(*) FROM corporate_action_batch_rejections"
            ).fetchone()[0]
            == 1
        )
        assert committed_ledger.read_snapshots(
            gap_session, gap_session, epoch_id=epoch_id, valid_only=True
        ) == [snapshot_before]
        assert all(
            committed_ledger.phase_completed(gap_session, phase) for phase in PHASES
        )
        assert committed_ledger.session_invalid_reason(gap_session) == ""
        assert store.load_epoch(epoch_id).status == "invalid"
        interleaved.cohorts[0]["ledger"].close()

    @pytest.mark.parametrize("detail_status", ("ready", "minimal"))
    def test_all_removed_cohorts_leave_pending_gap_and_fail_closed(
        self, tmp_path, detail_status
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        first_session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
        gap_session = next_session(first_session)
        epoch_id = orchestrator._epoch_id
        store = orchestrator.cohorts[0]["executor"].metric_store
        source.get_total_return_closes = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("shared benchmark gap")
        )
        hook_name = (
            "_after_gap_marker" if detail_status == "ready" else "_after_gap_blocker"
        )
        setattr(
            orchestrator,
            hook_name,
            lambda marker: (_ for _ in ()).throw(
                RuntimeError(f"crash with {detail_status} marker")
            ),
        )
        with pytest.raises(RuntimeError, match=f"crash with {detail_status} marker"):
            orchestrator.run_daily(gap_session.isoformat())
        pending = store.pending_critical_gap()
        assert pending is not None and pending.detail_status == detail_status
        orchestrator.cohorts[0]["ledger"].close()

        empty, empty_source = _authoritative_orchestrator(
            tmp_path, cohorts=0, strategy_modules=[FakeStrategy()]
        )
        calls_before = (
            len(empty_source.raw_calls),
            len(empty_source.action_calls),
            len(empty_source.benchmark_calls),
        )
        for replay_session in (gap_session, next_session(gap_session)):
            with pytest.raises(ValueError, match="critical gap cohort binding"):
                empty.run_daily(replay_session.isoformat())
            assert store.pending_critical_gap() == pending
            assert store.load_epoch(epoch_id).status == "invalid"
            assert (
                len(empty_source.raw_calls),
                len(empty_source.action_calls),
                len(empty_source.benchmark_calls),
            ) == calls_before

    def test_empty_orchestrator_without_pending_gap_returns_empty(self, tmp_path):
        empty, source = _authoritative_orchestrator(
            tmp_path, cohorts=0, strategy_modules=[FakeStrategy()]
        )

        assert empty.run_daily(date(2026, 3, 30).isoformat()) == {}
        assert source.raw_calls == []
        assert source.action_calls == []
        assert source.benchmark_calls == []

    def test_added_cohort_can_start_only_after_exact_original_recovery(self, tmp_path):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, cohorts=2, strategy_modules=[FakeStrategy()]
        )
        first_session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
        gap_session = next_session(first_session)
        old_epoch_id = orchestrator._epoch_id
        source.get_total_return_closes = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("shared benchmark gap")
        )
        orchestrator._after_gap_marker = lambda marker: (_ for _ in ()).throw(
            RuntimeError("restart boundary")
        )
        with pytest.raises(RuntimeError, match="restart boundary"):
            orchestrator.run_daily(gap_session.isoformat())
        store = orchestrator.cohorts[0]["executor"].metric_store
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()

        restarted, restarted_source = _authoritative_orchestrator(
            tmp_path,
            strategy_modules=[FakeStrategy()],
            cohort_configs=[
                _cohort_config(tmp_path, "cohort_0"),
                _cohort_config(tmp_path, "cohort_1"),
                _cohort_config(tmp_path, "cohort_2"),
            ],
        )
        later_session = next_session(gap_session)
        calls_before = (
            len(restarted_source.raw_calls),
            len(restarted_source.action_calls),
            len(restarted_source.benchmark_calls),
        )
        recovered = restarted.run_daily(later_session.isoformat())

        assert store.pending_critical_gap() is None
        assert store.load_epoch(old_epoch_id).status == "invalid"
        assert restarted._epoch_id != old_epoch_id
        assert set(recovered) == {"cohort_0", "cohort_1", "cohort_2"}
        assert all(not result["error"] for result in recovered.values())
        assert len(restarted_source.raw_calls) > calls_before[0]

    def test_completed_valid_cohort_is_preserved_when_fresh_peer_has_critical_gap(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, cohorts=2, strategy_modules=[FakeStrategy()]
        )
        sessions = [date(2026, 3, 30)]
        for _ in range(5):
            sessions.append(next_session(sessions[-1]))
        fresh_executor = orchestrator.cohorts[1]["executor"]
        original_execute = fresh_executor.execute_open_and_mark
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            for session in sessions[:-1]:
                orchestrator.run_daily(session.isoformat())
            fresh_executor.execute_open_and_mark = lambda *args, **kwargs: (
                _ for _ in ()
            ).throw(RuntimeError("fresh peer crash"))
            first_exit = orchestrator.run_daily(sessions[-1].isoformat())
            fresh_executor.execute_open_and_mark = original_execute

            completed_ledger = orchestrator.cohorts[0]["ledger"]
            completed_snapshot = completed_ledger.read_snapshots(
                sessions[-1], sessions[-1]
            )[0]
            store = orchestrator.cohorts[0]["executor"].metric_store
            valid_outcome = store.read_outcomes(orchestrator._epoch_id)[0]
            completed_executor = orchestrator.cohorts[0]["executor"]
            original_record_due = completed_executor.record_due_outcomes
            original_record_invalid = completed_executor.record_due_invalid_outcomes
            committed_due_calls = []
            committed_invalid_calls = []

            def recording_record_due(*args, **kwargs):
                committed_due_calls.append(kwargs.get("preserve_existing_valid", False))
                return original_record_due(*args, **kwargs)

            def recording_record_invalid(*args, **kwargs):
                committed_invalid_calls.append(kwargs.get("preserve_existing", False))
                return original_record_invalid(*args, **kwargs)

            completed_executor.record_due_outcomes = recording_record_due
            completed_executor.record_due_invalid_outcomes = recording_record_invalid
            original_get = source.get_daily_bars

            def missing_exit(tickers, start, end, adjusted=False):
                bars = original_get(tickers, start, end, adjusted=adjusted)
                bars.pop(("AAPL", start), None)
                return bars

            source.get_daily_bars = missing_exit
            orchestrator._screen_for_horizon = lambda *args, **kwargs: (
                _ for _ in ()
            ).throw(AssertionError("screening must stop after fresh peer gap"))
            replay = orchestrator.run_daily(sessions[-1].isoformat())

        epoch = store.current_epoch()
        assert not first_exit["cohort_0"]["error"]
        assert first_exit["cohort_1"]["error"]
        assert replay["cohort_0"]["replayed"]
        assert not replay["cohort_0"]["error"]
        assert replay["cohort_1"]["error"]
        assert completed_ledger.session_invalid_reason(sessions[-1]) == ""
        assert completed_ledger.read_snapshots(sessions[-1], sessions[-1]) == [
            completed_snapshot
        ]
        assert store.read_outcomes(orchestrator._epoch_id) == (valid_outcome,)
        assert valid_outcome.status == "valid"
        assert committed_due_calls == [False]
        assert committed_invalid_calls == [True]
        assert epoch is not None and epoch.status == "invalid"
        assert orchestrator.cohorts[1]["ledger"].session_invalid_reason(sessions[-1])

    def test_critical_outcome_conflict_invalidates_before_reraising(self, tmp_path):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        sessions = [date(2026, 3, 30)]
        for _ in range(5):
            sessions.append(next_session(sessions[-1]))
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            for session in sessions[:-1]:
                orchestrator.run_daily(session.isoformat())
            original_get = source.get_daily_bars

            def missing_exit(tickers, start, end, adjusted=False):
                bars = original_get(tickers, start, end, adjusted=adjusted)
                bars.pop(("AAPL", start), None)
                return bars

            source.get_daily_bars = missing_exit
            store = orchestrator.cohorts[0]["executor"].metric_store
            original_upsert = store.upsert_outcome
            store.upsert_outcome = lambda outcome: (_ for _ in ()).throw(
                ValueError("immutable outcome conflict sentinel")
            )
            with pytest.raises(ValueError, match="immutable outcome conflict sentinel"):
                orchestrator.run_daily(sessions[-1].isoformat())

        epoch = store.current_epoch()
        assert epoch is not None and epoch.status == "invalid"
        assert epoch.end_session == sessions[-1]
        assert store.pending_critical_gap() is not None
        calls_before = len(source.raw_calls)
        store.upsert_outcome = original_upsert
        replay = orchestrator.run_daily(sessions[-1].isoformat())
        assert replay["cohort_0"]["error"]
        assert store.pending_critical_gap() is None
        assert len(source.raw_calls) == calls_before

    def test_untraded_signal_outcomes_reuse_one_shared_raw_bundle_and_restart_idempotently(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(tmp_path, cohorts=2)
        sessions = [date(2026, 3, 30)]
        for _ in range(5):
            sessions.append(next_session(sessions[-1]))

        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            for session in sessions[:-1]:
                orchestrator.run_daily(session.isoformat())
            raw_before_exit = len(source.raw_calls)
            first = orchestrator.run_daily(sessions[-1].isoformat())
            raw_after_exit = len(source.raw_calls)
            replay = orchestrator.run_daily(sessions[-1].isoformat())

        cohort = orchestrator.cohorts[0]
        outcomes = cohort["executor"].metric_store.read_outcomes(orchestrator._epoch_id)
        assert not first["cohort_0"]["error"]
        assert not first["cohort_1"]["error"]
        assert source.raw_calls[-1] == (("AAPL", "MSFT"), sessions[-1])
        assert raw_after_exit == raw_before_exit + 2
        assert {(row.ticker, row.status) for row in outcomes} == {
            ("AAPL", "valid"),
            ("MSFT", "valid"),
        }
        assert replay["cohort_0"]["replayed"]
        assert replay["cohort_1"]["replayed"]
        assert len(source.raw_calls) == raw_after_exit
        assert (
            len(cohort["executor"].metric_store.read_outcomes(orchestrator._epoch_id))
            == 2
        )

    def test_cohorts_share_generation_metric_store_not_cohort_metric_databases(
        self, tmp_path
    ):
        orchestrator, _ = _authoritative_orchestrator(tmp_path, cohorts=2)

        paths = {
            cohort["executor"].metric_store.path for cohort in orchestrator.cohorts
        }
        assert paths == {tmp_path / "base" / "metrics_v2.sqlite3"}
        assert all(
            not (Path(cohort["config"].state_dir) / "metrics_v2.sqlite3").exists()
            for cohort in orchestrator.cohorts
        )

    def test_shared_outcome_does_not_remove_fresh_cohort_exit_ticker(self, tmp_path):
        orchestrator, source = _authoritative_orchestrator(tmp_path, cohorts=2)
        sessions = [date(2026, 3, 30)]
        for _ in range(5):
            sessions.append(next_session(sessions[-1]))
        fresh_executor = orchestrator.cohorts[1]["executor"]
        original_execute = fresh_executor.execute_open_and_mark

        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(sessions[0].isoformat())
            cutoff = session_close(sessions[0])
            shared_untraded = SignalRecord(
                signal_id="shared-untraded-signal",
                epoch_id=orchestrator._epoch_id,
                policy_id="foundation-30d",
                event_key="shared-untraded-event",
                strategy="filing_analysis",
                ticker="ZZZZ",
                direction="long",
                event_at=cutoff,
                observed_at=cutoff,
                reference_session=sessions[0],
                reference_close=Decimal("100"),
                decision_at=cutoff,
                evidence_hash="shared-untraded-evidence",
            )
            for cohort in orchestrator.cohorts:
                cohort["ledger"].record_signal(shared_untraded)
            for session in sessions[1:-1]:
                orchestrator.run_daily(session.isoformat())

            fresh_executor.execute_open_and_mark = lambda *args, **kwargs: (
                _ for _ in ()
            ).throw(RuntimeError("fresh cohort execution crash"))
            first_exit = orchestrator.run_daily(sessions[-1].isoformat())
            fresh_executor.execute_open_and_mark = original_execute
            shared_due = next(
                (signal, window)
                for signal, window in fresh_executor.due_outcome_signals(
                    sessions[-1], orchestrator._epoch_id
                )
                if signal.ticker == "ZZZZ"
            )
            fresh_executor.metric_store.load_outcome(
                fresh_executor.outcome_calculator.outcome_id(*shared_due)
            )
            fresh_required = fresh_executor.required_tickers(
                sessions[-1], orchestrator._epoch_id
            )
            before_replay = len(source.raw_calls)
            replay = orchestrator.run_daily(sessions[-1].isoformat())

        assert not first_exit["cohort_0"]["error"]
        assert first_exit["cohort_1"]["error"]
        assert fresh_required == ("AAPL", "MSFT", "ZZZZ")
        assert not replay["cohort_1"]["error"]
        assert source.raw_calls[before_replay] == (
            ("AAPL", "MSFT", "ZZZZ"),
            sessions[-1],
        )

    def test_completed_replay_repairs_missing_outcome_from_persisted_bars(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(tmp_path)
        sessions = [date(2026, 3, 30)]
        for _ in range(5):
            sessions.append(next_session(sessions[-1]))

        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            for session in sessions:
                orchestrator.run_daily(session.isoformat())
            store = orchestrator.cohorts[0]["executor"].metric_store
            with sqlite3.connect(store.path) as connection:
                connection.execute("DELETE FROM outcomes")
            raw_before_repair = len(source.raw_calls)
            replay = orchestrator.run_daily(sessions[-1].isoformat())

        assert replay["cohort_0"]["replayed"]
        assert len(source.raw_calls) == raw_before_repair
        assert len(store.read_outcomes(orchestrator._epoch_id)) == 2

    def test_completed_replay_fails_closed_for_conflicting_outcome_payload(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(tmp_path)
        sessions = [date(2026, 3, 30)]
        for _ in range(5):
            sessions.append(next_session(sessions[-1]))

        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            for session in sessions:
                orchestrator.run_daily(session.isoformat())
            store = orchestrator.cohorts[0]["executor"].metric_store
            with sqlite3.connect(store.path) as connection:
                payload = connection.execute(
                    "SELECT payload_json FROM outcomes ORDER BY outcome_id LIMIT 1"
                ).fetchone()[0]
                connection.execute(
                    "UPDATE outcomes SET payload_json = ? WHERE outcome_id = "
                    "(SELECT outcome_id FROM outcomes ORDER BY outcome_id LIMIT 1)",
                    (payload.replace('"status":"valid"', '"status":"invalid"'),),
                )
            raw_before_replay = len(source.raw_calls)
            with pytest.raises(ValueError, match="immutable outcome_id"):
                orchestrator.run_daily(sessions[-1].isoformat())

        epoch = store.current_epoch()
        assert epoch is not None and epoch.status == "invalid"
        assert epoch.end_session == sessions[-1]
        assert store.pending_critical_gap() is None
        assert len(source.raw_calls) == raw_before_replay

    def test_completed_replay_corrupt_current_bundle_closes_epoch_and_preserves_p0(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        sessions = [date(2026, 3, 30)]
        for _ in range(5):
            sessions.append(next_session(sessions[-1]))
        cohort = orchestrator.cohorts[0]
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            for session in sessions:
                orchestrator.run_daily(session.isoformat())
        store = cohort["executor"].metric_store
        epoch_id = orchestrator._epoch_id
        snapshot = cohort["ledger"].read_snapshots(sessions[-1], sessions[-1])[0]
        with sqlite3.connect(store.path) as connection:
            connection.execute("DELETE FROM outcomes")
        _corrupt_persisted_market_bundle(cohort["ledger"], sessions[-1])
        calls_before = len(source.raw_calls)

        with pytest.raises(KeyError, match="open"):
            orchestrator.run_daily(sessions[-1].isoformat())

        epoch = store.load_epoch(epoch_id)
        assert epoch.status == "invalid" and epoch.end_session == sessions[-1]
        assert store.pending_critical_gap() is None
        assert cohort["ledger"].session_invalid_reason(sessions[-1]) == ""
        assert cohort["ledger"].read_snapshots(sessions[-1], sessions[-1]) == [snapshot]
        assert len(source.raw_calls) == calls_before
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            clean = orchestrator.run_daily(next_session(sessions[-1]).isoformat())
        assert not clean["cohort_0"]["error"]
        assert store.current_epoch().epoch_id != epoch_id

    def test_completed_replay_corrupt_binding_digest_closes_epoch_and_preserves_p0(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        session = date(2026, 3, 30)
        cohort = orchestrator.cohorts[0]
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(session.isoformat())
        store = cohort["executor"].metric_store
        epoch_id = orchestrator._epoch_id
        snapshot = cohort["ledger"].read_snapshots(session, session)[0]
        cohort["ledger"].connection.execute(
            """
            UPDATE session_execution_contexts
            SET config_digest = 'corrupt-config-digest'
            WHERE cohort_id = ? AND session = ?
            """,
            (cohort["ledger"].cohort_id, session.isoformat()),
        )
        cohort["ledger"].connection.commit()
        calls_before = (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        )

        with pytest.raises(ValueError, match="effective config or borrow inputs"):
            orchestrator.run_daily(session.isoformat())

        epoch = store.load_epoch(epoch_id)
        assert epoch.status == "invalid" and epoch.end_session == session
        assert store.pending_critical_gap() is None
        assert cohort["ledger"].session_invalid_reason(session) == ""
        assert cohort["ledger"].read_snapshots(session, session) == [snapshot]
        assert (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        ) == calls_before

    def test_stage_only_repair_corrupt_entry_bundle_closes_epoch_and_preserves_p0(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        sessions = [date(2026, 3, 30)]
        for _ in range(5):
            sessions.append(next_session(sessions[-1]))
        cohort = orchestrator.cohorts[0]
        original_stage = cohort["engine"].screen_and_stage
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            for session in sessions[:-1]:
                orchestrator.run_daily(session.isoformat())
            cohort["engine"].screen_and_stage = lambda *args, **kwargs: (
                _ for _ in ()
            ).throw(RuntimeError("stage-only fixture crash"))
            first = orchestrator.run_daily(sessions[-1].isoformat())
        assert first["cohort_0"]["error"]
        cohort["engine"].screen_and_stage = original_stage
        store = cohort["executor"].metric_store
        epoch_id = orchestrator._epoch_id
        snapshot = cohort["ledger"].read_snapshots(sessions[-1], sessions[-1])[0]
        with sqlite3.connect(store.path) as connection:
            connection.execute("DELETE FROM outcomes")
        entry_context = cohort["ledger"].session_execution_context(sessions[1])
        assert entry_context is not None
        _corrupt_persisted_market_bundle(cohort["ledger"], sessions[1])
        calls_before = (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        )

        with pytest.raises(KeyError, match="open"):
            orchestrator.run_daily(sessions[-1].isoformat())

        epoch = store.load_epoch(epoch_id)
        assert epoch.status == "invalid" and epoch.end_session == sessions[-1]
        marker = store.pending_critical_gap()
        assert marker is not None and marker.detail_status == "ready"
        assert store.read_outcomes(epoch_id) == ()
        assert cohort["ledger"].session_invalid_reason(sessions[-1]) == ""
        assert cohort["ledger"].read_snapshots(sessions[-1], sessions[-1]) == [snapshot]
        for replay_session in (
            sessions[-1],
            next_session(sessions[-1]),
        ):
            with pytest.raises(KeyError, match="open"):
                orchestrator.run_daily(replay_session.isoformat())
            assert store.pending_critical_gap() == marker
            assert store.read_outcomes(epoch_id) == ()
            assert (
                len(source.raw_calls),
                len(source.action_calls),
                len(source.benchmark_calls),
            ) == calls_before

        cohort["ledger"].connection.execute(
            """
            UPDATE session_execution_contexts
            SET economic_inputs_json = ?, input_digest = ?, market_digest = ?
            WHERE cohort_id = ? AND session = ?
            """,
            (
                entry_context["economic_inputs_json"],
                entry_context["input_digest"],
                entry_context["market_digest"],
                cohort["ledger"].cohort_id,
                sessions[1].isoformat(),
            ),
        )
        cohort["ledger"].connection.commit()
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            clean = orchestrator.run_daily(next_session(sessions[-1]).isoformat())
        assert not clean["cohort_0"]["error"]
        assert store.pending_critical_gap() is None
        assert store.current_epoch().epoch_id != epoch_id

    def test_partial_resume_outcome_conflict_closes_epoch_and_preserves_commit(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, strategy_modules=[FakeStrategy()]
        )
        sessions = [date(2026, 3, 30)]
        for _ in range(5):
            sessions.append(next_session(sessions[-1]))
        cohort = orchestrator.cohorts[0]
        executor = cohort["executor"]
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            for session in sessions[:-1]:
                orchestrator.run_daily(session.isoformat())

        def crash_after_validation(phase):
            if phase == "validate_market_data":
                raise RuntimeError("partial outcome fixture crash")

        executor._after_phase_commit = crash_after_validation
        first = orchestrator.run_daily(sessions[-1].isoformat())
        assert first["cohort_0"]["error"]
        executor._after_phase_commit = lambda phase: None
        epoch_id = orchestrator._epoch_id
        due_signal, window = executor.due_outcome_signals(sessions[-1], epoch_id)[0]
        bars = dict(executor.persisted_input_bundle(sessions[-1]).bars)
        entry_session = executor.outcome_calculator.calendar.next_session(
            due_signal.reference_session
        )
        bars.update(executor.persisted_input_bundle(entry_session).bars)
        valid_outcome = executor.outcome_calculator.build(due_signal, window, bars)
        executor.metric_store.upsert_outcome(
            replace(valid_outcome, status="invalid", invalid_reason="corrupt fixture")
        )
        calls_before = len(source.raw_calls)

        with pytest.raises(ValueError, match="immutable outcome_id"):
            orchestrator.run_daily(sessions[-1].isoformat())

        epoch = executor.metric_store.load_epoch(epoch_id)
        assert epoch.status == "invalid" and epoch.end_session == sessions[-1]
        assert executor.metric_store.pending_critical_gap() is None
        snapshots = cohort["ledger"].read_snapshots(sessions[-1], sessions[-1])
        assert len(snapshots) == 1 and snapshots[0].valid
        assert cohort["ledger"].session_invalid_reason(sessions[-1]) == ""
        assert len(source.raw_calls) == calls_before
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            clean = orchestrator.run_daily(next_session(sessions[-1]).isoformat())
        assert not clean["cohort_0"]["error"]
        assert executor.metric_store.current_epoch().epoch_id != epoch_id

    def test_double_run_no_duplicates(self, tmp_path):
        orchestrator, source = _authoritative_orchestrator(tmp_path)
        session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ) as committee:
            first = orchestrator.run_daily(session.isoformat())
            before_replay_calls = (
                len(source.raw_calls),
                len(source.action_calls),
                len(source.benchmark_calls),
                committee.call_count,
            )
            second = orchestrator.run_daily(session.isoformat())

        ledger = orchestrator.cohorts[0]["ledger"]
        assert first["cohort_0"]["intents_staged"]
        assert second["cohort_0"]["replayed"]
        assert len(ledger.read_signals(session, session)) == 2
        assert len(ledger.pending_intents(next_session(session))) == 1
        assert ledger.read_fills() == []
        assert (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
            committee.call_count,
        ) == before_replay_calls

    def test_mixed_complete_and_stage_only_replay_skips_execution_inputs(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(tmp_path, cohorts=2)
        session = date(2026, 3, 30)
        second_engine = orchestrator.cohorts[1]["engine"]
        original_stage = second_engine.screen_and_stage
        fail_once = True

        def stage_with_one_crash(*args, **kwargs):
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise RuntimeError("staging crash")
            return original_stage(*args, **kwargs)

        second_engine.screen_and_stage = stage_with_one_crash
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            first = orchestrator.run_daily(session.isoformat())
            before = (
                len(source.raw_calls),
                len(source.action_calls),
                len(source.benchmark_calls),
            )
            replay = orchestrator.run_daily(session.isoformat())

        assert not first["cohort_0"]["error"]
        assert first["cohort_1"]["error"]
        assert replay["cohort_0"]["replayed"]
        assert not replay["cohort_1"]["error"]
        assert len(source.raw_calls) == before[0] + 1
        assert len(source.action_calls) == before[1]
        assert len(source.benchmark_calls) == before[2]

    def test_partial_execution_resume_uses_stored_bundle_not_provider_refetch(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(tmp_path)
        session = date(2026, 3, 30)
        executor = orchestrator.cohorts[0]["executor"]

        def crash_after_commit(phase):
            if phase == "validate_market_data":
                raise RuntimeError("execution crash")

        executor._after_phase_commit = crash_after_commit
        first = orchestrator.run_daily(session.isoformat())
        assert first["cohort_0"]["error"]
        before = (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        )

        executor._after_phase_commit = lambda phase: None
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            resumed = orchestrator.run_daily(session.isoformat())

        assert not resumed["cohort_0"]["error"]
        assert len(source.raw_calls) == before[0] + 1
        assert len(source.action_calls) == before[1]
        assert len(source.benchmark_calls) == before[2]

    def test_complete_and_stage_only_replay_validate_local_policy_without_market_io(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(tmp_path, cohorts=2)
        session = date(2026, 3, 30)
        second_engine = orchestrator.cohorts[1]["engine"]
        original_stage = second_engine.screen_and_stage

        def fail_stage(*args, **kwargs):
            raise RuntimeError("staging crash")

        second_engine.screen_and_stage = fail_stage
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            first = orchestrator.run_daily(session.isoformat())
        assert not first["cohort_0"]["error"]
        assert first["cohort_1"]["error"]
        second_engine.screen_and_stage = original_stage
        before = (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        )

        for cohort in orchestrator.cohorts:
            metric_store = cohort["executor"].metric_store
            drifted = {
                **cohort["executor"].config,
                "autoresearch": {
                    **cohort["executor"].config["autoresearch"],
                    "paper_ledger": {
                        **cohort["executor"]
                        .config["autoresearch"]
                        .get("paper_ledger", {}),
                        "slippage_bps": "20",
                    },
                },
            }
            cohort["executor"] = SessionExecutor(
                cohort["ledger"], drifted, metric_store=metric_store
            )

        with pytest.raises(ValueError, match="effective config"):
            orchestrator.run_daily(session.isoformat())
        epoch = metric_store.load_epoch(orchestrator._epoch_id)
        assert epoch.status == "invalid" and epoch.end_session == session
        assert metric_store.pending_critical_gap() is None
        assert all(
            cohort["ledger"].session_invalid_reason(session) == ""
            for cohort in orchestrator.cohorts
        )
        assert (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        ) == before

    def test_partial_orchestrator_resume_rehydrates_nonempty_borrow_document(
        self, tmp_path
    ):
        orchestrator, source = _authoritative_orchestrator(tmp_path)
        cohort = orchestrator.cohorts[0]
        ledger = cohort["ledger"]
        executor = cohort["executor"]
        session = date(2026, 3, 30)
        prior = previous_session(session)
        cutoff = session_close(prior)
        metric_epoch = executor.ensure_metric_epoch(
            orchestrator._metric_epoch_context, prior
        )
        orchestrator._epoch_id = metric_epoch.epoch_id
        signal = SignalRecord(
            stable_id("borrow_resume_signal", "AAPL"),
            orchestrator._epoch_id,
            "foundation-30d",
            "borrow-resume-event",
            "fake_strat",
            "AAPL",
            "long",
            cutoff,
            cutoff,
            prior,
            Decimal("100"),
            cutoff,
            stable_id("borrow_resume_evidence", "AAPL"),
        )
        ledger.record_signal(signal)
        intent = OrderIntent(
            stable_id("borrow_resume_intent", "AAPL"),
            (signal.signal_id,),
            ledger.cohort_id,
            "buy",
            1,
            cutoff,
            session,
            "next_session_open",
            "pending",
            None,
            None,
        )
        ledger.stage_intent(intent)

        def crash_after_commit(phase):
            if phase == "validate_market_data":
                raise RuntimeError("execution crash")

        executor._after_phase_commit = crash_after_commit
        bundle = SessionExecutor.fetch_input_bundle(
            session, ("AAPL",), source, executor.benchmark_symbols
        )
        with pytest.raises(RuntimeError, match="execution crash"):
            executor.execute_open_and_mark(
                session,
                orchestrator._epoch_id,
                bundle,
                {"AAPL": Decimal("0.0125")},
                datetime.now(timezone.utc),
            )
        assert executor.persisted_borrow_rates(session) == {"AAPL": Decimal("0.0125")}
        before = (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
        )

        executor._after_phase_commit = lambda phase: None
        with (
            patch(
                "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
                side_effect=_authoritative_committee,
            ),
            patch(
                "tradingagents.strategies.orchestration.session_executor.ensure_reference_bars",
                return_value={
                    "AAPL": bundle.bars[("AAPL", session)],
                    "MSFT": MarketBar(
                        **{
                            **bundle.bars[("AAPL", session)].__dict__,
                            "ticker": "MSFT",
                        }
                    ),
                },
            ),
        ):
            resumed = orchestrator.run_daily(session.isoformat())

        assert not resumed["cohort_0"]["error"]
        assert ledger.intent(intent.intent_id).status == "filled"
        assert len(source.raw_calls) == before[0]
        assert len(source.action_calls) == before[1]
        assert len(source.benchmark_calls) == before[2]


class TestThirtyDayFullLifecycle:
    def test_30_exact_sessions_preserve_delays_exits_and_accounting(self, tmp_path):
        orchestrator, source = _authoritative_orchestrator(
            tmp_path, cohorts=2, hold_days=2
        )
        sessions = [date(2026, 3, 30)]
        for _ in range(29):
            sessions.append(next_session(sessions[-1]))

        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ) as committee:
            results = [
                orchestrator.run_daily(session.isoformat()) for session in sessions
            ]
            before_replay = [
                (
                    len(cohort["ledger"].read_signals()),
                    cohort["ledger"]
                    .connection.execute("SELECT COUNT(*) FROM order_intents")
                    .fetchone()[0],
                    len(cohort["ledger"].read_fills()),
                )
                for cohort in orchestrator.cohorts
            ]
            before_replay_calls = (
                len(source.raw_calls),
                len(source.action_calls),
                len(source.benchmark_calls),
                committee.call_count,
            )
            replay = orchestrator.run_daily(sessions[-1].isoformat())

        assert results[0]["cohort_0"]["trades_opened"] == []
        for index, cohort in enumerate(orchestrator.cohorts):
            ledger = cohort["ledger"]
            fills = ledger.read_fills()
            snapshots = ledger.read_snapshots(valid_only=True)
            after_replay = (
                len(ledger.read_signals()),
                ledger.connection.execute(
                    "SELECT COUNT(*) FROM order_intents"
                ).fetchone()[0],
                len(fills),
            )
            assert fills[0].session == sessions[1]
            assert (fills[0].effective_at.hour, fills[0].effective_at.minute) == (
                13,
                30,
            )
            assert sum(fill.side == "buy" for fill in fills) >= 3
            assert sum(fill.side == "sell" for fill in fills) >= 2
            assert (
                ledger.connection.execute(
                    "SELECT COUNT(*) FROM order_intents WHERE price_rule = 'resting_stop'"
                ).fetchone()[0]
                >= 1
            )
            assert len(snapshots) == 30
            assert all(snapshot.valid for snapshot in snapshots)
            assert all(
                snapshot.net_equity
                == snapshot.cash + snapshot.long_market_value - snapshot.short_liability
                for snapshot in snapshots
            )
            assert after_replay == before_replay[index]
            assert replay[f"cohort_{index}"]["replayed"]

        assert len(source.benchmark_calls) == 30
        assert len(source.raw_calls) <= 2 * 30
        assert committee.call_count <= 2 * 30
        assert (
            len(source.raw_calls),
            len(source.action_calls),
            len(source.benchmark_calls),
            committee.call_count,
        ) == before_replay_calls


class TestThirtyDayCohortDivergence:
    def test_cohorts_share_inputs_and_learning_stays_disabled(self, tmp_path):
        orchestrator, source = _authoritative_orchestrator(tmp_path, cohorts=2)
        session = date(2026, 3, 30)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            result = orchestrator.run_daily(session.isoformat())

        assert not result["cohort_0"]["error"]
        assert not result["cohort_1"]["error"]
        assert len(source.benchmark_calls) == 1
        assert len(source.raw_calls) == 1
        assert all(
            cohort["engine"]._adaptive_confidence is False
            and cohort["config"].learning_policy.mode == "disabled"
            for cohort in orchestrator.cohorts
        )


class TestOpenBBEnrichment:
    def test_enrichment_is_shared_and_passed_after_mark(self, tmp_path):
        orchestrator, _ = _authoritative_orchestrator(tmp_path, cohorts=2)
        session = date(2026, 3, 30)
        enrichment = {"profiles": {"AAPL": {"sector": "Technology"}}}
        observed: list[dict] = []

        def committee(signals, **kwargs):
            observed.append(kwargs["enrichment"])
            return _authoritative_committee(signals)

        with (
            patch.object(
                orchestrator, "_fetch_openbb_enrichment", return_value=enrichment
            ),
            patch(
                "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
                side_effect=committee,
            ),
        ):
            orchestrator.run_daily(session.isoformat())

        assert observed == [enrichment, enrichment]
        assert all(
            cohort["ledger"].read_snapshots(session, session)[0].valid
            for cohort in orchestrator.cohorts
        )

    def test_graceful_degradation_without_openbb(self, tmp_path):
        orchestrator, _ = _authoritative_orchestrator(tmp_path)
        session = date(2026, 3, 30)
        observed: list[dict] = []

        def committee(signals, **kwargs):
            observed.append(kwargs["enrichment"])
            return _authoritative_committee(signals)

        with (
            patch.object(orchestrator, "_fetch_openbb_enrichment", return_value={}),
            patch(
                "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
                side_effect=committee,
            ),
        ):
            result = orchestrator.run_daily(session.isoformat())

        assert observed == [{}]
        assert not result["cohort_0"]["error"]
        assert result["cohort_0"]["intents_staged"]


class TestReactivatedStrategies:
    """Test reactivated govt_contracts and state_economics produce valid signals."""

    def test_govt_contracts_with_usaspending_data(self):
        """govt_contracts screen() produces candidates from contract data."""
        from tradingagents.strategies.modules.govt_contracts import (
            GovtContractsStrategy,
        )

        strategy = GovtContractsStrategy()
        assert strategy.track == "paper_trade"
        assert "openbb" in strategy.data_sources

        # Provide synthetic USASpending contract data
        data = {
            "usaspending": {
                "data": {
                    "contracts": [
                        {
                            "recipient": "Lockheed Martin Corp",
                            "amount": 500_000_000,
                            "award_id": "AWARD-LMT",
                        },
                        {
                            "recipient": "Northrop Grumman Systems",
                            "amount": 200_000_000,
                            "award_id": "AWARD-NOC",
                        },
                        {
                            "recipient": "Small Unknown Contractor",
                            "amount": 10_000_000,
                            "award_id": "AWARD-SMALL",
                        },  # Below threshold
                    ]
                }
            },
            "yfinance": {"prices": {}},
        }

        candidates = strategy.screen(data, "2026-03-15", strategy.get_default_params())
        # Should find LMT and NOC (Lockheed and Northrop), but not small contractor
        assert len(candidates) >= 1
        tickers = [c.ticker for c in candidates]
        assert "LMT" in tickers  # Lockheed
        for c in candidates:
            assert c.direction == "long"
            assert c.score > 0

    def test_govt_contracts_momentum_fallback(self):
        """govt_contracts falls back to momentum when no contract data."""
        from tradingagents.strategies.modules.govt_contracts import (
            GovtContractsStrategy,
        )

        strategy = GovtContractsStrategy()

        # Build price data with upward momentum for some contractors
        dates = pd.date_range("2026-02-01", periods=40, freq="B")
        prices = {}
        # LMT with strong upward momentum
        lmt_close = [400 + i * 2 for i in range(40)]  # rising
        prices["LMT"] = pd.DataFrame(
            {"Close": lmt_close, "Volume": [1e6] * 40}, index=dates
        )
        # BA with flat/declining
        ba_close = [200 - i * 0.1 for i in range(40)]
        prices["BA"] = pd.DataFrame(
            {"Close": ba_close, "Volume": [1e6] * 40}, index=dates
        )

        data = {
            "usaspending": {},  # No contract data
            "yfinance": {"prices": prices},
        }

        candidates = strategy.screen(data, "2026-03-25", strategy.get_default_params())
        # LMT should appear (positive momentum), BA should not (negative)
        if candidates:
            tickers = [c.ticker for c in candidates]
            assert "LMT" in tickers
            for c in candidates:
                assert c.metadata.get("source") == "momentum_fallback"

    def test_govt_contracts_exit_logic(self):
        """govt_contracts exit logic works correctly."""
        from tradingagents.strategies.modules.govt_contracts import (
            GovtContractsStrategy,
        )

        strategy = GovtContractsStrategy()
        params = strategy.get_default_params()

        # Test profit target
        should_exit, reason = strategy.check_exit("LMT", 100.0, 120.0, 5, params, {})
        assert should_exit is True
        assert reason == "profit_target"

        # Test stop loss
        should_exit, reason = strategy.check_exit("LMT", 100.0, 90.0, 5, params, {})
        assert should_exit is True
        assert reason == "stop_loss"

        # Test hold period
        should_exit, reason = strategy.check_exit("LMT", 100.0, 102.0, 35, params, {})
        assert should_exit is True
        assert reason == "hold_period"

        # Test no exit yet
        should_exit, reason = strategy.check_exit("LMT", 100.0, 105.0, 5, params, {})
        assert should_exit is False

    def test_state_economics_with_fred_data(self):
        """state_economics screen() combines FRED indicators with momentum."""
        from tradingagents.strategies.modules.state_economics import (
            StateEconomicsStrategy,
        )

        strategy = StateEconomicsStrategy()
        assert strategy.track == "paper_trade"
        assert "fred" in strategy.data_sources
        assert "openbb" in strategy.data_sources

        # Build price data for regional ETFs
        dates = pd.date_range("2026-02-01", periods=30, freq="B")
        prices = {}
        # KRE (regional banks) with positive momentum
        kre_close = [50 + i * 0.5 for i in range(30)]
        prices["KRE"] = pd.DataFrame(
            {"Close": kre_close, "Volume": [1e6] * 30}, index=dates
        )
        # IWN with slight positive
        iwn_close = [160 + i * 0.2 for i in range(30)]
        prices["IWN"] = pd.DataFrame(
            {"Close": iwn_close, "Volume": [1e6] * 30}, index=dates
        )

        data = {
            "yfinance": {"prices": prices},
            "fred": {
                "UNRATE": {"2026-01": 4.2, "2026-02": 4.0},  # Declining = bullish
                "ICSA": {
                    "2026-02-15": 220000,
                    "2026-02-22": 210000,
                },  # Declining = bullish
            },
        }

        candidates = strategy.screen(data, "2026-03-15", strategy.get_default_params())
        assert len(candidates) > 0
        # KRE should get econ_boost from declining unemployment
        kre_candidates = [c for c in candidates if c.ticker == "KRE"]
        if kre_candidates:
            assert kre_candidates[0].metadata.get("econ_boost", 0) > 0

    def test_state_economics_momentum_only_fallback(self):
        """state_economics falls back to pure momentum when no FRED data."""
        from tradingagents.strategies.modules.state_economics import (
            StateEconomicsStrategy,
        )

        strategy = StateEconomicsStrategy()

        dates = pd.date_range("2026-02-01", periods=30, freq="B")
        prices = {}
        kre_close = [50 + i * 0.5 for i in range(30)]
        prices["KRE"] = pd.DataFrame(
            {"Close": kre_close, "Volume": [1e6] * 30}, index=dates
        )

        data = {
            "yfinance": {"prices": prices},
            "fred": {},  # No FRED data
        }

        candidates = strategy.screen(data, "2026-03-15", strategy.get_default_params())
        assert len(candidates) > 0
        for c in candidates:
            assert c.metadata.get("econ_boost", 0) == 0.0  # No boost without FRED

    def test_state_economics_exit_logic(self):
        """state_economics exit logic: rebalance schedule (30-day default)."""
        from tradingagents.strategies.modules.state_economics import (
            StateEconomicsStrategy,
        )

        strategy = StateEconomicsStrategy()
        params = strategy.get_default_params()

        # Before rebalance (default rebalance_days=30)
        should_exit, reason = strategy.check_exit("KRE", 50.0, 55.0, 10, params, {})
        assert should_exit is False

        # At rebalance boundary
        should_exit, reason = strategy.check_exit("KRE", 50.0, 55.0, 30, params, {})
        assert should_exit is True
        assert reason == "rebalance"

    def test_twelve_strategies_registered(self):
        """Verify 12 strategies are registered (including quantum_readiness)."""
        from tradingagents.strategies.modules import get_paper_trade_strategies

        strategies = get_paper_trade_strategies()
        assert len(strategies) == 12
        names = [s.name for s in strategies]
        assert "govt_contracts" in names
        assert "state_economics" in names
        assert "commodity_macro" in names
        assert "weather_ag" in names
        assert "quantum_readiness" in names
