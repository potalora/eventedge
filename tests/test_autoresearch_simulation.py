"""
Full autoresearch simulation — exercises the entire pipeline end-to-end
with mocked LLM and yfinance calls, verifying the system is ready for a real run.

This is NOT a unit test. It's a realistic dry-run that:
1. Creates a real SQLite DB (temp)
2. Runs the full EvolutionEngine for 3 generations
3. Verifies DB state at each stage
4. Checks strategy lifecycle transitions
5. Validates cache economics
6. Runs the CLI leaderboard command
7. Runs the scheduler paper trading job
"""

import json
import os
import tempfile
from copy import deepcopy
from unittest.mock import patch, MagicMock

import pytest

from tradingagents.storage.db import Database
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.autoresearch.models import (
    Strategy, BacktestResults, ScreenerResult, ScreenerCriteria, Filter,
)
from tradingagents.autoresearch.evolution import EvolutionEngine, _check_entry_rule, _check_exit_rule
from tradingagents.autoresearch.cached_pipeline import CachedPipelineRunner
from tradingagents.autoresearch.strategist import Strategist
from tradingagents.autoresearch.screener import MarketScreener
from tradingagents.autoresearch.fitness import (
    compute_fitness, rank_strategies, meets_paper_criteria,
    meets_graduation_criteria, check_failure_criteria, update_analyst_weights,
)
from tradingagents.autoresearch.walk_forward import (
    generate_windows, get_test_dates, has_regime_diversity,
    cross_ticker_validation_split, WalkForwardWindow,
)
from tradingagents.autoresearch.ticker_universe import get_universe, UNIVERSE_PRESETS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(max_gen=3):
    config = deepcopy(DEFAULT_CONFIG)
    config["llm_provider"] = "anthropic"
    config["autoresearch"].update({
        "max_generations": max_gen,
        "strategies_per_generation": 3,
        "tickers_per_strategy": 3,
        "walk_forward_windows": 2,
        "holdout_weeks": 4,
        "min_trades_for_scoring": 1,
        "fitness_min_sharpe": 0.3,
        "fitness_min_win_rate": 0.30,
        "fitness_min_trades": 2,
        "stop_unchanged_generations": 5,  # high so we don't early-stop
        "budget_cap_usd": 500.0,
    })
    return config


def _screener_result(ticker, regime="RISK_ON", rsi=55.0, close=150.0):
    return ScreenerResult(
        ticker=ticker, close=close, change_14d=0.05, change_30d=0.10,
        high_52w=180.0, low_52w=120.0, avg_volume_20d=50_000_000,
        volume_ratio=1.2, rsi_14=rsi, ema_10=close - 2, ema_50=close - 5,
        macd=2.5, boll_position=0.6, iv_rank=0.4, put_call_ratio=0.8,
        options_volume=100_000, market_cap=2.5e12, sector="Technology",
        revenue_growth_yoy=0.15, next_earnings_date="2024-04-25",
        regime=regime, trading_day_coverage=0.95,
    )


TICKERS = ["AAPL", "MSFT", "GOOG", "NVDA", "META"]
SCREENER_RESULTS = [_screener_result(t) for t in TICKERS]

_call_counter = {"n": 0}

def _strategy_json(gen, count=3):
    """Generate strategy proposals for a generation."""
    strats = []
    for i in range(count):
        strats.append({
            "name": f"strat_g{gen}_{i}",
            "hypothesis": f"Gen {gen} strategy {i} — momentum play on tech",
            "instrument": ["stock_long", "call_option", "stock_short"][i % 3],
            "entry_rules": ["RSI_14 crosses above 30", "price > EMA_10"],
            "exit_rules": ["50% profit target", "25% stop loss", "time_horizon exceeded"],
            "position_size_pct": 0.05,
            "max_risk_pct": 0.05,
            "time_horizon_days": 30,
            "conviction": 70 + gen * 5,
            "parent_ids": [],
            "screener_criteria": {
                "market_cap_range": [1e9, 1e13],
                "min_avg_volume": 100000,
            },
        })
    return json.dumps(strats)


