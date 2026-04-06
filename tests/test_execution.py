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


from unittest.mock import patch, MagicMock
from tradingagents.execution.alpaca_broker import AlpacaBroker


class TestAlpacaBroker:
    @patch("tradingagents.execution.alpaca_broker.TradingClient")
    def test_get_account(self, mock_client_cls):
        mock_client = MagicMock()
        mock_account = MagicMock()
        mock_account.cash = 5000.0
        mock_account.portfolio_value = 5500.0
        mock_account.buying_power = 5000.0
        mock_client.get_account.return_value = mock_account
        mock_client_cls.return_value = mock_client

        broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
        acct = broker.get_account()
        assert acct.cash == 5000.0

    @patch("tradingagents.execution.alpaca_broker.TimeInForce", new_callable=MagicMock)
    @patch("tradingagents.execution.alpaca_broker.MarketOrderRequest", new_callable=MagicMock)
    @patch("tradingagents.execution.alpaca_broker.OrderSide", new_callable=MagicMock)
    @patch("tradingagents.execution.alpaca_broker.TradingClient")
    def test_submit_stock_order(self, mock_client_cls, mock_order_side, mock_market_req, mock_tif):
        mock_client = MagicMock()
        mock_order = MagicMock()
        mock_order.id = "order-123"
        mock_order.status = "filled"
        mock_order.filled_qty = 100
        mock_order.filled_avg_price = 10.0
        mock_client.submit_order.return_value = mock_order
        mock_client_cls.return_value = mock_client

        broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
        result = broker.submit_stock_order("SOFI", "buy", 100)
        assert result.status == "filled"
        assert result.filled_qty == 100

    @patch("tradingagents.execution.alpaca_broker.TradingClient")
    def test_get_positions(self, mock_client_cls):
        mock_client = MagicMock()
        mock_pos = MagicMock()
        mock_pos.symbol = "SOFI"
        mock_pos.qty = "100"
        mock_pos.avg_entry_price = "10.0"
        mock_pos.asset_class = "us_equity"
        mock_client.get_all_positions.return_value = [mock_pos]
        mock_client_cls.return_value = mock_client

        broker = AlpacaBroker(api_key="test", secret_key="test", paper=True)
        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0]["ticker"] == "SOFI"
