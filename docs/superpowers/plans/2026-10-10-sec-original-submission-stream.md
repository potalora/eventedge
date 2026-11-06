# SEC Original Submission Stream Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for the independent parser and I/O tasks, followed by root integration and independent review. The existing topic worktree is authoritative; only root commits and pushes.

**Goal:** Acquire and frame oversized original SEC submissions without copying their binary attachments into memory, while preserving the original source deadline and every evidence failure.

**Architecture:** An opt-in acquisition policy writes decoded responses to bounded private files. Two existing pure parser children consume the files through bounded pipes, frame read-only mapped bytes, and materialize only selected text. The diagnostic observer retains original bytes above the old 64 MiB ceiling before parsing; smaller filings keep the existing selected evidence, inventory and original submission hash.

**Tech Stack:** Python standard library file I/O, mmap, hashlib, contextvars and subprocess; existing BeautifulSoup parsing and pytest.

**Spec:** `docs/research/2026-10-10-coverage-runtime-repair-plan.md`; the detailed offline design is retained privately as `data/forward-readiness/resume-sec-original-submission-spool-final-design.md` (SHA256 `cb4674cb3e515b8a610629945506e1c4f037492788dd4d036929d57964202ae3`).

## Global constraints

- Policy name: `filing_acquisition_policy="bounded_original_submission_v1"`, paired with `complete_submission_v1` and `two_processes_v1`. Absence preserves the current 64 MiB bytes path.
- Original shared source deadline remains 600 seconds. No new job clock, retry reset, fallback process or replacement child. Model limits remain 2,400 seconds shared and 120 seconds per call.
- Finite limits: 512 MiB decoded submission; 16 MiB selected document; existing 32 MiB extracted text; shared 4 GiB physical spool; 8 GiB whole private evidence; at least 24 GiB fresh free disk for native launch. The last allowance covers a worst-case duplicate archive and at least 6 GiB headroom.
- Raw retention applies uniformly when decoded size exceeds 64 MiB. Exactly 64 MiB does not qualify. No accession allowlist or automatic raw archive of smaller filings.
- Complete EOF, file/response closure, hash and receipt publication precede framing and primary selection. A parsing failure cannot erase a completed oversized original capture.
- Retained hardlinks share one charged inode. Both child temporary copies count separately. No eviction, truncation, quota wait that cannot progress, or favorable partial acceptance.
- Strict primary selection stays unchanged, including Campbell DEF 14A ambiguity. Framing-only records do not establish selected evidence or model adequacy.
- The material-analysis question is pending. Do not implement quarantine permission, visual adequacy claims, PDF equivalence, new exclusions or weaker evidence checks in this plan.
- No production changes, merge or deployment. Fresh native preparation/launch requires root review of exact frozen artifacts and resource closure; maintenance resource holds must be honored.

## Task 1: Bounded native framing and selected evidence

**Owner:** Fresh parser implementation agent; root and a separate reviewer accept the result.

**Files:** `tradingagents/strategies/data_sources/filing_evidence.py`, new `tests/test_filing_evidence_buffer.py` and narrowly necessary existing parser tests.

**Interfaces:**

```python
frame_submission(raw_buffer, *, expected_accession, expected_form,
                 expected_date, observed_at, max_submission_bytes,
                 max_documents=2000, check=None) -> dict
select_primary(framed, raw_buffer, *, check=None) -> dict
build_evidence_from_buffer(selected_frame, raw_buffer, *, required_exhibits=(),
                           max_document_bytes=16 * 1024 * 1024,
                           max_total_text_bytes=32 * 1024 * 1024,
                           check=None) -> dict
```

`frame_submission` returns existing identity/header/role metadata, body-free `documents` inventories and `primary_candidates` indices. `select_primary` returns a copied frame with `primary_index` and no `primary_candidates` key, or the existing ambiguity error. The buffer is bytes or a read-only mmap. The existing `parse_submission(bytes, ...)` remains a strict compatibility wrapper returning its promised document bodies; the large path never calls this materializing wrapper.

- [x] Add a parity regression using existing synthetic native submissions:

```python
strict = build_evidence(parse_submission(raw, **identity))
with open(path, "wb") as target:
    target.write(raw)
with open(path, "rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
    frame = frame_submission(mapped, **identity, max_submission_bytes=len(raw))
    selected = select_primary(frame, mapped)
    assert build_evidence_from_buffer(selected, mapped) == strict
```

- [x] Run the new test before implementation and preserve the failing result.
- [x] Factor native framing into absolute range operations. Use regex `pos/endpos`, bounded header/metadata copies, bounded hash chunks and bounded whitespace/PDF wrapper scanning. Check the inherited callback during expensive loops. Never slice a binary document into bytes or keep live memoryviews after returning.
- [x] Preserve all existing format/identity/date/role/count/sequence/name/framing checks and original offset/hash values. Selection must use the existing K/Q/8-K PDF rule only.
- [x] Reuse the existing evidence builder through a bounded body-reader or equivalent shared code. Materialize only selected documents within the explicit bound, with no truncation or inventory loss.
- [x] Verify bytes/mmap equality, fake framing tags, malformed EOF/count/name/role metadata, giant binary attachments without proportional allocation, selected limit errors, and Campbell-style two-primary framing followed by strict ambiguity rejection.
- [x] Run existing parser/acquisition/assessment tests and hand off exact hashes. No Git mutation.

