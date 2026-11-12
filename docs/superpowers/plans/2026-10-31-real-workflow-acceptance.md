# Real Workflow Acceptance Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for bounded
> independent implementation and review. Root integrates and owns live execution.

**Goal:** Exercise a complete isolated paper workflow with real sources and live
models, retaining exact replay evidence and explicit edge-case coverage.

**Architecture:** A candidate-parameterized runner uses the existing native
generation lifecycle and guarded prospective launcher. Private pass-through
source/model hooks share one strict tape contract; replay has no network fallback.

**Tech Stack:** Python 3.12, pytest, Git, the supplied candidate's installed runtime,
Linux process/filesystem isolation for the actual native campaign.

**Spec:** `docs/superpowers/specs/2026-10-31-real-workflow-acceptance.md`

## Global Constraints

- New topic from fetched main; no EventEdge merge or production behavior edits.
- Exact candidate/profile/dependencies; real clock in the native lane.
- Actual source and model calls; no constructed success, reduced policies or paid fallback.
- Private bounded captures; no credentials; paper execution disabled; fake delivery only.
- Canonical auth directory and lock may perform normal refresh; never copy the grant.
- Root runs live actions only after integrated review and isolation verification.

## Shared interfaces

`tools/live_workflow/recorded_io.py` owns the process-local configured tape and the
cross-process occurrence reservation. Both helpers use these exact interfaces:

```python
configure_tape(*, mode, tape_dir, run_identity, phase)
reserve_event(*, boundary, operation, public_request) -> dict
finish_event(event, *, response_file=None, sdk_value=None, status=None,
             safe_headers=None, failure=None) -> dict
replay_event(*, boundary, operation, public_request) -> dict
install_source_io(*, mode, tape_dir, run_identity, phase)
install_model_io(*, mode, tape_dir, run_identity, phase)
```

Modes are `live_record` and `offline_replay`. `configure_tape` is idempotent for
identical configuration and rejects incompatible reconfiguration. Source/model
owners coordinate the finished record schema directly; retain JSON-compatible
`sdk_value`, bounded public `failure`, and body file/digest/count for HTTP. Model
requests omit client settings entirely. The runner-owned bootstrap sets explicit
phase and identity via one private config file inherited by child processes.

## Task 1: Source recording and strict tape

Files: `tools/live_workflow/recorded_io.py`, `tools/live_workflow/transport_safety.py`,
`tests/test_live_workflow_io.py`, `tests/test_live_workflow_transport_safety.py`.

- [ ] Write failing tests for exact identity/phase/occurrence, concurrent reservation,
  tampering, missing entries, credential redaction and finite streamed recording.
- [ ] Implement atomic private tape writes and strict replay; never eagerly consume
  streamed HTTP or change original timeout/close semantics.
- [ ] Keep live adapters/SDKs real; name raw-decoded versus SDK boundaries and record
  unsupported boundaries explicitly. Add immutable-parent mutation support.
- [ ] Deny broker writes in Requests, urllib, HTTPX/HTTPX2 and curl families used by
  the candidate; test legitimate source POSTs separately and reject unclassified
  low-level native bypasses.
- [ ] Run focused tests, inspect failures, self-review and report exact coverage.

## Task 2: Native model recording and replay

Files: `tools/live_workflow/recorded_models.py`, `tests/test_live_workflow_models.py`.

- [ ] Write failing tests proving original native call and response observer execute,
  late text is retained without accepted success, and provenance survives unchanged.
- [ ] Wrap the candidate's existing model helpers transparently; record exact logical
  request, role where available, received text and typed outcome before outer parsing.
- [ ] Replay only identical request/phase/occurrence and provenance; reject wrong
  prompt/model/effort/profile and missing recordings without contacting providers.
- [ ] Cover timeout/refusal/malformed/late response mutation without relabeling it native.
- [ ] Run focused tests and coordinate tape schema with Task 1; no auth implementation.

## Task 3: Runner and composition

Files: `scripts/run_live_workflow_acceptance.py`, `tools/__init__.py`,
`tools/live_workflow/{__init__,runner,bootstrap}.py`,
`tests/test_live_workflow_runner.py`, `docs/live-workflow-acceptance.md`.

- [ ] Test refusal of dirty/wrong candidate, unsafe paths/profile, inherited secrets,
  incompatible runtime, reused destination and non-paper execution.
- [ ] Prepare a standalone exact candidate clone and clean private venv using explicit
  verified dependencies; create the generation via candidate code, never a handwritten
  manifest. Normalize only allowlisted paths; retain canonical auth mapping.
- [ ] Install the owned minimal hooks into all descendants, excluding synthetic
  bootstrap/date/SDK/model fixtures. Verify application imports resolve to the clone.
- [ ] Execute real idle observation then guarded prospective launch with exact pins;
  record actual source/model outcomes and all sixteen book/report checks.
- [ ] Provide accepted-state no-IO replay and explicit cold-replay/edge coverage status.
  Keep observer/native composition inputs explicit; fake sink only.
- [ ] Run focused tests, document invocation and limitations, report integration needs.

## Task 4: Integration and actual evidence

- [ ] Root and independent reviewer check integrated code, secret boundaries, process
  cleanup, exact provenance and tests before any live provider/model call.
- [ ] Run one bounded native campaign in disposable state, with ordinary configured
  retries only; preserve any failure rather than silently rerunning the session.
- [ ] Run offline replay and controlled cases from retained actual evidence. Report
  unexercised cases and any missing exact recordings as gaps.
- [ ] Publish a reviewed topic PR with concrete commands/results; never merge EventEdge.
