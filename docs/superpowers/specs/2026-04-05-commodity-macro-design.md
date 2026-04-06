# Commodity Macro Strategy — Design Spec

**Date:** 2026-04-05
**Branch:** feature/trading-extensions
**Status:** Approved

## Overview

Add a new `commodity_macro` strategy that trades non-agricultural commodity ETFs
based on CFTC Commitments of Traders (COT) positioning extremes, macro regime
confirmation, and regulatory/supply chain catalysts. Includes a new `CFTCSource`
data source, cohort-aware instrument filtering, portfolio committee commodity
awareness, commodity enrichment pipeline, and covered call overlay support.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Scope boundary with `weather_ag` | Non-agricultural only | Different analytical frameworks (positioning vs catalyst). Portfolio committee handles convergence if both signal same ticker. |
| Instruments | ETFs only (v1) | PaperBroker lacks futures margin/rolling. `FUTURES_TO_ETF_MAP` preserves futures upgrade path. |
| COT data source | Standalone `cot_reports` library (`CFTCSource`) | Simpler than `openbb-cftc`, no dependency chain, easier to test independently. |
| LLM usage | Rule-based gate, LLM scores (pattern A) | Matches `weather_ag` pattern. LLM adds value for catalyst interpretation. Gate keeps costs down. |
| Architecture | Full integration (Approach 3) | Strategy + cohort filtering + enrichment pipeline + covered call support. Clean gen_006 evaluation from day one. |

## 1. New Files

### `tradingagents/strategies/data_sources/cftc_source.py`

New `CFTCSource` data source wrapping `cot_reports` library. Implements the
`DataSource` protocol. Two methods:

- `cot_report` — fetches a specific COT report type (disaggregated, legacy, TFF)
- `cot_positioning` — returns net speculative positioning + percentile rank for
  given commodities over a lookback window

Registered in `build_default_registry()`. No API key needed. Graceful `ImportError`
skip if `cot_reports` not installed (same pattern as OpenBB).

**Dependency:** `pip install cot_reports` as optional extra in `pyproject.toml`
(`pip install -e ".[commodities]"`).

### `tradingagents/strategies/modules/commodity_macro.py`

New `CommodityMacroStrategy` implementing the `StrategyModule` protocol. Contains:

- `COMMODITY_UNIVERSE` — tiered ticker dict with `FUTURES_TO_ETF_MAP`
- `screen()` — three-pillar gate logic (COT + macro + catalyst)
- `check_exit()` — hold period + COT normalization exit
- LLM prompt for `commodity_macro` analysis type

## 2. Strategy Signal Logic

### screen() Flow

**Step 1 — COT Gate (primary signal)**

- Pull COT data from `data["cftc"]` for gold, crude, silver, nat gas, copper
- Compute net speculative positioning as percentile over trailing 52 weeks
- Gate triggers when any commodity's speculative positioning is above 85th or
  below 15th percentile (extreme)
- Direction: extreme long positioning → contrarian short signal. Extreme short →
  contrarian long.

**Step 2 — Macro Confirmation**

- Pull FRED data from `data["fred"]` (already fetched by engine)
- Check: real rates direction (fed funds minus CPI), dollar strength proxy
  (yield curve slope), CPI momentum (3m delta)
- Macro must not contradict the COT signal. Veto rules:
  - Long gold/silver + real rates rising >50bps over 3 months → veto
  - Long energy + CPI momentum negative (deflation signal) → veto
  - Short any commodity + strong risk-on regime (VIX < 15, benign) → veto
  - All other combinations → pass (no veto)
- This is a filter (vetoes bad setups), not a signal generator.

**Step 3 — Catalyst Scan (optional boost)**

- Scan `data["regulations"]` for energy/mining/metals keywords
- Scan `data["finnhub"]` for commodity-relevant supply chain disruptions
- Aligned catalyst boosts score. No catalyst → signal still valid, lower score.

**Step 4 — Emit Candidates**

- Look up ETF proxy via `FUTURES_TO_ETF_MAP`
- Check ETF is in horizon/size eligible set (passed via params)
- If ETF is in `SHORT_ONLY_ETFS` and direction is long → substitute (`CL=F` long
  → `XLE` long) or skip
