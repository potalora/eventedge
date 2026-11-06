# Blind-spot Repairs Implementation Plan

> **For agentic workers:** Use focused regression-first implementation and independent review. The user authorized autonomous design and completion in this session; bounded tasks run concurrently with exclusive file ownership.

**Goal:** Repair all ten additional blind spots and the earlier environmental/outcome availability defects, then update PR #50 with verified evidence.

**Architecture:** Preserve immutable acquisition and ledger evidence. Separate portfolio-critical inputs from diagnostic obligations, require continuity before resumed execution, and make model/admission populations explicit. Provider contracts use captured real response shapes and declared economic/survey scope.

**Tech Stack:** Python 3.11, pytest, SQLite ledgers, existing provider adapters.

**Spec:** Findings B1–B10 in the preserved `data/blindspot-review/BLINDSPOTS.txt` and environmental/outcome investigations in `data/availability-investigation/`; the contracts below make the repair scope durable.

## Global constraints

- Work only in the existing `codex/audit-reliability-fixes` candidate for PR #50. No merge, production access, or deployment.
- Use a new generation for these behavior changes. Preserve old facts and missing periods; do not backdate decisions or fabricate historical fills.
- Add compact adversarial regressions at real boundaries. Reuse existing fixtures and parameterize genuinely parallel cases.
- Root owns shared ledger, executor, pipeline, metrics and engine integration. Workers propose shared-file patches rather than racing edits.

### Task 1: Environmental acquisition and survey coverage

Files: NOAA, drought and USDA adapters, weather strategy and focused source tests. Engine wiring is integrated by root.

- [x] Regress true USDM FIPS/camel-case responses; normalize exact categorical semantics and all requested states.
- [x] Regress NOAA flagged-row quarantine with raw pagination counts; acquire the complete regional contiguous window inside a bounded budget using a supported efficient data path. Keep missing dates/types and stale data invalid; preserve exclusion counts.
- [x] Declare crop/class/season/state USDA survey universes, so wheat does not demand unsupported Iowa coverage. Retain complete coverage within each declared universe.
- [x] Run source/weather tests plus a bounded real adapter smoke; capture counts and observed window without secrets.

### Task 2: Filing, award and opinion contracts

Files: SEC/USAspending/CourtListener adapters, filing monitor, filing/government strategies and focused tests. Engine query names are integrated by root.

- [x] Regress current SCHEDULE 13D/G and amendments through query normalization, parsing and routing while preserving legacy compatibility and verified subject identity.
- [x] Select newly obligated awards using provider `new_awards_only`/base obligation dates, not old award modifications or performance dates. Bind amount and event identity to that declared new-award thesis; reject contradictory records.
- [x] Normalize CourtListener cluster identity separately from nested opinion identities; retain all relevant opinion references.
- [x] Verify real captured shapes through adapter-to-strategy compositions and bounded current provider checks.

### Task 3: Committee semantics, context and admission visibility

Files: committee, strategy screening/enrichment/health and focused tests. Root integrates pipeline status and ledger population fields.

- [x] Distinguish successful `[]` abstention from failure. Model failure holds cash, produces a durable degraded decision record, and never silently invokes a different strategy. Explicit rule-only configuration remains identifiable.
- [x] Include every admitted event identity, thesis/evidence, exact score and position exposure in the prompt. Declare any size bound and retain excluded identities; no silent first-20/first-10 slicing.
- [x] Make bounded discovery/admission deterministic and record all discovered, admitted and excluded identities/reasons before analysis, for all screens with limits.
- [x] Regress native abstention/error and model-visible thesis/exposure changes with precise assertions.

### Task 4: Inventory continuity and persistent protection

Files: portfolio ledger, session executor, pipeline/recovery and execution tests.

- [x] Regress explicit and silently skipped split sessions plus a persistent stop across a failed session.
- [x] Acquire and durably bind effective-dated held-inventory actions since its last accepted accounting boundary. Apply each once before valuation/stop execution; missing coverage blocks valid resumed accounting.
- [x] Preserve persistent protective orders across gaps; expire only session-specific orders. Do not reconstruct unknown intraday fills or mark missed sessions valid.
- [x] Make dividend entitlement versus spendable cash explicit; unknown payment dates must not finance entries. Regress payment and replay behavior.

### Task 5: Independent outcome obligations and honest populations

Files: outcome/metric models, immutable ledger records, dependency planner, preflight, executor and pipeline.

- [x] Regress outcome-only missing intermediate prices/actions, maturity with healthy unrelated outcomes, cross-epoch maturity and crash replay.
- [x] Use a shared dependency plan: held/due/benchmark evidence is accounting-critical; outcomes need entry/maturity prices and continuous action evidence independently. The same held ticker retains critical precedence.
- [x] Retain missing outcome evidence as explicit invalid/pending obligations under the original identity across epochs; evaluate healthy outcomes even when another obligation fails. Never count absent mature outcomes as nonexistent.
- [x] Preserve provisional, validated, selected and executed classification from immutable observations into diagnostic readouts. Actionable accuracy excludes failed required analysis; report all populations and missingness separately.

### Task 6: Research diagnostics and final acceptance

Files: metrics/reporting and research protocol/acceptance docs; compact diagnostics tests.

- [x] Provide cost sensitivity, concentration/largest-contributor stress, market/exposure attribution and dependence-aware uncertainty where data supports them, with explicit insufficient-evidence results otherwise. Distinguish measured model calibration and executable fill validation from offline contract tests.
- [x] Update the protocol to remove accepted outcome coupling and document all chosen contracts, data requirements and inability to infer empirical alpha from synthetic tests.
- [x] Run focused suites, independent component/economic review, then `.venv/bin/python -m pytest -q -m 'not live' tests`, compilation, syntax and diff checks.
- Publication gate: commit and push only the topic branch, rewrite PR #50 around the complete change, wait for CI at its final SHA and report not deployed. The PR records publication and CI status.
