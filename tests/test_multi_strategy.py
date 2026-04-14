"""Tests for the multi-strategy autoresearch system.

Covers: paper-trade strategies, state manager, data sources, engine.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Strategy module tests
# ---------------------------------------------------------------------------


class TestStrategyModules:
    """Test that all paper-trade strategy modules conform to the StrategyModule protocol."""

    @pytest.fixture
    def strategies(self):
        from tradingagents.strategies.modules import get_all_strategies
        return get_all_strategies()

    def test_all_strategies_have_required_attributes(self, strategies):
        for s in strategies:
            assert hasattr(s, "name"), f"{s} missing name"
            assert hasattr(s, "track"), f"{s} missing track"
            assert hasattr(s, "data_sources"), f"{s} missing data_sources"
            assert s.track in ("backtest", "paper_trade"), f"{s.name} invalid track"
            assert isinstance(s.data_sources, list), f"{s.name} data_sources not list"

    def test_all_strategies_return_param_space(self, strategies):
        for s in strategies:
            space = s.get_param_space()
            assert isinstance(space, dict), f"{s.name} param_space not dict"
            for key, bounds in space.items():
                assert isinstance(bounds, tuple), f"{s.name}.{key} bounds not tuple"
                assert len(bounds) == 2, f"{s.name}.{key} bounds len != 2"

    def test_all_strategies_return_default_params(self, strategies):
        for s in strategies:
            defaults = s.get_default_params()
            assert isinstance(defaults, dict), f"{s.name} defaults not dict"
            space = s.get_param_space()
            for key in space:
                assert key in defaults, f"{s.name} default missing key {key}"

    def test_screen_returns_candidates(self, strategies):
        """Screen with empty data should return empty list, not crash."""
        for s in strategies:
            result = s.screen({}, "2025-01-15", s.get_default_params())
            assert isinstance(result, list), f"{s.name} screen didn't return list"

    def test_check_exit_returns_tuple(self, strategies):
        for s in strategies:
            should_exit, reason = s.check_exit(
                ticker="SPY",
                entry_price=100.0,
                current_price=95.0,
                holding_days=30,
                params=s.get_default_params(),
                data={},
            )
            assert isinstance(should_exit, bool), f"{s.name} check_exit[0] not bool"
            assert isinstance(reason, str), f"{s.name} check_exit[1] not str"

    def test_build_propose_prompt(self, strategies):
        for s in strategies:
            prompt = s.build_propose_prompt({"current_params": s.get_default_params()})
            assert isinstance(prompt, str), f"{s.name} prompt not str"
            assert len(prompt) > 50, f"{s.name} prompt too short"

    def test_all_strategies_30_day_horizon(self, strategies):
        """All strategies should have hold_days/rebalance_days defaults in 20-30 range."""
        for strategy in strategies:
            params = strategy.get_default_params()
            hold_key = "hold_days" if "hold_days" in params else "rebalance_days"
            hold = params.get(hold_key, 30)
            assert 20 <= hold <= 30, (
                f"{strategy.name}: {hold_key}={hold} outside 20-30 day range"
            )

    def test_all_strategies_param_space_floor(self, strategies):
        """All strategies should have hold_days/rebalance_days floor >= 20 and ceiling <= 45."""
        for strategy in strategies:
            space = strategy.get_param_space()
            hold_key = "hold_days" if "hold_days" in space else "rebalance_days"
            if hold_key in space:
                low, high = space[hold_key]
                assert low >= 20, f"{strategy.name}: {hold_key} floor {low} < 20"
                assert high <= 45, f"{strategy.name}: {hold_key} ceiling {high} > 45"




# ---------------------------------------------------------------------------
# State manager tests
# ---------------------------------------------------------------------------


class TestStateManager:
    @pytest.fixture
    def state(self, tmp_path):
        from tradingagents.strategies.state.state import StateManager
        return StateManager(str(tmp_path / "state"))

    def test_save_load_generation(self, state):
        results = {"scores": {"a": 1.5}, "gen": 1}
        state.save_generation(1, results)
        loaded = state.load_generation(1)
        assert loaded["scores"]["a"] == 1.5

    def test_get_latest_generation(self, state):
        assert state.get_latest_generation() == 0
        state.save_generation(1, {"gen": 1})
        state.save_generation(2, {"gen": 2})
        assert state.get_latest_generation() == 2

    def test_paper_trades(self, state):
        state.save_paper_trade({"strategy": "vix", "ticker": "SPY"})
        state.save_paper_trade({"strategy": "pead", "ticker": "AAPL"})

        all_trades = state.load_paper_trades()
        assert len(all_trades) == 2

        vix_trades = state.load_paper_trades(strategy="vix")
        assert len(vix_trades) == 1
        assert vix_trades[0]["ticker"] == "SPY"
        assert "trade_id" in vix_trades[0]

    def test_update_paper_trade(self, state):
        state.save_paper_trade({"strategy": "test", "ticker": "SPY"})
        trades = state.load_paper_trades()
        trade_id = trades[0]["trade_id"]

        state.update_paper_trade(trade_id, {"status": "closed", "pnl": 50.0})
        updated = state.load_paper_trades()
        assert updated[0]["status"] == "closed"
        assert updated[0]["pnl"] == 50.0

    def test_leaderboard(self, state):
        lb = [{"name": "strat_a", "score": 1.5}]
        state.save_leaderboard(lb)
        loaded = state.load_leaderboard()
        assert loaded[0]["name"] == "strat_a"

    def test_reflections(self, state):
        state.save_reflection(1, {"summary": "gen 1 went well"})
        reflections = state.load_reflections()
        assert len(reflections) == 1
        assert reflections[0]["generation"] == 1

    def test_reset(self, state):
        state.save_paper_trade({"strategy": "test", "ticker": "SPY"})
        state.reset()
        assert state.load_paper_trades() == []


# ---------------------------------------------------------------------------
# Data source registry tests
# ---------------------------------------------------------------------------


class TestDataSourceRegistry:
    def test_build_default_registry(self):
        from tradingagents.strategies.data_sources.registry import build_default_registry

        registry = build_default_registry()
        assert registry.get("yfinance") is not None
        assert "yfinance" in registry.available_sources()

    def test_register_and_get(self):
        from tradingagents.strategies.data_sources.registry import DataSourceRegistry

        registry = DataSourceRegistry()
        mock_source = MagicMock()
        mock_source.name = "test_source"
        mock_source.is_available.return_value = True

        registry.register(mock_source)
        assert registry.get("test_source") is mock_source
        assert "test_source" in registry.available_sources()

    def test_get_missing_returns_none(self):
        from tradingagents.strategies.data_sources.registry import DataSourceRegistry

        registry = DataSourceRegistry()
        assert registry.get("nonexistent") is None


# ---------------------------------------------------------------------------
# Multi-strategy engine tests
# ---------------------------------------------------------------------------


class TestMultiStrategyEngine:
    @pytest.fixture
    def engine(self, tmp_path):
        from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
        from tradingagents.strategies.state.state import StateManager
        from tradingagents.strategies.modules import get_all_strategies

        state = StateManager(str(tmp_path / "state"))
        config = {"autoresearch": {"state_dir": str(tmp_path / "state"), "total_capital": 5000}}

        return MultiStrategyEngine(
            config=config,
            strategies=get_all_strategies(),
            state_manager=state,
        )

    def test_engine_has_paper_trade_strategies(self, engine):
        assert len(engine.paper_trade_strategies) == 12
        names = {s.name for s in engine.paper_trade_strategies}
        assert "earnings_call" in names
        assert "insider_activity" in names

    def test_engine_build_regime_model(self, engine):
        data = {
            "yfinance": {"vix": pd.DataFrame({"Close": [20.0]}, index=pd.to_datetime(["2024-06-01"]))},
            "fred": {},
        }
        regime = engine._build_regime_model(data)
        assert "overall_regime" in regime
        assert "vix_level" in regime
        assert regime["vix_level"] == 20.0


# ---------------------------------------------------------------------------
# New state manager tests (Layer 1 extensions)
# ---------------------------------------------------------------------------


class TestPlaybookState:
    @pytest.fixture
    def state(self, tmp_path):
        from tradingagents.strategies.state.state import StateManager
        return StateManager(str(tmp_path / "state"))

    def test_save_load_playbook(self, state):
        playbook = {"strategies": ["vix", "pead"], "version": 2}
        state.save_playbook(playbook)
        loaded = state.load_playbook()
        assert loaded == playbook

    def test_load_playbook_default_none(self, state):
        assert state.load_playbook() is None


class TestVintageState:
    @pytest.fixture
    def state(self, tmp_path):
        from tradingagents.strategies.state.state import StateManager
        return StateManager(str(tmp_path / "state"))

    def test_save_load_vintage(self, state):
        state.save_vintage({"strategy": "vix", "params": {"threshold": 30}})
        vintages = state.load_vintages()
        assert len(vintages) == 1
        assert "vintage_id" in vintages[0]
        assert "created_at" in vintages[0]
        assert vintages[0]["strategy"] == "vix"

    def test_load_vintages_filter_by_strategy(self, state):
        state.save_vintage({"strategy": "vix", "params": {"threshold": 30}})
        state.save_vintage({"strategy": "pead", "params": {"window": 5}})
        state.save_vintage({"strategy": "vix", "params": {"threshold": 25}})

        vix_vintages = state.load_vintages(strategy="vix")
        assert len(vix_vintages) == 2
        assert all(v["strategy"] == "vix" for v in vix_vintages)

        pead_vintages = state.load_vintages(strategy="pead")
        assert len(pead_vintages) == 1

    def test_update_vintage(self, state):
        state.save_vintage({"strategy": "vix", "completed_trade_count": 0})
        vintages = state.load_vintages()
        vid = vintages[0]["vintage_id"]

        state.update_vintage(vid, {"completed_trade_count": 5, "status": "active"})
        updated = state.load_vintages()
        assert updated[0]["completed_trade_count"] == 5
        assert updated[0]["status"] == "active"


# ---------------------------------------------------------------------------
# Vintage-aware paper trading tests (Layer 2b)
# ---------------------------------------------------------------------------


class TestVintageTracking:
    def test_open_trade_with_vintage(self, tmp_path):
        """Trades opened with vintage_id have it in the record."""
        from tradingagents.strategies.state.state import StateManager
        from tradingagents.strategies.trading.paper_trader import PaperTrader
        state = StateManager(str(tmp_path))
        trader = PaperTrader(state)
        trade_id = trader.open_trade(
            strategy="test_strat", ticker="AAPL", direction="long",
            entry_price=150.0, entry_date="2024-01-01",
            vintage_id="v-001", is_exploration=True,
        )
        trades = state.load_paper_trades()
        trade = [t for t in trades if t.get("trade_id") == trade_id][0]
        assert trade["vintage_id"] == "v-001"
        assert trade["is_exploration"] is True

    def test_vintage_performance_no_trades(self, tmp_path):
        """Vintage with no completed trades returns zero metrics."""
        from tradingagents.strategies.state.state import StateManager
        from tradingagents.strategies.trading.paper_trader import PaperTrader
        state = StateManager(str(tmp_path))
        trader = PaperTrader(state)
        perf = trader.get_vintage_performance("nonexistent")
        assert perf["num_completed"] == 0
        assert perf["sharpe"] == 0.0

    def test_vintage_performance_with_trades(self, tmp_path):
        """Vintage with completed trades returns correct metrics."""
        from tradingagents.strategies.state.state import StateManager
        from tradingagents.strategies.trading.paper_trader import PaperTrader
        state = StateManager(str(tmp_path))
        trader = PaperTrader(state)
        # Open and close two trades
        t1 = trader.open_trade(
            strategy="s", ticker="AAPL", direction="long",
            entry_price=100.0, entry_date="2024-01-01", vintage_id="v-001",
        )
        t2 = trader.open_trade(
            strategy="s", ticker="MSFT", direction="long",
            entry_price=200.0, entry_date="2024-01-01", vintage_id="v-001",
        )
        trader.close_trade(t1, exit_price=110.0, exit_date="2024-02-01", exit_reason="hold_period")
        trader.close_trade(t2, exit_price=190.0, exit_date="2024-02-01", exit_reason="stop_loss")

        perf = trader.get_vintage_performance("v-001")
        assert perf["num_completed"] == 2
        assert perf["win_rate"] == 0.5  # 1 win, 1 loss

    def test_strategy_vintage_summary(self, tmp_path):
        """Strategy summary returns per-vintage breakdown."""
        from tradingagents.strategies.state.state import StateManager
        from tradingagents.strategies.trading.paper_trader import PaperTrader
        state = StateManager(str(tmp_path))
        trader = PaperTrader(state)
        trader.open_trade(
            strategy="s", ticker="AAPL", direction="long",
            entry_price=100.0, entry_date="2024-01-01", vintage_id="v-001",
        )
        trader.open_trade(
            strategy="s", ticker="MSFT", direction="long",
            entry_price=200.0, entry_date="2024-01-01", vintage_id="v-002",
            is_exploration=True,
        )
        summary = trader.get_strategy_vintage_summary("s")
        assert len(summary) == 2
        vintages = {s["vintage_id"] for s in summary}
        assert vintages == {"v-001", "v-002"}


class TestPortfolioCommittee:
    def test_empty_signals_returns_empty(self):
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        pc = PortfolioCommittee({"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}})
        result = pc.synthesize(signals=[])
        assert result == []

    def test_rule_based_aggregation(self):
        """Two strategies agreeing on same ticker should produce a recommendation."""
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        pc = PortfolioCommittee({"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}})
        signals = [
            {"ticker": "AAPL", "direction": "long", "score": 0.8, "strategy": "earnings"},
            {"ticker": "AAPL", "direction": "long", "score": 0.6, "strategy": "insider"},
        ]
        result = pc.synthesize(
            signals=signals,
            strategy_confidence={"earnings": 0.7, "insider": 0.8},
        )
        assert len(result) >= 1
        assert result[0].ticker == "AAPL"
        assert result[0].direction == "long"
        assert len(result[0].contributing_strategies) == 2

    def test_single_strategy_needs_material_event(self):
        """Single strategy signal filtered unless weighted score >= 0.5 (material event)."""
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        pc = PortfolioCommittee({"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}})

        # Weak event (score 0.5 * conf 0.5 = 0.25) -> filtered
        signals = [
            {"ticker": "AAPL", "direction": "long", "score": 0.5, "strategy": "earnings"},
        ]
        result = pc.synthesize(signals=signals, strategy_confidence={"earnings": 0.5})
        assert len(result) == 0

        # Strong event (score 2.0 * conf 0.5 = 1.0) -> included
        signals = [
            {"ticker": "AAPL", "direction": "long", "score": 2.0, "strategy": "earnings"},
        ]
        result = pc.synthesize(signals=signals, strategy_confidence={"earnings": 0.5})
        assert len(result) >= 1

    def test_max_position_enforced(self):
        """Position size should not exceed max_single_position_pct."""
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        pc = PortfolioCommittee({"autoresearch": {"paper_trade": {
            "portfolio_committee_enabled": False,
            "max_single_position_pct": 0.05,
        }}})
        signals = [
            {"ticker": "AAPL", "direction": "long", "score": 5.0, "strategy": "a"},
            {"ticker": "AAPL", "direction": "long", "score": 5.0, "strategy": "b"},
        ]
        result = pc.synthesize(signals=signals, strategy_confidence={"a": 1.0, "b": 1.0})
        if result:
            assert result[0].position_size_pct <= 0.05

    def test_regime_misalignment_reduces_confidence(self):
        """Crisis regime + long should get lower confidence than neutral."""
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        pc = PortfolioCommittee({"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}})
        signals = [
            {"ticker": "AAPL", "direction": "long", "score": 0.8, "strategy": "a"},
            {"ticker": "AAPL", "direction": "long", "score": 0.8, "strategy": "b"},
        ]
        conf = {"a": 0.8, "b": 0.8}

        normal = pc.synthesize(signals=signals, strategy_confidence=conf, regime_context={"overall_regime": "normal"})
        crisis = pc.synthesize(signals=signals, strategy_confidence=conf, regime_context={"overall_regime": "crisis"})

        if normal and crisis:
            assert crisis[0].confidence < normal[0].confidence

    def test_conflicting_directions_resolved(self):
        """When strategies disagree, majority weighted vote wins."""
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        pc = PortfolioCommittee({"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}})
        signals = [
            {"ticker": "AAPL", "direction": "long", "score": 0.9, "strategy": "a"},
            {"ticker": "AAPL", "direction": "short", "score": 0.3, "strategy": "b"},
            {"ticker": "AAPL", "direction": "long", "score": 0.7, "strategy": "c"},
        ]
        result = pc.synthesize(
            signals=signals,
            strategy_confidence={"a": 0.8, "b": 0.8, "c": 0.8},
        )
        if result:
            assert result[0].direction == "long"  # 2 longs vs 1 short


# ---------------------------------------------------------------------------
# Two-phase engine integration tests (Layer 4+5)
# ---------------------------------------------------------------------------


class TestTwoPhaseEngine:
    """Integration tests for the two-phase architecture."""

    def test_learning_loop_not_triggered_without_history(self):
        """Learning loop should not trigger when there's no history."""
        from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
        from tradingagents.strategies.state.state import StateManager
        from tradingagents.strategies.modules import get_all_strategies

        with tempfile.TemporaryDirectory() as tmpdir:
            state = StateManager(tmpdir)
            config = {"autoresearch": {"state_dir": tmpdir, "total_capital": 5000, "paper_trade": {"min_trades_for_evaluation": 20}}}
            engine = MultiStrategyEngine(config=config, strategies=get_all_strategies(), state_manager=state)

            result = engine.run_learning_loop()
            assert result.get("triggered") is False or result.get("strategies_evaluated", 0) == 0


