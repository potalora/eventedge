# Linked regulatory publication qualification

The candidate qualifies complete material for three explicitly named public
documents. It does not enable regulatory research, change a generation/profile,
run models or change production. Production was mounted read only during the
authenticated native check; all captures and diagnostic code were temporary.

## Native result and independent replay

The final check acquired **3/3 named roots**, four distinct publication nodes,
2,503,036 raw response bytes and all 34 original SEC graphics. The source byte
limit was 33,554,432 bytes; no bytes were reserved or budget exhausted. This is a
complete **named-document scope**, not exhaustive recent or marketwide discovery.
The older CFPB document does not make the October 7–21 window nonempty.

| Discovery root | Complete dependency material | Consumer preparation | Remaining requirement |
| --- | --- | --- | --- |
| `SEC-2026-6502-0001`, October 20 correction `C1-2026-20360` | Separate correction and exact October 5 original `2026-20360` / `SEC-2026-6187-0001`; 83 original publication pages and 34 validated original-size PNGs | Nine base recognition requests cover all 34 actual assets; every request has a retained hash | Actual recognition, independent original-image review and model-dependent continuation context have not run. Final assessment request remains blocked |
| `EPA-HQ-OW-1998-9208-0001`, October 21 repost | Verified March 11, 1997 original `97-5879`, pages 11270–11281 plus retained part-cover typography; catalogue PDF 403 retained | Complete text assessment request prepared; publication and repost identities remain separate | No semantic model assessment or verified issuer applicability. Historical repost has no fresh-rule execution authority |
| `CFPB-2026-0018-0001`, July 9 request for information | Verified official original `2026-13834`, all declared primary/attachment material | Complete text assessment request prepared, with its RFI action retained | No semantic model assessment or verified issuer applicability; no finalized-rule claim |

The sanitized result, actual availability clocks, graph/bundle identities,
qualification code identities and every prepared request hash are retained in
[`2026-10-22-linked-publications-native.json`](2026-10-22-linked-publications-native.json).
`semantic_assessment_complete` is **false**. The diagnostic made **zero model
calls** and did not create recognized/reviewed graphic receipts.

The exact frozen public-material capture is outside Git, mode `0600`:

```text
/var/tmp/eventedge-linked-runtime-20261022/output/publications-capture-review.json
SHA256 fc0f81950622fd26569fce0c5e078496d6977542af3710d7857aebfb2dec3085
bytes 7918428
```

The primary agent independently replayed the current candidate under
`unshare --net`. After removing only `checked_at` and `replay_only`, its result
equaled the final native summary exactly, including graph, code, original and
prepared-request identities. Its separate receipt is
`/var/tmp/eventedge-linked-runtime-20261022/root-offline-replay-final.json`.
The independent reviewer also reran the exact adversarial reproductions with
the current code and found the reported defects closed.

## Contract and failure behavior

`linked_publications_v1` is a separate prospective opt-in requiring
`complete_document_text_v1`, `verified_government_copy_v4` and the prospective
decision clock. Full engine configuration requires paper mode with execution
disabled. An absent opt-in retains the old acquisition and replay path; legacy
v1–v3 parser behavior and guards are unchanged. Existing reduced operating
profiles continue to exclude regulatory research.

The new source graph retains one bundle per actual discovery and deduplicates
publication nodes across original/correction roots. A two-root regression now
captures two nodes and fetches 34 images once, rather than four nodes and 68
image requests. Each bundle has its own closing relationship receipt and seal.
The validator independently rejects multiple renditions of one FR identity in
the same source graph.

The original/correction relationship is bound by fresh, reciprocal Federal
Register metadata, exact Regulations.gov document identities and the official
correction's original-number/date/page reference. CFR, release/docket and RIN
authority must agree. Closure is limited to eight publication nodes per root
and 256 per source, with the inherited provider deadline and common source byte
budget. Missing, inconsistent, unsupported or over-limit material remains an
explicit failed/incomplete source; it is not silently truncated.

Every supplied attachment and graphic must pass its original resource inventory.
Replay reextracts every retained body and validates every image, rather than
trusting a digest or former complete flag. Complete native discovery is also
replayed from retained raw publisher pages, including pagination, ordering,
window boundaries and exact selected rows. Named qualification retains its
original detail responses and exact named population. Omitting a discovered
root cannot preserve a complete source by adjusting counts and rehashing.

Publication dates identify publications, not acquisition availability. The
current GovInfo original already contains the corrected December 4 deadline.
The graph therefore labels it a publisher rendition acquired forward, **not
historical as-published text**. Closing relationship capture must follow all
dependency captures; bundle availability cannot precede that capture or seal.
Future selection windows and replay clocks inconsistent with retained
discovery acquisition are rejected. A deadline at final publication retains a
bound failed-publication marker; raw material completeness does not turn that
failed acquisition into usable input.

The archived EPA node preserves the catalogue's `97–5879` and records its narrow
normalization to the official `97-5879` identity. Its 1997 publication, printed
FRL/CFR authority and 2026 catalogue docket/posting are separate facts. No PDF
parser or invented 2026 original-rule date is used. The retained official
metadata includes its anomalous cover token `Rule`; it is preserved as raw
metadata and is not treated as an additional agency.

Special-form support is deliberately tied to the demonstrated SEC single DATES
correction and archived EPA part-cover form. Other correction directives,
archived layouts, PDFs or unaccounted material remain unsupported until their
originals and complete grammar are qualified. Bracketed omitted-material
markers, including lowercase equation/formula/TIFF forms, are refused.

## Reproduce without trading or model calls

With an existing Regulations.gov key supplied privately through the environment,
acquire a fresh named diagnostic to a new temporary capture:

```bash
python scripts/qualify_regulatory_publications.py \
  --document-id SEC-2026-6502-0001 \
  --document-id EPA-HQ-OW-1998-9208-0001 \
  --document-id CFPB-2026-0018-0001 \
  --as-of 2026-10-22 --budget-seconds 180 \
  --capture /var/tmp/new-publications-capture.json
```

The capture is created with `0600`, `O_EXCL` and `O_NOFOLLOW`; production capture
destinations are forbidden. This is not an accepted daily input. Replay reads a
bounded file, requires its expected SHA-256, binds the recorded policy and exact
qualification/codec/consumer code, and performs no network or model calls:

```bash
python scripts/qualify_regulatory_publications.py \
  --replay /var/tmp/eventedge-linked-runtime-20261022/output/publications-capture-review.json \
  --expected-capture-sha256 fc0f81950622fd26569fce0c5e078496d6977542af3710d7857aebfb2dec3085
```

## Regression evidence

New positive and negative tests were exercised red before repair, including the
independent review reproductions. The final linked-module/policy selection
passed **84 tests**. The independent reviewer separately passed the same 84
checks and confirmed two-root deduplication, raw-discovery population binding,
omitted-material refusal, metadata authority reconciliation and closing clocks.
An earlier broader legacy evidence/source/assessment/graphics/replay selection
passed 260 tests; another integration/CFPB/graphics selection passed 160 tests.
Final combined branch acceptance is recorded by the primary agent separately.

The new raw fixture manifest is
`tests/fixtures/regulation_linked_20261022_manifest.json`: six unaltered official
responses with URL, byte count, digest and retention clock. Those fixture clocks
are never substituted for publisher dates or real native acquisition clocks.
