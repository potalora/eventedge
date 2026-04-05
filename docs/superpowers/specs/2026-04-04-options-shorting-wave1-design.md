# Options & Short Selling — Wave 1 Design Spec

Follow-on to the 16-cohort horizon x size matrix. Adds covered calls and short equity to eligible cohorts via the existing pipeline (Approach A: extend, not parallel subsystem).

---

## Scope

**Wave 1 (this spec):**
- Covered calls (committee-initiated overlay on existing longs)
- Short equity (strategy-emitted bearish signals)

**Wave 2+ (future specs):**
- Put spreads, call spreads, LEAPS, protective puts
- Alpaca live trading (all vehicles — longs, shorts, options)
- Real borrow rate data from broker API
- Debate validation for high-conviction shorts

---

## 1. Data Model Changes

### OptionSpec (new dataclass in `modules/base.py`)

```python
@dataclass
class OptionSpec:
    strategy: Literal["covered_call", "protective_put", "put_spread", "call_spread", "leaps"]
    expiry_target_days: int       # desired DTE
    strike_offset_pct: float      # e.g. -0.05 for 5% OTM put
    max_premium_pct: float        # max premium as % of position value
```

### Candidate (extend existing in `modules/base.py`)

Add two fields with backward-compatible defaults:

```python
@dataclass
class Candidate:
    ticker: str
    date: str
    direction: str = "long"              # existing
    score: float = 0.0                   # existing
    confidence: float = 0.0              # existing
    metadata: dict = field(default_factory=dict)  # existing
    vehicle: str = "equity"              # NEW — "equity" or "option"
    option_spec: OptionSpec | None = None # NEW — only when vehicle="option"
```

### PortfolioSizeProfile (extend existing in `cohort_orchestrator.py`)

```python
@dataclass
class PortfolioSizeProfile:
    # ... existing fields ...
    short_eligible: bool = False
    options_eligible: list[str] = field(default_factory=list)
    max_short_exposure_pct: float = 0.0
    max_single_short_pct: float = 0.05
    max_options_premium_pct: float = 0.0
    margin_cash_buffer_pct: float = 0.0
    max_correlated_shorts: int = 0
```

### Eligibility by Size Tier

| Tier | `short_eligible` | `options_eligible` | `max_short_exposure_pct` | `max_options_premium_pct` | `margin_cash_buffer_pct` | `max_correlated_shorts` |
|------|-------------------|--------------------|--------------------------|---------------------------|--------------------------|------------------------|
| 5k   | False | [] | 0% | 0% | 0% | 0 |
| 10k  | False | ["covered_call"] | 0% | 5% | 0% | 0 |
| 50k  | True  | ["covered_call"] | 15% | 5% | 20% | 2 |
| 100k | True  | ["covered_call"] | 20% | 8% | 15% | 4 |

Note: Wave 1 only implements covered calls and short equity. Options list will expand in Wave 2 (put_spread, call_spread, etc. for 50k+/100k+).

Horizon gating: short selling requires 3m+ horizon. Covered calls work at any horizon >= 30d.

---

## 2. Strategy Signal Changes

### Strategies adding short signals

These strategies already return `direction="short"` or will be extended to:

| Strategy | Short Signal Source | Already emits short? |
|----------|-------------------|---------------------|
| `litigation` | Major adverse ruling, large settlement | Yes |
| `congressional_trades` | Cluster of congressional selling | Yes |
| `regulatory_pipeline` | Negative regulatory action, denial | **Add** |
| `supply_chain` | Critical supplier disruption, no alternatives | **Add** |
| `insider_activity` | C-suite cluster selling (3+ officers) | **Add** |
| `earnings_call` | Guidance cut, margin compression | **Add** (vehicle="option" with put spread in Wave 2; short equity for now) |

### Strategies that stay long-only

`govt_contracts`, `state_economics`, `weather_ag`, `filing_analysis` — bearish signals are better expressed as exits, not short entries.

### Vehicle selection at strategy level

Strategies set `vehicle="equity"` by default. In Wave 2, strategies with specific options conviction (e.g., earnings_call put spread around a known date) can set `vehicle="option"` + `option_spec`. For now, all short signals are equity-only.

### Covered calls

Not strategy-emitted. The portfolio committee identifies overlay candidates from existing long positions during its synthesis step (see Section 3).

---

## 3. Portfolio Committee Changes

File: `strategies/trading/portfolio_committee.py`

### Vehicle selection

When a strategy returns `direction="short"`, the committee checks cohort eligibility from `PortfolioSizeProfile`:
- Cohort eligible (short_eligible=True, horizon >= 3m) -> accept
- Cohort ineligible -> downgrade to "avoid" (skip the ticker entirely)

### Short book limits

Enforced as hard limits in committee output, sourced from `PortfolioSizeProfile`:
- Max short exposure (% of portfolio): 15% (50k) / 20% (100k)
- Max single short position: 5% of portfolio
- Max options premium deployed: 5% (50k) / 8% (100k)
- Max correlated short positions: 2 (50k) / 4 (100k)

### Covered call overlay

New step in committee synthesis, after processing incoming strategy signals:

1. Committee receives list of current long positions with: ticker, days held, current P&L, IV data (from yfinance), upcoming earnings dates
2. Sonnet LLM decides which positions are good covered call candidates
3. For each overlay candidate, committee emits a `TradeRecommendation` with `vehicle="option"` and an `OptionSpec(strategy="covered_call", ...)`

Committee considers: days held, IV rank, upcoming catalysts/earnings, recent price action, position profitability.

