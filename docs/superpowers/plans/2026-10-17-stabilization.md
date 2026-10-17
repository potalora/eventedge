# Sixteen-book stabilization implementation plan

> **For agentic workers:** Use `superpowers:subagent-driven-development` for bounded implementation and independent review. Root owns integration, verification and Git operations.

**Goal:** Complete the approved stabilization scope while retaining all sixteen books and explicitly deferring unproven document work.

**Architecture:** Extend the existing frozen-profile, immutable-input, ledger and process-ownership mechanisms. Share one derived completion contract between consumers; do not add a scheduler, service, database or repair framework.

**Tech stack:** Python, SQLite, existing shell/systemd entrypoints, pytest and the current two GitHub CI jobs.

**Spec:** `docs/superpowers/specs/2026-10-17-stabilization-design.md`

## Global constraints

- Retain exactly four horizons and four sizes: sixteen books.
- Preserve financial, risk, time, evidence and accepted-state integrity checks.
- Historical profiles and accepted receipts retain their existing semantics.
- New behavior is opt-in through a frozen prospective stabilization profile.
- No live provider/model calls during implementation; all acceptance IO is constructed and explicitly labelled.
- No production changes, generation start, timers, merge, main push or force push.
- Sol 6.1 performs substantive implementation/review; Luna performs bounded mechanical work.
- Agents edit only assigned files and never commit, push or spawn; root serializes integration and commits.
- The user approved the branch-base exception on October 17: build additively on reviewed candidate `72fe487a` and open a separate PR against `codex/audit-reliability-fixes`.

## Task 1: Freeze scope and align completion

**Files:** new `config/profiles/stabilization-v1.json`; a small profile/completion helper under `tradingagents/strategies/orchestration/`; existing `filing_research_policy.py`, `multi_strategy_engine.py`, `preflight.py`, `operational_report.py`, `release_readiness.py`, `scripts/recover_runtime.py`; their focused tests. Inspect `scripts/daily_trading.sh` and notification consumers and change only where needed to use the same completion result.

**Produces:** `profile_contract(frozen_config, *, profile_sha256)` validates the exact supported profile and produces its roster/exclusion contract; `evaluate_completion(profile, facts)` returns a deterministic projection containing workflow completion, continuity eligibility, coverage limits and fixed reason codes. Facts come from existing canonical readers. Existing clean/degraded/failed outcomes remain available. The helper must not trust an incoming completion boolean.

- [ ] Add failing tests for an exact supported profile: sixteen valid books, 48 exact health records, six supported and six explicitly excluded strategies. Mutate missing/duplicate health, unsupported disable reasons, profile/declaration hashes, failed shared inputs, screening/committee model holds and invalid staging; each must fail completion.
- [ ] Implement the frozen profile with the existing EDGAR filing switch, regulation lifecycle retirement, bounded-document policies off and top-level decision shadow off. Preserve supported focused-litigation identity checks, paper execution and existing risk/model budgets.
- [ ] Bind the three bulk-filing strategy exclusions to an immutable declaration and exact sentinel; do not classify arbitrary EDGAR failures as exclusions. Apply the same validated scope during screen preflight.
- [ ] Reuse canonical report/ledger/screening/committee readers for one completion predicate. Connect controller and release readiness to it; keep optional source degradation visible and distinct from continuity qualification.
- [ ] Run the focused profile, lifecycle, preflight, operational report, recovery report and readiness suites. Retain a historical-profile control. Package the diff and obtain independent spec/quality review.

Example decisive behavior:

```python
assert evaluate_completion(profile, valid_supported_session)["workflow_complete"] is True
assert evaluate_completion(profile, session_with_committee_timeout)["workflow_complete"] is False
assert evaluate_completion(profile, session_with_unknown_exclusion)["workflow_complete"] is False
assert evaluate_completion(profile, valid_supported_session)["research_coverage_complete"] is False
```

The tests construct validated source/ledger evidence through existing fixtures; they must not substitute those four result dictionaries for the production path.

## Task 2: Isolate proven source capabilities

**Files:** `research_resilience.py`, FRED/Yahoo acquisition sections and screening selection in `multi_strategy_engine.py`, required source adapters, source fingerprint/semantics integration, focused numeric/source/research tests. Coordinate any `session_executor.py` semantic-key edit with Task 3.

**Interface:** extend `project_sources(data, sources, *, codec_limits=None, capability_policy=None) -> tuple[dict, dict]`. New policy is `regime_strategy_capabilities_v1`; absence preserves the prior receipt and behavior. New acquisition manifests and selection receipts are versioned, strictly validated and reconstructed during replay.

- [ ] Reproduce PAYEMS-only failure with independently valid HY/yield/VIX. Assert the shared regime survives while original diagnostics and incomplete coverage remain. Reproduce required HY/yield failure and assert the hold remains.
- [ ] Persist complete operation outcomes at acquisition. Reject unknown or contradictory failure attribution; never treat a valid partial prefix as a successful fetch.
- [ ] Implement FRED required-regime and conservative strategy compartments. Implement Yahoo VIX independent from its atomic research basket. Keep failed frames out of accepted cache and preserve unknown-consumer fail-closed behavior.
- [ ] Make screening consume the actual validated projection; reconstruct identical scope, holds and hashes in admission/replay.
- [ ] Test malformed/forged manifests, non-finite current observations, alias conflicts, codec round-trip, Yahoo failures exceeding the diagnostic cap and zero-IO replay. Run existing research-resilience, hold-staging, numeric, FRED observation, weather and source-pipeline suites; obtain independent review.

