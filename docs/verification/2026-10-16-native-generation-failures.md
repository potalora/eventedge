# Native generation 22 failures and regression coverage

The native `trade.service` invocation for the October 15 reference session
finished on October 16 at 04:13:58 UTC. This was an unsuccessful trading
workflow despite the service's zero exit status. All sixteen books completed
accounting; only the four 30-day books completed staging. The original source
snapshots, model responses, daily attempt, reports, controller journal and
generation remain unchanged. Intake timers and paths remain held during repair.

The deployed application was `df78a7acdbc1e04a09a40a6222e72939a0599993` with
profile SHA256 `e0bd0ebc1087e82a533e2d33968ab1984c1a5b584257818df4f32d4e674556cc`.
The daily attempt SHA256 is
`74b0f4fae9976bbfc5ddfdfea61bb379004f26ef401a4cbd6982b79252d26269`;
the operational report SHA256 is
`fa6d5e982fe2739457c342ddf30500c7e4eac618b8268dc8efa0e04969b1e078`.
These identify retained native evidence, not constructed tests.

## What the native run established

- All fourteen non-EDGAR source acquisitions succeeded. EDGAR remained explicitly
  disabled. Acquisition success did not establish successful document analysis.
- All sixteen accounting ledgers had valid snapshots and all ten accounting
  phases completed. Twelve staging paths raised
  `ValueError: signal evidence contains a non-finite float` before a committee
  result was stored.
- The failing commodity candidates carried missing FRED observations as `NaN` in
  `metadata.macro_evidence`: CPI on October 1, 2025 and VIX on April 18 and
  December 25, 2025 and January 1 and April 3, 2026. The canonical signal writer
  correctly refused those non-finite values. The strategy had already excluded
  missing observations from its numerical calculations but had not represented
  their missingness safely in its evidence payload.
- The controller's early source-degradation branch returned
  `wait_next_session / accepted_outcome` even though the retained daily attempt
  exited 1, the operational report said `outcome=failed`, and staging was
  incomplete. This masked the failure at the service boundary.
- Two bounded-document reviews completed before midnight New York time. Two
  later reviews were refused by the legacy same-day review policy. The
  declaration's expiry was a separate timestamp; expiry had not caused these
  failures. A future overnight policy must be explicit and cannot reinterpret
  these failed receipts.
- Regulatory analysis produced thirty-six retained Sol responses: three
  sufficient no-directional-thesis assessments and thirty-three `insufficient`
  assessments. The latter cited missing incorporated materials, unproved issuer
  applicability, or both. Those are preserved abstentions, not successful
  directional research. Twelve other assessments stopped at image recognition:
  eight incomplete, two literal mismatches, one schema mismatch and one identity
  mismatch. Only three recognition/review bundles completed; the acquisition of
  ninety-eight original graphics was not evidence that all graphics were read.

The independent read-only structural audit passed 134 of 161 checks and failed
the remaining 27. It observed no change to authoritative input bytes. Its
SHA256 is `ebec3cc73e5e10fa34cc8ed2f95cde31c84e47b7898a1b3faf5063f2a3c5383a`.
Full semantic acceptance was not granted. Separate preservation checks confirmed
the previous generation's state and eight pending intents were unchanged.

## Why prior checks missed this

The provider probes established acquisition and retention. Constructed workflow
tests used clean FRED observations and a central COT position that produced no
commodity candidate. They therefore missed the path from a legitimate missing
observation through an actual commodity candidate into signal persistence.

Scheduler fixtures called source-degraded samples complete without explicitly
providing completed-staging evidence. That allowed the faulty early acceptance
branch to appear correct. Service exit status also cannot substitute for the
daily attempt, report and ledger results.

The first overnight document tests used foundation policy IDs per horizon.
Adding the actual frozen-profile policy-ID shape exposed a separate completed
replay defect: committee lookups used health policy IDs with a horizon suffix,
while the ledger stored committees under the configured paper-ledger policy ID.
This was found in constructed verification, not inferred as the cause of the
native staging crash.

Small synthetic graphics established schema and replay checks but did not prove
the model could enumerate dense production tables, footnotes and continuations.
Fresh publisher XML/HTML subsequently corroborated the original graphic IDs for
the investigated rule. Neighboring publisher prose described Table 2 where an
image still contained Table 1 continuation notes. That is not authority to move
images or infer semantic associations from position alone.

## Repairs under verification

Commodity evidence now preserves missing observations as explicit nulls with
their original dates. It does not replace missing numbers with zero, drop their
dates, relax canonical finite-number checks, or change macro availability and
veto calculations. The new regression first reproduced the same twelve-book
staging failure. After the change, its constructed sixteen-book workflow and
frozen replay completed; no provider or model calls occurred during replay.
The focused commodity and strategy suite passed 78 tests.

