# Lossless original storage under the existing resource contract

This is a continuation of the authorized supported-document repair in PR #50. It repairs an implementation gap in complete capture; it does not change source selection, admissibility, strategy, models, risk, production, deadlines or resource caps. The governing requirements remain [supported filing evidence](2026-10-11-supported-filing-evidence.md).

## Observed problem and acceptance boundary

The closed historical population has 3,016 completed-original receipts totaling 7,298,823,168 raw bytes. Retained hardlinks remain charged to the 4 GiB physical spool. Keeping that entire population uncompressed cannot fit. The old capture retained only 15 originals, so its receipts cannot establish the compressibility or replay time of the missing originals.

Use one standard gzip file per completed original, selected prospectively by the frozen helper contract field `original_storage_format=gzip_original_v1`. The legacy raw format remains the default. No codec fallback or population omission is allowed. Compression can expand data and may still fail the fixed resource limits. Fixture and sample results do not prove full-population fit, source coverage or operational acceptance.

## Acquisition ownership and encoding

Keep `CompletedSubmission`, raw acquisition receipts and parser dispatch unchanged. Add an owner-issued encoded retention lease to `filing_spool.py`. At the existing hook before parsing, stream the registered raw inode through standard-library gzip at level 1 with zero modification time and no filename. Independently verify the entire raw size and SHA-256 during encoding. Preserve every completed original, including subsequent parse failures and quarantines.

The raw parent, unfinished encoded output and parser copies all consume physical capacity while they exist. A finalized encoded inode contributes its actual stored bytes to `retained_bytes`; unfinalized encoded output contributes to transient parent bytes. Reserve before each bounded write. Release a charge only after confirmed descriptor closure and removal of the owned inode. Closing the raw parent must not release the encoded lease. Failed cleanup remains charged and fails acceptance.

The private capture quota remains 8 GiB. Charge encoded bytes and receipts before writing, including partial output, using the existing fail-closed capture counter. Keep lock order capture lock then spool-owner lock; never acquire the capture lock from inside an owner-locked callback. Existing physical-stat keys and equations remain unchanged.

## Versioned receipts

The new `sec-original-v2` receipt binds the complete raw identity, observation time, raw size and SHA-256 to the encoding version, exact stored basename, stored size and stored SHA-256. The receipt is immutable after publication. All completed acquisition identities must have exactly one matching receipt and stored payload. Reject missing, duplicate, unexplained or altered artifacts.

Report logical original count/bytes separately from physical retained bytes. Logical coverage must exactly match the acquisition inventory; physical bytes must match the actual files, private-capture counter and spool ledger. A smaller stored size is never a smaller discovery or analysis population.

## Governed replay store

Introduce a small original-store interface with `open_original(accession)` and `recheck_persistent_identities()`. Preserve the legacy raw-path adapter. A gzip adapter receives the complete expected-original inventory and exact versioned storage manifest. It validates every original identity and retains the existing no-symlink traversal, ownership, mode, type, inode and ancestor checks for persistent files and receipts.

The replay physical ledger begins with every persistent encoded byte charged. Reserve the complete expected raw size before creating one anonymous decoded scratch file. Only one original may be decoded at a time. Scratch remains outside retained evidence; close it before releasing its charge and before advancing. No retained evidence is written or replaced. The immutable business-replay baseline is taken only after every scratch descriptor is closed.

Decode with standard-library `zlib.decompressobj(wbits=31)` and bounded output chunks. Enforce encoded size/hash, raw size/hash, gzip checksum and exact end of stream. Reject concatenated members, trailing bytes, truncation, excess expansion, malformed metadata and unsupported encodings. Every loop shares the inherited absolute replay deadline. The existing 110-second worker and 120-second supervisor remain unchanged; no per-original or audit deadline restart is allowed.

The raw audit independently reconstructs source proofs, ownership facts/lineage and dispositions from exact decoded bytes. At the end, reopen and recheck every persistent encoded file and receipt, including ancestor identity. Ephemeral decoded paths cannot stand in for this final tamper check. Network, provider, model and retained-file mutation guards remain active throughout.

Final persistent verification permanently closes decode admission before callbacks or filesystem traversal. Successful audit returns an atomic sealed snapshot with no live or uncertain descriptors, active mappings or scratch reservations; failed verification is terminal. Descriptor owners retain uncertain closes without blindly closing reused descriptor numbers.

The private replay guard blocks scratch descriptor duplication/reopening and writable mappings and owns permitted read-only mappings until confirmed closed. Its final seal independently enumerates live process descriptors by inode, so an alias created through a previously retained low-level entry point cannot escape the closure check. Enumeration/identity/closure uncertainty fails closed. Ordinary business-replay database journals and logs remain writable outside protected evidence.

## Fixed bounds and proof

- Source/model/per-call deadlines: 900/2,400/120 seconds.
- Replay worker/supervisor: 110/120 seconds, one inherited absolute deadline.
- Original: 512 MiB; selected document: 16 MiB; extracted filing text: 32 MiB; selected corpus: 512 MiB.
- SEC parser processes: two; physical spool: 4 GiB; private capture: 8 GiB.
- No source/model calls during immutable replay; no paid API fallback, merge, deployment or production mutation.

Meaningful tests must cover arbitrary-byte round trips, incompressible expansion, raw/output/child accounting, cancellation and partial writes, failed cleanup, malformed receipts, corrupt/truncated/concatenated gzip streams, expansion bombs, all path substitutions, deadline exhaustion, forbidden retained-file writes and unchanged legacy behavior. Independently review the code and private helper integration, then run the full non-live suite and exact-final-commit CI.

Before any positive native verdict, acquire the complete required corpus lawfully, measure actual acquisition peaks and replay's retained-byte-plus-live-decode peak, and prove the whole capture/audit/business replay within existing limits. The EPA publisher HTTP 403 is a separate unresolved access gate. Clearing it alone does not establish capacity or timing readiness.
