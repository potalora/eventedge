# Cohort Redesign: Horizon x Size Matrix

**Date:** 2026-04-04
**Status:** Approved
**Source:** [docs/next-wave-cohort-redesign.md](../../next-wave-cohort-redesign.md)

---

## Overview

Replace the 2-cohort Control/Adaptive split with a 16-cohort matrix of 4 investment horizons x 4 portfolio sizes. Remove the adaptive cohort from active use (keep infrastructure dormant). Generations continue to work on top — each generation runs all 16 cohorts.

### Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Run all 16 cohorts or subset? | All 16, always | Simpler, complete data from day one |
| Horizon-aware screening? | Yes, 4 passes | Signal relevance differs by timeframe |
| Remove adaptive code? | Keep dormant | May re-enable later with new cohort matrix |
| Where do size rules live? | New `PortfolioSizeProfile` dataclass | Single source consumed by both committee and gate |
| Rebalance cadence? | No — full pipeline daily | Horizons differ in params/exits, not run frequency |
| Report generation? | Claude generates on request | No script-generated reports; CLAUDE.md note instead |

---

## 1. CohortConfig Changes

### Current

```python
@dataclass
class CohortConfig:
    name: str                          # "control" or "adaptive"
    state_dir: str
    adaptive_confidence: bool = False
    learning_enabled: bool = False
    use_llm: bool = True
```

### New

```python
@dataclass
class CohortConfig:
    name: str                          # "horizon_30d_size_5k"
    state_dir: str                     # "data/state/horizon_30d_size_5k/"
    horizon: str                       # "30d", "3m", "6m", "1y"
    size_profile: str                  # "5k", "10k", "50k", "100k"
    use_llm: bool = True
    adaptive_confidence: bool = False  # dormant
    learning_enabled: bool = False     # dormant
```

### PortfolioSizeProfile

```python
@dataclass
class PortfolioSizeProfile:
    name: str                          # "5k"
    total_capital: float               # 5000.0
    max_position_pct: float            # 0.25
    min_position_value: float          # 500.0
    max_positions: int                 # 5
    sector_concentration_cap: float    # 0.50
    cash_reserve_pct: float            # 0.10
```

### Lookup Tables

**SIZE_PROFILES** — module-level dict:

| Profile | Capital | Max Position % | Min Position | Max Positions | Sector Cap | Cash Reserve |
|---------|---------|---------------|-------------|---------------|------------|-------------|
| `5k` | $5,000 | 25% | $500 | 5 | 50% | 10% |
| `10k` | $10,000 | 20% | $1,000 | 8 | 40% | 10% |
| `50k` | $50,000 | 10% | $2,500 | 15 | 30% | 15% |
| `100k` | $100,000 | 8% | $5,000 | 20 | 25% | 15% |

**HORIZON_PARAMS** — module-level dict in `cohort_orchestrator.py` (strategies import from here):

| Param | 30d | 3m | 6m | 1y |
|-------|-----|-----|-----|-----|
| `hold_days` default | 25 | 90 | 180 | 300 |
| `hold_days` range | (20, 45) | (60, 120) | (120, 210) | (250, 365) |
| Exit volatility threshold | tight | medium | loose | very_loose |
| Signal decay window | (5, 10) | (15, 30) | (30, 60) | (60, 120) |

### build_default_cohorts()

Iterates the cartesian product of `["30d", "3m", "6m", "1y"]` x `["5k", "10k", "50k", "100k"]`, producing 16 `CohortConfig` objects. State dirs: `{base_state_dir}/horizon_{horizon}_size_{size}/`.

---

## 2. Horizon-Aware Strategy Parameters

### Protocol Change

`StrategyModule.get_param_space()` and `get_default_params()` gain an optional `horizon` argument:

```python
def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
    ...

def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
    ...
```

### What Changes Per Horizon

