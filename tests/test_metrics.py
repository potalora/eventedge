import pytest
import pandas as pd
from tradingagents.backtesting.metrics import compute_metrics


class TestComputeMetrics:
    def test_basic_metrics(self):
        equity_curve = pd.DataFrame({
            "date": pd.date_range("2025-09-01", periods=10, freq="W"),
            "portfolio_value": [5000, 5100, 5050, 5200, 5300, 5150, 5400, 5500, 5350, 5600],
        })
        trade_log = [
            {"ticker": "SOFI", "action": "buy", "fill_price": 10.0, "date": "2025-09-01", "pnl": 150.0},
            {"ticker": "SOFI", "action": "sell", "fill_price": 11.5, "date": "2025-09-15", "pnl": 150.0},
            {"ticker": "PLTR", "action": "buy", "fill_price": 20.0, "date": "2025-10-01", "pnl": -50.0},
            {"ticker": "PLTR", "action": "sell", "fill_price": 19.0, "date": "2025-10-15", "pnl": -50.0},
        ]
        m = compute_metrics(equity_curve, trade_log)
        assert "total_return" in m
        assert "max_drawdown" in m
        assert "win_rate" in m
        assert "sharpe_ratio" in m
        assert m["total_return"] == pytest.approx(0.12, abs=0.01)

    def test_empty_trade_log(self):
        equity_curve = pd.DataFrame({
            "date": pd.date_range("2025-09-01", periods=5, freq="W"),
            "portfolio_value": [5000, 5000, 5000, 5000, 5000],
        })
        m = compute_metrics(equity_curve, [])
        assert m["total_trades"] == 0
        assert m["win_rate"] == 0.0

    def test_all_winners(self):
        equity_curve = pd.DataFrame({
            "date": pd.date_range("2025-09-01", periods=3, freq="W"),
            "portfolio_value": [5000, 5200, 5500],
        })
        trade_log = [
            {"ticker": "A", "action": "sell", "fill_price": 12, "date": "2025-09-08", "pnl": 200.0},
            {"ticker": "B", "action": "sell", "fill_price": 15, "date": "2025-09-15", "pnl": 300.0},
        ]
        m = compute_metrics(equity_curve, trade_log)
        assert m["win_rate"] == 1.0
