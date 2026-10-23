# Real-data testing protocol — October 23, 2026

This is a development acceptance protocol, not deployment evidence. A topic PR
and successful tests do not change production. Behavior changes require a new
generation and Pedro's explicit deployment instruction.

## What the current evidence teaches

The [October 22 award review](2026-10-22-award-source-repair.md) and
`tests/test_award_ownership_review_20261022.py` cover the complete **October 21
37-award snapshot**. That coverage did not include the later award `363616285`.
The retained public fixture
`tests/fixtures/usaspending-recipient-fallback-2026-10-23.json` contains the whole
35-award search response for its September 23–October 23 window, two exact-award
detail 404s, a matching transaction-search response and a recipient profile.
Both detail identifiers failing is an observed endpoint discrepancy; it does not
prove that the award or recipient is absent. The recipient profile's parent is
not historical ownership proof for this award. Passing replay of this fixture
establishes these captured cases, not all future awards or all identity endpoints
for the other 34 awards.

`tests/test_fred_source_check.py` uses an injected source and one requested
vintage. It verifies all nine configured series are dispatched, safe diagnostics
and the shared deadline; it does not exercise the HTTP adapter or provider-day
rollover. The manual command in `scripts/check_fred_source.py` explicitly grants
no incident-recovery authority and does not infer release timeliness from
observation age. A later successful observation cannot explain an earlier
failure without its raw response and clocks.

The retained local-only warning record
`.handoff/2026-10-23/deployment-gen024/preflight-2026-10-23-provider-warning.json`
records that the 04:32 UTC anticipatory check requested October 23 while FRED
reported its current date as October 22: prior-day vintage requests returned 200
and the October 23 vintage returned 400. The local-only
`.handoff/2026-10-22/source-warning-reassessment/fred-recheck-summary.json`
records all nine series passing at the requested October 23 vintage at
06:02–06:03 UTC on the same prior candidate commit. These observations neither
erase the failed check nor qualify the current repair candidate. They establish
why provider-date boundaries need separate tests. These handoff records are not
Git fixtures; a reviewable validation receipt must preserve their scope if cited
outside this workspace.

`tests/test_decision_clock.py` already checks latest-completed-session references,
weekend/next-open eligibility, frozen replay and clock/evidence mismatch
rejection. These guards must remain. A provider's prior-day vintage shortly
after New York midnight is a separate question from whether the reference
exchange session is valid. No FRED clock defect is established by that symptom
alone.

## Required acceptance layers

| Layer | Evidence and acceptance | Failure remains visible |
| --- | --- | --- |
| Fresh acquisition | Cold, bounded acquisition of the complete configured population on adjacent completed sessions; record every award and every page through explicit terminal evidence. Compare additions, removals and changed identities, not just counts. | Missing detail, unresolved ownership, incomplete pagination and deadline exhaustion cannot disappear through sampling or an old cache. |
| HTTP adapter replay | Replay retained provider bodies/statuses through the real adapter at the transport boundary, with unexpected requests rejected. Include search → detail/fallback → normalization → frozen source output. | A successful parsed-row fixture alone cannot certify transport selection, response shape, pagination or fallback behavior. |
| Pure validation replay | Independently rebuild the frozen evidence's validity and attribution from retained inputs without HTTP, SDK or model access. Recompute bindings and coverage rather than trust supplied success flags. | Tampered or missing evidence must lose authority even when a stored status says verified. |
| Native integration | On the intended runtime and exact candidate/configuration, run the bounded full four-horizon × four-size workload in isolated scratch state with real providers and actual configured models, then validate source/analysis coverage, every committee/staging outcome, reports and immutable completed-session replay. | Source-only checks, model smoke tests, simulated committees and sixteen valid empty ledgers do not establish a complete native research/staging run. |
| Continuing observation | After separately authorized deployment, inspect the next completed session and the next relevant provider publication boundary using ordinary accepted reports. | One snapshot cannot certify continuity, recovery, profitability or the next session. |

Use the existing [forward-test protocol](../research/2026-10-09-forward-test-protocol.md)
and governed launch/replay harness; retain their resource caps and production
invariance checks. Do not change host time, extend budgets merely to obtain a
pass, or substitute the writable development shadow for production evidence.
If a required layer cannot complete before a PR, record it as pending and keep
deployment acceptance false.

## Time and population matrix

For each native observation, retain separately: actual UTC request start and
response completion, requested query date/window, latest completed exchange
session, next eligible session, provider's documented publication timezone/date
or explicit unknown, response `Date`, and observation/realtime-vintage dates.
At October 23 06:00 UTC, an October 23 query label does not by itself make
October 23 a completed XNYS session. Recompute the calendar relationship.

Use at least two adjacent eligible reference sessions for changing populations.
For a suspected time-boundary issue, add observations on both sides of the
relevant provider boundary and exchange close/open; include a weekend or holiday
fixture where applicable. Exercise the provider contract when its date still
differs from UTC or New York. Historical raw captures may replay these states;
changing `--as-of` alone cannot create historical information availability.
Until the second native observation exists, report single-snapshot acceptance
explicitly. Do not relabel the first snapshot as a new session to fill the gap.

For USAspending, retain all discovered awards even when only a verified listed
subset may proceed. Check exact award identities, recipient bindings, complete
transaction pagination and explicit recipient-only versus same-award-parent
proof. Test malformed JSON/types, missing UEIs/IDs, contradictory recipient or
award identifiers, conflicting rows, duplicate or looping pages, absent terminal
metadata, unexpected statuses, timeouts and exhausted budgets. Recipient names,
profile parents and precomputed attribution flags cannot create missing proof.

