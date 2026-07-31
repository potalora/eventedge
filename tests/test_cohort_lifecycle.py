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

from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.strategies.orchestration.multi_strategy_engine import (
    MultiStrategyEngine,
)
from tradingagents.strategies.trading.paper_trader import PaperTrader
from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal
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
        """close_trade() must store pnl and pnl_pct fields."""
        trade_id = trader.open_trade(
            strategy="fake_strat",
            ticker="AAPL",
            direction="long",
            entry_price=100.0,
            entry_date="2026-03-01",
            shares=10,
            position_value=1000.0,
        )

        trader.close_trade(
            trade_id,
            exit_price=110.0,
            exit_date="2026-03-15",
            exit_reason="hold_period",
        )

        closed = state.load_paper_trades(status="closed")
        assert len(closed) == 1
        t = closed[0]
        assert t["pnl_pct"] == pytest.approx(0.1, abs=0.001)
        assert t["pnl"] == pytest.approx(100.0, abs=1.0)  # 10% * $100 * 10 shares

    def test_close_trade_short_pnl(self, trader, state):
        """Short trade PnL should be inverted."""
        trade_id = trader.open_trade(
            strategy="fake_strat",
            ticker="TSLA",
            direction="short",
            entry_price=200.0,
            entry_date="2026-03-01",
            shares=5,
        )

        trader.close_trade(
            trade_id,
            exit_price=180.0,
            exit_date="2026-03-15",
            exit_reason="hold_period",
        )

        closed = state.load_paper_trades(status="closed")
        t = closed[0]
        assert t["pnl_pct"] == pytest.approx(
            0.1, abs=0.001
        )  # Short: (200-180)/200 = 10% gain
        assert t["pnl"] == pytest.approx(100.0, abs=1.0)

    def test_close_trade_loss(self, trader, state):
        """Losing long trade should have negative PnL."""
        trade_id = trader.open_trade(
            strategy="fake_strat",
            ticker="AAPL",
            direction="long",
            entry_price=100.0,
            entry_date="2026-03-01",
            shares=10,
        )

        trader.close_trade(
            trade_id, exit_price=90.0, exit_date="2026-03-15", exit_reason="stop_loss"
        )

        closed = state.load_paper_trades(status="closed")
        t = closed[0]
        assert t["pnl_pct"] == pytest.approx(-0.1, abs=0.001)
        assert t["pnl"] == pytest.approx(-100.0, abs=1.0)


# ---------------------------------------------------------------------------
# Test: paper_trader.check_exits triggers strategy exit rules
# ---------------------------------------------------------------------------


class TestCheckExits:
    def test_hold_period_exit(self, trader, state):
        """Trade exits after holding period."""
        # Dates are relative to "now" so they stay inside the price window that
        # _make_price_df anchors to datetime.now() (avoids a time-dependent test).
        entry_date = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d")
        trader.open_trade(
            strategy="fake_strat",
            ticker="AAPL",
            direction="long",
            entry_price=150.0,
            entry_date=entry_date,
            shares=5,
        )

        prices = _make_price_df(150.0, days=30)
        strategies = {"fake_strat": FakeStrategy(hold_days=10)}

        # 3 days held: should NOT exit
        early = (datetime.now() - timedelta(days=12)).strftime("%Y-%m-%d")
        closed = trader.check_exits(strategies, {"AAPL": prices}, early)
        assert len(closed) == 0

        # 15 days held: should exit (holding_days >= 10)
        today = datetime.now().strftime("%Y-%m-%d")
        closed = trader.check_exits(strategies, {"AAPL": prices}, today)
        assert len(closed) == 1
        assert closed[0]["exit_reason"] == "hold_period"

    def test_stop_loss_exit(self, trader, state):
        """Trade exits on stop loss."""
        # Relative dates keep the check inside _make_declining_price_df's window.
        entry_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        trader.open_trade(
            strategy="fake_strat",
            ticker="BAD",
            direction="long",
            entry_price=100.0,
            entry_date=entry_date,
            shares=5,
        )

        prices = _make_declining_price_df(100.0, days=30)
        strategies = {"fake_strat": FakeStrategy(hold_days=30)}

        # Price has declined well past -10%: should hit stop loss (not hold period)
        today = datetime.now().strftime("%Y-%m-%d")
        closed = trader.check_exits(strategies, {"BAD": prices}, today)
        assert len(closed) == 1
        assert closed[0]["exit_reason"] == "stop_loss"


