# Scheduled workflow acceptance plan

**Goal:** Prove the deployed prospective profile through its actual scheduled entrypoints in disposable state, and make these checks a release gate.

**Approved design:** Pedro approved the four-part acceptance proposal in this chat: complete evening workflow and 16 books/report; clock/calendar boundaries; actionable failure reporting followed by a valid evening run; and timeout/lock/state isolation. Include finite document expiry.

**Architecture:** Use the real shell wrappers, generation launcher, frozen profile validation, worker, source acquisition, decision clock/calendar, orchestration, ledgers and report generator. Test-only fixtures replace external transport/model responses and supply a consistent wall-clock instant. All state and caches live under a disposable repository. No production or test-only execution switches are added to application configuration.

## Constraints

- Continue existing release PR #50 on `codex/audit-reliability-fixes`, whose deployed baseline is `c52a62fed94b7971b143e8e30256fcdfa182d589`; canonical `origin/main` was fetched before work. Do not merge or deploy.
- Never access the VPS, live data providers, model accounts, credentials, notification delivery, or authoritative trading state for this suite.
- Keep prospective decision guards intact. Midday screen is a documented diagnostic, with no decision authority, model calls, staging or ledger writes.
- Retain all non-filesystem policies from the deployed profile. Any fixture document bytes/digests and path substitutions must be explicit and auditable; never claim the substituted fixture has the deployed profile digest.
- Tests must fail on the original defect or a meaningful missing side effect. Do not hide release-blocking failures with xfail, skip, weakened expectations or mock internal orchestration.

## Tasks

- [x] Verify baseline and inspect exact deployed profile/entrypoint chain. Baseline: 103 preflight, clock, shell and lock tests passed.
- [x] Add a subprocess acceptance harness under `tests/`, preserving real child-process launch and inherited locks. Test full evening execution/report for all 16 books, same-day failed screen followed by evening success, and accelerated calendar boundaries with real clock rules.
- [x] Add regressions for the documented midday diagnostic contract and minimally restore it without changing daily/governed admission. Preserve structured internal failure causes through CLI, attempt records and operational reports; prove safe classification and alert deduplication/resolution.
- [x] Exercise timeout/crash cleanup and lock reacquisition through the actual configured preflight path. Prove no manifest, ledger or accepted-input mutation; prove finite document expiry refuses eligibility without extending the scope.
- [x] Add a named CI acceptance gate and maintained acceptance documentation; independently review coverage/isolation and implementation.

Release acceptance requires passing focused checks and both exact-head CI jobs. Push
only the existing topic branch, update PR #50, and retain the distinction between
candidate acceptance and the deployed baseline. The PR checks are the current
release result; this implementation plan does not authorize deployment.

## Decisions and evidence

- 2026-10-14: The user-approved design already supplies the implementation authorization; no additional planning approval is needed.
- 2026-10-14: Existing release PR is the appropriate continuation because it contains the deployed code and is still open. Its deployed baseline and later acceptance changes will be distinguished in the PR.
- 2026-10-14: Inject only the wall-clock source in tests; retain real calendar and decision-clock calculations. Do not change host time.
- 2026-10-14: Known midday failure occurs before provider IO because decision admission is applied to a no-decision probe. A correction must preserve that boundary explicitly, not impersonate historical policy or relax `require_reference`.
- 2026-10-14: An initial gate passed 93 cases; subsequent boundary and isolation regressions bring the final gate to 97. The complete remaining suite passed 8,038 cases after correcting 17 obsolete test seams and one wall-clock-dependent lifecycle test. The final harness gate and exact-head CI must pass before release acceptance.
- 2026-10-14: Independent review reproduced and corrected native transport leakage, an outer-shell termination race, a native curl fixture bypass, and noncanonical notification timestamps. Source and harness reviewers accepted the final fixes. See `docs/scheduled-workflow-acceptance.md` for the retained limitations.
- 2026-10-14: Final isolation review found ancestor dotenv discovery when artifacts were nested beneath a project. An empty disposable repository `.env` now bounds the real loader; a synthetic ancestor-canary test fails without it and passes with it. A fresh sixteen-book evening passed with the separately evidenced `GOOGL,GOOG` exclusions and matching actual/accepted configuration fingerprints. Earlier isolation claims are superseded by these final checks.
