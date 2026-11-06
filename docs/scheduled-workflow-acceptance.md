# Scheduled workflow acceptance

The 2026-10-14 midday failure exposed a gap between independently tested pieces:
the shell test substituted Python, preflight tests substituted shared acquisition,
and prospective integration tests started after a completed market session. None
combined the frozen production policy with the midday entrypoint and real clock
guards. Healthy idle controller observations did not establish that combination.

## Release gate

Run the gates from the candidate checkout with its installed dependencies.
Recorded historical source data is required before accepting the constructed
workflow scenarios:

```sh
python scripts/run_historical_source_acceptance.py --report /tmp/eventedge-public-history.json
python scripts/run_historical_source_acceptance.py --private-corpus /absolute/private/corpus --require-full-supported-scope --report /absolute/private/full-history.json
python -m pytest -q -m 'scheduled_acceptance and not document_qualification and not live' tests
python -m pytest -q -m 'not live and not scheduled_acceptance and not document_qualification' tests
```

CI runs the public historical replay and both pytest commands in the baseline and
pinned optional-SDK environments. The private full-scope replay is additionally
required before release acceptance; public CI alone cannot qualify the complete
source set. A missing or invalid private corpus fails the full-scope command.
A failed gate blocks acceptance. Skipped, expected-failure, or omitted scenarios
do not establish the corresponding requirement.

The versioned manifest in `tests/historical/manifest.json` binds actual recorded
data, its acquisition time, reference/observation date, vintage, source format and
SHA256. The runner denies network and model calls. Original HTTP bytes test the
corresponding adapter; normalized accepted inputs test consumers and strategy
contracts. An alternate source format is identified as such. Neither normalized
captures nor reconstructed HTTP fixtures establish a native wire contract.
Controlled mutations are reported separately from the unchanged historical
baselines. Private/licensed inputs remain outside Git and public CI artifacts.

Preserve historical failures as failures. A replay can pass by correctly
retaining degraded coverage and rejecting unsafe data; it cannot turn that
evidence into healthy native qualification. Acquisition on a later date also
does not establish what was available at the earlier decision time. Current
provider/model availability and prospective continuity require separate native
evidence. Follow the [source incident policy](source-incident-policy.md) for each
newly observed failure and its reviewed reliability lesson.

The October 17 [stabilization profile](stabilization-profile.md) is the current
supported gate: the exact checked-in sixteen-book profile, two-session positive
paper fills, zero-IO replay, and explicit required/optional FRED failure cases.
The full 98-image scenario is deferred with its document features and remains
selectable using `-m 'document_qualification and not live'`. Its component and
replay regressions remain in ordinary CI. Historical document-profile evidence
below does not qualify those features for the stabilization profile.

The subprocess harness creates an independent temporary Git repository, frozen
generation worktree, profile, state directories and virtual environment. Its
fixture commit identifies that test snapshot, never the deployed release. It
uses the real controller, shell wrappers, generation manager, worker, locks,
source adapters, clock/calendar rules, orchestration, ledgers and report code.
Only external responses and the current-time source are replaced. All descendant
Python processes receive the same fixture boundaries and network denial,
including Python sockets and native curl transports. Negative transport tests
verify that unexpected calls fail before contacting a provider. The
wall-clock injection does not replace calendar calculations, admission checks or
the monotonic clock used for termination deadlines.

An explicit empty `.env` in the disposable repository bounds the manager's real
dotenv discovery. Its environment, home, temporary files and dummy authentication
storage are isolated. A synthetic ancestor-variable regression verifies that
normal loading cannot inherit configuration from the surrounding checkout.

## Profile provenance

The stabilization variant reads `config/profiles/stabilization-v1.json` directly.
It records the original and normalized profile hashes and audits only disposable
paths, inert authentication and a matching inert EDGAR contact header. Its frozen
blocklist, model routes, risk settings and explicit exclusions are unchanged.
The runtime artifact binds code, profile and installed-dependency fingerprints.
No bounded-document fixture is created for this variant.

The following retained profile remains a historical control.

The retained fixture is the original non-secret deployment profile, SHA256
`cfd010bec8effa9936df71a9fe87f72eac9403c43219163998d78d917314673d`.
Runtime copies substitute disposable filesystem/authentication paths and
constructed local document bytes with their matching digests. The harness records
and checks those substitutions. The normalized fixture has its own hash; it is
not represented as the exact deployed profile.

The separate environment fixture retains the effective `GOOGL,GOOG` compliance
exclusions recorded by the generation's October 14, 15:45 UTC preflight. It
includes that non-secret log excerpt and the retained evidence digests. The
harness checks the actual worker configuration and its accepted source binding;
this environment policy is declared separately from profile substitutions.