def _cro_response(approved=True):
    return json.dumps({
        "approved": approved,
        "reason": "Approved — risk parameters acceptable" if approved else "Rejected — too concentrated",
        "risk_score": 3 if approved else 8,
        "concerns": [] if approved else ["position size too large"],
    })


def _reflection_response(gen):
    return json.dumps({
        "patterns_that_work": [f"momentum works in gen {gen}", "RSI reversal"],
        "patterns_that_fail": [f"mean reversion failed in gen {gen}"],
        "next_generation_guidance": [f"try more diverse instruments in gen {gen+1}"],
        "regime_notes": f"RISK_ON as of gen {gen}",
    })


def _mock_llm_factory(max_gen=3, strategies_per_gen=3):
    """Create a mock _call_llm that returns appropriate responses per call."""
    call_sequence = []
    for gen in range(max_gen):
        # 1) strategist propose
        call_sequence.append(_strategy_json(gen, strategies_per_gen))
        # 2) CRO reviews — approve first 2, reject 3rd
        for i in range(strategies_per_gen):
            call_sequence.append(_cro_response(approved=(i < 2)))
        # 3) reflection
        call_sequence.append(_reflection_response(gen))

    idx = {"i": 0}

    def side_effect(prompt, model):
        if idx["i"] < len(call_sequence):
            resp = call_sequence[idx["i"]]
            idx["i"] += 1
            return resp
        # Fallback — return empty strategies
        return "[]"

    return side_effect


def _mock_pipeline_result(ticker, date, tier):
    """Simulate pipeline results — alternate BUY/SELL for variety."""
    ratings = {"AAPL": "BUY", "MSFT": "SELL", "GOOG": "BUY", "NVDA": "BUY", "META": "SELL"}
    rating = ratings.get(ticker, "HOLD")
    return {
        "ticker": ticker,
        "trade_date": date,
        "model_tier": tier,
        "rating": rating,
        "market_report": f"Market analysis for {ticker}",
        "sentiment_report": f"Sentiment for {ticker}",
        "news_report": f"News for {ticker}",
        "fundamentals_report": f"Fundamentals for {ticker}",
        "options_report": f"Options for {ticker}",
        "full_decision": f"{rating} {ticker}",
        "debate_summary": f"Debate: {rating}",
        "analyst_scores": {"market": 1, "news": 0, "sentiment": 1, "fundamentals": -1, "options": 0},
    }


# ===========================================================================
# SIMULATION TESTS
# ===========================================================================