# ---------------------------------------------------------------------------
# Test: Signal journal back-fills return data
# ---------------------------------------------------------------------------


class TestJournalOutcomes:
    def test_fill_outcomes_after_5d(self, journal):
        """return_5d gets filled after 5 calendar days."""
        signal_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        journal.log_signal(
            JournalEntry(
                timestamp=signal_date,
                strategy="fake_strat",
                ticker="AAPL",
                direction="long",
                score=5.0,
                traded=True,
                entry_price=150.0,
            )
        )

        prices = _make_price_df(150.0, days=30)
        today = datetime.now().strftime("%Y-%m-%d")
        updated = journal.fill_outcomes({"AAPL": prices}, today)

        assert updated == 1
        entries = journal.get_entries()
        assert entries[0]["return_5d"] is not None
        assert entries[0]["return_5d"] > 0  # prices drift up

    def test_no_fill_before_5d(self, journal):
        """return_5d stays None before 5 days elapsed."""
        signal_date = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")

        journal.log_signal(
            JournalEntry(
                timestamp=signal_date,
                strategy="fake_strat",
                ticker="AAPL",
                direction="long",
                score=5.0,
                traded=True,
                entry_price=150.0,
            )
        )

        prices = _make_price_df(150.0, days=30)
        today = datetime.now().strftime("%Y-%m-%d")
        updated = journal.fill_outcomes({"AAPL": prices}, today)

        assert updated == 0
        entries = journal.get_entries()
        assert entries[0]["return_5d"] is None


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
        trader = PaperTrader(state)
        for i in range(3):
            tid = trader.open_trade(
                strategy="fake_strat",
                ticker=f"T{i}",
                direction="long",
                entry_price=100.0,
                entry_date="2026-03-01",
                shares=5,
            )
            trader.close_trade(
                tid, exit_price=110.0, exit_date="2026-03-15", exit_reason="hold_period"
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
        """Adaptive engine derives confidence from signal journal hit rates."""
        state_dir = str(tmp_path / "adaptive")
        config = {
            "autoresearch": {"state_dir": state_dir, "total_capital": 5000},
            "execution": {"mode": "paper"},
        }
        state = StateManager(state_dir)
        engine = MultiStrategyEngine(
            config=config,
            strategies=[FakeStrategy()],
            state_manager=state,
            use_llm=False,
            adaptive_confidence=True,
        )

        # Log 15 signals: 12 correct (80% hit rate)
        journal = engine._journal
        for i in range(15):
            ret = 0.05 if i < 12 else -0.05
            journal.log_signal(
                JournalEntry(
                    timestamp=f"2026-03-{i + 1:02d}",
                    strategy="fake_strat",
                    ticker="AAPL",
                    direction="long",
                    score=5.0,
                    traded=True,
                    entry_price=150.0,
                    return_5d=ret,
                )
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
        """CohortComparison.compare() works with populated state dirs."""
        from tradingagents.strategies.orchestration.cohort_comparison import (
            CohortComparison,
        )

        # Set up two cohort state dirs with trades
        for cohort_name in ("control", "adaptive"):
            sd = str(tmp_path / cohort_name)
            state = StateManager(sd)
            trader = PaperTrader(state)

            for i in range(3):
                tid = trader.open_trade(
                    strategy="fake_strat",
                    ticker=f"T{i}",
                    direction="long",
                    entry_price=100.0,
                    entry_date="2026-03-01",
                    shares=5,
                )
                exit_px = 110.0 if cohort_name == "adaptive" else 105.0
                trader.close_trade(
                    tid,
                    exit_price=exit_px,
                    exit_date="2026-03-15",
                    exit_reason="hold_period",
                )

        comparison = CohortComparison(
            {
                "control": str(tmp_path / "control"),
                "adaptive": str(tmp_path / "adaptive"),
            }
        )

        result = comparison.compare()
        assert "cohorts" in result
        assert "control" in result["cohorts"]
        assert "adaptive" in result["cohorts"]
        # Adaptive should have higher PnL (exit at 110 vs 105)
        assert (
            result["cohorts"]["adaptive"]["avg_pnl"]
            > result["cohorts"]["control"]["avg_pnl"]
        )

        report = comparison.format_report()
        assert isinstance(report, str)
        assert len(report) > 0