# ---------------------------------------------------------------------------
# Signal Journal tests
# ---------------------------------------------------------------------------


class TestSignalJournal:
    """Tests for the signal journal (append-only JSONL log)."""

    def test_log_and_read_signals(self):
        from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal

        with tempfile.TemporaryDirectory() as tmpdir:
            journal = SignalJournal(tmpdir)

            entries = [
                JournalEntry(
                    timestamp="2026-03-30",
                    strategy="earnings_call",
                    ticker="AAPL",
                    direction="long",
                    score=0.75,
                    llm_conviction=0.8,
                    regime="normal",
                    traded=True,
                    entry_price=175.0,
                ),
                JournalEntry(
                    timestamp="2026-03-30",
                    strategy="supply_chain",
                    ticker="AAPL",
                    direction="long",
                    score=0.6,
                    traded=False,
                    entry_price=175.0,
                ),
                JournalEntry(
                    timestamp="2026-03-30",
                    strategy="filing_analysis",
                    ticker="GOOGL",
                    direction="short",
                    score=0.5,
                    traded=True,
                    entry_price=140.0,
                ),
            ]
            journal.log_signals(entries)

            # Read all
            all_entries = journal.get_entries()
            assert len(all_entries) == 3

            # Filter by strategy
            earnings = journal.get_entries(strategy="earnings_call")
            assert len(earnings) == 1
            assert earnings[0]["ticker"] == "AAPL"

            # Filter by ticker
            aapl = journal.get_entries(ticker="AAPL")
            assert len(aapl) == 2

    def test_convergence_detection(self):
        from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal

        with tempfile.TemporaryDirectory() as tmpdir:
            journal = SignalJournal(tmpdir)

            # 3 strategies agree on AAPL long
            for strat in ["earnings_call", "supply_chain", "insider_activity"]:
                journal.log_signal(JournalEntry(
                    timestamp="2026-03-30",
                    strategy=strat,
                    ticker="AAPL",
                    direction="long",
                    score=0.7,
                    traded=True,
                    entry_price=175.0,
                ))

            # Only 1 strategy on GOOGL
            journal.log_signal(JournalEntry(
                timestamp="2026-03-30",
                strategy="filing_analysis",
                ticker="GOOGL",
                direction="short",
                score=0.5,
                traded=False,
                entry_price=140.0,
            ))

            convergence = journal.get_convergence_signals("2026-03-30", min_strategies=2)
            assert len(convergence) == 1
            assert convergence[0]["ticker"] == "AAPL"
            assert convergence[0]["count"] == 3

    def test_convergence_empty_journal(self):
        from tradingagents.strategies.learning.signal_journal import SignalJournal

        with tempfile.TemporaryDirectory() as tmpdir:
            journal = SignalJournal(tmpdir)
            assert journal.get_convergence_signals("2026-03-30") == []

    def test_fill_outcomes(self):
        from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal

        with tempfile.TemporaryDirectory() as tmpdir:
            journal = SignalJournal(tmpdir)

            # Log a signal from 10 days ago
            journal.log_signal(JournalEntry(
                timestamp="2026-03-20",
                strategy="earnings_call",
                ticker="AAPL",
                direction="long",
                score=0.7,
                traded=True,
                entry_price=170.0,
            ))

            # Mock price cache: AAPL now at 180
            mock_prices = pd.DataFrame({"Close": [180.0]})
            price_cache = {"AAPL": mock_prices}

            updated = journal.fill_outcomes(price_cache, "2026-03-30")
            assert updated == 1

            entries = journal.get_entries()
            assert entries[0]["return_5d"] is not None
            assert entries[0]["return_10d"] is not None
            # 30d not yet elapsed
            assert entries[0]["return_30d"] is None

            # Return should be (180 - 170) / 170 ≈ 0.0588
            assert abs(entries[0]["return_5d"] - 0.058824) < 0.001

    def test_fill_outcomes_no_update_when_recent(self):
        from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal

        with tempfile.TemporaryDirectory() as tmpdir:
            journal = SignalJournal(tmpdir)

            # Signal from 2 days ago — too recent for any outcome
            journal.log_signal(JournalEntry(
                timestamp="2026-03-28",
                strategy="earnings_call",
                ticker="AAPL",
                direction="long",
                score=0.7,
                traded=True,
                entry_price=170.0,
            ))

            mock_prices = pd.DataFrame({"Close": [180.0]})
            updated = journal.fill_outcomes({"AAPL": mock_prices}, "2026-03-30")
            assert updated == 0


