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


class TestRiskGateCashReserve:
    """Test the new cash_reserve_pct gate in RiskGate."""

    def _make_gate(self, cash_reserve_pct: float, cash: float, portfolio_value: float):
        from tradingagents.strategies.trading.risk_gate import RiskGate, RiskGateConfig
        from unittest.mock import MagicMock

        config = RiskGateConfig(
            total_capital=portfolio_value,
            cash_reserve_pct=cash_reserve_pct,
            max_position_pct=1.0,
        )
        broker = MagicMock()
        broker.get_positions.return_value = []
        account = MagicMock()
        account.portfolio_value = portfolio_value
        account.buying_power = cash
        broker.get_account.return_value = account
        return RiskGate(config, broker)

    def test_cash_reserve_blocks_when_insufficient(self):
        gate = self._make_gate(cash_reserve_pct=0.15, cash=9000.0, portfolio_value=10000.0)
        passed, reason = gate.check("AAPL", "long", 8000.0, "test")
        assert not passed
        assert "cash_reserve" in reason

    def test_cash_reserve_allows_when_sufficient(self):
        gate = self._make_gate(cash_reserve_pct=0.15, cash=9000.0, portfolio_value=10000.0)
        passed, reason = gate.check("AAPL", "long", 5000.0, "test")
        assert passed

    def test_zero_cash_reserve_always_passes(self):
        gate = self._make_gate(cash_reserve_pct=0.0, cash=1000.0, portfolio_value=10000.0)
        passed, reason = gate.check("AAPL", "long", 900.0, "test")
        assert passed


class TestHorizonAwareStrategies:
    """Test that all strategies accept and respect the horizon argument."""

    @pytest.fixture
    def strategies(self):
        from tradingagents.strategies.modules import get_all_strategies
        return get_all_strategies()

    def test_get_default_params_accepts_horizon(self, strategies):
        for s in strategies:
            for h in ["30d", "3m", "6m", "1y"]:
                params = s.get_default_params(horizon=h)
                assert isinstance(params, dict), f"{s.name} get_default_params(horizon={h}) not dict"

    def test_get_param_space_accepts_horizon(self, strategies):
        for s in strategies:
            for h in ["30d", "3m", "6m", "1y"]:
                space = s.get_param_space(horizon=h)
                assert isinstance(space, dict), f"{s.name} get_param_space(horizon={h}) not dict"

    def test_hold_days_increases_with_horizon(self, strategies):
        for s in strategies:
            params_30d = s.get_default_params(horizon="30d")
            params_1y = s.get_default_params(horizon="1y")
            hold_key = "hold_days" if "hold_days" in params_30d else "rebalance_days"
            if hold_key in params_30d and hold_key in params_1y:
                assert params_1y[hold_key] > params_30d[hold_key], (
                    f"{s.name}: {hold_key} should increase: 30d={params_30d[hold_key]} vs 1y={params_1y[hold_key]}"
                )

    def test_param_space_range_widens_with_horizon(self, strategies):
        for s in strategies:
            space_30d = s.get_param_space(horizon="30d")
            space_1y = s.get_param_space(horizon="1y")
            if "hold_days" in space_30d and "hold_days" in space_1y:
                lo_30, hi_30 = space_30d["hold_days"]
                lo_1y, hi_1y = space_1y["hold_days"]
                assert lo_1y > lo_30, f"{s.name}: 1y hold_days min should be > 30d"
                assert hi_1y > hi_30, f"{s.name}: 1y hold_days max should be > 30d"

    def test_default_params_without_horizon_defaults_to_30d(self, strategies):
        for s in strategies:
            no_arg = s.get_default_params()
            with_30d = s.get_default_params(horizon="30d")
            assert no_arg == with_30d, f"{s.name}: no-arg should equal horizon='30d'"


class TestPortfolioCommitteeSizeAware:
    """Test that PortfolioCommittee uses PortfolioSizeProfile values."""

    def test_committee_uses_profile_max_position(self):
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES

        profile = SIZE_PROFILES["5k"]
        committee = PortfolioCommittee(config={}, size_profile=profile)
        assert committee._max_position == profile.max_position_pct

    def test_committee_uses_profile_sector_cap(self):
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES

        profile = SIZE_PROFILES["100k"]
        committee = PortfolioCommittee(config={}, size_profile=profile)
        assert committee._max_sector == profile.sector_concentration_cap

    def test_committee_without_profile_uses_config_defaults(self):
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee

        committee = PortfolioCommittee(config={})
        assert committee._max_position == 0.10
        assert committee._max_sector == 0.30

    def test_rule_based_synthesize_respects_size_profile(self):
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES

        profile = SIZE_PROFILES["5k"]  # max_position_pct=0.25
        committee = PortfolioCommittee(config={}, size_profile=profile)

        signals = [
            {"ticker": "AAPL", "direction": "long", "score": 0.9, "strategy": "strat_a"},
            {"ticker": "AAPL", "direction": "long", "score": 0.8, "strategy": "strat_b"},
        ]
        recs = committee.synthesize(
            signals=signals,
            regime_context={"overall_regime": "normal"},
            strategy_confidence={"strat_a": 0.8, "strat_b": 0.7},
            total_capital=5000.0,
        )
        for rec in recs:
            assert rec.position_size_pct <= profile.max_position_pct
