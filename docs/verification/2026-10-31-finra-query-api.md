# FINRA current-cycle query adapter, 2026-10-31

The candidate routes `OpenBBSource.fetch_short_interest` and its scalar caller
through FINRA's official `consolidatedShortInterest` query endpoint. It requests
the exact symbols and the settlement selected by the existing reviewed
publication calendar. This replaces the public caller's dependency on an
inaccessible current CDN archive. There is no automatic archive fallback.
The retained archive implementation and acquisition schemas 1, 2 and 3 remain
readable for historical proof and regression coverage.

This is prospective implementation and component qualification. It is not
deployed, does not repair an accepted historical session, and does not establish
whole-archive or historical completeness. Use requires a separately approved
new generation under the repository deployment policy.

## Contract and bounds

The [official FINRA API documentation](https://developer.finra.org/docs) describes
query filters, response limits and paging/count headers. The fixed endpoint is
`https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest`.
Queries use a `settlementDate` equality filter and a `symbolCode` domain filter.

- At most 1,000 exact symbols, partitioned into batches of 100: at most ten
  physical POSTs per attempt. Each request asks for batch size plus one rows,
  allowing an excess row to fail validation.
- At most two whole-acquisition attempts share the inherited 60-second operation
  cap and any earlier parent deadline. Physical POSTs do not retry internally.
- Each streamed response is bounded to 1 MiB of decompressed bytes. Responses
  and sessions close on success, rejection and timeout. The owned session has
  `trust_env=False`, sends no authentication, and follows no redirects.
- A successful batch requires HTTP 200, JSON content, `Record-Offset: 0`,
  `Record-Limit` equal to the requested limit, and `Record-Total` equal to the
  decoded row count. If supplied, `Total-Records-On-Page` must match too.
- Unexpected symbols, wrong settlement/accounting dates, duplicate rows, invalid
  JSON, an incomplete page, late results or any later batch failure withdraw all
  acquisition positives. Invalid consumed numeric values and missing symbols
  remain explicit per-symbol failures; missing data never becomes zero.

Acquisition schema 4 records official API provenance, exact query population,
reviewed publication context, accepted/missing/invalid symbol partitions,
monotonic duration, canonical rowset hash, and each batch's response hash, byte
count and paging counts. Counts must match each batch's exact partition as well
as the aggregate population. Query coverage does not use `archive_coverage` or
`historical_complete`. The existing date-only policy still waits until the
following New York calendar day; API accessibility does not alter that boundary.
The reviewed calendar remains limited to 2026.

## Verification

The focused adapter and integration command passed **65 tests**:

```sh
python -m pytest tests/test_finra_query.py tests/test_finra_query_integration.py -q
```

One existing `websockets.legacy` deprecation warning remained. Tests include
publication boundaries, 1,000-symbol batching, missing/invalid facts, transport
and streaming failures, paging inconsistencies, sanitized canonicalization
failure with guaranteed closure, per-batch row-count contradictions, cache
revalidation, unchanged source-codec round trips, monitor provenance, and
immutable committee replay after a different later acquisition. The two final
failure-boundary regressions were observed failing before their fixes.

Final broader application verification passed **555 tests**, with one optional
installed-SDK check skipped and two scheduled tests selected separately. The
broader run covers FINRA, OpenBB, request policy, enrichment, source inputs,
and all four sixteen-book interruption/factor-failure replay combinations.
Both isolated scheduled workflow cases passed (**2 tests**): complete query
coverage remained continuity-eligible; missing symbol coverage remained visibly
degraded. Each case validated the API receipt through the actual accepted daily
workflow and trusted read-only projection, with no changes to its accepted
inputs or canonical report pair during observation. Only external provider and
model responses were substituted. These constructed cases are not historical
provider evidence.

Independent review reproduced and closed two proof-validation gaps: impossible
per-batch row attribution and incomplete/invalid normalized facts in an accepted
enrichment envelope. The compatible native consumer's full suite passed
**148 tests and 184 subtests**. Final producer/native component validation of the
recorded live acquisition preserved exactly twelve positive symbols. Native
notification identity, legacy receipt behavior and pending delivery protection
remain unchanged.

A fresh integrated acquisition at **2026-10-31T08:34:03.929648+00:00** used only
the new public `OpenBBSource` route inside one 60-second budget. A database audit
guard rejected every SQLite connection, SDK initialization was forbidden, the
environment was cleared, and the source cache existed only in that process.

| Observation | Result |
| --- | --- |
| Current exact population | AAPL, ABR, ADM, CCJ, COPX, CPRI, CRI, DBA, MOO, SLV, UNG, UNH |
| Current settlement | October 15 for all twelve; no symbol errors |
| Current page proof | HTTP 200; total 12; offset 0; limit 13; 12 decoded rows |
| Repeated cached call | Same facts; twelve cache hits; no additional request |
| September 30 AAPL, `as_of=2026-10-22` | Short interest 120,763,539; days to cover 3.03; float percentage unknown |
| Historical page proof | HTTP 200; total 1; offset 0; limit 2; one decoded row |
| Schema-4 validation | Passed for both actual acquisitions |
| Monitor component projection | Complete for the exact twelve-symbol short-interest input; twelve healthy rows; original schema-4 metadata retained |
| Total physical requests / elapsed | Two POSTs / 0.487 seconds |

The September 30 normalized value matches the retained successful native
downloader/model control using `openbb-finra==1.6.1` and
`openbb-core==1.6.13`. This run did **not** execute the native model again: those
optional packages are absent from the local verification environment. The
comparison is labeled explicitly in the evidence. The monitor check exercises
`project_enrichment` and `assess_accepted_enrichment`; it is not an accepted
operational session, report-bound publication, all-sixteen-books production
replay or native notification poll.

Local handoff evidence is
`.handoff/2026-10-31/source-recovery/finra-query-implementation-qualification.json`
(SHA-256
`9c6168b09589d1b1fbfb645fa1cb24cb8ac36ff4338ae0b732c44651472df91d`).
The separate `finra-query-monitor-component.json` retains the projection for
independent native consumer interoperability checks. The tracked public fixture
is `tests/fixtures/finra_query_public_20261015.json`.

## Release ordering and remaining limits

Schema-4 acquisition is a new nested contract under the existing enrichment
monitor version. An old native reader rejects it; it cannot silently establish
recovery. Prepare and verify matching application observer and native consumer
changes, and install the compatible native reader before exposing schema-4
monitor observations. Preserve the existing notice identity and pending or
confirmed delivery receipts. The observer release is separate from deploying
generation code.

The application PR is stacked on [source-recovery PR #63](https://github.com/potalora/eventedge/pull/63).
Its paired consumer is [private-agent-context PR #31](https://github.com/potalora/private-agent-context/pull/31).
Neither PR is merged or deployed by this work.

Only a fresh, positively validated ordinary acquisition for the previously
failed exact symbols can support a subsequent recovery observation. Archive
diagnostics, historical failures and old schema-1/2/3 accepted inputs remain
unchanged. Native notification delivery, production observer installation and
the separately approved new-generation deployment remain outside this
qualification. No production database, checkout, manifest, service, timer or
notification receipt was changed.

Monitoring retains its existing 64 KiB sidecar and 256-operation scope bounds.
A valid acquisition with a larger population can exceed monitoring capacity;
that projection fails closed and cannot publish partial coverage as complete.
The twelve-symbol qualification fits both bounds.