## Task 2: Bounded spool acquisition and existing parser IPC

**Owner:** Fresh I/O implementation agent; files are disjoint from Task 1.

**Files:** new `tradingagents/strategies/data_sources/filing_spool.py`, `filing_acquisition.py`, `filing_parser_dispatch.py`, and dedicated spool/IPC tests. Do not modify generic request policy or engine files.

**Interfaces:**

```python
submission_spool_scope(root, *, original_deadline,
                       physical_limit=4 * 1024**3)  # context manager
current_submission_spool()  # active owner or None
spool_response(response, *, identity, max_bytes=512 * 1024**2) -> CompletedSubmission
completed_submission(record) -> None  # default no-op observation seam
dispatch_evidence(record, **arguments) -> dict  # additionally accepts closed receipt
```

The closed receipt exposes owner-generated `path`, `identity` (accession, form, filing date and source URL), `observed_at`, `size`, `sha256`, and `complete=True`. Supply explicit owner methods for retaining a hardlink and reserving/releasing exact child-copy bytes; root must not reach into private counters. Report current/peak retained, transient and total bytes plus completed/failed objects. Send root the precise class/method names before integration.

- [x] Add causal tests for late EOF/close, chunk limit, partial write, quota exhaustion and retention accounting. Establish failures before implementation.
- [x] Own unpredictable O_EXCL 0600 files under a fresh 0700 run directory. Stream decoded chunks of at most 64 KiB, reserve quota before writing, hash incrementally, and close both file and response before publishing completion. Preserve bounded typed source errors without raw provider text.
- [x] In `acquire_complete_submission`, use the new path only within the active scope. Call the completed-source observation seam before any parse; preserve default acquisition and source URL binding. Selected document limit increases only in the opt-in path.
- [x] Preserve exact two-child argv/env/pipes and direct pure-file import. Stream large jobs into anonymous child temporary files, verify size/hash, map read-only, call Task 1 APIs, and close mapping/file before returning the job response. No input path is passed to children.
- [x] Reserve each child copy in the parent shared ledger before transfer. Confirm child closure before releasing bytes. On IPC/deadline/crash failure, close and reap the existing children; never replace or fall back. Invalid filing evidence remains an ordinary rejected filing with its original retained receipt.
- [x] Test real two-child jobs, body/hash/job mismatches, partial pipes, kill/reap, late acceptance, concurrent writers, retained inode charges and exact 64 MiB boundary. Test selected limit separately from raw and result limits.
- [x] Run existing acquisition/dispatcher tests and hand off exact hashes. No Git mutation.

## Task 3: Opt-in scope, raw capture and immutable replay

**Owner:** Root, with a separate independent reviewer. Begin app glue only after Task 2 publishes its exact ownership interface.

**Files:** `event_monitor.py`, `multi_strategy_engine.py`, relevant source configuration/cache binding, dedicated wiring tests; new versioned private V6 observer/evidence/authority/replay/contract helpers. Frozen historical helpers remain unchanged.

- [x] Validate the exact three-policy pairing before acquiring data. Pass the private spool directory from explicit configuration; enter one shared spool scope around the complete EDGAR hydration lifetime, covering all existing acquisition workers and both parser children.
- [x] Bind the acquisition policy into cache/config identities and the frozen graph coverage. Legacy configuration starts no spool. A replay cannot substitute an old smaller-policy observation for the new policy.
- [x] Wrap `completed_submission` in the new native observer. For size >64 MiB, independently check closed regular file/owner/mode/inode/length/hash, reserve whole-capture space, publish a hardlink and immutable receipt, and keep its physical lease. Never encode raw bytes or base64 into JSON captures.
- [x] Require exact capture/hash/identity matches for oversized frozen evidence or its explicit parse-failure record. Missing or altered raw evidence fails capture acceptance. Record quota high-watermarks and child closure. Keep acquisition failures visible.
- [x] Protect original raw files and receipts during replay; make new acquisition/spool/publication entrypoints fatal. Replay may stream hashes but may not reframe, fetch, start parsers or mutate any protected artifact or financial database.
- [x] Update the concrete V6 contract with 4/8/24 GiB limits and current helper/runtime hashes only after root review. Confirm native `pyarrow` availability read-only; never install into the production environment to satisfy this diagnostic.
- [x] Run the focused integration/harness suite, then the required full non-live suite and independent review. Final results: 4,830 non-live application tests; 186 separate V6 tests; 114 independent review tests.
- [ ] Commit/push only the existing topic branch and check CI. Fresh native acceptance remains separate.

## Acceptance and rulings

Ruling: use original complete-submission framing, not synthetic SGML or a new separately assembled filing format. This preserves existing provenance; a framing defect still requires repair rather than accepting an invented equivalent.

Ruling: retain only oversized original bodies, alongside the existing evidence for smaller filings. An all-original archive would exceed 2 GiB even for the old incomplete corpus and is outside the smallest required repair.

Ruling: use finite 512 MiB / 16 MiB / 4 GiB / 8 GiB / 24 GiB resource bounds with unchanged time limits. These cover known individual sizes and reserve archive headroom; they do not promise that every future population fits. Exceeding a bound fails visibly.

Success for this plan means verified bounded acquisition/framing and immutable provenance. It does not answer the pending material-analysis decision or establish the full sixteen-book proof.
