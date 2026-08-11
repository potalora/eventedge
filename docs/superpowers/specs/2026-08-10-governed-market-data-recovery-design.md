# Governed Market-Data Recovery Design

## Purpose

Keep a single corrupt Yahoo daily bar from discarding an otherwise valid paper-
trading session when complete same-provider intraday evidence can reconstruct the
bar without guessing. The execution ledger remains fail-closed: an incomplete,
ambiguous, stale, or contradictory repair still invalidates the session before
any fill, mark, or new intent is committed.

This is a P0 behavior change. It will ship only through a reviewed topic branch
and a fresh immutable generation. `gen_008` and its failed 2026-08-10 epoch stay
unchanged; that session will not be replayed.

## Incident Evidence

On 2026-08-10, `gen_008` rejected the required raw Yahoo bar for `ESS`:

```text
Open  = 286.2099914550781
High  = 285.82501220703125
Low   = 281.5299987792969
Close = 283.2099914550781
```

The open exceeded the high. The same invalid values appeared in the original
batch, a cache-bypassing single-ticker fetch, and `yfinance repair=True`. Yahoo's
seven regular-session hourly bars were complete and aggregated to:

```text
Open  = 286.2099914550781
High  = 286.2099914550781
Low   = 281.5299987792969
Close = 283.2099914550781
```

Alpaca was available only through the limited IEX feed, and FMP historical data
was outside the configured subscription. Neither was authoritative enough to
replace a consolidated P0 bar. EventEdge therefore did the right thing on
2026-08-10: it cancelled pending entries, committed no fills, and invalidated the
shared epoch.

## Decision

Add one narrow recovery path for an otherwise usable Yahoo raw daily bar whose
only defect is OHLC envelope incoherence. Reconstruct that bar from complete raw
60-minute Yahoo regular-session bars, bind the evidence to the metric epoch, and
mark the run degraded.

This is not a general fallback. Missing daily rows, non-finite or non-positive
values, adjusted bars, missing provenance, stale or pre-close fetches, mismatched
tickers or sessions, and incomplete intraday coverage still fail closed.

## Recovery Contract

The raw-bar portion of `SessionExecutor.fetch_input_bundle()` will use a new
governed resolver for the shared P0 ticker set. The resolver returns either a
fully validated bundle or bounded per-ticker failure evidence. Healthy tickers
from the initial batch remain usable while each incoherent ticker is evaluated
independently. Candidate-only `resolve_candidate_daily_bars()` retry and
quarantine behavior does not change.

For an incoherent ticker, the resolver will:

1. Fetch that ticker alone with `interval="60m"`, `auto_adjust=False`,
   `actions=False`, `prepost=False`, `repair=False`, `threads=False`, a bounded
   timeout, and no cache reuse.
2. Normalize timezone-aware Yahoo timestamps to `America/New_York`. Derive the
   expected starts as `open + n * 60 minutes` while the start is earlier than
   the exact XNYS close. A normal session therefore requires 09:30 through
   15:30; a 13:00 early close requires 09:30 through 12:30, with the final bin
   shorter than 60 minutes. The observed regular-session starts must equal the
   expected set exactly. Missing, duplicate, shifted, premarket, or after-hours
   rows are rejected rather than silently filtered.
3. Require every interval row to have positive, finite, coherent OHLC values and
   an unambiguous ticker/session identity.
4. Aggregate the first open, maximum high, minimum low, and final close.
5. Require exact decimal agreement for the daily open and close. If the daily
   high is the broken bound, the daily low must also match the aggregate exactly;
   if the daily low is broken, the daily high must match exactly. A bar with both
   extremes in conflict is not recoverable.
6. Validate the reconstructed bar again through the normal raw P0 validator,
   including the post-close and freshness checks.

The accepted bar uses source `yfinance-60m-reconstruction`; it is never labelled
as an original Yahoo daily observation. No value is clamped, widened, rounded,
or borrowed from another feed.

## Immutable Evidence and Replay

Add a separate `GovernedBarRecoveryRecord` and metric-store table with a unique
index on metric epoch, session, and ticker. It is not an extension of the
candidate recovery record. It records:

- the stable recovery ID and contract version;
- the original daily OHLC, source, fetch time, and validation error;
- the expected and observed intraday interval starts;
- each intraday OHLC row and fetch time, capped by the session calendar;
- the reconstructed bar and final validation result; and
- the affected governed cohort IDs.

The resolver first loads an existing record. An accepted record under the
current contract reconstructs the exact `MarketBar` without another provider
call. If no record exists, the resolver fetches, validates, and saves the record
immutably before binding any cohort context. Unequal evidence fails closed.
Full vendor frames and credentials are never persisted.

The stable recovery ID, contract version, and canonical evidence digest are
carried in `SessionInputBundle`, included in every affected cohort's P0 market
inputs and provenance documents, and therefore bound into the existing context
digests. Resume must look up the record and verify its ID, version, digest, and
reconstructed bar before accepting a persisted phase. A deleted or changed
record, an unsupported contract version, or a digest mismatch fails closed even
when the reconstructed OHLC values happen to be unchanged.

All cohorts that require the ticker receive the same reconstructed `MarketBar`.
The existing exact-bar equality and context-binding checks remain in force.

## Run Status and Failure Reporting

A successful governed repair produces a completed but alertable degraded run:

- `success=false` at the generation-manager and process-exit boundary, matching
  the existing degraded-run policy;