- Emit `Candidate(ticker=ETF, direction=..., score=..., metadata={...})`
- Set `metadata["needs_llm_analysis"] = True`, `analysis_type = "commodity_macro"`

### Parameter Space

| Parameter | Range | Default | Description |
|-----------|-------|---------|-------------|
| `cot_extreme_pct` | (75, 95) | 85 | Percentile threshold for extreme positioning |
| `cot_lookback_weeks` | (26, 104) | 52 | Lookback window for percentile calculation |
| `hold_days` | horizon-dependent | from `HORIZON_PARAMS` | Holding period |
| `macro_veto_enabled` | (True, False) | True | Whether macro confirmation required |
| `catalyst_boost` | (0.0, 0.3) | 0.15 | Score boost when catalyst present |

### Exit Logic

- Primary: hold period reached
- Early exit: COT positioning normalizes (crosses back inside 30th-70th percentile)

## 3. CFTCSource Data Source

**Class:** `CFTCSource`
- `name = "cftc"`
- `requires_api_key = False`
- Wraps `cot_reports` library. Graceful `ImportError` if not installed.
- In-memory cache per session (COT data is weekly).

### Methods

**`cot_report(params)`** — Low-level fetch
- Input: `{"method": "cot_report", "report_type": "disaggregated_futures"}`
- Returns raw DataFrame as dict
- Supported types: `legacy_futures`, `disaggregated_futures`,
  `traders_in_financial_futures`

**`cot_positioning(params)`** — High-level analysis
- Input: `{"method": "cot_positioning", "commodities": ["gold", "crude_oil", ...], "lookback_weeks": 52}`
- Fetches disaggregated futures report
- Filters by commodity contract codes
- Computes: net speculative position (Managed Money long - short), percentile rank,
  week-over-week change
- Returns:
  ```json
  {
    "gold": {"net_position": 142000, "percentile": 0.87, "wow_change": -3200, "direction_signal": "short"},
    "crude_oil": {"net_position": -45000, "percentile": 0.12, "wow_change": 8100, "direction_signal": "long"}
  }
  ```

### Commodity Code Mapping

```python
COMMODITY_CODES = {
    "gold": "GOLD - COMEX",
    "silver": "SILVER - COMEX",
    "crude_oil": "CRUDE OIL, LIGHT SWEET - NEW YORK MERCANTILE EXCHANGE",
    "nat_gas": "NATURAL GAS - NEW YORK MERCANTILE EXCHANGE",
    "copper": "COPPER-GRADE #1 - COMEX",
}
```

**Implementation note:** These exact strings must be validated against actual CFTC
report data during implementation. The contract names in CFTC reports can vary.
The live API smoke tests (Section 8) are specifically designed to catch mismatches.

## 4. Cohort Integration

### PortfolioSizeProfile Additions

Three new fields:

```python
commodity_eligible: bool = False
max_commodity_allocation_pct: float = 0.0
commodity_instruments: list[str] = field(default_factory=list)
```

### Per-Profile Configuration

| Profile | `commodity_eligible` | `max_commodity_allocation_pct` | `commodity_instruments` |
|---------|:---:|:---:|---|
| 5k | `False` | 0.0 | `[]` |
| 10k | `True` | 0.10 | `["GLD", "SLV", "PDBC"]` |
| 50k | `True` | 0.10 | `["GLD", "SLV", "PDBC", "COPX", "XLE", "USO", "UNG"]` |
| 100k | `True` | 0.10 | `["GLD", "SLV", "PDBC", "COPX", "XLE", "USO", "UNG"]` |

### HORIZON_PARAMS Additions

| Horizon | `commodity_eligible` | `commodity_signal_decay_window` | `commodity_instruments_override` |
|---------|:---:|---|---|
| 30d | `False` | n/a | n/a — strategy skips |
| 3m | `True` | `(7, 21)` | None — uses size profile |
| 6m | `True` | `(14, 45)` | None |
| 1y | `True` | `(30, 90)` | `["GLD", "SLV"]` only |

### Instrument Filtering

Eligible instruments = intersection of size profile's `commodity_instruments` and
any horizon-level `commodity_instruments_override`. Examples:

- 3m/10k → `["GLD", "SLV", "PDBC"]`
- 1y/50k → `["GLD", "SLV"]` (horizon override narrows the 50k list)
- 30d/anything → no candidates emitted

