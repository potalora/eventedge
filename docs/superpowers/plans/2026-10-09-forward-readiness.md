# Forward readiness implementation plan

> **For agentic workers:** Use the subagent-driven-development workflow, scaled to the user's request for autonomous design, precise regression coverage and parallel independent work.

**Goal:** Repair the remaining concrete runtime, dividend and issuer-attribution gaps and make prospective ETF comparison interpretable.

**Architecture:** Preserve immutable event and accounting evidence; permit only audited enrichment of previously unknown dividend payment terms. Bound actual transport and aggregate model work. Measure paired benchmark excess using retained observations and keep model provenance lightweight.

**Tech Stack:** Python, SQLite, pytest, existing provider and model SDKs.

**Spec:** The user accepted the remaining readiness recommendations with two changes: ETF/S&P 500 comparisons replace a matched rule-only portfolio, and operating-cost accounting is out of scope. Their follow-up requests evaluation of smaller versus frontier/high-reasoning models by task.

## Global constraints

- Work only in the existing PR #50 candidate, baseline `936977ce6cc9b0dae62de1b75a6fcd5fa4b5f44b`, branch `codex/audit-reliability-fixes`.
- No main push, merge, deployment, production ledger mutation, service or timer changes. These behavior changes require a new generation.
- Preserve immutable economic action identity and replay. Unknown terms must never fabricate spendable cash.
- Keep source secrets and raw provider errors out of reports. Use real provider contracts and precise native boundary regressions.
- SPY is the primary S&P 500 opportunity-cost benchmark; BIL and exposure-matched SPY/BIL remain diagnostics. No matched rule-only portfolio or operating-cost subsystem.
- The root owns integration, documentation, review, verification and Git operations. Workers own disjoint files and report evidence in untracked `data/forward-readiness/`.

## Task 1: Dividend payment lifecycle

Own `execution/price_source.py`, relevant `execution/models.py`, corporate action/settlement code in `state/portfolio_ledger.py` and `execution/session_executor.py`, and focused dividend tests.

- [x] Reproduce normal-provider unknown dates and rejection of later verified terms with a minimal regression.
- [x] Implement a verified payment-date source and narrowly audited unknown-to-known updates, preserving ex-date entitlement and economic identity. Late terms must reach outstanding entitlements after positions close and settle once, without rewriting accepted sessions.
- [x] Verify long/short, missing/conflicting dates, retry/replay and crash boundaries; preserve provenance. Check actual provider semantics using primary documentation and a bounded read-only probe.
- [x] Report integration API and covering tests; root reviews and integrates.

## Task 2: Verified government-award issuer attribution

Own `data_sources/usaspending_source.py`, `modules/govt_contracts.py`, a narrowly scoped identity helper if needed, and focused award tests.

- [x] Reproduce the synthetic BOEING COUNTY PLUMBING false mapping before changing implementation.
- [x] Require verifiable native recipient/parent identity tied to a listed issuer. Do not replace substring matching with another unverified name heuristic or invent identifier mappings. Unknown identity is retained as explicit non-actionable evidence.
- [x] Verify provider-shaped recognized parent/subsidiary and unrelated/ambiguous recipients through strategy admission. Confirm the actual provider contract with primary evidence.
- [x] Report tests, provenance and source coverage implications.

## Task 3: Bounded runtime and provenance

Own `data_sources/fred_source.py`, a shared deadline helper, model call path files `learning/llm_analyzer.py`, `llm_utils.py`, `trading/portfolio_committee.py`, screening/runtime deadline sections in `orchestration/daily_pipeline.py` and `orchestration/multi_strategy_engine.py`, wrapper/preflight only as needed, and focused runtime tests.

- [x] Reproduce transport/thread overrun via a real subprocess boundary.
- [x] Bound FRED's real transport and aggregate model work, including SDK retries and backoff. Deadline exhaustion must produce explicit failed/degraded coverage, never rule-only selection or an apparently complete candidate sample.
- [x] Capture configured and returned model identity/revision where provided. Mark absent immutable identity as unpinned; do not invent a revision or add cost accounting.
- [x] Verify wrapper exit/continuation, replay and decision evidence. Coordinate model-routing decisions with root before changing configured models.
- [x] Reuse only validated successful candidate responses to exactly identical requests within the frozen source run, preserving deadline checks, independent receiving-candidate validation and original provenance. Keep horizon-specific prompts and committee decisions separate.

## Task 4: ETF-relative inference and model roles

Root owns `metrics/research.py`, `metrics/populations.py`, relevant service integration, research tests and protocol/docs. Model configuration edits coordinated with Task 3.

- [x] Add failing numeric regressions for paired benchmark uncertainty, identical-return cancellation, missing benchmark evidence and dependence/cluster disclosure.
- [x] Calculate paired SPY, BIL and exposure-matched excess intervals on the same dates with circular blocks. Report a fixed hurdle and explicit inconclusive result; retain descriptive scope and prohibit scenario pooling.
- [x] Count observable independent event/ticker proxies honestly; never call unknown grouping independence.
- [x] Inspect call sites and available supported models. Evaluate bounded extraction versus high-reasoning synthesis on frozen supplied evidence before choosing role routing. Keep risk/execution deterministic and distinguish semantic evidence from profit evidence.
- [x] Document fixed benchmark, review/budget rules, costs out of scope and conditional corporate-lifecycle/short/fill limitations.

## Task 5: Native acceptance, independent review and PR update

- [x] Verify intended-host package/credential/entitlement readiness read-only, identifying host and checkout precisely.
- [x] Run isolated source/model/ledger canaries with realistic candidate volume, retained timing/coverage/failure evidence and no production changes. The four-horizon run exposed the repaired identity-capacity defect; the final run then stopped at a reproducible nonfinite SPY adjusted close before screening. Execution of the checks is complete, but native staging/committee acceptance and future clean-session continuity remain outstanding.
- [x] Resolve native Senate nonstock and SEC ownership-document alias/cross-CIK contract defects with exact fixtures and independent review before the final canary.
- [x] Preserve the native four-horizon identity population with finite streaming/read bounds and immutable replay; retain safe diagnostics for candidate/reference/volatility failures.
- [x] Resolve independent economic and runtime/research review findings, run the complete non-live suite and required static/shell checks.
- [x] Update acceptance/protocol and PR description around the final implementation, with native/empirical acceptance gaps explicit.
- Publication: push only the existing topic branch and record CI for its exact current head in the PR. No merge or deployment is authorized.
