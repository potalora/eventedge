# First-session drought and bounded-document repairs

The October 14 gen_020 attempt retained valid accounting and staging but failed
input coverage. These repairs do not reinterpret its failed evidence as success.

## Drought diagnosis and repair

The retained source envelope recorded an HTTP 200 severity response with one
attempt, zero accepted states and all ten requested states missing:
`Drought Monitor requested states unavailable [invalid_response]`. The former
October 7–14 query excluded the October 6 observation, which was the latest
published weekly map on Wednesday October 14.

USDM observations are dated Tuesday and released Thursday at 08:30 Eastern:
https://droughtmonitor.unl.edu/About/WhatistheUSDM.aspx and
https://drought.unl.edu/Education/Tutorials/usdm/section-six.aspx.

The default adapter window now begins at the most recent published Tuesday,
bounded by the reference day and real acquisition instant. Composite and engine
callers use that shared default. The release boundary uses New York time,
including daylight saving. Explicit windows remain explicit; unsupported
historical vintages, future observations, malformed categories, missing states
and missing newly due weekly observations still fail. Observation dates remain
separate from acquisition/availability timestamps.

The engine also preserves typed `_coverage` failure reason, HTTP status and
attempt count. Previously it discarded these fields, allowing the report to
mislabel an `invalid_response` as `provider_error`.

Earlier tests froze observations inside seven days; their schema-shaped inputs
did not cover the Tuesday/Wednesday publication gap. The new external-I/O-only
regressions fail on the former implementation for severity, composite and engine
paths. Boundary cases cover Thursday before/at release, subsequent weekdays,
standard time, stale responses, explicit windows and vintage constraints.

## Bounded-document diagnosis and prompt repair

Sanitized native responses are retained in
`tests/fixtures/bounded_document_native_20261014.json`. They contain the public
selected-page source identity and response texts; transport/account provenance
and unrelated metadata are omitted. Replaying the actual production validators
produces the same results:

- 1y: `invalid_document_decision_blind_disagreement`; primary total labels omit
  the trailing colon while the blind labels include it.
- 30d and 6m: the blind citations flatten whitespace/newlines and are not
  contiguous substrings of their own transcription. Recognition fails, wrapped
  as `invalid_document_decision_automatic_review`.
- 3m: primary and blind recognition/agreement validate unchanged.

Equal printed counts do not establish matching full associations or literal
citations. Validators remain unchanged, and these native refusals remain tested.
Both visual prompts now explicitly require complete printed label identifiers
and punctuation, and exact citations copied from the model's own transcription,
preserving spaces and JSON-escaped newlines. The blind prompt receives no primary
response, primary hashes or expected claim values. Schema-perfect deterministic
campaign responses could not reveal these native formatting failures.

The prompt change affects output and must enter a new generation. It does not
repair the frozen gen_020 assessments or establish live model reliability.

A second native sample after that clarification still failed. Offline replay
of `repair-native-document-smoke-3/analyzer-attempt.json` proves its blind table
column was `Number of Shares`, while its transcription contained
`Number of\nShares`. Citations, units, printed counts and full row labels were
internally literal; validation correctly failed on the flattened column before
unit agreement. Both prompts now explicitly require row, column and unit fields
to preserve embedded newlines from their own transcription. The sanitized repeat
is retained alongside the original native fixture and must remain refused by
the unchanged parser. No new unit representation rule or tolerance was added.

The subsequent predeclared four-sample campaign (smokes 4–7) finished **2/4**:
4 and 5 passed three native stages; 6 and 7 failed after two stages. Every result
is retained in `tests/fixtures/bounded_document_campaign_20261015.json`. Both
failed blind recognitions were structurally valid; exact raw JSON and its
reserialized form agree, with no duplicate-key or escape defect. The ordinary
blind agreement gate correctly refused both:

- Sample 6 omitted total A/C `row` labels despite printing those full prose
  labels in its transcription and citations.
- Sample 7 retained matching rows but used entire identifier-prefixed row labels
  as primary units and the printed count noun `securities` as blind units. The
  existing conservative unit comparison does not equate those representations.

Both prompts now require printed prose labels to populate `row`, even outside
a table, and explicitly printed unscaled shares/securities counts to use their
literal printed count noun as `unit`, keeping the full label in `row`. This
does not authorize dropping scale/currency meaning or inferring units. Native
refusal regressions continue to use unchanged validators; prompt reliability
requires a separate fixed campaign, with no retry-until-success inference.

The analyzer formerly replaced every validation `ValueError` with generic
`invalid_bounded_document` in its attempt receipt while reraising the original
exception. Attempt diagnostics now preserve only a finite allowlist of fixed
document-decision failure codes; unexpected messages remain generic. Real
analyzer replay of samples 6/7 preserves `invalid_document_decision_blind_disagreement`,
both response receipts and the original exception, and still stops before Sol.
An injected unexpected validator exception confirms arbitrary details never
enter the durable attempt. No validation or classifier gate was relaxed.

