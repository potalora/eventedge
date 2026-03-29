import pytest
from unittest.mock import MagicMock, patch
from tradingagents.backtesting.engine import Backtester
from tradingagents.default_config import DEFAULT_CONFIG


class TestBacktester:
    def _make_config(self):
        config = DEFAULT_CONFIG.copy()
        config["backtest"] = {
            "initial_capital": 5000,
            "max_position_pct": 0.35,
            "max_options_risk_pct": 0.05,
            "slippage_bps": 10,
            "commission_per_trade": 0,
            "trading_frequency": "weekly",
            "accuracy_windows": [5, 10, 30],
        }
        return config

    @patch("tradingagents.backtesting.engine.TradingAgentsGraph")
    @patch("tradingagents.backtesting.engine.yf.download")
    def test_backtest_runs_and_returns_result(self, mock_yf_download, mock_graph_cls):
        import pandas as pd
        import numpy as np

        dates = pd.bdate_range("2025-09-01", "2025-09-30")
        mock_yf_download.return_value = pd.DataFrame({
            "Open": np.random.uniform(10, 12, len(dates)),
            "Close": np.random.uniform(10, 12, len(dates)),
        }, index=dates)

        mock_graph = MagicMock()
        mock_graph.propagate.return_value = (
            {"final_trade_decision": "Rating: BUY\nBuy SOFI"},
            "BUY",
        )
        mock_graph_cls.return_value = mock_graph

        config = self._make_config()
        bt = Backtester(config=config)
        result = bt.run(
            tickers=["SOFI"],
            start_date="2025-09-01",
            end_date="2025-09-30",
        )

        assert result is not None
        assert "metrics" in result
        assert "trade_log" in result
        assert "equity_curve" in result

    @patch("tradingagents.backtesting.engine.TradingAgentsGraph")
    @patch("tradingagents.backtesting.engine.yf.download")
    def test_backtest_respects_weekly_frequency(self, mock_yf_download, mock_graph_cls):
        import pandas as pd
        import numpy as np

        dates = pd.bdate_range("2025-09-01", "2025-09-30")
        mock_yf_download.return_value = pd.DataFrame({
            "Open": np.full(len(dates), 10.0),
            "Close": np.full(len(dates), 10.0),
        }, index=dates)

        mock_graph = MagicMock()
        mock_graph.propagate.return_value = (
            {"final_trade_decision": "Rating: HOLD"},
            "HOLD",
        )
        mock_graph_cls.return_value = mock_graph

        config = self._make_config()
        bt = Backtester(config=config)
        bt.run(tickers=["SOFI"], start_date="2025-09-01", end_date="2025-09-30")

        call_count = mock_graph.propagate.call_count
        assert 3 <= call_count <= 5
