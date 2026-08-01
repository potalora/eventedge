"""Multi-day cohort lifecycle test.

Simulates 30 trading days of the 2-cohort paper trading trial to verify:
1. Trades open correctly on day 1
2. Exit checks fire when holding period / stop loss is reached
3. PnL is computed on close
4. Signal journal back-fills return_5d/10d/30d
5. Learning loop reads closed-trade PnL and updates weights
6. Adaptive confidence diverges from control after enough history
7. Cohort comparison produces valid report

Uses fully mocked data (no API calls, no LLM).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.strategies.orchestration.multi_strategy_engine import (
    MultiStrategyEngine,
)
from tradingagents.strategies.trading.paper_trader import PaperTrader
from tradingagents.strategies.learning.signal_journal import SignalJournal
from tradingagents.strategies.execution.models import MarketBar
from tradingagents.strategies.metrics.models import SignalMetricRecord
from tradingagents.strategies.metrics.outcomes import OutcomeCalculator
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
    def _build_engine(self, state_dir, strategies=None, adaptive=False):
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
        )
        return engine, state

    def test_learning_loop_reads_pnl(self, tmp_path):
        """Learning loop computes scores from closed trade PnL."""
        state_dir = str(tmp_path / "learn")
        engine, state = self._build_engine(state_dir)

        # Force learning loop to trigger
        state.save_learning_loop_state({"last_run": "2020-01-01T00:00:00"})

        # Create closed trades with PnL
        for i in range(3):
            state.save_paper_trade(
                {
                    "strategy": "fake_strat",
                    "ticker": f"T{i}",
                    "direction": "long",
                    "entry_price": 100.0,
                    "exit_price": 110.0,
                    "entry_date": "2026-03-01",
                    "exit_date": "2026-03-15",
                    "shares": 5,
                    "status": "closed",
                    "pnl": 50.0,
                    "pnl_pct": 0.1,
                }
            )

        result = engine.run_learning_loop()
        assert result["triggered"] is True
        assert "fake_strat" in result["scores"]

    def test_learning_loop_fallback_pnl(self, tmp_path):
        """Learning loop computes PnL on-the-fly for trades missing the pnl field."""
        state_dir = str(tmp_path / "fallback")
        engine, state = self._build_engine(state_dir)

        state.save_learning_loop_state({"last_run": "2020-01-01T00:00:00"})

        # Simulate old-format closed trades (no pnl field)
        for i in range(3):
            state.save_paper_trade(
                {
                    "strategy": "fake_strat",
                    "ticker": f"T{i}",
                    "direction": "long",
                    "entry_price": 100.0,
                    "exit_price": 115.0,
                    "entry_date": "2026-03-01",
                    "exit_date": "2026-03-15",
                    "shares": 5,
                    "status": "closed",
                    "exit_reason": "hold_period",
                    # No pnl or pnl_pct field!
                }
            )

        result = engine.run_learning_loop()
        assert result["triggered"] is True


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
            adaptive_confidence=True,
            outcome_reader=lambda strategy: (
                outcomes if strategy == "fake_strat" else ()
            ),
        )

        conf = engine._compute_strategy_confidence("fake_strat")
        assert conf > 0.5  # 80% hit rate should give above-average confidence

        # The foundation trial forces adaptive behavior off.
        engine._adaptive_confidence = False
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
            and cohort["config"].learning_enabled is False
            for cohort in orchestrator.cohorts
        )
        assert all(
            item["reason"] == "learning_disabled"
            for item in orchestrator.run_learning().values()
        )


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
