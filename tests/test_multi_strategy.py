"""Tests for the multi-strategy autoresearch system.

Covers: paper-trade strategies, state manager, data sources, engine.
"""

from __future__ import annotations

import tempfile
from unittest.mock import MagicMock

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
            if getattr(s, "retirement_reason", None):
                with pytest.raises(RuntimeError, match=s.retirement_reason):
                    s.build_propose_prompt({"current_params": s.get_default_params()})
                continue
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
        from tradingagents.strategies.data_sources.registry import (
            build_default_registry,
        )

        registry = build_default_registry()
        assert registry.get("yfinance") is not None
        assert "yfinance" in registry.available_sources()

    def test_registry_passes_finnhub_reliability_config(self):
        from tradingagents.strategies.data_sources.registry import (
            build_default_registry,
        )

        reliability = {
            "rate_delay_s": 0.25,
            "workflow_budget_s": 12.0,
        }
        registry = build_default_registry(
            {
                "finnhub_api_key": "not-real",
                "finnhub_reliability": reliability,
            }
        )

        source = registry.get("finnhub")
        assert source is not None
        assert source._rate_delay_s == 0.25
        assert source._workflow_budget_s == 12.0

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
        from tradingagents.strategies.orchestration.multi_strategy_engine import (
            MultiStrategyEngine,
        )
        from tradingagents.strategies.state.state import StateManager
        from tradingagents.strategies.modules import get_all_strategies

        state = StateManager(str(tmp_path / "state"))
        config = {
            "autoresearch": {
                "state_dir": str(tmp_path / "state"),
                "total_capital": 5000,
            }
        }

        return MultiStrategyEngine(
            config=config,
            strategies=get_all_strategies(),
            state_manager=state,
        )

    def test_engine_has_paper_trade_strategies(self, engine):
        assert len(engine.paper_trade_strategies) == 5
        names = {s.name for s in engine.paper_trade_strategies}
        assert "earnings_call" in names
        assert "govt_contracts" in names

    def test_engine_build_regime_model(self, engine):
        data = {
            "yfinance": {
                "vix": pd.DataFrame(
                    {"Close": [20.0]}, index=pd.to_datetime(["2024-06-01"])
                )
            },
            "fred": {},
        }
        regime = engine._build_regime_model(data)
        assert "overall_regime" in regime
        assert "vix_level" in regime
        assert regime["vix_level"] == 20.0

    def test_finnhub_peer_batch_runs_once_and_sibling_news_survives(self, engine):
        source = MagicMock()
        source.new_workflow_deadline.return_value = 42.0
        source.fetch_recent_earnings.return_value = []
        source.fetch_company_news.side_effect = lambda symbol, _from, to, deadline: [
            {
                "headline": f"{symbol} supplier disruption",
                "summary": "Factory delay",
                "source": "wire",
            }
        ]
        source.fetch_supply_chains.return_value = {}
        engine.registry = MagicMock()
        engine.registry.get.return_value = source

        from tradingagents.strategies.orchestration.data_acquisition import (
            DEFAULT_SUPPLY_CHAIN_TICKERS,
        )

        result = engine._fetch_finnhub_data("2026-07-30")

        source.fetch_supply_chains.assert_called_once_with(
            list(DEFAULT_SUPPLY_CHAIN_TICKERS),
            deadline=42.0,
        )
        assert all(
            call.kwargs["deadline"] == 42.0
            for call in source.fetch_company_news.call_args_list
        )
        assert len(result["disruption_news"]) == len(DEFAULT_SUPPLY_CHAIN_TICKERS)
        assert "transcripts" not in result
        assert "supply_chains" not in result


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


