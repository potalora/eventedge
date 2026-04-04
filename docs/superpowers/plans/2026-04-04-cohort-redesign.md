# Cohort Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 2-cohort control/adaptive split with a 16-cohort horizon x size matrix while keeping the generation system and adaptive infrastructure dormant.

**Architecture:** New `PortfolioSizeProfile` dataclass and `HORIZON_PARAMS`/`SIZE_PROFILES` lookup tables define the matrix. `build_default_cohorts()` generates 16 `CohortConfig` objects from the cartesian product. The orchestrator screens once per horizon (4 passes), dedupes enrichment, and dispatches to 16 cohorts with size-appropriate committee/gate configs.

**Tech Stack:** Python 3.11+, pytest, dataclasses, existing LangGraph/Anthropic infrastructure.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `tradingagents/strategies/orchestration/cohort_orchestrator.py` | Modify | `CohortConfig` + `PortfolioSizeProfile` + lookup tables + `build_default_cohorts()` + horizon-grouped screening |
| `tradingagents/strategies/modules/base.py` | Modify | `StrategyModule` protocol: add `horizon` param to `get_param_space` and `get_default_params` |
| `tradingagents/strategies/modules/earnings_call.py` | Modify | Horizon-aware params |
| `tradingagents/strategies/modules/insider_activity.py` | Modify | Horizon-aware params |
| `tradingagents/strategies/modules/filing_analysis.py` | Modify | Horizon-aware params |
| `tradingagents/strategies/modules/regulatory_pipeline.py` | Modify | Horizon-aware params |
| `tradingagents/strategies/modules/supply_chain.py` | Modify | Horizon-aware params |
| `tradingagents/strategies/modules/litigation.py` | Modify | Horizon-aware params |
| `tradingagents/strategies/modules/congressional_trades.py` | Modify | Horizon-aware params |
| `tradingagents/strategies/modules/govt_contracts.py` | Modify | Horizon-aware params |
| `tradingagents/strategies/modules/state_economics.py` | Modify | Horizon-aware params |
| `tradingagents/strategies/modules/weather_ag.py` | Modify | Horizon-aware params |
| `tradingagents/strategies/trading/portfolio_committee.py` | Modify | Accept `PortfolioSizeProfile`, size-aware LLM prompt |
| `tradingagents/strategies/trading/risk_gate.py` | Modify | `cash_reserve_pct` field + cash reserve gate |
| `tradingagents/strategies/orchestration/multi_strategy_engine.py` | Modify | Pass horizon to strategy params, accept size profile |
| `tradingagents/strategies/orchestration/cohort_comparison.py` | Modify | `compare_by_horizon()`, `compare_by_size()`, `heatmap()` |
| `tests/test_cohort_redesign.py` | Create | All tests for this feature |

---

### Task 1: PortfolioSizeProfile and Lookup Tables

**Files:**
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py:1-26`
- Test: `tests/test_cohort_redesign.py`

- [ ] **Step 1: Write tests for PortfolioSizeProfile and lookup tables**

```python
# tests/test_cohort_redesign.py
"""Tests for the cohort redesign: horizon x size matrix."""
from __future__ import annotations

import pytest


class TestPortfolioSizeProfile:
    """Test PortfolioSizeProfile dataclass and SIZE_PROFILES lookup."""

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
    """Test HORIZON_PARAMS lookup table."""

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py -v`
Expected: FAIL — `SIZE_PROFILES` and `HORIZON_PARAMS` not defined

- [ ] **Step 3: Implement PortfolioSizeProfile and lookup tables**

Add to `tradingagents/strategies/orchestration/cohort_orchestrator.py` after the existing imports (before `CohortConfig`):

```python
@dataclass
class PortfolioSizeProfile:
    """Portfolio sizing rules for a capital tier."""

    name: str                          # "5k", "10k", "50k", "100k"
    total_capital: float
    max_position_pct: float
    min_position_value: float
    max_positions: int
    sector_concentration_cap: float
    cash_reserve_pct: float


SIZE_PROFILES: dict[str, PortfolioSizeProfile] = {
    "5k": PortfolioSizeProfile(
        name="5k", total_capital=5_000.0, max_position_pct=0.25,
        min_position_value=500.0, max_positions=5,
        sector_concentration_cap=0.50, cash_reserve_pct=0.10,
    ),
    "10k": PortfolioSizeProfile(
        name="10k", total_capital=10_000.0, max_position_pct=0.20,
        min_position_value=1_000.0, max_positions=8,
        sector_concentration_cap=0.40, cash_reserve_pct=0.10,
    ),
    "50k": PortfolioSizeProfile(
        name="50k", total_capital=50_000.0, max_position_pct=0.10,
        min_position_value=2_500.0, max_positions=15,
        sector_concentration_cap=0.30, cash_reserve_pct=0.15,
    ),
    "100k": PortfolioSizeProfile(
        name="100k", total_capital=100_000.0, max_position_pct=0.08,
        min_position_value=5_000.0, max_positions=20,
        sector_concentration_cap=0.25, cash_reserve_pct=0.15,
    ),
}


