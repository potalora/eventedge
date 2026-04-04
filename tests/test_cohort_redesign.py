"""Tests for the cohort redesign: horizon x size matrix."""
from __future__ import annotations

import pytest


class TestPortfolioSizeProfile:
    def test_size_profiles_has_four_tiers(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
        assert set(SIZE_PROFILES.keys()) == {"5k", "10k", "50k", "100k"}

    def test_each_profile_has_required_fields(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
        for name, profile in SIZE_PROFILES.items():
            assert profile.name == name
            assert profile.total_capital > 0
            assert 0 < profile.max_position_pct <= 1.0
            assert profile.min_position_value > 0
            assert profile.max_positions > 0
            assert 0 < profile.sector_concentration_cap <= 1.0
            assert 0 < profile.cash_reserve_pct <= 1.0

    def test_larger_portfolios_have_stricter_concentration(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
        assert SIZE_PROFILES["5k"].max_position_pct > SIZE_PROFILES["100k"].max_position_pct
        assert SIZE_PROFILES["5k"].sector_concentration_cap > SIZE_PROFILES["100k"].sector_concentration_cap

    def test_larger_portfolios_have_more_positions(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
        assert SIZE_PROFILES["5k"].max_positions < SIZE_PROFILES["100k"].max_positions


class TestHorizonParams:
    def test_horizon_params_has_four_horizons(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        assert set(HORIZON_PARAMS.keys()) == {"30d", "3m", "6m", "1y"}

    def test_each_horizon_has_required_keys(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        required = {"hold_days_default", "hold_days_range", "signal_decay_window"}
        for horizon, params in HORIZON_PARAMS.items():
            assert required.issubset(set(params.keys())), f"{horizon} missing keys"

    def test_longer_horizons_have_longer_hold_days(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        assert HORIZON_PARAMS["30d"]["hold_days_default"] < HORIZON_PARAMS["3m"]["hold_days_default"]
        assert HORIZON_PARAMS["3m"]["hold_days_default"] < HORIZON_PARAMS["6m"]["hold_days_default"]
        assert HORIZON_PARAMS["6m"]["hold_days_default"] < HORIZON_PARAMS["1y"]["hold_days_default"]

    def test_hold_days_range_contains_default(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        for horizon, params in HORIZON_PARAMS.items():
            lo, hi = params["hold_days_range"]
            default = params["hold_days_default"]
            assert lo <= default <= hi, f"{horizon}: default {default} not in range ({lo}, {hi})"


class TestCohortConfig:
    def test_cohort_config_has_horizon_and_size_profile(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import CohortConfig
        cfg = CohortConfig(
            name="horizon_30d_size_5k",
            state_dir="data/state/horizon_30d_size_5k",
            horizon="30d",
            size_profile="5k",
        )
        assert cfg.horizon == "30d"
        assert cfg.size_profile == "5k"
        assert cfg.adaptive_confidence is False
        assert cfg.learning_enabled is False
        assert cfg.use_llm is True


class TestBuildDefaultCohorts:
    def test_produces_16_cohorts(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import build_default_cohorts
        cohorts = build_default_cohorts({"autoresearch": {"state_dir": "data/state"}})
        assert len(cohorts) == 16

    def test_all_horizon_size_combinations_present(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import build_default_cohorts
        cohorts = build_default_cohorts({"autoresearch": {"state_dir": "data/state"}})
        names = {c.name for c in cohorts}
        for h in ["30d", "3m", "6m", "1y"]:
            for s in ["5k", "10k", "50k", "100k"]:
                expected = f"horizon_{h}_size_{s}"
                assert expected in names, f"Missing cohort {expected}"

    def test_state_dirs_are_unique(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import build_default_cohorts
        cohorts = build_default_cohorts({"autoresearch": {"state_dir": "data/state"}})
        dirs = [c.state_dir for c in cohorts]
        assert len(dirs) == len(set(dirs))

    def test_state_dir_matches_name(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import build_default_cohorts
        cohorts = build_default_cohorts({"autoresearch": {"state_dir": "data/state"}})
        for c in cohorts:
            assert c.state_dir == f"data/state/{c.name}"

    def test_no_adaptive_or_learning_enabled(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import build_default_cohorts
        cohorts = build_default_cohorts({"autoresearch": {"state_dir": "data/state"}})
        for c in cohorts:
            assert c.adaptive_confidence is False
            assert c.learning_enabled is False

    def test_custom_base_state_dir(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import build_default_cohorts
        cohorts = build_default_cohorts({"autoresearch": {"state_dir": "/tmp/test_state"}})
        for c in cohorts:
            assert c.state_dir.startswith("/tmp/test_state/")
