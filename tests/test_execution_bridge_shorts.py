"""Tests for ExecutionBridge short trade routing."""
from __future__ import annotations

from tradingagents.strategies.trading.execution_bridge import ExecutionBridge


class TestExecutionBridgeShorts:
    def _make_bridge(self, long_only=False, capital=50_000):
        config = {
            "execution": {"mode": "paper"},
            "autoresearch": {
                "total_capital": capital,
                "risk_gate": {"long_only": long_only},
            },
        }
        return ExecutionBridge(config)

    def test_short_execution_uses_short_sell(self):
        bridge = self._make_bridge(long_only=False)
        result = bridge.execute_recommendation(
            ticker="AAPL", direction="short", position_size_pct=0.10,
            confidence=0.8, strategy="litigation", current_price=150.0,
        )
        assert result is not None
        assert result.status == "filled"
        assert "AAPL" in bridge.broker.short_positions

    def test_long_execution_still_uses_stock_order(self):
        bridge = self._make_bridge(long_only=False)
        result = bridge.execute_recommendation(
            ticker="MSFT", direction="long", position_size_pct=0.10,
            confidence=0.8, strategy="earnings_call", current_price=300.0,
        )
        assert result is not None
        assert result.status == "filled"
        assert "MSFT" in bridge.broker.positions
        assert "MSFT" not in bridge.broker.short_positions

    def test_short_rejected_when_long_only(self):
        bridge = self._make_bridge(long_only=True)
        result = bridge.execute_recommendation(
            ticker="AAPL", direction="short", position_size_pct=0.10,
            confidence=0.8, strategy="litigation", current_price=150.0,
        )
        assert result is None

    def test_close_short_position(self):
        bridge = self._make_bridge(long_only=False)
        bridge.execute_recommendation(
            ticker="AAPL", direction="short", position_size_pct=0.10,
            confidence=0.8, strategy="litigation", current_price=150.0,
        )
        qty = bridge.broker.short_positions["AAPL"]["quantity"]
        result = bridge.close_position("AAPL", shares=qty, current_price=140.0, direction="short")
        assert result.status == "filled"
        assert "AAPL" not in bridge.broker.short_positions

    def test_close_long_position(self):
        bridge = self._make_bridge(long_only=False)
        bridge.execute_recommendation(
            ticker="MSFT", direction="long", position_size_pct=0.10,
            confidence=0.8, strategy="earnings_call", current_price=300.0,
        )
        qty = bridge.broker.positions["MSFT"]["quantity"]
        result = bridge.close_position("MSFT", shares=qty, current_price=310.0, direction="long")
        assert result.status == "filled"