The new `verified_scoped_claims_next_open_v2` document policy permits review
after midnight only before the real next exchange open. It binds the source
envelope, generation, frozen profile, registry, enrichment and final accepted
decision inputs before committee effects. Expiry is checked against that final
clock. Historical policy and receipt interpretation remain unchanged.

The controller now gives an explicit failed report precedence over source
degradation and requires completed staging for acceptance. The prospective
native entry point requires a fresh, unused generation and a real pre-open
window under the existing runtime lease. Its exclusive intent prevents a
second start after an uncertain outcome. The controller and daily pipeline
read committee records using the actual paper-ledger policy ID. Focused tests
for these repairs and commodity evidence passed 182 cases. Thirteen regression
cases were additionally selected by the scheduled-workflow CI marker.

The prospective `recorded_abstention_v1` regulatory assessment policy separates
a completed no-trade abstention from a supported no-thesis finding. Pedro
explicitly chose to count valid abstentions as completed while keeping evidence
gaps visible. This covers unavailable external supporting material and unproved
issuer exposure after all supplied source content has been reviewed. It does
not cover missing primary content, failed declared attachment acquisition or
incomplete image recognition/review. The policy retains unresolved dependencies,
exact source citations and the supplied material proof, with no order authority
or sufficient-issuer-evidence claim.
The report distinguishes assessment completion from decision-evidence
completion and withholds performance claims when the latter is unresolved.
Constructed 16-book staging, reporting and read-only replay cover this policy;
the combined regulatory and document test selection passed 684 cases. This
does not relabel the 33 historical insufficient responses.

Two separate one-target native diagnostics remained failed: one violated the
numeric-literal precision contract, and the corrected prompt returned partial
coverage when table footnotes appeared only in an adjacent context image.
Independent review was correctly skipped. Both original responses remain
preserved. These probes used an earlier diagnostic contract and do not establish
the behavior of the prospective implementation.

The prospective graphics policy requests one target original at a time, with
up to six separately identified neighboring originals as context. Target
coverage and context-association closure are separate checks. Every cross-image
literal must identify its original, exact local target anchors and visible
association, and an independent image-backed review checks each claim. Context
images earn no target-coverage credit: every one of the 98 originals still
requires its own recognition and review. The runtime permits eight concurrent
model stages globally and four targets per document within the unchanged
absolute phase deadline. Dense publisher tables and cross-image footnotes must
pass native acceptance; constructed coverage tests alone cannot establish model
accuracy or capacity.

The existing scheduled-workflow selection passed 322 tests in 17 minutes.
That run began before the latest graphics and abstention additions; it is not
the final candidate's full CI result.

A subsequent sealed probe used the actual prospective V6 contract and original
EP06OC26.001 with its four bound context originals. Its successful transport
returned 8,964 bytes after 68.70 seconds, but its 27 structured elements contained
no cells despite printing 45 numeric table values in its transcript. It also
omitted the neighboring e/f/g footnote associations. The validator rejected
`invalid_regulation_graphics_table_links`; independent review was not called.
The original response SHA256 is
`b7156302c2c5d02c48979532b42f7454cc03da6c636edbdc57b1295f283969d4`.
This is a model-output completeness failure, not demonstrated transport
truncation. Its assertion of complete coverage is not accepted as evidence.

The next sealed probe used the prospective V7 grid contract against those same
originals. Luna returned 884 bytes after 23.98 seconds: explicit partial coverage,
empty material lists and unresolved footnote associations. Its original response
SHA256 is `a043797ea149fa18b13049035259028a733160df380f0e48800aec79f2b300fb`.
The validator rejected `invalid_regulation_graphics_incomplete`, and independent
review was not called. Direct image inspection shows the table and e/f/g note
continuation are legible; source acquisition is not the apparent cause. The grid
candidate has not established native completion and is not a deployable repair.

A separate compact-response diagnostic kept those same five originals, their
order, Luna high and the native transport fixed. It returned 5,029 bytes after
48.13 seconds, with response SHA256
`2a54a3a41d641a51f3a9eb975510f776cbda58c9246ec2d5128a375943b2df19`.
An independent comparison found all 45 numeric strings in their correct cells,
but an omitted word in note d, flattened header lines, invalid region dimensions
and note coordinates, a malformed echoed request hash, and missing e/f/g notes.
The response explicitly declared partial coverage and unresolved context; it
remains failed and did not reach independent native review. Its claim that the
supplied context originals lacked those notes is contradicted by the retained
third image. The simpler representation improved transcription, but did not
establish a complete reader.

An offline check followed the frozen caller, worker and real SDK serialization
with an in-memory HTTP transport. All five high-detail image blocks preserved
their exact original bytes and order, including the third image containing
e/f/g. Attachment requests also bypass response reuse. This rules out image
dropping in that exercised local serialization path; it does not prove what the
server decoded or the model attended to during the native attempt.

