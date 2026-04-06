import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from tradingagents.storage.db import Database
from tradingagents.dashboard.components.formatters import format_rating_badge, format_currency
from tradingagents.dashboard.components.charts import make_equity_curve_chart


class TestFormatters:
    def test_format_rating_badge_buy(self):
        result = format_rating_badge("BUY")
        assert "BUY" in result

    def test_format_rating_badge_sell(self):
        result = format_rating_badge("SELL")
        assert "SELL" in result

    def test_format_currency(self):
        assert format_currency(5000.0) == "$5,000.00"
        assert format_currency(-150.50) == "-$150.50"
        assert format_currency(0) == "$0.00"


class TestLeaderboardPage:
    @patch("tradingagents.dashboard.pages.leaderboard.st")
    def test_render_empty(self, mock_st):
        db = MagicMock()
        mock_st.selectbox.return_value = "All"
        db.get_top_strategies.return_value = []
        from tradingagents.dashboard.pages.leaderboard import render
        render(db)
        mock_st.info.assert_called()

    @patch("tradingagents.dashboard.pages.leaderboard.st")
    def test_render_with_strategies(self, mock_st):
        db = MagicMock()
        db.get_top_strategies.return_value = [
            {"id": 1, "name": "strat1", "instrument": "stock_long",
             "fitness_score": 1.5, "status": "backtested", "generation": 0,
             "hypothesis": "test", "entry_rules": ["RSI > 30"],
             "exit_rules": ["25% stop loss"]},
        ]
        db.get_strategies_by_status.return_value = []
        mock_st.selectbox.side_effect = ["All", "strat1"]
        mock_st.columns.side_effect = [
            [MagicMock(), MagicMock(), MagicMock()],
            [MagicMock(), MagicMock(), MagicMock(), MagicMock()],
        ]
        db.get_strategy_backtest.return_value = {
            "sharpe": 1.5, "win_rate": 0.6, "num_trades": 10, "max_drawdown": -0.05,
        }
        from tradingagents.dashboard.pages.leaderboard import render
        render(db)
        mock_st.dataframe.assert_called()


class TestEvolutionPage:
    @patch("tradingagents.dashboard.pages.evolution.st")
    def test_render_empty(self, mock_st):
        db = MagicMock()
        db.get_reflections.return_value = []
        from tradingagents.dashboard.pages.evolution import render
        render(db)
        mock_st.info.assert_called()

    @patch("tradingagents.dashboard.pages.evolution.st")
    def test_render_with_reflections(self, mock_st):
        db = MagicMock()
        db.get_reflections.return_value = [
            {"generation": 0, "patterns_that_work": ["momentum"],
             "patterns_that_fail": ["mean_rev"],
             "next_generation_guidance": ["try breakouts"],
             "regime_notes": "RISK_ON"},
        ]
        db.get_strategies_by_generation.return_value = [
            {"fitness_score": 1.5},
        ]
        db.get_analyst_weights.return_value = {"market": 1.0, "news": 0.9}
        mock_st.expander.return_value.__enter__ = MagicMock()
        mock_st.expander.return_value.__exit__ = MagicMock()
        from tradingagents.dashboard.pages.evolution import render
        render(db)


class TestPaperTradingPage:
    @patch("tradingagents.dashboard.pages.paper_trading.st")
    def test_render_empty(self, mock_st):
        db = MagicMock()
        db.get_strategies_by_status.return_value = []
        from tradingagents.dashboard.pages.paper_trading import render
        render(db)
        mock_st.info.assert_called()

    @patch("tradingagents.dashboard.pages.paper_trading.st")
    def test_render_with_paper_strategies(self, mock_st):
        db = MagicMock()
        db.get_strategies_by_status.return_value = [
            {"id": 1, "name": "strat1", "instrument": "stock_long",
             "fitness_score": 1.5, "status": "paper", "generation": 0},
        ]
        db.get_strategy_backtest.return_value = {
            "sharpe": 1.5, "win_rate": 0.6, "num_trades": 10,
        }
        db.get_strategy_trades.return_value = [
            {"ticker": "AAPL", "entry_date": "2024-01-01", "exit_date": "2024-01-15",
             "pnl": 50.0, "pnl_pct": 0.03},
        ]
        mock_st.expander.return_value.__enter__ = MagicMock()
        mock_st.expander.return_value.__exit__ = MagicMock()
        from tradingagents.dashboard.pages.paper_trading import render
        render(db)


class TestCharts:
    def test_make_equity_curve_chart_returns_figure(self):
        import pandas as pd
        df = pd.DataFrame({
            "date": pd.date_range("2025-09-01", periods=5, freq="W"),
            "portfolio_value": [5000, 5100, 5050, 5200, 5300],
        })
        fig = make_equity_curve_chart(df)
        assert fig is not None
        assert hasattr(fig, "data")

    def test_make_equity_curve_chart_empty_df(self):
        import pandas as pd
        df = pd.DataFrame(columns=["date", "portfolio_value"])
        fig = make_equity_curve_chart(df)
        assert fig is not None
