# Metric Epoch Runtime Wiring — Design Addendum

**Parent design:** `2026-07-31-metrics-governance-design.md`
**Scope:** Close the runtime wiring gap before MetricsService readers land.
**Status:** Approved and implemented within the P0-P3 program, including the
durable critical-gap recovery amendment below.

## Problem

The metrics package can persist and validate immutable semantic epochs, but the
daily production path does not create one. `GenerationManager` supplies the
frozen generation ID and commit to `run_cohorts.py`; that boundary currently
does not consume them. The orchestrator still uses the legacy P0 fallback
`foundation-v1`, so derived outcomes can be written while
`MetricStore.current_epoch()` remains empty. Reusing that fallback as a metric
epoch would conflate execution-ledger identity with semantic metric identity.

## Considered approaches

1. **Recommended: explicit generation identity plus a secret-free effective
   policy document.** Pass the generation ID and frozen commit through the CLI
   boundary, hash only behavior-affecting model selections and each cohort's
   effective execution policy, then create or reuse the metric epoch before the
   session lifecycle begins. This is deterministic, auditable, and sensitive
   to every approved epoch boundary.
2. **Hash the complete runtime configuration.** Rejected because the config can
   contain credentials, filesystem paths, and operational values that do not
   change metric semantics. It would create false epoch boundaries and risks
   allowing secrets into diagnostic material.
3. **Reuse `paper_ledger.epoch_id`.** Rejected because it neither records a
   `MetricEpoch` nor proves generation, model, pricing, cost, clock, and config
   compatibility.

## Architecture

Add one pure runtime-context builder at the orchestration boundary. It accepts:

- exact non-empty generation ID and frozen commit;
- an allowlisted model/behavior document;
- sorted cohort descriptors and the effective, secret-free policy document
  exposed by each `SessionExecutor`;
- centrally defined execution-clock, pricing, and cost-model contract versions.

The builder returns the existing `EpochContext`. Its behavior and configuration
hashes use canonical JSON and stable SHA-256 identities. It never hashes API
keys, tokens, state paths, timestamps, live borrow rates, positions, prices, or
other session-varying values.

`scripts/run_cohorts.py` must fail before constructing an orchestrator or state
database if `EVENTEDGE_GENERATION_ID` or `EVENTEDGE_GENERATION_COMMIT` is absent.
It passes both values explicitly to `CohortOrchestrator`; the orchestrator does
not read environment variables itself.

At the beginning of every valid XNYS daily run, after the cohort effective
policies exist and before any ledger lookup or write, the orchestrator calls
`EpochManager.ensure_epoch(context, session)`. The returned metric epoch ID is
the session's sole epoch ID for P0 snapshots, benchmarks, signals, fills, v2
signals, outcomes, and later health records. This preserves the P0 stable signal
identity contract while making `MetricStore.current_epoch()` authoritative.

An exact replay of a session that already invalidated the current epoch is a
read-only replay boundary: the manager returns that matching invalid epoch for
the same session instead of opening a replacement. The orchestrator must not
write new session records under it. Only a strictly later XNYS session can open
the replacement epoch. A different semantic context presented for the already
invalidated session is a conflict and fails closed.

### Durable critical-gap recovery boundary

P0 cohort ledgers and the shared metrics store are separate SQLite databases,
so a critical gap cannot depend on process-local ordering alone. The shared
`MetricStore` is the recovery coordinator. Before any dependent outcome, P0
invalidation, corporate-action rejection, or metric-epoch mutation, the
orchestrator durably inserts one `pending` `CriticalGapMarker`. A partial unique
index permits only one pending marker per generation store, and marker
completion is idempotent.

The marker is deliberately bounded and schema-validated. It contains the
stable marker/epoch IDs, one exact XNYS gap session, the stable
`critical_market_data_gap` reason, bounded cohort/ticker invalid-outcome
reasons, and—only when a corporate-action batch must be rejected—a bounded
canonical rejection intent per cohort. That intent includes only the exact
corporate-action fields already required by the P0 rejection audit, the
governed ticker names, and deterministic validation errors. It excludes market
bars, positions, credentials, filesystem paths, provider response bodies, and
arbitrary exception details, and it never participates in the semantic epoch
hash.

Recovery is an idempotent state machine:

1. Write any missing due invalid outcomes from persisted entry evidence only,
   preserving every existing immutable outcome.
2. Replay any pending corporate-action rejection/audit/quarantine write from
   the marker's canonical intent.
3. Preserve cohorts whose P0 snapshot and complete phase chain already
   committed; invalidate and cancel due work only for uncommitted cohorts.
