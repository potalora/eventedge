# Orchestration Simplification Implementation Plan

> **Goal:** Preserve the merged PR 2 behavior while replacing the 1,501-line
> `CohortOrchestrator.run_daily()` with a small coordinator, centralizing daily
> result interpretation, and deleting enough repeated production code to make
> the failure surface materially easier to reason about.

**Base commit:** `18dd856057a0a334a61272f96f867339c1528c2e`

**Baseline production surface:**

- `cohort_orchestrator.py`: 3,306 lines
- `generation_manager.py`: 1,194 lines
- `scripts/run_cohorts.py`: 466 lines
- `daily_pipeline.py`: 0 lines
- total: 4,966 lines
- `CohortOrchestrator.run_daily()`: 1,501 lines

**Acceptance targets:**

- the same four-file production surface is at most 4,221 lines, a real 15%
  reduction that counts the new module and does not count test growth;
- `CohortOrchestrator.run_daily()` is 150-250 lines;
- the incident corpus, ledger/metric invariants, exact worker wire, manifest
  history, and the full non-live suite remain unchanged;
- no new framework, stage hierarchy, registry, event bus, or dependency-
  injection layer is introduced.

## Measured outcome and safety decision

The 15% whole-surface target was not achievable as a behavior-preserving
change. The first readable phase extraction reduced `run_daily()` from 1,501
lines to 122 lines but required explicit state and phase functions for replay,
critical-gap, candidate-evidence, and exception-ordering contracts. It left the
four-file surface 14 lines above the 4,966-line baseline. Removing the remaining
759 lines needed to hit 4,221 would have required deleting strict validators or
moving them outside the measured surface, neither of which is an acceptable
reliability refactor.

The accepted safety-bounded result merges finalization state, removes repeated
phase dispatch, and reaches:

- 4,930 physical production lines, 36 fewer than the baseline;
- 4,603 nonblank production lines;
- a 91-line `CohortOrchestrator.run_daily()` coordinator;
- one explicit ordered phase list and one per-run state object;
- no removal of governed, replay, ledger, metric, issue-integrity, or untrusted-
  wire validation.

The original 15% target remains recorded above as a missed target rather than
being retroactively redefined. PR review and release notes must state this
tradeoff plainly.

---

## Task 1: Freeze the merged behavior before extraction

**Files:**

- Add: `tests/test_daily_pipeline_characterization.py`
- Modify only if a missing invariant is found: existing incident tests

### Steps

1. Add table-driven characterization for the canonical outer result summary:
   clean, candidate reference degradation, candidate volatility degradation,
   candidate issue plus real failure, governed recovery, and governed failure.
2. Assert exact outcome, failed/degraded cohorts, quarantined tickers, governed
   summaries/failures, candidate issue summaries, and operator message inputs.
3. Treat the existing lifecycle and volatility suites as the durable incident
   corpus. They already assert no fabricated fills/marks/intents/outcomes,
   governed overlap, no-I/O replay, tamper failure, partial resume, invalid
   epochs, and pending critical-gap finalization.
4. Run the corpus at the base commit and record the exact result as the old
   side of the old-versus-new comparison.

```bash
PYTHONPATH=. .venv/bin/pytest -q \
  tests/test_daily_pipeline_characterization.py \
  tests/test_cohort_lifecycle.py \
  tests/test_shared_policy_volatility_evidence.py \
  tests/test_cohort_failure_reporting.py \
  tests/test_generation_manager.py \
  tests/test_30day_simulation.py
```

---

## Task 2: Centralize pure reporting and daily classification

**Files:**

- Add: `tradingagents/strategies/orchestration/daily_pipeline.py`
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py`
- Modify: `tradingagents/strategies/orchestration/generation_manager.py`
- Modify: `scripts/run_cohorts.py`
- Modify: `tests/test_daily_pipeline_characterization.py`

### Steps

1. Move the strict candidate/governed aggregators and cohort counters into
   `daily_pipeline.py`; retain compatibility re-exports from
   `cohort_orchestrator.py` until all callers migrate.
2. Add one immutable `DailyRunSummary` and one strict
   `summarize_cohort_results()` function.
3. Make the worker CLI and generation manager consume that summary while
   keeping their rendering, return-code contradiction checks, and wire/history
   trust boundaries separate.
4. Extract manifest-result normalization and bounded history-entry creation as
   pure functions. Delete repeated outcome, ticker, recovery, issue, and history
   assembly.
5. Run characterization, worker-wire, and generation-manager tests.

---

## Task 3: Extract finalization and repeated cohort-result construction

**Files:**

- Modify: `tradingagents/strategies/orchestration/daily_pipeline.py`
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py`
- Modify: relevant lifecycle/failure tests

