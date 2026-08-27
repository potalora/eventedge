# Multi-session candidate issue recovery design

**Date:** 2026-08-27

**Status:** Approved (2026-08-27)

**Base revision:** `3e15f78f75a65cbe78043477ad94e3b6eac24bfc`

## Incident

The 2026-08-26 `gen_011` run completed execution, staging, and snapshots for all 16 cohorts. APOS produced an invalid candidate-only reference bar, so the run correctly quarantined APOS and attached one durable candidate-input issue to each affected cohort. Final reporting then rejected the otherwise valid degraded result with:

```text
ValueError: candidate input issue reference id is invalid
```

The reporting validator treats the date embedded in an epoch ID as the issue session. That assumption is false. An epoch ID records the epoch's start session, and `EpochManager.ensure_epoch()` intentionally reuses the same open epoch across later XNYS sessions when its semantic context has not changed. In this incident the epoch began on 2026-08-24 and the candidate issue belonged to 2026-08-26.

This is a reporting-contract bug. The candidate quarantine was correct, and no governed-data or execution safety check should be relaxed.

## Goals

- Accept candidate-input issue references from a later XNYS session in the same open metric epoch.
- Keep the validator fail-closed for malformed, future-dated, cross-run, incomplete, or conflicting references.
- Turn the reproduced 2026-08-26 shape into a degraded run result instead of an invalid worker result.
- Preserve a byte-for-byte pre-recovery archive of `gen_011`, then retain its worktree, run history, and audited post-cancellation state as incident evidence.
- Prevent the two pending COIN short intents in `gen_011` from executing during recovery.
- Resume scheduled operation in a fresh generation at the reviewed merged commit, without replaying a missed session.

## Non-goals

- Do not weaken governed market-data validation.
- Do not change candidate quarantine, signal generation, sizing, portfolio policy, broker, or fill behavior.
- Do not rewrite `gen_011` run history, copy its portfolio state into the new generation, or classify its failed report as a clean historical run. The two explicit cancellation transitions are the only permitted ledger mutations.
- Do not revive or merge the abandoned VPS Codex branch.
- Do not replay the 2026-08-25 or 2026-08-26 sessions.

## Validator change

`aggregate_candidate_input_issues()` will continue to validate the issue's `session` as the current XNYS trading session when `trading_date` is supplied. It will parse the date embedded in `epoch_id` separately as `epoch_start_session`.

An epoch-scoped candidate issue is valid only when all of the following hold:

1. `epoch_id` has the existing canonical generation/date/hash shape.
2. `epoch_start_session` parses to the same ISO date text embedded in the ID.
3. `epoch_start_session` is an XNYS session.
4. `epoch_start_session <= issue_session`.
5. The issue session is canonical, is an XNYS session, and equals `trading_date` when a run date is supplied.
6. All existing issue-ID, dependency-kind, reason-code, ticker, affected-cohort, coverage, single-epoch, single-session, and durable-scope checks pass unchanged.

The implementation remains pure and does not query the metric store. Within one aggregated report, the exact epoch ID still scopes every reference, while the date ordering captures the lifecycle already enforced by `EpochManager`.

This structural validator is not the source of truth for whether an epoch exists. Upstream durable hydration remains exact: `finalize_daily_results()` reads candidate issues for `state.epoch_id` and `state.session`, validates each record's integrity, and rejects any record whose epoch or issue session differs. That store-backed boundary is unchanged. Only the incorrect equality between epoch start and issue session is removed from report aggregation.

## Regression coverage

The test change will first reproduce the failure with an epoch that begins on 2026-08-24 and a candidate issue reported for 2026-08-26. The test must fail on `3e15f78` with the observed `reference id is invalid` error, then pass after the validator change.

Additional assertions will prove that the narrower rule remains fail-closed:

- a future epoch start is rejected with `candidate input issue reference id is invalid`;
- a non-session epoch start is rejected with the same ID error;
- a wrong or non-session issue session is still rejected;
- durable hydration remains exact-session and exact-epoch scoped, with direct tests for each mismatch;
- existing malformed-reference, scope-conflict, cohort-coverage, and collection-bound tests remain green;
- the existing epoch lifecycle test still proves that identical context reuses an open epoch across later sessions.

Local verification will run the focused reporting and epoch tests, followed by the repository's non-live test suite. The deployment candidate will also run the focused regression on the VPS before a generation is started.

## Production recovery

Production recovery happens only after review, passing tests, merge, and exact commit verification.

