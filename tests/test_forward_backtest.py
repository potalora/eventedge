"""Tests for real forward-return backtesting (no LLM in the loop)."""

import os
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pandas as pd
import numpy as np
import pytest

from tradingagents.strategies.state.models import (
    Strategy, BacktestResults, ScreenerResult, ScreenerCriteria,
)
from tradingagents.strategies._dormant.screener import MarketScreener
from tradingagents.strategies._dormant.evolution import (
    EvolutionEngine, _check_entry_rule,
)
from tradingagents.strategies._dormant.walk_forward import WalkForwardWindow
from tradingagents.storage.db import Database
from tradingagents.default_config import DEFAULT_CONFIG


def _make_config():
    config = deepcopy(DEFAULT_CONFIG)
    config["llm_provider"] = "anthropic"
    config["autoresearch"] = {
        "max_generations": 1,
        "strategies_per_generation": 2,
        "tickers_per_strategy": 2,
        "walk_forward_windows": 2,
        "holdout_weeks": 4,
        "min_trades_for_scoring": 1,
        "fast_backtest": True,
        "fast_backtest_max_workers": 1,
        "cache_model": "claude-haiku-4-5-20251001",
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


def _make_screener_result(ticker="AAPL", regime="RISK_ON", close=150.0,
                           rsi=55.0, ema_10=148.0, ema_50=145.0):
    return ScreenerResult(
        ticker=ticker, close=close, change_14d=0.05, change_30d=0.10,
        high_52w=180.0, low_52w=120.0, avg_volume_20d=50_000_000,
        volume_ratio=1.2, rsi_14=rsi, ema_10=ema_10, ema_50=ema_50,
        macd=2.5, boll_position=0.6, iv_rank=0.4, put_call_ratio=0.8,
        options_volume=100_000, market_cap=2.5e12, sector="Technology",
        revenue_growth_yoy=0.15, next_earnings_date="2024-04-25",
        regime=regime, trading_day_coverage=0.95,
    )


def _make_price_df(start_date="2024-01-15", num_days=30, start_price=150.0,
                    daily_returns=None):
    """Create a price DataFrame for testing.

    Args:
        daily_returns: List of daily returns (e.g., [0.01, -0.02, ...]).
                       If None, generates small random returns.
    """
    dates = pd.bdate_range(start=start_date, periods=num_days)
    if daily_returns is None:
        daily_returns = [0.005] * num_days  # steady 0.5% daily gain

    prices = [start_price]
    for r in daily_returns[:num_days - 1]:
        prices.append(prices[-1] * (1 + r))

    df = pd.DataFrame({
        "Open": [p * 0.999 for p in prices],
        "High": [p * 1.005 for p in prices],
        "Low": [p * 0.995 for p in prices],
        "Close": prices,
        "Volume": [1_000_000] * len(prices),
    }, index=dates[:len(prices)])
    return df


def _make_strategy(**kwargs):
    defaults = dict(
        generation=0, parent_ids=[], name="test_strat",
        hypothesis="test", conviction=75,
        screener=ScreenerCriteria(),
        instrument="stock_long",
        entry_rules=["RSI_14 < 60", "price > EMA_50"],
        exit_rules=["15% profit target", "10% stop loss", "time_horizon exceeded"],
        position_size_pct=0.05, max_risk_pct=0.05,
        time_horizon_days=30, regime_born="RISK_ON", status="proposed",
    )
    defaults.update(kwargs)
    return Strategy(**defaults)


# ===========================================================================
# TestForwardPriceFetch
# ===========================================================================

class TestForwardPriceFetch:
    """Test MarketScreener.fetch_forward_prices()."""

    @patch("yfinance.download")
    def test_returns_dataframe_per_ticker(self, mock_download):
        df_aapl = _make_price_df("2024-01-15", 20, 150.0)
        df_msft = _make_price_df("2024-01-15", 20, 380.0)

        # Simulate multi-ticker download with MultiIndex columns
        combined = pd.concat(
            {"AAPL": df_aapl, "MSFT": df_msft}, axis=1
        )
        mock_download.return_value = combined

        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, "test.db"))
            screener = MarketScreener(_make_config())
            result = screener.fetch_forward_prices(
                ["AAPL", "MSFT"], "2024-01-15", "2024-03-15"
            )

            assert "AAPL" in result
            assert "MSFT" in result
            assert isinstance(result["AAPL"], pd.DataFrame)
            assert "Close" in result["AAPL"].columns
            db.close()

    @patch("yfinance.download")
    def test_handles_missing_ticker(self, mock_download):
        # Only AAPL has data
        df_aapl = _make_price_df("2024-01-15", 20, 150.0)
        combined = pd.concat({"AAPL": df_aapl}, axis=1)
        mock_download.return_value = combined

        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, "test.db"))
            screener = MarketScreener(_make_config())
            result = screener.fetch_forward_prices(
                ["AAPL", "MISSING"], "2024-01-15", "2024-03-15"
            )

            assert "AAPL" in result
            assert "MISSING" not in result
            db.close()

    @patch("yfinance.download")
    def test_single_ticker_no_multiindex(self, mock_download):
        df = _make_price_df("2024-01-15", 20, 150.0)
        mock_download.return_value = df

        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, "test.db"))
            screener = MarketScreener(_make_config())
            result = screener.fetch_forward_prices(
                ["AAPL"], "2024-01-15", "2024-03-15"
            )

            assert "AAPL" in result
            assert len(result["AAPL"]) == 20
            db.close()

    @patch("yfinance.download")
    def test_empty_download_returns_empty(self, mock_download):
        mock_download.return_value = pd.DataFrame()

        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, "test.db"))
            screener = MarketScreener(_make_config())
            result = screener.fetch_forward_prices(
                ["AAPL"], "2024-01-15", "2024-03-15"
            )

            assert result == {}
            db.close()