class TestLLMAnalyzerRegime:
    """Test regime context integration in LLM analyzer."""

    def test_regime_suffix_with_context(self):
        from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer

        suffix = LLMAnalyzer._regime_suffix({
            "overall_regime": "stressed",
            "vix_level": 28.5,
            "vix_regime": "elevated",
            "credit_spread_bps": 450,
            "credit_regime": "widening",
        })
        assert "stressed" in suffix
        assert "28.5" in suffix
        assert "450" in suffix

    def test_regime_suffix_without_context(self):
        from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer

        assert LLMAnalyzer._regime_suffix(None) == ""
        assert LLMAnalyzer._regime_suffix({}) == ""


# ---------------------------------------------------------------------------
# LLM Analyzer prompt override tests
# ---------------------------------------------------------------------------


class TestLLMAnalyzerPrompts:
    def test_get_default_prompt(self):
        from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer

        analyzer = LLMAnalyzer()
        prompt = analyzer.get_prompt("earnings_call")
        assert "earnings" in prompt.lower()
        assert len(prompt) > 50

    def test_get_prompt_unknown_strategy(self):
        from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer

        analyzer = LLMAnalyzer()
        assert analyzer.get_prompt("nonexistent") == ""

    def test_set_and_get_override(self):
        from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer

        analyzer = LLMAnalyzer()
        original = analyzer.get_prompt("litigation")
        analyzer.set_prompt_override("litigation", "Custom prompt for testing")
        assert analyzer.get_prompt("litigation") == "Custom prompt for testing"

        # Revert
        analyzer.set_prompt_override("litigation", "")
        assert analyzer.get_prompt("litigation") == original