### USO/UNG Short-Only Enforcement

Handled inside `commodity_macro.screen()`. When COT analysis suggests going long
crude or nat gas:
- `CL=F` long → emit `XLE` long (energy sector proxy) if eligible, else skip
- `NG=F` long → skip (no suitable long proxy)
- Short direction passes through normally

## 5. Portfolio Committee Changes

### 5a. Commodity Allocation Cap

New block in `_rule_based_synthesize()` after existing sector concentration
enforcement. Identifies commodity tickers from size profile's
`commodity_instruments`, sums allocation, scales down proportionally if exceeding
`max_commodity_allocation_pct`. Same pattern as existing short exposure cap.

### 5b. Commodity-Aware Regime Alignment

Modify `_assess_regime_alignment()` to accept optional `ticker` parameter
(default `""`). Existing callers unaffected.

| Regime | Ticker | Direction | Alignment |
|--------|--------|-----------|-----------|
| Crisis/Stressed | GLD/SLV | Long | **Aligned** (safe haven) |
| Crisis/Stressed | Other commodity | Long | Misaligned |
| Crisis/Stressed | Any | Short | Aligned |
| Benign | Any commodity | Long | Neutral |
| Normal | Any | Any | Neutral |

Safe haven set: `SAFE_HAVEN_ETFS = {"GLD", "SLV"}` — hardcoded constant.

### 5c. LLM Synthesis Prompt

Extend `_build_prompt()` to include commodity enrichment context when available:

```
Commodity context:
  GLD: COT speculative percentile=87%, contango=n/a (physical)
  USO: COT speculative percentile=22%, contango=2.1%/month
```

Same pattern as existing sector classification and short interest blocks.

## 6. Commodity Enrichment Pipeline

### OpenBB Futures Curves

Extend `CohortOrchestrator._fetch_openbb_enrichment()` to fetch futures term
structure for commodity signals via OpenBB's `derivatives.futures.historical`.

Uses `ETF_TO_FUTURES_UNDERLYING` map to look up which curve to fetch.

Output in `enrichment["commodity_futures_curves"]`:
```json
{
    "CL": {"front_month": 72.50, "second_month": 73.80, "contango_pct": 1.8},
    "GC": {"front_month": 2340, "second_month": 2338, "contango_pct": -0.1}
}
```

Graceful degradation — missing curves just mean that commodity lacks contango data.

### New FRED Commodity Series

Add to `SERIES_MAP` in `fred_source.py`:

```python
"wti_spot": "DCOILWTICO",
"gold_spot": "GOLDAMGBD228NLBM",
"copper_spot": "PCOPPUSDM",
```

Fetched alongside existing FRED data. `commodity_macro` reads from `data["fred"]`
for macro confirmation cross-referencing.

### Covered Call Overlay Extension

GLD and SLV flagged as preferred covered call candidates in
`generate_covered_call_overlays()`:

- `earnings_in` set to `999` for commodity ETFs (no earnings gap risk)
- LLM overlay context notes: predictable IV, no binary events
- Small conditional: if ticker in `COMMODITY_ETFS`, override earnings_days and
  add commodity-specific note to LLM context

## 7. Instrument Constants

All defined in `commodity_macro.py`:

```python
FUTURES_TO_ETF_MAP = {
    "GC=F": "GLD",
    "SI=F": "SLV",
    "CL=F": "USO",
    "NG=F": "UNG",
    "HG=F": "COPX",
}

ETF_TO_FUTURES_UNDERLYING = {
    "GLD": "GC",
    "SLV": "SI",
    "USO": "CL",
    "UNG": "NG",
    "COPX": "HG",
    "PDBC": None,
    "XLE": "CL",
}

SHORT_ONLY_ETFS = {"USO", "UNG"}
SAFE_HAVEN_ETFS = {"GLD", "SLV"}
COMMODITY_ETFS = {"GLD", "SLV", "PDBC", "COPX", "XLE", "USO", "UNG"}
```

**Future-proofing:** When futures execution is added, a config flag
`use_futures_direct: bool = False` skips the ETF translation in `screen()`.
Strategy logic unchanged.

## 8. Testing

### Unit Tests (`tests/test_commodity_macro.py`)

