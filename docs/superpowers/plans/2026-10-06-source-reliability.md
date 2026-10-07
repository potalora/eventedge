# Source reliability implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development with bounded file ownership, regression-first work and independent review. The root integrates and commits.

**Goal:** Recover transient source failures safely, preserve honest coverage and produce reproducible daily outcomes and reports.

**Architecture:** Extend the existing provider, immutable-input, ledger and reporting boundaries. Add a common acquisition policy and safe source snapshot store; use an explicit SIP daily-price policy for new generations. Keep research models unchanged.

**Tech stack:** Python 3.11, requests, pandas, SQLite, pytest; no new service or dependency.

**Spec:** `docs/superpowers/specs/2026-10-06-source-reliability-design.md`

## Global constraints

- Keep all twelve strategies and sixteen scenario books.
- Do not mutate production, historical observations, accepted economic inputs, credentials or scheduler state.
- Topic branch `codex/source-reliability` starts at fetched `origin/main` in the existing isolated checkout; origin here is potalora/eventedge.
- Source failures must never become empty success or be cached as healthy.
- New price/source behavior requires a new generation; prioritize current correctness over historical compatibility.
- Tests run offline and must prove the incident before the repair where feasible.
- Root owns integration and commits; agents edit only assigned files and do not spawn other agents.

## Task 1: Provider contracts and bounded acquisition

Files: source adapters and a new `data_sources/request_policy.py`; provider-focused tests. Root owns engine fetch wiring; coordinate changes to its source-specific methods explicitly.

Interfaces: provide `provider_request(provider, method, url, **kwargs)` compatible with monkeypatched requests transports, `provider_call(provider, operation, callable)` for SDK calls, and a `provider_budget(provider, deadline, ...)` context for a shared deadline and diagnostics. Refine exact keyword signatures in the implementation brief before integration. Keep existing `SourceFetchError` safe envelopes and partial data.

- [x] Reproduce EDGAR 500 and malformed-200 becoming healthy empty data through actual adapter/fetch/health/preflight.
- [x] Test transient 503→200, valid empty200, terminal401/403/400, Retry-After, pacing, exhausted deadlines, no late retry, partial results and redaction with injected clock/sleep/random functions.
- [x] Implement shared recovery and migrate adapters without nested retry amplification. Confirm CourtListener 5/min,50/hour,125/day defaults and 3–5 maximum attempts within one budget.
- [x] Repair EDGAR query filters/schema and remove brittle CapitolTrades fallback; keep FMP stable endpoint behavior and explicit missing-key/errors.
- [x] Parameterize required-operation conformance tests across all configured sources, including NOAA/USDA/drought/CFTC/finnhub partial or exhausted failures.
- [x] Run focused suites and report exact signatures, files and red/green evidence.

## Task 2: Explicit primary SIP daily-price policy

Files: `execution/price_source.py`, `execution/alpaca_daily_bar.py`, related price/execution modules and tests; coordinate factory/config changes with root.

Interfaces: a price-source factory taking the existing config and retaining the full price-source protocol. New `paper_ledger.pricing_version=raw-alpaca-sip-v1` selects validated raw SIP daily bars; SIP is the current default, with no fallback to Yahoo raw daily prices. Document any remaining Yahoo history/volatility dependency.

- [x] Reproduce AYI/BR incident fixtures; assert the new policy accepts coherent exact-session SIP bars and does not touch Yahoo daily bars.
- [x] Assert missing credentials, unready session, wrong symbol/feed/date, multiple rows, incoherent OHLC and transport errors fail visibly; governed obligations remain blocking.
- [x] Wire raw pricing through candidate references and held/pending positions, including preflight. Keep dividend-adjusted benchmarks on their validated Yahoo path.
- [x] Test persisted/replayed provenance and immutable same-session input reuse with the existing ledger guards.
- [x] Run price/recovery/ledger regression suites and report integration details.

## Task 3: Persistent acquisition cache and frozen daily source bundle

Files: new `orchestration/source_inputs.py`, `multi_strategy_engine.py`, `daily_pipeline.py`, `generation_manager.py`, preflight wiring and focused tests.

Interfaces: `SourceInputStore` has canonical versioned encode/decode, freshness-aware `load_cached`/`save_cached`, and exclusive session `freeze`/`load_frozen`. Payload identity includes source/session/window/config; successful cache entries are separate from immutable accepted session bundles.

- [x] Test round trips for source pandas/date/decimal payloads; reject malformed, mismatched, future-dated and expired entries without unsafe deserialization.
- [x] Test failures/partial results never enter the success cache and two phases reuse only still-valid matching data.
- [x] Wire per-provider deadline contexts into fetch fan-out, using one acquisition start/deadline and safe diagnostics.
- [x] Freeze the exact daily shared inputs before screening, then reuse them on a permitted resume; ensure preflight does not mutate accepted inputs or accounting.
- [x] Test changed later provider values cannot replace a frozen bundle and corrupt bundles fail visibly.

## Task 4: Coverage propagation and deterministic operational report

Files: `daily_pipeline.py`, `cohort_orchestrator.py`, outcome/envelope validation, new `orchestration/operational_report.py`, report CLI, tests and monitoring instructions as applicable.

- [x] Reproduce recorded data_failure with valid accounting producing clean; derive required coverage degradation from persisted exact-session strategy health.
- [x] Preserve affected strategy/cohort scope, successful-empty semantics, recovered events and optional enrichment distinctions in result/manifest/CLI evidence.
- [x] Produce reports from exact-session attempt identities plus read-only ledger/metrics queries, preserving earlier preflight incidents even when daily succeeds.
- [x] Test multiple attempts, completed degraded runs, missing cohorts, corrupted evidence, misleading generic exit0, independent capital books, distinct fills and partial closes.
- [x] Expose a repeatable report command suitable for the existing monitor; validate report contents against its structured source summary.

## Task 5: Full incident acceptance and reviewed delivery

Files: an offline incident corpus/acceptance test, README, verification notes, release runbook and CI only where needed.

- [x] Replay source faults at HTTP/SDK boundaries and model fixtures through real daily orchestration, persisting health, accounting, staging, wire result and report.
- [x] Repeat sessions and resume interrupted stages; assert stable input identities, no duplicate accounting and honest outcomes.
- [x] Run `python -m pytest -q -m 'not live' tests` on the integrated tree, relevant lint and diff checks.
- [x] Obtain independent implementation and final branch review; repair findings and rerun affected checks.
- [x] Update README with Humanizer, configuration/cutoff rules, remaining dependencies, operational commands and deployment requirements.
Delivery gate: commit explicit paths, push the topic branch, open/attach the PR, and verify CI before handoff. A PR is not a deployment; prospective continuity requires observed sessions.

## Task 6: Clef and Jev decision-model assessment

- [x] Identify exact projects using current primary sources.
- [x] Assess bounded decision classification, calibration, costs/licensing, leakage, prompt injection and suitability versus the current event/committee workflow.
- [x] Save a source-linked memo and a small offline/shadow evaluation proposal. No new model account, API calls, live decisions or purchases.
