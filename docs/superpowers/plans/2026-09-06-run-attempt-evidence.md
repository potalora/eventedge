# Matrix Run Attempt Evidence Implementation Plan

> **For agentic workers:** Use test-driven development and task review. Root owns integration and final verification.

**Goal:** Preserve actionable evidence for every completed, timed-out, or launch-failed daily/preflight worker invocation while retaining the full research matrix.

**Architecture:** The generation manager writes an append-only operational JSON artifact outside generation state. Existing worker results and trading semantics remain authoritative and unchanged. The CLI prints the artifact path and governed failure reasons.

**Tech Stack:** Existing Python standard library, pytest, Ruff; no new runtime dependency.

## Global constraints

- Retain all 12 strategies and all 16 cohorts, existing classification and exit codes, financial rules, provenance, and immutable epoch rules.
- Do not write preflight evidence inside `data/generations`; preserve the existing full-tree identity regression.
- No production operations, generation creation, main commits, merge, or force push. Topic branch and PR only.
- Evidence is operational information, not permission to replay or promote a failed research observation.
- Continue keeping the legacy latest daily log for compatibility. Archive attempts separately with unique exclusive filenames.
- Archive stdout/stderr/result strings after redacting known credential environment values and common credential/header/query formats. Never store an environment dump. Use restrictive file permissions.
- Failure to write diagnostics must be visible but must not change an already completed economic result or cause an implicit retry.

## Task 1: Capture immutable attempts at the manager boundary

**Files:** add `tradingagents/strategies/orchestration/run_evidence.py`, add `tests/test_run_evidence.py`, modify `generation_manager.py`, extend `tests/test_generation_manager.py`.

**Interface:** The existing `_run_cohorts_subprocess(...) -> dict` remains callable with its current arguments. Successful evidence persistence adds `evidence_path` to its returned result. Persistence failure adds `evidence_error` and logs a warning. Daily history keeps those two optional diagnostic fields.

- [ ] Reproduce lost evidence with two invocations on the same date; assert two independent artifacts and unchanged first bytes after the second invocation.
- [ ] Reproduce missing governed preflight detail: retain its full structured result and captured streams while the entire generation tree remains byte/stat-identical.
- [ ] Reproduce lost timeout output, including `TimeoutExpired` byte streams, and launch errors; preserve failure kind, elapsed time, partial output, and original reason.
- [ ] Add tests for credential redaction, file mode, and persistence failure preserving the worker result.
- [ ] Run these tests before implementation and confirm expected failures.
- [ ] Implement a small evidence helper with one immutable JSON file per attempt under `<repo>/data/logs/run_attempts/`. Use an exclusive unique name; store schema version, generation ID/commit, requested session, action (`daily` or `preflight`), preflight mode, UTC started/finished times, process return code, process status (`completed`, `timeout`, `launch_error`), sanitized stdout/stderr, and sanitized structured result. Session and generation need not be interpolated unsafely into paths; unique IDs plus a readable timestamp are sufficient.
- [ ] Wrap the existing result-producing manager method to persist the returned result once. Capture raw subprocess streams/return code in a private per-call record; do not reinterpret daily wire or duplicate normalization logic. Preserve timeout output even when `write_log=False` (that flag controls only the legacy generation-local log). Keep process completion distinct from research clean/degraded/failed outcome.
- [ ] Preserve diagnostic pointers in daily history. Do not alter preflight history or manifest behavior.
- [ ] Run new evidence tests and existing generation/preflight contracts.

## Task 2: Surface evidence in the existing CLI

**Files:** modify `scripts/run_generations.py`, add `tests/test_run_evidence_cli.py`, update README.

- [ ] With a stub manager result, assert failed governed preflight prints ticker/reason and evidence path and still exits nonzero.
- [ ] Assert success also prints the evidence path; assert archival failure is visible and preserves original daily/preflight exit behavior.
- [ ] Implement a shared small diagnostic printer, called for daily and preflight results. Print bounded governed failure details, preserve original classification, and direct the operator to the retained artifact for full detail.
- [ ] Document artifact location, redaction, preflight state isolation, incomplete hard-kill coverage, retention/disk considerations, and the distinction between operational outcome and strategy coverage.
- [ ] Run CLI and shell-gate regressions.

## Task 3: Review and verify the complete branch

- [ ] Root reviews the evidence capture boundary and confirms the matrix and decision paths are unchanged.
- [ ] Run `python -m pytest -m 'not live' -q`, focused Ruff, and `git diff --check` in the isolated checkout. Record environment-related baseline failures separately if any; do not hide them.
- [ ] Independent branch review covers semantics, state isolation, errors, redaction, artifact collisions, and tests. Resolve material findings and rerun covering checks.
- [ ] Commit exact source/test/doc paths on `codex/matrix-run-evidence`, push that branch, and create a PR. Do not deploy.

## Deliberate follow-on work

The matrix-wide source coverage/eligibility model, shared input/analysis simplification, and source-fidelity corrections are separate changes described in the companion design. This PR preserves evidence needed to diagnose and validate those changes; it does not claim to fix the unretained September exceptions or establish reliable clean runs. Attempts interrupted by host loss/SIGKILL before subprocess return need a later started/completed reconciliation design; this change handles normal completion, Python exceptions, and managed timeouts.
