# Typed Candidate-Input Isolation Implementation Plan

> **For Codex:** Execute this plan with test-driven development and task-level review. Production commits are made only by the root agent. PR 2 branches from merged PR 1 commit `86cf9cbb945cd3770ea889943f970c17a77e0946`; PR 3 and deployment remain out of scope until this PR is merged.

**Goal:** Prevent candidate-only reference-bar and volatility-history failures from cascading across otherwise valid candidates or being mistaken for execution failures, while keeping every governed obligation fail-closed.

**Architecture:** Keep the existing governed market-data path unchanged. Candidate adapters normalize bounded retry exhaustion into structured evidence. `CohortOrchestrator.run_daily()` establishes the governed set first, persists one immutable `CandidateInputIssue` per candidate/dependency/session, filters only affected candidate signals and reference bars, and routes every return through one finalizer. Cohort results carry bounded issue references; generation history stores one deduplicated run-level summary.

**Constraints:**

- Open lots, pending entries, benchmarks, outcome obligations, marks, replay bindings, ledger identity, and metric epochs remain strict failures.
- A ticker that is both a candidate and governed is never quarantined as candidate-only.
- One initial fetch plus one bounded retry is the maximum candidate recovery policy.
- Provider exception text is not copied into worker output or history.
- Accepted immutable issue evidence is reused without provider I/O; unequal replay evidence fails closed.
- A candidate issue yields `degraded`, `execution_valid=true`, `staging_valid=false`, and process exit 0 only when no cohort or governed failure is also present.
- The existing `candidate_bar_quarantines` field remains as bounded compatibility output during PR 2.
- Do not add stage classes, registries, event buses, or new dependencies.
- Do not merge PR #29 wholesale or add its specialized volatility table and parallel aggregation fields.

---

## Task 0: Preserve the reviewed plan

**Files:**

- Add: `docs/superpowers/plans/2026-08-22-candidate-input-isolation.md`

Verify the baseline remains `86cf9cbb945cd3770ea889943f970c17a77e0946`
with 1,836 passing non-live tests and four intentional deselections, then commit
this reviewed plan before touching production code:

```bash
git add docs/superpowers/plans/2026-08-22-candidate-input-isolation.md
git commit -m "docs: plan typed candidate input isolation"
```

---

## Task 1: Define and persist one immutable candidate-input issue

**Files:**

- Create: `tradingagents/strategies/orchestration/candidate_inputs.py`
- Modify: `tradingagents/strategies/metrics/store.py`
- Create: `tests/test_candidate_inputs.py`
- Modify: `tests/test_metrics_store.py`
- Modify: `tests/test_metric_epochs.py`

### Step 1: Write failing model tests

Cover an immutable `CandidateInputIssue` with these bounded fields:

- `issue_id`, `epoch_id`, `session`, `dependency_kind`, `reason_code`, `ticker`
- `source`, timezone-aware `fetched_at`, `requested_history_digest`, `returned_history_digest`
- exact `expected_sessions`, exact `observed_sessions`, `retryable`
- canonical affected signal identities and affected cohort names

Require exact dependency and reason-code vocabularies, uppercase tickers, SHA-256 digests, bounded strings/counts, sorted unique identities/cohorts, XNYS session dates, and deterministic `reference()` output. Reject malformed and mutable nested values.

Use only `reference_bar` and `volatility_history` dependency kinds, and only
`provider_error`, `missing_data`, `stale_data`, and `invalid_data` reason codes.
Keep raw provider text out of the record; the digests and bounded reason code are
the durable evidence boundary.

### Step 2: Write failing immutable-store tests

Add one `candidate_input_issues` table with a unique `(epoch_id, session, dependency_kind, ticker)` scope. Prove:

- `issue_id` is the primary key and the dependency scope is independently unique;
- exact duplicate save is idempotent;
- a different payload or ID for the same scope raises a deterministic unequal-replay error;
- the same ID cannot name a different scope;
- stored payload integrity is revalidated on read;
- old read-only metric stores without the table return no issues;
- bounded epoch/session reads are deterministic.

### Step 3: Implement the smallest model and store surface

