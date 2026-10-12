# Retained-document Luna pilot

The approved next-generation pilot now has an opt-in application attachment path, a fixed retained-document sample, independent probe validation and read-only replay. Both actual ChatGPT-plan calls completed. **Neither extraction passed the frozen quality contract.** The existing v4 candidate still has OCR/vision disabled; this work gives no analysis, order, source-recovery or deployment authority. The earlier resilience milestone remains closed at its separately recorded commit.

## Application interface

`PlanDocumentInput` requires explicit PNG/PDF bytes, a safe filename and an independently supplied SHA256. The parent and disposable worker validate format signatures, exact hashes, canonical base64 and finite bounds: 4 MiB per input, 8 MiB total, eight inputs. There is no URL, file-ID, path-fetch or truncation fallback. These are transport-integrity checks, not PDF validity or completeness checks.

`call_analysis_model(..., document_inputs=[...])` accepts attachments only with the native `ChatGPTPlanClient` and `gpt-6-luna`. Native profile binding, paid-environment removal, zero SDK retries, no redirects, the strict completed Responses stream and existing model deadlines remain in force. Ordinary text requests retain their prior shape. Successful provenance records ordered input hashes, formats, filenames and sizes without document content. No existing research caller or configuration enables this optional interface.

`luna_document_pilot.py` independently compares requested page coverage, printed numeric literals, units, row/column associations and source-linked support against gold read before inference. Unknown fields, invalid schema, missing/duplicate pages or probes, bad citations and numeric-boundary corruption fail. Model uncertainty remains unresolved only after schema validation. Blank-cell checks are deliberately limited to independently verified pages containing no numeric values; mixed populated/blank tables are unsupported. A passing sample receipt would still leave complete transcription, unselected pages and whole-document quality unverified.

## Fixed sample and prospective bounds

All inputs came from retained originals; there were no new source downloads or bulk filing runs. The identity inventory includes complete submission hashes, exact encoded body ranges/hashes, native decoding provenance, complete PDF hashes, page inventories and the previous parser failures. Four selected pages were rendered and visually inspected before root independently authored ten probes and support passages.

| Retained original | Complete PDF SHA256 | Inventory and selected pages | Purpose |
| --- | --- | --- | --- |
| `0001193125-26-409540/ck0000000000-ex99_1.pdf` | `9315ae6e9bd08695efd5b8633fd9566dc0edd5c34702420d8b488c260331e561` | 23 pages; pages 3 and 23 selected | Shares, subscription amount, per-share price and prior holdings. The complete original PDF was sent. |
| `0001193125-26-409540/ck0000000000-ex99_2.pdf` | `c18369ed34b294f2d9ab611d7d3218dcbd3991778135b5cae74d77f989044201` | 32 pages; pages 6 and 28 selected | Three percentage/rate probes and three blank shareholder-table cells. The two selected PNGs were sent. |
| `0001772351-26-000012/ex994.pdf` | `cf4a5952c0bcefc3255fc174e4fd0bbc4aa991e448ef1f00c7813ac52780ed7a` | One page; failure control | Known malformed embedded font; failed preflight, zero model calls. Readability cannot establish recovery or remove the existing whole-filing exclusion. |

The two multi-page exhibits still have their recorded native/structural capability failures. A visual probe cannot clear those failures. This small sample contains prose numbers and a blank table, not a populated numeric matrix or broad issuer/scan coverage.

The executed preparation was frozen before either call under SHA256 `36992c395f49ce3cfe3d15359ea54544d15ee071bf1b750539e380d406d468f0`. It fixes gold, prompts, complete originals, rendered inputs, wire requests, code hashes, `gpt-6-luna`/high and one attempt per document. Earlier preparations were superseded before execution and remain retained. Each call had a 110-second model allowance inside a 120-second outer wall limit, 2 GiB sampled aggregate RSS, twelve processes, 64 KiB console and three-second cleanup bounds. No existing budget was increased. RSS/process enforcement is the existing sampled local watchdog, not a claimed kernel memory limit.

## Actual results

| Input | Native transport | Frozen extraction verdict | Observed cause |
| --- | --- | --- | --- |
| Complete 23-page PDF | Completed in 34.85 seconds; native Luna/high, account-bound ChatGPT plan | **Failed** | Four printed numeric literals matched, but `United States dollars` and `Shares of Common Stock` were omitted from two unit fields. |
| Two PNG pages from the 32-page original | Completed in 31.09 seconds; native Luna/high, account-bound ChatGPT plan | **Failed** | `10.0%` and `1.0%` were split into numeric strings plus separate percent units, contrary to the frozen literal-preservation criterion. Three numeric citations omitted required surrounding support; all three blank-cell citations were null instead of quoting their headers. |
| Malformed-font control | Not called | **Failed preflight** | Existing source defect preserved. |

All four requested page identities were returned. Seven numerical magnitudes and the three blank cells were recognized; that diagnostic observation does not override failed unit, literal or citation checks. Some failures expose prompt/schema alignment limits rather than demonstrated wrong digits. The complete page transcriptions have not been independently word-checked. No quality or source-coverage pass is claimed.

Both quality failures intentionally returned worker exit code one; supervisor receipts retain `worker_failed`, unchanged inputs and zero remaining PIDs. Peak ownership was two processes; neither call approached the 2 GiB watchdog. There were exactly two model calls and no retry. The returned model alias had no immutable revision, so provenance remains unpinned.

## Verification and replay

The local evidence index is `data/forward-readiness/luna-retained-pilot-20261012/`. Its `native-integrated/` tree retains preparation, original PDFs, four PNGs, gold/specs, exact credential-free wire requests, raw completed text, native provenance, failure receipts, capture manifests, supervision, replay results and executed-code bytes verified against the pre-call hashes. The two later replay/depth repairs have offline coverage; no further model call is claimed at the published commit.

Independent review reproduced and repaired wrong-row citations, numeric-prefix matching, fabricated blank values, uncertainty bypassing schema and transport-rejected text being promoted by replay. Each repair has a failing-then-passing regression. There are 63 new attachment cases and 31 pilot/replay cases. The wider existing plan/model/deadline selection passed 263 tests before the final replay-only repair; the final focused selection and exact-commit CI are recorded on [PR #50](https://github.com/potalora/eventedge/pull/50).

Read-only replay requires a separately retained capture-manifest hash, verifies every captured artifact plus original/render identities, and recomputes the extraction receipt without importing provider clients. Native transport rejection remains failed even if retained raw text appears readable. Both actual failed receipts replayed identically after the final replay/depth repairs, with zero calls and unchanged files:

- PDF capture: `decc0c13d3f111edbe39fb714f5d9554583d0202abedfac46600d33185d1119f`.
- Image capture: `b54d188b23c6a2b1a18f796fce11b3bb2f1b5efe9784902922a285098baa731b`.

Replay proves anchored artifact integrity and deterministic validation. It does not independently prove provider execution, render derivation or gold accuracy; the retained native-call, source-identity and visual-review evidence address those separately within this limited pilot.

## Remaining gate

Before another prospective pilot, align the request and schema explicitly: require printed units from prose as well as headings, specify whether percent/currency symbols repeat in the value field, and require sufficient row/column context or blank-table headers in citations. Preserve these failures. Freeze any revised policy/prompt and its finite attempt count before new calls. A populated numeric table, wider coverage and independent complete-page checks remain necessary before any broader extraction-quality claim or next-generation semantic integration. No research acquisition or generation was resumed, no analysis/trading path was enabled, and nothing was merged or deployed.