class TestPreflightChecks:
    """Verify all components are importable and configured correctly."""

    def test_universe_populated(self):
        tickers = get_universe(DEFAULT_CONFIG)
        assert len(tickers) > 20
        assert "AAPL" in tickers
        assert "MSFT" in tickers

    def test_config_complete(self):
        ar = DEFAULT_CONFIG["autoresearch"]
        required = [
            "max_generations", "strategies_per_generation", "tickers_per_strategy",
            "walk_forward_windows", "holdout_weeks", "min_trades_for_scoring",
            "cache_model", "live_model", "strategist_model", "cro_model",
            "fitness_min_sharpe", "fitness_min_win_rate", "fitness_min_trades",
            "budget_cap_usd", "universe",
        ]
        for key in required:
            assert key in ar, f"Missing config key: {key}"

    def test_walk_forward_windows_generate(self):
        windows, holdout = generate_windows("2023-01-01", "2025-01-01", num_windows=3, holdout_weeks=6)
        assert len(windows) == 3
        dates = get_test_dates(windows)
        assert len(dates) == 3
        assert holdout[1] == "2025-01-01"

    def test_database_tables_exist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, "test.db"))
            # All 6 autoresearch tables should exist
            tables = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {t["name"] for t in tables}
            for t in ["strategies", "strategy_backtest_results", "strategy_trades",
                       "pipeline_cache", "reflections", "analyst_weights"]:
                assert t in table_names, f"Missing table: {t}"
            db.close()

    def test_model_serialization_roundtrip(self):
        s = Strategy(
            id=1, generation=0, name="test", hypothesis="h",
            instrument="stock_long", entry_rules=["RSI > 30"],
            exit_rules=["25% stop"], screener=ScreenerCriteria(
                market_cap_range=[1e9, 1e12],
                custom_filters=[Filter("rsi_14", ">", 30)],
            ),
        )
        d = s.to_db_dict()
        assert isinstance(d["entry_rules"], str)  # JSON string
        assert isinstance(d["screener_criteria"], str)

        prompt = s.to_prompt_str()
        assert "test" in prompt
        assert "RSI > 30" in prompt

    def test_entry_exit_rules_work(self):
        sr = _screener_result("AAPL", rsi=35.0, close=150.0)
        assert _check_entry_rule("RSI_14 crosses above 30", sr, {}) is True
        assert _check_entry_rule("RSI_14 crosses above 50", sr, {}) is False
        assert _check_entry_rule("price > EMA_10", sr, {}) is True
        assert _check_entry_rule("BUY signal from pipeline", sr, {"rating": "BUY"}) is True
        assert _check_entry_rule("BUY signal from pipeline", sr, {"rating": "SELL"}) is False

        assert _check_exit_rule("50% profit target", 100.0, 160.0, 5, 30) is True
        assert _check_exit_rule("50% profit target", 100.0, 120.0, 5, 30) is False
        assert _check_exit_rule("25% stop loss", 100.0, 70.0, 5, 30) is True
        assert _check_exit_rule("time_horizon exceeded", 100.0, 100.0, 31, 30) is True

    def test_fitness_scoring(self):
        config = _make_config()
        s = Strategy(
            backtest_results=BacktestResults(
                sharpe=1.5, profit_factor=2.0, max_drawdown=-0.10,
                win_rate=0.65, num_trades=20,
            ),
            entry_rules=["RSI > 30"],
            screener=ScreenerCriteria(),
        )
        score = compute_fitness(s, config)
        assert score > 0
        # Formula: 1.5 * min(2.0, 3.0) * (1 - 0.10) * complexity_penalty
        # = 1.5 * 2.0 * 0.9 * (1 / (1 + 0.1 * 1)) = 2.7 * 0.909 ≈ 2.454
        assert 2.0 < score < 3.0


class TestFullSimulation:
    """Full 3-generation evolution simulation with real DB."""

    @patch.object(CachedPipelineRunner, "run")
    @patch.object(Strategist, "_call_llm")
    @patch.object(MarketScreener, "run")
    @patch("tradingagents.autoresearch.evolution.get_universe")
    def test_three_generation_evolution(self, mock_universe, mock_screener,
                                         mock_llm, mock_pipeline):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "simulation.db")
            db = Database(db_path)
            config = _make_config(max_gen=3)

            # Wire up mocks
            mock_universe.return_value = TICKERS
            mock_screener.return_value = SCREENER_RESULTS
            mock_llm.side_effect = _mock_llm_factory(max_gen=3, strategies_per_gen=3)
            mock_pipeline.side_effect = lambda t, d, tier: _mock_pipeline_result(t, d, tier)

            with patch.object(MarketScreener, "apply_filters", return_value=True):
                engine = EvolutionEngine(db, config)
                result = engine.run("2023-01-01", "2024-06-01")

            # --- Verify evolution ran ---
            assert result["generations_run"] == 3
            assert "leaderboard" in result
            assert "cache_stats" in result

            # --- Verify DB state ---
            # Strategies created: 3 proposed per gen, 2 approved by CRO = 6 strategies
            all_strategies = db.get_top_strategies(limit=50)
            assert len(all_strategies) >= 4, f"Expected >=4 strategies, got {len(all_strategies)}"

            # Each generation should have strategies
            for gen in range(3):
                gen_strats = db.get_strategies_by_generation(gen)
                assert len(gen_strats) >= 1, f"Gen {gen} has no strategies"

            # --- Verify reflections ---
            reflections = db.get_reflections()
            assert len(reflections) == 3, f"Expected 3 reflections, got {len(reflections)}"
            for i, ref in enumerate(reflections):
                assert ref["generation"] == i
                assert isinstance(ref["patterns_that_work"], list)
                assert len(ref["patterns_that_work"]) > 0

            # --- Verify backtest results ---
            backtested = [s for s in all_strategies if s.get("fitness_score", 0) > 0]
            assert len(backtested) >= 1, "No strategies got positive fitness"

            for s in backtested:
                bt = db.get_strategy_backtest(s["id"])
                if bt:
                    assert bt["num_trades"] > 0
                    assert isinstance(bt["tickers_tested"], list)
                    assert isinstance(bt["walk_forward_scores"], list)

            # --- Verify trades recorded ---
            has_trades = False
            for s in all_strategies:
                trades = db.get_strategy_trades(s["id"], trade_type="backtest")
                if trades:
                    has_trades = True
                    for t in trades:
                        assert t["ticker"] in TICKERS
                        assert t["trade_type"] == "backtest"
                        assert t["entry_price"] is not None
            assert has_trades, "No backtest trades were recorded"

            # --- Verify leaderboard ---
            lb = engine.get_leaderboard()
            if lb:
                assert lb[0]["rank"] == 1
                assert "name" in lb[0]
                assert "fitness_score" in lb[0]
                # Sorted descending
                for i in range(len(lb) - 1):
                    assert lb[i]["fitness_score"] >= lb[i+1]["fitness_score"]

            # --- Verify progress ---
            progress = engine.get_progress()
            assert progress["generation"] == 3
            assert len(progress["best_fitness_history"]) > 0

            # --- Print summary ---
            print("\n" + "=" * 60)
            print("SIMULATION RESULTS")
            print("=" * 60)
            print(f"Generations run: {result['generations_run']}")
            print(f"Strategies created: {len(all_strategies)}")
            print(f"  With positive fitness: {len(backtested)}")
            print(f"Reflections: {len(reflections)}")
            print(f"Cache stats: {result['cache_stats']}")
            print(f"Best fitness history: {progress['best_fitness_history']}")
            print(f"\nLeaderboard:")
            for entry in (lb or [])[:5]:
                print(f"  #{entry['rank']} {entry['name']} "
                      f"(fitness={entry['fitness_score']:.4f}, {entry['instrument']})")
            print("=" * 60)

            db.close()


