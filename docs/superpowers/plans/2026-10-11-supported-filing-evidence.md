# Supported filing evidence implementation plan

**Goal:** Complete the user-approved supported-document reliability goal and its isolated operational proof.

**Architecture:** Add pure bounded PDF and ownership-lineage adapters; integrate their independently verifiable receipts with an opt-in disposition policy and existing hydration, model validation and replay. Keep annual comparison and existing exact material exceptions unchanged.

**Tech stack:** Python, existing SEC acquisition/capture, a maintained pure-Python PDF reader selected against primary documentation, pytest and existing subprocess/deadline infrastructure.

**Spec:** `docs/superpowers/specs/2026-10-11-supported-filing-evidence.md` records the approved goal. User autonomy authorizes implementation, review, topic pushes and justified isolated retries without intermediate approval questions.

## Global constraints

- Existing worktree `.worktrees/governed-sip-release`, branch `codex/audit-reliability-fixes`, base `8f69cc90ab854f5290d10eb43360d1b624a1084b`. Root owns Git operations.
- No merge, main push, production changes, service/timer changes, paid API fallback, or budget increases. Future deployment requires a new generation.
- Retain source900/model2400/per-call120 seconds and all existing memory, byte, worker, corpus and spool caps. All evidence and private reports remain local and private.
- Retrieval, truncation, parser, invalid-output and budget failures are failures. No model-only exclusions. Exact citations, issuer identities and no-orders-on-deferred are mandatory.
- Parallel workers own disjoint new modules/tests; root or a separately assigned integrator owns shared files. Review every task and the integrated result. Do not repeat already verified work without a changed boundary or unresolved concern.

## Task 1: Native-text PDF adapter

Own new `tradingagents/strategies/data_sources/filing_pdf.py`, focused `tests/test_filing_pdf.py`, and the minimal PDF dependency declarations in `pyproject.toml` and `requirements.txt`. Do not edit filing_evidence or integration files.

Implement a pure bounded adapter for PDF document bodies, including strict SEC PDF wrapper/UU decoding if needed. Return a JSON-compatible envelope with decoded hash/size, extraction version, complete ordered page inventory, per-page text/hash and quality findings. Enforce caller-supplied byte/text/page limits and deadline callback. Unreadable/malformed/encrypted/empty failed extraction is a technical failure, never a deferred materiality result. Evidence-backed visual/layout limitations may be reported separately. Conservative treatment must not silently drop raster/vector images, tables, rotated or incomplete text. Select the smallest maintained dependency using current official documentation; record exactly how quality checks can and cannot prove faithful extraction.

Use meaningful synthetic valid/malformed/native-wrapped fixtures and, if locally retained, inspect representative captured PDF bodies without network calls. Cover stream limits, every-page inventory, images/graphics, missing text, malformed objects, encoding loss, forms, dependency availability and deadline cancellation. Report the proposed stable call/result interface before root integration. No Git writes or subagents.

## Task 2: Ownership history contracts

Own new `tradingagents/strategies/data_sources/filing_ownership_lineage.py` and focused `tests/test_filing_ownership_lineage.py`. Do not edit hydration, parser, annual policy or model modules.

Implement pure selection and validation contracts usable with existing SEC complete-submission evidence and checked history envelopes. Preserve subject CIK, reporting-person roster and multiple securities; separate base forms from amendments; establish chronology and all necessary chain members. Explicit previous-accession references are candidate evidence, never independent completeness proof. Explicit incorporated agreement references must become bounded acquisition obligations rather than be silently omitted. Design receipt inputs/outputs for root integration and fail closed when inputs cannot prove completeness. Distinguish complete, genuine independently evidenced ambiguity, acquisition-required, and technical failure. Existing annual exact-form comparison semantics must remain untouched.

Read source parsing/history APIs and official SEC schema semantics already captured in private evidence. Add adversarial fixtures for mismatched subject/reporter/security, multiple candidate bases, missing intervening amendments, multi-CUSIP groups, missing dates, chronology and incomplete history. Do not invent a chain from one reporter identifier or accession numbering. Report interfaces and unresolved integration choices. No Git writes or subagents.

## Task 3: Prospective disposition contract

Own new `data_sources/filing_supported_policy.py`, new `data_sources/filing_publisher_facts.py` if justified, and focused tests only. Root owns existing material policy and all integration files.

