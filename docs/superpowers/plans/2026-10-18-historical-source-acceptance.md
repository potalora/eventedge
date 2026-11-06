# Historical Source Acceptance Implementation Plan

> **For agentic workers:** Use bounded parallel implementation for the independent corpus and reliability components. The root owns integration, review and verification.

**Goal:** Repair the observed defects, require recorded historical source data in acceptance, and retain reviewed source-reliability lessons.

**Architecture:** A manifest binds public and private historical artifacts to their provenance and replay boundary. A separate pure report projector compares validated source observations with a reviewed catalog. Existing CI and release checks run the replay before constructed integration and fault tests.

**Tech Stack:** Python, pytest, JSON, GitHub Actions, existing source adapters and operational reports.

**Spec:** `docs/superpowers/specs/2026-10-18-historical-source-acceptance.md`

## Global constraints

- Full private captures and operational state stay outside Git.
- All historical replay denies external IO and preserves failed observations.
- Public subset, full supported-source replay and native workflow qualification are distinct claims.
- No source policy, risk, budget, model, accepted-state or deployment changes.
- Update the approved stabilization topic branch; never push main, force-push or merge.

## 1. Integrate reviewed repairs

- [x] Apply the retained final repair patch and verify its file hashes.
- [x] Run the repaired Finnhub, NOAA, spool, FIFO, FRED and host-inspection paths in an isolated offline environment.
- [x] Commit the repairs with the original failure evidence preserved.

## 2. Historical corpus and replay

Files: `tests/historical/manifest.json`, `tests/historical/public/`,
`tests/historical/replay.py`, `tests/test_real_historical_sources.py`,
`scripts/run_historical_source_acceptance.py`.

- [x] Test missing/tampered artifacts and rejected external IO before implementing the runner.
- [x] Retain exact public historical extracts, hashes, acquisition/reference/vintage fields and original-format boundaries.
- [x] Replay public data through real consumers and private accepted envelopes through all supported source paths; replay retained Alpaca wire bytes through its adapter.
- [x] Report public/full scope, gaps, original baselines, controlled mutations, IO counters and candidate/corpus identities separately.
- [x] Verify the full gate fails without the three required private artifacts and passes only with their exact recorded hashes.

Command contract:

```sh
python scripts/run_historical_source_acceptance.py \
  --private-corpus /absolute/private/corpus \
  --require-full-supported-scope --report /absolute/private/report.json
```

## 3. Source-reliability learning

Files: `tradingagents/strategies/orchestration/source_reliability.py`,
`tradingagents/strategies/orchestration/operational_report.py`,
`config/source-reliability-v1.json`, `tests/test_source_reliability_learning.py`,
`docs/source-incident-policy.md`.

- [x] Test stable observation identity, duplicate suppression, novel patterns, exact-evidence attribution and invalid-catalog handling.
- [x] Implement a bounded observational projector; retain source failures and distinguish pattern matching from cause attribution.
- [x] Seed the catalog with sanitized reviewed evidence and expose the projection in JSON and Markdown.
- [x] Verify mutations/harness failures cannot become empirical provider observations or alter execution/incident state.

## 4. Workflow and final acceptance

Files: `.github/workflows/nonlive-tests.yml`,
`docs/scheduled-workflow-acceptance.md`, `docs/stabilization-profile.md`.

- [x] Require the public historical gate in both CI environments before scheduled and ordinary tests.
- [x] Document the required full private release gate and the distinction between actual historical baseline and controlled faults.
- [x] Independently review both implementations, run focused regressions and the full private gate, and check public output for private-data leakage.
- [ ] Commit, push the existing topic branch and update PR51 with final scope and evidence.
- [ ] Verify both complete CI environments on the final commit; retain original and final results separately in the private handoff.

This checklist records preparation of the candidate. The PR checks and private
handoff retain the subsequent commit-bound release verification results.
