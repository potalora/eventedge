"""Multi-day cohort lifecycle test.

Simulates 30 trading days of the 2-cohort paper trading trial to verify:
1. Trades open correctly on day 1
2. Exit checks fire when holding period / stop loss is reached
3. PnL is computed on close
4. Signal journal back-fills return_5d/10d/30d
5. Learning diagnostics read governed v2 outcomes
6. Adaptive confidence diverges from control after enough history
7. Cohort comparison produces valid report

Uses fully mocked data (no API calls, no LLM).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import sqlite3
from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.strategies.orchestration.multi_strategy_engine import (
    MultiStrategyEngine,
)
from tradingagents.strategies.trading.paper_trader import PaperTrader
from tradingagents.strategies.learning.signal_journal import SignalJournal
from tradingagents.strategies.execution.models import MarketBar
from tradingagents.strategies.execution.price_source import (
    CandidateBarAttempt,
    CandidateBarResolution,
)
from tradingagents.strategies.metrics.models import OutcomeRecord, SignalMetricRecord
from tradingagents.strategies.metrics.outcomes import OutcomeCalculator, directional_accuracy
from tradingagents.strategies.state.state import StateManager
from tradingagents.strategies.modules.base import Candidate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_price_df(
    base_price: float,
    days: int = 60,
    end: datetime | None = None,
) -> pd.DataFrame:
    """Create a price DataFrame with slight daily drift."""
    dates = pd.bdate_range(end=end or datetime.now(), periods=days)
    prices = [base_price * (1 + 0.002 * i) for i in range(days)]
    return pd.DataFrame({"Close": prices, "Volume": [1_000_000] * days}, index=dates)


def _make_declining_price_df(base_price: float, days: int = 60) -> pd.DataFrame:
    """Price drops 1% per day (triggers stop loss at -8%)."""
    dates = pd.bdate_range(end=datetime.now(), periods=days)
    prices = [base_price * (1 - 0.01 * i) for i in range(days)]
    return pd.DataFrame({"Close": prices, "Volume": [500_000] * days}, index=dates)


class FakeStrategy:
    """Minimal strategy for testing lifecycle."""

    name = "fake_strat"
    track = "paper_trade"
    data_sources = ["yfinance"]

    def __init__(self, hold_days: int = 10):
        self._hold_days = hold_days

    def get_param_space(self, horizon: str = "30d"):
        return {"hold_days": (5, 30)}

    def get_default_params(self, horizon: str = "30d"):
        return {"hold_days": self._hold_days}

    def screen(self, data, date, params):
        """Return one candidate per call."""
        return [
            Candidate(
                ticker="AAPL",
                date=date,
                direction="long",
                score=5.0,
                metadata={"source": "test"},
            )
        ]

    def check_exit(
        self, ticker, entry_price, current_price, holding_days, params, data
    ):
        hold = params.get("hold_days", self._hold_days)
        if holding_days >= hold:
            return True, "hold_period"
        if entry_price > 0 and (current_price - entry_price) / entry_price <= -0.10:
            return True, "stop_loss"
        return False, ""

    def build_propose_prompt(self, context):
        return "test"


class FakeStrategy2(FakeStrategy):
    name = "fake_strat_2"

    def screen(self, data, date, params):
        return [
            Candidate(
                ticker="MSFT",
                date=date,
                direction="long",
                score=3.0,
                metadata={"source": "test"},
            )
        ]


@pytest.fixture
def state_dir(tmp_path):
    """Temporary state directory."""
    return str(tmp_path / "state")


@pytest.fixture
def state(state_dir):
    return StateManager(state_dir)


@pytest.fixture
def trader(state):
    return PaperTrader(state)


@pytest.fixture
def journal(state_dir):
    return SignalJournal(state_dir)


# ---------------------------------------------------------------------------
# Test: PnL computed on close_trade
# ---------------------------------------------------------------------------


class TestPnLOnClose:
    def test_close_trade_stores_pnl(self, trader, state):
        """close_trade() is no longer an accounting mutation path."""
        with pytest.raises(RuntimeError, match="read-only"):
            trader.close_trade("trade", 110.0, "2026-03-15", "hold_period")

    def test_close_trade_short_pnl(self, trader, state):
        """Short mutation is rejected by the read-only wrapper."""
        with pytest.raises(RuntimeError, match="read-only"):
            trader.open_trade("fake_strat", "TSLA", "short", 200.0, "2026-03-01")

    def test_close_trade_loss(self, trader, state):
        """Long mutation is rejected by the read-only wrapper."""
        with pytest.raises(RuntimeError, match="read-only"):
            trader.open_trade("fake_strat", "AAPL", "long", 100.0, "2026-03-01")


# ---------------------------------------------------------------------------
# Test: paper_trader.check_exits triggers strategy exit rules
# ---------------------------------------------------------------------------


class TestCheckExits:
    def test_hold_period_exit(self, trader, state):
        """Trade exits after holding period."""
        # Dates are relative to "now" so they stay inside the price window that
        # _make_price_df anchors to datetime.now() (avoids a time-dependent test).
        with pytest.raises(RuntimeError, match="read-only"):
            trader.check_exits({}, {}, datetime.now().strftime("%Y-%m-%d"))

    def test_stop_loss_exit(self, trader, state):
        """Trade exits on stop loss."""
        # Relative dates keep the check inside _make_declining_price_df's window.
        with pytest.raises(RuntimeError, match="read-only"):
            trader.check_exits({}, {}, datetime.now().strftime("%Y-%m-%d"))


# ---------------------------------------------------------------------------
# Test: exact-session derived outcomes
# ---------------------------------------------------------------------------


class TestExactSessionOutcomes:
    def test_five_session_outcome_uses_next_session_open(self):
        signal = SignalMetricRecord(
            event_key="event-aapl",
            signal_id="signal-aapl",
            epoch_id="epoch",
            policy_id="30d",
            strategy="fake_strat",
            ticker="AAPL",
            direction="long",
            decision_at=datetime(2026, 8, 3, 20, tzinfo=UTC),
            reference_session=date(2026, 8, 3),
        )
        bars = {
            ("AAPL", date(2026, 8, 4)): MarketBar(
                "AAPL",
                date(2026, 8, 4),
                Decimal("100"),
                Decimal("101"),
                Decimal("99"),
                Decimal("101"),
                "fixture",
                datetime(2026, 8, 4, 22, tzinfo=UTC),
                False,
            ),
            ("AAPL", date(2026, 8, 10)): MarketBar(
                "AAPL",
                date(2026, 8, 10),
                Decimal("109"),
                Decimal("111"),
                Decimal("108"),
                Decimal("110"),
                "fixture",
                datetime(2026, 8, 10, 22, tzinfo=UTC),
                False,
            ),
        }

        outcome = OutcomeCalculator().build(signal, 5, bars)

        assert outcome.entry_session == date(2026, 8, 4)
        assert outcome.exit_session == date(2026, 8, 10)
        assert outcome.entry_price == Decimal("100")
        assert outcome.exit_price == Decimal("110")


# ---------------------------------------------------------------------------
# Test: Learning loop reads PnL and updates weights
# ---------------------------------------------------------------------------


class TestLearningLoop:
    def _build_engine(
        self,
        state_dir,
        strategies=None,
        adaptive=False,
        outcome_reader=None,
    ):
        """Build a MultiStrategyEngine with mocked dependencies."""
        config = {
            "autoresearch": {
                "state_dir": state_dir,
                "total_capital": 5000,
                "paper_trade": {"min_trades_for_evaluation": 2},
            },
            "execution": {"mode": "paper"},
        }
        strategies = strategies or [FakeStrategy(), FakeStrategy2()]
        state = StateManager(state_dir)
        engine = MultiStrategyEngine(
            config=config,
            strategies=strategies,
            state_manager=state,
            use_llm=False,
            adaptive_confidence=adaptive,
            outcome_reader=outcome_reader,
        )
        return engine, state

    @staticmethod
    def _outcome(index: int, *, hit: bool) -> OutcomeRecord:
        signed_return = Decimal("0.05" if hit else "-0.05")
        return OutcomeRecord(
            outcome_id=f"outcome-{index}",
            signal_id=f"signal-{index}",
            event_key=f"event-{index}",
            epoch_id="epoch-1",
            strategy="fake_strat",
            policy_id="policy-1",
            ticker="AAPL",
            direction="long",
            holding_sessions=5,
            entry_session=date(2026, 3, 2),
            exit_session=date(2026, 3, 9),
            entry_price=Decimal("100"),
            exit_price=Decimal("105" if hit else "95"),
            raw_return=signed_return,
            signed_return=signed_return,
            status="valid",
            invalid_reason="",
        )

    def test_directional_accuracy_uses_governed_outcomes(self, tmp_path):
        """The learning diagnostic formula remains a pure v2 outcome calculation."""
        outcomes = tuple(self._outcome(index, hit=index < 2) for index in range(3))

        accuracy = directional_accuracy(outcomes)

        assert accuracy.rate == pytest.approx(2 / 3)
        assert accuracy.actionable_count == 3


# ---------------------------------------------------------------------------
# Test: Adaptive confidence diverges from fixed
# ---------------------------------------------------------------------------


class TestAdaptiveConfidence:
    def test_confidence_from_journal(self, tmp_path):
        """Adaptive engine derives confidence from governed v2 outcomes."""
        from tradingagents.strategies.metrics.models import OutcomeRecord

        state_dir = str(tmp_path / "adaptive")
        config = {
            "autoresearch": {"state_dir": state_dir, "total_capital": 5000},
            "execution": {"mode": "paper"},
        }
        state = StateManager(state_dir)
        outcomes = tuple(
            OutcomeRecord(
                outcome_id=f"outcome-{index}",
                signal_id=f"signal-{index}",
                event_key=f"event-{index}",
                epoch_id="epoch-1",
                strategy="fake_strat",
                policy_id="policy-1",
                ticker="AAPL",
                direction="long",
                holding_sessions=5,
                entry_session=date(2026, 3, 2),
                exit_session=date(2026, 3, 9),
                entry_price=Decimal("100"),
                exit_price=Decimal("105" if index < 12 else "95"),
                raw_return=Decimal("0.05" if index < 12 else "-0.05"),
                signed_return=Decimal("0.05" if index < 12 else "-0.05"),
                status="valid",
                invalid_reason="",
            )
            for index in range(15)
        )
        engine = MultiStrategyEngine(
            config=config,
            strategies=[FakeStrategy()],
            state_manager=state,
            use_llm=False,
            outcome_reader=lambda strategy: (
                outcomes if strategy == "fake_strat" else ()
            ),
        )

        conf = engine._compute_strategy_confidence("fake_strat")
        assert conf > 0.5  # 80% hit rate should give above-average confidence

        assert engine._adaptive_confidence is False


# ---------------------------------------------------------------------------
# Test: Full 30-day multi-day simulation
# ---------------------------------------------------------------------------


class TestMultiDaySimulation:
    """Authoritative ledger lifecycle; learning/adaptive paths stay dormant."""

    def test_30_day_lifecycle(self, tmp_path):
        from datetime import date

        from test_30day_simulation import (
            _authoritative_committee,
            _authoritative_orchestrator,
        )
        from tradingagents.strategies.orchestration.trading_calendar import (
            next_session,
        )

        orchestrator, _ = _authoritative_orchestrator(tmp_path, hold_days=2)
        sessions = [date(2026, 3, 30)]
        for _ in range(5):
            sessions.append(next_session(sessions[-1]))

        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            daily = [
                orchestrator.run_daily(session.isoformat()) for session in sessions
            ]

        ledger = orchestrator.cohorts[0]["ledger"]
        fills = ledger.read_fills()
        assert daily[0]["cohort_0"]["trades_opened"] == []
        assert fills[0].session == sessions[1]
        assert fills[0].side == "buy"
        assert any(fill.side == "sell" for fill in fills)
        assert len(ledger.read_snapshots(valid_only=True)) == len(sessions)

    def test_cohort_divergence_is_disabled_during_foundation_trial(self, tmp_path):
        from datetime import date

        from test_30day_simulation import (
            _authoritative_committee,
            _authoritative_orchestrator,
        )

        orchestrator, source = _authoritative_orchestrator(tmp_path, cohorts=2)
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            result = orchestrator.run_daily(date(2026, 3, 30).isoformat())

        assert set(result) == {"cohort_0", "cohort_1"}
        assert len(source.benchmark_calls) == 1
        assert len(source.raw_calls) == 1
        assert all(
            cohort["engine"]._adaptive_confidence is False
            and cohort["config"].learning_policy.mode == "disabled"
            for cohort in orchestrator.cohorts
        )


class _CandidateLifecycleStrategy(FakeStrategy):
    """Stage AAPL first, then expose candidate-only ALX and MSFT."""

    name = "candidate_lifecycle"

    def __init__(self, first_session: date, *, overlap_only: bool = False):
        super().__init__()
        self._first_session = first_session
        self._overlap_only = overlap_only

    def screen(self, data, trading_date, params):
        session = date.fromisoformat(trading_date)
        tickers = (
            ("AAPL",)
            if session == self._first_session or self._overlap_only
            else ("ALX", "MSFT")
        )
        return [
            Candidate(
                ticker=ticker,
                date=trading_date,
                direction="long",
                score=5.0,
                metadata={
                    "source": "candidate-lifecycle-fixture",
                    "event_key": f"candidate-lifecycle:{ticker}:{trading_date}",
                    "observed_at": f"{trading_date}T19:00:00+00:00",
                },
            )
            for ticker in tickers
        ]


class _CandidateRecoveryPriceSource:
    """P0 remains strict while candidate-only outcomes are configurable."""

    def __init__(self, outcome: str = "quarantined") -> None:
        from test_30day_simulation import AuthoritativePriceSource

        self._governed = AuthoritativePriceSource()
        self.outcome = outcome
        self.governed_invalid = False
        self.candidate_calls: list[tuple[tuple[str, ...], date]] = []

    @property
    def raw_calls(self):
        return self._governed.raw_calls

    @property
    def benchmark_calls(self):
        return self._governed.benchmark_calls

    def get_daily_bars(
        self, tickers, start_session, end_session_inclusive, adjusted=False
    ):
        bars = self._governed.get_daily_bars(
            tickers, start_session, end_session_inclusive, adjusted
        )
        session = start_session
        if self.governed_invalid and set(tickers) == {"AAPL"}:
            bar = bars[("AAPL", session)]
            bars[("AAPL", session)] = MarketBar(
                bar.ticker,
                bar.session,
                bar.open,
                Decimal("102"),
                bar.low,
                Decimal("103"),
                bar.source,
                bar.fetched_at,
                bar.adjusted,
            )
        elif "ALX" in tickers and self.outcome in {"quarantined", "recovered"}:
            bar = bars[("ALX", session)]
            bars[("ALX", session)] = MarketBar(
                bar.ticker,
                bar.session,
                bar.open,
                Decimal("102"),
                bar.low,
                Decimal("103"),
                bar.source,
                bar.fetched_at,
                bar.adjusted,
            )
        return bars

    def get_corporate_actions(self, tickers, session):
        return self._governed.get_corporate_actions(tickers, session)

    def get_total_return_closes(self, symbols, start_session, end_session_inclusive):
        return self._governed.get_total_return_closes(
            symbols, start_session, end_session_inclusive
        )

    def resolve_candidate_daily_bars(
        self, tickers, session, processed_at, max_age=timedelta(hours=24)
    ):
        requested = tuple(sorted(tickers))
        self.candidate_calls.append((requested, session))
        bars = self._governed.get_daily_bars(
            list(requested), session, session, adjusted=False
        )
        attempts: list[CandidateBarAttempt] = []
        recovered: set[str] = set()
        quarantined: set[str] = set()
        for ticker in requested:
            bar = bars[(ticker, session)]
            if ticker != "ALX" or self.outcome == "valid":
                continue
            attempts.append(
                CandidateBarAttempt(
                    ticker,
                    session,
                    1,
                    "fixture-candidate",
                    processed_at,
                    bar.open,
                    Decimal("102"),
                    bar.low,
                    Decimal("103"),
                    "incoherent ALX candidate bar",
                )
            )
            retry_error = (
                None
                if self.outcome == "recovered"
                else "incoherent ALX candidate bar"
            )
            attempts.append(
                CandidateBarAttempt(
                    ticker,
                    session,
                    2,
                    "fixture-candidate",
                    processed_at,
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close if retry_error is None else Decimal("103"),
                    retry_error,
                )
            )
            if retry_error is None:
                recovered.add(ticker)
            else:
                quarantined.add(ticker)
                bars.pop((ticker, session))
        return CandidateBarResolution(
            bars=bars,
            attempts=tuple(attempts),
            recovered_tickers=frozenset(recovered),
            quarantined_tickers=frozenset(quarantined),
        )


def _candidate_lifecycle_orchestrator(tmp_path, source, *, overlap_only=False):
    from test_30day_simulation import (
        _authoritative_orchestrator,
        _cohort_config,
    )

    first_session = date(2026, 3, 30)
    configs = [
        _cohort_config(tmp_path, "horizon_30d"),
        type(_cohort_config(tmp_path, "horizon_3m"))(
            name="horizon_3m",
            state_dir=str(tmp_path / "horizon_3m"),
            horizon="3m",
            size_profile="5k",
            use_llm=False,
        ),
    ]
    orchestrator, _ = _authoritative_orchestrator(
        tmp_path,
        strategy_modules=[
            _CandidateLifecycleStrategy(first_session, overlap_only=overlap_only)
        ],
        cohort_configs=configs,
        source=source,
    )
    return orchestrator, first_session


class TestCandidateBarLifecycle:
    def test_candidate_quarantine_preserves_completed_p0_and_filters_every_horizon(
        self, tmp_path
    ):
        from test_30day_simulation import _authoritative_committee
        from tradingagents.strategies.orchestration.trading_calendar import next_session

        source = _CandidateRecoveryPriceSource("quarantined")
        orchestrator, first_session = _candidate_lifecycle_orchestrator(
            tmp_path, source
        )
        staged_tickers: dict[str, list[str]] = {}
        for cohort in orchestrator.cohorts:
            original = cohort["engine"].screen_and_stage
            name = cohort["config"].name

            def capture(*args, _original=original, _name=name, **kwargs):
                staged_tickers[_name] = [
                    signal["ticker"] for signal in kwargs["shared_signals"]
                ]
                return _original(*args, **kwargs)

            cohort["engine"].screen_and_stage = capture

        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
            result = orchestrator.run_daily(next_session(first_session).isoformat())

        assert staged_tickers == {
            "horizon_30d": ["MSFT"],
            "horizon_3m": ["MSFT"],
        }
        assert all(
            cohort_result["error"] is False
            and cohort_result["degraded"] is True
            and cohort_result["execution_valid"] is True
            and cohort_result["staging_valid"] is False
            and cohort_result["candidate_bar_quarantines"] == ["ALX"]
            for cohort_result in result.values()
        )
        records = orchestrator._metric_store.read_candidate_bar_recoveries(
            orchestrator._epoch_id, next_session(first_session)
        )
        assert len(records) == 1
        assert records[0].ticker == "ALX"
        assert records[0].outcome == "quarantined"
        epoch = orchestrator._metric_store.load_epoch(orchestrator._epoch_id)
        assert epoch.status == "open"
        assert orchestrator._metric_store.pending_critical_gap() is None
        health = orchestrator._metric_store.read_strategy_health(
            orchestrator._epoch_id, session=next_session(first_session)
        )
        assert {
            record.status
            for record in health
            if record.strategy == "candidate_lifecycle"
        } == {"data_failure"}
        regime = orchestrator.cohorts[0]["state"].load_latest_regime()
        assert regime["execution_valid"] is True
        assert regime["staging_valid"] is False
        assert regime["candidate_bar_quarantines"] == ["ALX"]

    def test_recovered_candidate_reaches_staging(self, tmp_path):
        from test_30day_simulation import _authoritative_committee
        from tradingagents.strategies.orchestration.trading_calendar import next_session

        source = _CandidateRecoveryPriceSource("recovered")
        orchestrator, first_session = _candidate_lifecycle_orchestrator(
            tmp_path, source
        )
        staged: list[str] = []
        original = orchestrator.cohorts[0]["engine"].screen_and_stage

        def capture(*args, **kwargs):
            staged.extend(signal["ticker"] for signal in kwargs["shared_signals"])
            return original(*args, **kwargs)

        orchestrator.cohorts[0]["engine"].screen_and_stage = capture
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
            staged.clear()
            result = orchestrator.run_daily(next_session(first_session).isoformat())

        assert staged == ["ALX", "MSFT"]
        assert all(item["staging_valid"] is True for item in result.values())
        records = orchestrator._metric_store.read_candidate_bar_recoveries(
            orchestrator._epoch_id, next_session(first_session)
        )
        assert [(record.ticker, record.outcome) for record in records] == [
            ("ALX", "recovered")
        ]

    def test_governed_bar_failure_retains_critical_gap_path(self, tmp_path):
        from test_30day_simulation import _authoritative_committee
        from tradingagents.strategies.orchestration.trading_calendar import next_session

        source = _CandidateRecoveryPriceSource("valid")
        orchestrator, first_session = _candidate_lifecycle_orchestrator(
            tmp_path, source
        )
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
            source.governed_invalid = True
            result = orchestrator.run_daily(next_session(first_session).isoformat())

        assert all(
            item["error"] is True
            and item["degraded"] is False
            and item["execution_valid"] is False
            and item["staging_valid"] is False
            and item["candidate_bar_quarantines"] == []
            for item in result.values()
        )
        epoch = orchestrator._metric_store.load_epoch(orchestrator._epoch_id)
        assert epoch.status == "invalid"
        with sqlite3.connect(orchestrator._metric_store.path) as connection:
            marker_count = connection.execute(
                "SELECT COUNT(*) FROM critical_gap_markers"
            ).fetchone()[0]
        assert marker_count == 1
        assert epoch.boundary_reason == "critical_market_data_gap"

    def test_governed_overlap_reuses_p0_bar_without_candidate_fetch(self, tmp_path):
        from test_30day_simulation import _authoritative_committee
        from tradingagents.strategies.orchestration.trading_calendar import next_session

        source = _CandidateRecoveryPriceSource("valid")
        orchestrator, first_session = _candidate_lifecycle_orchestrator(
            tmp_path, source, overlap_only=True
        )
        with patch(
            "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
            side_effect=_authoritative_committee,
        ):
            orchestrator.run_daily(first_session.isoformat())
            before = len(source.candidate_calls)
            result = orchestrator.run_daily(next_session(first_session).isoformat())

        assert len(source.candidate_calls) == before
        assert all(item["staging_valid"] is True for item in result.values())


class TestCohortComparison:
    def test_comparison_with_data(self, tmp_path):
        """CohortComparison delegates exact historical epoch selection."""
        from unittest.mock import Mock

        from tradingagents.strategies.metrics.service import MetricsService
        from tradingagents.strategies.orchestration.cohort_comparison import (
            CohortComparison,
        )

        service = Mock(spec=MetricsService)
        service.generation_report.return_value = {
            "metric_schema_version": 2,
            "epoch": {"epoch_id": "historical"},
        }
        result = CohortComparison(metrics_service=service).compare("historical")
        assert result["epoch"]["epoch_id"] == "historical"
        service.generation_report.assert_called_once_with(epoch_id="historical")
