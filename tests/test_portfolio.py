# tests/test_portfolio.py
import pytest
from tradingagents.backtesting.portfolio import Portfolio, Order, Position


class TestPortfolio:
    def test_initial_state(self):
        p = Portfolio(initial_capital=5000.0)
        assert p.cash == 5000.0
        assert p.get_total_value({}) == 5000.0
        assert len(p.positions) == 0

    def test_buy_stock(self):
        p = Portfolio(initial_capital=5000.0)
        order = Order(
            ticker="SOFI", action="buy", quantity=100,
            instrument_type="stock", price=10.0,
        )
        p.execute_order(order, fill_price=10.0, date="2026-01-15", slippage_bps=0)
        assert p.cash == 4000.0
        assert len(p.positions) == 1
        assert p.positions["SOFI_stock"].quantity == 100

    def test_sell_stock(self):
        p = Portfolio(initial_capital=5000.0)
        buy = Order(ticker="SOFI", action="buy", quantity=100, instrument_type="stock", price=10.0)
        p.execute_order(buy, fill_price=10.0, date="2026-01-15", slippage_bps=0)

        sell = Order(ticker="SOFI", action="sell", quantity=100, instrument_type="stock", price=12.0)
        p.execute_order(sell, fill_price=12.0, date="2026-02-15", slippage_bps=0)

        assert p.cash == 5200.0
        assert p.positions["SOFI_stock"].quantity == 0

    def test_total_value_with_positions(self):
        p = Portfolio(initial_capital=5000.0)
        buy = Order(ticker="SOFI", action="buy", quantity=100, instrument_type="stock", price=10.0)
        p.execute_order(buy, fill_price=10.0, date="2026-01-15", slippage_bps=0)

        prices = {"SOFI": 12.0}
        assert p.get_total_value(prices) == 4000.0 + 100 * 12.0

    def test_slippage_applied(self):
        p = Portfolio(initial_capital=5000.0)
        buy = Order(ticker="SOFI", action="buy", quantity=100, instrument_type="stock", price=10.0)
        p.execute_order(buy, fill_price=10.0, date="2026-01-15", slippage_bps=10)
        assert p.cash == pytest.approx(5000.0 - 100 * 10.01, abs=0.01)

    def test_trade_log(self):
        p = Portfolio(initial_capital=5000.0)
        buy = Order(ticker="SOFI", action="buy", quantity=100, instrument_type="stock", price=10.0)
        p.execute_order(buy, fill_price=10.0, date="2026-01-15", slippage_bps=0)
        assert len(p.trade_log) == 1
        assert p.trade_log[0]["ticker"] == "SOFI"
        assert p.trade_log[0]["action"] == "buy"

    def test_equity_curve(self):
        p = Portfolio(initial_capital=5000.0)
        p.record_snapshot("2026-01-15", {"SOFI": 10.0})
        buy = Order(ticker="SOFI", action="buy", quantity=100, instrument_type="stock", price=10.0)
        p.execute_order(buy, fill_price=10.0, date="2026-01-15", slippage_bps=0)
        p.record_snapshot("2026-01-16", {"SOFI": 11.0})
        curve = p.get_equity_curve()
        assert len(curve) == 2
        assert curve[1]["portfolio_value"] == 4000.0 + 100 * 11.0

    def test_buy_options(self):
        p = Portfolio(initial_capital=5000.0)
        order = Order(
            ticker="SOFI", action="buy", quantity=2,
            instrument_type="option", price=1.20,
            option_details={"strike": 10.0, "expiry": "2026-06-20", "right": "call"},
        )
        p.execute_order(order, fill_price=1.20, date="2026-03-01", slippage_bps=0)
        assert p.cash == 5000.0 - 2 * 100 * 1.20
