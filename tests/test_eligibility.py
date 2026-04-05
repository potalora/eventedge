"""Tests for PortfolioSizeProfile eligibility flags."""
from __future__ import annotations

from tradingagents.strategies.orchestration.cohort_orchestrator import (
    PortfolioSizeProfile,
    SIZE_PROFILES,
)


class TestEligibility:
    def test_5k_long_only(self):
        p = SIZE_PROFILES["5k"]
        assert p.short_eligible is False
        assert p.options_eligible == []
        assert p.max_short_exposure_pct == 0.0

    def test_10k_covered_calls_only(self):
        p = SIZE_PROFILES["10k"]
        assert p.short_eligible is False
        assert "covered_call" in p.options_eligible
        assert p.max_options_premium_pct == 0.05

    def test_50k_short_eligible(self):
        p = SIZE_PROFILES["50k"]
        assert p.short_eligible is True
        assert "covered_call" in p.options_eligible
        assert p.max_short_exposure_pct == 0.15
        assert p.max_options_premium_pct == 0.05
        assert p.margin_cash_buffer_pct == 0.20
        assert p.max_correlated_shorts == 2

    def test_100k_full_access(self):
        p = SIZE_PROFILES["100k"]
        assert p.short_eligible is True
        assert "covered_call" in p.options_eligible
        assert p.max_short_exposure_pct == 0.20
        assert p.max_options_premium_pct == 0.08
        assert p.margin_cash_buffer_pct == 0.15
        assert p.max_correlated_shorts == 4

    def test_default_eligibility_is_safe(self):
        p = PortfolioSizeProfile(
            name="test", total_capital=1000, max_position_pct=0.25,
            min_position_value=100, max_positions=5,
            sector_concentration_cap=0.50, cash_reserve_pct=0.10,
        )
        assert p.short_eligible is False
        assert p.options_eligible == []
        assert p.max_short_exposure_pct == 0.0
