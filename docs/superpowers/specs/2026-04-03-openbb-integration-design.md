# OpenBB Integration Design Spec

**Date:** 2026-04-03
**Branch:** feature/trading-extensions
**Status:** Approved

## Summary

Add `OpenBBSource` to the autoresearch data source registry, exposing 8 data methods that fill critical gaps in the system: sector classification, analyst consensus estimates, structured insider trading data, short interest, government trades via stable API, unusual options activity, SEC litigation releases, and Fama-French factors. Integrate these data into all 7 active strategies, reactivate 2 archived strategies (`govt_contracts`, `state_economics`), and enrich the portfolio committee with sector concentration enforcement and factor context. Validate via 30-day simulation, then launch as a new generation for live paper trading.

## Approach

**Single `OpenBBSource` with lazy sub-modules (Approach C).** One class registered as `"openbb"` in the `DataSourceRegistry`. Internally organized into method groups (`equity_*`, `derivatives_*`, `regulators_*`, `factors_*`). OpenBB imported lazily on first `fetch()` call. Selective package install (~24 MB) instead of full OpenBB (~37 MB).

### Why This Approach

- Matches existing `DataSource` protocol pattern (like FREDSource, CongressSource)
- Single registry entry keeps strategy integration simple (`data_sources = ["openbb", "yfinance"]`)
- Lazy import means zero cost if no strategy uses OpenBB
- Selective install respects 16GB M4 Air constraint
- Internal method grouping keeps code navigable without multiple registry entries

## Installation

Selective packages only:

```bash
pip install openbb-core openbb-equity openbb-derivatives openbb-regulators \
            openbb-sec openbb-yfinance openbb-fmp openbb-congress-gov \
            openbb-government-us openbb-finra openbb-famafrench
```

~24 MB total. All providers used are free (no key) except FMP (free tier: 250 calls/day).

## OpenBBSource Architecture

**File:** `tradingagents/autoresearch/data_sources/openbb_source.py` (~350 lines)

**Class:** `OpenBBSource` implementing the `DataSource` protocol:
- `name = "openbb"`
- `requires_api_key = False` (FMP key is optional; core functionality works without it)
- `fetch(params)` dispatches on `params["method"]`
- `is_available()` returns `True` if `openbb` is importable, `False` otherwise
- Lazy `obb` singleton initialization on first fetch
- In-memory cache per session with `clear_cache()`

### Methods

| Method | Params | Returns | Provider |
|--------|--------|---------|----------|
| `equity_profile` | `ticker` | `{"sector", "industry", "market_cap", "name", "description"}` | yfinance (free) |
| `equity_estimates` | `ticker` | `{"consensus_eps", "consensus_revenue", "price_target_mean", "price_target_high", "price_target_low", "num_analysts", "forward_pe"}` | FMP (free tier) |
| `equity_insider_trading` | `ticker` | `{"trades": [{"owner", "title", "transaction_type", "shares", "price", "value", "date", "ownership_type"}]}` | SEC (free) |
| `equity_short_interest` | `ticker` | `{"short_interest", "short_pct_of_float", "days_to_cover", "date"}` | FINRA (free) |
| `equity_government_trades` | `days_back` | `{"trades": [{"ticker", "representative", "chamber", "transaction_type", "amount", "transaction_date", "district"}]}` | congress-gov (free) |
| `derivatives_options_unusual` | (none) | `{"unusual": [{"ticker", "contract_type", "strike", "expiration", "volume", "open_interest", "vol_oi_ratio"}]}` | best-effort (graceful fallback if unavailable) |
| `regulators_sec_litigation` | (none) | `{"releases": [{"title", "date", "url", "category"}]}` | SEC (free) |
| `factors_fama_french` | `model` ("5"), `period` ("monthly") | `{"factors": {"Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"}, "history": dict}` | famafrench (free) |

### Error Handling

- Each method catches exceptions and returns `{"error": "..."}` (same pattern as existing sources)
- Provider missing API key: OpenBB auto-skips, method returns empty/error
- Rate limiting: OpenBB providers handle their own limits
- OpenBB not installed: `is_available()` returns `False`, registry skips, strategies run without OpenBB data

## Strategy Integration

All strategy changes are additive. Every OpenBB data access is gated on `if "openbb" in data and "key" in data["openbb"]:`. No strategy breaks if OpenBB is unavailable.

### Modified Strategies (7)

