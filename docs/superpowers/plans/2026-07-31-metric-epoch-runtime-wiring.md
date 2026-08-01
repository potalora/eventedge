# Metric Epoch Runtime Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the daily cohort lifecycle create and enforce one registered,
secret-free semantic metric epoch, and persist invalid due outcomes when market
validation fails without fabricating a price.

**Architecture:** `run_cohorts.py` passes mandatory frozen generation identity
to `CohortOrchestrator`. A pure orchestration builder hashes allowlisted model
identity plus each cohort's existing canonical execution-policy document into
`EpochContext`; `SessionExecutor` owns the shared `EpochManager` call. The
returned metric epoch ID becomes the sole P0/v2 epoch for that session. Outcome
repair uses the existing shared response and persisted entry context even when
the valuation lifecycle is invalid. A bounded pending marker in the shared
metrics store coordinates crash recovery across that store and the independent
P0 cohort ledgers before any dependent mutation.

**Tech Stack:** Python 3.10+, dataclasses, canonical JSON/SHA-256 identities,
SQLite/WAL, exchange-calendars XNYS, pytest.

## Global Constraints

- Production remains paper-trading only.
- Automated learning remains disabled and cannot be enabled by configuration or environment variables.
- Existing generation history is never rewritten.
- Behavioral, execution-clock, pricing, cost, configuration, model, or metric-schema changes require a fresh metric epoch.
- P2/P3 metric code reads authoritative P0 ledger records, never `paper_trades.json` or `equity_snapshots.jsonl`.
- A missing or stale price invalidates the affected valuation session; no entry-price or nearest-price fallback is allowed.
- Unit tests are deterministic and API/LLM-free.
- Do not deploy, patch a live generation, create `gen_004`, merge, or change `trade.timer` while implementing this plan.
- Never hash, print, or persist credentials, API keys, tokens, filesystem state paths, timestamps, live borrow rates, prices, positions, or other session-varying state into the semantic context.

---

### Task 1: Authoritative Runtime Metric Epoch and Failed-Session Outcomes

**Files:**
- Create: `tradingagents/strategies/execution/contracts.py`
- Create: `tradingagents/strategies/orchestration/metric_epoch_context.py`
- Modify: `tradingagents/strategies/orchestration/session_executor.py`
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py`
- Modify: `tradingagents/strategies/metrics/models.py`
- Modify: `tradingagents/strategies/metrics/store.py`
- Modify: `tradingagents/strategies/metrics/epochs.py`
- Modify: `scripts/run_cohorts.py`
- Create: `tests/test_metric_epoch_runtime.py`
- Modify: `tests/test_outcome_metrics_v2.py`
- Modify: `tests/test_30day_simulation.py`
- Modify only direct constructor fixtures as required in:
  `tests/test_multi_strategy.py`, `tests/test_cohort_redesign.py`, and
  `tests/test_ledger_migration.py`

**Interfaces:**
- Produces constants `POLICY_DOCUMENT_VERSION`, `EXECUTION_CLOCK_VERSION`,
  `PRICING_VERSION`, and `COST_MODEL_VERSION`.
- Produces immutable `CohortSemanticPolicy` and
  `build_epoch_context(generation_id, generation_commit, models, strategies,
  cohort_policies) -> EpochContext`.
- Produces `SessionExecutor.semantic_policy_document() -> dict[str, object]`.
- Produces `SessionExecutor.ensure_metric_epoch(context, session) -> MetricEpoch`.
- Produces `SessionExecutor.invalidate_metric_epoch(session, reason) -> MetricEpoch`.
- Produces a bounded `CriticalGapMarker` and idempotent
  `MetricStore.begin_critical_gap`, `attach_critical_gap_details`,
  `pending_critical_gap`, and `complete_critical_gap` recovery operations.
- `CohortOrchestrator(..., generation_id: str, generation_commit: str, ...)`
  requires exact generation identity and uses the returned metric epoch ID for
  every P0 and v2 record in the session.
- Production outcome repair persists deterministic invalid records from exact
  available bars even when P0 market validation invalidates the session.

- [ ] **Step 1: Write failing contract/context tests**

Create `tests/test_metric_epoch_runtime.py` with deterministic fixtures. Cover
these exact behaviors before creating production code:

```python
def test_epoch_context_is_stable_across_order_and_state_paths() -> None:
    first = build_epoch_context(
        generation_id="gen_004",
        generation_commit="abc123",
        models={"llm_provider": "anthropic", "autoresearch_model": "sonnet"},
        strategies=("filing_analysis", "litigation"),
        cohort_policies=_policies(state_path="/one", reverse=False),
    )
    second = build_epoch_context(
        generation_id="gen_004",
        generation_commit="abc123",
        models={"autoresearch_model": "sonnet", "llm_provider": "anthropic"},
        strategies=("litigation", "filing_analysis"),
        cohort_policies=_policies(state_path="/two", reverse=True),
    )
    assert first == second


