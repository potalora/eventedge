"""Tests for tradingagents.strategies.state.models."""
import json
import pytest
from datetime import datetime

from tradingagents.strategies.state.models import (
    Filter,
    ScreenerCriteria,
    BacktestResults,
    ScreenerResult,
    Strategy,
)


class TestFilter:
    def test_less_than_true(self):
        f = Filter(field="rsi_14", op="<", value=30.0)
        assert f.evaluate(25.0) is True

    def test_less_than_false(self):
        f = Filter(field="rsi_14", op="<", value=30.0)
        assert f.evaluate(35.0) is False

    def test_greater_than_true(self):
        f = Filter(field="rsi_14", op=">", value=70.0)
        assert f.evaluate(75.0) is True

    def test_greater_than_false(self):
        f = Filter(field="rsi_14", op=">", value=70.0)
        assert f.evaluate(65.0) is False

    def test_less_than_or_equal_true(self):
        f = Filter(field="rsi_14", op="<=", value=30.0)
        assert f.evaluate(30.0) is True

    def test_less_than_or_equal_false(self):
        f = Filter(field="rsi_14", op="<=", value=30.0)
        assert f.evaluate(30.1) is False

    def test_greater_than_or_equal_true(self):
        f = Filter(field="rsi_14", op=">=", value=70.0)
        assert f.evaluate(70.0) is True

    def test_greater_than_or_equal_false(self):
        f = Filter(field="rsi_14", op=">=", value=70.0)
        assert f.evaluate(69.9) is False

    def test_equal_true(self):
        f = Filter(field="rsi_14", op="==", value=50.0)
        assert f.evaluate(50.0) is True

    def test_equal_false(self):
        f = Filter(field="rsi_14", op="==", value=50.0)
        assert f.evaluate(50.1) is False

    def test_between_true(self):
        f = Filter(field="rsi_14", op="between", value=[30.0, 70.0])
        assert f.evaluate(50.0) is True

    def test_between_boundary_low(self):
        f = Filter(field="rsi_14", op="between", value=[30.0, 70.0])
        assert f.evaluate(30.0) is True

    def test_between_boundary_high(self):
        f = Filter(field="rsi_14", op="between", value=[30.0, 70.0])
        assert f.evaluate(70.0) is True

    def test_between_false_below(self):
        f = Filter(field="rsi_14", op="between", value=[30.0, 70.0])
        assert f.evaluate(29.9) is False

    def test_between_false_above(self):
        f = Filter(field="rsi_14", op="between", value=[30.0, 70.0])
        assert f.evaluate(70.1) is False

    def test_unknown_op_returns_false(self):
        f = Filter(field="rsi_14", op="!=", value=50.0)
        assert f.evaluate(99.0) is False


class TestScreenerCriteria:
    def test_default_construction(self):
        sc = ScreenerCriteria()
        assert sc.market_cap_range == [0, float("inf")]
        assert sc.min_avg_volume == 100_000
        assert sc.sector is None
        assert sc.min_options_volume is None
        assert sc.custom_filters == []

    def test_to_dict_defaults(self):
        sc = ScreenerCriteria()
        d = sc.to_dict()
        assert d["market_cap_range"] == [0, float("inf")]
        assert d["min_avg_volume"] == 100_000
        assert d["sector"] is None
        assert d["min_options_volume"] is None
        assert d["custom_filters"] == []

    def test_from_dict_defaults(self):
        sc = ScreenerCriteria.from_dict({})
        assert sc.market_cap_range == [0, float("inf")]
        assert sc.min_avg_volume == 100_000
        assert sc.sector is None
        assert sc.min_options_volume is None
        assert sc.custom_filters == []

    def test_to_dict_from_dict_round_trip(self):
        sc = ScreenerCriteria(
            market_cap_range=[1e9, 1e12],
            min_avg_volume=500_000,
            sector="Technology",
            min_options_volume=10_000,
        )
        d = sc.to_dict()
        sc2 = ScreenerCriteria.from_dict(d)
        assert sc2.market_cap_range == [1e9, 1e12]
        assert sc2.min_avg_volume == 500_000
        assert sc2.sector == "Technology"
        assert sc2.min_options_volume == 10_000

    def test_with_custom_filters_round_trip(self):
        filters = [
            Filter(field="rsi_14", op="<", value=30.0),
            Filter(field="volume_ratio", op=">", value=2.0),
            Filter(field="close", op="between", value=[10.0, 500.0]),
        ]
        sc = ScreenerCriteria(custom_filters=filters)
        d = sc.to_dict()
        sc2 = ScreenerCriteria.from_dict(d)
        assert len(sc2.custom_filters) == 3
        assert sc2.custom_filters[0].field == "rsi_14"
        assert sc2.custom_filters[0].op == "<"
        assert sc2.custom_filters[0].value == 30.0
        assert sc2.custom_filters[2].op == "between"
        assert sc2.custom_filters[2].value == [10.0, 500.0]