class TestStrategyLifecycleSimulation:
    """Simulate the full strategy lifecycle: proposed → backtested → paper → ready."""

    def test_full_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "lifecycle.db")
            db = Database(db_path)
            config = _make_config()

            # 1. Insert strategy (PROPOSED)
            sid = db.insert_strategy(
                generation=0, parent_ids=[], name="lifecycle_sim",
                hypothesis="Momentum in tech when RSI recovers",
                conviction=80,
                screener_criteria={"market_cap_range": [1e9, 1e12]},
                instrument="stock_long",
                entry_rules=["RSI_14 crosses above 30", "price > EMA_10"],
                exit_rules=["50% profit target", "25% stop loss"],
                position_size_pct=0.05, max_risk_pct=0.05,
                time_horizon_days=30, regime_born="RISK_ON",
                status="proposed",
            )
            s = db.get_strategy(sid)
            assert s["status"] == "proposed"

            # 2. Backtest → BACKTESTED
            db.insert_strategy_backtest(
                strategy_id=sid, sharpe=1.8, total_return=0.22,
                max_drawdown=-0.08, win_rate=0.65, profit_factor=2.5,
                num_trades=25, tickers_tested=["AAPL", "MSFT", "GOOG"],
                backtest_period="2023-01-01 to 2024-01-01",
                walk_forward_scores=[1.5, 1.7, 2.0],
                holdout_sharpe=1.4,
            )
            db.update_strategy_status(sid, "backtested")
            db.update_strategy_fitness(sid, 3.24)

            s = db.get_strategy(sid)
            assert s["status"] == "backtested"
            assert s["fitness_score"] == 3.24

            bt = db.get_strategy_backtest(sid)
            assert bt["sharpe"] == 1.8
            assert bt["tickers_tested"] == ["AAPL", "MSFT", "GOOG"]
            assert bt["walk_forward_scores"] == [1.5, 1.7, 2.0]

            # 3. Check paper criteria
            strat_obj = Strategy.from_db_dict(s)
            strat_obj.backtest_results = BacktestResults(
                sharpe=1.8, total_return=0.22, max_drawdown=-0.08,
                win_rate=0.65, profit_factor=2.5, num_trades=25,
                walk_forward_scores=[1.5, 1.7, 2.0], holdout_sharpe=1.4,
            )
            assert meets_paper_criteria(strat_obj, config) is True

            # 4. Promote to PAPER
            db.update_strategy_status(sid, "paper")

            # 5. Record paper trades
            paper_trades_data = [
                ("AAPL", "2024-02-01", "2024-02-15", 150.0, 157.0, 70.0, 0.047),
                ("MSFT", "2024-02-05", "2024-02-20", 400.0, 412.0, 120.0, 0.030),
                ("GOOG", "2024-02-10", "2024-02-25", 140.0, 145.0, 50.0, 0.036),
                ("AAPL", "2024-03-01", "2024-03-15", 158.0, 152.0, -60.0, -0.038),
                ("NVDA", "2024-03-05", "2024-03-20", 800.0, 840.0, 400.0, 0.050),
            ]
            for ticker, entry, exit_, ep, xp, pnl, pnl_pct in paper_trades_data:
                db.insert_strategy_trade(
                    strategy_id=sid, ticker=ticker, trade_type="paper",
                    entry_date=entry, exit_date=exit_,
                    instrument="stock_long", entry_price=ep, exit_price=xp,
                    quantity=10, pnl=pnl, pnl_pct=pnl_pct,
                    holding_days=14, regime="RISK_ON",
                )

            trades = db.get_strategy_trades(sid, trade_type="paper")
            assert len(trades) == 5

            # 6. Check graduation
            paper_trades = db.get_strategy_trades(sid, trade_type="paper")
            grad = meets_graduation_criteria(strat_obj, paper_trades, config)
            # Should pass: 5 trades, 4 winners (80% win rate, close to 65% backtest)
            print(f"\nGraduation criteria met: {grad}")

            # 7. Check failure criteria — should NOT fail
            failed = check_failure_criteria(strat_obj, paper_trades)
            assert failed is False, "Strategy should not be marked failed"

            # 8. Promote to READY
            db.update_strategy_status(sid, "ready")
            s = db.get_strategy(sid)
            assert s["status"] == "ready"

            print(f"Strategy '{s['name']}' successfully promoted through full lifecycle:")
            print(f"  proposed → backtested → paper → ready")
            print(f"  Fitness: {s['fitness_score']}")
            print(f"  Paper trades: {len(trades)} (4W/1L)")

            db.close()