Keep canonicalization and validation in `candidate_inputs.py`; keep SQL lifecycle and immutable insert/load logic in `MetricStore`. Do not add a second specialized issue model.

### Step 4: Verify and commit

Run:

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_candidate_inputs.py tests/test_metrics_store.py tests/test_metric_epochs.py
.venv/bin/ruff check tradingagents/strategies/orchestration/candidate_inputs.py tradingagents/strategies/metrics/store.py tests/test_candidate_inputs.py tests/test_metrics_store.py tests/test_metric_epochs.py
git diff --check
```

Commit:

```bash
git add tradingagents/strategies/orchestration/candidate_inputs.py tradingagents/strategies/metrics/store.py tests/test_candidate_inputs.py tests/test_metrics_store.py tests/test_metric_epochs.py
git commit -m "feat: persist typed candidate input issues"
```

---

## Task 2: Normalize candidate reference-bar failures without weakening governed data

**Files:**

- Modify: `tradingagents/strategies/execution/price_source.py`
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py`
- Modify: `tests/test_market_data_contract.py`
- Modify: `tests/test_cohort_lifecycle.py`

### Step 1: Write failing adapter tests

Use deterministic NCL/UI/ZKH fixtures to prove:

- a batch provider exception becomes one bounded first-attempt failure per requested candidate and each ticker receives at most one isolated retry;
- one retry exception quarantines only that ticker and does not abort later candidates;
- successful candidates retain their validated bars;
- raw provider exception text is not serialized;
- the existing governed resolver continues to fail closed and is not routed through candidate normalization.

### Step 2: Implement bounded adapter normalization

Keep `CandidateBarResolution` as the adapter result. Convert provider exceptions and malformed frames into bounded `CandidateBarAttempt.validation_error` values, retry each invalid ticker once, and return accepted/recovered/quarantined sets without raising for candidate-only availability failures. Unexpected programmer/invariant errors still raise.

### Step 3: Write failing orchestration tests

Prove that a quarantined reference-bar candidate:

- creates one persisted `CandidateInputIssue` with `dependency_kind=reference_bar`;
- retains the existing `CandidateBarRecoveryRecord` compatibility audit;
- is removed from signals and execution reference bars before staging;
- creates no fills, marks, outcomes, or policy evidence for the quarantined
  ticker; strategy-health data-failure evidence remains allowed and expected;
- leaves unrelated candidates stageable;
- produces no critical-gap marker and does not invalidate the metric epoch.

Prove a candidate ticker whose execution reference bar is already governed is
never sent to the candidate resolver: the governed bar is reused, no candidate
issue is created, and normal staging may continue.

### Step 4: Implement issue enrichment and persistence

Derive affected signal identities and cohort names only after the governed set is known. Build request/response digests from canonical expected session and attempt evidence. Persist the existing `CandidateBarRecoveryRecord` compatibility evidence first, then persist the generic issue, and only then filter signals.

On replay, load and integrity-check stored reference-bar issues before invoking
the candidate resolver. Recompute the current request, signal-identity, and
affected-cohort scope; equal evidence seeds filtering with zero provider calls,
while any scope or digest mismatch fails closed through the common finalizer.
Add a test whose resolver raises if called during an equal replay and a tamper or
scope-mismatch test that remains `failed` while retaining the prior issue
reference.

Add a partial-write recovery test: when compatibility quarantine evidence is
durable but its generic issue is missing, deterministically rebuild and save the
issue from that compatibility evidence without provider I/O. The reverse
partial state must not be producible by the write order.

### Step 5: Verify and commit

Run:

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_market_data_contract.py tests/test_cohort_lifecycle.py tests/test_candidate_inputs.py tests/test_metrics_store.py
.venv/bin/ruff check tradingagents/strategies/execution/price_source.py tradingagents/strategies/orchestration/cohort_orchestrator.py tests/test_market_data_contract.py tests/test_cohort_lifecycle.py
git diff --check
```

Commit:

```bash
git add tradingagents/strategies/execution/price_source.py tradingagents/strategies/orchestration/cohort_orchestrator.py tests/test_market_data_contract.py tests/test_cohort_lifecycle.py
git commit -m "fix: isolate candidate reference input failures"
```

---

## Task 3: Isolate candidate-only volatility history with the same issue type

**Files:**

- Modify: `tradingagents/strategies/orchestration/candidate_inputs.py`
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py`
- Modify: `tests/test_shared_policy_volatility_evidence.py`
- Modify: `tests/test_cohort_lifecycle.py`