class TestPortfolioCommittee:
    def test_empty_signals_returns_empty(self):
        from tradingagents.strategies.trading.portfolio_committee import (
            PortfolioCommittee,
        )

        pc = PortfolioCommittee(
            {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}}
        )
        result = pc.synthesize(signals=[])
        assert result == []

    def test_rule_based_aggregation(self):
        """Two strategies agreeing on same ticker should produce a recommendation."""
        from tradingagents.strategies.trading.portfolio_committee import (
            PortfolioCommittee,
        )

        pc = PortfolioCommittee(
            {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}}
        )
        signals = [
            {
                "ticker": "AAPL",
                "direction": "long",
                "score": 0.8,
                "strategy": "earnings",
            },
            {
                "ticker": "AAPL",
                "direction": "long",
                "score": 0.6,
                "strategy": "insider",
            },
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
        from tradingagents.strategies.trading.portfolio_committee import (
            PortfolioCommittee,
        )

        pc = PortfolioCommittee(
            {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}}
        )

        # Weak event (score 0.5 * conf 0.5 = 0.25) -> filtered
        signals = [
            {
                "ticker": "AAPL",
                "direction": "long",
                "score": 0.5,
                "strategy": "earnings",
            },
        ]
        result = pc.synthesize(signals=signals, strategy_confidence={"earnings": 0.5})
        assert len(result) == 0

        # Strong event (score 2.0 * conf 0.5 = 1.0) -> included
        signals = [
            {
                "ticker": "AAPL",
                "direction": "long",
                "score": 2.0,
                "strategy": "earnings",
            },
        ]
        result = pc.synthesize(signals=signals, strategy_confidence={"earnings": 0.5})
        assert len(result) >= 1

    def test_max_position_enforced(self):
        """Position size should not exceed max_single_position_pct."""
        from tradingagents.strategies.trading.portfolio_committee import (
            PortfolioCommittee,
        )

        pc = PortfolioCommittee(
            {
                "autoresearch": {
                    "paper_trade": {
                        "portfolio_committee_enabled": False,
                        "max_single_position_pct": 0.05,
                    }
                }
            }
        )
        signals = [
            {"ticker": "AAPL", "direction": "long", "score": 5.0, "strategy": "a"},
            {"ticker": "AAPL", "direction": "long", "score": 5.0, "strategy": "b"},
        ]
        result = pc.synthesize(
            signals=signals, strategy_confidence={"a": 1.0, "b": 1.0}
        )
        if result:
            assert result[0].position_size_pct <= 0.05

    def test_regime_misalignment_reduces_confidence(self):
        """Crisis regime + long should get lower confidence than neutral."""
        from tradingagents.strategies.trading.portfolio_committee import (
            PortfolioCommittee,
        )

        pc = PortfolioCommittee(
            {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}}
        )
        signals = [
            {"ticker": "AAPL", "direction": "long", "score": 0.8, "strategy": "a"},
            {"ticker": "AAPL", "direction": "long", "score": 0.8, "strategy": "b"},
        ]
        conf = {"a": 0.8, "b": 0.8}

        normal = pc.synthesize(
            signals=signals,
            strategy_confidence=conf,
            regime_context={"overall_regime": "normal"},
        )
        crisis = pc.synthesize(
            signals=signals,
            strategy_confidence=conf,
            regime_context={"overall_regime": "crisis"},
        )

        if normal and crisis:
            assert crisis[0].confidence < normal[0].confidence

    def test_conflicting_directions_resolved(self):
        """When strategies disagree, majority weighted vote wins."""
        from tradingagents.strategies.trading.portfolio_committee import (
            PortfolioCommittee,
        )

        pc = PortfolioCommittee(
            {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}}
        )
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

    def test_dead_learning_methods_removed(self):
        """Dead learning loop & adaptive confidence methods are deleted in gen_003."""
        from tradingagents.strategies.orchestration.multi_strategy_engine import (
            MultiStrategyEngine,
        )

        for method_name in (
            "run_learning_loop",
            "_compute_strategy_confidence",
            "_read_strategy_outcomes",
            "_should_trigger_learning_loop",
            "set_cycle_tracker",
        ):
            assert not hasattr(MultiStrategyEngine, method_name)


# ---------------------------------------------------------------------------
# Signal Journal tests
# ---------------------------------------------------------------------------