A subsequent directed probe kept the five originals and their order fixed and
asked only for a plain transcription of the third image. It returned 2,258 bytes
after 34.19 seconds, SHA256
`9e5e33eb0b8b3ff48c242050be10d0afde7b6a9d75341e64895a86ed8c5ebc0d`.
The response transcribed Table 2 from the fourth original, EP06OC26.003, rather
than the three notes in the requested third original, EP06OC26.002. This is an
observed image-selection failure even without the structured extraction schema.
It does not establish server-side image loss.

The paired labelled diagnostic changed only the private input-content builder
to place a deterministic filename and one-based ordinal immediately before each
image. All image bytes, order, model, profile and transcription instructions
remained identical. The real SDK's serialized body was checked offline before
the call. Luna then returned the requested third original's e/f/g notes in
11.24 seconds: 760 bytes, SHA256
`5228129ddfaa3d74df3631366130394b8ec941a7164a72958915814f144e46a8`.
This provides native evidence for explicit image labels in the successor
contract. It does not establish complete dense-table recognition, independent
review or full-corpus acceptance. Both diagnostic outputs remain unmodified.

The largest rule's complete source plus fifty minimal constructed graphic
units measures 374,403 tokens with `tiktoken==0.12.0` and `o200k_base`. This is an
explicit planning surrogate, not a verified model-specific tokenization or
entitlement. The chosen 475,000-token planning limit leaves only 100,597
tokens for expansion into actual complete graphic material and provider
framing. Native graphic outputs must be counted before assuming that Sol can
receive the assembled document unchanged.

The prospective compact contract stores each printed occurrence once in a matrix,
standalone text block or note body. Named coordinates and explicit inclusive
regions describe merged cells, headers, units and note associations. Runtime
receipts bind original identities and hashes; the model no longer has to copy
hashes or invent mechanical IDs. Only the explicit new policy adds the verified
filename/ordinal labels before each original. Legacy request bytes remain unchanged.
Original-image inspection identified hierarchical
headers, headerless continuation tables, mixed forms and a body cell split
between images. Those cases now drive source-format fixtures. A split cell
retains its external fragments separately and requires an image-bound association;
it cannot manufacture a single printed quote by concatenation. The finite parser
and independent image review must both accept the result before it reaches Sol.

One bounded correction allowance can replace invalid recognition, replace a
candidate rejected by independent review, or repeat a structurally malformed
review against the unchanged valid candidate. It cannot retry authorization,
transport, integrity, source-drift or deadline failures. Every raw failure and
replacement remains in the replayed chain; a final review receives only the
current candidate and originals. Identical source work shares one complete
success or failure partition within the invocation, preventing four horizons
from independently restarting the same correction cycle. The unchanged absolute
deadline includes waiting and correction work.

The earlier tests established parser and replay behavior using constructed
responses, but did not test whether the native model selected the intended image
or could produce the full response format. The paired original-image probes
exposed that gap. The successor must pass the actual dense table and difficult
continuation/chart/form examples before full CI and a fresh native generation;
constructed tests remain useful for refusal, retention and replay guarantees,
and are reported separately from model accuracy.

The first full production-contract V8 probe made two native recognition calls
in 91.42 seconds and exhausted its single correction allowance. The first raw
response was malformed JSON; the replacement used three empty strings for blank
cells and fifteen string locations where the schema required nulls and objects.
Both failed stages remain preserved and replayable; no independent review ran.
The replacement response SHA256 is
`07891fe6ec72359b49d59a132c1d24b4f2f12892ebbf636b8ce9e17983cce4dd`.
Independent image comparison found all 45 numeric strings correct, but also a
missing word in note d, an invented heading and incomplete row hierarchy. Numeric
agreement therefore did not establish complete content. The V9 request replaces
ambiguous schema shorthand with literal JSON examples and explicit blank,
location and hierarchy instructions. Structural and semantic acceptance remain
unchanged, and the V8 failures are not reinterpreted as passes.

The prospective prompt policy measures the final assembled system and user
text using a hash-pinned bundled vocabulary. Its 475,000-token limit reserves
25,000 of the 500,000-token planning assumption; it does not prove transport
entitlement. Missing tokenizer support, altered vocabulary, excess bytes or an
exceeded planning limit preserve a failed proof and prevent dispatch. The source
text is never truncated to fit. The existing 119 installed distributions are
pinned; the new environment adds only `tiktoken==0.12.0` and `regex==2026.9.29`.
A separate environment is prepared before cutover, and both environments' regular
non-bytecode files are checked before activation and intake restoration.

These changes require a fresh generation, full CI, native workflow verification
and truthful reporting of remaining evidence limitations before production
acceptance. Those pending steps have not passed.
