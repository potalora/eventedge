"""Tests for the Evolution Engine."""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from copy import deepcopy

from tradingagents.autoresearch.evolution import (
    EvolutionEngine,
    _check_entry_rule,
    _check_exit_rule,
)
from tradingagents.autoresearch.models import (
    Strategy,
    BacktestResults,
    ScreenerResult,
    ScreenerCriteria,
)
from tradingagents.default_config import DEFAULT_CONFIG


def _make_config():
    config = deepcopy(DEFAULT_CONFIG)
    config["llm_provider"] = "anthropic"
    config["autoresearch"] = {
        "max_generations": 2,
        "strategies_per_generation": 2,
        "tickers_per_strategy": 3,
        "walk_forward_windows": 2,
        "holdout_weeks": 4,
        "min_trades_for_scoring": 1,
        "cache_model": "claude-haiku-4-5-20251001",
        "live_model": "claude-sonnet-4-20250514",
        "strategist_model": "claude-sonnet-4-20250514",
        "cro_model": "claude-haiku-4-5-20251001",
        "fitness_min_sharpe": 1.0,
        "fitness_min_win_rate": 0.50,
        "fitness_min_trades": 5,
        "paper_min_trades": 5,
        "paper_max_divergence_pct": 15,
        "analyst_weight_min": 0.3,
        "analyst_weight_max": 2.5,
        "complexity_penalty_factor": 0.1,
        "stop_unchanged_generations": 3,
        "universe": "sp500_nasdaq100",
        "budget_cap_usd": 150.0,
    }
    return config


def _make_screener_result(ticker="AAPL", **overrides):
    defaults = dict(
        ticker=ticker, close=150.0, change_14d=0.05, change_30d=0.10,
        high_52w=180.0, low_52w=120.0, avg_volume_20d=50_000_000,
        volume_ratio=1.2, rsi_14=55.0, ema_10=148.0, ema_50=145.0,
        macd=2.5, boll_position=0.6, iv_rank=0.4, put_call_ratio=0.8,
        options_volume=100_000, market_cap=2.5e12, sector="Technology",
        revenue_growth_yoy=0.15, next_earnings_date="2024-04-25",
        regime="RISK_ON", trading_day_coverage=0.95,
    )
    defaults.update(overrides)
    return ScreenerResult(**defaults)


class TestCheckEntryRule:
    """Tests for _check_entry_rule()."""

    def test_rsi_crosses_above(self):
        sr = _make_screener_result(rsi_14=35.0)
        assert _check_entry_rule("RSI_14 > 30", sr, {}) is True

    def test_rsi_crosses_above_fails(self):
        sr = _make_screener_result(rsi_14=25.0)
        assert _check_entry_rule("RSI_14 > 30", sr, {}) is False

    def test_price_above_ema10(self):
        sr = _make_screener_result(close=150.0, ema_10=148.0)
        assert _check_entry_rule("price > EMA_10", sr, {}) is True

    def test_price_below_ema10(self):
        sr = _make_screener_result(close=140.0, ema_10=148.0)
        assert _check_entry_rule("price > EMA_10", sr, {}) is False

    def test_ema_crossover(self):
        sr = _make_screener_result(ema_10=150.0, ema_50=145.0)
        assert _check_entry_rule("EMA_10 > EMA_50", sr, {}) is True

    def test_macd_positive(self):
        sr = _make_screener_result(macd=2.5)
        assert _check_entry_rule("MACD > 0", sr, {}) is True

    def test_buy_signal_from_pipeline(self):
        sr = _make_screener_result()
        assert _check_entry_rule("BUY signal from pipeline", sr, {"rating": "BUY"}) is True

    def test_buy_signal_from_pipeline_fail(self):
        sr = _make_screener_result()
        assert _check_entry_rule("BUY signal from pipeline", sr, {"rating": "SELL"}) is False

    def test_unknown_rule_defaults_true(self):
        sr = _make_screener_result()
        assert _check_entry_rule("some unknown rule", sr, {}) is True