# ---------------------------------------------------------------------------
# Prompt optimizer tests
# ---------------------------------------------------------------------------


class TestPromptOptimizer:
    @pytest.fixture
    def optimizer_setup(self, tmp_path):
        from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
        from tradingagents.strategies.learning.prompt_optimizer import PromptOptimizer

        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)
        return optimizer, analyzer, tmp_path

    def test_evaluate_prompts_insufficient_data(self, optimizer_setup):
        """With no journal data, all strategies should have 0 signals."""
        from tradingagents.strategies.learning.signal_journal import SignalJournal

        optimizer, _, tmp_path = optimizer_setup
        journal = SignalJournal(str(tmp_path))
        scores = optimizer.evaluate_prompts(journal)

        for strategy, score in scores.items():
            assert score["n_signals"] == 0
            assert score["hit_rate"] == 0.0

    def test_identify_worst_prompt_no_data(self, optimizer_setup):
        """With no eligible strategies, should return None."""
        optimizer, _, _ = optimizer_setup
        scores = {
            "earnings_call": {"hit_rate": 0.0, "n_signals": 5},
            "litigation": {"hit_rate": 0.0, "n_signals": 3},
        }
        assert optimizer.identify_worst_prompt(scores) is None

    def test_identify_worst_prompt_with_data(self, optimizer_setup):
        optimizer, _, _ = optimizer_setup
        scores = {
            "earnings_call": {"hit_rate": 0.7, "n_signals": 25},
            "litigation": {"hit_rate": 0.4, "n_signals": 25},
            "supply_chain": {"hit_rate": 0.6, "n_signals": 25},
        }
        assert optimizer.identify_worst_prompt(scores) == "litigation"

    def test_start_trial_creates_files(self, optimizer_setup):
        optimizer, analyzer, tmp_path = optimizer_setup
        trial_id = optimizer.start_trial("litigation", "New test prompt")

        assert trial_id.startswith("litigation_")
        assert (tmp_path / "prompts" / "litigation_trial.txt").exists()
        assert (tmp_path / "prompts" / "litigation_baseline.txt").exists()
        assert analyzer.get_prompt("litigation") == "New test prompt"

    def test_get_active_trial(self, optimizer_setup):
        optimizer, _, _ = optimizer_setup
        assert optimizer.get_active_trial() == (None, None)

        optimizer.start_trial("litigation", "New test prompt")
        trial_id, trial = optimizer.get_active_trial()
        assert trial_id is not None
        assert trial["strategy"] == "litigation"
        assert trial["status"] == "active"

    def test_commit_revert_restores_baseline(self, optimizer_setup):
        from tradingagents.strategies.learning.llm_analyzer import _DEFAULT_PROMPTS

        optimizer, analyzer, _ = optimizer_setup
        original = analyzer.get_prompt("litigation")
        trial_id = optimizer.start_trial("litigation", "Modified prompt")
        assert analyzer.get_prompt("litigation") == "Modified prompt"

        optimizer.commit_or_revert(trial_id, "revert")
        assert analyzer.get_prompt("litigation") == original

    def test_prompt_version_hash(self, optimizer_setup):
        optimizer, _, _ = optimizer_setup
        v1 = optimizer.get_prompt_version("earnings_call")
        assert len(v1) == 12
        # Same prompt -> same hash
        v2 = optimizer.get_prompt_version("earnings_call")
        assert v1 == v2


