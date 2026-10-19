# NOAA yearly delivery implementation plan

> **For agentic workers:** Use Superpowers subagent-driven-development for implementation and independent task review. Root owns integration, verification and topic-branch publication.

**Goal:** Make the prospective NOAA weather acquisition work through official yearly GHCNd files with real-data evidence and preserved safety boundaries.

**Architecture:** A separate opt-in public source adapter owns bounded acquisition, parsing and the documented regional summary. Registry selection and frozen profile identity make the behavior explicit. Main receives a standalone topic PR; the existing stabilization PR receives the reviewed source change and its profile selection.

**Tech Stack:** Python, requests, gzip/CSV, existing provider budgets, pytest and GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-19-noaa-yearly-delivery.md`

## Global constraints

- Ten unchanged agricultural states; all inventory-selected stations; at most seven-day observation lag; 180-second deadline narrowed by parents.
- Actual NOAA fixture bytes and acquisition provenance; fault controls remain separate.
- Explicit `ghcnd_yearly_v1` delivery and `ghcnd_metric_exact_to_standard_v1` unit policy.
- No API fallback, replacement of accepted failed evidence, production changes, main push, merge or force-push.

## Task 1: Retained real fixtures

Files: `tests/fixtures/noaa_ghcnd_yearly/` and its provenance manifest.

- [x] Select exact public annual/catalog rows from the retained originals; hash originals and selected bytes.
- [x] Include real independent station `.dly` values for unit checks and honest cross-year originals where available.
- [x] Validate fixture authenticity independently before use; document normalized versus original formats.

## Task 2: Bounded adapter and contract tests

Files: `tradingagents/strategies/data_sources/noaa_yearly_source.py`, `tests/test_noaa_yearly_source.py`.

Interface: `NOAAYearlySource().fetch_ag_weather_summary(date, lookback_days=30, *, vintage_as_of=None)` returns the existing regional summary contract plus explicit delivery/unit/provenance metadata; `fetch(params)`, `is_available()` and `clear_cache()` satisfy the source registry contract.

- [x] Write meaningful real-input assertions and run them failing before implementation.
- [x] Implement bounded catalog/year acquisition, strict real-data parsing and scoped quality/duplicate checks.
- [x] Implement documented unit conversions and unchanged regional-window aggregation semantics.
- [x] Cover parent deadlines, read/decode/row/memory caps, cross-year windows, failed completeness and cleanup with real-derived fault controls.
- [x] Independently review and resolve correctness/resource findings.

## Task 3: Explicit integration and acceptance

Files: registry, focused registry tests, NOAA delivery runbook and verification CLI; in the stabilization checkout, profile and historical acceptance integration.

- [x] Test registry selection and unknown-policy rejection before implementation; bind changed configuration fingerprints.
- [ ] Preserve missing-policy legacy behavior; select the new policy explicitly in the prospective stabilization profile.
- [x] Run public real fixtures and full retained-file replay without provider/model I/O; missing full originals must fail explicitly.
- [x] Run bounded native source-only acquisition and bind the final decoder to those exact retained bodies with code/dependency identity and coverage/timing limits.
- [ ] Run required focused and complete CI checks on the final pushed commits; retain reports and exact results.
- [ ] Open the main-based source PR and update existing stabilization PR, with generation requirement and not-deployed status.