class TestSignalJournal:
    """Tests for the signal journal (append-only JSONL log)."""

    def test_log_and_read_signals(self):
        from tradingagents.strategies.learning.signal_journal import (
            JournalEntry,
            SignalJournal,
        )

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
        from tradingagents.strategies.learning.signal_journal import (
            JournalEntry,
            SignalJournal,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            journal = SignalJournal(tmpdir)

            # 3 strategies agree on AAPL long
            for strat in ["earnings_call", "supply_chain", "insider_activity"]:
                journal.log_signal(
                    JournalEntry(
                        timestamp="2026-03-30",
                        strategy=strat,
                        ticker="AAPL",
                        direction="long",
                        score=0.7,
                        traded=True,
                        entry_price=175.0,
                    )
                )

            # Only 1 strategy on GOOGL
            journal.log_signal(
                JournalEntry(
                    timestamp="2026-03-30",
                    strategy="filing_analysis",
                    ticker="GOOGL",
                    direction="short",
                    score=0.5,
                    traded=False,
                    entry_price=140.0,
                )
            )

            convergence = journal.get_convergence_signals(
                "2026-03-30", min_strategies=2
            )
            assert len(convergence) == 1
            assert convergence[0]["ticker"] == "AAPL"
            assert convergence[0]["count"] == 3

    def test_convergence_empty_journal(self):
        from tradingagents.strategies.learning.signal_journal import SignalJournal

        with tempfile.TemporaryDirectory() as tmpdir:
            journal = SignalJournal(tmpdir)
            assert journal.get_convergence_signals("2026-03-30") == []

    def test_fill_outcomes(self):
        from tradingagents.strategies.learning.signal_journal import (
            JournalEntry,
            SignalJournal,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            journal = SignalJournal(tmpdir)

            # Log a signal from 10 days ago
            journal.log_signal(
                JournalEntry(
                    timestamp="2026-03-20",
                    strategy="earnings_call",
                    ticker="AAPL",
                    direction="long",
                    score=0.7,
                    traded=True,
                    entry_price=170.0,
                )
            )

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
        from tradingagents.strategies.learning.signal_journal import (
            JournalEntry,
            SignalJournal,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            journal = SignalJournal(tmpdir)

            # Signal from 2 days ago — too recent for any outcome
            journal.log_signal(
                JournalEntry(
                    timestamp="2026-03-28",
                    strategy="earnings_call",
                    ticker="AAPL",
                    direction="long",
                    score=0.7,
                    traded=True,
                    entry_price=170.0,
                )
            )

            mock_prices = pd.DataFrame({"Close": [180.0]})
            updated = journal.fill_outcomes({"AAPL": mock_prices}, "2026-03-30")
            assert updated == 0


class TestLLMAnalyzerRegime:
    """Test regime context integration in LLM analyzer."""

    def test_regime_suffix_with_context(self):
        from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer

        suffix = LLMAnalyzer._regime_suffix(
            {
                "overall_regime": "stressed",
                "vix_level": 28.5,
                "vix_regime": "elevated",
                "credit_spread_bps": 450,
                "credit_regime": "widening",
            }
        )
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


# ---------------------------------------------------------------------------
# Signal journal high-conviction failures test
# ---------------------------------------------------------------------------


class TestSignalJournalFailures:
    def test_get_high_conviction_failures(self, tmp_path):
        from tradingagents.strategies.learning.signal_journal import (
            JournalEntry,
            SignalJournal,
        )

        journal = SignalJournal(str(tmp_path))

        # Log some signals with outcomes
        entries = [
            JournalEntry(
                timestamp="2024-06-01",
                strategy="litigation",
                ticker="AAPL",
                direction="short",
                score=0.8,
                llm_conviction=0.9,
                entry_price=100.0,
                return_5d=-0.05,  # Direction-signed loss on the short
            ),
            JournalEntry(
                timestamp="2024-06-02",
                strategy="litigation",
                ticker="MSFT",
                direction="short",
                score=0.7,
                llm_conviction=0.8,
                entry_price=200.0,
                return_5d=0.03,  # Direction-signed profit on the short
            ),
            JournalEntry(
                timestamp="2024-06-03",
                strategy="litigation",
                ticker="GOOGL",
                direction="short",
                score=0.6,
                llm_conviction=0.3,
                entry_price=150.0,
                return_5d=-0.02,  # Direction-signed loss, but low conviction
            ),
        ]
        journal.log_signals(entries)

        failures = journal.get_high_conviction_failures("litigation")
        assert len(failures) == 1
        assert failures[0]["ticker"] == "AAPL"

    def test_prompt_version_field_in_journal(self, tmp_path):
        from tradingagents.strategies.learning.signal_journal import (
            JournalEntry,
            SignalJournal,
        )

        journal = SignalJournal(str(tmp_path))
        entry = JournalEntry(
            timestamp="2024-06-01",
            strategy="test",
            ticker="AAPL",
            direction="long",
            score=0.5,
            prompt_version="abc123def456",
        )
        journal.log_signal(entry)

        entries = journal.get_entries()
        assert len(entries) == 1
        assert entries[0]["prompt_version"] == "abc123def456"


# ---------------------------------------------------------------------------
# Execution bridge tests (no weight parameter)
# ---------------------------------------------------------------------------


class TestExecutionBridgeNoWeights:
    """Test execution bridge without weight parameter."""

    def test_staged_execution_interfaces_have_no_weight_or_direct_price(self):
        import inspect

        from tradingagents.strategies.trading.execution_bridge import ExecutionBridge

        stage = list(inspect.signature(ExecutionBridge.stage_intent).parameters)
        execute = list(inspect.signature(ExecutionBridge.execute_due_intent).parameters)
        assert stage == [
            "self",
            "recommendation",
            "signal_records",
            "marked_account",
            "decision_at",
            "eligible_session",
        ]
        assert execute == [
            "self",
            "intent",
            "opening_bar",
            "marked_account",
            "risk_context",
            "cost_model",
        ]
        assert "current_price" not in stage + execute
        assert "strategy_weight" not in stage + execute


# ---------------------------------------------------------------------------
# Cohort orchestrator tests
# ---------------------------------------------------------------------------


class TestCohortOrchestrator:
    """Test cohort orchestrator initialization and config."""

    def test_build_default_cohorts(self):
        """build_default_cohorts returns 16 cohorts (4 horizons x 4 sizes)."""
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            build_default_cohorts,
        )

        cohorts = build_default_cohorts({"autoresearch": {"state_dir": "data/state"}})
        assert len(cohorts) == 16
        assert cohorts[0].name == "horizon_30d_size_5k"
        # All cohorts use the sole fail-closed production learning policy.
        for c in cohorts:
            assert c.learning_policy.mode == "disabled"

    def test_separate_state_dirs(self):
        """Each cohort gets its own state directory."""
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            build_default_cohorts,
        )

        cohorts = build_default_cohorts({"autoresearch": {"state_dir": "data/state"}})
        dirs = [c.state_dir for c in cohorts]
        assert len(dirs) == len(set(dirs))

    def test_orchestrator_creates_engines(self, tmp_path):
        """Orchestrator creates one engine per cohort."""
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            CohortConfig,
            CohortOrchestrator,
        )

        configs = [
            CohortConfig(
                name="a",
                state_dir=str(tmp_path / "a"),
                horizon="30d",
                size_profile="5k",
                use_llm=False,
            ),
            CohortConfig(
                name="b",
                state_dir=str(tmp_path / "b"),
                horizon="3m",
                size_profile="10k",
                use_llm=False,
            ),
        ]
        orch = CohortOrchestrator(
            configs,
            {"autoresearch": {"state_dir": str(tmp_path)}},
            generation_id="gen_test",
            generation_commit="test-commit",
        )
        assert len(orch.cohorts) == 2
        assert orch.cohorts[0]["config"].name == "a"
        assert orch.cohorts[1]["config"].name == "b"