Use prospective identifier `verified_supported_documents_v1`. Root will make it a new explicitly opted-in material-policy version that retains the exact-ten entries and adds separately validated rules; old THREE/TEN semantics remain unchanged. Do not edit their modules in this task. A self-hashed model assertion or caller boolean is not proof.

Pure evaluate/validate functions should recompute a JSON-compatible disposition from exact current evidence, complete acquisition receipts, optional raw-verified ownership lineage, and complete PDF extraction receipts. Return admitted, deferred, or failed with specific reason codes, complete proof dependencies and manifest digest. The contract must allow simultaneous reasons and preserve residual technical failures. Any technical failure, missing relevant acquisition, exhausted limit or unclosed lineage/material obligation prevents a positive scoped verdict, even if another allowed reason is present. A source-only admitted receipt does not certify successful model analysis.

PDF deferred proof requires complete selected-document/page inventory and successfully processed native content with positive visual/layout observations that cannot faithfully enter text. Parser exceptions, missing/undecoded text, encryption, unsupported encoding/capability gaps, byte/page/stream/text bounds and absent receipts are failures. Every selected document must be accounted for. Do not invent source-dependent materiality; state the exact unsupported selected content and preserve unresolved visual meaning. No generic file-extension exclusion.

Ownership ambiguity must be revalidated through Task 2 using retained original primary bodies and complete history/body inputs. Acquisition-required and technical-failure statuses cannot defer. Do not let a visual current filing exempt its unresolved history obligations. For publisher facts use a narrowly typed, independently recomputable source check; the captured CBL XML with zero aggregate ownership and positive percentage for the same reporting person/security is an initial candidate to assess. Bind exact raw XML paths, values and source identity, and do not generalize arithmetic across dates, denominators, reporters, share classes or option eligibility. Unknown semantic tension remains failed/insufficient unless independently established under a defined check.

Receipts bind policy/version, source identities/hashes, extractor/verifier versions, every required obligation, structured proof facts and exact reasons. Validators recompute from source inputs and reject forged/rehashed proof, model claims, missing acquisitions and failure laundering. Keep original full-coverage false for material deferrals. Report integration interfaces and limitations. No Git writes or subagents.

## Task 4: Integration and analysis repair

Integrate Task 1 into document selection and capture/replay; integrate Task 2 into bounded SEC acquisition and filing prompts without changing annual behavior. Integrate Task 3 through config validation, source reports, candidate admission and replay. Fix proven issuer and citation-generation defects with focused regressions and a deadline-bounded corrective attempt only where necessary. Retain all attempts. Deferred records cannot enter orders; unsupported analyses cannot masquerade as complete.

## Task 5: Review and exact-head verification

Review task diffs and integrated failure semantics. Resolve important findings. Run focused adversarial tests, the full non-live suite and required checks. Commit and push only the topic branch; require exact-head CI success. Record unchanged constraints and source-policy differences in runbook and PR.

### Task 5 verification and constrained regulatory outcome