@pytest.mark.parametrize(
    "change",
    (
        "generation_commit",
        "model",
        "cohort_horizon",
        "policy_id",
        "risk_gate",
        "cost_parameter",
        "clock_version",
        "pricing_version",
        "cost_version",
    ),
)
def test_every_semantic_change_rotates_context_hash(change: str) -> None:
    assert _changed_context(change) != _baseline_context()


@pytest.mark.parametrize(
    "secret_key",
    ("fmp_api_key", "courtlistener_token", "noaa_cdo_token"),
)
def test_secrets_never_participate_in_context(secret_key: str) -> None:
    assert _context_with_secret(secret_key, "one") == _context_with_secret(
        secret_key, "two"
    )
```

Also assert empty generation ID/commit and non-string model values raise
`ValueError`, and assert no serialized/hash input contains secret values or a
cohort state path.

- [ ] **Step 2: Run the context tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_metric_epoch_runtime.py -v
```

Expected: collection fails because `execution.contracts` and
`orchestration.metric_epoch_context` do not exist.

- [ ] **Step 3: Centralize the exact execution contracts**

Create `tradingagents/strategies/execution/contracts.py`:

```python
POLICY_DOCUMENT_VERSION = "execution-policy-v2"
EXECUTION_CLOCK_VERSION = "exact-next-xnys-open-v1"
PRICING_VERSION = "raw-unadjusted-daily-ohlc-v1"
COST_MODEL_VERSION = "adverse-equity-fill-v1"
```

Replace the four matching literals in
`SessionExecutor._static_context_documents` with these constants. Do not alter
the resulting JSON document.

- [ ] **Step 4: Implement the pure, allowlisted context builder**

Create `tradingagents/strategies/orchestration/metric_epoch_context.py` with:

```python
from dataclasses import asdict, dataclass
from typing import Mapping

from tradingagents.strategies.execution.contracts import (
    COST_MODEL_VERSION,
    EXECUTION_CLOCK_VERSION,
    PRICING_VERSION,
)
from tradingagents.strategies.metrics.epochs import EpochContext
from tradingagents.strategies.execution.ids import stable_id


@dataclass(frozen=True)
class CohortSemanticPolicy:
    name: str
    horizon: str
    size_profile: str
    policy_id: str
    use_llm: bool
    learning_enabled: bool
    execution_policy: dict[str, object]


def build_epoch_context(
    *,
    generation_id: str,
    generation_commit: str,
    models: Mapping[str, str | None],
    strategies: tuple[str, ...],
    cohort_policies: tuple[CohortSemanticPolicy, ...],
) -> EpochContext:
    generation_id = _required_text("generation_id", generation_id)
    generation_commit = _required_text("generation_commit", generation_commit)
    model_document = {
        _required_text("model key", key): (
            _required_text(f"model {key}", value) if value is not None else None
        )
        for key, value in sorted(models.items())
    }
    policy_document = [
        asdict(policy)
        for policy in sorted(cohort_policies, key=lambda row: row.name)
    ]
    behavior_hash = stable_id(
        "metric_behavior",
        generation_commit,
        model_document,
        tuple(sorted(strategies)),
        tuple((row.name, row.use_llm) for row in sorted(
            cohort_policies, key=lambda item: item.name
        )),
    )
    config_hash = stable_id("metric_configuration", policy_document)
    return EpochContext(
        generation_id=generation_id,
        generation_commit=generation_commit,
        behavior_hash=behavior_hash,
        config_hash=config_hash,
        execution_clock_version=EXECUTION_CLOCK_VERSION,
        pricing_version=PRICING_VERSION,
        cost_model_version=COST_MODEL_VERSION,
    )
```

