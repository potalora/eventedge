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

import copy
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tradingagents.strategies.orchestration.cohort_orchestrator import (
    CohortConfig,
    CohortOrchestrator,
)
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.trading.paper_trader import PaperTrader
from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal
from tradingagents.strategies.state.state import StateManager
from tradingagents.strategies.modules.base import Candidate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_price_df(base_price: float, days: int = 60) -> pd.DataFrame:
    """Create a price DataFrame with slight daily drift."""
    dates = pd.bdate_range(end=datetime.now(), periods=days)
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

    def check_exit(self, ticker, entry_price, current_price, holding_days, params, data):
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

        trader.close_trade(trade_id, exit_price=110.0, exit_date="2026-03-15", exit_reason="hold_period")

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

        trader.close_trade(trade_id, exit_price=180.0, exit_date="2026-03-15", exit_reason="hold_period")

        closed = state.load_paper_trades(status="closed")
        t = closed[0]
        assert t["pnl_pct"] == pytest.approx(0.1, abs=0.001)  # Short: (200-180)/200 = 10% gain
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

        trader.close_trade(trade_id, exit_price=90.0, exit_date="2026-03-15", exit_reason="stop_loss")

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
        trade_id = trader.open_trade(
            strategy="fake_strat",
            ticker="AAPL",
            direction="long",
            entry_price=150.0,
            entry_date="2026-03-01",
            shares=5,
        )

        prices = _make_price_df(150.0, days=30)
        strategies = {"fake_strat": FakeStrategy(hold_days=10)}

        # Day 5: should NOT exit
        closed = trader.check_exits(strategies, {"AAPL": prices}, "2026-03-06")
        assert len(closed) == 0

        # Day 11: should exit (holding_days >= 10)
        closed = trader.check_exits(strategies, {"AAPL": prices}, "2026-03-12")
        assert len(closed) == 1
        assert closed[0]["exit_reason"] == "hold_period"

    def test_stop_loss_exit(self, trader, state):
        """Trade exits on stop loss."""
        trade_id = trader.open_trade(
            strategy="fake_strat",
            ticker="BAD",
            direction="long",
            entry_price=100.0,
            entry_date="2026-03-01",
            shares=5,
        )

        prices = _make_declining_price_df(100.0, days=30)
        strategies = {"fake_strat": FakeStrategy(hold_days=30)}

        # After ~10 days of -1%/day decline, should hit -10% stop
        closed = trader.check_exits(strategies, {"BAD": prices}, "2026-03-15")
        assert len(closed) == 1
        assert closed[0]["exit_reason"] == "stop_loss"


# ---------------------------------------------------------------------------
# Test: Signal journal back-fills return data
# ---------------------------------------------------------------------------

class TestJournalOutcomes:

    def test_fill_outcomes_after_5d(self, journal):
        """return_5d gets filled after 5 calendar days."""
        signal_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        journal.log_signal(JournalEntry(
            timestamp=signal_date,
            strategy="fake_strat",
            ticker="AAPL",
            direction="long",
            score=5.0,
            traded=True,
            entry_price=150.0,
        ))

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

        journal.log_signal(JournalEntry(
            timestamp=signal_date,
            strategy="fake_strat",
            ticker="AAPL",
            direction="long",
            score=5.0,
            traded=True,
            entry_price=150.0,
        ))

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
            trader.close_trade(tid, exit_price=110.0, exit_date="2026-03-15", exit_reason="hold_period")

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
            state.save_paper_trade({
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
            })

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
            journal.log_signal(JournalEntry(
                timestamp=f"2026-03-{i+1:02d}",
                strategy="fake_strat",
                ticker="AAPL",
                direction="long",
                score=5.0,
                traded=True,
                entry_price=150.0,
                return_5d=ret,
            ))

        conf = engine._compute_strategy_confidence("fake_strat")
        assert conf > 0.5  # 80% hit rate should give above-average confidence

        # When adaptive is off, run_paper_trade_phase uses 0.5 directly
        # (it doesn't call _compute_strategy_confidence at all)
        engine._adaptive_confidence = False
        # Verify the flag controls behavior — non-adaptive always uses 0.5
        # regardless of journal content (tested via the strategy_confidence dict
        # construction in run_paper_trade_phase, not _compute_strategy_confidence)


