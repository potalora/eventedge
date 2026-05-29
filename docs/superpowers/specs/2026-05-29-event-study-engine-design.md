# Event Study Engine — Design

**Date:** 2026-05-29
**Status:** Approved (design)
**Author:** EventEdge (with Claude)

## Motivation

EventEdge runs 12 event-driven strategies (earnings, insider Form 4, 8-K/10-Q
filings, litigation, govt contracts, congressional trades, etc.) but has **no
statistical validation** that the event *types* we trade actually predict
abnormal returns. `SignalJournal` logs raw forward returns (`return_5d`,
`return_10d`, `return_30d`) per signal, but does no market-adjustment and no
significance testing.

This was surfaced by scanning our inspiration upstream
`virattt/ai-hedge-fund`, whose in-progress `v2/` rewrite added a rigorous
**event study engine** (Cumulative Abnormal Returns via the market model,
t-test, bootstrap CI). Their engine is welded to their FD API client and an
earnings-specific data model, so we adopt the *math and structure*, not the I/O.

(Note: the other upstream item considered — refactoring trade records to a
generic `metadata: dict` — is **already implemented** in EventEdge:
`Candidate`, `BacktestTrade`, and `JournalEntry` all use `metadata`. No work
needed.)

## Goal

Measure, per strategy, whether the events we act on produce statistically
significant market-adjusted drift, and over what horizon — directly informing
`hold_days` and whether a strategy earns its place. Offline, read-only,
runnable on data we already have.

## Non-Goals

- No coupling into the live trading path (committee, orchestrator, journal
  unchanged).
- No automated strategy retirement based on results (possible future bolt-on).
- No per-event-type historical backfill sourcing in this build (the engine
  supports it via a future adapter; only the journal adapter ships now).

## Architecture

New package `tradingagents/strategies/validation/` (beside `learning/`, since
this is offline analysis, not live trading).

| File | Responsibility | Depends on |
|------|---------------|-----------|
| `stats.py` | Pure functions: `fit_market_model` (OLS α/β/R² via `np.linalg.lstsq`), `compute_abnormal_returns`, `sum_car`, `ttest_cars` (scipy `ttest_1samp`), `bootstrap_ci` (percentile, `rng_seed` param). No I/O, no state. | numpy, scipy |
| `models.py` | Dataclasses: `EventSpec` (input), `MarketModelFit`, `EventCAR`, `WindowStats`, `AggregateResult`, `EventStudyResult`. | — |
| `engine.py` | `compute_car(events, price_fn, *, windows, estimation, n_bootstrap, rng_seed)` — orchestration. | stats, models |
| `price_adapter.py` | `yfinance_price_fn()` → callable `(ticker, start, end) -> {date: close}` backed by `YFinanceSource.fetch_prices`. | yfinance_source |
| `journal_source.py` | `events_from_journal(journal, ...) -> list[EventSpec]` — reads `JournalEntry` rows, groups by `strategy`. | signal_journal |

**Key boundary:** the engine never imports a data source. It takes a `price_fn`
callable. This is what makes the "shared core, multiple event sources" design
clean — the journal adapter ships now; a backfill adapter later just builds a
different `EventSpec` list and passes the same `price_fn`.

### Input model

```python
@dataclass
class EventSpec:
    ticker: str
    event_date: str        # YYYY-MM-DD, anchors day 0
    group: str             # aggregation key (e.g. strategy name)
    metadata: dict = field(default_factory=dict)  # carried through to EventCAR
```

## Data Flow

```
events: list[EventSpec]          price_fn (yfinance-backed, cached)
        │                                  │
        ▼                                  ▼
compute_car(events, price_fn, windows=[(0,5),(0,10),(0,30)],
            estimation=(-250,-11), n_bootstrap=10_000, rng_seed=None)
   │
   ├─ 1. Fetch market proxy (SPY) closes ONCE, wide range → returns series
   ├─ 2. Group events by ticker; fetch each ticker's closes ONCE (price_fn cache dedupes)
   ├─ 3. Align ticker∩SPY trading days → daily simple returns
   ├─ 4. Per event:
   │       • slice estimation window [-250,-11]; skip if < min_estimation_days (200) → fit_market_model → α,β,R²
   │       • slice event window [0,+max] → compute_abnormal_returns: AR_t = R_t − (α + β·R_mkt,t)
   │       • sum_car over each window → cars dict keyed by window label ("[0,+5]", ...)
   │       → EventCAR (carries group + metadata; null CAR for any window past available data)
   └─ 5. Aggregate by group → per window: mean CAR, std, t-stat, p-value, bootstrap 95% CI
          → AggregateResult per group → EventStudyResult
```