The final local non-live suite passed 5,860 tests, with four deselected, one existing websockets dependency warning, in 220.10 seconds. The closed log is `data/forward-readiness/supported-documents-full-suite.txt`. Independent review accepted filing changes (186 focused tests, including 25 reviewer cases), raw capture (113 focused tests), and regulatory changes (123 focused tests, including 23 reviewer cases). No reproduced review blocker remained. The exact-current-head CI result is recorded on [PR #50](https://github.com/potalora/eventedge/pull/50) and must pass before a new native freeze. Historical commit `8f69cc9` results of 5,362 local / 5,359 CI passed and three CI skips remain historical.

The implemented regulatory contract requires every rule and attachment. It has no first-three/title fallback. Its narrow inert HTML subset rejects CSS/style, forms, tables, lists and embeds. The regulatory PDF adapter is not wired: an advertised PDF requiring that adapter fails technically. No free-prose issuer bridge or automatic directional regulatory target is supported. Complete source content and a valid no-directional-thesis assessment may produce a journal-only record only with no unresolved facts; missing applicability evidence cannot be relabeled no thesis. These constraints replace any broader planned PDF/issuer capability below as the actual candidate outcome.

No new native workload, raw audit or immutable replay is verified. Original full coverage remains false and scoped acceptance pending. The exact EPA download returned native-host HTTP 403; external document access remains blocked. All-original capture of 7,298,823,168 uncompressed raw bytes cannot fit the unchanged 4 GiB physical spool cap, which includes retained hard-link originals. Compression is unproven and not implemented. Do not raise the cap, omit originals, or launch a blind retry. A new freeze and capture-adapter wiring remain required. Not merged. Not deployed.

## Task 6: Native proof and immutable replay

Re-coordinate VPS exclusivity, verify own-host authentication/entitlement, freeze final source scope/config/code/dependencies and prepare a new isolated run. Complete all four horizons/sixteen books within existing bounds. Validate committee/staging, strict capture, zero-call replay, unchanged production and empty owned workers. Diagnose any failure before a justified retry; retain each failure. Do not treat a concrete external blocker as success.

## Task 7: Deliverable evidence

Update full-system visual HTML and rendered PDF guide, native report, protocol and PR with current complete counts and actual verdicts. Explicitly distinguish original full coverage from supported operational coverage. Final acceptance requires all preceding gates; the goal stays active until done or a qualifying external blocker remains after independent work.

## Task 8: Regulatory content and source-grounded issuer contract

The frozen issuer-failure diagnosis establishes a missing-content defect: the Regulations.gov list schema has no summary field, all fourteen summaries are empty, and one accepted AEP thesis explicitly lacks company attribution. This repair belongs to the approved issuer work in Task 4, and must finish before Tasks 5–7.

Own `data_sources/regulations_source.py`, new `data_sources/regulation_evidence.py` and `data_sources/regulation_assessment.py`, `modules/regulatory_pipeline.py`, and focused new/updated regulation tests. Do not edit shared source_inputs, LLManalyzer, engine, replay or configuration files; root integrates those interfaces.

Add an opt-in `complete_document_text_v1` regulation evidence path using the existing source family. Fetch exact identity-bound document details with include=attachments and every logical main/attachment body through its advertised Regulations download URL. Reconcile included relationships and preserve alternatives. Select verified HTML, UTF-8 text, then supported native-text PDF; never presume formats of different resource IDs interchangeable. No silent slices or dropped attachments. Keep discovery completeness separate from content completeness and retain every failure. Do not permit a regulatory deferral to hide missing source material.

Use the existing inherited source deadline without restarting it, existing bounded response helpers and conservative finite limits no larger than existing comparable source limits: detail 2 MiB, 64 attachment resources, selected body 16 MiB, selected bytes per rule 64 MiB, extracted text per rule 8 MiB. Integrate the current source aggregate 32 MiB read budget conservatively or report a clearly justified existing-cap interpretation before code; do not silently increase an existing budget. Credential headers go only to api.regulations.gov. Advertised downloads require HTTPS downloads.regulations.gov, exact resource-ID path, no credentials/query/fragment/redirects. Other hosts fail explicitly until reviewed.

Pure evidence validation must bind original/detail hashes, identity, all logical units, selection completeness, text hash/version and unresolved material dependencies. Preserve source evidence for replay. Source-only completeness cannot certify model adequacy.

Pure assessment contract must require complete content, exact contiguous citations and typed sufficient/insufficient outcomes. A completed no-directional-thesis result may have no ticker and must not claim no affected company. Directional results require an explicit source-grounded company applicability/exposure bridge plus a target identity in the frozen universe. A model-proposed valid ticker alone is never proof. Unresolved applicability, missing content, bad citations/schema, unverified targets and deadlines stay failures. Provide stable request/result interfaces for root's model/engine integration.

Regression cases must reproduce empty-summary/no-ticker and AEP false acceptance; exercise discovery/detail mismatch, malformed included relationships, missing attachment, arbitrary download URL, failed/truncated body, deadlines, full-text preservation, supported complete no-thesis, insufficient and incorrect company attribution. No tests call live providers or models. Root has retained one supplemental live detail receipt at `data/forward-readiness/regulations-detail-contract-probe-20261011/detail.json` for EPA-R03-OAR-2026-6205-0001; use exact identity/public source shape, not credentials, as fixture evidence. Any further bounded diagnostic must stay explicitly supplemental, never amend old closed capture.