class TestCheckExitRule:
    """Tests for _check_exit_rule()."""

    def test_profit_target_triggered(self):
        assert _check_exit_rule("50% profit target", 100.0, 160.0, 5, 30) is True

    def test_profit_target_not_triggered(self):
        assert _check_exit_rule("50% profit target", 100.0, 120.0, 5, 30) is False

    def test_stop_loss_triggered(self):
        assert _check_exit_rule("25% stop loss", 100.0, 70.0, 5, 30) is True

    def test_stop_loss_not_triggered(self):
        assert _check_exit_rule("25% stop loss", 100.0, 90.0, 5, 30) is False

    def test_time_horizon_exceeded(self):
        assert _check_exit_rule("time_horizon exceeded", 100.0, 100.0, 31, 30) is True

    def test_time_horizon_not_exceeded(self):
        assert _check_exit_rule("time_horizon exceeded", 100.0, 100.0, 10, 30) is False

    def test_unknown_rule_defaults_false(self):
        assert _check_exit_rule("something weird", 100.0, 100.0, 5, 30) is False


class TestShouldStop:
    """Tests for _should_stop()."""

    def test_budget_exceeded(self):
        engine = EvolutionEngine(MagicMock(), _make_config())
        engine._budget_used = 200.0
        assert engine._should_stop() is True

    def test_budget_not_exceeded(self):
        engine = EvolutionEngine(MagicMock(), _make_config())
        engine._budget_used = 10.0
        assert engine._should_stop() is False

    def test_unchanged_fitness(self):
        config = _make_config()
        config["autoresearch"]["stop_unchanged_generations"] = 3
        engine = EvolutionEngine(MagicMock(), config)
        engine._best_fitness_history = [1.5, 1.5, 1.5]
        assert engine._should_stop() is True

    def test_changing_fitness(self):
        config = _make_config()
        config["autoresearch"]["stop_unchanged_generations"] = 3
        engine = EvolutionEngine(MagicMock(), config)
        engine._best_fitness_history = [1.0, 1.2, 1.5]
        assert engine._should_stop() is False

    def test_not_enough_history(self):
        config = _make_config()
        config["autoresearch"]["stop_unchanged_generations"] = 3
        engine = EvolutionEngine(MagicMock(), config)
        engine._best_fitness_history = [1.5]
        assert engine._should_stop() is False


class TestCheckEntryRules:
    """Tests for EvolutionEngine._check_entry_rules()."""

    def test_all_rules_pass(self):
        engine = EvolutionEngine(MagicMock(), _make_config())
        strategy = Strategy(entry_rules=["RSI_14 > 30", "price > EMA_10"])
        sr = _make_screener_result(rsi_14=35.0, close=150.0, ema_10=148.0)
        assert engine._check_entry_rules(strategy, sr) is True

    def test_one_rule_fails(self):
        engine = EvolutionEngine(MagicMock(), _make_config())
        strategy = Strategy(entry_rules=["RSI_14 > 30", "price > EMA_10"])
        sr = _make_screener_result(rsi_14=25.0, close=150.0, ema_10=148.0)
        assert engine._check_entry_rules(strategy, sr) is False

    def test_empty_rules_returns_true(self):
        engine = EvolutionEngine(MagicMock(), _make_config())
        strategy = Strategy(entry_rules=[])
        sr = _make_screener_result()
        assert engine._check_entry_rules(strategy, sr) is True


class TestCheckExitRules:
    """Tests for EvolutionEngine._check_exit_rules()."""

    def test_exit_triggered(self):
        engine = EvolutionEngine(MagicMock(), _make_config())
        strategy = Strategy(exit_rules=["50% profit target", "25% stop loss"],
                            time_horizon_days=30)
        assert engine._check_exit_rules(strategy, 100.0, 160.0, 5) is True

    def test_no_exit_triggered(self):
        engine = EvolutionEngine(MagicMock(), _make_config())
        strategy = Strategy(exit_rules=["50% profit target", "25% stop loss"],
                            time_horizon_days=30)
        assert engine._check_exit_rules(strategy, 100.0, 105.0, 5) is False

    def test_empty_rules_uses_time_horizon(self):
        engine = EvolutionEngine(MagicMock(), _make_config())
        strategy = Strategy(exit_rules=[], time_horizon_days=30)
        assert engine._check_exit_rules(strategy, 100.0, 100.0, 31) is True


