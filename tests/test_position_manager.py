import pytest
from tradingagents.execution.position_manager import PositionManager
from tradingagents.execution.paper_broker import PaperBroker


class TestPositionManager:
    def _make_pm(self, capital=5000.0):
        broker = PaperBroker(initial_capital=capital)
        config = {
            "execution": {
                "daily_loss_limit_pct": 0.10,
                "execution_enabled": True,
                "confirm_before_trade": False,
            },
            "backtest": {
                "max_position_pct": 0.35,
                "max_options_risk_pct": 0.05,
            },
        }
        return PositionManager(broker=broker, config=config)

    def test_parse_buy_stock_decision(self):
        pm = self._make_pm()
        orders = pm.parse_decision(
            decision_text="Rating: BUY\nBuy 100 shares of SOFI at $10",
            rating="BUY", ticker="SOFI", current_price=10.0,
        )
        assert len(orders) >= 1
        assert orders[0]["action"] == "buy"
        assert orders[0]["ticker"] == "SOFI"

    def test_parse_sell_decision(self):
        pm = self._make_pm()
        orders = pm.parse_decision(
            decision_text="Rating: SELL\nExit all SOFI positions",
            rating="SELL", ticker="SOFI", current_price=10.0,
        )
        assert len(orders) >= 1
        assert orders[0]["action"] == "sell"

    def test_parse_hold_decision_no_orders(self):
        pm = self._make_pm()
        orders = pm.parse_decision(
            decision_text="Rating: HOLD\nMaintain position",
            rating="HOLD", ticker="SOFI", current_price=10.0,
        )
        assert len(orders) == 0

    def test_risk_check_rejects_oversized_position(self):
        pm = self._make_pm(capital=5000.0)
        passed, reason = pm.check_risk(
            ticker="SOFI", action="buy", instrument_type="stock",
            quantity=400, price=10.0,
        )
        assert passed is False
        assert "position" in reason.lower()

    def test_risk_check_passes_within_limits(self):
        pm = self._make_pm(capital=5000.0)
        passed, reason = pm.check_risk(
            ticker="SOFI", action="buy", instrument_type="stock",
            quantity=100, price=10.0,
        )
        assert passed is True

    def test_execute_decision_end_to_end(self):
        pm = self._make_pm(capital=5000.0)
        results = pm.execute_decision(
            decision_text="Rating: BUY\nBuy SOFI shares",
            rating="BUY", ticker="SOFI", current_price=10.0,
        )
        assert len(results) >= 1
        assert results[0].status == "filled"
