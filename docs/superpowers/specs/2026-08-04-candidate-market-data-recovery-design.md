# Candidate Market-Data Recovery Design

## Purpose

Prevent one transient, malformed raw market-data bar for a new candidate from
discarding an otherwise valid paper-trading cycle, without ever weakening the
fail-closed boundary for positions, exits, fills, corporate actions, marks, or
benchmarks.

This is a behavior change. It is implemented on a topic branch and, after
review and merge, will start a new generation. `gen_004` and `gen_005` remain
frozen.

## Decision

Use a two-class reference-bar policy.

1. **Governed tickers** are every ticker needed to mark or govern the account:
   open lots, due exits, pending entries, outcomes, corporate actions, and the
   benchmark. Their validated execution bundle is authoritative. A missing,
   stale, adjusted, or incoherent bar remains a critical market-data gap and
   fails closed exactly as it does now.
2. **Candidate-only tickers** are new signal tickers not already represented
   by the validated execution bundle. They get an initial raw batch attempt.
   An invalid candidate bar receives exactly one cache-bypassing, single-ticker
   raw refetch. A valid refetch restores eligibility; a second failure removes
   only that ticker's candidate signals from every horizon for that session.

Candidate overlap with a governed ticker reuses the already validated execution
bar. It must not fetch again or become eligible for quarantine.

## Audit and State Semantics

The adapter will retain bounded, normalized evidence for each candidate attempt:
ticker, session, source, fetched time, raw OHLC values when available, and the
validation failure. It must never persist a full vendor frame.

Candidate recovery outcomes are immutable shared metrics records keyed by
epoch/session/ticker. They include the initial attempt, optional retry, outcome
(`recovered` or `quarantined`), and affected signal event identities/strategies.
They are not ledger ticker quarantines, which persist and would incorrectly
poison future executions.

The existing P0 marking and accounting snapshot may be valid before candidate
screening. A candidate-only failure therefore preserves that valid account
session and records `execution_valid=true, staging_valid=false` in the run
result/audit. It stages no signals or intents for the quarantined ticker, does
not invoke the broad critical-gap invalidation path, and does not close or
invalidate the metric epoch. The top-level run remains degraded/alertable.

Regime observations must not be presented as a promotable screened result until
candidate reference-bar resolution has completed. A candidate-data failure is a
visible data/availability failure, never a benign silent strategy.

## Interfaces

- `PriceSource` keeps its existing fully validated `get_daily_bars()` contract
  for governed execution paths.
- A new candidate-attempt interface returns per-ticker success or bounded
  validation evidence without discarding healthy bars from the same batch.
- A cache-bypassing single-ticker operation is explicit on the price-source
  interface; a different cache key alone is not considered a fresh refetch.
- Candidate resolution returns validated bars plus recovered/quarantined ticker
  outcomes. It does not raise a critical-gap error for a known candidate-only
  failure.

## Verification

Tests must prove:

1. An incoherent candidate batch bar is retried once, recovered, and remains
   stageable with both attempts recorded.
2. A candidate whose retry is still invalid is removed from all horizons while
   healthy candidates stage; the accounting session remains valid and the
   metric epoch remains active.
3. An invalid governed bar still fails closed with no candidate fallback.
4. A candidate that overlaps a governed ticker reuses the validated bundle and
   makes no extra fetch.
5. Retry evidence is deterministic, bounded, immutable, and idempotent across
   replay.

## Non-goals

- No historical rerun or modification of `gen_004`/`gen_005` state.
- No automatic secondary vendor or synthetic price substitution.
- No relaxation of raw-bar coherence, freshness, adjusted-price, corporate
  action, benchmark, or execution validation.
- No persistent ledger quarantine for a candidate-data incident.