4. Invalidate the marker's exact original metric epoch with the stable reason.
5. Mark the recovery complete only if both outcome recovery and every required
   P0 rejection audit succeeded.

An integrity error in completed, stage-only, partial-resume, or post-execution
persisted context/outcome repair enters this same boundary. Committed P0 rows
are not overwritten, the original error is surfaced after the safety closure,
and a newly failed recovery write leaves the marker pending for another retry.

At daily startup, after XNYS date/close validation but before epoch resolution
or ledger early returns, the orchestrator checks for a pending marker. A replay
of the gap session completes it and returns without any price/action/benchmark
fetch. A strictly later session first completes the same zero-fetch recovery,
then resolves a replacement epoch and performs only that later session's normal
fetches. A session earlier than the pending gap fails closed. Deterministic
crash hooks cover process death after marker persistence, after P0 invalidation,
and after metric invalidation.

## Canonical semantic inputs

The behavior document contains only:

- frozen generation commit;
- configured LLM provider and model names used by the generation;
- sorted active strategy names;
- each cohort's `use_llm` flag.

The context builder itself, not only its caller, enforces the exact model-key
allowlist: `llm_provider`, `deep_think_llm`, `quick_think_llm`, `cache_model`,
`live_model`, `strategist_model`, `cro_model`, and `autoresearch_model`. It also
enforces exact top-level and nested execution-policy schemas and canonical
scalar/string-list leaves. Unexpected or hidden state/secret keys are rejected
before hashing.

The configuration document contains a sorted entry per cohort:

- cohort name, horizon, size profile, policy ID, and learning-disabled flags;
- the executor's existing canonical paper-only execution policy: schema,
  XNYS provider/version, bar-age rule, benchmark symbols, risk gates, cost
  parameters, short-selling limit, and price/clock/cost contracts.

The executor exposes this existing document through a read-only public method;
the builder does not duplicate its rules. Cohort state directories and current
borrow inputs are excluded.

The centrally defined contract strings are the current P0 contracts:

- execution clock: `exact-next-xnys-open-v1`;
- pricing: `raw-unadjusted-daily-ohlc-v1`;
- cost model: `adverse-equity-fill-v1`.

Those constants are used both in the P0 execution policy document and the
metric `EpochContext`, preventing drift between two copies.

## Lifecycle and failures

- Identical context on a later session reuses the open epoch.
- A generation, commit, model, cohort policy, execution clock, pricing, cost,
  configuration, dependency-contract, or metric-schema change opens a fresh
  epoch through the existing boundary semantics.
- Missing generation metadata, an unsupported/noncanonical semantic value, or
  an epoch conflict fails closed before session mutation.
- On a critical market-data gap, the durable marker is written first. Every due
  outcome that can be derived from the persisted valid entry context is then
  written as an immutable invalid row; no current price is fabricated. Required
  P0 rejection audits and cohort invalidations follow, then the orchestrator
  invalidates the marker's exact shared metric epoch once, stops further
  metric/session staging, and completes the marker only after all required
  writes succeed. A later clean session recovers first and creates a new epoch.
  Task 7 reuses the same manager for unclassified strategy silence.
- Outcomes from a closed prior epoch are never attached to the new epoch.
- No legacy generation artifact is rewritten, and no VPS, generation, service,
  or timer operation is part of this change.

## Verification

Tests must prove:

- CLI generation metadata is required before state construction;
- identical inputs are stable across mapping order and state-directory changes;
- every allowlisted model and effective-policy field changes the proper hash;
- secrets, paths, timestamps, live borrow rates, positions, and prices do not;
- all cohorts share one `MetricStore` and one metric epoch ID;
- first run creates the epoch before ledger writes, replay reuses it, and a
  semantic change on a later XNYS session closes/opens at the exact boundary;
- a missing/stale due bar writes one invalid outcome, invalidates the shared
  epoch once, performs no further metric write, and an exact replay neither
  refetches data nor opens another epoch;
- crashes after marker persistence, P0 invalidation, and metric invalidation
  leave a pending boundary that a same-session replay completes without a
  fetch; direct later-session recovery completes before replacement creation;
- a failed corporate-action rejection audit leaves the marker pending, and its
  zero-fetch replay writes exactly one idempotent rejection row before marker
  completion;
- completed, stage-only, partial-resume, and outcome-repair corruption preserve
  committed P0, invalidate the original epoch, and surface the original error;
- P0 `SignalRecord.signal_id`, v2 `SignalMetricRecord.signal_id`, and outcomes
  use the returned metric epoch identity consistently;
- missing metadata and malformed context fail without creating state;
- focused P0/Task 1-4 regressions, full offline tests, Ruff, compileall, and
  `git diff --check` pass within the existing memory/API budgets.