Shared parameters (hold_days, signal decay, exit volatility) come from the `HORIZON_PARAMS` lookup table. Strategy-specific parameters (e.g., `min_cluster_size` for insider_activity) stay per-strategy and don't change with horizon.

### Strategy-Specific Considerations

- **earnings_call**: 30d reacts to single quarter; 1y spans 4 earnings cycles
- **insider_activity**: Longer horizons aggregate more filings before acting
- **filing_analysis**: 10-K (annual) more relevant for 6m/1y; 10-Q for 30d/3m
- **regulatory_pipeline**: Regulatory timelines are inherently long; 30d may have low signal
- **supply_chain**: Disruption recovery varies; longer horizons ride out noise
- **litigation**: Court cases are slow; 30d trades on sentiment, 1y trades on outcome
- **congressional_trades**: Holding period disclosure lag favors 3m+
- **govt_contracts**: Contract award cycles are long; 6m/1y natural fit
- **state_economics**: Macro data is slow-moving; 3m+ horizons benefit most
- **weather_ag**: Seasonal; 30d for acute events, 6m/1y for crop cycle plays

### Flow

`screen()` and `check_exit()` signatures are unchanged — they receive `params` as a dict, so horizon-adjusted values flow through naturally. The orchestrator calls `strategy.get_default_params(horizon=cohort.horizon)` before screening.

---

## 3. Orchestrator Screening & Dispatch

### Current Flow

1. Fetch data once
2. Screen once with default params → shared signals
3. OpenBB enrichment once
4. Dispatch to 2 cohorts

### New Flow

1. **Shared data fetch** — one call, same as today
2. **Screen per horizon** — 4 passes with horizon-aware params, cached by horizon key
3. **OpenBB enrichment** — dedupe signal tickers across all 4 horizon screens, fetch once
4. **Dispatch to 16 cohorts** — each cohort receives signals from its matching horizon cache

```python
def run_daily(self, trading_date):
    data = self._fetch_all_data(...)

    # Screen once per horizon (4 passes)
    horizon_signals = {}
    for horizon in ["30d", "3m", "6m", "1y"]:
        horizon_signals[horizon] = self._screen_for_horizon(data, trading_date, horizon)

    # Enrich once (dedupe tickers across horizons)
    all_tickers = dedupe(...)
    enrichment = self._fetch_openbb_enrichment(all_tickers)

    # Dispatch to all 16 cohorts
    results = {}
    for cohort in self.cohorts:
        signals = horizon_signals[cohort.horizon]
        results[cohort.name] = cohort.engine.run_paper_trade_phase(
            signals=signals, enrichment=enrichment
        )
    return results
```

Each cohort's `MultiStrategyEngine` is constructed with `PortfolioSizeProfile` values feeding into its `PortfolioCommittee` and `RiskGate`. The engine receives pre-screened signals and size-appropriate config.

---

## 4. Portfolio Committee & Risk Gate Size Awareness

### PortfolioCommittee

- Constructor accepts `PortfolioSizeProfile` (or values extracted from it)
- `_rule_based_synthesize()` uses `max_position_pct` and `sector_concentration_cap` from the profile
- `_llm_synthesize()` prompt gets capital context (e.g., "You are sizing for a $5k portfolio with max 5 positions")
- No structural change to `TradeRecommendation` — `position_size_pct` already works as a percentage

### RiskGate

- `RiskGateConfig` fields populated from `PortfolioSizeProfile` at construction time:
  - `total_capital` <- profile capital
  - `max_positions` <- profile max_positions
  - `max_position_pct` <- profile max_position_pct
  - `min_position_value` <- profile min_position_value
- New field: `cash_reserve_pct` from profile
- New gate in `check()`: reject if trade would push cash below `cash_reserve_pct` of total capital
- No changes to `check_exit()`, `enforce_stop_losses()`, or existing gate sequence

---

## 5. Comparison & Reporting

### CohortComparison

Same interface, extended for the matrix:

```python
class CohortComparison:
    def compare(self) -> dict:
        # All 16 cohorts, same per-cohort metrics as today
        ...

    def compare_by_horizon(self, size: str) -> dict:
        # Filter to cohorts matching size, compare across horizons
        ...

    def compare_by_size(self, horizon: str) -> dict:
        # Filter to cohorts matching horizon, compare across sizes
        ...

    def heatmap(self, metric: str = "sharpe") -> dict[str, dict[str, float]]:
        # Returns {horizon: {size: metric_value}}
        ...
```

### GenerationComparison

Unchanged — compares the same cohort name across generations. Example: "How did `horizon_3m_size_10k` perform in gen_005 vs gen_006?"

### Report Generation

No script-generated reports. CLAUDE.md gets a note instructing Claude to generate reports on request by reading state dirs and calling comparison methods.

---

## 6. State Directory Structure

Each cohort gets its own state dir under the generation:

```
data/generations/gen_005/
    horizon_30d_size_5k/
        paper_trades.json
        signal_journal.jsonl
        regime_snapshots.json
    horizon_30d_size_10k/
        ...
    ... (16 total)
    horizon_1y_size_100k/
        ...
```

Existing generations (gen_001 through gen_004) keep their old `control/` and `adaptive/` structure — they're frozen code snapshots.

---

## 7. What Gets Removed vs Kept

### Removed (from active code paths)

- `build_default_cohorts()` current 2-cohort control/adaptive setup — replaced with 16-cohort matrix builder
- Adaptive cohort config — no cohort sets `adaptive_confidence=True` or `learning_enabled=True` by default
- `--learning` flag handling in `run_cohorts.py` — no cohort uses it
- Adaptive state directories for new generations (`adaptive/` subdir pattern)

### Kept (dormant)

- `adaptive_confidence` and `learning_enabled` fields on `CohortConfig`
- `_compute_strategy_confidence()` in `MultiStrategyEngine`
- `run_learning_loop()` and `_should_trigger_learning_loop()`
- `PromptOptimizer` and prompt trial infrastructure
- `SignalJournal` confidence evolution logic in `CohortComparison`

### Kept (active)

- `CohortOrchestrator` shared data fetch pattern
- `CohortConfig` / `CohortComparison` infrastructure (extended)
- Portfolio committee, risk gate, state management
- All 10 strategies (extended with horizon params)
- Generation system (unchanged, runs on top)

---

## 8. Resource Impact

16 cohorts x 10 strategies = 160 strategy evaluations per daily run, mitigated by:

- **Shared data fetch** across all cohorts (already exists)
- **4 screening passes** (per horizon, not per cohort) — shared across size variants
- **1 OpenBB enrichment** call (deduped tickers across all horizons)
- **16 portfolio committee calls/day** at Haiku rates (~$0.016/day)
- **16 small JSON state dirs** per generation, negligible disk

---

## 9. Files Modified

| File | Change |
|------|--------|
| `strategies/orchestration/cohort_orchestrator.py` | New `CohortConfig` fields, `PortfolioSizeProfile`, `SIZE_PROFILES`, `HORIZON_PARAMS`, new `build_default_cohorts()`, horizon-grouped screening in `run_daily()` |
| `strategies/orchestration/cohort_comparison.py` | `compare_by_horizon()`, `compare_by_size()`, `heatmap()` methods |
| `strategies/modules/base.py` | `get_param_space(horizon)` and `get_default_params(horizon)` protocol change |
| `strategies/modules/*.py` (10 files) | Horizon-aware param implementations |
| `strategies/trading/portfolio_committee.py` | Accept `PortfolioSizeProfile`, size-aware prompts |
| `strategies/trading/risk_gate.py` | `cash_reserve_pct` field, cash reserve gate |
| `strategies/orchestration/multi_strategy_engine.py` | Pass horizon to strategy param calls, accept size profile for committee/gate construction |
| `scripts/run_cohorts.py` | Remove `--learning` flag handling |
| `CLAUDE.md` | Update architecture docs, add report generation note |