Model/effort selection, strategy exclusions, prospective clocks, source policies,
paper execution settings and the document's finite effective window remain in
force. Synthetic provider/model responses do not prove live provider availability,
account authentication, model extraction accuracy or financial performance.

The approved bulk-filing rollback deliberately keeps EDGAR research unavailable.
The full-profile rehearsal must preserve that documented coverage loss and its
degraded status. Acceptance requires completed accounting/staging for all sixteen
books and positive independent decision evidence while accurately reporting the
EDGAR exclusion; it must not make the source appear healthy to obtain a clean
label. The report can have complete, internally consistent evidence while its
input coverage remains degraded. Unexpected additional failures remain
acceptance failures.

## Contracts covered

- Complete scheduled evening chain, including the screen warning, governed hard
  gate, sixteen scenario books, durable attempt records and final report. A
  second ordinary session fills the first session's positive paper intents;
  repeating the controller must leave financial rows and accepted inputs intact.
  Every document and committee role retains its configured model and effort.
- Before-close and after-close behavior, the existing fifteen-minute historical
  SIP delay, early closes, holidays, daylight-saving changes and delayed timer
  wakeups. A successful diagnostic screen cannot authorize trading.
- A failed midday probe followed by a legitimate same-session evening run, with
  the original failure retained. Fixed runtime reasons remain distinct from
  provider failures and source-repair proposals.
- Receipt-backed deduplication and supported resolution of runtime preflight
  observations. A missing report, different configuration or unrelated successful
  probe cannot erase an incident or authorize a duplicate delivery.
- Actual process interruption and lock handoff, with no unintended changes to
  manifests, ledgers or accepted inputs. Nested source transports are included
  in the preflight timeout, worker-crash and outer-controller timeout cases.
  The outer controller's existing termination grace applies to its whole group,
  allowing a manager to forward termination after the shell leader exits.
- The document's exact finite expiry, with no automatic renewal or broader
  authority. Date-only document eligibility uses New York end-of-day. For this
  original profile, October 16's final fractional second exceeds the declared
  `2026-10-17T03:59:59Z` endpoint, so date-only October 16 is conservatively
  excluded; an explicit instant at the endpoint remains within the window.

The separately approved Friday successor changes only that scope endpoint to
`2026-10-17T04:00:00Z`, admitting the October 16 date-only reference. The original
document, historical claims and safeguards remain unchanged; later sessions are
still excluded. The original-expiry fixtures above retain their historical
expectations. Actual review completion and the decision cutoff must also remain
within the successor's finite scope. This profile preparation is not deployment
or native workflow acceptance.

The harness does not run systemd or prove live provider/model availability. Its
expiry cases exercise the eligibility boundary directly. Calendar cases cover
diagnostic acquisition, admission boundaries, and real controller dispatch at
18:00 New York after daylight-saving changes and an early close. Those two
dispatch cases stop at a deliberately stalled external transport; the complete
positive financial path uses ordinary October sessions. An uncatchable kill of
the manager itself cannot execute its signal forwarding; service containment remains
the systemd control group's responsibility.

## October 15 regression matrix

This matrix describes the earlier document-enabled candidate and its separate
qualification requirements. It is not the October 17 stabilization scope.

The additional prospective profile exercises government-copy V3 graphics and
the explicitly selected bounded-document recognition request V4. The original
profile and legacy request contracts remain separate controls. These tests cover
the following boundaries; each required gate must pass on the release candidate.

The complete 98-image scheduled scenario passed in 254.10 seconds against the
`fresh_generation_v3_v4` working-tree snapshot. Its retained log is
`data/forward-readiness/deployment-20261014/fresh-generation-20261015/scheduled-graphics-acceptance-v3.log`.
That variant records exactly three prospective policy changes: government-copy
V3, recognition request V4, and a document purpose explicitly limited to the
historical printed pledge counts and their associations, without asserting
current holdings or a present catalyst. Real internal subprocesses completed
all sixteen accounting/staging books and the complete evidence report; external
provider/model responses remained offline fixtures. This result establishes
that scenario, not the entire release gate or a native generation.

The current prospective profile adds a **fourth** change,
`model_runtime_policy=full_regulation_graphics_v1`; its SHA256 is
`e0bd0ebc1087e82a533e2d33968ab1984c1a5b584257818df4f32d4e674556cc`.
The earlier three-change snapshot above remains historical. A later V4-request
runtime-policy fixture passed five tests in 262.25 seconds with all 98 images,
four horizons, sixteen books, 26 recognition/26 review requests, three regulatory
Sol requests and sixteen committee requests. Its constructed outputs do not
certify the subsequent V5 request or native semantic acceptance.