Implement `_required_text` to reject non-strings and empty/whitespace strings.
Before hashing, recursively validate `execution_policy` contains only `dict`
with string keys, `list`, `str`, `int`, `bool`, or `None`. Reject floats,
`Decimal`, dates, paths, sets, arbitrary objects, and non-finite values instead
of stringifying them. The executor policy is already canonical and should pass.
Do not accept a full application config in this module.

Enforce equality with the exact eight-key model set in this public builder, not
only at its caller; all eight keys are required even when their configured
value is explicitly `None`. Enforce equality with the exact top-level and
nested schemas for the executor policy; empty or partial documents fail closed;
allowed scalar or string-list leaves must not contain hidden mappings. Reject
API keys/tokens, state paths, timestamps, live borrow rates, prices, positions,
and every other unexpected key before hashing.

Apply `_required_text` to every strategy name before sorting. Reject duplicate
cohort names and duplicate strategy names so two semantically ambiguous input
documents cannot collapse to one hash. Use the public strict
`execution.ids.stable_id`; do not import the metrics package's private
`_stable_id`, whose permissive string conversion is not an epoch contract.

- [ ] **Step 5: Expose and test the existing secret-free effective policy**

Add:

```python
def semantic_policy_document(self) -> dict[str, object]:
    """Return canonical, secret-free, session-invariant execution semantics."""
    config_inputs, _ = self._static_context_documents((), {})
    return config_inputs
```

Test that changing every effective paper policy field changes the configuration
hash, while changing state paths, API keys, live borrow rates, and prices does
not. Keep the existing execution-context digest tests green to prove moving the
contract strings did not change P0 behavior.

- [ ] **Step 6: Write failing CLI and lifecycle integration tests**

Add tests proving:

```python
def test_run_cohorts_requires_generation_metadata_before_state_write(...):
    # Valid XNYS date, both generation env vars absent.
    # Expect SystemExit(2), exact missing-metadata message, and no state path.


def test_session_executor_registers_and_reuses_metric_epoch(...):
    first = executor.ensure_metric_epoch(context, date(2026, 8, 3))
    repeated = executor.ensure_metric_epoch(context, date(2026, 8, 4))
    assert first == repeated == store.current_epoch()


def test_later_semantic_change_closes_old_epoch_before_any_new_ledger_write(...):
    # Run one session, rebuild with changed allowlisted model/config on the next
    # XNYS session, and assert the old epoch closed at previous_session(new).


def test_p0_signal_and_outcome_use_registered_metric_epoch(...):
    # Run through the real orchestrator lifecycle.
    # Assert MetricStore.current_epoch exists and its ID equals ledger signal,
    # snapshot, benchmark, v2 signal, and outcome epoch IDs. Recompute signal_id
    # with metrics.identity.signal_id and assert exact equality.


def test_invalidated_session_replay_does_not_open_replacement_epoch(...):
    # Invalidate the registered epoch on one XNYS session. Re-resolve the exact
    # same context/session and assert the same invalid epoch is returned, no
    # row count changes, and a changed context for that date fails closed.
```

For direct `CohortOrchestrator` tests, pass explicit synthetic
`generation_id="gen_test"` and `generation_commit="test-commit"`. Do not add a
production fallback for missing metadata.

