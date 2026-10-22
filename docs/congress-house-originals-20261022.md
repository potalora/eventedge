# Congress audit gap and official originals, October 22, 2026

The prepared change preserves Congress as `display_audit_only_v1`: signals,
model context and order authority remain disabled. It requires review and a new
generation before activation. No production code, accepted inputs or incident
history were changed during this investigation.

## Verified gap

The October 21 `gen_023` accepted publisher snapshot was acquired at
`2026-10-21T22:02:07.093919+00:00`. Its revision was
`d4e2142fe0cc4c91af1053c1de37ab17a7bd3ef8`, generated and checked at
`2026-10-21T11:02:41.182Z`. Its native Parquet tables contained 575 filings and
11,702 printed transaction rows. The later official House index contained 39
filings in the September 21–October 21 window, of which the publisher contained
37. Filings `20035687` and `20035691`, both dated October 20, were absent from
the complete publisher tables, rather than lost through window filtering or
EventEdge parsing. Neither publisher extraction nor transaction completeness is
independently established by this audit.

Offline reconstruction of the retained revision metadata, manifest, both Parquet
shards and official index produced the identical accepted snapshot, including
content SHA-256
`89f4662b3d7b44570bcf83b01a0418c6aa7b6878c684d64beac21bcd20ffd09c`
and its incomplete coverage. The preceding publisher revision lacked two
different filings which the October 21 revision subsequently included. This
establishes asynchronous coverage between publisher snapshots and later official
indexes; it does not establish when each official document first became public.

At `2026-10-22T08:14:43Z`, the [publisher revision API](https://huggingface.co/api/datasets/austin-starks/congressional-stock-trades/revision/main)
still returned the same revision. Its [manifest](https://huggingface.co/datasets/austin-starks/congressional-stock-trades/resolve/d4e2142fe0cc4c91af1053c1de37ab17a7bd3ef8/snapshot.json)
declared a 20-hour refresh target and October 19 as the latest House filing date.
The target is an observational freshness signal, separate from the existing
48-hour hard acceptance bound.

## Bounded forward original capture

The optional `house_originals` envelope is separate from the immutable publisher
snapshot. Selection reconstructs its native artifacts before using missing House
IDs to form fixed official PDF URLs. Acquisition shares the existing Congress
deadline, single-attempt audit scope, 108 physical-request cap and 32 MiB total
download cap. Additional original-only limits are eight selected documents,
1 MiB per document and 4 MiB retained PDF bytes in total. It forwards no ambient
authentication and follows no original-document redirects.

Each retained original has its exact bytes encoded as base64, byte count,
SHA-256 and forward acquisition time. Basic PDF header/end framing rejects error
pages and obvious truncation; it does not certify document semantics or extract
transactions. Failed acquisitions and cap omissions remain explicit. A compact
report summary exposes capture counts, bytes, digest and publisher refresh lag
without publishing PDF bodies. Malformed or unbound supplementary evidence is
rejected during audit, continuity and report validation.

A read-only exercise of the final helper against the retained October 21 gap
made two bounded official requests at `2026-10-22T08:29:53Z`, totaling 139,539
bytes. Both returned HTTP 200 and retained `original_only` evidence:

| Official filing | Bytes | SHA-256 |
| --- | ---: | --- |
| [20035687](https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035687.pdf) | 69,331 | `7286068460aee5a6aaf27bd3075291326d9cb80fc7126824aec7e6e66353ede8` |
| [20035691](https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035691.pdf) | 70,208 | `5153274d550bad6f41c23a0d1217a00ef59297e06dc41847f964033bf1baa6bf` |

This diagnostic held bodies in memory and recorded their metadata; it did not
write them into accepted production inputs. Separate pypdf inspection found
one-page originals with readable transaction text and font/NUL artifacts in
headings. The disclosed assets were a private-company SAFE and a Treasury bill.
There is no independently validated official-PDF transaction parser in the
existing Congress adapter, and these observations establish no listed-stock
ticker or trading signal.

Capturing originals leaves publisher rows, raw artifacts, content hash,
reconciliation and the original `invalid_response` House-gap failure unchanged.
The existing continuity warning remains available for a properly validated
recent display-only gap; optional original failures remain visible separately.
No capture closes a canonical source incident or backdates historical
availability.

## Repair evidence qualification

The existing broker exporter requires the healthy preceding-session origin and
every intermediate accepted acquisition, with matching acquisition/session
dates. `gen_023` has accepted inputs for October 20 and 21, without its required
October 19 origin. Its delayed October 20 acquisition also falls on October 21
in New York. Those are evidence qualification failures, distinct from publisher
availability or parsing.

The exporter now identifies `missing_origin_evidence`,
`missing_session_evidence` and `acquisition_session_mismatch` through fixed typed
errors; the CLI emits the safe reason and retains failure exit status. It does
not relax those proof requirements or publish a partial repair bundle. Existing
external brokers must preserve the typed reason if they currently replace all
export failures with a generic `evidence_unavailable` label.

## Verification

Deterministic cases cover verified-ID selection, fixed origin, redirects,
timeouts, HTML/truncated/oversized bodies, request/document/aggregate caps,
hash/time/authority tampering, original publisher replay identity and separate
failure diagnostics. A maximum 4 MiB original corpus passes the Congress
`SourceInputStore` encode/decode limits, continuity validation and source report
projection while preserving all publisher input and incomplete coverage. The
source has an existing explicit 512 MiB/8-million-node codec allowance; this
change increases neither limit.

Run:

```bash
python -m pytest -q tests/test_congress*.py tests/test_source_repair.py tests/test_source_repair_intervention.py
```

The focused combined run passed **371 tests**, with two skips and the existing
websockets deprecation warning. Skips were the absent optional older retained
public-research fixture and a cross-UID permission check requiring a Linux root
publisher. They are separate from the verified retained October 21
reconstruction and the two current official-original observations.
