# Free source alternatives

Reviewed October 10, 2026. Source availability, acquisition completeness and
fitness for a trading decision are separate questions. A denied endpoint or
failed adapter does not establish that the underlying public data is unavailable.
The original 600-second acceptance limit remains unchanged. None of this work is
a production deployment.

## Source-by-source options

| Input | Free route and what it supplies | Decision and remaining evidence |
| --- | --- | --- |
| SEC filings and histories | [SEC submissions API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces), [EDGAR archives and indexes](https://www.sec.gov/about/developer-resources), accession directories and individual filed documents | Prefer official data. All 14 previously failed history captures reproduced through the native validator offline. The three oversized submissions have accessible official metadata; document selection and material image/PDF content still require validation. Bulk metadata is not filing text. |
| Congressional disclosures | [Official House discovery](https://disclosures-clerk.house.gov/FinancialDisclosure) plus the [publisher's revisioned public dataset](https://github.com/austin-starks/congressional-disclosures) | Implemented as display/audit only, with trading signals and model context disabled. The local real adapter reconciled all 37 House filing IDs in the September 9–October 9 window and retained 11 publisher-reported Senate filings. Senate discovery, extraction accuracy and historical first availability remain unverified. Native acceptance remains pending. |
| Court filings | CourtListener's existing focused queries; [Free Law Project bulk data](https://wiki.free.law/c/courtlistener/help/api/bulk-data/); [GovInfo court opinions](https://www.govinfo.gov/help/uscourts) | GovInfo supplies opinions from participating courts, not a complete stream of new dockets. Bulk access does not prove current marketwide coverage. Keep the approved focused issuer/case scope explicit; no equivalent free complete docket replacement has been established. |
| Proposed rules | [Federal Register bulk XML](https://www.govinfo.gov/bulkdata/FR) and its publication API, alongside the repaired Regulations.gov route | A useful official alternate publication source. It cannot silently replace Regulations.gov posting dates, docket records or comments. The direct Federal Register site presented an access challenge in this inspection, so current API usability is unproven. |
| Weather | [NCEI GHCN-Daily](https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily), its public Access service and station files | The application already uses public regional NCEI summaries. Station files are an official alternate transport, with the same station selection, quality flags, units and date-window checks required. NWS alerts are a different product and cannot substitute for the daily temperature/precipitation history. |
| USDA crops | [NASS Quick Stats](https://data.nass.usda.gov/Quick_Stats/) and [NASS downloads](https://data.nass.usda.gov/Data_and_Statistics/) | Official API and downloadable data are available. Preserve commodity, crop class, reporting year, geography, units and publication vintage. A downloadable file is not automatically the same observation population as the current adapter. |
| Drought | [U.S. Drought Monitor data downloads](https://droughtmonitor.unl.edu/Data.aspx) | Official weekly time series and GIS downloads provide alternate formats. Keep the Tuesday observation date distinct from the Thursday publication date; weekly data must not be represented as daily updates. |
| Government awards | [USAspending API and download endpoints](https://api.usaspending.gov/docs/endpoints) | Free official recipient and parent identifiers can improve attribution. They do not provide a universal verified join to exchange-listed securities. Retain the approved listed-target scope and explicit unresolved awards; do not guess a parent from a similar name. |
| Futures positioning | [CFTC public reporting API/exports](https://publicreporting.cftc.gov/stories/s/COT-Help/p2fg-u73y/) and [traditional COT reports](https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm) | Both distribute the underlying official reports. Keep report family, contract identity and Tuesday position date distinct from publication. An alternate transport must reproduce the same chosen COT population. |

The Congress source retains every selected-year filing and printed transaction
row, original Parquet/House-index bytes, a full immutable publisher revision and
actual acquisition time. Its full local envelope is about 15.6 MB; rows are not
trimmed to fit a smaller cache. A live redirect incompatibility was reproduced
and fixed before the successful local check. Eight physical HTTP requests and
serialization completed in 2.42 seconds locally; this is not a VPS or end-to-end
timing result. Publisher refresh targets are not an availability guarantee.

## Security identity and short interest

[OpenFIGI's public mapping API](https://www.openfigi.com/api/documentation) accepts
CUSIPs without an API key. A bounded local probe sent 70 distinct CUSIPs from 82
of the 85 unresolved ownership rows in 14 successful requests. Reconciliation
against the frozen asset master produced eligible security candidates for 29
rows, including 16 present in the SEC company map and 13 absent from that map.
These are mapping candidates, not new accepted execution bindings. A production rule still
needs exact filing/CUSIP provenance, complete mapping outcomes, a current unique
eligible asset match and frozen evidence for replay. In particular, the source
parser must preserve and validate unique structured issuer/CUSIP relationships;
similar names and the first returned ticker are insufficient. Missing results do not prove
that an issuer is unlisted. SEC cover-page XBRL is another official security-level
input; its entity and class contexts must be preserved, rather than matching a
visible symbol string. [SEC XBRL guide](https://www.sec.gov/files/edgar/filer-information/specifications/xbrl-guide-2025-09-08.pdf)

[FINRA lists Public API credentials at $0/month](https://developer.finra.org/fees)
and documents the consolidated-short-interest dataset. This is the next official
route to test for the required settlement cycle. It needs credential provisioning;
no account or subscription was created. The [public files page](https://www.finra.org/finra-data/browse-catalog/equity-short-interest/files)
still showed September 15 during inspection, while the required September 30
cycle had failed through the existing CDN path. Neither observation proves that
all official access routes lack the newer cycle. Full symbol coverage and native
runtime remain unproved. Daily short-sale volume measures a different quantity
and must not replace short interest.

## Missing bars

Keep the selected consolidated SIP source and exact failed symbol/session pairs
as the starting point. Alpaca's free IEX feed covers one exchange, so it cannot
silently supply equivalent consolidated prices or volume. [Alpaca feed definitions](https://docs.alpaca.markets/us/docs/historical-stock-data-1)
Alternative vendors also need adjustment, corporate-action, timestamp and
coverage parity. Missing observations remain explicit until a verified source
supplies the required bar or an independently proven asset lifecycle explains
its absence. No prices are forward-filled and no liquidity rule is relaxed.

The retained October 9 diagnostics for GYRO, HCHL, HFBL, MDRR and TULP establish
no qualifying daily-OHLC activity through a complete SIP tape, daily-response
evidence and a bound asset master. All five remain invalid price rows. The
prospective `reference_session_activity_v1` policy uniformly makes them
ineligible for new entries for that session, with original discoveries and
failed observations retained. Held assets, pending instructions and benchmarks
keep their price obligations. This is a completed offline eligibility finding;
fresh integrated native acceptance remains pending.

## Acceptance boundary

The approved prospective policies allow labeled current-only filing analysis
only when complete SEC history proves there is no unique earlier comparator;
comparison claims remain prohibited. Failed histories and unavailable selected
prior bodies still block that permission. Congress remains display/audit only.
These policy choices do not erase failed native runs or establish completion of
the full 600-second workload, model analysis, sixteen-book staging or replay.
