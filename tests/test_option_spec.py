"""Tests for OptionSpec and Candidate vehicle field."""
from __future__ import annotations

import pytest
from tradingagents.strategies.modules.base import Candidate, OptionSpec


class TestOptionSpec:
    def test_create_covered_call(self):
        spec = OptionSpec(
            strategy="covered_call",
            expiry_target_days=30,
            strike_offset_pct=0.05,
            max_premium_pct=0.03,
        )
        assert spec.strategy == "covered_call"
        assert spec.expiry_target_days == 30
        assert spec.strike_offset_pct == 0.05
        assert spec.max_premium_pct == 0.03

    def test_option_spec_all_strategies(self):
        for strat in ("covered_call", "protective_put", "put_spread", "call_spread", "leaps"):
            spec = OptionSpec(strategy=strat, expiry_target_days=45, strike_offset_pct=-0.05, max_premium_pct=0.05)
            assert spec.strategy == strat


class TestCandidateVehicle:
    def test_default_vehicle_is_equity(self):
        c = Candidate(ticker="AAPL", date="2026-04-04")
        assert c.vehicle == "equity"
        assert c.option_spec is None

    def test_candidate_with_option_spec(self):
        spec = OptionSpec(strategy="covered_call", expiry_target_days=30, strike_offset_pct=0.05, max_premium_pct=0.03)
        c = Candidate(ticker="AAPL", date="2026-04-04", vehicle="option", option_spec=spec)
        assert c.vehicle == "option"
        assert c.option_spec.strategy == "covered_call"

    def test_backward_compat_no_vehicle(self):
        c = Candidate(ticker="MSFT", date="2026-04-04", direction="short", score=0.8)
        assert c.vehicle == "equity"
        assert c.option_spec is None
