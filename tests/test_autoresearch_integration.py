"""End-to-end integration tests for the autoresearch system."""

import os
import json
import tempfile
import pytest
from unittest.mock import patch, MagicMock
from copy import deepcopy

from tradingagents.storage.db import Database
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.autoresearch.models import (
    Strategy, BacktestResults, ScreenerResult, ScreenerCriteria,
)
from tradingagents.autoresearch.evolution import EvolutionEngine
from tradingagents.autoresearch.cached_pipeline import CachedPipelineRunner
from tradingagents.autoresearch.strategist import Strategist
from tradingagents.autoresearch.screener import MarketScreener
from tradingagents.autoresearch.fitness import (
    compute_fitness, rank_strategies, meets_paper_criteria,
    update_analyst_weights,
)
from tradingagents.autoresearch.walk_forward import generate_windows


def _make_config():
    config = deepcopy(DEFAULT_CONFIG)
    config["llm_provider"] = "anthropic"
    config["autoresearch"] = {
        "max_generations": 2,
        "strategies_per_generation": 2,
        "tickers_per_strategy": 2,
        "walk_forward_windows": 2,
        "holdout_weeks": 4,
        "min_trades_for_scoring": 1,
        "cache_model": "claude-haiku-4-5-20251001",
        "live_model": "claude-sonnet-4-20250514",
        "strategist_model": "claude-sonnet-4-20250514",
        "cro_model": "claude-haiku-4-5-20251001",
        "fitness_min_sharpe": 0.5,
        "fitness_min_win_rate": 0.40,
        "fitness_min_trades": 2,
        "paper_min_trades": 2,
        "paper_max_divergence_pct": 20,
        "analyst_weight_min": 0.3,
        "analyst_weight_max": 2.5,
        "complexity_penalty_factor": 0.1,
        "stop_unchanged_generations": 3,
        "universe": "sp500_nasdaq100",
        "budget_cap_usd": 150.0,
    }
    return config


def _make_screener_result(ticker="AAPL", regime="RISK_ON"):
    return ScreenerResult(
        ticker=ticker, close=150.0, change_14d=0.05, change_30d=0.10,
        high_52w=180.0, low_52w=120.0, avg_volume_20d=50_000_000,
        volume_ratio=1.2, rsi_14=55.0, ema_10=148.0, ema_50=145.0,
        macd=2.5, boll_position=0.6, iv_rank=0.4, put_call_ratio=0.8,
        options_volume=100_000, market_cap=2.5e12, sector="Technology",
        revenue_growth_yoy=0.15, next_earnings_date="2024-04-25",
        regime=regime, trading_day_coverage=0.95,
    )


def _make_strategy_llm_response(names=None):
    """Create a mock LLM response with strategy proposals."""
    names = names or ["momentum_gen0", "mean_rev_gen0"]
    strategies = []
    for name in names:
        strategies.append({
            "name": name,
            "hypothesis": f"Test hypothesis for {name}",
            "instrument": "stock_long",
            "entry_rules": ["RSI_14 crosses above 30", "price > EMA_10"],
            "exit_rules": ["50% profit target", "25% stop loss"],
            "position_size_pct": 0.05,
            "max_risk_pct": 0.05,
            "time_horizon_days": 30,
            "conviction": 75,
            "parent_ids": [],
            "screener_criteria": {"market_cap_range": [0, 1e15], "min_avg_volume": 100000},
        })
    return json.dumps(strategies)


def _make_cro_response(approved=True):
    return json.dumps({
        "approved": approved,
        "reason": "Approved" if approved else "Rejected",
        "risk_score": 3,
        "concerns": [],
    })


def _make_reflection_response():
    return json.dumps({
        "patterns_that_work": ["momentum in tech"],
        "patterns_that_fail": ["mean reversion in low vol"],
        "next_generation_guidance": ["try breakouts"],
        "regime_notes": "RISK_ON favors momentum",
    })


def _make_pipeline_result(rating="BUY"):
    return {
        "rating": rating,
        "market_report": "Market looks good",
        "sentiment_report": "Positive",
        "news_report": "Good news",
        "fundamentals_report": "Strong",
        "options_report": "Normal IV",
        "full_decision": f"{rating} with conviction",
        "debate_summary": "Bulls win",
        "analyst_scores": {"market": 1, "news": 1, "sentiment": 0, "fundamentals": 1, "options": 0},
    }