- [ ] **Step 7: Wire generation identity and epoch creation**

In `scripts/run_cohorts.py`, after date parsing but before importing project
state/orchestrator modules, require both environment values:

```python
generation_id = os.environ.get("EVENTEDGE_GENERATION_ID", "").strip()
generation_commit = os.environ.get("EVENTEDGE_GENERATION_COMMIT", "").strip()
if not generation_id or not generation_commit:
    parser.error(
        "EVENTEDGE_GENERATION_ID and EVENTEDGE_GENERATION_COMMIT are required"
    )
```

Pass them explicitly to `CohortOrchestrator`. Add required keyword-only
constructor parameters with no default. Build one shared `MetricStore`, one
`EpochContext`, and one `EpochManager` after all effective cohort policies are
available. The allowlisted model keys are exactly:

```python
(
    "llm_provider",
    "deep_think_llm",
    "quick_think_llm",
    "cache_model",
    "live_model",
    "strategist_model",
    "cro_model",
    "autoresearch_model",
)
```

The last five values come from `base_config["autoresearch"]`; the first three
come from the root config. Active strategy names come from the instantiated
strategy modules. Each `CohortSemanticPolicy` uses the effective cohort config,
including its resolved policy ID, horizon, size profile, forced
`learning_enabled=False`, and executor policy document.

Add `SessionExecutor.ensure_metric_epoch`:

```python
def ensure_metric_epoch(
    self, context: EpochContext, session: date
) -> MetricEpoch:
    epoch = EpochManager(self.metric_store).ensure_epoch(context, session)
    if epoch.status == "invalid" and epoch.end_session == session:
        return epoch
    if epoch.status != "open" or epoch.end_session is not None:
        raise RuntimeError("metric epoch is not open")
    return epoch
```

Extend `EpochManager.ensure_epoch` with one exact-replay rule before creating a
new epoch: when the current epoch is `invalid`, its `end_session` equals the
requested session, and its semantic context matches, return it unchanged. A
semantic mismatch for that same invalidated session raises `ValueError`; a
strictly later session may create the replacement. `SessionExecutor` accepts
the invalid return only for this read-only replay case and the orchestrator
must exit through existing invalid-ledger results before any new write.

Add `SessionExecutor.invalidate_metric_epoch` as the sole orchestration-facing
wrapper around `EpochManager.invalidate_current`. It uses the stable reason
`critical_market_data_gap`; an identical repeated call for the same session is
idempotent, while any conflict fails closed.

At the start of `run_daily`, after validating the XNYS date/close but before
ledger reads, call this method on one executor sharing the generation store.
Use the returned ID as `self._epoch_id` and the local session epoch everywhere
the P0 fallback was previously used. Delete the `foundation-v1` fallback.

- [ ] **Step 8: Write failing missing/stale due-outcome lifecycle tests**

Extend the real lifecycle fixture so a due signal has a valid persisted entry
bar but the shared exit response is (a) missing and (b) stale. Assert:

```python
assert result[cohort_name]["error"] is True
assert ledger.session_invalid_reason(exit_session)
rows = metric_store.read_outcomes(metric_epoch_id)
assert len(rows) == 1
assert rows[0].status == "invalid"
assert rows[0].exit_price is None
assert rows[0].signed_return is None
assert rows[0].invalid_reason in {"missing_exit_bar", "stale_exit_bar"}
```

Replay the same invalid session and assert the immutable outcome count remains
one with no market-data refetch and no replacement metric epoch.

- [ ] **Step 9: Persist due outcomes on failed market validation**

Add a bounded `SessionExecutor.validated_outcome_bars(...)` helper that applies
the same exact raw-bar identity, OHLC, timezone, freshness, and close-safe rules
to only the due outcome tickers. It returns `(valid_bars, invalid_reasons)`;
missing and stale bars are never returned as usable bars. Extend
`record_due_outcomes` with an optional mapping of exact current-ticker invalid
reasons. For a forced invalid reason, persist `entry_price` only when its
historical context is valid, set `exit_price`, `raw_return`, and
`signed_return` to `None`, and set `status="invalid"` without a fallback price.