class TestBacktestResults:
    def test_default_construction(self):
        br = BacktestResults()
        assert br.sharpe == 0.0
        assert br.total_return == 0.0
        assert br.max_drawdown == 0.0
        assert br.win_rate == 0.0
        assert br.profit_factor == 0.0
        assert br.num_trades == 0
        assert br.tickers_tested == []
        assert br.backtest_period == ""
        assert br.walk_forward_scores == []
        assert br.holdout_sharpe is None

    def test_custom_construction(self):
        br = BacktestResults(
            sharpe=1.5,
            total_return=0.35,
            max_drawdown=-0.12,
            win_rate=0.6,
            profit_factor=1.8,
            num_trades=42,
            tickers_tested=["AAPL", "MSFT"],
            backtest_period="2023-01-01 to 2024-01-01",
            walk_forward_scores=[1.2, 1.4, 1.6],
            holdout_sharpe=1.3,
        )
        assert br.sharpe == 1.5
        assert br.num_trades == 42
        assert br.tickers_tested == ["AAPL", "MSFT"]
        assert br.holdout_sharpe == 1.3


class TestScreenerResult:
    def test_construction_with_all_fields(self):
        sr = ScreenerResult(
            ticker="AAPL",
            close=175.0,
            change_14d=0.05,
            change_30d=0.10,
            high_52w=200.0,
            low_52w=140.0,
            avg_volume_20d=5_000_000,
            volume_ratio=1.2,
            rsi_14=55.0,
            ema_10=172.0,
            ema_50=165.0,
            macd=1.5,
            boll_position=0.6,
            iv_rank=45.0,
            put_call_ratio=0.8,
            options_volume=200_000,
            market_cap=2.8e12,
            sector="Technology",
            revenue_growth_yoy=0.08,
            next_earnings_date="2026-04-30",
            regime="RISK_ON",
            trading_day_coverage=0.95,
        )
        assert sr.ticker == "AAPL"
        assert sr.close == 175.0
        assert sr.sector == "Technology"
        assert sr.regime == "RISK_ON"

    def test_construction_with_none_optionals(self):
        sr = ScreenerResult(
            ticker="XYZ",
            close=50.0,
            change_14d=0.01,
            change_30d=0.02,
            high_52w=60.0,
            low_52w=40.0,
            avg_volume_20d=200_000,
            volume_ratio=1.0,
            rsi_14=50.0,
            ema_10=49.0,
            ema_50=48.0,
            macd=0.2,
            boll_position=0.5,
            iv_rank=None,
            put_call_ratio=None,
            options_volume=None,
            market_cap=1e9,
            sector="Healthcare",
            revenue_growth_yoy=None,
            next_earnings_date=None,
            regime="TRANSITION",
            trading_day_coverage=0.90,
        )
        assert sr.iv_rank is None
        assert sr.put_call_ratio is None
        assert sr.options_volume is None