### Decisions

- **Returns:** simple daily `close[t]/close[t-1] − 1` on raw yfinance closes,
  consistently for stock and SPY.
- **Market proxy:** SPY (US-only universe; non-US benchmark mapping is out of
  scope — see TradingAgents upstream `78d063d`, deferred).
- **Event anchoring:** `event_date` is day 0. If not a trading day, snap forward
  to the next trading day (safer than upstream's raw filing-date use, because
  our journal dates are scan dates).
- **Insufficient pre-history:** event with `< min_estimation_days` skipped; its
  ticker recorded in `skipped_tickers`.
- **Window past today:** windows that can't be fully filled return `None` for
  that event (still counted in windows that can).
- **Estimation window:** `[-250, -11]` (10-day buffer avoids event
  contamination), `min_estimation_days = 200`.
- **Event windows:** `[(0,5), (0,10), (0,30)]` — mirror `SignalJournal`'s
  `return_5d/10d/30d` and our 20–30 day strategy horizon. This is the fixed
  default; `windows` remains a `compute_car` parameter (so it isn't a hardcoded
  constant), but is **not** exposed on the CLI. `EventCAR.cars` is a
  `dict[str, float | None]` keyed by window label ("[0,+5]", "[0,+10]",
  "[0,+30]") so it generalizes to whatever windows are passed.

### Cost / memory

SPY fetched once; each ticker fetched once via the existing `fetch_prices`
cache. Bootstrap (10k resamples) runs on the small per-group CAR vector, not
price data — negligible RSS. Fits the 16GB M4 constraint.

## Entry Point & Output

CLI subcommand on `scripts/run_generations.py` (consistent with `compare`,
`run-learning`):

```bash
python scripts/run_generations.py event-study [--gen gen_005] [--strategy earnings_call] \
    [--since 2026-03-31] [--json out.json]
```

Loads the generation's `SignalJournal`, builds `EventSpec`s via
`journal_source`, runs `compute_car`, prints a per-strategy table, optional JSON
dump. Read-only — no state mutation, no trades.

Example output (one block per group):

```
earnings_call   (n=42 events)
  window    mean_CAR   t      p       95% CI
  [0,+5]    +1.83%    2.41   0.020   [+0.31%, +3.28%]
  [0,+10]   +2.10%    1.98   0.054   [-0.04%, +4.22%]
  [0,+30]   +0.92%    0.61   0.544   [-2.10%, +3.95%]
```

## Error Handling

- yfinance fetch failure for a ticker → skip + record in `skipped_tickers`;
  never crash the run.
- Empty event list → empty `EventStudyResult`.
- Market proxy fetch fails → result with all tickers skipped + logged warning
  (mirrors upstream).

## Testing

`tests/test_event_study.py`, all offline, no real APIs:

- **`stats.py`** — synthetic data: known α/β recovery from constructed series;
  `sum_car` index math; `ttest_cars` on a fixed array; `bootstrap_ci`
  determinism via `rng_seed`.
- **`engine.py`** — fake in-memory `price_fn` (no network): grouping, window
  slicing, skip logic, null-window handling, snap-forward.
- **`journal_source.py`** — temp `SignalJournal` with seeded entries.

## Dependencies

Add `scipy>=1.11` to `pyproject.toml`.

## Docs

- `CLAUDE.md`: document new `validation/` module; fix strategy count
  (11 → 12, add `quantum_readiness`); note core pipeline and `backtesting/`
  modules are pruned.
- Add `tests/test_event_study.py` to the key-test-files list.

## Out of Scope / Future

- Backfill event sourcing per event type (engine supports it via a new adapter).
- Non-US benchmark mapping.
- Learning loop reading `EventStudyResult` to flag/retire dead strategies.