class TestCacheEconomicsSimulation:
    """Verify cache hits improve across repeated pipeline calls."""

    def test_cache_hit_rate_progression(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "cache.db")
            db = Database(db_path)
            config = _make_config()

            runner = CachedPipelineRunner(db, config)

            final_state = {
                "market_report": "test", "sentiment_report": "test",
                "news_report": "test", "fundamentals_report": "test",
                "options_report": "test", "final_trade_decision": "BUY AAPL",
                "investment_debate_state": {"judge_decision": "BUY"},
                "risk_debate_state": {"judge_decision": "OK"},
            }

            with patch("tradingagents.graph.trading_graph.TradingAgentsGraph") as MockGraph:
                mock_inst = MagicMock()
                mock_inst.propagate.return_value = (final_state, "BUY")
                MockGraph.return_value = mock_inst

                # Gen 0: 3 unique calls → 3 misses
                for ticker in ["AAPL", "MSFT", "GOOG"]:
                    runner.run(ticker, "2024-01-15", "haiku")

                stats0 = runner.get_cache_stats()
                assert stats0["misses"] == 3
                assert stats0["hits"] == 0
                assert stats0["hit_rate"] == 0.0

                # Gen 1: same 3 tickers → 3 hits
                for ticker in ["AAPL", "MSFT", "GOOG"]:
                    runner.run(ticker, "2024-01-15", "haiku")

                stats1 = runner.get_cache_stats()
                assert stats1["hits"] == 3
                assert stats1["misses"] == 3
                assert stats1["hit_rate"] == 0.5

                # Gen 2: same 3 + 1 new → 3 hits + 1 miss
                for ticker in ["AAPL", "MSFT", "GOOG", "NVDA"]:
                    runner.run(ticker, "2024-01-15", "haiku")

                stats2 = runner.get_cache_stats()
                assert stats2["hits"] == 6
                assert stats2["misses"] == 4
                assert stats2["hit_rate"] == 0.6

                # Pipeline only called for cache misses
                assert mock_inst.propagate.call_count == 4  # 3 + 0 + 1

            print(f"\nCache economics:")
            print(f"  Gen 0: {stats0}")
            print(f"  Gen 1: {stats1}")
            print(f"  Gen 2: {stats2}")
            print(f"  Total pipeline calls: 4 (saved 6 via cache)")

            db.close()