Call the writer for due signals after a failed shared fetch/bundle validation
or invalid `SessionExecutionResult`, using the available shared response or an
empty mapping. Keep the P0 session invalid and cohort result error unchanged.
Identical replay must call immutable upsert; unequal payload must fail closed.
Do not make a second price-source call.

Treat candidate execution-reference validation as the same critical gap after
screening but before staging. Route the original exception through the durable
stop, preserve committed P0, and pass only bars from the already fetched and
validated shared execution bundle when one exists; otherwise pass an empty
mapping. Never refetch candidate bars during recovery, and surface the original
reference error after exact-epoch closure.

After all affected due outcomes have been attempted, invalidate the shared
metric epoch exactly once with `critical_market_data_gap`, stop screening and
staging for that daily run, and return the cohort errors. Persist invalid
outcomes before invalidating the epoch. If any immutable outcome write fails,
fail closed and do not conceal that error behind the epoch invalidation. The
same-session replay must resolve the already-invalid epoch without opening a
replacement, observe the existing ledger invalidation, and perform neither a
fetch nor a new metric write. Add focused tests for the write-before-invalidate
order, one shared invalidation across multiple cohorts, and a clean next XNYS
session opening a new epoch.

- [x] **Step 9a: Amend critical-gap handling into a durable state machine**

Add a migration-safe `critical_gap_markers` table to the shared generation
`MetricStore`. A partial unique index allows one pending marker. Use a two-phase
write: first persist a minimal blocker before provider-derived detail and before
any outcome, cohort-ledger invalidation, corporate-action rejection, or
metric-epoch invalidation; then atomically attach validated recovery detail.
The minimal blocker contains the marker/epoch IDs, exact XNYS session, stable
general reason, and every affected cohort mapped exactly once to an opaque
ledger binding. Derive the binding from cohort identity plus normalized ledger
path and filesystem identity, but persist only the stable hash. Bound and
validate cohort/ticker counts, reasons, audit fields/items, and total serialized
size.

For a corporate-action batch rejection, persist a bounded canonical audit
intent in the marker: exact action fields, governed ticker names, and
deterministic validation errors only. Do not persist market bars, positions,
credentials, state paths, provider response bodies, or arbitrary exception
payloads. Reconstruct the P0 rejection from this intent so a process restart
can retry the audit/quarantine write without fetching.

Guard all phase-1 preparation and persistence, including affected-name
collection, ledger-binding derivation, marker construction/validation, and the
insert. On failure, directly invalidate the explicit epoch/session without
requiring a marker or binding. Cover the
post-blocker hook, detail construction, validation, and phase-2 attachment with
the same exact-epoch invalidation boundary. Leave the minimal blocker pending
when phase 2 fails. Do not auto-recover a minimal-only blocker: it does not
contain enough evidence to distinguish an unnecessary audit from an interrupted
one, so same/later runs must require manual resolution.

Complete a pending marker in this order:

1. Write missing due invalid outcomes from persisted entry evidence only.
2. Replay required corporate-action rejection/audit writes.
3. Preserve complete P0 phase chains/snapshots and invalidate only uncommitted
   cohort sessions. If another runner committed after marker attachment, write
   the required audit/quarantine idempotently without invalidating or replacing
   that committed snapshot/phase chain.
4. Invalidate the marker's exact original metric epoch.
5. Complete the marker only when outcome and audit writes both succeeded.

At `run_daily` startup, after XNYS date/close validation and before epoch
resolution or every ledger early return, complete a pending marker. The exact
gap-session replay returns its recovered errors with zero raw/action/benchmark
fetches. A direct later-session run completes the same recovery first, opens a
replacement epoch, and fetches only the later session. Reject sessions earlier
than the pending gap.

