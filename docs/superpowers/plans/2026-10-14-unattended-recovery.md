# Unattended recovery implementation plan

1. Fix generation transaction ordering and interruption reconciliation; test real Git side effects and durable-state failure boundaries.
2. Add explicit source-retirement dependency validation, acquisition exclusion, accepted-input binding, and truthful operational reporting; test legacy behavior and rejection cases.
3. Implement durable bounded runtime recovery using the existing safe pipeline resume paths; wire candidate systemd definitions and report status without changing trading semantics.
4. Repair the durable Hermes owner-action handoff and reply routing; test crash/replay/reminder behavior with no real sends.
5. Complete Ansible runtime provisioning, publisher CLI validation, and explicit intake deactivation with focused controller tests.
6. Run accelerated unattended scenarios and independent code/evidence review. Fix findings, run relevant repository checks and required application CI, and update the three existing topic PRs. Stop before merge or deployment.

Independent work is delegated by file ownership. The primary agent integrates contracts, reviews safety boundaries, owns commits and PRs, and accepts final evidence.
