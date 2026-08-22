# Reliability-First Orchestration Refactor Design

## Purpose

Escape the recurring EventEdge repair-generation cycle by making daily-run
outcomes unambiguous, consolidating candidate-input isolation, and reducing the
oversized daily orchestrator without weakening any governed market-data,
ledger, replay, or portfolio-policy boundary.

The work ships as three sequential pull requests. Read-only characterization,
incident-fixture preparation, and review may run in parallel worktrees, but
production-code integration remains ordered because every PR establishes the
contract consumed by the next one.

## Observed Failure Pattern

The current system uses the same `success` boolean for two different claims:

- whether a run was clean enough to count as a clean research observation; and
- whether the operating-system process completed safely.

A candidate-only quarantine intentionally produces `success=false`,
`degraded=true`, and `execution_valid=true`. `run_generations.py` currently
turns every `success=false` result into process exit 1, `daily_trading.sh`
propagates it under `set -e`, and systemd therefore declares the oneshot failed.
This is an alert-routing defect, not an execution-integrity failure.

PR #29 addresses a separate candidate-volatility cascade, but adds another
parallel quarantine path and has an early replay/identity exit that can preserve
details while losing the degraded classification. It is evidence and source
material, not a merge-ready architectural foundation.

## Non-Negotiable Invariants

- Open lots, pending entries, marks, benchmarks, outcome obligations, ledger
  identity, metric epochs, and replay bindings remain fail-closed.
- Candidate-only bad inputs may be quarantined only when the ticker is not also
  required by a governed obligation.
- A degraded execution-valid run is not a clean performance observation.
- A shared upstream incident is represented once at run level and referenced by
  affected cohorts rather than copied into 16 apparent independent failures.
- Historical failed sessions are never replayed to manufacture a valid result.
- Deployment uses a fresh immutable generation pinned to the reviewed merged
  commit; the previous generation and a verified rollback archive are retained.
- Production behavior changes and structural refactoring do not share a PR.

## Canonical Run Outcome

Add one authoritative generation-level outcome:

| Outcome | Meaning | Process exit | Performance eligibility |
|---|---|---:|---|
| `clean` | Required execution and staging completed without degradation | 0 | Eligible subject to existing metric rules |
| `degraded` | Execution completed safely with alertable recovery or quarantine | 0 | Not a clean observation |
| `failed` | Execution or a governed invariant could not be proven valid | 1 | Ineligible |

The structured manifest is authoritative. A completed degraded process exits
zero because nonzero shell status means the command failed to complete safely.
Operators and dashboards continue to alert on the manifest's `degraded`
outcome. Transitional `success`, `degraded`, and `execution_valid` fields remain
serialized for old manifests and external consumers, but new decisions consume
`outcome`. Historical `success=true` entries may be read conservatively as
clean. Historical `success=false` entries are treated as failed because the old
booleans cannot distinguish pure degradation from a mixed failed/degraded run.

Preflight retains its independent `success` contract. Per-cohort
`execution_valid` and `staging_valid` fields also remain because they describe
different integrity dimensions from the generation-level operational outcome.

## Component Architecture

Use a functional core with a thin imperative shell. Do not add stage base
classes, registries, plugins, event buses, or a new dependency-injection system.

```text
load run context
      |
validate governed obligations ---- invalid ----> failed
      |
validate candidate inputs -------- issues -----> quarantine candidates
      |
execute eligible cohorts
      |
aggregate run-level issues and cohort references
      |
finalize once: persist -> report -> return RunOutcome
```

The target module boundary is:

