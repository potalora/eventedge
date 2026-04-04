# Next Wave: Cohort Redesign

## 1. Remove Prompt Learning Cohort

Retire the current Control vs Adaptive (prompt optimization / journal-derived confidence) A/B trial. The adaptive cohort adds complexity without proven alpha over the control baseline.

**What to remove:**
- Adaptive cohort config in `build_default_cohorts()`
- Weekly learning loop (`--learning` in `run_cohorts.py`)
- Journal-derived `strategy_confidence` logic
- Prompt optimization infrastructure
- `adaptive_confidence` config key
- Adaptive state directories (`data/state/adaptive/`, `data/generations/*/adaptive/`)

**What to keep:**
- `CohortOrchestrator` shared data fetch pattern
- `CohortConfig` / `CohortComparison` infrastructure (reused for new cohorts)
- Portfolio committee, risk gate, state management
- All 10 strategies

---

## 2. New Cohort Dimensions

Replace the Control/Adaptive split with a matrix of **investment horizons** and **portfolio sizes**.

### Investment Horizons

| Cohort | Horizon | Hold Days Range | Rebalance Cadence |
|--------|---------|-----------------|-------------------|
| `horizon_30d` | 30 days | 20-45 days | Weekly |
| `horizon_3m` | 3 months | 60-120 days | Bi-weekly |
| `horizon_6m` | 6 months | 120-210 days | Monthly |
| `horizon_1y` | 1 year | 250-365 days | Quarterly |

### Portfolio Sizes

| Cohort | Capital | Min Position | Max Positions |
|--------|---------|-------------|---------------|
| `size_5k` | $5,000 | $500 | 5 |
| `size_10k` | $10,000 | $1,000 | 8 |
| `size_50k` | $50,000 | $2,500 | 15 |
| `size_100k` | $100,000 | $5,000 | 20 |

### Full Matrix

Each combination runs as an independent cohort with its own state directory:

```
horizon_30d_size_5k    horizon_30d_size_10k    horizon_30d_size_50k    horizon_30d_size_100k
horizon_3m_size_5k     horizon_3m_size_10k     horizon_3m_size_50k     horizon_3m_size_100k
horizon_6m_size_5k     horizon_6m_size_10k     horizon_6m_size_50k     horizon_6m_size_100k
horizon_1y_size_5k     horizon_1y_size_10k     horizon_1y_size_50k     horizon_1y_size_100k
```

16 cohorts total. State dirs: `data/state/{cohort_name}/`

---

## 3. Strategy Adjustments by Horizon

All 10 strategies need horizon-aware parameter spaces.

### What Changes Per Horizon

| Parameter | 30d | 3m | 6m | 1y |
|-----------|-----|-----|-----|-----|
| `hold_days` default | 25 | 90 | 180 | 300 |
| `hold_days` range | 20-45 | 60-120 | 120-210 | 250-365 |
| `rebalance_days` | 7 | 14 | 30 | 90 |
| Exit volatility threshold | Tight | Medium | Loose | Very loose |
| Signal decay window | 5-10 days | 15-30 days | 30-60 days | 60-120 days |

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

### Implementation Approach

Each strategy's `get_param_space()` and `get_default_params()` accept a `horizon` argument:

```python
def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
    ...

def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
    ...
```

---

## 4. Portfolio Size Adjustments

The portfolio committee and risk gate need size-aware logic.

| Rule | 5k | 10k | 50k | 100k |
|------|-----|------|------|------|
| Max single position % | 25% | 20% | 10% | 8% |
| Min position size | $500 | $1,000 | $2,500 | $5,000 |
| Max concurrent positions | 5 | 8 | 15 | 20 |
| Sector concentration cap | 50% | 40% | 30% | 25% |
| Cash reserve minimum | 10% | 10% | 15% | 15% |

Smaller portfolios accept higher concentration (fewer positions, bigger bets). Larger portfolios enforce stricter diversification.

---

## 5. Comparison & Reporting

Extend `CohortComparison` and daily reports to support the matrix:

- Compare across horizons (fixed size): "How does 30d vs 1y perform at $50k?"
- Compare across sizes (fixed horizon): "How does $5k vs $100k perform at 3m?"
- Heatmap view: horizon x size with Sharpe ratio / total return in each cell
- Per-cohort signal log, trade journal, and P&L tracking

---

## 6. Resource Considerations

16 cohorts x 10 strategies = 160 strategy evaluations per daily run. Mitigation:

- Shared data fetch across all cohorts (already exists in orchestrator)
- Strategies screen once per horizon (not per size) — 4 screens, not 16
- Portfolio committee runs per cohort (size affects sizing, so 16 calls)
- LLM calls: ~16 portfolio committee calls/day at Haiku rates (~$0.016/day)
- State: 16 small JSON state dirs, negligible disk