1. Before any mutation, capture the `systemctl is-enabled` and `is-active` results for `trade.timer`, `trade-rerun.path`, `trade-preflight.timer`, `trade.service`, `trade-rerun.service`, and `trade-preflight.service`. Refuse if a recorded automatic entry-point enablement is not exactly `enabled` or `disabled`, or if its activity is not exactly `active` or `inactive`. Persistently disable and stop the three automatic entry points with `systemctl disable --now`. The VPS installs its unit files directly in `/etc/systemd/system`, so runtime mask symlinks in `/run/systemd/system` do not override them. Install a unique runtime `RefuseManualStart=yes` drop-in for all six units instead, reload systemd, and verify the three entry points are persistently disabled while all six units are inactive and refuse manual starts. Confirm no daily/preflight worker remains and `.triggers/run-now` is absent. This remains safe across a reboot: runtime drop-ins disappear, but the automatic entry points remain durably disabled and the oneshot services are static.
2. Capture the `gen_011` manifest entry, worktree commit, state paths, database hashes, row counts, and full pending-intent set. The only allowed pending rows are the two reviewed COIN shorts: one in `horizon_30d_size_100k` for quantity 27 and one in `horizon_30d_size_50k` for quantity 13, both eligible on 2026-08-27. Resolve and record their exact intent IDs, linked ticker provenance, side, price rule, eligible session, cohort ownership, quantities, `external_order_id`, and matching `external_orders` rows. Refuse mutation unless both are still pending, provenance is unambiguous, and neither was submitted to a broker.
3. Make and verify a timestamp-unique, collision-refusing rollback archive of the entire `gen_011` state and relevant manifest material before changing ledger state. Record the archive hash and prove a test extraction reproduces the captured database hashes.
4. Re-open each affected cohort ledger through `PortfolioLedger` and cancel only the two recorded intent IDs with `cancel_intent()`. Use one timezone-aware operation timestamp and the audit reason `operator incident recovery: retire gen_011 before fresh generation`.
5. Re-open the ledgers and verify both intents are `cancelled`, their status transitions contain the exact timestamp and reason, and a direct read-only status query finds no remaining pending intents in any cohort. Compare all unrelated table counts and content hashes to the pre-mutation capture. Do not alter signals, fills, marks, lots, or session history.
6. Retire `gen_011` with `--keep-worktree`, retaining its worktree and post-cancellation state as incident evidence.
7. Deploy the reviewed merged commit. The root checkout may contain only the separately inventoried pre-existing runtime paths; refuse any other local change. Require root `HEAD`, the new manifest entry's `git_commit`, and detached generation worktree `HEAD` to equal the reviewed merge SHA.
8. Start `gen_012` as a fresh immutable generation. Verify its state directory is the manifest-owned path and contains no inherited database, journal, snapshot, signal, intent, fill, lot, mark, metric-epoch, candidate-issue, or external-order state.
9. Run generation/status and the time-appropriate no-write preflight smoke check, verifying that the manager supplies the `gen_012` ID, reviewed commit, and isolated state path to its worktree. Before the XNYS close, run screen mode; the governed probe is expected to report `not_ready`. At or after the close, require the governed/all probe. If the captured daily trigger is still future, remove all six runtime start-barrier drop-ins and restore each automatic entry point to its exactly recorded enablement and activity; do not alter the captured persistent state of the oneshot services. Confirm the restored state and next triggers. Do not invoke a historical replay. If the daily trigger has passed, leave the entry points persistently disabled and all six runtime barriers installed.

Cancellation has no supported inverse transition. If either cancellation or its verification fails, keep every entry point persistently disabled and all six runtime start barriers installed, preserve a copy of the partially mutated state for forensics, restore the entire `gen_011` state directory from the verified archive by same-filesystem replacement, and require its database hashes to match the pre-mutation capture. Do not attempt row-by-row reversal. Any other failed precondition, commit-identity check, fresh-state check, or smoke check also leaves the scheduler disabled for investigation.

## Acceptance criteria

- The multi-session regression fails before the code change and passes after it.
- The validator accepts an epoch start before the issue session and rejects an epoch start after it.
- Focused and non-live tests pass at the commit selected for deployment.
- The fix is reviewed and merged. The new generation is created from that exact commit; any unrelated root-checkout changes are inventoried and excluded from its frozen worktree.
- Exactly the two reviewed COIN intents are cancelled through durable ledger transitions in `gen_011`.
- Both intents are proven unsubmitted before cancellation, and no other pending intent exists or changes status.
- `gen_011` remains available with its original worktree, run history, logs, pre-recovery rollback archive, and audited post-cancellation state.
- `gen_012` begins empty at the reviewed merged commit and becomes the active generation.
- The daily, rerun, and preflight entry points are restored to their recorded state without replaying 2026-08-25 or 2026-08-26.