# ---------------------------------------------------------------------------
# Signal journal high-conviction failures test
# ---------------------------------------------------------------------------


class TestSignalJournalFailures:
    def test_get_high_conviction_failures(self, tmp_path):
        from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal

        journal = SignalJournal(str(tmp_path))

        # Log some signals with outcomes
        entries = [
            JournalEntry(
                timestamp="2024-06-01", strategy="litigation", ticker="AAPL",
                direction="short", score=0.8, llm_conviction=0.9,
                entry_price=100.0, return_5d=0.05,  # Wrong! Short but price went up
            ),
            JournalEntry(
                timestamp="2024-06-02", strategy="litigation", ticker="MSFT",
                direction="short", score=0.7, llm_conviction=0.8,
                entry_price=200.0, return_5d=-0.03,  # Correct
            ),
            JournalEntry(
                timestamp="2024-06-03", strategy="litigation", ticker="GOOGL",
                direction="short", score=0.6, llm_conviction=0.3,
                entry_price=150.0, return_5d=0.02,  # Wrong but low conviction
            ),
        ]
        journal.log_signals(entries)

        failures = journal.get_high_conviction_failures("litigation")
        assert len(failures) == 1
        assert failures[0]["ticker"] == "AAPL"

    def test_prompt_version_field_in_journal(self, tmp_path):
        from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal

        journal = SignalJournal(str(tmp_path))
        entry = JournalEntry(
            timestamp="2024-06-01", strategy="test", ticker="AAPL",
            direction="long", score=0.5, prompt_version="abc123def456",
        )
        journal.log_signal(entry)

        entries = journal.get_entries()
        assert len(entries) == 1
        assert entries[0]["prompt_version"] == "abc123def456"