The pre-final nonlive run retained **two failures, 8,410 passed, one skipped
and 305 deselected** in 606.55 seconds (`nonlive-runtime-policy-v4.log`). The two
fixture cases were repaired; their focused regression selection passed **77
tests**. That does not convert the earlier broad run into a pass. Exact-commit
CI, the complete final release gates and native full-generation acceptance
remain pending.

| Boundary | Retained or constructed evidence | Focused checks |
| --- | --- | --- |
| Genuine native model failures | Original selected-page responses retain punctuation disagreements, flattened citations/columns, blind context refusal and unrelated signature uncertainty. Failures remain failed after replay. | `test_bounded_document_native_failures.py`, `test_bounded_document_scoped_readability.py` |
| Complete graphical source through the scheduler | All 98 unmodified public original-size PNGs from three SEC notices, full rule text and constructed model outputs traverse all four horizons, sixteen books and the final report. The fixture tests structure and integration; its model outputs do not prove native recognition accuracy. | `test_scheduled_graphics_acceptance.py`, `test_regulation_graphics.py`, `test_regulation_graphics_assessment.py` |
| Recognition, review and continuation | Every batch supplies its original images to both Luna-high requests before Sol. Replayed receipts bind exact image bytes, prompts and responses; continuation context derives only from prior reviewed receipts. Missing, uncertain or tampered material stops analysis. Invocation-local reuse requires the same complete candidate, model/client identity, images, context and request contract. | `test_regulation_graphics_runtime.py`, `test_regulation_graphics.py` |
| Deadline acceptance | A final decoded response, regional collection or summary cannot become accepted merely because requests started before the deadline. Regulation graph finalization must also reject late completion while retaining original evidence. | `test_noaa_regional_budget.py`, `test_regulation_final_deadline.py` |
| Finite source storage | Regulation-specific V3 caches and V3-only source envelopes use an explicit 32 MiB encoded capacity, sufficient for the retained 98-image corpus. Shared envelopes retain any pre-existing larger capacity required by complete-filing or Congress-audit policies. Unrelated sources and legacy regulation policies retain their prior limits; oversized and malformed documents remain rejected. | `test_source_inputs.py`, `test_scheduled_graphics_acceptance.py` |
| Public intervention projection | A producer with umask `077` publishes the sanitized proposal atomically as `0644`. Real subprocess permission checks verify readability; the native root-producer/Hermes-UID-2001 probe also verifies that the consumer cannot write it. Private logs retain their private mode. | `test_source_repair_intervention.py` and retained native permission probe |
| V4 requested material | Both prospective producer prompts assess the requested probes and all materially required context. Unrequested handwriting/authenticity remain explicitly unassessed. Requested value, unit, row, column, citation or readability uncertainty still fails; V3 prompts and failed responses remain unchanged. | `test_bounded_document_scoped_readability.py`, `test_bounded_document_native_failures.py` |
| Historical transport recovery | Strict complete-graph replay must prove government copies for the entire one-for-one multiset of failed primary requests before report deduplication. Derived recovery rows bind resource, original receipt, government receipt, unit and graph identities. Extra/missing failures, incomplete or tampered proofs and unrelated provider errors remain unresolved; original HTTP diagnostics are preserved. | `test_regulation_source_recovery_report.py`, `test_regulation_replay_report.py` |

The matrix catches these demonstrated failure classes and nearby boundary
regressions. It cannot guarantee that every future provider response, document
layout, model judgment or operational failure is covered. New failures require
retained evidence and an appropriate regression; they do not become exclusions
because a prior fixture passed.

## Native evidence and remaining acceptance

These retained component observations concern the document-enabled candidate.
Native document qualification is deferred for stabilization, whose native
acceptance must instead match its exact declared supported scope.

Private component receipts are retained under
`data/forward-readiness/deployment-20261014/fresh-generation-20261015/`:

- `noaa-full-native-probe-16-20261015.json`: the candidate queried all 7,877
  declared stations across all ten states and completed in 116.619 seconds,
  within the 180-second source budget. The earlier eight-worker 188.134-second
  observation did not establish deadline acceptance.
- `regulations-native-probe-final-20261015.json`: the final source candidate
  with publication-deadline checks retained all twelve required documents and
  all 98 raw graphics. Observed at `2026-10-15T23:36:32.541899Z`, probe completion
  took 83.659 seconds, including 78.388 seconds of acquisition, with no source or
  validation error. The receipt explicitly does not establish semantic
  recognition of those images. The earlier 83.511-second probe remains retained
  separately.
- `projection-native-probe-20261015.json`: root production-style umask `077`
  yielded a `0644` candidate projection readable, but not writable, by the actual
  Hermes consumer UID 2001. This component check does not prove continuous
  consumer delivery or a deployed application change.

