# SEC Material Quarantine Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for the disjoint pure-evidence and private-harness tasks. Root owns hydration/orchestration integration, review, commits and native acceptance. Reuse the existing topic worktree; no agent may mutate Git or the VPS except root.

**Goal:** Retain and frame the complete original submissions for the three approved filings, keep their analyses non-actionable with explicit material gaps, and complete the remaining verified population's isolated sixteen-book proof.

**Architecture:** A new explicit material policy permits a distinct framing-only envelope for exact approved identities. Strict selected-evidence completeness stays false for those envelopes; a separately recomputed material scope establishes whether the remaining analysis population is complete. Native acceptance binds all three full originals and exclusions to immutable source, health, committee and replay evidence.

**Tech Stack:** Existing Python SEC framing/spool/parser code, canonical JSON/SHA256 scopes, pytest, versioned private V6 diagnostic helpers, native isolated Python and GitHub CI.

**Spec:** Pedro's 2026-10-10 approval to retain all three originals, quarantine their analyses and continue; `docs/research/2026-10-10-coverage-runtime-repair-plan.md`, section “Approved three-filing material quarantine”.

## Global constraints

- Opt-in `filing_material_policy="retained_three_material_gaps_v1"` requires `complete_submission_v1`, `bounded_original_submission_v1`, and `two_processes_v1`. Default behavior stays strict.
- Exact identities: NioCorp `0001193125-26-402806`, `10-K`, `2026-09-25`, FILER `0001512228`, 310 documents; Campbell `0000016732-26-000031`, `DEF 14A`, `2026-10-07`, FILER `0000016732`, 91 documents; Dentsply `0000818479-26-000278`, `8-K`, `2026-10-09`, FILER `0000818479`, 505 documents.
- Preserve exact SEC URL, native roles, headers, all document ranges/hashes and primary-candidate inventory. Campbell's HTML/PDF ambiguity remains unresolved. Never select a convenient representation or catch arbitrary parser errors as permission.
- The three known material gaps are unassessed technical diagrams, unverified proxy PDF/image material, and unverified visual redline semantics. No fourth accession or general missing-evidence category is permitted.
- Every HTTP/body/EOF/framing/hash/identity/deadline/storage/closure failure still fails. Raw retention is mandatory for the union of originals above 64 MiB and exact approved quarantine identities, including smaller variants.
- Keep 600 source seconds, 2,400 shared model seconds, 120 seconds per model call, 3,500 supervisor seconds and 3,600 worker seconds. Keep 512 MiB raw, 16 MiB selected document, 32 MiB extracted text, 4 GiB shared spool, 8 GiB private capture and 24 GiB fresh free space.
- Native acceptance requires the exact three approved identities in the fresh population. Session 2026-10-09 preserves the September 25 through October 9 window. Refuse silent population drift after the latest completed session changes.
- No model call, signal, regime input or prior-comparator use may consume quarantined evidence. A different filing requiring a quarantined prior remains a failure.
- No merge, deployment, production state mutation, or trading run. Keep the previous failed attempts and frozen historical helpers unchanged. Root must obtain fresh resource coordination before any native preparation or launch.

## Task 1: Pure framing-only evidence and acquisition routing

**Owner:** Fresh implementation agent; root and independent reviewer accept.

**Files:** Create `tradingagents/strategies/data_sources/filing_material_policy.py`; modify `filing_parser_dispatch.py`, `filing_acquisition.py`, `edgar_source.py`; create `tests/test_filing_material_policy.py` and `tests/test_filing_material_dispatch.py`. Do not edit hydration, orchestration, existing parser semantics, or private helpers.

**Interfaces:**

```python
POLICY = 'retained_three_material_gaps_v1'
configured(config: dict) -> bool
policy_manifest() -> dict  # fresh canonical identities, URLs, counts, primary names and gap codes
policy_manifest_sha256() -> str
approved_identity(identity: dict) -> dict | None
# identity keys: accession, form, filing_date, source_url
# Unknown accession returns None; a known accession with mismatched identity raises ValueError.
quarantine_evidence(framed: dict, *, submission_size: int,
                    required_exhibits=()) -> dict
validate_quarantined_evidence(evidence: dict) -> dict
```

`quarantine_evidence` consumes only the full native frame after successful EOF, hash and framing. It returns existing original metadata and `document_inventory`, an immutable `material_quarantine` declaration, `structural_status="insufficient"`, `structural_scope="complete_original_inventory_unselected"`, `analysis_adequacy="insufficient"`, `dependency_assessment="not_assessed"`, empty units/dependencies and exact explicit issues. The declaration binds original decoded size, all primary candidates, policy-manifest digest and approved gaps. Required exhibit references must exist unambiguously in the complete inventory; missing bodies never become quarantine permission. Do not use `_validate_evidence` to admit this representation to analysis.

- [x] Add exact-three synthetic native-framing tests using the retained header identities and full document counts; establish failing tests before implementation. Include wrong accession/form/date/URL/FILER/count, duplicate roles, changed issue/declaration, units added, malformed ranges, unknown fourth accession and missing required dependency.
- [x] Implement the pure module without provider/package initialization imports. Return fresh declaration objects so callers cannot mutate the policy.
- [x] Add optional `material_policy=None` through `EDGARSource.get_complete_submission` and acquisition. Only a matched approved identity on the bounded spool route may request the new parser mode; invalid policy/pairing fails before acquisition. Pass no parent file path into the child.
- [x] Extend only the spooled parser request schema with the explicit material policy. Direct-load the pure material module next to `filing_evidence.py`; after `frame_submission`, construct quarantine evidence for exact approved identities. Ordinary files still use strict `select_primary` and selected extraction. Keep exact two-child argv/env/pipes, quota and closed acknowledgement.
- [x] Exercise real two-child acquisition with the synthetic exact-three submissions. Prove Campbell retains both primary candidates, malformed originals still fail, the observation hook runs before framing, normal files are unchanged and no child remains.
- [x] Run focused existing acquisition/parser/evidence tests and return exact hashes. Root alone commits.

