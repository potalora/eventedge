"""Tests for eligibility wiring into pipeline."""
from __future__ import annotations

from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
from tradingagents.strategies.trading.execution_bridge import ExecutionBridge


class TestCohortEligibilityWiring:
    def test_50k_profile_enables_shorts_on_bridge(self):
        config = {
            "execution": {"mode": "paper"},
            "autoresearch": {"total_capital": 50_000, "risk_gate": {"long_only": True}},
        }
        bridge = ExecutionBridge(config)
        profile = SIZE_PROFILES["50k"]
        # Simulate what MultiStrategyEngine does
        if profile.short_eligible:
            bridge.risk_gate.config.long_only = False
            bridge.risk_gate.config.earnings_blackout_days = 5
            bridge.risk_gate.config.max_borrow_cost_pct = 0.05
            bridge.risk_gate.config.max_margin_utilization_pct = 0.70
        assert bridge.risk_gate.config.long_only is False
        assert bridge.risk_gate.config.earnings_blackout_days == 5

    def test_5k_profile_stays_long_only(self):
        profile = SIZE_PROFILES["5k"]
        assert not profile.short_eligible

    def test_config_has_short_selling_section(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        ss = DEFAULT_CONFIG["autoresearch"]["short_selling"]
        assert "borrow_cost_tiers" in ss
        assert ss["borrow_cost_reject_above"] == 0.05
        assert ss["hard_to_borrow_si_pct"] == 30

    def test_config_has_covered_call_options(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        opts = DEFAULT_CONFIG["options"]
        assert opts["covered_call_min_hold_days"] == 14
        assert opts["covered_call_default_dte"] == 30
        assert opts["covered_call_strike_offset"] == 0.05

    def test_autoresearch_model_is_sonnet(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        model = DEFAULT_CONFIG["autoresearch"]["autoresearch_model"]
        assert "sonnet" in model
