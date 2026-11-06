# Lossless Original Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Complete each bounded task with its own regression tests; the root agent integrates and accepts the result.

**Goal:** Retain and independently audit every completed original losslessly while accounting for every persistent and transient byte under the existing caps.

**Architecture:** Preserve raw acquisition/parser interfaces. Add owner-issued gzip retention leases and a versioned private capture receipt. Decode one original at a time in a governed read-only audit store, then recheck every persistent file before immutable business replay.

**Tech Stack:** Python >=3.10, standard-library gzip/zlib and descriptor-based filesystem operations; existing pytest suite and native helper boundary.

**Spec:** [Lossless original storage](../specs/2026-10-11-lossless-original-storage.md).

## Global constraints

- 900/2,400/120-second source/model/per-call limits; unchanged 110/120-second replay worker/supervisor.
- 512 MiB originals, 16 MiB selected documents, 32 MiB extracted filing text, 512 MiB selected corpus, two SEC parser processes, 4 GiB physical spool, 8 GiB private capture.
- One prospective `gzip_original_v1` encoding; legacy raw format remains exact. Preserve all completed originals and failure evidence. No sample can certify corpus fit.
- No new dependency, source/model replay call, paid API fallback, merge, deployment, production mutation, cap increase or deadline restart.

## Task 1: Accounted encoded retention

**Files:** `tradingagents/strategies/data_sources/filing_spool.py`, new `tests/test_filing_encoded_spool.py`.

**Interfaces:** Add frozen owner-issued `EncodedOriginalLease(path, record, size, sha256, encoding)`. Add `SpoolScope.retain_encoded(record, destination, *, reserve_external=None)` and `release_encoded(lease)`. The optional external reservation callback runs before each bounded encoded write while the caller already holds the capture lock. Encoding is fixed gzip level 1 with zero timestamp and empty filename; return a lease only after the raw count/hash and closed encoded file are verified.

- [x] Write failing tests for exact raw round trip and simultaneous physical charges, for example:

```python
lease = owner.retain_encoded(record, destination)
assert gzip.decompress(destination.read_bytes()) == original
assert owner.stats()['total_bytes'] == len(original) + lease.size
owner.discard(record)
assert owner.stats()['retained_bytes'] == lease.size
```

- [x] Add capacity/expansion, external reservation, concurrent child-copy, cancellation, partial-write, inode substitution, failed-close/unlink, owner-close and legacy-regression cases.
- [x] Run the new tests to establish failures, implement the smallest owner lifecycle change, and run new tests plus `tests/test_filing_spool.py` and acquisition validation tests.
- [x] Root reviews accounting and cleanup before accepting integration. Do not commit independently.

## Task 2: Bounded original store and raw audit

**Files:** new `tradingagents/strategies/data_sources/filing_original_store.py`, `tradingagents/strategies/orchestration/filing_supported_audit.py`, new `tests/test_filing_original_store.py`, existing raw-audit tests.

**Interfaces:** `RawOriginalStore` and `GzipOriginalStore` own a complete expected-original map and supplied paths/receipts. Both provide `open_original(accession)` as a context manager yielding one read-only byte view, `recheck_persistent_identities()`, and verifiable closed-scratch/physical-peak status. The audit keeps accepting legacy raw-path maps and adapts them through the raw store. The gzip store validates exact original inventory and uses the same inherited `check` callback; its physical limit cannot exceed 4 GiB.

The v2 receipt has exactly `contract`, `identity`, `observed_at`, `size`, `sha256`, `complete`, `encoding`, `stored_name`, `stored_size`, and `stored_sha256`. The fixed values are `contract=sec-original-v2`, `encoding=gzip_original_v1`, and `stored_name=sec-original-{accession}.bin.gz`. The first six source fields must equal the expected complete acquisition record. The persistent receipt basename remains `sec-original-{accession}.json`. Do not allow a mixed-format store or infer the format from filenames.

- [x] Write failing tests showing exact decode and rejection of altered hash/size, truncated/member-concatenated/trailing gzip data, expansion beyond declared size, and a deadline expiring during decode.
- [x] Add tests for full retained-byte baseline plus one live decode, refused concurrent opens, path/ancestor substitution after an earlier file was audited, and scratch close failure retaining its charge.
- [x] Implement bounded descriptor-based stores without changing source evidence requirements. Move common safe path traversal only where needed; preserve all existing independent raw reconstruction checks.
- [x] Verify every store closes scratch before the audit returns and all persistent identities are rechecked after graph/disposition reconstruction. Run original-store, supported-raw-audit and acquisition validation tests.

## Task 3: Private capture/replay integration

**Files:** private `data/forward-readiness/supported-documents-native-helper-draft/bundle/goal_native_originals_v6.py`, supported/source-facts/oracle/replay helpers and their authority/canary/supervisor contract validators; private draft tests and README.

**Interfaces:** Explicit frozen-helper field `original_storage_format=gzip_original_v1` selects `sec-original-v2`. Receipt fields preserve the raw identity/size/hash and add exact encoding, stored basename/size/hash. Logical original totals and physical stored totals are separate. All module bytes and format identity are pinned before launch.

- [x] Add failing private tests for opted-in v2 capture and unchanged raw-v1 behavior, including retention before subsequent parse failure/quarantine and fatal quota/publication errors.
- [x] Wire owner leases to existing quota reservation under capture-lock-before-owner-lock order. Validate all receipts and exact inventory; never use reduced encoded bytes as reduced acquisition coverage.
- [x] Construct the governed gzip store before raw audit; allow only its owned anonymous scratch, preserve retained-evidence mutation and network/model guards, and assert scratch closure before the immutable baseline.
- [x] Update exact source facts, no-use oracle, contract pins and private helper manifest. Independently test forged receipts, missing originals, unapproved formats, altered stored payloads and unsupported-policy imports.

## Task 4: Review, verification and readiness evidence

**Files:** maintained supported-document/source verification docs, private helper review/report, goal acceptance audit and full-system guide.

- [x] Obtain independent adversarial review of owner lifecycle, decoder/audit and private helper gates; repair every reproduced finding.
- [ ] Run `.venv/bin/python -m pytest -q -m 'not live' tests` once integration is stable, then commit/push only the topic branch and require exact-final-head CI success.
- [ ] Preserve prior guide/checkpoint evidence and update the current guide with actual results. Record compression diagnostics only at their actual sample scope.
- [x] Keep native acceptance pending until complete lawful source bytes, full-population physical peaks and whole replay timing are verified under unchanged limits. Do not launch a known-failing full retry or treat source-only diagnostics as operational proof.