## Task 2: Hydration, non-actionable scope, reporting and replay

**Owner:** Root. Begin integration once Task 1's interfaces are published; files are disjoint.

**Files:** Create `tradingagents/strategies/orchestration/filing_material_validation.py`; modify `filing_hydration.py`, `event_monitor.py`, `multi_strategy_engine.py`, `filing_inputs.py`, `filing_acquisition_validation.py`, `scoped_replay.py`, `operational_report.py` and policy-focused tests.

**Interfaces:**

```python
build_material_scope(graph: dict, collections: dict) -> dict
validate_filing_material_policy(data: dict, config: dict) -> dict | None
signal_edgar(original_edgar: dict, summary: dict, *, projected_edgar=None) -> dict
validate_filing_material_health(records, policy_ids, strategy_names, expected_scope) -> None
```

The scope records every original occurrence by collection/index/row hash, evidence hash and disposition, plus canonical quarantine identities/gaps/counts, strict coverage and scoped coverage. It never rewrites original rows or source errors during validation. The signal projection may only remove approved quarantines from already validated input; it removes their corpus content as well as rows and cannot admit new rows.

- [x] Add causal regressions for the exact exception and a fourth ordinary failure. Preserve `coverage.complete=False` and strict failed-row counts for approved gaps; separately compute `scoped_failed_rows`, `scoped_complete` and material counts. Only an independently valid material scope may explain strict incompleteness without an EDGAR error.
- [x] Preserve every occurrence and original `requires_prior` declaration. Mark approved annual-current rows `prior_status="not_assessed_material_quarantine"`; create no comparison binding or prior obligation for their disabled analysis. Another current filing requiring their evidence still fails.
- [x] Thread the configured policy into the monitor and acquisition. Bind it through the existing configuration fingerprint. Reject undeclared, missing, stale or altered material scopes before regime/screening.
- [x] Validate original source data, then apply material exclusion after verified-target projection and before regime, screening and enrichment. Guard direct current/prior/PQC inputs against quarantine as a second boundary.
- [x] Bind the recomputed material summary into every enabled EDGAR-dependent health row, reports and immutable replay. Preserve every unrelated provider error and its data-failure status. Report original acquisition separately from incomplete material interpretation.
- [x] Verify no quarantined model call or candidate, duplicate occurrence/PQC exclusion, fourth-file failure, bad graph/health/cache/replay rejection, ordinary behavior and strict default behavior.

## Task 3: V6 proof and native delivery

**Owner:** Private-harness implementation agents for disjoint helpers, preparer and closed retrieval; root owns integration, independent review, launch approval and execution.

**Files:** Only versioned private V6 helpers/tests under `data/forward-readiness/`; new versioned preparation/streamed retrieval helpers as needed. Never stage private evidence or historical helper edits.

- [ ] Add material policy and canonical scope to V6 config/facts/oracle/public summary/replay. Require exactly the approved three quarantines, all full raw files and zero quarantined admitted/model/committee evidence. Replace blanket completeness only through the validated explicit scoped contract.
- [ ] Retain raw for `size > 64 * 1024**2 or approved_identity(identity) is not None`; use the identical predicate in read-only validation. Preserve fatal pre-parse publication, owner modes/inodes, all capture quotas and source deadline.
- [ ] Preserve immutable replay: verify originals and material scope before baseline, but never reframe or regenerate evidence during replay. Keep all parser/acquisition/model/publication/mutation guards.
- [ ] Test missing/extra/duplicate quarantines, wrong identity/gaps, small missing raw, changed hashes, source failures, altered health/summary, and any quarantined candidate across horizons. Re-run the private suite after root refreshes provisional pins.
- [ ] Root runs the required complete non-live suite and independent review, commits/pushes the topic only and checks CI. Freeze the exact committed runtime and versioned helper hashes into a fresh unapproved concrete contract; verify archive bytes/roster independently.
- [ ] Root coordinates a fresh native window and verifies host/user/new boot, 1,209-file production baseline, native dependencies/SDK authority, session October 9 and fresh 24 GiB space on distinct spool and `/tmp` filesystems. Review the concrete preparer result before the literal launch-approval flag transition.
- [ ] Run one isolated V6 main process, observe closure and resource/deadline facts, preserve all raw/source/model/book evidence, and stream-retrieve the closed scratch with exact 0400-original/0600-other mode rules. No whole multi-GiB file/archive buffering.
- [ ] If main acceptance passes, run the authorized bounded immutable replay, preserve its evidence separately and verify all source files and financial database schemas/rows unchanged. Report actual coverage and every remaining blocker without upgrading failed attempts.

## Root acceptance

Success requires a fresh source freeze within 600 seconds, native analysis under existing bounds, all sixteen committees/staging books, complete immutable replay, exact three raw-backed non-actionable material gaps and unchanged production. Local/CI passing and diagnostic preparation are necessary but do not by themselves meet this goal.