**`earnings_call.py`** -- Add `"openbb"` to `data_sources`
- Enrich `screen()` with `data["openbb"]["estimates"]` for consensus EPS
- Use EPS surprise magnitude (actual vs consensus) as scoring signal
- More analysts covering = higher conviction

**`insider_activity.py`** -- Add `"openbb"` to `data_sources`
- Enrich with `data["openbb"]["insider_trading"]` for structured `title` field
- Officer-level weighting (C-suite buys score higher than director buys)
- Pull `data["openbb"]["profile"]` for sector context

**`congressional_trades.py`** -- Add `"openbb"` to `data_sources`
- Use `data["openbb"]["government_trades"]` as primary source
- Fall back to `data["congress"]` (CapitolTrades RSC scraping) if OpenBB returns empty
- Map field names to existing normalized format

**`supply_chain.py`** -- Add `"openbb"` to `data_sources`
- After identifying disruption candidates, pull `data["openbb"]["short_interest"]`
- High short interest + disruption = amplified signal (squeeze potential)
- Pull `data["openbb"]["profile"]` for sector context

**`litigation.py`** -- Add `"openbb"` to `data_sources`
- Merge `data["openbb"]["sec_litigation"]` with `data["courtlistener"]["dockets"]`
- SEC enforcement actions weighted higher (SEC doesn't bring frivolous cases)

**`filing_analysis.py`** -- Add `"openbb"` to `data_sources`
- Pull `data["openbb"]["estimates"]` to compare filing disclosures against Street expectations
- Filings contradicting analyst consensus = higher conviction
- Pull `data["openbb"]["profile"]` for sector

**`regulatory_pipeline.py`** -- Add `"openbb"` to `data_sources`
- Pull `data["openbb"]["profile"]` for sector validation on ticker-to-regulation mapping
- Reduces false positive mappings

### Reactivated Strategies (2)

**`govt_contracts.py`** -- Move from `_archive/` to `strategies/`
- Change `track` from `"backtest"` to `"paper_trade"`
- Change `data_sources` to `["usaspending", "yfinance", "openbb"]`
- Rewrite `screen()` to use USASpendingSource for actual contract data + OpenBB for enrichment (profile, estimates, short interest)
- Exit logic (profit targets, stop losses, hold period) stays unchanged

**`state_economics.py`** -- Move from `_archive/` to `strategies/`
- Change `track` from `"backtest"` to `"paper_trade"`
- Change `data_sources` to `["fred", "yfinance", "openbb"]`
- Rewrite `screen()` to combine FRED economic indicators with ETF momentum (composite signal)
- Use `data["openbb"]["factors_fama_french"]` for factor exposure context
- Exit logic stays unchanged

### Strategies NOT Changed

`_archive/weather_ag.py` -- Stays archived. OpenBB doesn't provide NOAA/USDA weather data needed for the core signal.

## Portfolio Committee Changes

Changes to `portfolio_committee.py` and `cohort_orchestrator.py`:

**Data flow change:**
```
CohortOrchestrator
  -> registry.fetch_all(strategy.data_sources)    # existing
  -> strategy.screen(data)                         # existing
  -> registry.fetch("openbb", method="equity_profile", tickers=signal_tickers)  # NEW
  -> portfolio_committee.synthesize(signals, regime_context + openbb_enrichment) # ENRICHED
```

The orchestrator does a second OpenBB fetch after strategies produce signals, to enrich signal tickers with profile/estimates/short interest before passing to the portfolio committee.

**Portfolio committee enrichments:**

1. **Sector concentration enforcement** -- Use `equity_profile` sector data to enforce `_max_sector` limit (default 30%). Currently can't enforce this because there's no sector data.
2. **Short interest context** -- Pass short interest as signal metadata. Committee flags crowded shorts and adjusts sizing.
3. **Factor exposure awareness** -- Pass Fama-French factor returns in `regime_context`. LLM prompt gets factor regime context.
4. **Estimates context** -- Pass consensus EPS/revenue for signal tickers. Committee assesses whether signals align with or contradict Street expectations.

These changes are to data passed TO the committee, not its core logic. LLM prompt gets richer context; rule-based fallback gets sector data for concentration limits.

## File Changes

### New Files (2)

| File | Lines | Description |
|------|-------|-------------|
| `tradingagents/autoresearch/data_sources/openbb_source.py` | ~350 | OpenBBSource class with 8 methods, lazy init, cache |
| `tests/test_openbb_source.py` | ~250 | Unit tests for all methods + graceful degradation |

### Modified Files -- Infrastructure (3)

| File | Change |
|------|--------|
| `data_sources/registry.py` | Add `OpenBBSource` to `build_default_registry()` |
| `data_sources/__init__.py` | Add `OpenBBSource` to imports and `__all__` |
| `default_config.py` | Add `fmp_api_key` to autoresearch section |

### Modified Files -- Strategies (9)

| File | Change |
|------|--------|
| `strategies/earnings_call.py` | Add `"openbb"`, enrich with consensus EPS |
| `strategies/insider_activity.py` | Add `"openbb"`, enrich with titles + sector |
| `strategies/congressional_trades.py` | Add `"openbb"`, use govt trades API as primary |
| `strategies/supply_chain.py` | Add `"openbb"`, add short interest + sector |
| `strategies/litigation.py` | Add `"openbb"`, merge SEC litigation releases |
| `strategies/filing_analysis.py` | Add `"openbb"`, compare vs analyst consensus |
| `strategies/regulatory_pipeline.py` | Add `"openbb"`, sector validation |
| `_archive/govt_contracts.py` | Move to `strategies/`, rewrite screen, paper_trade track |
| `_archive/state_economics.py` | Move to `strategies/`, rewrite screen, paper_trade track |

### Modified Files -- Orchestration (2)

| File | Change |
|------|--------|
| `portfolio_committee.py` | Accept enrichment dict, sector limits, factor context in LLM prompt |
| `cohort_orchestrator.py` | Fetch OpenBB profiles/estimates/shorts for signal tickers, pass to committee |

### Modified Files -- Config & Registration (3)

| File | Change |
|------|--------|
| `.env.example` | Add `FMP_API_KEY=` |
| `strategies/__init__.py` | Register `govt_contracts` and `state_economics` in `get_paper_trade_strategies()` |
| `tests/test_multi_strategy.py` | Update for 9 strategies, fix count assertions |

### Modified Files -- Simulation (1)

| File | Change |
|------|--------|
| `tests/test_30day_simulation.py` | Add `TestOpenBBEnrichment`, `TestReactivatedStrategies`, extend cohort divergence test |

**Total: 2 new files, 18 modified files.**

No files deleted. `congress_source.py` stays as fallback. Archived strategy files are moved (git tracks rename).

## Implementation Phases

### Phase 1: Implement & Unit Test

All code changes on `feature/trading-extensions` branch. Unit tests pass before anything else:
- `test_openbb_source.py` -- all 8 methods mocked, cache, error handling
- `test_multi_strategy.py` -- updated for 9 strategies
- All existing tests still pass (`pytest tests/ -v`)

### Phase 2: 30-Day Simulation

Update `tests/test_30day_simulation.py`:

1. **`TestOpenBBEnrichment`** -- Mock OpenBBSource, run 30 days via CohortOrchestrator. Verify portfolio committee receives sector data and enforces concentration limits. Verify graceful degradation (30 days with OpenBB unavailable = identical to baseline).

2. **`TestReactivatedStrategies`** -- Test `govt_contracts` and `state_economics` produce valid signals from synthetic data. Exit logic works over 30-day lifecycle. Don't break when OpenBB missing.

3. **Extend `TestThirtyDayCohortDivergence`** -- Increase to 9 strategies. Verify cohort divergence still works.

All tests use mocked data -- no real API calls.

### Phase 3: Start New Generation

Once all tests pass, commit and start a new generation:

```bash
python scripts/run_generations.py start "9-strategy OpenBB enrichment: sector classification, analyst estimates, short interest, govt trades API, SEC litigation, Fama-French factors, reactivated govt_contracts + state_economics"
```

Creates detached worktree + isolated state directory. Runs alongside existing generation for comparison.

### Verification Criteria (all must pass before work is done)

1. All unit tests pass (`pytest tests/ -v` -- zero failures)
2. All 30-day simulation tests pass -- including OpenBB enrichment, reactivated strategies, cohort divergence
3. `dry_run.py` script completes without errors (full pipeline with real LLM calls)
4. New generation started and first `run-daily` succeeds
5. Both cohorts (control + adaptive) produce signals and trades on day 1
6. OpenBB graceful degradation confirmed -- system behaves identically to pre-OpenBB baseline when OpenBB unavailable