### Step 1: Port only the useful PR #29 characterization

Write failing tests for:

- valid NCL plus invalid UI/ZKH candidate histories: only UI/ZKH are filtered and NCL stages;
- an invalid open-lot or pending-entry history remains a strict failure;
- candidate/governed overlap remains governed;
- empty or still-invalid candidate history after the real `_fetch_missing_prices`
  boundary becomes a bounded issue, while an empty or invalid governed history
  remains a strict failure;
- exact missing, duplicate, stale, and out-of-order XNYS session evidence;
- no fabricated policy context, fill, mark, outcome, or ticker-level metric for
  quarantined candidates; bounded strategy-health evidence remains allowed.

The overlap fixture must include an unfinished cohort's candidate ticker that
is an open lot or pending entry in an already-completed cohort. Inject invalid
volatility history and assert a strict failure, no candidate issue, and no
ledger mutation, proving classification scans every session cohort.

### Step 2: Implement governed-first validation and separate retry boundaries

Build the governed policy ticker set from open-lot and pending-entry projections before classifying candidate-only tickers. Validate and retry governed history through the existing strict path. Validate candidate-only history independently; after one candidate retry, convert remaining failures into `CandidateInputIssue(dependency_kind=volatility_history)`. Never place a governed ticker in an issue.

`_fetch_missing_prices` currently returns `None` and its yfinance adapter can
normalize provider exceptions to an empty frame. Treat the post-fetch cache as
the real boundary: missing, empty, or invalid history maps to the stable bounded
reason code and the retry-boundary timestamp. Do not add another volatility
adapter/result type solely to preserve exception taxonomy.

### Step 3: Filter and persist once

Remove affected signals and their candidate reference bars before policy staging.
Save the generic issue and attach only its bounded reference to affected cohort
results. Regime state retains only the existing bar-quarantine compatibility
field; do not add generic issue IDs there.

Load and validate stored volatility-history issues before any refetch. Recompute
the expected XNYS-window digest plus current identity/cohort scope. Equal stored
evidence filters the ticker with zero `_fetch_missing_prices` calls; unequal or
tampered replay evidence fails closed through the common finalizer. Do not add a
second durable issue field to regime snapshots: the typed store, cohort
references, and deduplicated run history are authoritative.

### Step 4: Verify and commit

Run:

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_shared_policy_volatility_evidence.py tests/test_cohort_lifecycle.py tests/test_candidate_inputs.py tests/test_metrics_store.py
.venv/bin/ruff check tradingagents/strategies/orchestration/candidate_inputs.py tradingagents/strategies/orchestration/cohort_orchestrator.py tests/test_shared_policy_volatility_evidence.py tests/test_cohort_lifecycle.py
git diff --check
```

Commit:

```bash
git add tradingagents/strategies/orchestration/candidate_inputs.py tradingagents/strategies/orchestration/cohort_orchestrator.py tests/test_shared_policy_volatility_evidence.py tests/test_cohort_lifecycle.py
git commit -m "fix: isolate candidate volatility inputs"
```

---

## Task 4: Finalize once and aggregate one run-level issue summary

**Files:**

- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py`
- Modify: `scripts/run_cohorts.py`
- Modify: `tradingagents/strategies/orchestration/generation_manager.py`
- Modify: `tests/test_cohort_failure_reporting.py`
- Modify: `tests/test_generation_manager.py`

### Step 1: Write the PR #29 early-exit regression first

Persist a candidate issue, then force candidate identity validation failure, identity replay conflict, and invalid metric-epoch exits. Assert every path:

- reaches the common finalizer;
- retains typed issue references and `degraded=true`;
- preserves the correct execution/staging validity;
- is still overall `failed` when a real cohort/replay/epoch failure is present.

### Step 2: Write aggregation and untrusted-wire tests

Prove that:

- 16 identical cohort references become one sorted run-level issue summary;
- a reference lists all affected cohorts without repeating the full durable payload;
- duplicate IDs with unequal summaries, malformed fields, unknown dependency/reason codes, and oversized collections fail closed;
- clean and governed-only runs contain no candidate issue field;
- mixed issue plus real failure remains `RunOutcome.FAILED` and process exit 1;
- issue-only execution-valid degradation is `RunOutcome.DEGRADED` and process exit 0.

### Step 3: Implement a common finalizer and consumers

Make every `run_daily()` return call pass through the finalizer after issue state becomes available. The finalizer adds bounded references, compatibility quarantine fields, and degraded validity once. Add a pure typed-reference aggregator used by `run_cohorts.py` and `GenerationManager`; history stores one deduplicated `candidate_input_issues` list.

Do not change the PR 1 worker envelope version or preflight contract.

### Step 4: Verify and commit

Run:

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_candidate_inputs.py tests/test_cohort_failure_reporting.py tests/test_generation_manager.py tests/test_cohort_lifecycle.py tests/test_shared_policy_volatility_evidence.py tests/test_shell_preflight_contract.py
.venv/bin/ruff check scripts/run_cohorts.py tradingagents/strategies/orchestration/cohort_orchestrator.py tradingagents/strategies/orchestration/generation_manager.py tests/test_cohort_failure_reporting.py tests/test_generation_manager.py
bash -n scripts/daily_trading.sh scripts/run_preflight.sh
git diff --check
```

Commit:

```bash
git add scripts/run_cohorts.py tradingagents/strategies/orchestration/cohort_orchestrator.py tradingagents/strategies/orchestration/generation_manager.py tests/test_cohort_failure_reporting.py tests/test_generation_manager.py
git commit -m "fix: finalize candidate input degradation once"
```

---

## Task 5: Documentation, whole-branch verification, review, and merge

**Files:**

- Modify only if behavior text changed: `README.md`
- Modify: this plan if exact verification commands change

### Step 1: Document operator-visible semantics

If README behavior text needs correction, state plainly that candidate-only input issues are one degraded run-level event, not 16 failures and not a clean research observation. Use the humanizer review before committing prose.

### Step 2: Run exact-head verification

```bash
PYTHONPATH=. .venv/bin/pytest -q tests/test_candidate_inputs.py tests/test_market_data_contract.py tests/test_cohort_lifecycle.py tests/test_shared_policy_volatility_evidence.py tests/test_metrics_store.py tests/test_metric_epochs.py tests/test_cohort_failure_reporting.py tests/test_generation_manager.py tests/test_shell_preflight_contract.py
PYTHONPATH=. .venv/bin/pytest -q -m "not live"
.venv/bin/pip check
.venv/bin/python -m compileall -q tradingagents scripts
git diff --name-only --diff-filter=ACMRT private/main...HEAD | rg '\.py$' | xargs .venv/bin/ruff check
bash -n scripts/daily_trading.sh scripts/run_preflight.sh deploy/systemd/install.sh
git diff --check private/main...HEAD
git status --short --branch
```

Record the exact commit and every command result. Repo-wide pre-existing Ruff failures are reported separately; changed-file Ruff must be clean.

### Step 3: Review the whole branch

Request task-level reviews after each implementation task and one independent whole-branch review against:

- `docs/superpowers/specs/2026-08-21-reliability-first-orchestration-refactor-design.md`
- this plan
- `private/main...HEAD`

Resolve all Critical and Important findings, rerun affected tests, then rerun the full exact-head gate.

### Step 4: Push, create, and merge PR 2

Push only `codex/candidate-input-isolation`. Create a PR against current `potalora/eventedge:main`; verify its head/base OIDs, mergeability, and available checks. Merge only the reviewed exact head.

After merge:

```bash
git fetch private main
git merge-base --is-ancestor <reviewed-head> private/main
PYTHONPATH=. .venv/bin/pytest -q tests/test_candidate_inputs.py tests/test_cohort_failure_reporting.py tests/test_generation_manager.py
```

Close PR #29 as superseded with a link to merged PR 2. Do not delete its review worktree or evidence until the replacement is verified on merged `main`.

### Step 5: Hand off to PR 3

Create PR 3 from the merged PR 2 commit in a new worktree. Do not deploy PR 2 by itself.
