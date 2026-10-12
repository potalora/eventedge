# Research Fault Containment Implementation Plan

> **For agentic workers:** Use the subagent-driven development workflow for bounded independent tasks. Root owns shared pipeline integration, reviews, Git and final acceptance.

**Goal:** Complete safe independent work despite unpredictable research failures, retain every failure and dependent exclusion, and prove operational completion separately from full research coverage.

**Architecture:** Add the prospective `research_resilience_policy="dependency_isolation_v1"` opt-in without changing existing filing-policy verdicts. Recompute a source-bound dependency projection for usable research, validate candidate evidence before committee/staging, and preserve trusted exits when new entries are held. Reuse the existing clean/degraded/failed lifecycle, immutable source store and transactional ledger staging.

**Tech Stack:** Existing Python source envelopes, deterministic evidence validation, SQLite ledger/metric stores, pytest and private governed capture/replay helpers; no new service or model dependency.

**Spec:** `docs/superpowers/specs/2026-10-11-research-fault-containment-design.md`, explicitly approved by Pedro with “Implement this resilience policy.”

**October 12 execution update:** Pedro directed continued work, deployment and testing to remain local so the VPS can undergo maintenance. Complete the same full operational and replay goals in a fresh isolated Mac deployment. Preserve the Linux/VPS launch artifacts as historical; use a separately reviewed local platform profile. Local EventEdge consent and both bounded Luna/Sol connection checks are complete, with app credit spending off. No VPS launch or remote preparation is planned.

## Global constraints

- Work only in the existing topic worktree and PR. No merge, main push, VPS/production mutation or paid API fallback. The authorized local deployment must use a fresh isolated generation; production deployment remains unauthorized.
- Source/model/per-call limits remain 900/2,400/120 seconds; replay remains 110 seconds worker / 120 seconds supervisor. Existing workers, bytes, memory, spool and private-evidence caps remain fixed.
- Preserve original source envelopes, failure attempts, discovery populations and strict full-coverage verdicts. A contained failure is not a successful parse, an absent event or a low-value classification.
- Source identity, accepted evidence, ledger/accounting, held/due prices, proof persistence and replay conflicts remain integrity failures. Unknown internal exceptions are not automatically successful degraded runs.
- Excluded evidence cannot enter candidate, shared model context, committee or entry-intent authority. Where independence is unproved, hold the larger dependent scope.
- Model deadline exhaustion keeps the existing whole-sample hold; faster responses cannot gain preferential trading authority.
- No shared file edits without assigned ownership. Root freezes each accepted checkpoint; no test/proof is claimed against later edits.

## Task 1: Exact filing dependency isolation

**Owner:** bounded application worker. **Files:** create `tradingagents/strategies/orchestration/filing_research_isolation.py` and `tests/test_filing_research_isolation.py`; coordinate any necessary changes to `filing_supported_scope.py`, `filing_material_validation.py`, `filing_inputs.py` or `filing_supported_audit.py` before editing them. Root owns the engine.

**Interfaces:** `isolate_filing_research(original_edgar, config, *, strict_material_scope, acquisition_scope) -> (projected_edgar, receipt)` and `validate_filing_research_isolation(original_edgar, receipt, config, *, projected_edgar=None)` recompute the same complete source-bound selection. The policy check accepts the autoresearch dictionary. The receipt uses `filing-research-isolation-v1`, original envelope/material/acquisition digests, exact ordered occurrences, dependency edges, included/excluded references, reason codes and discovery scope; it grants no order authority.

- [x] Add failing tests mixing a valid filing with a previously unseen malformed original; the original graph and strict failure counts remain unchanged, while only independently verified rows reach projection.
- [x] Add failures for changed accepted hashes/receipts, missing original, contradictory occurrence identity, incomplete discovery, failed predecessor, ownership incorporation and aggregate PQC dependencies. Missing independence excludes the larger scope; integrity corruption raises.
- [x] Implement deterministic closure using existing recomputed acquisition/material/disposition evidence. Keep every duplicate occurrence and original outcome in the receipt. Remove excluded aliases and dependent metadata from the model projection.
- [ ] Verify all completed raw originals remain captured and hash-audited. If the current raw-audit contract needs an extension for failed parses, retain its historical behavior and bind the exact recorded failure; never invent structural validity or normalize bytes.
- [x] Run focused regression/adversarial tests and report the exact reviewed files, red/green evidence and remaining integration requirements.