class TestStrategy:
    def test_default_construction(self):
        s = Strategy()
        assert s.id == 0
        assert s.generation == 0
        assert s.parent_ids == []
        assert s.name == ""
        assert isinstance(s.screener, ScreenerCriteria)
        assert s.instrument == "stock_long"
        assert s.entry_rules == []
        assert s.exit_rules == []
        assert s.position_size_pct == 0.05
        assert s.max_risk_pct == 0.05
        assert s.time_horizon_days == 30
        assert s.hypothesis == ""
        assert s.conviction == 50
        assert s.backtest_results is None
        assert s.status == "proposed"
        assert s.regime_born == "TRANSITION"
        assert s.fitness_score == 0.0
        assert isinstance(s.created_at, datetime)

    def test_to_db_dict_serializes_json_fields(self):
        s = Strategy(
            generation=2,
            parent_ids=[1, 3],
            name="Momentum Play",
            hypothesis="RSI oversold bounce",
            conviction=75,
            instrument="stock_long",
            entry_rules=["RSI < 30", "volume_ratio > 2"],
            exit_rules=["RSI > 60", "stop_loss -5%"],
            position_size_pct=0.03,
            max_risk_pct=0.02,
            time_horizon_days=14,
            regime_born="RISK_ON",
            status="backtested",
        )
        d = s.to_db_dict()
        assert d["generation"] == 2
        assert json.loads(d["parent_ids"]) == [1, 3]
        assert d["name"] == "Momentum Play"
        assert d["hypothesis"] == "RSI oversold bounce"
        assert d["conviction"] == 75
        assert json.loads(d["entry_rules"]) == ["RSI < 30", "volume_ratio > 2"]
        assert json.loads(d["exit_rules"]) == ["RSI > 60", "stop_loss -5%"]
        assert d["position_size_pct"] == 0.03
        assert d["max_risk_pct"] == 0.02
        assert d["time_horizon_days"] == 14
        assert d["regime_born"] == "RISK_ON"
        assert d["status"] == "backtested"
        # screener_criteria should be valid JSON
        sc_dict = json.loads(d["screener_criteria"])
        assert "market_cap_range" in sc_dict

    def test_from_db_dict_round_trip(self):
        s = Strategy(
            generation=1,
            parent_ids=[5, 7],
            name="Mean Reversion",
            hypothesis="Bollinger band reversion",
            conviction=60,
            screener=ScreenerCriteria(
                market_cap_range=[1e9, 1e12],
                min_avg_volume=250_000,
                sector="Financials",
            ),
            instrument="stock_long",
            entry_rules=["boll_position < 0.1"],
            exit_rules=["boll_position > 0.5"],
            position_size_pct=0.04,
            max_risk_pct=0.03,
            time_horizon_days=7,
            regime_born="RISK_OFF",
            status="proposed",
        )
        db_dict = s.to_db_dict()
        # Simulate DB row with an id added
        db_dict["id"] = 42
        s2 = Strategy.from_db_dict(db_dict)

        assert s2.id == 42
        assert s2.generation == 1
        assert s2.parent_ids == [5, 7]
        assert s2.name == "Mean Reversion"
        assert s2.hypothesis == "Bollinger band reversion"
        assert s2.conviction == 60
        assert s2.screener.sector == "Financials"
        assert s2.screener.min_avg_volume == 250_000
        assert s2.instrument == "stock_long"
        assert s2.entry_rules == ["boll_position < 0.1"]
        assert s2.exit_rules == ["boll_position > 0.5"]
        assert s2.position_size_pct == 0.04
        assert s2.max_risk_pct == 0.03
        assert s2.time_horizon_days == 7
        assert s2.regime_born == "RISK_OFF"
        assert s2.status == "proposed"

    def test_to_prompt_str_basic(self):
        s = Strategy(
            name="Test Strategy",
            generation=0,
            status="proposed",
            instrument="stock_long",
            hypothesis="This is a test hypothesis",
            entry_rules=["RSI < 30"],
            exit_rules=["RSI > 60"],
            position_size_pct=0.05,
            max_risk_pct=0.02,
            time_horizon_days=21,
            conviction=80,
        )
        prompt = s.to_prompt_str()
        assert "Test Strategy" in prompt
        assert "gen 0" in prompt
        assert "proposed" in prompt
        assert "stock_long" in prompt
        assert "This is a test hypothesis" in prompt
        assert "RSI < 30" in prompt
        assert "RSI > 60" in prompt
        assert "5%" in prompt
        assert "2%" in prompt
        assert "21 days" in prompt
        assert "80/100" in prompt

    def test_to_prompt_str_no_backtest(self):
        s = Strategy(name="No Backtest")
        prompt = s.to_prompt_str()
        assert "Backtest:" not in prompt

    def test_to_prompt_str_with_backtest_results(self):
        br = BacktestResults(
            sharpe=1.75,
            total_return=0.42,
            win_rate=0.65,
            num_trades=30,
            max_drawdown=-0.08,
            profit_factor=2.1,
        )
        s = Strategy(name="Winning Strategy", backtest_results=br)
        prompt = s.to_prompt_str()
        assert "Backtest:" in prompt
        assert "Sharpe=1.75" in prompt
        assert "Return=42.0%" in prompt
        assert "Win=65%" in prompt
        assert "Trades=30" in prompt
        assert "MaxDD=-8.0%" in prompt
        assert "PF=2.10" in prompt

    def test_to_prompt_str_with_fitness_score(self):
        s = Strategy(name="Fit Strategy", fitness_score=0.8523)
        prompt = s.to_prompt_str()
        assert "Fitness: 0.8523" in prompt

    def test_to_prompt_str_no_fitness_when_zero(self):
        s = Strategy(name="No Fitness")
        prompt = s.to_prompt_str()
        assert "Fitness:" not in prompt