```python
projection, scope = project_sources(original, sources,
    capability_policy="regime_strategy_capabilities_v1")
assert original == retained_original
assert scope["research_coverage_complete"] is False
assert projection["fred"]["BAMLH0A0HYM2"].equals(retained_hy)
```

No generic per-symbol survivor sampling or new source registry belongs in this task.

## Task 3: Seal shared economics and recover unstarted books

**Files:** one focused shared-execution receipt helper; `daily_pipeline.py`, `runtime_recovery.py`, narrowly scoped `session_executor.py`/ledger transaction use; recovery and source-pipeline tests.

**Interface:** `prepare_shared_execution(owner, session, epoch_id, bundle, processed_at, borrow_by_book)` returns a read-only prepared payload; `freeze_shared_execution(owner, payload, deadline=None)` publishes through the existing immutable typed source store and validates the winning payload; `load_shared_execution(owner, session)` returns the stored payload or None; `validate_shared_execution(owner, payload, session, epoch_id)` returns validated per-book recovery plans only after checking every book. Identity binds generation, code, effective frozen configuration, full epoch context, session, exact roster and scopes. Existing bound execution contexts remain authoritative. Use explicit named dataclass codecs for the bundle, not generic serialization.

- [ ] Add failing first/middle/last-book interruption cases at context binding and snapshot/staging boundaries. Verify mixed started/unstarted books resume accounting with zero acquisition/model calls, while absent committee decisions remain missed samples.
- [ ] Seal all accepted inputs before any economic effect. Include relevant overdue intent state in the precondition; use the existing ledger transaction to make cancellation and initial context binding atomic.
- [ ] Validate the entire receipt and all cohort identities/starting states before the first resumed effect. Preserve volatility, borrow, corporate-action, phase-chain and later-session checks. Historical missing seals remain fail-closed.
- [ ] Detect an existing seal before ordinary daily reentry can take its fresh-acquisition branch; route it through the same validated plans.
- [ ] Test corrupt seal, altered scope/roster/profile/epoch, stale starting state, missing required evidence and a crash between overdue cancellation and context binding. Repeat recovery and assert unchanged fill/cash/snapshot counts.
- [ ] Run focused recovery, phase, execution-context and ledger suites; obtain independent review of the state transition and crash behavior.

```python
before_calls = fixture.calls.copy()
result = recover_owner(owner, session, now=clock)
assert all(cohort["ledger"].phase_completed(session, phase)
           for cohort in owner.cohorts for phase in PHASES)
assert fixture.calls == before_calls
assert result["reason_code"] == "checkpoint_missing"  # no invented committee
```

## Task 4: Reuse worker process ownership

**Files:** `generation_manager.py`, relevant generation-manager and scheduled-timeout tests.

**Consumes:** existing `_run_preflight_worker` process-group ownership and timeout result contract. Reuse it for ordinary daily launches, with a clearer shared name if warranted and compatibility for existing tests/callers. Do not use the 65 KiB diagnostic-tail helper for full result envelopes.

- [ ] Add a real child/grandchild regression for ordinary daily timeout and leader exit; retain stdout/stderr and inherited-lock evidence.
- [ ] Route the daily launch through the existing owner. Preserve versioned result parsing, timeout classification, log writing and signal exit semantics.
- [ ] Verify descendant termination and lock reuse on Linux/macOS-compatible process checks. Run focused manager/timeout suites and obtain independent review.

This is suitable for bounded execution once the exact call-site and tests are specified; escalate process-semantics uncertainty to Sol.

## Task 5: Exact-profile composed acceptance and delivery

**Files:** `tests/scheduled_acceptance/harness.py`, deterministic bootstrap fixtures, new stabilization scheduled tests, marker/CI selection, `docs/runtime-recovery.md`, concise stabilization release/verification documentation.

**Consumes:** the exact checked-in profile from Task 1, source contract from Task 2 and restart/process behavior from Tasks 3/4. The harness may normalize only paths and inert credentials with an explicit substitution audit; it must not silently substitute policy/model/request versions.

- [ ] Add the new profile variant to the existing harness without changing retained historical fixtures.
- [ ] Run the real shell/controller/manager/worker path through two sessions, sixteen books and a positive paper-intent/fill lifecycle; replay with zero external calls. Assert exact profile/model identities, no deferred document calls and report/controller/readiness agreement.
- [ ] Exercise the finite source/crash/hung-child cases specified above at their appropriate composed or focused boundary. Keep the known NaN/null and failed-report-precedence regressions.
- [ ] Mark the deferred expensive full-graphics corpus as a separate qualification selection; retain document unit/replay regressions. Make the stabilization profile the scheduled release gate.
- [ ] Record code/profile/runtime dependency identity using the existing installed-distribution fingerprint. Keep constructed acceptance, native first-run acceptance, five-session continuity and performance claims distinct.
- [ ] Obtain a whole-change review, resolve material findings and run both existing complete non-live CI jobs on the final frozen branch. Do not repeatedly rerun unchanged broad suites.
- [ ] Push only the topic branch, open and attach the appropriate PR, and report exact checks and limitations. Stop before merge/deployment.

## Coordination and acceptance record

Task 1 precedes the source/acceptance integration. Tasks 2 and 3 may proceed concurrently only with explicit file ownership; Task 4 is independent. Task 5 follows their reviewed integration. Root maintains task reports, red/green evidence, review packages and decisions in one plan-specific ledger. A passing suite is not a native generation claim.

Preparation evidence: candidate `72fe487a` passed 96 focused recovery/readiness/source/lifecycle tests on October 17 (27.24 seconds, one existing websockets deprecation warning). The user approved the candidate branch base before implementation.