The next fixed four-sample campaign (8–11) finished **3/4**: sample 8 failed
primary recognition; samples 9, 10 and 11 passed three stages each. Sample 8
used a spatially padded table grid. Other column headers occurred between
`Number of` and the next-line `Shares`, so its reconstructed column label was
not contiguous in its own transcription. Counts, units, rows and citations were
literal; the column alone failed the unchanged recognition validator. All four
V3 samples are retained alongside the preceding campaign.

The transcription contract now specifies linear table serialization in both
visual prompts: complete header cells from left to right, then complete row
cells in row order, separated by blank lines. Printed cell text and within-cell
newlines remain contiguous; horizontally padded grids and interleaved fragments
are excluded. Probe fields and citations must still copy literal substrings.
The failed sample stays refused. A clearly constructed linear-cell control
demonstrates schema representability only, preserving all printed probe values;
it does not rehabilitate native sample 8 or validate extraction truth.

Native history is cumulative: smoke 1 used the wrong authorization schema and
failed before transport, so it is not extraction evidence. With authentic SIWC,
smoke 2 passed and smoke 3 failed. The fixed campaigns remain 2/4 (4–7) and 3/4
(8–11); passing samples never suppress earlier failures. Any subsequent campaign
must retain its predeclared size and all outcomes.

The fixed linear-table campaign (12–15) also finished **3/4**: 12, 14 and 15
passed three stages; 13 failed primary recognition. Sample 13's copied citation
omitted an intervening printed `N/A` cell. All original request-v2 responses and
outcomes remain retained, and its string citation remains invalid. A constructed
full-page literal citation already passes the existing v2 validator, demonstrating
that full-page citation scope itself is not new authority.

## Versioned page-citation capability

Request `bounded-document-recognition-request-v3` explicitly permits the exact
JSON citation object `{"scope":"page"}`. Only this exact one-key object under
v3, for its own page with transcription at most 4096 characters, resolves to
that unchanged page transcription. The ordinary numeric, literal value, unit,
row, column and citation checks then run. Other scopes, extra keys, long pages,
cross-page substitutions and legacy/unknown request markers fail. Ordinary string
citations retain their strict behavior and are never normalized or repaired.

The raw returned text and response hash retain the object as received. Final
recognition-v1 receipts retain textual citations, and their source/request hashes
bind request v3. Automatic review revalidation reconstructs these textual receipts
without replacing raw-response provenance. The blind request is independently
answer-free and permits the same explicit capability.

Existing request-v2 reviews dispatch to the exact original production blind
prompt (2017 characters; SHA256
`a79591be157bda8db8aad3f1a45b316172ac5f35e4f5b87a48234c968e835847`).
This preserves the actual retained gen_020 3m sealed review. Unknown blind request
versions fail. New capability and revised prompts use request v3; none of the
earlier campaign responses are relabeled or accepted as v3.

The new contract tests showed eight expected failures before implementation.
They cover literal-equivalence, 4096/4097 boundaries, malformed/extra/duplicate
markers, legacy/unknown versions, cross-page and numeric/association failures,
unchanged string refusal, exact raw hashes, real analyzer stages and automatic
review replay. A separate review found no blocking implementation issue. The
226-test focused result establishes isolated contracts, not a new native campaign
or extraction-accuracy guarantee.

## EDGAR and verification boundary

`filing_research_policy=disabled_v1` intentionally stops bulk SEC research before
provider acquisition. Its coverage loss and dependent strategy failures remain
visible; this work does not enable a provider or create a replacement.

Focused pytest evidence is under the private deployment evidence directory:
`drought-release-red.txt`, `drought-release-green.txt`,
`document-prompt-red.txt`, `document-prompt-green.txt`, and
`source-document-focused.txt`. These are isolated tests with external I/O
fixtures; they do not prove live provider/model availability or deployment.
Both new regression modules carry `scheduled_acceptance`, so the named release
gate includes the actual incident contracts and native-response refusals.
The existing `scripts/run_native_document_smoke.py` can independently check the
new prompts using the retained original/page and existing ChatGPT-plan profile,
with a private remapped registry and a new output directory. It makes at most
three bounded model calls, attempts no orders, and leaves validators authoritative.
Its authentication input must be the existing EventEdge-owned SIWC profile
(`~/.config/eventedge/plan-auth.json` by default). Codex's `~/.codex/auth.json`
has a different schema and is not an interchangeable authorization input.

## Candidate profile acceptance

The scheduled harness preserves the original `cfd010be...` fixture and its
baseline run. Its explicit `government_copy_v2` variant applies exactly one
audited policy override, `regulation_retrieval_policy` from v1 to v2, before the
existing disposable path/auth/document-byte substitutions. The candidate
profile digest, normalized fixture digest, and original baseline digest remain
separate. Effective-worker and durable-report checks bind the selected policy.
The sixteen-book test runs both variants through two sessions and an unchanged
repeat, including positive independent earnings decisions and subsequent fills.

That full campaign retains its existing empty regulatory event response. Its v2
variant proves profile propagation and complete, consistent pipeline evidence;
it does not claim nonempty regulatory acquisition or analysis. Separate
scheduled-acceptance tests replay actual retained FDA/FTC detail and GovInfo bytes
through the real v2 source/parser contracts and preserve unsupported material
refusals. The original intentional EDGAR coverage loss remains visible in both
campaign variants.