- `degraded=true`;
- `execution_valid=true`;
- the normal staging result; and
- a bounded `governed_bar_recoveries` list in the generation result and run
  history.

If recovery is unavailable or invalid, the existing P0 critical-gap path runs
unchanged. The shared resolver produces a bounded
`governed_failure_map: dict[ticker, normalized_reason]` across required
execution, outcome, and benchmark tickers before the critical-gap transition.
Reasons use a fixed grammar such as `missing TICKER/SESSION`, `incoherent
TICKER/SESSION`, or `invalid_benchmark TICKER/SESSION`; raw exception text is
not persisted. The critical-gap marker and top-level generation error retain
this map even when no outcome signals are due. This closes the observability gap
that reduced the 2026-08-10 alert to the generic
`critical_market_data_gap` reason.

## State-Aware Preflight

Keep the current isolated fetch, screen, and event-identity checks. Add a
separate read-only P0 probe. `run_cohorts.py --preflight` receives the real
`AUTORESEARCH_STATE_DIR` for this probe while the strategy-screen engine keeps
its temporary state directory. The probe opens the active generation's existing
metric store and cohort ledgers through SQLite `mode=ro`/`query_only`, without
migrations or writes, resolves the exact pending-entry, open-lot, due-outcome,
and benchmark ticker set for the requested session, and runs the same governed
resolver in non-persisting probe mode.

Preflight and daily execution use one repo-level advisory runtime lock: preflight
acquires a shared non-blocking lock and daily execution acquires an exclusive
lock. The lock is ephemeral operational state, not generation state. Preflight
fails if daily execution is active or if any database identity/data version
changes across the probe. All read-only connections are closed before return.

For a new generation, preflight must prove that the state directory has no
metric store and no cohort ledger. It then reports `state_status=uninitialized`,
treats open, pending, and due-outcome sets as empty, and probes only configured
benchmarks. Partial initialization or one missing database in an otherwise
initialized generation fails closed.

The midday preflight runs before the XNYS close, so its governed bar probe
reports `governed_probe_status=not_ready` without claiming P0 coverage; the
existing integration checks still run. The embedded after-close preflight in
`daily_trading.sh` must complete its governed probe before daily execution. An
unrecoverable P0 probe failure blocks that daily invocation before ledger state
is touched, while the existing isolated screen-preflight result remains
non-gating.

The probe reports:

- the governed ticker set;
- successful proposed recoveries with bounded evidence summaries; and
- exact per-ticker failures.

It does not persist a recovery, cancel an intent, open a writable SQLite
connection, call an LLM, stage a candidate, or execute a trade. If the state
cannot be opened read-only or the required ticker set cannot be proven, preflight
fails rather than falling back to the current stateless claim.

## Verification

Tests must prove:

1. The exact `ESS` daily/hourly fixture is rejected by the normal daily
   validator, then recovered by the governed resolver with the expected high and
   explicit reconstructed source.
2. Missing, duplicated, shifted, extra-session, stale, adjusted, non-finite, or
   incoherent hourly rows fail closed.
3. A mismatch in daily open, close, or the unaffected extreme fails closed.
4. Missing daily rows and all non-coherence validation failures never enter the
   reconstruction path.
5. One bad ticker does not refetch or alter healthy governed bars.
6. Recovery evidence is bounded, immutable, idempotent, and reused after a
   simulated crash without another provider call. Deletion, payload tampering,
   evidence-digest mismatch, and contract-version mismatch all fail resume even
   when the final OHLC is unchanged.
7. Every cohort receives the exact same reconstructed bar before any ledger
   mutation, execution remains valid, and the generation reports the run as
   degraded/non-clean rather than successful.
8. A failed recovery still commits no fill, mark, or staged intent and records
   the exact ticker/reason in the critical-gap marker and generation result.
9. Preflight reads the real required-ticker set without changing any generation
   file or SQLite database and reports the same proposed recovery or failure as
   the daily path. Uninitialized, partially initialized, pre-close, and
   concurrent-daily cases follow their explicit contracts.
10. Candidate-only retry/quarantine behavior and all existing P0 validation
    tests remain unchanged.

The focused recovery, lifecycle, metric-store, preflight, failure-reporting, and
generation-manager suites run first. The full test suite and Ruff must pass
before publication or deployment.

## Rollout

After review and merge:

1. Verify Hermes scheduler state, remote commit parity, and `gen_008` state.
2. Create and verify a rollback archive.
3. Fast-forward Hermes `main` only to the reviewed merge commit.
4. Start the next allocated generation after verifying the manifest (expected
   `gen_009` if `gen_008` remains the highest ID); do not modify or resume
   `gen_008`.
5. Run read-only preflight for the next XNYS session. Do not trigger a historical
   2026-08-10 run.
6. Leave the normal timer in control of the first daily execution unless Pedro
   explicitly authorizes a same-session manual trigger.

## Non-goals

- No 2026-08-10 replay or repair of `gen_008` state.
- No relaxation of raw-bar, corporate-action, benchmark, clock, or ledger
  validation.
- No paid SIP subscription or new external provider in this change.
- No Alpaca IEX or FMP value substitution.
- No interpolation, forward-fill, daily-value clamping, or cross-source price
  synthesis; the documented complete-session 60-minute aggregation is the only
  derived price allowed by this contract.
- No strategy, portfolio-policy, sizing, or promotion change.