### TradeRecommendation changes

```python
@dataclass
class TradeRecommendation:
    # ... existing fields ...
    vehicle: str = "equity"
    option_spec: OptionSpec | None = None
```

### LLM model

All autoresearch LLM calls switch from Haiku to Sonnet (`claude-sonnet-4-20250514`). Config key `autoresearch_model` updated.

---

## 4. Risk Gate Additions

File: `strategies/trading/risk_gate.py`

### New gates for short positions

| Gate | Rule | Config Key |
|------|------|-----------|
| Short squeeze stop | Auto-cover if short moves 15% against within 5 trading days | `short_squeeze_stop_pct`, `short_squeeze_window_days` |
| Earnings blackout | No new short entries within 5 trading days of earnings | `earnings_blackout_days` |
| Borrow cost check | Skip short if estimated borrow cost >5% annualized | `max_borrow_cost_pct` |
| Margin utilization | Reject new short entries if margin utilization >70% (do not auto-close existing) | `max_margin_utilization_pct` |

### New gates for options

| Gate | Rule | Config Key |
|------|------|-----------|
| Premium decay floor | Close option if remaining value <20% of entry premium | `premium_decay_floor_pct` |
| Max premium deployment | Reject if total options premium exceeds cohort limit | Uses `PortfolioSizeProfile.max_options_premium_pct` |

### Borrow cost estimation heuristic

Derived from OpenBB short interest data (no additional API calls):

| Short Interest % | Estimated Annualized Borrow Cost |
|-----------------|--------------------------------|
| < 5% | 0.5% |
| 5-15% | 2.0% |
| 15-30% | 5.0% |
| > 30% | Reject (hard-to-borrow) |

### RiskGateConfig additions

```python
long_only: bool = True                    # flipped to False for eligible cohorts
short_squeeze_stop_pct: float = 0.15
short_squeeze_window_days: int = 5
earnings_blackout_days: int = 5
max_borrow_cost_pct: float = 0.05
max_margin_utilization_pct: float = 0.70
premium_decay_floor_pct: float = 0.20
```

The `long_only` flag is set per-cohort based on `PortfolioSizeProfile.short_eligible` — 5k/10k cohorts stay `long_only=True`, 50k/100k get `False` (at 3m+ horizons only).

---

## 5. PaperBroker Execution Changes

File: `execution/paper_broker.py`

### Short position tracking

New state alongside existing positions:

```python
short_positions: dict[str, ShortPosition]  # symbol -> position
margin_used: float                          # total margin reserved
accrued_borrow_cost: float                  # cumulative borrow cost
```

New methods:
- `submit_short_sell(symbol, qty, stop_loss)` — opens short, reserves margin (150% of position value, Reg T estimate)
- `submit_cover(symbol, qty)` — closes short, releases margin, calculates inverted P&L
- `accrue_borrow_cost(date)` — daily accrual based on SI% heuristic, deducted from cash

### Options position tracking

New state:

```python
option_positions: dict[str, OptionPosition]  # option_id -> position
```

Extend existing `submit_options_order()` to track:
- Entry premium
- DTE at entry
- Position type (covered_call, etc.)
- Linked stock position (for covered calls)

New method:
- `close_option(symbol, option_id)` — close by position ID, calculate P&L

### Margin simulation

- Short positions reserve 150% of position value as margin (Reg T)
- Margin utilization = `margin_used / total_equity`
- Margin recalculated on price changes (mark-to-market daily)

---

## 6. State Persistence

Extend cohort state JSON (`data/state/` per cohort) with:

```json
{
  "short_positions": [],
  "option_positions": [],
  "margin_used": 0.0,
  "accrued_borrow_cost": 0.0
}
```

Backward compatible — missing keys default to empty/zero on load.

---

## 7. Config Changes

### `default_config.py`

```python
# Autoresearch model upgrade
"autoresearch_model": "claude-sonnet-4-20250514",

# Options config (extend existing)
"options": {
    # ... existing ...
    "covered_call_min_hold_days": 14,
    "covered_call_default_dte": 30,
    "covered_call_strike_offset": 0.05,  # 5% OTM
},

# Short selling config (new section)
"short_selling": {
    "borrow_cost_tiers": {
        5: 0.005,
        15: 0.02,
        30: 0.05,
    },
    "borrow_cost_reject_above": 0.05,
    "hard_to_borrow_si_pct": 30,
},
```

---

## 8. Data Dependencies

All data needed is already available — no new API integrations:

| Need | Source | Status |
|------|--------|--------|
| Options chains | yfinance | Available |
| Implied volatility | yfinance | Available |
| Short interest % | OpenBB `equity_short_interest` | Available |
| Earnings dates | yfinance | Available |
| Greeks | py_vollib via `options_data.py` | Available |
| Borrow rates | Estimated from SI% heuristic | New logic, no API |

---

## 9. Testing

- Unit tests for `OptionSpec` and extended `Candidate` dataclass
- Unit tests for eligibility gating in `PortfolioSizeProfile`
- Unit tests for each new risk gate (squeeze stop, blackout, borrow cost, margin, premium decay)
- Unit tests for PaperBroker short position lifecycle (open, accrue, cover, P&L)
- Unit tests for PaperBroker options position lifecycle (open, decay check, close)
- Unit tests for committee vehicle selection and covered call overlay logic
- Integration test: full pipeline with short-eligible cohort producing a short trade
- Integration test: covered call overlay on existing long position
- All tests mock LLM and external API calls per project conventions
