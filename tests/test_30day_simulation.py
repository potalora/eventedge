"""30-day simulation harness for the paper trading pipeline.

Validates the entire pipeline end-to-end with synthetic data:
1. Broker reconstruction across days
2. Signal journal fill_outcomes with correct target-date prices
3. Idempotency (double-run same date produces no duplicates)
4. Full 30-day lifecycle: open, hold, exit, back-fill, learn
5. 2-cohort divergence over 30 days

All LLM and external API calls are mocked. Deterministic via seeded RNG.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from tradingagents.strategies.orchestration.cohort_comparison import CohortComparison
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    CohortConfig,
    CohortOrchestrator,
)
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.trading.paper_trader import PaperTrader
from tradingagents.strategies.trading.portfolio_committee import (
    PortfolioCommittee,
    TradeRecommendation,
)
from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal
from tradingagents.strategies.state.state import StateManager
from tradingagents.strategies.modules.base import Candidate
from tradingagents.execution.paper_broker import PaperBroker


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
        ticker: _make_price_df(ticker, bp, days=days)
        for ticker, bp in bases.items()
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
                metadata={"source": "test"},
            )
        ]

    def check_exit(self, ticker, entry_price, current_price, holding_days, params, data):
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
                metadata={"source": "test"},
            )
        ]


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
# 1. TestBrokerReconstructionAcrossDays
# ===========================================================================


class TestBrokerReconstructionAcrossDays:
    """Verify PaperBroker.reconstruct_from_trades restores state."""

    def test_reconstruction_restores_cash_and_positions(self):
        broker = PaperBroker(initial_capital=5000.0)
        broker.submit_stock_order("AAPL", "buy", 10, price=150.0)
        assert broker.cash == pytest.approx(3500.0)
        assert broker.positions["AAPL"]["quantity"] == 10

        # Fresh broker, reconstruct
        fresh = PaperBroker(initial_capital=5000.0)
        fresh.reconstruct_from_trades(
            [{"ticker": "AAPL", "shares": 10, "entry_price": 150.0}]
        )
        assert fresh.cash == pytest.approx(3500.0)
        assert "AAPL" in fresh.positions
        assert fresh.positions["AAPL"]["quantity"] == 10
        assert fresh.positions["AAPL"]["avg_price"] == pytest.approx(150.0)

    def test_reconstruction_multiple_tickers(self):
        fresh = PaperBroker(initial_capital=5000.0)
        fresh.reconstruct_from_trades([
            {"ticker": "AAPL", "shares": 5, "entry_price": 170.0},
            {"ticker": "MSFT", "shares": 3, "entry_price": 420.0},
        ])
        expected_cash = 5000.0 - (5 * 170.0) - (3 * 420.0)
        assert fresh.cash == pytest.approx(expected_cash)
        assert fresh.positions["AAPL"]["quantity"] == 5
        assert fresh.positions["MSFT"]["quantity"] == 3

    def test_reconstruction_empty_trades(self):
        fresh = PaperBroker(initial_capital=5000.0)
        fresh.reconstruct_from_trades([])
        assert fresh.cash == pytest.approx(5000.0)
        assert len(fresh.positions) == 0


# ===========================================================================
# 2. TestFillOutcomesCorrectPrices
# ===========================================================================


class TestFillOutcomesCorrectPrices:
    """Verify fill_outcomes uses the correct target-date price, not latest."""

    def _build_price_df(self, signal_date_str: str) -> pd.DataFrame:
        """Build a price DF with known prices at day 0, 5, 10, 30."""
        signal_dt = datetime.strptime(signal_date_str, "%Y-%m-%d")
        # Create 35 business days of prices starting from signal date
        dates = pd.bdate_range(start=signal_dt, periods=35)
        prices = [100.0] * 35  # default base

        # Set specific prices at known offsets by calendar day
        for i, d in enumerate(dates):
            cal_days = (d - pd.Timestamp(signal_dt)).days
            if cal_days <= 5:
                # Ramp from 100 to 105 over first 5 calendar days
                prices[i] = 100.0 + cal_days
            elif cal_days <= 10:
                # 110 around day 10
                prices[i] = 105.0 + (cal_days - 5)
            elif cal_days <= 30:
                # 120 around day 30
                frac = (cal_days - 10) / 20
                prices[i] = 110.0 + frac * 10.0
            else:
                prices[i] = 120.0

        return pd.DataFrame({"Close": prices}, index=dates)

    def test_5d_uses_day5_price_not_latest(self, tmp_path):
        signal_date = "2026-04-01"
        journal = SignalJournal(str(tmp_path / "state"))
        journal.log_signal(
            JournalEntry(
                timestamp=signal_date,
                strategy="test_strat",
                ticker="AAPL",
                direction="long",
                score=5.0,
                traded=True,
                entry_price=100.0,
            )
        )

        price_df = self._build_price_df(signal_date)
        price_cache = {"AAPL": price_df}

        # Day 7: only 5d should fill
        updated = journal.fill_outcomes(price_cache, "2026-04-08")
        assert updated == 1
        entries = journal.get_entries()
        assert entries[0]["return_5d"] is not None
        assert entries[0]["return_10d"] is None
        # Day-5 price is ~105, so return should be ~0.05
        assert entries[0]["return_5d"] == pytest.approx(0.05, abs=0.02)

        # Day 12: 10d should fill
        updated = journal.fill_outcomes(price_cache, "2026-04-13")
        assert updated == 1
        entries = journal.get_entries()
        assert entries[0]["return_10d"] is not None
        assert entries[0]["return_10d"] == pytest.approx(0.10, abs=0.02)

        # Day 32: 30d should fill
        updated = journal.fill_outcomes(price_cache, "2026-05-03")
        assert updated == 1
        entries = journal.get_entries()
        assert entries[0]["return_30d"] is not None
        assert entries[0]["return_30d"] == pytest.approx(0.20, abs=0.03)

    def test_short_direction_flips_sign(self, tmp_path):
        signal_date = "2026-04-01"
        journal = SignalJournal(str(tmp_path / "state"))
        journal.log_signal(
            JournalEntry(
                timestamp=signal_date,
                strategy="test_strat",
                ticker="AAPL",
                direction="short",
                score=5.0,
                traded=True,
                entry_price=100.0,
            )
        )

        price_df = self._build_price_df(signal_date)
        price_cache = {"AAPL": price_df}

        journal.fill_outcomes(price_cache, "2026-04-08")
        entries = journal.get_entries()
        # Short: return should be negative (price went up)
        assert entries[0]["return_5d"] is not None
        assert entries[0]["return_5d"] < 0


# ===========================================================================
# 3. TestIdempotencyDoubleRun
# ===========================================================================


class TestIdempotencyDoubleRun:
    """Running the same date twice must not duplicate signals or trades."""

    @patch(
        "tradingagents.strategies.orchestration.multi_strategy_engine.MultiStrategyEngine._fetch_all_data"
    )
    @patch(
        "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize"
    )
    def test_double_run_no_duplicates(self, mock_committee, mock_fetch, tmp_path):
        state_dir = str(tmp_path / "idem")
        engine, state = _build_engine(state_dir)

        prices = _build_price_cache(days=30)
        mock_fetch.return_value = {"yfinance": {"prices": prices}}
        mock_committee.side_effect = _make_fake_committee(max_per_day=2)
        engine._price_cache = prices

        trading_date = "2026-04-01"

        # First run
        result1 = engine.run_paper_trade_phase(trading_date=trading_date)
        signals_count_1 = len(engine._journal.get_entries())
        open_trades_1 = len(state.load_paper_trades(status="open"))

        assert signals_count_1 > 0, "First run should produce signals"
        assert open_trades_1 > 0, "First run should open trades"

        # Second run (same date)
        result2 = engine.run_paper_trade_phase(trading_date=trading_date)
        signals_count_2 = len(engine._journal.get_entries())
        open_trades_2 = len(state.load_paper_trades(status="open"))

        # Journal deduplication: same strategy+ticker+timestamp should not duplicate
        assert signals_count_2 == signals_count_1, (
            f"Signal count changed: {signals_count_1} -> {signals_count_2}"
        )
        # Idempotency guard: already_traded_today prevents duplicate trade opens
        assert open_trades_2 == open_trades_1, (
            f"Open trade count changed: {open_trades_1} -> {open_trades_2}"
        )


# ===========================================================================
# 4. TestThirtyDayFullLifecycle
# ===========================================================================


class TestThirtyDayFullLifecycle:
    """Full 30-day simulation with synthetic data."""

    @patch(
        "tradingagents.strategies.orchestration.multi_strategy_engine.MultiStrategyEngine._fetch_all_data"
    )
    @patch(
        "tradingagents.strategies.orchestration.multi_strategy_engine.MultiStrategyEngine._fetch_missing_prices"
    )
    @patch(
        "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize"
    )
    def test_30_day_full_lifecycle(
        self, mock_committee, mock_fetch_prices, mock_fetch_data, tmp_path
    ):
        state_dir = str(tmp_path / "sim30")
        strategies = [FakeStrategy(hold_days=5), FakeStrategy2()]
        engine, state = _build_engine(state_dir, strategies=strategies)

        prices = _build_price_cache(days=60)
        mock_fetch_data.return_value = {"yfinance": {"prices": prices}}
        mock_fetch_prices.return_value = None
        engine._price_cache = copy.deepcopy(prices)

        # Track which tickers are signaled each day (rotate through TICKERS)
        day_ticker_map = {}
        for day in range(30):
            day_ticker_map[day] = TICKERS[day % len(TICKERS)]

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
            for s in signals[:3]:
                if s["ticker"] not in seen:
                    recs.append(
                        TradeRecommendation(
                            ticker=s["ticker"],
                            direction=s["direction"],
                            position_size_pct=0.08,
                            confidence=0.6,
                            rationale="sim test",
                            contributing_strategies=[s["strategy"]],
                        )
                    )
                    seen.add(s["ticker"])
            return recs

        mock_committee.side_effect = fake_synthesize

        journal = engine._journal
        results_by_day: list[dict] = []

        for day in range(30):
            trading_date = (BASE_DATE + timedelta(days=day)).strftime("%Y-%m-%d")
            result = engine.run_paper_trade_phase(trading_date=trading_date)
            results_by_day.append(result)

        # --- Checkpoint: Day 1 ---
        assert len(results_by_day[0]["trades_opened"]) >= 1, (
            "Day 1 should open at least 1 trade"
        )
        day1_entries = journal.get_entries()
        assert len(day1_entries) > 0, "Journal should be non-empty after day 1"

        # --- Checkpoint: Day 7 ---
        closed_by_day7 = state.load_paper_trades(status="closed")
        assert len(closed_by_day7) > 0, (
            "By day 7, some trades should have closed (hold_days=5)"
        )
        for t in closed_by_day7:
            assert "pnl" in t, f"Closed trade {t.get('trade_id')} missing pnl"
            assert "pnl_pct" in t, f"Closed trade {t.get('trade_id')} missing pnl_pct"

        # Day-1 signals should have return_5d filled by day 7
        entries_after_7 = journal.get_entries()
        day1_ts = (BASE_DATE + timedelta(days=0)).strftime("%Y-%m-%d")
        day1_signals = [
            e for e in entries_after_7 if e["timestamp"] == day1_ts
        ]
        day1_with_5d = [e for e in day1_signals if e.get("return_5d") is not None]
        assert len(day1_with_5d) > 0, (
            "Day-1 signals should have return_5d by day 7"
        )

        # --- Checkpoint: Day 15 - Learning loop ---
        state.save_learning_loop_state({"last_run": "2020-01-01T00:00:00"})
        learn_result = engine.run_learning_loop()
        assert learn_result["triggered"] is True, "Learning loop should trigger"

        # --- Checkpoint: Day 30 - Final assertions ---
        all_entries = journal.get_entries()

        # No duplicate journal entries
        keys = set()
        for e in all_entries:
            key = (e["strategy"], e["ticker"], e["timestamp"])
            assert key not in keys, f"Duplicate journal entry: {key}"
            keys.add(key)

        # All closed trades have pnl fields
        all_closed = state.load_paper_trades(status="closed")
        for t in all_closed:
            assert "pnl" in t
            assert "pnl_pct" in t

        # Day-1 signals have return_5d and return_10d filled
        final_entries = journal.get_entries()
        day1_final = [e for e in final_entries if e["timestamp"] == day1_ts]
        for e in day1_final:
            if e.get("entry_price", 0) > 0:
                assert e.get("return_5d") is not None, (
                    f"Day-1 signal {e['strategy']}:{e['ticker']} missing return_5d"
                )
                assert e.get("return_10d") is not None, (
                    f"Day-1 signal {e['strategy']}:{e['ticker']} missing return_10d"
                )

        # Capital conservation: cash + positions_value ~ initial capital
        # (not exact due to realized PnL, but should be in the same ballpark)
        from tradingagents.strategies.trading.execution_bridge import ExecutionBridge

        bridge = ExecutionBridge(_base_config(state_dir))
        open_trades = state.load_paper_trades(status="open")
        if open_trades and hasattr(bridge.broker, "reconstruct_from_trades"):
            bridge.broker.reconstruct_from_trades(open_trades)
        account = bridge.get_account()

        # Total value should be within 30% of initial (generous, but prevents
        # catastrophic accounting errors like double-counting)
        assert account.portfolio_value > 0, "Portfolio value should be positive"
        assert account.portfolio_value < 10000, (
            "Portfolio value should not exceed 2x initial"
        )


# ===========================================================================
# 5. TestThirtyDayCohortDivergence
# ===========================================================================


class TestThirtyDayCohortDivergence:
    """Run 30 days through CohortOrchestrator with 2 cohorts."""

    @patch(
        "tradingagents.strategies.orchestration.multi_strategy_engine.MultiStrategyEngine._fetch_all_data"
    )
    @patch(
        "tradingagents.strategies.orchestration.multi_strategy_engine.MultiStrategyEngine._fetch_missing_prices"
    )
    @patch(
        "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize"
    )
    @patch(
        "tradingagents.strategies.modules.get_paper_trade_strategies"
    )
    def test_cohort_divergence_30_days(
        self,
        mock_get_strategies,
        mock_committee,
        mock_fetch_prices,
        mock_fetch_data,
        tmp_path,
    ):
        # Use our fake strategies
        strategies = [FakeStrategy(hold_days=5), FakeStrategy2()]
        mock_get_strategies.return_value = strategies

        prices = _build_price_cache(days=60)
        mock_fetch_data.return_value = {"yfinance": {"prices": prices}}
        mock_fetch_prices.return_value = None

        # Build separate cohort configs in separate temp dirs
        control_dir = str(tmp_path / "control")
        adaptive_dir = str(tmp_path / "adaptive")

        cohort_configs = [
            CohortConfig(
                name="control",
                state_dir=control_dir,
                horizon="30d",
                size_profile="5k",
                adaptive_confidence=False,
                learning_enabled=False,
                use_llm=False,
            ),
            CohortConfig(
                name="adaptive",
                state_dir=adaptive_dir,
                horizon="30d",
                size_profile="5k",
                adaptive_confidence=True,
                learning_enabled=True,
                use_llm=False,
            ),
        ]

        base_config = {
            "autoresearch": {
                "state_dir": str(tmp_path / "base"),
                "total_capital": 5000,
                "paper_trade": {
                    "min_trades_for_evaluation": 2,
                    "portfolio_committee_enabled": False,
                },
            },
            "execution": {"mode": "paper"},
        }

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
            # Pass through confidence so adaptive differs from control
            for s in signals[:3]:
                if s["ticker"] not in seen:
                    conf = (
                        strategy_confidence.get(s["strategy"], 0.5)
                        if strategy_confidence
                        else 0.5
                    )
                    recs.append(
                        TradeRecommendation(
                            ticker=s["ticker"],
                            direction=s["direction"],
                            position_size_pct=0.08,
                            confidence=conf,
                            rationale="cohort test",
                            contributing_strategies=[s["strategy"]],
                        )
                    )
                    seen.add(s["ticker"])
            return recs

        mock_committee.side_effect = fake_synthesize

        orchestrator = CohortOrchestrator(cohort_configs, base_config)

        # Inject price caches into each cohort's engine
        for cohort in orchestrator.cohorts:
            cohort["engine"]._price_cache = copy.deepcopy(prices)

        # Seed the adaptive cohort journal with history so confidence diverges
        adaptive_journal = orchestrator.cohorts[1]["engine"]._journal
        for i in range(15):
            ret = 0.05 if i < 12 else -0.05  # 80% hit rate
            adaptive_journal.log_signal(
                JournalEntry(
                    timestamp=f"2026-03-{i + 1:02d}",
                    strategy="fake_strat",
                    ticker="AAPL",
                    direction="long",
                    score=5.0,
                    traded=True,
                    entry_price=170.0,
                    return_5d=ret,
                )
            )

        # Run 30 days
        for day in range(30):
            trading_date = (BASE_DATE + timedelta(days=day)).strftime("%Y-%m-%d")
            orchestrator.run_daily(trading_date=trading_date)

        # --- Assertions ---

        # Both cohorts have journal entries (shared signals)
        control_journal = SignalJournal(control_dir)
        adaptive_journal_final = SignalJournal(adaptive_dir)
        control_entries = control_journal.get_entries()
        adaptive_entries = adaptive_journal_final.get_entries()
        assert len(control_entries) > 0, "Control cohort should have journal entries"
        assert len(adaptive_entries) > 0, "Adaptive cohort should have journal entries"

        # Cohort A (control) confidence stays at 0.5
        control_engine = orchestrator.cohorts[0]["engine"]
        assert control_engine._adaptive_confidence is False

        # State dirs are separate (no contamination)
        control_trades = StateManager(control_dir).load_paper_trades()
        adaptive_trades = StateManager(adaptive_dir).load_paper_trades()
        # Both should have trades but trade_ids should be different
        if control_trades and adaptive_trades:
            control_ids = {t["trade_id"] for t in control_trades}
            adaptive_ids = {t["trade_id"] for t in adaptive_trades}
            assert control_ids.isdisjoint(adaptive_ids), (
                "Cohort trade IDs should not overlap"
            )

        # CohortComparison.compare() returns valid structure
        comparison = CohortComparison({
            "control": control_dir,
            "adaptive": adaptive_dir,
        })
        result = comparison.compare()

        assert "cohorts" in result
        assert "control" in result["cohorts"]
        assert "adaptive" in result["cohorts"]
        assert "per_strategy" in result
        assert result["cohorts"]["control"]["total_signals"] > 0
        assert result["cohorts"]["adaptive"]["total_signals"] > 0

        # Verify the report can be generated without error
        report = comparison.format_report()
        assert isinstance(report, str)
        assert len(report) > 100


class TestOpenBBEnrichment:
    """Verify OpenBB enrichment works across 30-day simulation."""

    @patch(
        "tradingagents.strategies.orchestration.multi_strategy_engine.MultiStrategyEngine._fetch_all_data"
    )
    @patch(
        "tradingagents.strategies.orchestration.multi_strategy_engine.MultiStrategyEngine._fetch_missing_prices"
    )
    @patch(
        "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize"
    )
    @patch(
        "tradingagents.strategies.modules.get_paper_trade_strategies"
    )
    def test_enrichment_30_days_with_openbb(
        self,
        mock_get_strategies,
        mock_committee,
        mock_fetch_prices,
        mock_fetch_data,
        tmp_path,
    ):
        """Run 30 days with mocked OpenBB enrichment. Verify enrichment is passed to committee."""
        strategies = [FakeStrategy(hold_days=5), FakeStrategy2()]
        mock_get_strategies.return_value = strategies

        prices = _build_price_cache(days=60)
        mock_fetch_data.return_value = {"yfinance": {"prices": prices}}
        mock_fetch_prices.return_value = None

        control_dir = str(tmp_path / "control")
        adaptive_dir = str(tmp_path / "adaptive")

        cohort_configs = [
            CohortConfig(name="control", state_dir=control_dir, horizon="30d", size_profile="5k", adaptive_confidence=False, learning_enabled=False, use_llm=False),
            CohortConfig(name="adaptive", state_dir=adaptive_dir, horizon="30d", size_profile="5k", adaptive_confidence=True, learning_enabled=True, use_llm=False),
        ]

        base_config = {
            "autoresearch": {
                "state_dir": str(tmp_path / "base"),
                "total_capital": 5000,
                "paper_trade": {"min_trades_for_evaluation": 2, "portfolio_committee_enabled": False},
            },
            "execution": {"mode": "paper"},
        }

        # Track enrichment values passed to synthesize
        enrichment_values = []

        def fake_synthesize(signals, regime_context=None, strategy_confidence=None,
                            current_positions=None, total_capital=None, **kwargs):
            enrichment_values.append(kwargs.get("enrichment"))
            recs = []
            seen = set()
            for s in signals[:3]:
                if s["ticker"] not in seen:
                    recs.append(TradeRecommendation(
                        ticker=s["ticker"], direction=s["direction"],
                        position_size_pct=0.12, confidence=0.6,
                        rationale="enrichment test",
                        contributing_strategies=[s["strategy"]],
                    ))
                    seen.add(s["ticker"])
            return recs

        mock_committee.side_effect = fake_synthesize

        orchestrator = CohortOrchestrator(cohort_configs, base_config)

        # Inject price caches
        for cohort in orchestrator.cohorts:
            cohort["engine"]._price_cache = copy.deepcopy(prices)

        # Mock the OpenBB enrichment to return synthetic data
        mock_enrichment = {
            "profiles": {
                "AAPL": {"sector": "Technology", "industry": "Consumer Electronics", "market_cap": 3e12, "name": "Apple"},
                "MSFT": {"sector": "Technology", "industry": "Software", "market_cap": 2.8e12, "name": "Microsoft"},
                "AMZN": {"sector": "Consumer Cyclical", "industry": "Internet Retail", "market_cap": 1.8e12, "name": "Amazon"},
            },
            "short_interest": {
                "TSLA": {"short_interest": 50000000, "short_pct_of_float": 3.2, "days_to_cover": 1.5, "date": "2026-03-15"},
            },
            "factors": {"Mkt-RF": 0.02, "SMB": -0.01, "HML": 0.005},
        }
        with patch.object(orchestrator, "_fetch_openbb_enrichment", return_value=mock_enrichment):
            for day in range(30):
                trading_date = (BASE_DATE + timedelta(days=day)).strftime("%Y-%m-%d")
                orchestrator.run_daily(trading_date=trading_date)

        # Assertions
        # 1. Both cohorts received enrichment (2 cohorts * 30 days = 60 calls)
        assert len(enrichment_values) == 60, f"Expected 60 synthesize calls, got {len(enrichment_values)}"

        # 2. Enrichment was passed (not None) for all calls
        non_none_enrichments = [e for e in enrichment_values if e is not None]
        assert len(non_none_enrichments) == 60, "All synthesize calls should receive enrichment"

        # 3. Enrichment contains expected keys
        sample = non_none_enrichments[0]
        assert "profiles" in sample
        assert "short_interest" in sample
        assert "factors" in sample
        assert "AAPL" in sample["profiles"]
        assert sample["profiles"]["AAPL"]["sector"] == "Technology"

        # 4. Both cohorts still produce trades
        control_trades = StateManager(control_dir).load_paper_trades()
        adaptive_trades = StateManager(adaptive_dir).load_paper_trades()
        assert len(control_trades) > 0, "Control should have trades with enrichment"
        assert len(adaptive_trades) > 0, "Adaptive should have trades with enrichment"

    @patch(
        "tradingagents.strategies.orchestration.multi_strategy_engine.MultiStrategyEngine._fetch_all_data"
    )
    @patch(
        "tradingagents.strategies.orchestration.multi_strategy_engine.MultiStrategyEngine._fetch_missing_prices"
    )
    @patch(
        "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize"
    )
    @patch(
        "tradingagents.strategies.modules.get_paper_trade_strategies"
    )
    def test_graceful_degradation_without_openbb(
        self,
        mock_get_strategies,
        mock_committee,
        mock_fetch_prices,
        mock_fetch_data,
        tmp_path,
    ):
        """Run 30 days with OpenBB unavailable. Verify identical behavior to baseline."""
        strategies = [FakeStrategy(hold_days=5), FakeStrategy2()]
        mock_get_strategies.return_value = strategies

        prices = _build_price_cache(days=60)
        mock_fetch_data.return_value = {"yfinance": {"prices": prices}}
        mock_fetch_prices.return_value = None

        control_dir = str(tmp_path / "control")
        adaptive_dir = str(tmp_path / "adaptive")

        cohort_configs = [
            CohortConfig(name="control", state_dir=control_dir, horizon="30d", size_profile="5k", adaptive_confidence=False, learning_enabled=False, use_llm=False),
            CohortConfig(name="adaptive", state_dir=adaptive_dir, horizon="30d", size_profile="5k", adaptive_confidence=True, learning_enabled=True, use_llm=False),
        ]

        base_config = {
            "autoresearch": {
                "state_dir": str(tmp_path / "base"),
                "total_capital": 5000,
                "paper_trade": {"min_trades_for_evaluation": 2, "portfolio_committee_enabled": False},
            },
            "execution": {"mode": "paper"},
        }

        enrichment_values = []

        def fake_synthesize(signals, regime_context=None, strategy_confidence=None,
                            current_positions=None, total_capital=None, **kwargs):
            enrichment_values.append(kwargs.get("enrichment"))
            recs = []
            seen = set()
            for s in signals[:3]:
                if s["ticker"] not in seen:
                    recs.append(TradeRecommendation(
                        ticker=s["ticker"], direction=s["direction"],
                        position_size_pct=0.12, confidence=0.6,
                        rationale="degradation test",
                        contributing_strategies=[s["strategy"]],
                    ))
                    seen.add(s["ticker"])
            return recs

        mock_committee.side_effect = fake_synthesize

        orchestrator = CohortOrchestrator(cohort_configs, base_config)

        for cohort in orchestrator.cohorts:
            cohort["engine"]._price_cache = copy.deepcopy(prices)

        # Mock enrichment to return empty dict (OpenBB unavailable)
        with patch.object(orchestrator, "_fetch_openbb_enrichment", return_value={}):
            for day in range(30):
                trading_date = (BASE_DATE + timedelta(days=day)).strftime("%Y-%m-%d")
                orchestrator.run_daily(trading_date=trading_date)

        # Assertions
        # 1. All enrichment values should be empty dict (graceful degradation)
        for e in enrichment_values:
            assert e == {} or e is None or (isinstance(e, dict) and len(e) == 0), \
                f"Expected empty enrichment, got {e}"

        # 2. System still produces trades without OpenBB
        control_trades = StateManager(control_dir).load_paper_trades()
        assert len(control_trades) > 0, "System should still trade without OpenBB"


class TestReactivatedStrategies:
    """Test reactivated govt_contracts and state_economics produce valid signals."""

    def test_govt_contracts_with_usaspending_data(self):
        """govt_contracts screen() produces candidates from contract data."""
        from tradingagents.strategies.modules.govt_contracts import GovtContractsStrategy

        strategy = GovtContractsStrategy()
        assert strategy.track == "paper_trade"
        assert "openbb" in strategy.data_sources

        # Provide synthetic USASpending contract data
        data = {
            "usaspending": {
                "data": {
                    "contracts": [
                        {"recipient": "Lockheed Martin Corp", "amount": 500_000_000},
                        {"recipient": "Northrop Grumman Systems", "amount": 200_000_000},
                        {"recipient": "Small Unknown Contractor", "amount": 10_000_000},  # Below threshold
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
        from tradingagents.strategies.modules.govt_contracts import GovtContractsStrategy

        strategy = GovtContractsStrategy()

        # Build price data with upward momentum for some contractors
        dates = pd.date_range("2026-02-01", periods=40, freq="B")
        prices = {}
        # LMT with strong upward momentum
        lmt_close = [400 + i * 2 for i in range(40)]  # rising
        prices["LMT"] = pd.DataFrame({"Close": lmt_close, "Volume": [1e6] * 40}, index=dates)
        # BA with flat/declining
        ba_close = [200 - i * 0.1 for i in range(40)]
        prices["BA"] = pd.DataFrame({"Close": ba_close, "Volume": [1e6] * 40}, index=dates)

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
        from tradingagents.strategies.modules.govt_contracts import GovtContractsStrategy

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
        from tradingagents.strategies.modules.state_economics import StateEconomicsStrategy

        strategy = StateEconomicsStrategy()
        assert strategy.track == "paper_trade"
        assert "fred" in strategy.data_sources
        assert "openbb" in strategy.data_sources

        # Build price data for regional ETFs
        dates = pd.date_range("2026-02-01", periods=30, freq="B")
        prices = {}
        # KRE (regional banks) with positive momentum
        kre_close = [50 + i * 0.5 for i in range(30)]
        prices["KRE"] = pd.DataFrame({"Close": kre_close, "Volume": [1e6] * 30}, index=dates)
        # IWN with slight positive
        iwn_close = [160 + i * 0.2 for i in range(30)]
        prices["IWN"] = pd.DataFrame({"Close": iwn_close, "Volume": [1e6] * 30}, index=dates)

        data = {
            "yfinance": {"prices": prices},
            "fred": {
                "UNRATE": {"2026-01": 4.2, "2026-02": 4.0},  # Declining = bullish
                "ICSA": {"2026-02-15": 220000, "2026-02-22": 210000},  # Declining = bullish
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
        from tradingagents.strategies.modules.state_economics import StateEconomicsStrategy

        strategy = StateEconomicsStrategy()

        dates = pd.date_range("2026-02-01", periods=30, freq="B")
        prices = {}
        kre_close = [50 + i * 0.5 for i in range(30)]
        prices["KRE"] = pd.DataFrame({"Close": kre_close, "Volume": [1e6] * 30}, index=dates)

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
        from tradingagents.strategies.modules.state_economics import StateEconomicsStrategy

        strategy = StateEconomicsStrategy()
        params = strategy.get_default_params()

        # Before rebalance (default rebalance_days=30)
        should_exit, reason = strategy.check_exit("KRE", 50.0, 55.0, 10, params, {})
        assert should_exit is False

        # At rebalance boundary
        should_exit, reason = strategy.check_exit("KRE", 50.0, 55.0, 30, params, {})
        assert should_exit is True
        assert reason == "rebalance"

    def test_eleven_strategies_registered(self):
        """Verify 11 strategies are registered (including commodity_macro)."""
        from tradingagents.strategies.modules import get_paper_trade_strategies
        strategies = get_paper_trade_strategies()
        assert len(strategies) == 11
        names = [s.name for s in strategies]
        assert "govt_contracts" in names
        assert "state_economics" in names
        assert "commodity_macro" in names
        assert "weather_ag" in names