## Task 2: Candidate admission and safe staging

**Owner:** root. **Files:** new `tradingagents/strategies/orchestration/research_resilience.py`; modify `multi_strategy_engine.py`, `daily_pipeline.py`, `decision_clock.py`, configuration/semantic-identity validators and only necessary ledger interfaces; add `tests/test_research_resilience.py` and `tests/test_research_hold_staging.py`.

**Interfaces:** `research_resilience.configured(config)` validates the autoresearch opt-in. Source, screening and staging share the same source-bound receipt. Per-candidate bindings retain actual provider/source references, analyzer-input digests and shared-context digest; staging recomputes them against frozen inputs. Hold decisions use the existing durable committee/staging records and explicit reason codes, with zero new entry recommendations.

- [x] Prove policy absence preserves prior behavior and invalid policy values fail before work. Include the opt-in in generation/source configuration identity.
- [x] Write failing screening tests for one good and one bad filing, unrelated good source, provider outage/incomplete discovery, invalid model response and unavailable shared regime context. Healthy candidates require complete dependencies; excluded candidates stay journaled with reasons.
- [x] Integrate Task 1 before models; prevent failed-source values from reaching shared regime/context. Bind validated candidates to their actual source inputs, then revalidate before committee and staging. Retained late signals keep their original accepted evidence rather than being rebound to new source facts.
- [x] Write failing hold tests with a held lot: classified research failure or model deadline yields zero new entry intents, preserves existing stops and stages validated price/time exits through `complete_staging`. Research-driven exits require healthy dependencies; account/held-bar/accepted-volatility/replay corruption still fails.
- [x] Preserve actual earlier fills from previously staged intents. Verify interruption before/after staging, durable hold replay and no duplicated exits, fills or entry decisions.

## Task 3: Operational evidence and independent replay

**Owner:** private helper worker after bounded v4 helper review. **Files:** fresh private helper/launch copies under `data/forward-readiness/`; root integrates public `source_coverage.py`, `operational_report.py` and result schemas as needed. Historical closed helpers remain unchanged.

- [ ] Add a prospectively versioned operational contract binding the research policy and exact code. Report `execution_valid`, staging validity, research coverage, contained failures and original full coverage separately through the existing run outcome.
- [ ] Preserve complete discovery/acquisition/model-attempt populations even when failed. Byte capture and immutable identities remain mandatory; a failed acquisition has its recorded outcome rather than an invented original.
- [ ] Independently recompute included/excluded source and candidate dependencies. Reject a removed failure, reintroduced dependency, unbound entry, forged successful coverage or changed accepted evidence.
- [ ] Verify network-free replay reproduces the same degraded/hold result, every committee/staging decision, original failure census and exact ledger effects. Keep resource/deadline and process cleanup checks unchanged.

## Task 4: Campaign, review and release evidence

The application freeze passed 7,209 non-live tests (four live exclusions, one existing warning) in 384.71 seconds, with all 481 checked inputs unchanged. The final enrichment/replay independent review passed 145 focused cases. Full-suite and review evidence are private under `data/forward-readiness/research-resilience-final-verification/` and `research-enrichment-independent-review/`. The private native collector, local platform integration and campaign remain in progress.

- [x] Run all four horizons and sixteen fixture paper books with unseen malformed research, provider/model failures, mixed usable data, global deadline hold and recovery interruptions. Exercise real acquisition/screening/ledger code; fixtures replace transports and model responses only.
- [x] Independently review dependency isolation, global context, exit preservation, integrity failures, replay and reporting. Repair reproduced findings and run focused checks before the full suite.
- [ ] Freeze the final implementation, run the complete non-live suite with before/after hashes, commit/push only the topic branch and require exact-commit CI success.
- [ ] Freeze the reviewed local deployment, platform guards and current prerequisites; run the full isolated native campaign on the Mac within unchanged caps, then immutable zero-call replay. Do not repeat an unexplained failed run.
- [ ] Verify local isolation and owned workers empty; update PR, protocol, verification record and full-system HTML/PDF guide with actual results and remaining gaps. Record that no VPS operations were performed under the local scope; do not claim a fresh VPS-state verification. Safe operational completion is not full coverage or evidence of profitability.