class TestGetLeaderboard:
    """Tests for get_leaderboard()."""

    def test_empty_leaderboard(self):
        db = MagicMock()
        db.get_top_strategies.return_value = []
        engine = EvolutionEngine(db, _make_config())
        assert engine.get_leaderboard() == []

    def test_leaderboard_format(self):
        db = MagicMock()
        db.get_top_strategies.return_value = [
            {"name": "strat1", "instrument": "stock_long", "fitness_score": 1.5,
             "status": "backtested", "generation": 0},
            {"name": "strat2", "instrument": "call_option", "fitness_score": 1.2,
             "status": "backtested", "generation": 1},
        ]
        engine = EvolutionEngine(db, _make_config())
        lb = engine.get_leaderboard()
        assert len(lb) == 2
        assert lb[0]["rank"] == 1
        assert lb[0]["name"] == "strat1"


class TestGetProgress:
    """Tests for get_progress()."""

    def test_initial_progress(self):
        engine = EvolutionEngine(MagicMock(), _make_config())
        progress = engine.get_progress()
        assert progress["generation"] == 0
        assert progress["budget_used"] == 0.0


class TestEvolutionRun:
    """Tests for the full run() loop with mocked components."""

    @patch("tradingagents.autoresearch.evolution.get_universe")
    @patch("tradingagents.autoresearch.evolution.generate_windows")
    @patch("tradingagents.autoresearch.evolution.get_test_dates")
    @patch("tradingagents.autoresearch.evolution.rank_strategies")
    @patch("tradingagents.autoresearch.evolution.update_analyst_weights")
    def test_full_loop(self, mock_update_weights, mock_rank, mock_test_dates,
                       mock_gen_windows, mock_universe):
        db = MagicMock()
        db.get_top_strategies.return_value = []
        db.get_strategies_by_generation.return_value = []
        config = _make_config()
        config["autoresearch"]["max_generations"] = 1

        mock_universe.return_value = ["AAPL", "MSFT"]

        # Mock screener
        with patch.object(MarketScreener, "run") as mock_screener_run:
            mock_screener_run.return_value = [
                _make_screener_result("AAPL"),
                _make_screener_result("MSFT"),
            ]

            # Mock walk-forward
            from tradingagents.autoresearch.walk_forward import WalkForwardWindow
            mock_gen_windows.return_value = (
                [WalkForwardWindow("2023-01-01", "2023-06-01", "2023-06-02", "2023-08-01")],
                ("2023-10-01", "2023-12-01"),
            )
            mock_test_dates.return_value = ["2023-06-02"]

            # Mock strategist
            with patch.object(Strategist, "propose") as mock_propose, \
                 patch.object(Strategist, "reflect") as mock_reflect:

                strat = Strategy(
                    id=1, name="test_strat", generation=0,
                    entry_rules=["RSI_14 > 30"],
                    exit_rules=["50% profit target"],
                    instrument="stock_long",
                    backtest_results=BacktestResults(
                        sharpe=1.5, total_return=0.10, max_drawdown=-0.05,
                        win_rate=0.6, profit_factor=2.0, num_trades=10,
                    ),
                )
                mock_propose.return_value = [strat]
                mock_rank.return_value = [strat]
                mock_reflect.return_value = {}

                # Mock pipeline
                with patch.object(CachedPipelineRunner, "run") as mock_pipeline_run:
                    mock_pipeline_run.return_value = {
                        "rating": "BUY",
                        "market_report": "good",
                        "analyst_scores": {"market": 1},
                    }

                    # Mock apply_filters to return True
                    with patch.object(MarketScreener, "apply_filters", return_value=True):
                        engine = EvolutionEngine(db, config)
                        result = engine.run("2023-01-01", "2023-12-01")

                        assert result["generations_run"] == 1
                        assert "leaderboard" in result
                        mock_propose.assert_called_once()
                        mock_reflect.assert_called_once()


# Import after definition to avoid circular issues
from tradingagents.autoresearch.screener import MarketScreener
from tradingagents.autoresearch.strategist import Strategist
from tradingagents.autoresearch.cached_pipeline import CachedPipelineRunner