# ---------------------------------------------------------------------------
# Cohort comparison tests
# ---------------------------------------------------------------------------


class TestCohortComparison:
    """Test the v2 service adapter."""

    def test_compare_delegates_current_report(self):
        from unittest.mock import Mock

        from tradingagents.strategies.metrics.service import MetricsService
        from tradingagents.strategies.orchestration.cohort_comparison import (
            CohortComparison,
        )

        service = Mock(spec=MetricsService)
        service.generation_report.return_value = {"metric_schema_version": 2}
        assert CohortComparison(metrics_service=service).compare() == {
            "metric_schema_version": 2
        }
        service.generation_report.assert_called_once_with()

    def test_heatmap_projects_report_scalar(self):
        from unittest.mock import Mock

        from tradingagents.strategies.metrics.service import MetricsService
        from tradingagents.strategies.orchestration.cohort_comparison import (
            CohortComparison,
        )

        service = Mock(spec=MetricsService)
        service.generation_report.return_value = {
            "headline_books": {"horizon_30d_size_100k": {"total_return": 0.1}},
            "stress_tests": {"horizon_30d_size_5k": {"total_return": -0.1}},
        }
        heatmap = CohortComparison(metrics_service=service).heatmap("total_return")
        assert heatmap["30d"]["100k"] == 0.1
        assert heatmap["30d"]["5k"] == -0.1
        assert heatmap["1y"]["50k"] is None