HORIZON_PARAMS: dict[str, dict] = {
    "30d": {
        "hold_days_default": 25,
        "hold_days_range": (20, 45),
        "signal_decay_window": (5, 10),
    },
    "3m": {
        "hold_days_default": 90,
        "hold_days_range": (60, 120),
        "signal_decay_window": (15, 30),
    },
    "6m": {
        "hold_days_default": 180,
        "hold_days_range": (120, 210),
        "signal_decay_window": (30, 60),
    },
    "1y": {
        "hold_days_default": 300,
        "hold_days_range": (250, 365),
        "signal_decay_window": (60, 120),
    },
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/orchestration/cohort_orchestrator.py tests/test_cohort_redesign.py
git commit -m "feat: add PortfolioSizeProfile, SIZE_PROFILES, and HORIZON_PARAMS"
```

---

### Task 2: CohortConfig and build_default_cohorts()

**Files:**
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py:17-237`
- Test: `tests/test_cohort_redesign.py`

- [ ] **Step 1: Write tests for new CohortConfig and build_default_cohorts()**

Append to `tests/test_cohort_redesign.py`:

```python
class TestCohortConfig:
    """Test updated CohortConfig with horizon and size_profile fields."""

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
    """Test the 16-cohort matrix builder."""

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py::TestCohortConfig tests/test_cohort_redesign.py::TestBuildDefaultCohorts -v`
Expected: FAIL — `CohortConfig` missing `horizon`/`size_profile` fields

- [ ] **Step 3: Update CohortConfig and build_default_cohorts()**

In `tradingagents/strategies/orchestration/cohort_orchestrator.py`, replace the `CohortConfig` dataclass (lines 17-25):

```python
@dataclass
class CohortConfig:
    """Configuration for a single cohort in the horizon x size matrix."""

    name: str                           # "horizon_30d_size_5k"
    state_dir: str                      # Unique per cohort
    horizon: str                        # "30d", "3m", "6m", "1y"
    size_profile: str                   # "5k", "10k", "50k", "100k"
    use_llm: bool = True
    adaptive_confidence: bool = False   # dormant
    learning_enabled: bool = False      # dormant
```

Replace `build_default_cohorts()` (lines 213-237):

```python
def build_default_cohorts(base_config: dict) -> list[CohortConfig]:
    """Build the 16-cohort horizon x size matrix.

    Produces one cohort per combination of 4 horizons x 4 portfolio sizes.
    All cohorts use fixed confidence (adaptive/learning dormant).
    """
    base_state_dir = base_config.get("autoresearch", {}).get(
        "state_dir", "data/state"
    )
    horizons = ["30d", "3m", "6m", "1y"]
    sizes = ["5k", "10k", "50k", "100k"]
    cohorts = []
    for h in horizons:
        for s in sizes:
            name = f"horizon_{h}_size_{s}"
            cohorts.append(
                CohortConfig(
                    name=name,
                    state_dir=f"{base_state_dir}/{name}",
                    horizon=h,
                    size_profile=s,
                    use_llm=True,
                )
            )
    return cohorts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/orchestration/cohort_orchestrator.py tests/test_cohort_redesign.py
git commit -m "feat: update CohortConfig and build_default_cohorts for 16-cohort matrix"
```

---

### Task 3: StrategyModule Protocol — Horizon-Aware Params

**Files:**
- Modify: `tradingagents/strategies/modules/base.py:107-121`
- Test: `tests/test_cohort_redesign.py`

- [ ] **Step 1: Write tests for horizon-aware protocol**

Append to `tests/test_cohort_redesign.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py::TestHorizonAwareStrategies -v`
Expected: FAIL — strategies don't accept `horizon` argument

- [ ] **Step 3: Update StrategyModule protocol**

In `tradingagents/strategies/modules/base.py`, change the protocol methods (lines 113-121):

```python
    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        """Return evolvable parameters and their (min, max) ranges.
        For bool params: (True, False). For categorical: tuple of options.

        Args:
            horizon: Investment horizon ("30d", "3m", "6m", "1y").
        """
        ...

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        """Return sensible default parameters.

        Args:
            horizon: Investment horizon ("30d", "3m", "6m", "1y").
        """
        ...
```

- [ ] **Step 4: Update all 10 strategy modules**

Each strategy needs the same pattern. Import `HORIZON_PARAMS` and use it to set `hold_days` (or `rebalance_days` for `state_economics`). Strategy-specific params stay unchanged across horizons.

**EarningsCallStrategy** (`tradingagents/strategies/modules/earnings_call.py:30-44`):

```python
    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_range"],
            "min_conviction": (0.3, 0.8),
            "max_positions": (2, 6),
            "analyze_qa_only": (True, False),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_default"],
            "min_conviction": 0.5,
            "max_positions": 4,
            "analyze_qa_only": False,
        }
```

**InsiderActivityStrategy** (`tradingagents/strategies/modules/insider_activity.py:29-45`):

```python
    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_range"],
            "min_cluster_size": (2, 5),
            "min_sell_threshold": (2, 5),
            "min_conviction": (0.3, 0.8),
            "max_positions": (2, 5),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_default"],
            "min_cluster_size": 2,
            "min_sell_threshold": 2,
            "min_conviction": 0.5,
            "max_positions": 3,
        }
```

**FilingAnalysisStrategy** (`tradingagents/strategies/modules/filing_analysis.py:45-62`):

```python
    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_range"],
            "min_conviction": (0.3, 0.7),
            "max_positions": (3, 8),
            "forms_to_analyze": (
                ["10-K", "10-Q"],
                ["10-K", "10-Q", "DEF 14A", "8-K", "SC 13D", "SC 13G"],
            ),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_default"],
            "min_conviction": 0.5,
            "max_positions": 5,
            "forms_to_analyze": ["10-K", "10-Q", "DEF 14A", "8-K", "SC 13D", "SC 13G"],
        }
```

**RegulatoryPipelineStrategy** (`tradingagents/strategies/modules/regulatory_pipeline.py:27-41`):

```python
    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_range"],
            "min_conviction": (0.3, 0.8),
            "max_positions": (2, 5),
            "days_lookback": (7, 30),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_default"],
            "min_conviction": 0.5,
            "max_positions": 3,
            "days_lookback": 14,
        }
```

**SupplyChainStrategy** (`tradingagents/strategies/modules/supply_chain.py:38-54`):

```python
    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_range"],
            "min_conviction": (0.3, 0.8),
            "max_positions": (2, 6),
            "news_lookback_days": (3, 14),
            "hop_depth": (1, 3),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_default"],
            "min_conviction": 0.5,
            "max_positions": 4,
            "news_lookback_days": 7,
            "hop_depth": 2,
        }
```

**LitigationStrategy** (`tradingagents/strategies/modules/litigation.py:46-60`):

```python
    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_range"],
            "min_conviction": (0.3, 0.8),
            "max_positions": (2, 5),
            "lookback_days": (7, 30),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_default"],
            "min_conviction": 0.5,
            "max_positions": 3,
            "lookback_days": 14,
        }
```

**CongressionalTradesStrategy** (`tradingagents/strategies/modules/congressional_trades.py:48-62`):

```python
    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_range"],
            "min_amount_bucket": (1, 4),
            "max_positions": (2, 5),
            "min_members": (1, 3),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_default"],
            "min_amount_bucket": 1,
            "max_positions": 3,
            "min_members": 1,
        }
```

**GovtContractsStrategy** (`tradingagents/strategies/modules/govt_contracts.py:63-77`):

```python
    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_range"],
            "stop_loss_pct": (0.05, 0.15),
            "profit_target_pct": (0.05, 0.25),
            "max_positions": (2, 5),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_default"],
            "stop_loss_pct": 0.08,
            "profit_target_pct": 0.15,
            "max_positions": 3,
        }
```

**StateEconomicsStrategy** (`tradingagents/strategies/modules/state_economics.py:49-61`):

This strategy uses `rebalance_days` instead of `hold_days`. Map it the same way:

```python
    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "lookback_days": (10, 60),
            "top_n": (1, 4),
            "rebalance_days": hp["hold_days_range"],
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "lookback_days": 21,
            "top_n": 2,
            "rebalance_days": hp["hold_days_default"],
        }
```

**WeatherAgStrategy** (`tradingagents/strategies/modules/weather_ag.py:68-87`):

```python
    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "lookback_days": (10, 60),
            "hold_days": hp["hold_days_range"],
            "min_return": (-0.05, 0.05),
            "heat_stress_threshold": (2, 15),
            "precip_deficit_threshold": (-50, -10),
            "drought_min_score": (0.3, 2.0),
            "crop_decline_threshold": (1, 5),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "lookback_days": 21,
            "hold_days": hp["hold_days_default"],
            "min_return": 0.0,
            "heat_stress_threshold": 2,
            "precip_deficit_threshold": -10,
            "drought_min_score": 0.3,
            "crop_decline_threshold": 1,
        }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py::TestHorizonAwareStrategies -v`
Expected: All 5 tests PASS

- [ ] **Step 6: Run existing strategy tests to check backward compatibility**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_multi_strategy.py::TestStrategyModules -v`
Expected: All existing tests PASS (no-arg calls default to "30d")

- [ ] **Step 7: Commit**

```bash
git add tradingagents/strategies/modules/base.py tradingagents/strategies/modules/earnings_call.py tradingagents/strategies/modules/insider_activity.py tradingagents/strategies/modules/filing_analysis.py tradingagents/strategies/modules/regulatory_pipeline.py tradingagents/strategies/modules/supply_chain.py tradingagents/strategies/modules/litigation.py tradingagents/strategies/modules/congressional_trades.py tradingagents/strategies/modules/govt_contracts.py tradingagents/strategies/modules/state_economics.py tradingagents/strategies/modules/weather_ag.py tests/test_cohort_redesign.py
git commit -m "feat: add horizon-aware params to all 10 strategies"
```

---

### Task 4: RiskGate Cash Reserve Gate

**Files:**
- Modify: `tradingagents/strategies/trading/risk_gate.py:16-130`
- Test: `tests/test_cohort_redesign.py`

- [ ] **Step 1: Write tests for cash reserve gate**

Append to `tests/test_cohort_redesign.py`:

```python
class TestRiskGateCashReserve:
    """Test the new cash_reserve_pct gate in RiskGate."""

    def _make_gate(self, cash_reserve_pct: float, cash: float, portfolio_value: float):
        from tradingagents.strategies.trading.risk_gate import RiskGate, RiskGateConfig
        from unittest.mock import MagicMock

        config = RiskGateConfig(
            total_capital=portfolio_value,
            cash_reserve_pct=cash_reserve_pct,
        )
        broker = MagicMock()
        broker.get_positions.return_value = []
        account = MagicMock()
        account.portfolio_value = portfolio_value
        account.buying_power = cash
        broker.get_account.return_value = account
        return RiskGate(config, broker)

    def test_cash_reserve_blocks_when_insufficient(self):
        # $10k portfolio, 15% reserve = $1500 must stay cash
        # $9000 cash, buying $8000 would leave $1000 < $1500
        gate = self._make_gate(cash_reserve_pct=0.15, cash=9000.0, portfolio_value=10000.0)
        passed, reason = gate.check("AAPL", "long", 8000.0, "test")
        assert not passed
        assert "cash_reserve" in reason

    def test_cash_reserve_allows_when_sufficient(self):
        # $10k portfolio, 15% reserve = $1500 must stay cash
        # $9000 cash, buying $5000 would leave $4000 > $1500
        gate = self._make_gate(cash_reserve_pct=0.15, cash=9000.0, portfolio_value=10000.0)
        passed, reason = gate.check("AAPL", "long", 5000.0, "test")
        assert passed

    def test_zero_cash_reserve_always_passes(self):
        gate = self._make_gate(cash_reserve_pct=0.0, cash=1000.0, portfolio_value=10000.0)
        passed, reason = gate.check("AAPL", "long", 900.0, "test")
        assert passed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py::TestRiskGateCashReserve -v`
Expected: FAIL — `RiskGateConfig` has no `cash_reserve_pct` field

- [ ] **Step 3: Add cash_reserve_pct to RiskGateConfig and check()**

In `tradingagents/strategies/trading/risk_gate.py`, add to `RiskGateConfig` (after line 26):

```python
    cash_reserve_pct: float = 0.0           # Min cash as % of portfolio (0 = disabled)
```

In `RiskGateConfig.from_dict()` (line 43), add:

```python
            cash_reserve_pct=rg.get("cash_reserve_pct", 0.0),
```

In `RiskGate.check()`, add a new gate after gate 8 (buying power check, line 128), before `return True, ""`:

```python
        # 9. Cash reserve check
        if self.config.cash_reserve_pct > 0:
            min_cash = account.portfolio_value * self.config.cash_reserve_pct
            remaining_cash = account.buying_power - position_value
            if remaining_cash < min_cash:
                return False, (
                    f"cash_reserve: ${remaining_cash:.0f} remaining < "
                    f"${min_cash:.0f} ({self.config.cash_reserve_pct:.0%} reserve)"
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py::TestRiskGateCashReserve -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/trading/risk_gate.py tests/test_cohort_redesign.py
git commit -m "feat: add cash_reserve_pct gate to RiskGate"
```

---

### Task 5: PortfolioCommittee Size Awareness

**Files:**
- Modify: `tradingagents/strategies/trading/portfolio_committee.py:32-45, 241-268`
- Test: `tests/test_cohort_redesign.py`

- [ ] **Step 1: Write tests for size-aware committee**

Append to `tests/test_cohort_redesign.py`:

```python
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
        assert committee._max_position == 0.10  # existing default
        assert committee._max_sector == 0.30    # existing default

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py::TestPortfolioCommitteeSizeAware -v`
Expected: FAIL — `PortfolioCommittee.__init__` doesn't accept `size_profile`

- [ ] **Step 3: Update PortfolioCommittee to accept size_profile**

In `tradingagents/strategies/trading/portfolio_committee.py`, update `__init__` (lines 35-45):

```python
    def __init__(self, config: dict | None = None, size_profile: Any = None) -> None:
        self.config = config or {}
        pt_config = self.config.get("autoresearch", {}).get("paper_trade", {})
        self._model_name = pt_config.get(
            "portfolio_committee_model",
            self.config.get("autoresearch", {}).get("autoresearch_model", "claude-haiku-4-5-20251001"),
        )
        self._enabled = pt_config.get("portfolio_committee_enabled", True)

        # Use size profile if provided, otherwise fall back to config defaults
        if size_profile is not None:
            self._max_sector = size_profile.sector_concentration_cap
            self._max_position = size_profile.max_position_pct
        else:
            self._max_sector = pt_config.get("max_sector_concentration_pct", 0.30)
            self._max_position = pt_config.get("max_single_position_pct", 0.10)

        self._size_profile = size_profile
        self._client = None
```

Update the LLM system prompt in `_llm_synthesize()` (line 264) to include horizon/size context. Replace the `system=` string:

```python
                system_parts = [
                    "You are a portfolio manager synthesizing trading signals from multiple strategies.",
                ]
                if self._size_profile:
                    system_parts.append(
                        f"Portfolio: ${self._size_profile.total_capital:,.0f} capital, "
                        f"max {self._size_profile.max_positions} positions, "
                        f"max {self._size_profile.max_position_pct:.0%} per position."
                    )
                else:
                    system_parts.append("Investment horizon: 30 days.")
                system_parts.append(
                    "Given signals, regime context, and strategy confidence scores, output a ranked list of trades. "
                    "Return ONLY a JSON array of objects with keys: ticker, direction, position_size_pct, confidence, "
                    "rationale, contributing_strategies, regime_alignment. "
                    f"Keep position_size_pct between 0.01 and {self._max_position:.2f}. Keep rationale under 80 chars."
                )
                system_prompt = "\n".join(system_parts)
```

And use `system=system_prompt` in the `client.messages.create()` call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py::TestPortfolioCommitteeSizeAware -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/trading/portfolio_committee.py tests/test_cohort_redesign.py
git commit -m "feat: add size_profile support to PortfolioCommittee"
```

---

### Task 6: Orchestrator Horizon-Grouped Screening

**Files:**
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py:28-136`
- Modify: `tradingagents/strategies/orchestration/multi_strategy_engine.py:100-157`
- Test: `tests/test_cohort_redesign.py`

- [ ] **Step 1: Write tests for horizon-grouped screening**

Append to `tests/test_cohort_redesign.py`:

```python
class TestOrchestratorHorizonScreening:
    """Test that the orchestrator screens once per horizon, not once per cohort."""

    def test_screen_for_horizon_uses_horizon_params(self):
        """Verify _screen_for_horizon passes horizon to strategy.get_default_params."""
        from unittest.mock import MagicMock, patch, call
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            CohortConfig, CohortOrchestrator, SIZE_PROFILES,
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

        with patch("tradingagents.strategies.orchestration.cohort_orchestrator.get_paper_trade_strategies",
                    return_value=[mock_strategy]):
            with patch("tradingagents.strategies.orchestration.multi_strategy_engine.get_paper_trade_strategies",
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

        # Each horizon should have exactly 4 cohorts (one per size)
        for h in HORIZON_PARAMS:
            count = sum(1 for c in cohorts if c.horizon == h)
            assert count == 4, f"Horizon {h} has {count} cohorts, expected 4"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py::TestOrchestratorHorizonScreening -v`
Expected: FAIL — `_screen_for_horizon` method doesn't exist

- [ ] **Step 3: Add _screen_for_horizon to orchestrator and update run_daily**

In `tradingagents/strategies/orchestration/multi_strategy_engine.py`, update `screen_and_enrich` (line 101) to accept an optional `horizon` parameter:

```python
    def screen_and_enrich(
        self,
        trading_date: str,
        data: dict,
        horizon: str = "30d",
    ) -> tuple[list[dict], dict]:
```

And change line 117 from:
```python
            params = strategy.get_default_params()
```
to:
```python
            params = strategy.get_default_params(horizon=horizon)
```

In `tradingagents/strategies/orchestration/cohort_orchestrator.py`, update `CohortOrchestrator.__init__` to store strategies and build engines with size profiles:

```python
    def __init__(self, cohort_configs: list[CohortConfig], base_config: dict):
        from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
        from tradingagents.strategies.state.state import StateManager
        from tradingagents.strategies.modules import get_paper_trade_strategies

        self.cohorts: list[dict[str, Any]] = []
        strategies = get_paper_trade_strategies()

        for cfg in cohort_configs:
            cohort_config = copy.deepcopy(base_config)
            cohort_config.setdefault("autoresearch", {})["state_dir"] = cfg.state_dir

            # Apply size profile to config for risk gate
            profile = SIZE_PROFILES.get(cfg.size_profile)
            if profile:
                cohort_config.setdefault("autoresearch", {})["total_capital"] = profile.total_capital

            state = StateManager(cfg.state_dir)
            engine = MultiStrategyEngine(
                config=cohort_config,
                strategies=strategies,
                state_manager=state,
                use_llm=cfg.use_llm,
                adaptive_confidence=cfg.adaptive_confidence,
            )
            self.cohorts.append({
                "config": cfg,
                "engine": engine,
                "state": state,
                "size_profile": profile,
            })

        self._base_config = base_config
        self._strategies = strategies

        # OpenBB availability check
        first_engine = self.cohorts[0]["engine"] if self.cohorts else None
        openbb_source = (
            first_engine.registry.get("openbb") if first_engine else None
        )
        if openbb_source is not None and openbb_source.is_available():
            self.openbb_degraded = False
            logger.info("OpenBB: available — sector enforcement and enrichment active")
        else:
            self.openbb_degraded = True
            logger.warning(
                "OpenBB: UNAVAILABLE — sector enforcement disabled, enrichment skipped. "
                "Install with: pip install -e '[.openbb]' and set FMP_API_KEY"
            )
```

Add `_screen_for_horizon` method:

```python
    def _screen_for_horizon(
        self, data: dict, trading_date: str, horizon: str,
    ) -> tuple[list[dict], dict]:
        """Screen all strategies with horizon-specific params.

        Returns (signals, regime_model) for the given horizon.
        """
        first_engine = self.cohorts[0]["engine"]
        return first_engine.screen_and_enrich(trading_date, data, horizon=horizon)
```

Replace `run_daily` body (lines 80-136):

```python
    def run_daily(self, trading_date: str | None = None) -> dict[str, Any]:
        """Run all cohorts with shared data fetch and per-horizon screening.

        1. Fetch data ONCE.
        2. Screen once per horizon (4 passes).
        3. OpenBB enrichment once (deduped tickers across horizons).
        4. Dispatch to all 16 cohorts.
        """
        if not trading_date:
            trading_date = datetime.now().strftime("%Y-%m-%d")

        logger.info("=== Cohort daily run: %s ===", trading_date)

        # Fetch data once
        first_engine = self.cohorts[0]["engine"]
        lookback_start = (
            datetime.strptime(trading_date, "%Y-%m-%d") - timedelta(days=7)
        ).strftime("%Y-%m-%d")
        shared_data = first_engine._fetch_all_data(lookback_start, trading_date)
        logger.info("Shared data fetched: %s", list(shared_data.keys()))

        # Screen once per horizon (4 passes, cached)
        horizons = sorted({c["config"].horizon for c in self.cohorts})
        horizon_signals: dict[str, tuple[list[dict], dict]] = {}
        for horizon in horizons:
            signals, regime = self._screen_for_horizon(shared_data, trading_date, horizon)
            horizon_signals[horizon] = (signals, regime)
            logger.info("Horizon %s: %d signals", horizon, len(signals))

        # OpenBB enrichment once (dedupe tickers across all horizons)
        all_signals = []
        for signals, _ in horizon_signals.values():
            all_signals.extend(signals)
        enrichment = self._fetch_openbb_enrichment(all_signals)

        # Dispatch to all cohorts
        results: dict[str, Any] = {}
        for cohort in self.cohorts:
            cfg = cohort["config"]
            name = cfg.name
            logger.info("--- Running cohort: %s ---", name)

            signals, regime = horizon_signals[cfg.horizon]

            try:
                result = cohort["engine"].run_paper_trade_phase(
                    trading_date=trading_date,
                    shared_signals=signals,
                    shared_regime=regime,
                    enrichment=enrichment,
                    size_profile=cohort.get("size_profile"),
                )
                results[name] = result
                n_signals = len(result.get("signals", []))
                n_trades = len(result.get("trades_opened", []))
                account = result.get("account", {})
                logger.info(
                    "Cohort %s: %d signals, %d trades, portfolio=$%.0f",
                    name, n_signals, n_trades, account.get("portfolio_value", 0),
                )
            except Exception:
                logger.error("Cohort %s failed", name, exc_info=True)
                results[name] = {"error": True}

        return results
```

- [ ] **Step 4: Update MultiStrategyEngine.run_paper_trade_phase to accept size_profile**

In `tradingagents/strategies/orchestration/multi_strategy_engine.py`, add `size_profile` parameter to `run_paper_trade_phase` (line 159):

```python
    def run_paper_trade_phase(
        self,
        trading_date: str | None = None,
        data: dict | None = None,
        shared_signals: list[dict] | None = None,
        shared_regime: dict | None = None,
        enrichment: dict | None = None,
        size_profile: Any = None,
    ) -> dict:
```

And update the `PortfolioCommittee` construction (line 241) and `RiskGateConfig` usage to pass the size profile:

Change line 241 from:
```python
        committee = PortfolioCommittee(self.config)
```
to:
```python
        committee = PortfolioCommittee(self.config, size_profile=size_profile)
```

Update the `ExecutionBridge` construction to pass size profile to the risk gate. In the bridge init area (line 228), after creating the bridge, apply size profile to its risk gate config:

```python
        bridge = ExecutionBridge(self.config)
        if size_profile is not None:
            bridge.risk_gate.config.max_positions = size_profile.max_positions
            bridge.risk_gate.config.max_position_pct = size_profile.max_position_pct
            bridge.risk_gate.config.min_position_value = size_profile.min_position_value
            bridge.risk_gate.config.total_capital = size_profile.total_capital
            bridge.risk_gate.config.cash_reserve_pct = size_profile.cash_reserve_pct
        bridge.risk_gate.reset_daily(trading_date)
        bridge.risk_gate.update_high_water_mark()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py::TestOrchestratorHorizonScreening -v`
Expected: All tests PASS

- [ ] **Step 6: Run full test suite to check nothing broke**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/ -v --timeout=120`
Expected: All existing tests still pass

- [ ] **Step 7: Commit**

```bash
git add tradingagents/strategies/orchestration/cohort_orchestrator.py tradingagents/strategies/orchestration/multi_strategy_engine.py tests/test_cohort_redesign.py
git commit -m "feat: horizon-grouped screening in orchestrator, size_profile passthrough to committee/gate"
```

---

### Task 7: CohortComparison Matrix Methods

**Files:**
- Modify: `tradingagents/strategies/orchestration/cohort_comparison.py`
- Test: `tests/test_cohort_redesign.py`

- [ ] **Step 1: Write tests for comparison matrix methods**

Append to `tests/test_cohort_redesign.py`:

```python
class TestCohortComparisonMatrix:
    """Test compare_by_horizon, compare_by_size, and heatmap methods."""

    @pytest.fixture
    def comparison(self, tmp_path):
        """Create a CohortComparison with 4 dummy cohorts (2 horizons x 2 sizes)."""
        from tradingagents.strategies.orchestration.cohort_comparison import CohortComparison

        dirs = {}
        for h in ["30d", "3m"]:
            for s in ["5k", "10k"]:
                name = f"horizon_{h}_size_{s}"
                d = tmp_path / name
                d.mkdir()
                # Empty signal journal
                (d / "signal_journal.jsonl").write_text("")
                # Empty paper trades
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py::TestCohortComparisonMatrix -v`
Expected: FAIL — `compare_by_horizon`, `compare_by_size`, `heatmap` don't exist

- [ ] **Step 3: Implement comparison matrix methods**

In `tradingagents/strategies/orchestration/cohort_comparison.py`, add after `format_report()` (before the internal helpers section):

```python
    def compare_by_horizon(self, size: str) -> dict:
        """Compare cohorts across horizons for a fixed portfolio size.

        Args:
            size: Size profile key (e.g., "5k", "10k").

        Returns:
            Same structure as compare(), filtered to matching cohorts.
        """
        matching = {
            name for name in self._dirs
            if f"_size_{size}" in name
        }
        return self._filtered_compare(matching)

    def compare_by_size(self, horizon: str) -> dict:
        """Compare cohorts across sizes for a fixed horizon.

        Args:
            horizon: Horizon key (e.g., "30d", "3m").

        Returns:
            Same structure as compare(), filtered to matching cohorts.
        """
        matching = {
            name for name in self._dirs
            if f"horizon_{horizon}_" in name
        }
        return self._filtered_compare(matching)

    def heatmap(self, metric: str = "sharpe") -> dict[str, dict[str, float | None]]:
        """Return horizon x size matrix of a single metric.

        Args:
            metric: Key from per-cohort metrics (e.g., "sharpe", "hit_rate_5d",
                    "win_rate", "avg_pnl").

        Returns:
            {horizon: {size: metric_value}} e.g. {"30d": {"5k": 1.2, "10k": 0.8}}
        """
        data = self.compare()
        result: dict[str, dict[str, float | None]] = {}

        for name, metrics in data["cohorts"].items():
            # Parse horizon and size from name: "horizon_30d_size_5k"
            parts = name.split("_")
            if len(parts) >= 4 and parts[0] == "horizon" and parts[2] == "size":
                horizon = parts[1]
                size = parts[3]
                result.setdefault(horizon, {})[size] = metrics.get(metric)

        return result

    def _filtered_compare(self, names: set[str]) -> dict:
        """Run compare() and filter to only the named cohorts."""
        full = self.compare()
        filtered_cohorts = {
            k: v for k, v in full["cohorts"].items() if k in names
        }
        filtered_strategies = {}
        for strat, cohort_data in full["per_strategy"].items():
            filtered = {k: v for k, v in cohort_data.items() if k in names}
            if filtered:
                filtered_strategies[strat] = filtered

        return {
            "cohorts": filtered_cohorts,
            "per_strategy": filtered_strategies,
            "confidence_evolution": full.get("confidence_evolution", {}),
            "prompt_trials": full.get("prompt_trials", {}),
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py::TestCohortComparisonMatrix -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/orchestration/cohort_comparison.py tests/test_cohort_redesign.py
git commit -m "feat: add compare_by_horizon, compare_by_size, heatmap to CohortComparison"
```

---

### Task 8: Update CLAUDE.md and Final Integration Test

**Files:**
- Modify: `CLAUDE.md`
- Test: `tests/test_cohort_redesign.py`

- [ ] **Step 1: Write integration test**

Append to `tests/test_cohort_redesign.py`:

```python
class TestIntegration:
    """End-to-end integration test for the 16-cohort matrix."""

    def test_build_cohorts_and_comparison_roundtrip(self, tmp_path):
        """Build 16 cohorts, create state dirs, run comparison."""
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            build_default_cohorts,
        )
        from tradingagents.strategies.orchestration.cohort_comparison import CohortComparison

        cohorts = build_default_cohorts({"autoresearch": {"state_dir": str(tmp_path)}})
        assert len(cohorts) == 16

        # Create state dirs with empty journals
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

        # Heatmap should have 4 horizons x 4 sizes
        hm = comp.heatmap("sharpe")
        assert len(hm) == 4
        for horizon_data in hm.values():
            assert len(horizon_data) == 4

        # compare_by_horizon should return 4 cohorts
        by_h = comp.compare_by_horizon("10k")
        assert len(by_h["cohorts"]) == 4

        # compare_by_size should return 4 cohorts
        by_s = comp.compare_by_size("6m")
        assert len(by_s["cohorts"]) == 4

    def test_all_strategies_produce_consistent_params_across_horizons(self):
        """Every strategy should return the same keys regardless of horizon."""
        from tradingagents.strategies.modules import get_all_strategies

        for s in get_all_strategies():
            keys_30d = set(s.get_default_params(horizon="30d").keys())
            for h in ["3m", "6m", "1y"]:
                keys_h = set(s.get_default_params(horizon=h).keys())
                assert keys_30d == keys_h, (
                    f"{s.name}: param keys differ between 30d and {h}: "
                    f"30d={keys_30d}, {h}={keys_h}"
                )
```

- [ ] **Step 2: Run integration test**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py::TestIntegration -v`
Expected: All 2 tests PASS

- [ ] **Step 3: Update CLAUDE.md architecture and reporting note**

In `CLAUDE.md`, update the Architecture Overview section to replace the 2-cohort diagram with the matrix description. Update the Autoresearch System section to reflect 16 cohorts. Add a note about report generation:

Replace the cohort section of the ASCII architecture diagram:

```
                                          ┌── 16 Cohorts (Horizon × Size) ─┐
                                          │  4 horizons: 30d, 3m, 6m, 1y  │
CohortOrchestrator ── Shared Data Fetch ──┤  4 sizes: 5k, 10k, 50k, 100k  │
          │                               │  = 16 independent portfolios   │
     4 horizon screens                    │  Adaptive/learning: dormant    │
                                          └────────────────────────────────┘
                                              StateManager (JSON)
```

Add to the Autoresearch System section, after the Phase 2 description:

```
### Report Generation

Reports are not script-generated. Ask Claude to generate reports by reading state directories and calling `CohortComparison` methods (`compare()`, `compare_by_horizon()`, `compare_by_size()`, `heatmap()`).
```

- [ ] **Step 4: Run full test suite**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/ -v --timeout=120`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md tests/test_cohort_redesign.py
git commit -m "docs: update CLAUDE.md for 16-cohort matrix, add integration tests"
```

---

### Task 9: Run Full Test Suite and Final Verification

- [ ] **Step 1: Run all cohort redesign tests**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cohort_redesign.py -v`
Expected: All tests PASS

- [ ] **Step 2: Run full test suite**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/ -v --timeout=120`
Expected: All existing tests PASS. No regressions.

- [ ] **Step 3: Verify backward compatibility of existing gen_001-gen_004**

The old `build_default_cohorts()` in frozen worktrees is untouched. New code only affects new generations. Verify by checking that `CohortConfig` still accepts the old field set (dormant fields default correctly).

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -c "from tradingagents.strategies.orchestration.cohort_orchestrator import CohortConfig; c = CohortConfig(name='control', state_dir='data/state/control', horizon='30d', size_profile='5k'); print(f'OK: {c.name}, adaptive={c.adaptive_confidence}, learning={c.learning_enabled}')"` 
Expected: `OK: control, adaptive=False, learning=False`