# ---------------------------------------------------------------------------
# Strategy confidence tests
# ---------------------------------------------------------------------------


class TestStrategyConfidence:
    """Test journal-derived strategy confidence."""

    def test_insufficient_data_returns_neutral(self, tmp_path):
        """With < 10 outcomes, confidence = 0.5."""
        from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
        from tradingagents.strategies.modules import get_paper_trade_strategies

        engine = MultiStrategyEngine(
            config={"autoresearch": {"state_dir": str(tmp_path)}},
            strategies=get_paper_trade_strategies(),
        )
        assert engine._compute_strategy_confidence("earnings_call") == 0.5

    def test_all_hits_returns_high_confidence(self, tmp_path):
        """100% hit rate -> 0.9 confidence (capped)."""
        from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
        from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal
        from tradingagents.strategies.modules import get_paper_trade_strategies

        journal = SignalJournal(str(tmp_path))
        for i in range(15):
            journal.log_signal(JournalEntry(
                timestamp=f"2026-01-{i+1:02d}",
                strategy="earnings_call",
                ticker="AAPL",
                direction="long",
                score=0.8,
                return_5d=0.05,  # positive = correct for long
            ))
        engine = MultiStrategyEngine(
            config={"autoresearch": {"state_dir": str(tmp_path)}},
            strategies=get_paper_trade_strategies(),
        )
        conf = engine._compute_strategy_confidence("earnings_call")
        assert conf == pytest.approx(0.9)

    def test_all_misses_returns_low_confidence(self, tmp_path):
        """0% hit rate -> 0.2 confidence (floored)."""
        from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
        from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal
        from tradingagents.strategies.modules import get_paper_trade_strategies

        journal = SignalJournal(str(tmp_path))
        for i in range(15):
            journal.log_signal(JournalEntry(
                timestamp=f"2026-01-{i+1:02d}",
                strategy="earnings_call",
                ticker="AAPL",
                direction="long",
                score=0.8,
                return_5d=-0.05,  # negative = wrong for long
            ))
        engine = MultiStrategyEngine(
            config={"autoresearch": {"state_dir": str(tmp_path)}},
            strategies=get_paper_trade_strategies(),
        )
        conf = engine._compute_strategy_confidence("earnings_call")
        assert conf == pytest.approx(0.2)

    def test_50pct_hit_rate_maps_correctly(self, tmp_path):
        """50% hit rate -> should be about 0.55 confidence."""
        from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
        from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal
        from tradingagents.strategies.modules import get_paper_trade_strategies

        journal = SignalJournal(str(tmp_path))
        for i in range(20):
            journal.log_signal(JournalEntry(
                timestamp=f"2026-01-{i+1:02d}",
                strategy="earnings_call",
                ticker="AAPL",
                direction="long",
                score=0.8,
                return_5d=0.05 if i < 10 else -0.05,
            ))
        engine = MultiStrategyEngine(
            config={"autoresearch": {"state_dir": str(tmp_path)}},
            strategies=get_paper_trade_strategies(),
        )
        conf = engine._compute_strategy_confidence("earnings_call")
        # 50% hit rate: (0.5 - 0.3) / 0.4 * 0.7 + 0.2 = 0.55
        assert conf == pytest.approx(0.55)

    def test_adaptive_flag_uses_journal(self, tmp_path):
        """Engine with adaptive_confidence=True uses journal."""
        from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
        from tradingagents.strategies.modules import get_paper_trade_strategies

        engine = MultiStrategyEngine(
            config={"autoresearch": {"state_dir": str(tmp_path)}},
            strategies=get_paper_trade_strategies(),
            adaptive_confidence=True,
        )
        assert engine._adaptive_confidence is True

    def test_non_adaptive_flag_uses_fixed(self, tmp_path):
        """Engine with adaptive_confidence=False uses 0.5."""
        from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
        from tradingagents.strategies.modules import get_paper_trade_strategies

        engine = MultiStrategyEngine(
            config={"autoresearch": {"state_dir": str(tmp_path)}},
            strategies=get_paper_trade_strategies(),
            adaptive_confidence=False,
        )
        assert engine._adaptive_confidence is False


