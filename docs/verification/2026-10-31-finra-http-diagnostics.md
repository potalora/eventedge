# FINRA archive diagnosis and retained HTTP observations

The required October 15, 2026 short-interest archive remains inaccessible from
bounded public probes on October 31 at 04:53 UTC. Its exact native SDK URL,
<https://cdn.finra.org/equity/otcmarket/biweekly/shrt20261015.csv>, returned HTTP
403 with a 111-byte XML `AccessDenied` response. Its SHA-256 was
`a824bc7739e226e1b40ea0f8c4e4f4c6f796fc3b4abfa6e9abe3bd119a30d938`,
matching the independent October 30 probe. A same-CDN September 30 control,
<https://cdn.finra.org/equity/otcmarket/biweekly/shrt20260930.csv>, returned HTTP
206 and the expected CSV header prefix for a requested byte range.

FINRA's [official reporting calendar](https://www.finra.org/filing-reporting/regulatory-filing-systems/short-interest)
still lists October 26 publication for October 15 settlement. The reviewed
publication rule therefore correctly requires that cycle. HTTP 403 does not
establish whether the file is absent or access is denied by the origin/CDN, and
the successful control does not establish every FINRA endpoint is healthy.
These later observations cannot establish the original SDK response status.
Increasing a timeout does not resolve the observed access denial. A complete
public archive or provider-side access correction is still needed; local
repairs cannot manufacture that data.

## Why automatic recovery did not cure this failure

OpenBB FINRA 1.6.1's native downloader returns silently for every non-200
response. EventEdge checks the cache again and correctly rejects the absent
required current archive, but the original HTTP cause is already lost. It
records `provider_error`, which is outside the transient retry set (timeouts,
transport errors, provider maintenance, HTTP 429/500/502/503/504). Blindly
retrying a known 403 would not supply an unavailable archive.

The separate source incident/repair path consumed main source observations and
source-health failures. Optional FINRA enrichment failures were retained in
accepted decision input and excluded from research, but did not become main
source failures. Thus a complete main workflow could coexist with failed
supported enrichment and `continuity_eligible: false`, without a FINRA repair
request. Repair admission did not itself authorize acquisition, override an
upstream access policy, or rewrite an already accepted session. The historical
session remains disqualified regardless of later recovery.

## Narrow diagnostic repair

Acquisition schema 3 retains a `current_archive_probe` only when the native SDK
returns and the required current archive is still missing. Schema 1 and 2
remain readable unchanged. Every consumer of durable FINRA acquisition metadata
uses the shared versioned validator; no archived metadata is rewritten.

The probe is one streaming GET to the fixed native FINRA URL with
`Range: bytes=0-0`. It reads response headers without consuming the body,
follows no redirects, sends no ambient authentication or cookies, and performs
no cache write. Its connect and read timeout allocations together are capped
at three seconds and clipped to the existing remaining acquisition budget.
Requests timeouts are cooperative inactivity limits, consistent with the
existing provider policy, rather than a forced process interruption. No
request starts after an expired budget. Healthy current archives do not probe.

The exact diagnostic fields are `kind: post_download_http_probe`,
`settlement_date`, fixed `url`, aware `observed_at`, `http_status`, and `outcome`.
Only fixed outcomes are retained: `http_response`, `timeout`, `transport_error`,
`probe_error`, or `budget_exhausted`. Response bodies, headers, exception text,
redirect destinations, credentials and arbitrary provider strings are excluded.
Cleanup failures cannot replace the original acquisition failure.

This is explicitly a **later diagnostic observation**. Its HTTP status never
becomes the original SDK response status, changes the original acquisition
`provider_error`, triggers retries, certifies archive contents, fills the cache,
or changes current/historical completeness. Even a later HTTP 200 or 206 leaves
the missing archive failed. A redirect is retained as a status without following
it. The existing current-cycle and per-symbol freshness gates remain required.

## Verification

New tests first failed because the probe and schema were absent. Synthetic
regressions cover later 403 retention, 200/206/redirect/transient responses
remaining failures, sanitized probe exceptions, response/session cleanup,
existing deadlines, no networking for a healthy archive, strict diagnostic
validation and compatibility with archived schema 1/2. The existing native
fixture disables real networking; dedicated probe tests inject the transport.
Public probes used scratch files only. No production database, cache, service,
generation or accepted decision input was changed.
