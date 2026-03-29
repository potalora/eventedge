import pytest
from tradingagents.execution.base_broker import BaseBroker, OrderResult, AccountInfo
from tradingagents.execution.paper_broker import PaperBroker


class TestPaperBroker:
    def test_initial_account(self):
        broker = PaperBroker(initial_capital=5000.0)
        acct = broker.get_account()
        assert acct.cash == 5000.0
        assert acct.portfolio_value == 5000.0

    def test_submit_stock_buy(self):
        broker = PaperBroker(initial_capital=5000.0)
        result = broker.submit_stock_order("SOFI", "buy", 100, "market", price=10.0)
        assert result.status == "filled"
        assert result.filled_qty == 100
        acct = broker.get_account()
        assert acct.cash == 4000.0

    def test_submit_stock_sell(self):
        broker = PaperBroker(initial_capital=5000.0)
        broker.submit_stock_order("SOFI", "buy", 100, "market", price=10.0)
        result = broker.submit_stock_order("SOFI", "sell", 100, "market", price=12.0)
        assert result.status == "filled"
        acct = broker.get_account()
        assert acct.cash == 5200.0

    def test_get_positions(self):
        broker = PaperBroker(initial_capital=5000.0)
        broker.submit_stock_order("SOFI", "buy", 100, "market", price=10.0)
        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0]["ticker"] == "SOFI"
        assert positions[0]["quantity"] == 100

    def test_insufficient_funds_rejected(self):
        broker = PaperBroker(initial_capital=100.0)
        result = broker.submit_stock_order("NVDA", "buy", 10, "market", price=500.0)
        assert result.status == "rejected"

    def test_cancel_order(self):
        broker = PaperBroker(initial_capital=5000.0)
        assert broker.cancel_order("fake-id") is False

    def test_submit_options_order(self):
        broker = PaperBroker(initial_capital=5000.0)
        result = broker.submit_options_order(
            symbol="SOFI", expiry="2026-06-20", strike=10.0,
            right="call", side="buy", qty=2, price=1.20,
        )
        assert result.status == "filled"
        acct = broker.get_account()
        assert acct.cash == 5000.0 - 2 * 100 * 1.20