# ---------------------------------------------------------------------------
# Risk gate position sizing tests (no weight parameter)
# ---------------------------------------------------------------------------


class TestRiskGateNoWeights:
    """Test risk gate position sizing without weight scaling."""

    def test_basic_sizing(self):
        """Position size from committee pct, no weight."""
        from tradingagents.strategies.trading.risk_gate import RiskGate, RiskGateConfig
        from tradingagents.execution.paper_broker import PaperBroker

        broker = PaperBroker(initial_capital=5000.0)
        gate = RiskGate(RiskGateConfig(), broker)
        # 5% of $5000 = $250, at $50/share = 5 shares
        shares = gate.compute_position_size(0.05, 50.0)
        assert shares == 5

    def test_caps_at_max_position_pct(self):
        """Position capped at 15% of portfolio."""
        from tradingagents.strategies.trading.risk_gate import RiskGate, RiskGateConfig
        from tradingagents.execution.paper_broker import PaperBroker

        broker = PaperBroker(initial_capital=5000.0)
        gate = RiskGate(RiskGateConfig(), broker)
        # 50% request should be capped at 15% = $750, at $50 = 15 shares
        shares = gate.compute_position_size(0.50, 50.0)
        assert shares == 15

    def test_zero_price_returns_zero(self):
        from tradingagents.strategies.trading.risk_gate import RiskGate, RiskGateConfig
        from tradingagents.execution.paper_broker import PaperBroker

        broker = PaperBroker(initial_capital=5000.0)
        gate = RiskGate(RiskGateConfig(), broker)
        assert gate.compute_position_size(0.05, 0.0) == 0

    def test_min_position_enforced(self):
        """Tiny positions below $100 min are rejected."""
        from tradingagents.strategies.trading.risk_gate import RiskGate, RiskGateConfig
        from tradingagents.execution.paper_broker import PaperBroker

        broker = PaperBroker(initial_capital=5000.0)
        config = RiskGateConfig(min_position_value=1000.0, max_position_pct=0.01)
        gate = RiskGate(config, broker)
        # 1% of $5000 = $50. Floor wants $1000 but max_position_pct cap = $50.
        # Floor exceeds cap → rejected.
        shares = gate.compute_position_size(0.01, 60.0)
        assert shares == 0


# ---------------------------------------------------------------------------
# Execution bridge tests (no weight parameter)
# ---------------------------------------------------------------------------


class TestExecutionBridgeNoWeights:
    """Test execution bridge without weight parameter."""

    def test_execute_without_weight(self):
        """execute_recommendation no longer takes strategy_weight."""
        import inspect

        from tradingagents.strategies.trading.execution_bridge import ExecutionBridge

        sig = inspect.signature(ExecutionBridge.execute_recommendation)
        params = list(sig.parameters.keys())
        assert "strategy_weight" not in params
        # Verify expected params are present
        assert "position_size_pct" in params
        assert "current_price" in params
        assert "strategy" in params


# ---------------------------------------------------------------------------
# Cohort orchestrator tests
# ---------------------------------------------------------------------------


class TestCohortOrchestrator:
    """Test cohort orchestrator initialization and config."""

    def test_build_default_cohorts(self):
        """build_default_cohorts returns 16 cohorts (4 horizons x 4 sizes)."""
        from tradingagents.strategies.orchestration.cohort_orchestrator import build_default_cohorts

        cohorts = build_default_cohorts({"autoresearch": {"state_dir": "data/state"}})
        assert len(cohorts) == 16
        assert cohorts[0].name == "horizon_30d_size_5k"
        # All cohorts have adaptive/learning dormant
        for c in cohorts:
            assert c.adaptive_confidence is False
            assert c.learning_enabled is False

    def test_separate_state_dirs(self):
        """Each cohort gets its own state directory."""
        from tradingagents.strategies.orchestration.cohort_orchestrator import build_default_cohorts

        cohorts = build_default_cohorts({"autoresearch": {"state_dir": "data/state"}})
        dirs = [c.state_dir for c in cohorts]
        assert len(dirs) == len(set(dirs))

    def test_orchestrator_creates_engines(self, tmp_path):
        """Orchestrator creates one engine per cohort."""
        from tradingagents.strategies.orchestration.cohort_orchestrator import CohortConfig, CohortOrchestrator

        configs = [
            CohortConfig(name="a", state_dir=str(tmp_path / "a"), horizon="30d", size_profile="5k", use_llm=False),
            CohortConfig(name="b", state_dir=str(tmp_path / "b"), horizon="3m", size_profile="10k", use_llm=False),
        ]
        orch = CohortOrchestrator(configs, {"autoresearch": {"state_dir": str(tmp_path)}})
        assert len(orch.cohorts) == 2
        assert orch.cohorts[0]["config"].name == "a"
        assert orch.cohorts[1]["config"].name == "b"


# ---------------------------------------------------------------------------
# Cohort comparison tests
# ---------------------------------------------------------------------------