# ===========================================================================
# TestSimulateTrade
# ===========================================================================

class TestSimulateTrade:
    """Test EvolutionEngine._simulate_trade() with real prices."""

    def _make_engine(self, tmpdir):
        db = Database(os.path.join(tmpdir, "test.db"))
        config = _make_config()
        engine = EvolutionEngine(db, config)
        return engine, db

    def test_profit_target_hit(self):
        """Price rises to hit 15% profit target."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine, db = self._make_engine(tmpdir)

            # Price goes from 100 to 116 over 10 days (hits 15% target)
            returns = [0.015] * 10 + [0.005] * 20  # ~16% in 10 days
            engine._forward_price_cache["AAPL"] = _make_price_df(
                "2024-01-15", 30, 100.0, returns
            )

            strategy = _make_strategy(
                exit_rules=["15% profit target", "10% stop loss"],
                time_horizon_days=30,
            )

            trade = engine._simulate_trade(
                strategy, "AAPL", "2024-01-15", 100.0, "2024-03-15", "RISK_ON"
            )

            assert trade is not None
            assert trade["exit_reason"] == "profit_target"
            assert trade["pnl_pct"] > 0.14  # should be ~15%+
            assert trade["holding_days"] < 30
            db.close()

    def test_stop_loss_hit(self):
        """Price drops to hit 10% stop loss."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine, db = self._make_engine(tmpdir)

            # Price drops ~2% per day
            returns = [-0.02] * 10 + [0.01] * 20
            engine._forward_price_cache["AAPL"] = _make_price_df(
                "2024-01-15", 30, 100.0, returns
            )

            strategy = _make_strategy(
                exit_rules=["20% profit target", "10% stop loss"],
                time_horizon_days=30,
            )

            trade = engine._simulate_trade(
                strategy, "AAPL", "2024-01-15", 100.0, "2024-03-15", "RISK_ON"
            )

            assert trade is not None
            assert trade["exit_reason"] == "stop_loss"
            assert trade["pnl_pct"] < -0.09  # should be ~-10%
            db.close()

    def test_trailing_stop(self):
        """Price rises then falls, trailing stop triggers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine, db = self._make_engine(tmpdir)

            # Rise 10%, then drop 8% from peak
            returns = [0.02] * 5 + [-0.02] * 5 + [0.01] * 20
            engine._forward_price_cache["AAPL"] = _make_price_df(
                "2024-01-15", 30, 100.0, returns
            )

            strategy = _make_strategy(
                exit_rules=["50% profit target", "5% trailing stop"],
                time_horizon_days=30,
            )

            trade = engine._simulate_trade(
                strategy, "AAPL", "2024-01-15", 100.0, "2024-03-15", "RISK_ON"
            )

            assert trade is not None
            assert trade["exit_reason"] == "trailing_stop"
            db.close()

    def test_time_horizon_exit(self):
        """Price stays flat, exits at time horizon."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine, db = self._make_engine(tmpdir)

            # Flat prices
            returns = [0.001] * 30
            engine._forward_price_cache["AAPL"] = _make_price_df(
                "2024-01-15", 30, 100.0, returns
            )

            strategy = _make_strategy(
                exit_rules=["50% profit target", "50% stop loss", "time_horizon exceeded"],
                time_horizon_days=10,
            )

            trade = engine._simulate_trade(
                strategy, "AAPL", "2024-01-15", 100.0, "2024-06-15", "RISK_ON"
            )

            assert trade is not None
            assert trade["exit_reason"] == "time_horizon"
            assert trade["holding_days"] == 10
            # PnL should be small (0.1% daily * 10 days ≈ 1%)
            assert abs(trade["pnl_pct"]) < 0.05
            db.close()

    def test_window_end_exit(self):
        """Window ends before time horizon."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine, db = self._make_engine(tmpdir)

            returns = [0.001] * 30
            engine._forward_price_cache["AAPL"] = _make_price_df(
                "2024-01-15", 30, 100.0, returns
            )

            strategy = _make_strategy(
                exit_rules=["50% profit target", "50% stop loss"],
                time_horizon_days=90,  # much longer than window
            )

            # Window ends 5 trading days after entry
            window_end = pd.bdate_range("2024-01-15", periods=6)[-1].strftime("%Y-%m-%d")

            trade = engine._simulate_trade(
                strategy, "AAPL", "2024-01-15", 100.0, window_end, "RISK_ON"
            )

            assert trade is not None
            assert trade["exit_reason"] == "window_end"
            db.close()

    def test_short_instrument_inverted_pnl(self):
        """stock_short PnL is inverted: profit when price drops."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine, db = self._make_engine(tmpdir)

            # Price drops 5% over the period
            returns = [-0.005] * 30
            engine._forward_price_cache["AAPL"] = _make_price_df(
                "2024-01-15", 30, 100.0, returns
            )

            strategy = _make_strategy(
                instrument="stock_short",
                exit_rules=["time_horizon exceeded"],
                time_horizon_days=10,
            )

            trade = engine._simulate_trade(
                strategy, "AAPL", "2024-01-15", 100.0, "2024-06-15", "RISK_ON"
            )

            assert trade is not None
            assert trade["pnl_pct"] > 0  # short profits when price drops
            db.close()

    def test_no_price_data_returns_none(self):
        """Ticker not in cache returns None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine, db = self._make_engine(tmpdir)

            strategy = _make_strategy()

            trade = engine._simulate_trade(
                strategy, "UNKNOWN", "2024-01-15", 100.0, "2024-03-15", "RISK_ON"
            )

            assert trade is None
            db.close()


# ===========================================================================
# TestBacktestWithRealPrices
# ===========================================================================

class TestBacktestWithRealPrices:
    """Test _backtest_strategy() uses real prices instead of fake ±3%."""

    def _make_engine_with_prices(self, tmpdir, tickers=None, start_price=100.0):
        tickers = tickers or ["AAPL", "MSFT"]
        db = Database(os.path.join(tmpdir, "test.db"))
        config = _make_config()
        engine = EvolutionEngine(db, config)

        # Populate forward price cache with varied returns
        # Use 200 days to cover multiple windows spread across months
        for i, ticker in enumerate(tickers):
            if i % 2 == 0:
                returns = [0.01] * 200  # steady gainer
            else:
                returns = [-0.005] * 200  # steady loser
            engine._forward_price_cache[ticker] = _make_price_df(
                "2024-01-15", 200, start_price, returns
            )

        return engine, db

    @patch.object(MarketScreener, "apply_filters", return_value=True)
    @patch.object(MarketScreener, "batch_fetch")
    def test_uses_real_prices_not_fake(self, mock_batch, mock_filters):
        """Verify PnL comes from real prices, not hardcoded ±3%."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine, db = self._make_engine_with_prices(tmpdir)

            # Pre-populate screener cache
            for ticker in ["AAPL", "MSFT"]:
                sr = _make_screener_result(ticker)
                engine._screener_cache[(ticker, "2024-01-15")] = sr
                engine._screener_cache[(ticker, "2024-04-15")] = sr

            strategy = _make_strategy(
                id=1, name="real_price_test",
                entry_rules=["RSI_14 < 60"],  # will trigger (RSI=55)
                exit_rules=["15% profit target", "10% stop loss", "time_horizon exceeded"],
                time_horizon_days=20,
            )
            # Insert strategy in DB
            sid = db.insert_strategy(
                generation=0, parent_ids=[], name="real_price_test",
                hypothesis="test", conviction=75,
                screener_criteria={}, instrument="stock_long",
                entry_rules=["RSI_14 < 60"],
                exit_rules=["15% profit target", "10% stop loss", "time_horizon exceeded"],
                position_size_pct=0.05, max_risk_pct=0.05,
                time_horizon_days=20, regime_born="RISK_ON", status="proposed",
            )
            strategy.id = sid

            screener_results = [
                _make_screener_result("AAPL"),
                _make_screener_result("MSFT"),
            ]

            windows = [
                WalkForwardWindow("2024-01-01", "2024-01-14", "2024-01-15", "2024-02-15"),
                WalkForwardWindow("2024-02-16", "2024-04-14", "2024-04-15", "2024-05-15"),
            ]

            result = engine._backtest_strategy(
                strategy, windows, ["2024-01-15", "2024-04-15"], screener_results
            )

            assert result.backtest_results is not None
            bt = result.backtest_results
            assert bt.num_trades > 0

            # Key assertion: PnL should NOT be exactly ±3%
            trades = db.get_strategy_trades(sid, trade_type="backtest")
            for trade in trades:
                assert abs(trade["pnl_pct"]) != 0.03, \
                    f"PnL is exactly ±3% — still using fake prices! pnl={trade['pnl_pct']}"

            db.close()

    @patch.object(MarketScreener, "apply_filters", return_value=True)
    @patch.object(MarketScreener, "batch_fetch")
    def test_no_trades_when_rules_dont_trigger(self, mock_batch, mock_filters):
        """Entry rules that never trigger produce 0 trades."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine, db = self._make_engine_with_prices(tmpdir)

            for ticker in ["AAPL", "MSFT"]:
                engine._screener_cache[(ticker, "2024-01-15")] = _make_screener_result(
                    ticker, rsi=70.0  # RSI too high for RSI < 30 rule
                )

            strategy = _make_strategy(
                entry_rules=["RSI_14 < 30"],  # won't trigger (RSI=70)
                exit_rules=["10% stop loss"],
            )

            windows = [
                WalkForwardWindow("2024-01-01", "2024-01-14", "2024-01-15", "2024-02-15"),
            ]

            result = engine._backtest_strategy(
                strategy, windows, ["2024-01-15"],
                [_make_screener_result("AAPL", rsi=70.0),
                 _make_screener_result("MSFT", rsi=70.0)],
            )

            assert result.backtest_results.num_trades == 0
            db.close()

    @patch.object(MarketScreener, "apply_filters", return_value=True)
    @patch.object(MarketScreener, "batch_fetch")
    def test_multiple_windows_aggregate(self, mock_batch, mock_filters):
        """Trades from multiple windows combine into single BacktestResults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine, db = self._make_engine_with_prices(tmpdir)

            for ticker in ["AAPL", "MSFT"]:
                for date in ["2024-01-15", "2024-04-15"]:
                    engine._screener_cache[(ticker, date)] = _make_screener_result(ticker)

            strategy = _make_strategy(
                entry_rules=["RSI_14 < 60"],
                exit_rules=["time_horizon exceeded"],
                time_horizon_days=5,
            )

            windows = [
                WalkForwardWindow("2024-01-01", "2024-01-14", "2024-01-15", "2024-02-15"),
                WalkForwardWindow("2024-02-16", "2024-04-14", "2024-04-15", "2024-05-15"),
            ]

            result = engine._backtest_strategy(
                strategy, windows, ["2024-01-15", "2024-04-15"],
                [_make_screener_result("AAPL"), _make_screener_result("MSFT")],
            )

            bt = result.backtest_results
            # 2 windows × 2 tickers = up to 4 trades
            assert bt.num_trades >= 2
            assert len(bt.walk_forward_scores) == 2  # one Sharpe per window
            db.close()