class TestFullEvolutionLoop:
    """Test the full evolution loop with 2 generations, all mocked."""

    @patch.object(CachedPipelineRunner, "run")
    @patch.object(Strategist, "_call_llm")
    @patch.object(MarketScreener, "run")
    @patch("tradingagents.autoresearch.evolution.get_universe")
    def test_two_generations(self, mock_universe, mock_screener_run,
                              mock_llm, mock_pipeline):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)
            config = _make_config()

            mock_universe.return_value = ["AAPL", "MSFT"]
            mock_screener_run.return_value = [
                _make_screener_result("AAPL"),
                _make_screener_result("MSFT"),
            ]

            # LLM calls: gen0 propose, gen0 CRO x2, gen0 reflect,
            #             gen1 propose, gen1 CRO x2, gen1 reflect
            mock_llm.side_effect = [
                _make_strategy_llm_response(["strat_g0_a", "strat_g0_b"]),
                _make_cro_response(True),
                _make_cro_response(True),
                _make_reflection_response(),
                _make_strategy_llm_response(["strat_g1_a", "strat_g1_b"]),
                _make_cro_response(True),
                _make_cro_response(True),
                _make_reflection_response(),
            ]

            # Need positive mean return AND variance AND at least one loser
            # for fitness > 0 (sharpe > 0, profit_factor > 0).
            # 2 tickers × 2 windows = 4 trades: 3 BUY + 1 SELL
            _pipeline_calls = {"n": 0}
            def _pipeline_side_effect(ticker, date, tier, screener_result=None):
                _pipeline_calls["n"] += 1
                # Make 3 out of 4 calls return BUY, 1 returns SELL
                if _pipeline_calls["n"] % 4 == 0:
                    return _make_pipeline_result("SELL")
                return _make_pipeline_result("BUY")
            mock_pipeline.side_effect = _pipeline_side_effect

            with patch.object(MarketScreener, "apply_filters", return_value=True):
                engine = EvolutionEngine(db, config)
                result = engine.run("2023-01-01", "2024-01-01")

            assert result["generations_run"] == 2
            assert "leaderboard" in result

            # Verify DB was populated — strategies with fitness > 0
            # get promoted to "backtested" and appear in top list
            strategies = db.get_top_strategies(limit=10)
            assert len(strategies) > 0

            reflections = db.get_reflections()
            assert len(reflections) == 2

            db.close()


