"""Tests for short-specific risk gates."""
from __future__ import annotations

from unittest.mock import MagicMock
from tradingagents.strategies.trading.risk_gate import RiskGate, RiskGateConfig, _estimate_borrow_cost
from tradingagents.execution.base_broker import AccountInfo


def _make_broker(cash=50_000, portfolio_value=50_000, positions=None):
    broker = MagicMock()
    broker.get_account.return_value = AccountInfo(
        cash=cash, portfolio_value=portfolio_value, buying_power=cash,
    )
    broker.get_positions.return_value = positions or []
    return broker


class TestBorrowCostEstimation:
    def test_low_si(self):
        assert _estimate_borrow_cost(3.0) == 0.005

    def test_medium_si(self):
        assert _estimate_borrow_cost(10.0) == 0.02

    def test_high_si(self):
        assert _estimate_borrow_cost(25.0) == 0.05

    def test_hard_to_borrow(self):
        assert _estimate_borrow_cost(35.0) == 0.10


class TestShortRiskGates:
    def test_long_only_blocks_shorts(self):
        broker = _make_broker()
        gate = RiskGate(RiskGateConfig(long_only=True, total_capital=50_000), broker)
        passed, reason = gate.check("AAPL", "short", 5000, "litigation")
        assert not passed
        assert "long_only" in reason

    def test_short_allowed_when_not_long_only(self):
        broker = _make_broker()
        gate = RiskGate(RiskGateConfig(long_only=False, total_capital=50_000), broker)
        passed, _ = gate.check("AAPL", "short", 5000, "litigation")
        assert passed

    def test_earnings_blackout_blocks_short(self):
        broker = _make_broker()
        config = RiskGateConfig(long_only=False, total_capital=50_000, earnings_blackout_days=5)
        gate = RiskGate(config, broker)
        passed, reason = gate.check("AAPL", "short", 5000, "litigation", earnings_dates={"AAPL": 3})
        assert not passed
        assert "earnings_blackout" in reason

    def test_earnings_blackout_allows_long(self):
        broker = _make_broker()
        config = RiskGateConfig(long_only=False, total_capital=50_000, earnings_blackout_days=5)
        gate = RiskGate(config, broker)
        passed, _ = gate.check("AAPL", "long", 5000, "earnings_call", earnings_dates={"AAPL": 3})
        assert passed

    def test_earnings_blackout_allows_distant_earnings(self):
        broker = _make_broker()
        config = RiskGateConfig(long_only=False, total_capital=50_000, earnings_blackout_days=5)
        gate = RiskGate(config, broker)
        passed, _ = gate.check("AAPL", "short", 5000, "litigation", earnings_dates={"AAPL": 15})
        assert passed

    def test_borrow_cost_blocks_expensive_short(self):
        broker = _make_broker()
        config = RiskGateConfig(long_only=False, total_capital=50_000, max_borrow_cost_pct=0.05)
        gate = RiskGate(config, broker)
        passed, reason = gate.check("GME", "short", 5000, "litigation", short_interest={"GME": 35.0})
        assert not passed
        assert "borrow_cost" in reason

    def test_borrow_cost_allows_cheap_short(self):
        broker = _make_broker()
        config = RiskGateConfig(long_only=False, total_capital=50_000, max_borrow_cost_pct=0.05)
        gate = RiskGate(config, broker)
        passed, _ = gate.check("AAPL", "short", 5000, "litigation", short_interest={"AAPL": 3.0})
        assert passed

    def test_margin_utilization_blocks_when_high(self):
        broker = _make_broker()
        config = RiskGateConfig(long_only=False, total_capital=50_000, max_margin_utilization_pct=0.70)
        gate = RiskGate(config, broker)
        gate._margin_used = 37_500  # 75% of 50k
        passed, reason = gate.check("TSLA", "short", 5000, "litigation")
        assert not passed
        assert "margin_utilization" in reason

    def test_margin_utilization_allows_when_low(self):
        broker = _make_broker()
        config = RiskGateConfig(long_only=False, total_capital=50_000, max_margin_utilization_pct=0.70)
        gate = RiskGate(config, broker)
        gate._margin_used = 10_000  # 20%
        passed, _ = gate.check("TSLA", "short", 5000, "litigation")
        assert passed

    def test_disabled_gates_dont_block(self):
        """When gate values are 0 (disabled), shorts pass through."""
        broker = _make_broker()
        config = RiskGateConfig(long_only=False, total_capital=50_000)
        # All short gates default to 0 = disabled
        gate = RiskGate(config, broker)
        passed, _ = gate.check("AAPL", "short", 5000, "litigation",
                               earnings_dates={"AAPL": 2}, short_interest={"AAPL": 40.0})
        assert passed
