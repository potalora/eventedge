# Reliability first

Updated October 24, 2026 Pacific. Pedro's current direction is to preserve future
research ideas in the [experiment log](future-generations.md) and prioritize
reliability. Selection, source expansion, model and strategy experiments are
parked. Recording an idea does not authorize implementation or deployment.

This is a dated assessment, not a live health endpoint. Changing operational
facts must be checked against the exact generation's canonical reports,
runtime status and maintained runbooks before acting.

## Current evidence

The read-only assessment at approximately 11:34 a.m. Pacific found gen_025 at
`e33cc9eed38aa668dbde5f5e2844b7fbfb9391ac`, with clean tracked code and a healthy
runtime heartbeat. The timer was active and the latest observed controller
invocation exited successfully. No recovery worker was retrying or waiting for
user action. No production state or service was changed by this review.

The October 22 and 23 canonical reports are clean for the supported workflow.
Both report valid evidence, accounting, source-input coverage and staging across
all sixteen books, with no candidate-input issues or open source incidents.
Full research coverage is explicitly false under the declared exclusions.
These are report-level findings; this assessment did not rerun the independent
SQLite readiness checker.

Only **two of the five required qualifying sessions** have been observed. The
remaining sessions are an empirical continuity gate, not evidence of a current
failure. Keep the generation unchanged and use its exact-commit read-only
readiness checker while idle once five ordinary eligible sessions are available.
Do not manufacture extra sessions or treat repeated checks as new observations.

## Reliability work and disposition

| Item | Evidence and present disposition | Next bounded action |
|---|---|---|
| Deterministic test clocks | Backported the deployed revision's shared historical-clock fixture and its two consumers. Both previously failing tests now pass; the full non-live suite passes. Runtime freshness checks are unchanged. | Review and integrate the topic PR; no production deployment is needed for this test-only repair. |
| CourtListener historical timeout | One screen operation exhausted three physical attempts on October 23. Its later accepted source acquisition succeeded, and no current source incident is open. Root cause remains unresolved. | Retain the [reviewed lesson](reliability/2026-10-24-courtlistener-review.md) and its proposed catalog entry. Integrate it through reviewed Git on the matching catalog baseline; preserve the original attempt, report and review diagnostic. |
| Five-session continuity | Two qualifying reports exist; sixteen books are consistent in each report. | Accumulate the remaining ordinary sessions, then run the full exact-generation readiness gate. |
| Recovery and notification continuity | Watcher startup reconciliation was separately deployed and verified in [infrastructure PR #52](https://github.com/potalora/hermes-setup/pull/52). This review found no current runtime incident. | Continue normal scheduled observation. Do not repeat fault drills or reset acknowledgement/delivery state without a new verified reason. |

There is no verified new production defect in this assessment. A healthy latest
acquisition does not erase earlier failure evidence, and a historical review flag
does not by itself establish an ongoing outage. Use evidence to distinguish code
defects, access/configuration problems, source/network uncertainty and harness
failures before proposing a repair.

## Verification on October 24

- On this topic branch: all **36 primary SIP tests** pass. The full non-live
  suite passes with **2,760 passed, 4 deselected** in 505.29 seconds. The only
  warning is an existing dependency deprecation for `websockets.legacy`.
  Command: `PYTHONPATH=. python -m pytest -q -m 'not live' --tb=short tests`.
- Supplemental validation in a local checkout of deployed revision `e33cc9e`:
  **67 source-learning and preflight diagnostic tests** pass. The proposed
  CourtListener case validates and leaves the report projection and review flag
  unchanged with network access denied and zero model calls; see the
  [historical review](reliability/2026-10-24-courtlistener-review.md).
- At 11:41 a.m. Pacific, all **89 protected production file hashes** matched the
  earlier baseline. The active generation remained at `e33cc9e` with clean
  tracked code. This work made no production changes and is **not deployed**.
- Independent review found no blockers in the test backport or documentation.

## Working rules

- Prioritize deterministic verification, complete evidence, bounded failure
  handling, preserved ledgers and reliable recovery/notifications.
- Preserve existing source deadlines, retry limits, qualification checks and
  risk gates. A passing result obtained by weakening them is not a repair.
- Complete fixes and relevant regression checks on a topic branch for review.
  A PR is not a deployment; production changes require Pedro's explicit current
  instruction. Intended behavior changes require a new generation.
- Record future research hypotheses, results and rejected alternatives in
  [future-generations.md](future-generations.md). Reliability work does not
  implicitly reopen their implementation or promotion.
- Update this status with dated evidence when a finding changes. Keep private
  source payloads, accounts, credentials, notifications and runtime state outside Git.