class TestStrategyLifecycle:
    """Test strategy status transitions."""

    def test_proposed_to_backtested(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            # Insert a proposed strategy
            sid = db.insert_strategy(
                generation=0, parent_ids=[], name="lifecycle_test",
                hypothesis="test", conviction=75,
                screener_criteria={"market_cap_range": [0, 1e15]},
                instrument="stock_long", entry_rules=["RSI > 30"],
                exit_rules=["25% stop loss"], position_size_pct=0.05,
                max_risk_pct=0.05, time_horizon_days=30,
                regime_born="RISK_ON", status="proposed",
            )

            strat = db.get_strategy(sid)
            assert strat["status"] == "proposed"

            # Simulate backtesting → update status
            db.update_strategy_status(sid, "backtested")
            db.update_strategy_fitness(sid, 1.5)

            strat = db.get_strategy(sid)
            assert strat["status"] == "backtested"
            assert strat["fitness_score"] == 1.5

            # Insert backtest results
            db.insert_strategy_backtest(
                strategy_id=sid, sharpe=1.5, total_return=0.15,
                max_drawdown=-0.05, win_rate=0.65, profit_factor=2.0,
                num_trades=20, tickers_tested=["AAPL", "MSFT"],
                backtest_period="2023-01-01 to 2023-12-01",
                walk_forward_scores=[1.2, 1.4, 1.6],
            )

            backtest = db.get_strategy_backtest(sid)
            assert backtest is not None
            assert backtest["sharpe"] == 1.5

            # Promote to paper
            db.update_strategy_status(sid, "paper")
            strat = db.get_strategy(sid)
            assert strat["status"] == "paper"

            # Record paper trades
            db.insert_strategy_trade(
                strategy_id=sid, ticker="AAPL", trade_type="paper",
                entry_date="2024-01-05", exit_date="2024-01-20",
                instrument="stock_long", entry_price=150.0,
                exit_price=155.0, quantity=10, pnl=50.0,
                pnl_pct=0.033, holding_days=15, regime="RISK_ON",
            )

            trades = db.get_strategy_trades(sid, trade_type="paper")
            assert len(trades) == 1

            # Promote to ready
            db.update_strategy_status(sid, "ready")
            strat = db.get_strategy(sid)
            assert strat["status"] == "ready"

            db.close()

    def test_strategy_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            sid = db.insert_strategy(
                generation=0, parent_ids=[], name="fail_test",
                hypothesis="test", conviction=50,
                screener_criteria={}, instrument="stock_long",
                entry_rules=[], exit_rules=[],
                position_size_pct=0.05, max_risk_pct=0.05,
                time_horizon_days=30, regime_born="CRISIS",
                status="paper",
            )

            db.update_strategy_status(sid, "failed")
            strat = db.get_strategy(sid)
            assert strat["status"] == "failed"
            db.close()


class TestCacheEconomics:
    """Test that cache hit rate improves across generations."""

    def test_cache_hit_rate_improves(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            # Simulate pipeline cache behavior
            runner = CachedPipelineRunner(db, _make_config())

            # Gen 0: all misses
            with patch("tradingagents.graph.trading_graph.TradingAgentsGraph") as MockGraph:
                mock_instance = MagicMock()
                mock_instance.propagate.return_value = (
                    {
                        "market_report": "test",
                        "sentiment_report": "test",
                        "news_report": "test",
                        "fundamentals_report": "test",
                        "options_report": "test",
                        "final_trade_decision": "BUY",
                        "investment_debate_state": {"judge_decision": "BUY"},
                        "risk_debate_state": {"judge_decision": "OK"},
                    },
                    "BUY",
                )
                MockGraph.return_value = mock_instance

                # First run: cache miss
                runner.run("AAPL", "2024-01-15", "haiku")
                stats_gen0 = runner.get_cache_stats()
                assert stats_gen0["misses"] == 1
                assert stats_gen0["hits"] == 0

                # Second run: same ticker/date → cache hit
                runner.run("AAPL", "2024-01-15", "haiku")
                stats_gen1 = runner.get_cache_stats()
                assert stats_gen1["hits"] == 1
                assert stats_gen1["hit_rate"] == 0.5

                # Third run: same → another hit
                runner.run("AAPL", "2024-01-15", "haiku")
                stats_gen2 = runner.get_cache_stats()
                assert stats_gen2["hits"] == 2
                assert stats_gen2["hit_rate"] > stats_gen1["hit_rate"]

            db.close()


class TestPaperTradingLoop:
    """Test the paper trading flow."""

    def test_paper_trade_recorded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            # Create a strategy in PAPER status
            sid = db.insert_strategy(
                generation=0, parent_ids=[], name="paper_test",
                hypothesis="test", conviction=75,
                screener_criteria={"market_cap_range": [0, 1e15]},
                instrument="stock_long", entry_rules=["RSI > 30"],
                exit_rules=["25% stop loss"], position_size_pct=0.05,
                max_risk_pct=0.05, time_horizon_days=30,
                regime_born="RISK_ON", status="paper",
            )

            # Add backtest results
            db.insert_strategy_backtest(
                strategy_id=sid, sharpe=1.5, total_return=0.10,
                max_drawdown=-0.05, win_rate=0.60, profit_factor=2.0,
                num_trades=10, tickers_tested=["AAPL"],
                backtest_period="2023-01-01 to 2023-12-01",
                walk_forward_scores=[1.2, 1.5],
            )

            # Record paper trades
            for i in range(3):
                pnl = 10.0 if i < 2 else -5.0
                db.insert_strategy_trade(
                    strategy_id=sid, ticker="AAPL", trade_type="paper",
                    entry_date=f"2024-0{i+1}-01", exit_date=f"2024-0{i+1}-15",
                    instrument="stock_long", entry_price=150.0,
                    exit_price=150.0 + pnl, quantity=10,
                    pnl=pnl * 10, pnl_pct=pnl / 150.0,
                    holding_days=14, regime="RISK_ON",
                )

            trades = db.get_strategy_trades(sid, trade_type="paper")
            assert len(trades) == 3

            # Verify backtest exists
            backtest = db.get_strategy_backtest(sid)
            assert backtest["sharpe"] == 1.5

            db.close()


class TestReflectionAndWeights:
    """Test reflection and analyst weight updates work together."""

    def test_reflection_stored_and_retrieved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)

            db.insert_reflection(
                generation=0,
                patterns_that_work=["momentum works well"],
                patterns_that_fail=["mean reversion fails"],
                next_generation_guidance=["try breakouts"],
                regime_notes="RISK_ON environment",
            )

            reflections = db.get_reflections()
            assert len(reflections) == 1
            assert reflections[0]["patterns_that_work"] == ["momentum works well"]

            latest = db.get_latest_reflection()
            assert latest["generation"] == 0

            db.close()

    def test_analyst_weights_update(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db = Database(db_path)
            config = _make_config()

            trade_results = [
                {"pnl": 100, "analyst_scores": {"market": 1, "news": 1, "sentiment": -1, "fundamentals": 0, "options": 0}},
                {"pnl": -50, "analyst_scores": {"market": -1, "news": 0, "sentiment": 1, "fundamentals": -1, "options": 1}},
            ]

            weights = update_analyst_weights(db, trade_results, config)

            assert len(weights) == 5
            assert all(0.3 <= w <= 2.5 for w in weights.values())

            # Verify persisted
            stored = db.get_analyst_weights()
            assert len(stored) == 5

            db.close()