# ===========================================================================
# TestHoldoutWithRealPrices
# ===========================================================================

class TestHoldoutWithRealPrices:
    """Test _run_holdout() uses real prices."""

    @patch.object(MarketScreener, "apply_filters", return_value=True)
    @patch.object(MarketScreener, "batch_fetch")
    def test_holdout_uses_real_prices(self, mock_batch, mock_filters):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, "test.db"))
            config = _make_config()
            engine = EvolutionEngine(db, config)

            # Add price data
            engine._forward_price_cache["AAPL"] = _make_price_df(
                "2024-06-01", 30, 150.0, [0.01] * 30
            )

            # Add screener cache
            engine._screener_cache[("AAPL", "2024-06-01")] = _make_screener_result("AAPL")

            # Insert strategy in DB
            sid = db.insert_strategy(
                generation=0, parent_ids=[], name="holdout_test",
                hypothesis="test", conviction=75,
                screener_criteria={}, instrument="stock_long",
                entry_rules=["RSI_14 < 60"],
                exit_rules=["time_horizon exceeded"],
                position_size_pct=0.05, max_risk_pct=0.05,
                time_horizon_days=10, regime_born="RISK_ON", status="backtested",
            )
            db.update_strategy_fitness(sid, 1.0)

            # Add backtest results for overfit comparison
            db.insert_strategy_backtest(
                strategy_id=sid, sharpe=1.5, total_return=0.10,
                max_drawdown=-0.05, win_rate=0.60, profit_factor=2.0,
                num_trades=10, tickers_tested=["AAPL"],
                backtest_period="2024-01-01 to 2024-05-30",
                walk_forward_scores=[1.2, 1.5],
            )

            strategies = db.get_top_strategies(limit=5)
            screener_results = [_make_screener_result("AAPL")]

            results = engine._run_holdout(
                strategies, "2024-06-01", "2024-07-01", screener_results
            )

            assert len(results) > 0
            assert "holdout_sharpe" in results[0]
            db.close()