class TestCohortComparison:
    """Test cross-cohort comparison."""

    def test_empty_comparison(self, tmp_path):
        """Comparison with empty journals returns zeros."""
        from tradingagents.strategies.orchestration.cohort_comparison import CohortComparison

        dirs = {
            "control": str(tmp_path / "control"),
            "adaptive": str(tmp_path / "adaptive"),
        }
        # Create dirs
        (tmp_path / "control").mkdir()
        (tmp_path / "adaptive").mkdir()
        comp = CohortComparison(dirs)
        result = comp.compare()
        assert "cohorts" in result
        for name in ["control", "adaptive"]:
            assert result["cohorts"][name]["total_signals"] == 0

    def test_format_report_returns_string(self, tmp_path):
        """format_report returns a non-empty string."""
        from tradingagents.strategies.orchestration.cohort_comparison import CohortComparison

        dirs = {
            "control": str(tmp_path / "control"),
            "adaptive": str(tmp_path / "adaptive"),
        }
        (tmp_path / "control").mkdir()
        (tmp_path / "adaptive").mkdir()
        comp = CohortComparison(dirs)
        report = comp.format_report()
        assert isinstance(report, str)
        assert "Cohort Comparison" in report or "comparison" in report.lower() or len(report) > 0


# ---------------------------------------------------------------------------
# EDGAR data flow tests
# ---------------------------------------------------------------------------


class TestEdgarDataFlow:
    """Test EDGAR data quality fixes: Form 4 prompt and prior filing text."""

    def test_form4_prompt_includes_field_descriptions(self):
        """LLM prompt for Form 4 analysis includes transaction_code explanations."""
        from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer

        analyzer = LLMAnalyzer.__new__(LLMAnalyzer)
        # Mock _call_llm to capture the prompt
        calls = []
        analyzer._call_llm = lambda system, user: (calls.append((system, user)), '{"direction": "neutral"}')[1]
        analyzer._regime_suffix = lambda ctx: ""

        analyzer.analyze_insider_context(
            [{"transaction_type": "buy", "transaction_code": "P", "shares": 1000,
              "price_per_share": 50.0, "owner_name": "Jane CEO", "is_officer": True}],
            "AAPL",
        )
        assert len(calls) == 1
        system_prompt = calls[0][0]
        # Verify the prompt explains transaction codes
        assert "transaction_code" in system_prompt
        assert '"P" = open-market purchase' in system_prompt
        assert '"A" = grant/award' in system_prompt
        assert '"F" = tax withholding' in system_prompt
        # Verify the filing data is in the user prompt
        user_prompt = calls[0][1]
        assert "Jane CEO" in user_prompt
        assert "1000" in user_prompt

    def test_event_monitor_fetches_prior_text(self):
        """poll_edgar_filings fetches prior filing text for 10-K/10-Q."""
        from tradingagents.strategies.learning.event_monitor import EventMonitor

        mock_source = MagicMock()
        mock_source.is_available.return_value = True
        mock_source.search_filings.return_value = [{
            "form_type": "10-K",
            "file_url": "https://sec.gov/current.htm",
            "file_date": "2026-03-15",
            "ticker": "AAPL",
            "ciks": ["320193"],
        }]
        # Current filing text
        mock_source.get_filing_text.side_effect = [
            "<html>Current 10-K content</html>",
            "<html>Prior 10-K content</html>",
        ]
        # Prior filing lookup
        mock_source.get_company_filings.return_value = [
            {"filing_date": "2025-03-15", "accession_number": "0001-23-456",
             "primary_document": "prior.htm"},
        ]

        registry = MagicMock()
        registry.get.return_value = mock_source

        monitor = EventMonitor(registry)
        filings = monitor.poll_edgar_filings(
            form_types=["10-K"], days_back=7, max_text_fetches=5,
        )

        assert len(filings) == 1
        assert filings[0]["current_text"]
        assert "Current 10-K content" in filings[0]["current_text"]
        assert filings[0]["prior_text"]
        assert "Prior 10-K content" in filings[0]["prior_text"]

    def test_event_monitor_no_prior_when_no_cik(self):
        """No prior text fetched when filing has no CIK."""
        from tradingagents.strategies.learning.event_monitor import EventMonitor

        mock_source = MagicMock()
        mock_source.is_available.return_value = True
        mock_source.search_filings.return_value = [{
            "form_type": "10-Q",
            "file_url": "https://sec.gov/current.htm",
            "file_date": "2026-03-15",
            "ticker": "AAPL",
            "ciks": [],  # No CIK
        }]
        mock_source.get_filing_text.return_value = "<html>Current content</html>"

        registry = MagicMock()
        registry.get.return_value = mock_source

        monitor = EventMonitor(registry)
        filings = monitor.poll_edgar_filings(form_types=["10-Q"], days_back=7)

        assert len(filings) == 1
        assert filings[0]["current_text"]
        assert filings[0]["prior_text"] == ""
        # Should NOT call get_company_filings
        mock_source.get_company_filings.assert_not_called()

    def test_event_monitor_proxy_no_prior_text(self):
        """DEF 14A filings get proxy_text but no prior_text."""
        from tradingagents.strategies.learning.event_monitor import EventMonitor

        mock_source = MagicMock()
        mock_source.is_available.return_value = True
        mock_source.search_filings.return_value = [{
            "form_type": "DEF 14A",
            "file_url": "https://sec.gov/proxy.htm",
            "file_date": "2026-03-15",
            "ticker": "AAPL",
            "ciks": ["320193"],
        }]
        mock_source.get_filing_text.return_value = "<html>Proxy content</html>"

        registry = MagicMock()
        registry.get.return_value = mock_source

        monitor = EventMonitor(registry)
        filings = monitor.poll_edgar_filings(form_types=["DEF 14A"], days_back=7)

        assert len(filings) == 1
        assert "proxy_text" in filings[0]
        assert "prior_text" not in filings[0]  # Only 10-K/10-Q get prior_text