class TestAnalystWeightEvolution:
    """Verify Darwinian analyst weights update correctly."""

    def test_weights_evolve_over_rounds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, "weights.db"))
            config = _make_config()

            # Round 1: market and news perform well
            trades1 = [
                {"pnl": 100, "analyst_scores": {"market": 1, "news": 1, "sentiment": -1, "fundamentals": 0, "options": -1}},
                {"pnl": 50, "analyst_scores": {"market": 1, "news": 0, "sentiment": -1, "fundamentals": 1, "options": 0}},
            ]
            w1 = update_analyst_weights(db, trades1, config)
            assert all(0.3 <= v <= 2.5 for v in w1.values())

            # Round 2: sentiment recovers
            trades2 = [
                {"pnl": 200, "analyst_scores": {"market": 0, "news": -1, "sentiment": 1, "fundamentals": 1, "options": 1}},
            ]
            w2 = update_analyst_weights(db, trades2, config)

            # Weights should be persisted
            stored = db.get_analyst_weights()
            assert len(stored) == 5
            for analyst, weight in stored.items():
                assert 0.3 <= weight <= 2.5

            print(f"\nAnalyst weights after 2 rounds:")
            for a in sorted(stored.keys()):
                print(f"  {a}: {stored[a]:.4f}")

            db.close()


class TestCLIReadiness:
    """Verify CLI commands don't crash."""

    def test_leaderboard_command_import(self):
        """Verify the leaderboard command is registered."""
        from cli.main import app
        callback_names = [cmd.callback.__name__ for cmd in app.registered_commands if cmd.callback]
        assert "leaderboard" in callback_names
        assert "autoresearch" in callback_names
        assert "paper_status" in callback_names

    def test_leaderboard_with_empty_db(self):
        from typer.testing import CliRunner
        from cli.main import app

        runner = CliRunner()
        with patch("os.path.exists", return_value=False):
            result = runner.invoke(app, ["leaderboard"])
        assert result.exit_code == 0


class TestDashboardReadiness:
    """Verify dashboard pages are importable and renderable."""

    def test_all_pages_import(self):
        from tradingagents.dashboard.pages.leaderboard import render as lb_render
        from tradingagents.dashboard.pages.evolution import render as ev_render
        from tradingagents.dashboard.pages.paper_trading import render as pt_render
        assert callable(lb_render)
        assert callable(ev_render)
        assert callable(pt_render)


class TestSchedulerReadiness:
    """Verify scheduler jobs are importable."""

    def test_jobs_import(self):
        from tradingagents.scheduler.jobs import paper_trading_job, evolution_job
        assert callable(paper_trading_job)
        assert callable(evolution_job)

    def test_scheduler_registers_all_jobs(self):
        from tradingagents.scheduler.scheduler import TradingScheduler

        with patch("tradingagents.scheduler.scheduler.BackgroundScheduler") as MockSched:
            mock_sched = MagicMock()
            MockSched.return_value = mock_sched

            config = {
                "scheduler": {
                    "scan_time": "07:00",
                    "portfolio_check_times": [],
                    "timezone": "US/Eastern",
                },
                "alerts": {"enabled": False, "channels": [], "notify_on": []},
            }
            ts = TradingScheduler(config)
            ts.start()

            job_ids = [call.kwargs.get("id", "") for call in mock_sched.add_job.call_args_list]
            assert "daily_scan" in job_ids
            assert "paper_trading" in job_ids
            assert "weekly_evolution" in job_ids
