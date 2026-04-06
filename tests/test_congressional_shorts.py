"""Tests for congressional_trades sale/short signals."""
from __future__ import annotations

from tradingagents.strategies.modules.congressional_trades import CongressionalTradesStrategy


class TestCongressionalShortSignals:
    def _make_data(self, trades_list):
        return {"congress": {"trades": trades_list}}

    def test_sale_cluster_generates_short(self):
        trades = [
            {"ticker": "XYZ", "transaction_type": "sale", "amount": "$15,001 - $50,000",
             "representative": "Rep A", "chamber": "house", "transaction_date": "2026-04-01"},
            {"ticker": "XYZ", "transaction_type": "sale", "amount": "$50,001 - $100,000",
             "representative": "Rep B", "chamber": "house", "transaction_date": "2026-04-01"},
        ]
        strategy = CongressionalTradesStrategy()
        candidates = strategy.screen(self._make_data(trades), "2026-04-04", strategy.get_default_params())
        shorts = [c for c in candidates if c.direction == "short"]
        assert len(shorts) >= 1
        assert shorts[0].ticker == "XYZ"

    def test_purchases_still_generate_long(self):
        trades = [
            {"ticker": "MSFT", "transaction_type": "purchase", "amount": "$15,001 - $50,000",
             "representative": "Rep A", "chamber": "house", "transaction_date": "2026-04-01"},
            {"ticker": "MSFT", "transaction_type": "purchase", "amount": "$50,001 - $100,000",
             "representative": "Rep B", "chamber": "senate", "transaction_date": "2026-04-01"},
        ]
        strategy = CongressionalTradesStrategy()
        candidates = strategy.screen(self._make_data(trades), "2026-04-04", strategy.get_default_params())
        longs = [c for c in candidates if c.direction == "long"]
        assert len(longs) >= 1
        assert longs[0].ticker == "MSFT"

    def test_mixed_buy_sell_both_directions(self):
        trades = [
            {"ticker": "AAPL", "transaction_type": "purchase", "amount": "$15,001 - $50,000",
             "representative": "Rep A", "chamber": "house", "transaction_date": "2026-04-01"},
            {"ticker": "AAPL", "transaction_type": "purchase", "amount": "$15,001 - $50,000",
             "representative": "Rep B", "chamber": "house", "transaction_date": "2026-04-01"},
            {"ticker": "TSLA", "transaction_type": "sale", "amount": "$50,001 - $100,000",
             "representative": "Rep C", "chamber": "senate", "transaction_date": "2026-04-01"},
            {"ticker": "TSLA", "transaction_type": "sale", "amount": "$50,001 - $100,000",
             "representative": "Rep D", "chamber": "house", "transaction_date": "2026-04-01"},
        ]
        strategy = CongressionalTradesStrategy()
        candidates = strategy.screen(self._make_data(trades), "2026-04-04", strategy.get_default_params())
        longs = [c for c in candidates if c.direction == "long"]
        shorts = [c for c in candidates if c.direction == "short"]
        assert any(c.ticker == "AAPL" for c in longs)
        assert any(c.ticker == "TSLA" for c in shorts)

    def test_sale_partial_recognized(self):
        """Sale (Partial) and Sale (Full) transaction types should be recognized."""
        trades = [
            {"ticker": "XYZ", "transaction_type": "Sale (Partial)", "amount": "$15,001 - $50,000",
             "representative": "Rep A", "chamber": "house", "transaction_date": "2026-04-01"},
            {"ticker": "XYZ", "transaction_type": "Sale (Full)", "amount": "$15,001 - $50,000",
             "representative": "Rep B", "chamber": "house", "transaction_date": "2026-04-01"},
        ]
        strategy = CongressionalTradesStrategy()
        candidates = strategy.screen(self._make_data(trades), "2026-04-04", strategy.get_default_params())
        shorts = [c for c in candidates if c.direction == "short"]
        assert len(shorts) >= 1