### Steps

1. Add small pure helpers for a canonical lifecycle result, cohort failure
   assignment, candidate filtering, and stable result ordering/defaults.
2. Represent daily mutable collections in one explicit `DailyRunState`
   dataclass; keep durable reads/writes in the orchestrator.
3. Replace repeated ad hoc result dictionaries without changing intentional
   omissions on failed historical paths.
4. Route every post-context return through one finalizer. Preserve the current
   no-cohort behavior as a characterized compatibility case.
5. Re-run invalid-epoch, pending-gap, replay/tamper, and mixed-failure tests.

---

## Task 4: Extract execution and screening phases

**Files:**

- Modify: `tradingagents/strategies/orchestration/daily_pipeline.py`
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py`

### Steps

1. Extract pure partitioning of complete replay, stage-only, stored resume, and
   fresh execution states.
2. Extract shared governed ticker scopes and deterministic membership maps.
3. Extract small execution-result and due-outcome phase functions while leaving
   ledger calls, critical-gap persistence, and exception re-raise ordering in
   the orchestrator shell.
4. Extract horizon screening/result assembly and delete the duplicate
   completed-versus-new due-outcome loops.
5. Run partial-resume, critical-gap, ledger, metric-epoch, and 30-day tests.

---

## Task 5: Extract candidate validation phases and thin `run_daily()`

**Files:**

- Modify: `tradingagents/strategies/orchestration/daily_pipeline.py`
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py`

### Steps

1. Move pure candidate identity, evidence digest, issue reconstruction, scope,
   history-evidence, and conflict functions into `daily_pipeline.py`.
2. Extract reference-bar validation/replay and volatility-history
   validation/replay into explicit phase functions. Keep governed-first
   classification, compatibility write order, one bounded retry, secret-safe
   errors, and zero-provider-I/O replay unchanged.
3. Leave `run_daily()` as the 150-250 line transaction coordinator joining the
   context, governed execution, screening, candidate validation, staging, and
   finalization phases.
4. Delete specialized/duplicated aggregation and payload construction made
   obsolete by the shared phase results.

---

## Task 6: Exact-head verification, review, and merge

### Focused and parity gates

```bash
PYTHONPATH=. .venv/bin/pytest -q \
  tests/test_daily_pipeline_characterization.py \
  tests/test_candidate_inputs.py \
  tests/test_market_data_contract.py \
  tests/test_cohort_lifecycle.py \
  tests/test_shared_policy_volatility_evidence.py \
  tests/test_metrics_store.py \
  tests/test_metric_epochs.py \
  tests/test_cohort_failure_reporting.py \
  tests/test_generation_manager.py \
  tests/test_30day_simulation.py \
  tests/test_shell_preflight_contract.py
PYTHONPATH=. .venv/bin/pytest -q -m "not live"
.venv/bin/pip check
.venv/bin/python -m compileall -q tradingagents scripts
git diff --name-only --diff-filter=ACMRT private/main...HEAD \
  | rg '\.py$' | xargs .venv/bin/ruff check
bash -n scripts/daily_trading.sh scripts/run_preflight.sh deploy/systemd/install.sh
git diff --check private/main...HEAD
```

### Quantitative gates

1. Measure the four-file production surface against the 4,966-line baseline;
   require at most 4,221 lines.
2. Measure `CohortOrchestrator.run_daily()` by AST start/end lines; require
   150-250 lines.
3. Run the exact incident corpus at the reviewed head and compare its complete
   passing test roster and exact asserted payloads with the recorded base run.
4. Request independent spec and quality reviews of the whole branch. Resolve
   every Critical and Important finding and rerun the complete gate.
5. Push only `codex/orchestration-simplification`, verify GitHub head/base OIDs
   and mergeability, and merge only the reviewed exact head.
6. Fetch merged `main`, prove ancestry/tree parity, and rerun the focused,
   incident, line-count, and full non-live gates before deployment.

PR 3 is not a vehicle for unrelated behavior fixes. A newly discovered defect
must first receive a failing regression and an explicit decision before the
structural extraction continues.
