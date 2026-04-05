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


class TestOrchestratorHorizonScreening:
    """Test that the orchestrator screens once per horizon, not once per cohort."""

    def test_screen_for_horizon_uses_horizon_params(self):
        """Verify _screen_for_horizon passes horizon to strategy.get_default_params."""
        from unittest.mock import MagicMock, patch
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            CohortConfig, CohortOrchestrator,
        )

        mock_strategy = MagicMock()
        mock_strategy.name = "test_strat"
        mock_strategy.get_default_params.return_value = {"hold_days": 90}
        mock_strategy.screen.return_value = []

        configs = [
            CohortConfig(
                name="horizon_3m_size_5k", state_dir="/tmp/test/horizon_3m_size_5k",
                horizon="3m", size_profile="5k",
            ),
        ]

        with patch("tradingagents.strategies.modules.get_paper_trade_strategies",
                    return_value=[mock_strategy]):
            with patch("tradingagents.strategies.orchestration.multi_strategy_engine.build_default_registry"):
                orch = CohortOrchestrator(configs, {"autoresearch": {"state_dir": "/tmp/test"}})

        # Call the horizon screening method directly
        orch._screen_for_horizon({}, "2026-01-15", "3m")
        mock_strategy.get_default_params.assert_called_with(horizon="3m")

    def test_orchestrator_groups_cohorts_by_horizon(self):
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            build_default_cohorts, HORIZON_PARAMS,
        )
        cohorts = build_default_cohorts({"autoresearch": {"state_dir": "data/state"}})
        horizons_seen = {c.horizon for c in cohorts}
        assert horizons_seen == set(HORIZON_PARAMS.keys())

        for h in HORIZON_PARAMS:
            count = sum(1 for c in cohorts if c.horizon == h)
            assert count == 4, f"Horizon {h} has {count} cohorts, expected 4"


class TestCohortComparisonMatrix:
    """Test compare_by_horizon, compare_by_size, and heatmap methods."""

    @pytest.fixture
    def comparison(self, tmp_path):
        from tradingagents.strategies.orchestration.cohort_comparison import CohortComparison

        dirs = {}
        for h in ["30d", "3m"]:
            for s in ["5k", "10k"]:
                name = f"horizon_{h}_size_{s}"
                d = tmp_path / name
                d.mkdir()
                (d / "signal_journal.jsonl").write_text("")
                (d / "paper_trades.json").write_text("[]")
                dirs[name] = str(d)

        return CohortComparison(dirs)

    def test_compare_returns_all_cohorts(self, comparison):
        result = comparison.compare()
        assert len(result["cohorts"]) == 4

    def test_compare_by_horizon_filters_by_size(self, comparison):
        result = comparison.compare_by_horizon("5k")
        assert set(result["cohorts"].keys()) == {"horizon_30d_size_5k", "horizon_3m_size_5k"}

    def test_compare_by_size_filters_by_horizon(self, comparison):
        result = comparison.compare_by_size("30d")
        assert set(result["cohorts"].keys()) == {"horizon_30d_size_5k", "horizon_30d_size_10k"}

    def test_heatmap_returns_matrix(self, comparison):
        result = comparison.heatmap(metric="sharpe")
        assert "30d" in result
        assert "3m" in result
        assert "5k" in result["30d"]
        assert "10k" in result["30d"]

    def test_compare_by_horizon_invalid_size_returns_empty(self, comparison):
        result = comparison.compare_by_horizon("999k")
        assert len(result["cohorts"]) == 0

    def test_compare_by_size_invalid_horizon_returns_empty(self, comparison):
        result = comparison.compare_by_size("99y")
        assert len(result["cohorts"]) == 0


class TestIntegration:
    """End-to-end integration test for the 16-cohort matrix."""

    def test_build_cohorts_and_comparison_roundtrip(self, tmp_path):
        from pathlib import Path
        from tradingagents.strategies.orchestration.cohort_orchestrator import build_default_cohorts
        from tradingagents.strategies.orchestration.cohort_comparison import CohortComparison

        cohorts = build_default_cohorts({"autoresearch": {"state_dir": str(tmp_path)}})
        assert len(cohorts) == 16

        dirs = {}
        for c in cohorts:
            p = Path(c.state_dir)
            p.mkdir(parents=True, exist_ok=True)
            (p / "signal_journal.jsonl").write_text("")
            (p / "paper_trades.json").write_text("[]")
            dirs[c.name] = c.state_dir

        comp = CohortComparison(dirs)
        result = comp.compare()
        assert len(result["cohorts"]) == 16

        hm = comp.heatmap("sharpe")
        assert len(hm) == 4
        for horizon_data in hm.values():
            assert len(horizon_data) == 4

        by_h = comp.compare_by_horizon("10k")
        assert len(by_h["cohorts"]) == 4

        by_s = comp.compare_by_size("6m")
        assert len(by_s["cohorts"]) == 4

    def test_all_strategies_produce_consistent_params_across_horizons(self):
        from tradingagents.strategies.modules import get_all_strategies

        for s in get_all_strategies():
            keys_30d = set(s.get_default_params(horizon="30d").keys())
            for h in ["3m", "6m", "1y"]:
                keys_h = set(s.get_default_params(horizon=h).keys())
                assert keys_30d == keys_h, (
                    f"{s.name}: param keys differ between 30d and {h}: "
                    f"30d={keys_30d}, {h}={keys_h}"
                )
