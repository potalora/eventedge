"""Tests for PaperBroker short position tracking."""
from __future__ import annotations

from tradingagents.execution.paper_broker import PaperBroker


class TestPaperBrokerShorts:
    def test_short_sell_opens_position(self):
        broker = PaperBroker(initial_capital=50_000)
        result = broker.submit_short_sell("AAPL", qty=10, price=150.0, stop_loss=172.5)
        assert result.status == "filled"
        assert result.filled_qty == 10
        assert result.filled_price == 150.0
        assert broker.cash == 50_000  # cash unchanged on short open
        assert broker.margin_used > 0

    def test_short_sell_reserves_margin(self):
        broker = PaperBroker(initial_capital=50_000)
        broker.submit_short_sell("AAPL", qty=10, price=150.0, stop_loss=172.5)
        expected_margin = 10 * 150.0 * 1.5  # Reg T
        assert broker.margin_used == expected_margin

    def test_short_sell_rejected_insufficient_margin(self):
        broker = PaperBroker(initial_capital=5_000)
        result = broker.submit_short_sell("AAPL", qty=100, price=150.0)
        assert result.status == "rejected"
        assert broker.margin_used == 0

    def test_cover_short_with_profit(self):
        broker = PaperBroker(initial_capital=50_000)
        broker.submit_short_sell("AAPL", qty=10, price=150.0, stop_loss=172.5)
        result = broker.submit_cover("AAPL", qty=10, price=140.0)
        assert result.status == "filled"
        assert broker.cash == 50_000 + 100  # (150-140)*10
        assert broker.margin_used == 0

    def test_cover_short_with_loss(self):
        broker = PaperBroker(initial_capital=50_000)
        broker.submit_short_sell("AAPL", qty=10, price=150.0, stop_loss=172.5)
        result = broker.submit_cover("AAPL", qty=10, price=160.0)
        assert result.status == "filled"
        assert broker.cash == 50_000 - 100  # (150-160)*10 = loss
        assert broker.margin_used == 0

    def test_cover_nonexistent_short_rejected(self):
        broker = PaperBroker(initial_capital=50_000)
        result = broker.submit_cover("AAPL", qty=10, price=140.0)
        assert result.status == "rejected"

    def test_short_positions_in_get_positions(self):
        broker = PaperBroker(initial_capital=50_000)
        broker.submit_stock_order("MSFT", "buy", 5, price=300.0)
        broker.submit_short_sell("AAPL", qty=10, price=150.0)
        positions = broker.get_positions()
        tickers = {p["ticker"] for p in positions}
        assert "MSFT" in tickers
        assert "AAPL" in tickers
        short_pos = next(p for p in positions if p["ticker"] == "AAPL")
        assert short_pos.get("side") == "short"

    def test_account_includes_short_impact(self):
        broker = PaperBroker(initial_capital=50_000)
        broker.submit_short_sell("AAPL", qty=10, price=150.0)
        account = broker.get_account()
        assert account.buying_power < 50_000  # reduced by margin

    def test_accrue_borrow_cost(self):
        broker = PaperBroker(initial_capital=50_000)
        broker.submit_short_sell("AAPL", qty=10, price=150.0)
        broker.accrue_borrow_cost("2026-04-04", borrow_rates={"AAPL": 0.02})
        expected_daily = (10 * 150.0) * 0.02 / 365
        assert abs(broker.accrued_borrow_cost - expected_daily) < 0.01
        assert broker.cash < 50_000

    def test_reconstruct_includes_shorts(self):
        broker = PaperBroker(initial_capital=50_000)
        open_trades = [
            {"ticker": "AAPL", "shares": 10, "entry_price": 150.0, "direction": "short"},
            {"ticker": "MSFT", "shares": 5, "entry_price": 300.0, "direction": "long"},
        ]
        broker.reconstruct_from_trades(open_trades)
        positions = broker.get_positions()
        assert len(positions) == 2
        short_pos = next(p for p in positions if p["ticker"] == "AAPL")
        assert short_pos["side"] == "short"
        long_pos = next(p for p in positions if p["ticker"] == "MSFT")
        assert long_pos.get("side") != "short"