These are candidate component observations, not a complete native generation.
Native graphics recognition and independent image-backed review, V4 selected-page
recognition/blind review in the integrated current-session run remain required
acceptance gates. Final native evidence must bind the candidate commit and frozen
configuration, all enabled source and required model stages, all sixteen books,
accounting, staging, reports and read-only replay. The approved bulk-EDGAR rollback
and other frozen strategy exclusions must remain explicit; no additional failure
may be silently excluded. A report degraded solely by that approved EDGAR policy
must retain that description and cannot be called full-source green.
The prospective harness profile adds no source or strategy exclusions.

## Plan-stream moderation metadata regression

The candidate adapter accepts only the observed `response.metadata` envelope,
bound to an established response identity and valid sequence. Its exact
moderation containers require `protection_type="omnimod"`, one output/result,
`is_blocked=false`, 42 strictly false category flags and empty generation/tool
containers. Category labels are bounded opaque names; their exact vocabulary
is not verified. Unknown surrounding fields, blocked/flagged states, malformed
values and oversized/deep structures fail closed. Metadata cannot supply text,
model identity, lifecycle closure or the mandatory completed terminal event.

`test_chatgpt_plan_metadata.py` and `test_chatgpt_plan_transport.py` cover this
contract. The original native unsupported-event and depth-bound failures remain
failed in retained receipts, including the structural and moderation diagnostic
observations. This is an explicit adapter contract, not a claim of documented
upstream semantics. Component success must still be distinguished from the
complete generation gates above.

The retained `native-candidate-document-v2.json` subsequently passed native Luna
recognition, blind Luna verification and Sol thesis validation in 70.852 seconds
with this parser. Sol correctly retained the historical limitation and found no
current trading context. This establishes that selected-page component only;
it does not establish complete-document quality or generation acceptance.

## Graphics legibility and finite runtime

Graphics request versions remain explicit in every receipt. V2 clarified the
supplied-asset boundary; V3 required exact literal blocks and within-cell newlines;
V4 distinguishes readable material from missing pixel detail. V5 adds nonempty
positional descriptions for directly visible unprinted axes and other geometry,
using visual source/appropriate qualitative precision without inventing printed
labels, units or values. Null text remains restricted to actually blank cells or
unprinted row/column labels. Edge cropping alone is not missing material when
every character and association is unambiguous; unreadable, ambiguous or actually
missing material still fails. Validators remain unchanged. Exact V1–V4 requests
and receipts replay unchanged, including the retained V4 null-axis failure
(`regulation_graphics_v4_native_null_axis_failure.json`) and successful native V4
small recognition/review bundle (`regulation_graphics_v4_native_small_bundle.json`).
Original failures are never repaired, normalized or upgraded into accepted evidence.

The prospective `full_regulation_graphics_v1` runtime policy gives graphics
recognition and review calls 240 seconds each, within one shared 9,000-second
model phase. Ordinary calls retain 120 seconds. Existing profiles retain the
2,400-second phase; tighter enclosing deadlines always win. The finite policy
requires the native subscription route, Luna-high recognition, Sol-high thesis,
government-copy V3 and disabled live execution. It adds no source exclusion.

One retained timing diagnostic recognized the actual first four images of the
50-image document in 162.588 seconds, validating 27,700 response bytes. It used
the older V2 request and a private 240-second override, with no review, Sol,
retry or production change. This proves that the old 120-second cap can be too
short; it does not certify the new policy or the complete corpus. At the caps,
the longest 26-stage graphics sequence, four serial horizon Sol waves and sixteen
serial committee calls require 8,640 seconds before other work. The 9,000-second
phase leaves bounded margin and remains inside the existing 11,100-second daily child
and 12,600-second service ceilings. It is a capacity limit, not a guarantee that
all future model calls will finish. Full native acceptance remains mandatory.

The native V5 dense-batch component subsequently passed at
`2026-10-16T00:31:53.757767Z` (`native-candidate-dense_graphics-v6.json`), bound to
candidate source manifest `46d5584f27a8a6abc567977006dc5a5f2802292edfde4ababcd1757f142ed569`
and the four-change profile above. Recognition took 160.971 seconds and independent
image-backed review 33.689 seconds; total probe time was 194.764 seconds. This
covers only the first four images of the 50-image notice. Sol was not called;
neither the full 50-image document, all 98 graphics nor a native generation is
accepted by this component result. Exact-commit CI and full native acceptance
remain pending.

## Deployment boundary

Passing these checks establishes isolated candidate behavior. It does not deploy
the candidate, repair an active generation, install an alert consumer, change a
timer, or prove a live financial session. Production changes still require
Pedro's explicit deployment or narrowly scoped patch authorization and the
repository's governed release process.