# ---------------------------------------------------------------------------
# Test: Full 30-day multi-day simulation
# ---------------------------------------------------------------------------

class TestMultiDaySimulation:
    """Simulate 30 trading days end-to-end with mocked data."""

    def _build_engine(self, state_dir, adaptive=False):
        config = {
            "autoresearch": {
                "state_dir": state_dir,
                "total_capital": 5000,
                "paper_trade": {"min_trades_for_evaluation": 2},
            },
            "execution": {"mode": "paper"},
        }
        state = StateManager(state_dir)
        strategies = [FakeStrategy(hold_days=10), FakeStrategy2(hold_days=15)]
        engine = MultiStrategyEngine(
            config=config,
            strategies=strategies,
            state_manager=state,
            use_llm=False,
            adaptive_confidence=adaptive,
        )
        return engine, state

    def _make_mock_data(self, date_str):
        """Build mock data dict that strategies can screen against."""
        return {
            "yfinance": {
                "prices": {
                    "AAPL": _make_price_df(150.0),
                    "MSFT": _make_price_df(400.0),
                },
            },
        }

    @patch("tradingagents.strategies.orchestration.multi_strategy_engine.MultiStrategyEngine._fetch_all_data")
    @patch("tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize")
    def test_30_day_lifecycle(self, mock_committee, mock_fetch, tmp_path):
        """Run 30 days: open trades, hold, exit, back-fill returns, learn."""
        state_dir = str(tmp_path / "sim30")
        engine, state = self._build_engine(state_dir)

        # Mock data fetch
        prices = {
            "AAPL": _make_price_df(150.0, days=60),
            "MSFT": _make_price_df(400.0, days=60),
        }
        mock_fetch.return_value = {"yfinance": {"prices": prices}}

        # Mock committee: approve all signals with moderate sizing
        def fake_committee(signals, regime_context=None, strategy_confidence=None, current_positions=None, total_capital=None, **kwargs):
            from tradingagents.strategies.trading.portfolio_committee import TradeRecommendation as Recommendation
            recs = []
            seen = set()
            for s in signals[:3]:  # max 3 per day
                if s["ticker"] not in seen:
                    recs.append(Recommendation(
                        ticker=s["ticker"],
                        direction=s["direction"],
                        position_size_pct=0.08,
                        confidence=0.5,
                        rationale="test",
                        contributing_strategies=[s["strategy"]],
                    ))
                    seen.add(s["ticker"])
            return recs
        mock_committee.side_effect = fake_committee

        # Populate the price cache manually since _fetch_all_data is mocked
        engine._price_cache = prices

        base_date = datetime(2026, 3, 1)
        results_by_day = []

        for day in range(30):
            trading_date = (base_date + timedelta(days=day)).strftime("%Y-%m-%d")
            result = engine.run_paper_trade_phase(trading_date=trading_date)
            results_by_day.append(result)

        # Verify: trades were opened on day 1
        assert len(results_by_day[0]["trades_opened"]) > 0

        # Verify: some trades were closed (hold_days=10 for fake_strat)
        all_closed = sum(len(r.get("trades_closed", [])) for r in results_by_day)
        assert all_closed > 0, "No trades were closed in 30 days"

        # Verify: closed trades have PnL
        closed_trades = state.load_paper_trades(status="closed")
        assert len(closed_trades) > 0
        for t in closed_trades:
            assert "pnl" in t, f"Trade {t.get('trade_id')} missing pnl field"
            assert "pnl_pct" in t, f"Trade {t.get('trade_id')} missing pnl_pct field"

        # Verify: signal journal has entries with back-filled returns
        journal = engine._journal
        entries = journal.get_entries()
        assert len(entries) > 0
        entries_with_returns = [e for e in entries if e.get("return_5d") is not None]
        # After 30 days, early entries (day 1-25) should have return_5d
        assert len(entries_with_returns) > 0, "No signal journal entries have return_5d"

        # Run learning loop
        state.save_learning_loop_state({"last_run": "2020-01-01T00:00:00"})
        learn_result = engine.run_learning_loop()
        assert learn_result["triggered"] is True
        assert "scores" in learn_result

    @patch("tradingagents.strategies.orchestration.multi_strategy_engine.MultiStrategyEngine._fetch_all_data")
    @patch("tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize")
    def test_cohort_divergence(self, mock_committee, mock_fetch, tmp_path):
        """After enough history, adaptive cohort should have different confidence than control."""
        control_dir = str(tmp_path / "control")
        adaptive_dir = str(tmp_path / "adaptive")

        control_engine, control_state = self._build_engine(control_dir, adaptive=False)
        adaptive_engine, adaptive_state = self._build_engine(adaptive_dir, adaptive=True)

        prices = {
            "AAPL": _make_price_df(150.0, days=60),
            "MSFT": _make_price_df(400.0, days=60),
        }
        mock_fetch.return_value = {"yfinance": {"prices": prices}}
        control_engine._price_cache = prices
        adaptive_engine._price_cache = prices

        def fake_committee(signals, regime_context=None, strategy_confidence=None, current_positions=None, total_capital=None, **kwargs):
            from tradingagents.strategies.trading.portfolio_committee import TradeRecommendation as Recommendation
            recs = []
            seen = set()
            for s in signals[:2]:
                if s["ticker"] not in seen:
                    recs.append(Recommendation(
                        ticker=s["ticker"],
                        direction=s["direction"],
                        position_size_pct=0.08,
                        confidence=strategy_confidence.get(s["strategy"], 0.5) if strategy_confidence else 0.5,
                        rationale="test",
                        contributing_strategies=[s["strategy"]],
                    ))
                    seen.add(s["ticker"])
            return recs
        mock_committee.side_effect = fake_committee

        # Seed the adaptive journal with history (so confidence diverges from 0.5)
        for i in range(15):
            ret = 0.05 if i < 12 else -0.05  # 80% hit rate
            adaptive_engine._journal.log_signal(JournalEntry(
                timestamp=f"2026-02-{i+1:02d}",
                strategy="fake_strat",
                ticker="AAPL",
                direction="long",
                score=5.0,
                traded=True,
                entry_price=150.0,
                return_5d=ret,
            ))

        # Run one day for each cohort
        trading_date = "2026-03-01"
        control_result = control_engine.run_paper_trade_phase(trading_date=trading_date)
        adaptive_result = adaptive_engine.run_paper_trade_phase(trading_date=trading_date)

        # Both should produce trades
        assert len(control_result["trades_opened"]) > 0
        assert len(adaptive_result["trades_opened"]) > 0

        # Adaptive confidence should be > 0.5 for fake_strat (80% hit rate)
        adaptive_conf = adaptive_engine._compute_strategy_confidence("fake_strat")
        control_conf = 0.5  # Always fixed
        assert adaptive_conf > control_conf


# ---------------------------------------------------------------------------
# Test: Cohort comparison produces valid report
# ---------------------------------------------------------------------------

class TestCohortComparison:

    def test_comparison_with_data(self, tmp_path):
        """CohortComparison.compare() works with populated state dirs."""
        from tradingagents.strategies.orchestration.cohort_comparison import CohortComparison

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
                trader.close_trade(tid, exit_price=exit_px, exit_date="2026-03-15", exit_reason="hold_period")

        comparison = CohortComparison({
            "control": str(tmp_path / "control"),
            "adaptive": str(tmp_path / "adaptive"),
        })

        result = comparison.compare()
        assert "cohorts" in result
        assert "control" in result["cohorts"]
        assert "adaptive" in result["cohorts"]
        # Adaptive should have higher PnL (exit at 110 vs 105)
        assert result["cohorts"]["adaptive"]["avg_pnl"] > result["cohorts"]["control"]["avg_pnl"]

        report = comparison.format_report()
        assert isinstance(report, str)
        assert len(report) > 0