Before every recovery write, require all originally affected cohort names to
resolve exactly once to their original opaque ledger bindings. Removed,
renamed, duplicated, changed-path, missing-audit-cohort, and legacy-unbound
markers remain pending and fail before fetch. Permit extra cohorts only when all
original bindings resolve: exclude extras from old-gap writes and let them
enter normal execution only after recovery completes on a later session.
Retain the shared `MetricStore` directly on the orchestrator and inspect it
before the empty-cohort return. With a pending ready or minimal marker and no
cohorts, invalidate the marker's exact epoch directly if needed, leave it
pending, and raise the missing-binding error without fetching. Preserve `{}`
only for an empty topology with no pending marker.

Route completed, stage-only, stored partial-resume, bound-context validation,
and post-execution outcome-repair corruption through this boundary. Preserve
committed P0 and existing immutable outcomes, surface the original integrity
error after safety closure, and leave the marker pending if a new recovery
write fails.

For invalid-outcome recovery, distinguish a missing entry execution context
from a present but malformed/conflicting one. Only true absence may produce
`missing_entry_bar`. Propagate parsing or integrity failure from a present
context so the ready marker stays pending, exact epoch closes, committed P0 is
preserved, and same/later zero-fetch recovery remains blocked until exact
evidence repair.

Add deterministic crash hooks after minimal blocker persistence, ready-marker
attachment, P0 invalidation, and metric invalidation. Tests must cover every
crash boundary, unrepresentable provider fields/errors plus item/byte overflow,
exact affected-ledger topology and legacy failure, safe added-cohort behavior,
binding-derivation failure, all-removed ready/minimal recovery, interleaved
committed audit replay, direct later-session recovery, and audit-write failure
followed by zero-fetch replay with exactly one rejection row. Also cover
candidate reference failure without refetch/staging, malformed present entry
evidence remaining pending until repair, malformed completed/stage/partial contexts,
immutable conflicts, committed-P0 preservation, and marker migration/bounds.

- [ ] **Step 10: Run focused, full, and static verification**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_metric_epoch_runtime.py \
  tests/test_outcome_metrics_v2.py \
  tests/test_30day_simulation.py \
  tests/test_session_executor.py \
  tests/test_metric_epochs.py \
  tests/test_generation_manager.py \
  tests/test_ledger_migration.py -v

/usr/bin/time -l .venv/bin/python -m pytest tests/ -m "not live" -q

.venv/bin/ruff check \
  tradingagents/strategies/execution/contracts.py \
  tradingagents/strategies/metrics/epochs.py \
  tradingagents/strategies/orchestration/metric_epoch_context.py \
  tradingagents/strategies/orchestration/session_executor.py \
  tradingagents/strategies/orchestration/cohort_orchestrator.py \
  scripts/run_cohorts.py tests/test_metric_epoch_runtime.py \
  tests/test_outcome_metrics_v2.py tests/test_30day_simulation.py

.venv/bin/python -m compileall -q tradingagents scripts/run_cohorts.py
git diff --check
```

Expected: all offline tests pass; no external API/LLM call; peak RSS remains
well below 8 GB; no warning is newly introduced by these files.

- [ ] **Step 11: Commit**

```bash
git add docs/superpowers/specs/2026-07-31-metric-epoch-runtime-wiring-design.md \
  docs/superpowers/plans/2026-07-31-metric-epoch-runtime-wiring.md \
  tradingagents/strategies/execution/contracts.py \
  tradingagents/strategies/metrics/epochs.py \
  tradingagents/strategies/orchestration/metric_epoch_context.py \
  tradingagents/strategies/orchestration/session_executor.py \
  tradingagents/strategies/orchestration/cohort_orchestrator.py \
  scripts/run_cohorts.py tests/test_metric_epoch_runtime.py \
  tests/test_outcome_metrics_v2.py tests/test_30day_simulation.py \
  tests/test_multi_strategy.py tests/test_cohort_redesign.py \
  tests/test_ledger_migration.py
git commit -m "fix(metrics): wire authoritative runtime epochs"
```

---