| Test | What it validates |
|------|-------------------|
| `test_cftc_source_positioning` | Mock COT data → correct percentiles and direction signals |
| `test_cftc_source_unavailable` | Graceful degradation when `cot_reports` not installed |
| `test_screen_cot_gate_triggers` | Extreme positioning (90th pctl) → candidates with correct ETF tickers |
| `test_screen_cot_gate_no_trigger` | Moderate positioning (50th pctl) → empty list |
| `test_screen_macro_veto` | COT extreme + contradicting macro → no candidates |
| `test_screen_catalyst_boost` | Score increases when catalyst aligns |
| `test_short_only_enforcement` | COT long crude → USO NOT emitted long, XLE substituted or skipped |
| `test_horizon_filtering` | 30d → no candidates. 3m → candidates. 1y → GLD/SLV only. |
| `test_futures_to_etf_map` | All map entries resolve, no dangling keys |
| `test_check_exit_hold_period` | Standard hold period exit |
| `test_check_exit_cot_normalization` | Early exit on positioning normalization |

### Integration Tests (`tests/test_commodity_macro_integration.py`)

| Test | What it validates |
|------|-------------------|
| `test_commodity_in_cohort_matrix` | 3m/10k cohort sizes commodities within `max_commodity_allocation_pct` |
| `test_commodity_regime_alignment` | Crisis + GLD long = aligned, crisis + XLE long = misaligned |
| `test_covered_call_overlay_on_gld` | GLD long position → overlay considers it with earnings_days=999 |
| `test_30d_cohort_excludes_commodities` | 30d cohort gets no commodity candidates |
| `test_5k_cohort_excludes_commodities` | 5k profiles get no commodity candidates |

### Live API Smoke Tests (`tests/test_commodity_macro_live.py`)

Marked with `@pytest.mark.live`, skipped in normal `pytest` runs, invoked with
`pytest -m live`.

| Test | What it validates |
|------|-------------------|
| `test_cftc_source_live_fetch` | Real COT report is non-empty, expected columns present, `COMMODITY_CODES` strings match actual data |
| `test_cftc_positioning_live` | Live positioning for gold/crude returns percentiles in 0-1 range, non-null net positions |
| `test_fred_commodity_series_live` | `DCOILWTICO`, `GOLDAMGBD228NLBM`, `PCOPPUSDM` return non-empty series with recent dates |
| `test_openbb_futures_curve_live` | Gold/crude futures curves present, contango calculation sane (if OpenBB installed) |

## 9. Files Modified

| File | Change |
|------|--------|
| `tradingagents/strategies/data_sources/cftc_source.py` | **New** — CFTCSource |
| `tradingagents/strategies/data_sources/registry.py` | Register CFTCSource in `build_default_registry()` |
| `tradingagents/strategies/data_sources/__init__.py` | Export CFTCSource |
| `tradingagents/strategies/data_sources/fred_source.py` | Add 3 commodity series to `SERIES_MAP` |
| `tradingagents/strategies/data_sources/openbb_source.py` | Add `commodity_futures_curve` method |
| `tradingagents/strategies/modules/commodity_macro.py` | **New** — CommodityMacroStrategy |
| `tradingagents/strategies/modules/__init__.py` | Register strategy, add to `get_paper_trade_strategies()` |
| `tradingagents/strategies/learning/llm_analyzer.py` | Add `commodity_macro` prompt to `_DEFAULT_PROMPTS` |
| `tradingagents/strategies/orchestration/cohort_orchestrator.py` | Add fields to `PortfolioSizeProfile`, update `SIZE_PROFILES`, update `HORIZON_PARAMS`, extend `_fetch_openbb_enrichment()` |
| `tradingagents/strategies/orchestration/multi_strategy_engine.py` | Add CFTC to `_fetch_all_data()` |
| `tradingagents/strategies/trading/portfolio_committee.py` | Commodity allocation cap, regime alignment, LLM prompt |
| `pyproject.toml` | Add `commodities` optional extra with `cot_reports` |
| `tests/test_commodity_macro.py` | **New** — unit tests |
| `tests/test_commodity_macro_integration.py` | **New** — integration tests |
| `tests/test_commodity_macro_live.py` | **New** — live API smoke tests |
