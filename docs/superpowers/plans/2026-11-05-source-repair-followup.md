# Source repair follow-up implementation plan

**Goal:** Recover bounded intervention projection from concurrent evidence replacement and make the native consumer reject an empty projection that omits a current terminal broker outcome.

**Scope:** Two independent review candidates; no source adapter, trading, deployment, receipt-reset, worker-retry, or notification changes. The application topic starts at origin/main and carries the exact existing linear prerequisite history through deployed dba04fb; its PR is based on codex/finra-query-api. The native topic starts at origin/master.

## Application candidate

- [x] Add regression tests showing report replacement is classified as transient only by the interventions CLI. Exercise actual capture identity checks, fresh recapture, changed content and invalid paths; retain export/request rejection.
- [x] Add a specific capture-change exception in source_repair.py. Preserve every existing no-follow, identity and byte check. Propagate this exception through intervention-only broad handlers, then return exit 75 with a fixed diagnostic from scripts/source_repair.py. The installed broker already bounds fresh subprocess retries to 120 seconds; do not create another retry budget.
- [x] Run source-repair, intervention, enrichment and endurance regressions. Reproduce broker recovery with the actual installed bounded runner redirected to scratch. Verify terminal failures and continuous churn remain bounded and visible.
- [x] Repair the independently reproduced export blocker: capture and validate the exact frozen generation configuration, bind its SHA to manifest and report, include it in the private bundle, and pass the same operating profile and frozen configuration used by normal reports into source-history projection. Test real supported-profile declarations, missing/mismatched/unsafe configuration, and legacy no-profile exports. Re-run export against the retained live evidence without submitting a worker.

## Native consumer candidate

- [x] Reproduce a current report-bound empty proposal followed by a new root-owned terminal outcome; require read_proposals to report unavailable, without fabricating or sending a card.
- [x] Check current incident identities from validated report evidence against bounded no-follow reads of sanitized outcomes. Detect missing or contradictory urgent cases; retain legacy report compatibility where source-incident evidence is absent. Recheck sampled outcome bytes/absence before accepting the proposal.
- [x] Test correct matching cases, unrelated historical outcomes, malformed/unsafe files, outcome mutation, missing outcomes and terminal reason changes. Run the existing native consumer and policy suites.

## Review and handoff

- [x] Preserve a private incident record and current live source diagnostic separately from historical accepted inputs. Confirm protected hashes remain unchanged.
- [x] Obtain independent code review of both candidates and resolve material findings.
- [ ] Commit and push only topic branches; open review PRs with exact tests and limitations. Do not merge or deploy. The historical terminal admission receipt remains unchanged and projection reconciliation remains a separate operator action.

The November 4 initiating exception cannot be reconstructed from its generic console. The deterministic capture-race reproduction demonstrates a real defect but does not prove the historic trigger. Fresh source-only acquisition now includes the missing House filing; no Congress parser change is justified.