```text
tradingagents/strategies/orchestration/
|-- run_outcome.py          # canonical generation outcome and compatibility
|-- candidate_inputs.py     # typed candidate-only validation issues
|-- daily_pipeline.py       # small pure phase functions
|-- cohort_orchestrator.py  # coordination and side-effect boundaries
`-- generation_manager.py   # subprocess invocation and outcome persistence
```

All early exits flow through one finalizer. Phase outputs are immutable values,
not ad hoc mutations distributed throughout the results dictionary.

## PR 1: Truthful Operational Outcome

- Introduce `RunOutcome` with `clean`, `degraded`, and `failed`.
- Add `outcome` to every daily subprocess result and persisted daily history.
- Keep legacy booleans as compatibility projections.
- Make `run_generations.py run-daily` exit nonzero only when any generation has
  outcome `failed`.
- Make the dashboard prefer the canonical outcome and fall back to legacy
  fields for historical manifests.
- Add a contract matrix covering clean, execution-valid degraded, failed,
  malformed output, the exact 16-cohort roster, worker return-code
  contradictions, mixed generations, timeout, and legacy history.
- Do not alter candidate-volatility behavior or cohort execution logic.

## PR 2: Typed Candidate-Input Isolation

- Reimplement the useful candidate/governed split from PR #29 against merged
  PR 1 rather than merging PR #29 wholesale.
- Add one immutable `CandidateInputIssue` record for candidate-only input
  failures. Its bounded structured evidence includes dependency kind, reason
  code, ticker, session, source, fetch time, requested/returned history digest,
  expected/observed sessions, retryability, and affected signal identities.
- Keep any ticker shared with an open lot or pending entry in the strict
  governed volatility set.
- Quarantine candidate-only failures, remove their signals/reference bars
  before staging, and preserve execution validity while marking the run
  degraded.
- Route identity/replay conflicts through the common finalizer so stored issues
  cannot disappear from degraded aggregation.
- Persist and reuse accepted immutable issue evidence without a provider
  refetch; unequal replay evidence fails closed.
- Record one run-level issue with cohort references instead of repeating the
  full issue payload per cohort.
- Add deterministic incident fixtures for NCL/UI/ZKH candidate-only failure,
  candidate/governed overlap, mixed valid and invalid candidates, provider
  exceptions, immutable replay, and the PR #29 early-exit defect.

PR #29 is closed as superseded only after PR 2 merges.

## PR 3: Decompose and Delete

- Freeze PR 2 output using characterization and incident-corpus tests.
- Extract pure context-loading, governed-validation, candidate-validation,
  execution, aggregation, and finalization functions into `daily_pipeline.py`.
- Keep `CohortOrchestrator.run_daily()` as the transaction coordinator and
  side-effect boundary.
- Remove legacy specialized candidate-volatility aggregation and duplicated
  outcome interpretation after all consumers use the canonical structures.
- Preserve exact ledger, metric-epoch, replay, and manifest behavior except for
  explicitly approved canonical serialization changes.
- Reduce `run_daily()` to approximately 150-250 lines.
- Reduce production LOC in the touched orchestration/status surface by at least
  15 percent without counting test growth or replacing clear code with dense
  expressions.

PR 3 is a behavior-preserving refactor. Any newly discovered behavior defect is
first reproduced and fixed separately through a failing test before extraction
continues.

## Verification Gates

Every PR requires, at its exact head commit:

- focused tests for every modified subsystem;
- the full `pytest -m "not live"` suite;
- Ruff and `git diff --check`;
- task-level review and a whole-branch review with all Critical and Important
  findings resolved;
- explicit-path staging that excludes `.env`, `data/`, generated state, and
  unrelated work; and
- green remote checks when available. The absence of configured CI never
  substitutes for local verification.

PR 3 additionally requires deterministic old-versus-new output comparison,
incident-corpus parity, ledger invariants, line-count evidence, and full
verification again after merging into current `main`.

## Deployment and VPS Validation

After PR 3 is merged and local `main` is fast-forwarded to the private remote:

1. Confirm Hermes host, repository, branch, exact pre-deploy commit, timer,
   service result, processes, disk, generation manifest, and dirty files.
2. Preserve the existing user-owned `deploy/systemd/install.sh` modification;
   do not overwrite, clean, stage, or commit it.
3. Create a timestamped rollback archive containing generation state,
   worktrees, repository metadata needed for recovery, and configuration as
   allowed by the established deployment procedure. Verify its SHA-256 and tar
   listing before changing the checkout.
4. Fetch `origin/main`, prove the reviewed merge commit is its exact tip and a
   descendant of the deployed commit, then fast-forward without force.
5. Run focused tests, the relevant full non-live suite when host resources
   permit, Ruff, shell syntax checks, and generation/import parity on Hermes.
6. Start a fresh immutable generation pinned to the merged PR 3 commit. Retire
   the previous active generation while retaining its state and worktree.
7. Run the repository's read-only governed preflight for the current valid XNYS
   session and verify no state mutation.
8. Execute one authorized normal paper-trading run through
   `scripts/daily_trading.sh` only when the session/date and runtime guard permit
   it. Never replay a failed historical epoch.
9. Verify process/service result, timer state, manifest coherence, and ledger
   evidence for unintended fills or pending intents.

If the current day is not a safe runnable session, deployment validation uses
the read-only preflight plus deterministic on-host contract tests; it does not
fabricate or replay a trading day.

## Generation Admission Rule

A production anomaly permits a new code generation only when:

1. a deterministic reproduction exists;
2. evidence distinguishes a code defect from provider state or an intentional
   protective gate;
3. the reproduction fails before and passes after the change;
4. the complete boundary and invariant suites pass at the deployment commit;
5. a rollback criterion is written and verified; and
6. the generation is a reviewed immutable commit rather than an in-place patch.

Execution-valid degradation opens a deduplicated data-quality incident; it does
not automatically trigger a new generation.