# ===========================================================================
# TestPipelineRulesInBacktest
# ===========================================================================

class TestPipelineRulesInBacktest:
    """Test that pipeline signal rules default to True in backtest mode."""

    def test_buy_signal_defaults_true_in_backtest(self):
        sr = _make_screener_result("AAPL")
        result = _check_entry_rule(
            "BUY signal from pipeline", sr, pipeline_result=None,
            backtest_mode=True,
        )
        assert result is True

    def test_sell_signal_defaults_true_in_backtest(self):
        sr = _make_screener_result("AAPL")
        result = _check_entry_rule(
            "SELL signal from pipeline", sr, pipeline_result=None,
            backtest_mode=True,
        )
        assert result is True

    def test_pipeline_signal_with_actual_result(self):
        """When pipeline_result is provided and not in backtest mode, evaluate normally."""
        sr = _make_screener_result("AAPL")
        result = _check_entry_rule(
            "BUY signal from pipeline", sr,
            pipeline_result={"rating": "BUY"},
            backtest_mode=False,
        )
        assert result is True

        result = _check_entry_rule(
            "BUY signal from pipeline", sr,
            pipeline_result={"rating": "SELL"},
            backtest_mode=False,
        )
        assert result is False

    def test_non_pipeline_rules_unaffected_by_backtest_mode(self):
        """Regular rules work the same regardless of backtest_mode."""
        sr = _make_screener_result("AAPL", rsi=55.0)

        result_bt = _check_entry_rule("RSI_14 < 60", sr, backtest_mode=True)
        result_normal = _check_entry_rule("RSI_14 < 60", sr, backtest_mode=False)
        assert result_bt is True
        assert result_normal is True

        result_bt = _check_entry_rule("RSI_14 < 40", sr, backtest_mode=True)
        assert result_bt is False