## Capture and evidence gate

Retain sanitized public request/response material before normalization: method,
URL, nonsecret query/body, status, relevant headers, request start/completion,
response byte count, SHA-256 of the consumed decompressed entity bytes, capture version and
sanitization rules. Keep credentials, authorization headers, session state and
private data outside Git. Where sanitization changes bytes, retain a separately
identified sanitized-artifact digest; do not claim it is the original raw digest.
Canonical JSON reserialization is not a substitute for hashing original bytes.

The October 23 fixture preserves raw-response digests and request-start receipts;
its transaction and recipient captures lack retained server `Date` headers.
Those fields are unavailable, not inferred from neighboring requests. Preserve
this limitation when citing it; future captures should retain completion times
and server dates where supplied.

For this repair, `scripts/check_usaspending_source.py` implements the bounded
USAspending capture/replay evidence manifest, with a nonzero exit on missing or
invalid required evidence. Reuse the existing native launch/report/replay receipts
for full-workload acceptance. The focused manifest and separate native receipts
provide these bindings:

- Exact candidate commit, configuration digest, runtime identity and policy
  versions; artifact paths and recomputed digests, not operator-supplied booleans.
- Each observation's clocks, reference/query sessions, discovery window, full
  population digest/count, page census and terminal evidence; adjacent-session
  comparison or an explicit pending continuity result.
- Capture provenance and limitations; adapter replay, independent frozen
  validation and adversarial-case command/results tied to that same candidate.
- Full native sixteen-book result, actual model-call/committee census,
  staging/report validation, replay result, deadlines/remaining workers and
  unchanged production checkout/manifest/state evidence.

Keep offline, native snapshot and continuity verdicts separate. Recompute the
latest completed session with the real clock when admitting a fresh native run;
reject stale or mislabeled acceptance instead of altering the frozen replay
clock. Reject missing artifacts, digest mismatch, candidate/config mismatch,
incomplete required populations or any failed required layer. An unresolved
award may remain an explicitly validated nonactionable exclusion under the
approved policy; it cannot silently become complete attribution. Record the
exact supported scope rather than assert universal source coverage.

Run from the repository's Python environment, choosing a new output directory:

```sh
python scripts/check_usaspending_source.py --session 2026-10-22 \
  --session 2026-10-23 --output /path/to/new-public-capture --budget-seconds 180
python scripts/check_usaspending_source.py --replay /path/to/new-public-capture
```

The capture uses a fresh real adapter for each adjacent XNYS query session and
one shared deadline. It records commit/dirty status, runtime versions, actual UTC
receipts, latest completed reference, exact prepared requests, all consumed
responses and whole-population policy evidence. It then replays all requests in
order through the same adapter and independently validates the frozen scope.
Artifact hashes, normalized populations, policy digests and safe request
diagnostics must match. Replay changes no retained files and uses the recorded
clock for metadata validation, so old evidence does not become a fresh snapshot.

Only explicitly unread retry responses (429/500/502/503/504, closed before any
body read) may retain status/order with `body_state=intentionally_unconsumed_retry`,
`complete=false` and zero retained bytes. This makes no claim about their missing
bodies. Every accepted identity response remains fully consumed; unread 200/404
responses, partially read retry bodies and transport exceptions without a
complete receipt fail capture acceptance. `response_started_at` is the
`Session.send` return receipt, not a measured first-byte arrival; for nonstreamed
requests the body has already been consumed at that point. Failed first attempts
remain retained and visible.

Two windows queried at one instant test population drift, not two historical
information sets or two completed native observations. The next-session query is
labeled diagnostic. A clean candidate commit capture, separate full native
sixteen-book acceptance and subsequent ordinary-session observation remain
required for their respective claims. The checker does not certify the native
model workload, authorize deployment or close incidents.

## Shared-window contradiction found by native testing

The clean-commit October 23 captures exposed a second, separate acceptance gap.
The October 21/22 queries returned 38/37 awards; October 22/23 returned 37/35.
Award `CONT_AWD_36C24827C0008_3600_-NONE-_-NONE-` has an October 21 base obligation
date and amount 63,117,507.88, so it belongs to both pairs' shared date ranges.
It appeared in October 21 and October 23 query results and was absent from the
October 22 result. Independently valid HTTP acquisition, per-window scope and
exact offline replay did not establish a mutually consistent population. The
old v1 `ok=true` verdict did not perform this comparison and is insufficient for
combined acceptance. Retain that original receipt rather than rewriting it.

Checker v2 therefore compares award membership within the intersection of the
two query windows and checks native identifiers, recipient identity, economic
fields and source modification dates. Natural entry/exit outside that shared
interval is allowed. Acquisition clocks and computed issuer attribution are
excluded. Changed source fields, including legitimate updates between requests,
produce an explicit inconsistency requiring assessment; the checker does not
infer a provider defect, indexing delay or recovery.

The result separates `acquisition_ok`, `offline_replay_ok` and
`population_consistent`; top-level `ok` requires all three. Contradictory windows
retain both complete payloads, raw captures and comparison details, while the
command exits unsuccessfully. Independent offline replay may still pass.
Replaying old v1 captures applies the new comparison and returns the reassessed
verdict without modifying their existing manifests or receipts. The portable
regression retains all 38/37 native award-key/date/amount projections with the
original payload digests; complete HTTP-layer tests separately verify that
inconsistent populations remain retained and nonaccepted.
