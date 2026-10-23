# USAspending recipient-only fallback

USAspending's October 23 search returned award `363616285` while both of its
detail URLs returned 404. The retained exact-award transaction search and
recipient profile identified the same award and recipient. The checked-in
fixture preserves the full 35-award search and the original public responses.
This is an observed endpoint discrepancy; its backend cause is unknown.

The versioned `exact_award_recipient_v1` fallback runs only after a retained 404
for the exact generated award identifier. It requires native generated and
numeric award IDs, recipient UEI/hash/name, agency, description, dates and
cumulative obligation amount to agree across the original search and a complete
exact-award transaction search. The exact-hash recipient profile must independently
confirm the same recipient. A malformed 200, 403, timeout or other failure does
not trigger this fallback.

Successful corroboration grants `native_recipient_verified`. The award's parent
UEI remains empty. Profile parents, business types and names do not establish
ownership or a listed issuer; unresolved ownership remains nonactionable under
the existing verified-listed-target policy. No crosswalk entry is added.

## Bounds and replay

All requests consume the existing shared provider deadline. Each identity body
is streamed with a 2 MiB limit and closed. The transaction search is capped at
ten pages of ten rows. It requires contiguous, complete pagination, rejects
duplicate rows and sums all transaction obligations using decimal cents. This
conservative version also requires matching descriptions and recipient identity
throughout; unsupported historical changes fail closed.

Retained proof includes the original search row, exact requests, response bodies,
canonical body hashes and ordered UTC observation times. Acquisition must fit
within ten minutes; future observations and future modification dates are
rejected. A future performance start is allowed because it is not publication
evidence. Old valid proof remains replayable without acquiring new data.

The attribution validator rechecks the complete proof from frozen inputs.
Operational reporting then binds one original 404 diagnostic to that exact
award URL's hashed operation identity. It preserves the HTTP failure and retry
count, adds the verified recipient-recovery basis, and leaves unrelated failures
unresolved. Hashes provide integrity checks, not independent authentication of
an arbitrary fabricated capture.

## Test scope

The regression tests replay the entire recorded 35-award population through the
real adapter and request policy. Expected dispositions are nine verified listed
targets, 25 verified nonlisted targets and one unresolved recipient. The latter
retains the 404 proof and acquires no parent or ticker. Separate negative cases
cover mismatched identifiers, malformed bodies, partial/duplicate pagination,
empty HTTP 200, exhausted deadlines, oversized bodies, future/tampered evidence,
and unrelated or duplicated transport failures. A 503 retry ending in 404 is
tested through the real request policy and report recovery.

Recorded response bodies use explicitly synthetic ordered acquisition clocks
when replayed; the investigation did not request the endpoints in the final
adapter's order. This replay is not represented as a fresh live fallback.
A subsequent live check at 06:35 UTC found that the award detail had recovered
upstream and returned 200. Its complete 37-award October 22 and 35-award
October 23 query captures therefore exercised the normal path. Retained 404
replay and current live acquisition establish distinct facts.

See the [real-data testing protocol](2026-10-23-real-data-testing.md) for the
repeatable capture/replay commands, clock and population comparisons, native
sixteen-book acceptance, and the separate next-session observation requirement.
Development tests and a PR are not deployment evidence. Production activation
requires the authorized new-generation workflow and its retained receipts.
