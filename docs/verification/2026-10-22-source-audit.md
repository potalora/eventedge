# Source usefulness and reliability audit — October 22, 2026

This audit separates availability, admitted coverage, freshness, economic meaning
and actual consumption. The inspected production generation was `gen_023` at
`56bdf1929a58778c6c58f435f2ad5a2e4b5d1729`. Its authoritative checkout/data were
read only. Candidate repairs described below are development changes, **not
deployed**. No daily/trade run, accepted input, manifest, service or timer was
changed, and no model calls were made for this audit.

Evidence: the existing sanitized `source-health.json`, native
`source-results.json` and `optional-results.json` receipts from October 22; the
October 21 canonical production operational report; maintained source/profile
documentation; code tracing; explicit offline counterexamples; and the bounded
native CFTC/calendar checks below. One passing request is neither a completeness
claim nor a future uptime guarantee.

## Consumption matrix

The profile has sixteen books: 30d/3m/6m/1y crossed with 5k/10k/50k/100k.
Five supported research strategies serve every horizon. Commodity macro is
eligible for 3m/6m/1y, subject to each size's commodity/risk limits; 30d generates
no commodity entries, and 1y limits its instruments to GLD/SLV. Shared source,
regime, execution and accounting requirements still span the full book roster.
See `operating_contract.py`, `cohort_orchestrator.py` and `commodity_macro.py`.

| Source/capability | Actual consumer and value | Current acquisition/admission and limitation | Access/qualification action |
| --- | --- | --- | --- |
| Alpaca asset master | All new-entry security identity and eligibility; focused/award attribution joins | Complete asset-master request passed: 33,566 returned records, subsequently filtered by policy | Existing authenticated route works; verify exact asset identity/lifecycle, rather than treating every returned asset as eligible |
| Alpaca consolidated SIP bars and corporate actions | Every book's reference/open/close/held/pending/benchmark pricing, liquidity and ledger continuity | Existing native governed check passed the four sampled benchmarks; this does not prove every possible signal symbol/session | Keep exact feed/adjustment/session and complete-pagination guards; exercise actual admitted candidates plus held/pending assets, and retain unexplained missing bars |
| Yahoo research price basket | Shared market context and nominally all six supported strategies; weather/contract momentum | Two operations passed; atomic 37-symbol research basket plus independent VIX capability | Require exact expected session coverage, not merely one finite close. The fixed basket includes disabled state-proxy ETFs but omits several declared food/agriculture names; treat universe changes as prospective policy work |
| Yahoo VIX | Shared regime; independent fallback when FRED VIX is unavailable | Existing separate capability prevents unrelated basket failure from removing VIX | Validate observation/session freshness independently of successful transport |
| FRED | Shared HY spread/yield curve/VIX regime across books; commodity macro FEDFUNDS/CPI/VIX veto; state unemployment optional disabled consumer | Nine series recovered in the fresh check after the maintenance incident; optional series are compartmentalized | Preserve realtime vintage and original missing observations. Root repair addresses HTTP-200 maintenance/error semantics and metadata. Do not substitute an older finite value for a rejected latest value |
| Finnhub earnings calendar + earnings news | `earnings_call`: fiscal event, EPS/revenue actual/estimate and concatenated news proxy | Calendar returned 212 discoveries; ten events admitted. Around those events 74 articles were discovered, 28 admitted and 46 excluded by per-event article cap; eight proxy texts produced | Existing free-key paths worked. This is **earnings news**, not actual call/Q&A transcripts or a verified prior-quarter guidance comparison; preserve that distinction in model claims and documentation |
| Finnhub issuer news + peers | `supply_chain`: news-keyword screening for seven declared issuers; peer context is not a supplier/customer relationship | 1,153 articles and seven peer collections returned; candidate admission further bounds analysis | Existing paths work within the declared scope. Measure event duplication, publication dates, keyword false positives and admitted/excluded counts before broadening the issuer universe |
| Finnhub commodity news | Optional catalyst boost for `commodity_macro` | Shares the fetched news universe with other Finnhub consumers; absence of a keyword catalyst is not provider failure | Preserve CFTC/FRED deterministic evidence; do not claim marketwide commodity-news coverage |
| Finnhub PQC news | Intended `quantum_readiness`, currently disabled by bulk-filing policy | Original probe: ten configured symbols, six queried; 479 article discoveries, 56 keyword hits despite the strategy exclusion. Candidate now makes zero PQC requests when `quantum_readiness` is explicitly disabled | Candidate retains all ten configured query exclusions with `strategy_disabled` and the unchanged budget, preserving active earnings/supply-chain requests. Reusing/broadening this research requires verified supporting filing evidence |
| SEC issuer map | Focused litigation issuer identity; award identity cross-checks | Still active even though bulk filing research is disabled | Keep the focused public lookup; a bulk-source retirement must not hide this dependency |
| CourtListener focused dockets | `litigation`: declared issuer/case queries; currently one watchlist issuer plus governed held/pending/prior-shortlist targets | Two requests, 28 dockets, declared scope complete. Metadata only; no case-document/full-opinion claim, no marketwide coverage | Existing token route works. Check omitted issuer counts, legal-name/CIK binding, docket identity and actual materiality. GovInfo opinions/bulk collections are not equivalent new-docket coverage |
| NOAA GHCN yearly | `weather_ag`: state/day/type weather evidence and derived heat/frost/precipitation gates | Three operations; ten states, 7,877 catalog stations, 229,598 selected records; observations through Oct19, two-day lag within declared seven-day limit | Official yearly transport works. Preserve quality flags, trace precipitation, units, station inventory and state/day/type completeness. Full Access-JSON rounding parity remains unproved and is not required/claimed by yearly policy |
| USDA NASS crop conditions | `weather_ag`: commodity/class/state Good+Excellent decline, season-aware gates | Three operations passed; corn/soy/wheat scope with separate active/inactive crop-class coverage | Existing API/mirror routes work. Verify current reporting week for active classes; inactive wheat classes are not a stale-feed incident. Keep crop reporting year distinct from observation year |
| Drought Monitor | `weather_ag`: ten-state categorical drought composite | Two requests passed; latest published weekly Tuesday map selected using Thursday 08:30 New York release boundary | Existing public service works. Preserve observation vs publication distinction; explicit broad historical windows still need date consistency checks. Composite/severity are currently two acquisitions and should not silently diverge at a release boundary |
| CFTC disaggregated futures COT | `commodity_macro`: five commodities' managed-money net positions, 52-observation percentile and week-over-week change | Original endpoint checks passed but could admit arbitrarily stale history. Candidate repair now binds current positioning to the official holiday-aware publication calendar | Official annual archives and release page both accessible. Candidate native check returned Oct13 for all five commodities, exactly the report due at Oct16 15:30 Eastern. Missing calendar/current report fails explicitly; no invented age allowance |
| USAspending awards + issuer attribution | `govt_contracts`: new-awards-only discovery, cumulative award obligations, verified listed issuer targets | Complete 30-day acquisition returned 37 awards; original attribution: nine verified listed, ten verified no-listed-target, eighteen unresolved | Transport success does not resolve issuer identity. Reviewed UEI/parent dispositions in the adjacent award repair improve the subset; retain every unresolved award rather than guessing from recipient names |
| OpenBB profiles via Yahoo | Every committee's sector/classification context for admitted signal tickers; several strategy optional enrichment hooks are currently not populated | Original cold probe tested only AAPL. Candidate now rejects blank name/sector and invalid supplied market cap, exact-symbol ambiguity, preserving healthy batch siblings | Broaden to actual stock/ETF signal population. Missing classification is an explicit optional failure; do not fabricate an ETF sector or call an empty profile complete |
| FINRA short interest via OpenBB | Every committee's optional squeeze/short-interest context | Cold AAPL check succeeded at Sep30, the correct current published cycle on Oct22. SDK inventory listed two older absent dates; success did not originally enforce current-cycle coverage per symbol | Official schedule and per-symbol stale/current admission are separate from historical inventory. Adjacent FINRA repair verifies holiday settlement dates and publication boundaries. Daily short-sale volume is not an equivalent replacement |
| French monthly factor data via OpenBB | Every committee's optional market/style factor context | Cold probe returned four keys and twelve history rows. Original adapter silently used US monthly three-factor SDK default despite an unused default model label of five | Candidate explicitly preserves three-factor default, validates required finite values and unique completed observation months, sorts history and passes actual observation/acquisition dates into committee context. No fixed monthly publication SLA was established, so no daily-age cutoff is invented |
| SEC bulk filings/ownership histories | `insider_activity`, `filing_analysis`, `quantum_readiness`; potentially high-value original disclosures | Explicitly disabled in production. Separate read-only audit verified real submissions/archive and 10-K/8-K/Form4 originals for one issuer; no bulk acceptance claim | Requalify marketwide discovery, issuer/class attribution, comparator histories, complete material text/graphics and bounded replay before a separately reviewed new-generation opt-in |
| Regulations.gov / official government copies | `regulatory_pipeline`; optional commodity regulatory catalyst | Explicitly retired in production. Separate diagnostic exercised all six former agency scopes: FDA/FTC complete; candidate EPA improves from four to eight of nine complete; SEC correction needs original dependency; DOL/CFPB current windows empty | Wider year discovery found no DOL notice and six CFPB notices. One actual CFPB body is accessible but outside the supported government-copy parser. Candidate preserves EPA identity/notation; historical reposts, corrections, additional agencies and graphics still require qualification below. No policy activation |
| Maturity-identified futures curves | Optional commodity committee enrichment | Explicitly retired: old historical price adapter did not identify contract maturities | No supported free route was qualified in this audit. Require dated contract identities/maturities and actual curve points; never relabel a historical price series as a futures curve |
| State-economic events | `state_economics`, explicitly disabled | Existing state ETF/return proxy was not supported as causal state-event evidence | FRED state data can be useful, but reopening this strategy requires documented event/release timing and verified issuer/geographic exposure, not merely an accessible endpoint |
| Bounded retained documents | Optional `bounded_document_research` strategy/workflow | Policy off; registry empty. No provider request and no model/document qualification in this audit | Retained graphics/document qualification remains separate; an installed reader is not evidence that a real document corpus passes |
| Other OpenBB handlers | Estimates, insider/government trades, unusual options, SEC RSS, sector screener | Implemented SDK routes exist, but active acquisition supplies only `enrichment_only`, then committee profiles/FINRA/factors. Most strategy hooks never receive their expected estimates/profile/short payloads | Do not count dormant handlers as available active sources. First define a consumer/contract and bounded native check; FMP entitlement-dependent routes are not proved by a credential being present |

## Prioritized findings and bounded follow-ups

1. **Reject semantic success with unusable evidence.** CFTC accepted 52 rows
   ending in 2025 for an October 2026 acquisition and the source projector called
   it usable. Fama-French accepted NaN/Inf/boolean factors and empty histories;
   profiles accepted symbol-only rows. These were reproduced against real
   adapter/projector code, not inferred from endpoint errors. Candidate source
   repairs and regressions are described below.

2. **Close attribution/audit gaps without inventing data.** USAspending unresolved
   identities and the two missing House disclosures were visible despite healthy
   HTTP responses. Follow the adjacent reviewed identity and original-PDF
   investigations. Congress stays display/audit only: publisher Senate discovery
   and extraction accuracy remain unverified, and an original PDF does not prove
   a publisher-extracted transaction row is correct.

3. **Test the actual decision population.** Original optional checks covered only
   AAPL. Run bounded candidate-symbol profile/FINRA probes across stocks, ETFs,
   missing classifications, current-cycle absences, exact symbol aliases and
   malformed/latest numeric rows. Do not weaken the whole research-price basket
   or sector/borrow/liquidity checks merely to obtain green status.

4. **Remove misleading capability assumptions.** Earnings news cannot answer
   call-transcript Q&A questions; peers cannot prove supply-chain edges; docket
   metadata cannot prove complete case documents; new-award cumulative obligations
   are not a fresh incremental contract announcement. Keep the exact supported
   claim in prompts, diagnostics and research evaluation.

5. **Respect the disabled PQC consumer during acquisition.** The old
   `multi_strategy_engine.py::_fetch_finnhub_data` still requested PQC news and
   folded its failures into the shared Finnhub error. The candidate makes no
   PQC requests when `quantum_readiness` is explicitly disabled, retains every
   configured query with `strategy_disabled`, and preserves the configured
   admission budget. A regression reproduced the extra six requests and failure
   coupling, then passed after the fix; the focused Finnhub/filing suite passed
   60 tests. Active earnings failures still fail the source. Existing enabled
   PQC behavior is preserved. Commodity catalysts retain the general company
   news collection; incidental PQC articles are no longer available when that
   consumer is disabled. This output-affecting scope change requires a new
   generation. The broader needed-source roster retains policy sentinels and
   independent audit/identity dependencies; it cannot simply drop every source
   named by a disabled strategy.

6. **Distinguish advisory review from a missing monitor.** The October 21 report's
   `source_reliability.catalog_status` was available, with valid observation IDs.
   Assessment was unavailable because the code deliberately emits
   `preflight_failure_requires_review` and `source_health_failure_requires_review`
   for canonical earlier failures. `source_incidents` itself was available with
   available history and explicit Congress/FRED incidents. This is conservative
   advisory status, not proof the monitor failed to run. The notification/repair
   consumers do not directly act on this advisory flag; verify the actual saved
   monitor receipt and error/recovery notification path separately, without
   sending a message or executing a repair under this audit authorization.

7. **Qualify deferred sources individually.** Full SEC and regulatory evidence
   have potential research value, but retirement is a reviewed scope choice, not
   evidence the data disappeared. Document access feasibility, complete material
   coverage, point-in-time availability and a consumer that can use that evidence
   before proposing a separate re-enable. No blanket re-enable is justified.

## Candidate CFTC and enrichment repairs

The [CFTC release schedule](https://www.cftc.gov/MarketReports/CommitmentsofTraders/ReleaseSchedule/index.htm)
supplies holiday-specific release dates and a 15:30 Eastern publication time;
the [COT documentation](https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm)
distinguishes Tuesday positions from later publication. Both the local process
and production owner fetched the calendar successfully, with HTTP 200 and 47,849
bytes. Candidate `cftc_source.py` acquires it through the existing killable byte
transport and shared provider budget, records its digest/acquisition/cutoff,
requires each commodity's expected published observation, and excludes an
unpublished Tuesday tail. Unknown/incomplete calendars and unavailable prior-year
coverage fail explicitly. A future calendar change/delayed report remains a
visible maintenance event. This adds one bounded official request.

Independent review exposed two additional publication-boundary defects, both
reproduced before repair: an in-memory archive's old acquisition timestamp could
hide a newly due report, and local calendar/positioning processing could finish
after the shared deadline. Current-call assessment now governs freshness while
the original archive clock remains in lineage; decision availability uses the
later validation clock. Calendar parsing, table/row traversal, positioning and
final publication all check the inherited provider budget. Four additional
regressions cover stale cache, formerly unpublished cached data and both local
processing deadline boundaries.

The [French library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html)
provides separate monthly/daily and three/five-factor products. The installed
OpenBB provider query model confirmed the active route's three-factor/monthly
default. Candidate `openbb_source.py` makes that request explicit, validates
finite required factors, rejects duplicate/malformed/future observation months,
sorts history and retains actual dates in committee context. No fixed publisher
release-day promise was established; monthly observations are not subjected to a
daily freshness rule. Profile failures are per symbol, preserving healthy batch
results and preventing complete-enrichment claims for empty classifications.

Red evidence: all ten initial CFTC publication tests failed against the original
implementation, and all eighteen initial enrichment semantic tests failed against
the original implementation. The new focused cases then passed after their
respective repairs. Broader source/transport/pipeline verification and any later
native optional probes are recorded in the final PR validation, rather than
claiming deployment or whole-strategy performance from these checks.

The later focused CFTC/FINRA/OpenBB selection passed 170 tests with one existing
websockets deprecation warning. An earlier wider source-contract/transport/
recovery/OpenBB sixteen-book selection passed 196 tests with one optional-SDK
skip and the same warning. FINRA subsequently added publication-aware
normalization, captured clocks and stale-cache rejection; final integrated
verification belongs to the final branch checks.

## Deferred sources: fresh access and actual document checks

The diagnostic ran under the production owner with the canonical production
tree mounted read only in a private mount namespace. It used existing
authentication without retaining credentials or raw logs, made no model calls,
and never activated the disabled policies. The sanitized source receipts are
[`2026-10-22-deferred-source-audit.json`](2026-10-22-deferred-source-audit.json).
Raw probe scripts/safe receipts are in the private scratch directory
`/var/tmp/eventedge-deferred-check-20261022`.

SEC submissions for one declared issuer returned 1,001 recent rows with a
matching CIK; its selected older archive validated 1,268 rows against the exact
descriptor count. Three real originals were acquired and hashed: a 1,520,323-byte
10-K with 1,170 inline XBRL nodes and two image references; a 38,465-byte 8-K with
44 inline XBRL nodes; and a 23,004-byte raw Form4 XML with a matching issuer CIK
and eleven transaction elements. The audit's first Form4 control used the
XSL-rendered view and failed XML parsing; switching to the documented raw
basename, as the existing native adapter already does, corrected that harness
error. None of these checks establishes complete filing material, full-market
coverage, comparator analysis or reliable model interpretation of graphics.

The actual complete-material Regulations source adapter independently exercised
the October 7–21 window:

| Agency | Discovered notices | Complete material | Remaining gap |
| --- | ---: | ---: | --- |
| FDA | 1 | 1 | This notice passed; no provider-wide claim |
| FTC | 1 | 1 | This notice passed; no provider-wide claim |
| EPA | 9 | 4 | Five distinct content/access/unsupported-shape failures below |
| SEC | 2 | 1 | One official correction is outside the original-notice parser contract |
| DOL | 0 | 0 | Complete empty discovery window; body parsing unexercised |
| CFPB | 0 | 0 | Complete empty discovery window; body parsing unexercised |

The baseline unsupported cases were traced to their actual originals:

- `SEC-2026-6502-0001` has FR number `C1-2026-20360`. Its official 1,297-byte
  GovInfo body is a correction to the October 5 original, changing the comment
  deadline to December 4. The strict parser expects an original `YYYY-NNNNN`
  notice and correctly refuses to pretend a correction is an entire new rule.
  Qualification needs the original/correction dependency and actual availability
  clocks; changing the identifier regex alone would be insufficient.
- `EPA-HQ-OW-1998-9208-0001` is a newly posted, PDF-only historical notice with an
  old `97–5879` FR number, a 1998 docket and HTTP 403 on its required primary PDF.
  The primary URL remains HTTP 403, but a subsequent bounded check retrieved the
  authoritative [March 11, 1997 GovInfo HTML](https://www.govinfo.gov/content/pkg/FR-1997-03-11/html/97-5879.htm)
  (80,480 bytes) and corresponding PDF (176,631 bytes, valid PDF header). The HTML
  identifies the exact FR number and pages 11270–11281. This is accessible
  historical material, not an unavailable source. Its 2026 posting timestamp
  cannot be used to invent an original 2026 rule date. The current document
  contract cannot yet bind a 1997 publication to this later repost and remains
  explicitly unsupported; access receipt:
  [`2026-10-22-archived-epa-access.json`](2026-10-22-archived-epa-access.json).
- `EPA-R10-OAR-2026-4291-0013` and `EPA-HQ-OAR-2024-0505-0951` name multiple CFR
  parts. Their official GovInfo bodies are accessible, but the current EPA
  identity grammar only supports one part. Preserve all parts and exact identity
  in any prospective extension rather than dropping inconvenient metadata.
- `EPA-R09-OAR-2026-5581-0001` has an accessible 39,918-byte official body whose
  agency line includes the valid `U.S.` prefix; the literal matcher lacks it.
- `EPA-R09-OAR-2026-5119-0001` has an accessible 83,856-byte official body with
  subscript notation `<INF>2.5,</INF>`. The extractor rejects that punctuation
  shape instead of silently flattening or changing it.

## Candidate regulatory repairs and remaining qualification

Four original official EPA detail/body pairs and the actual SEC correction were
retained byte for byte under `tests/fixtures`, with source URLs, capture time,
byte counts and SHA-256 values in `regulation_20261022_manifest.json`. The EPA
parser now accepts bounded, explicit lists of unique CFR parts, the observed
comma/dotted FRL docket forms and the valid `U.S.` agency prefix. Exact detail/body
identity checks still bind every part and the whole docket. Numeric subscript
notation can retain the observed single trailing comma; the extracted value,
source span, text span and downstream subscript annotation preserve that comma.
Ranges, duplicates, dropped parts, different authorities/dockets, malformed
notation and unsupported markup continue to fail.

The native candidate repeat used the same October 7–21 EPA discovery window and
complete-material contract. Eight of nine notices now have complete admitted
material (formerly four); the source graph validates, and its overall outcome
remains `batch_failure` because the historical notice is unresolved. It consumed
503,053 bytes in 13.607 seconds, with no model calls or production writes. The
receipt identifies the exact candidate parser digest and all admitted evidence
digests:
[`2026-10-22-epa-native-check.json`](2026-10-22-epa-native-check.json).

The current resource inventory binds one notice's detail, primary and attachments;
it has no cross-notice original/correction-chain dependency, publication history
or original resolver. Implementing an original lookup alone would therefore
produce a misleading complete graph. A new guard rejects `Cn-YYYY-NNNNN`
correction identities before primary acquisition, across all agencies, even if
the correction's own download becomes available. The actual SEC correction is
retained as regression evidence, not relabeled as an original. Independent
review reproduced the same admission hole with constructed cross-agency
corrections; these are regression controls, not claims of observed EPA/FDA/etc.
correction publications.

Remaining correction qualification requires a bounded, exact original lookup;
retention and independent identity validation of both original and correction;
all material attachments/graphics and correction-chain completeness; separate
publication/acquisition clocks; and replay validation that cannot treat either
document alone as a complete original. Historical reposts similarly need true
original-publication identity while preserving the observed repost clock. These
are explicit contract gaps; neither metadata-only substitution nor a relaxed
identifier matcher resolves them.

The four real EPA acquisition cases failed before the parser repairs and then
passed through full source admission, typed serialization/replay and assessment
request construction. A recovered-primary SEC correction and five constructed
cross-agency controls failed to be refused before their respective guard fixes.
After repair, the seven-module regulatory selection passed **215 tests**,
including fixture integrity, positive real bodies, malformed/identity controls,
immutability and correction refusal. No strategy/source policy was enabled;
full graphical-document/model qualification and any prospective re-enablement
remain separate work requiring a new generation.

## Wider DOL/CFPB representative check

A final read-only diagnostic widened discovery explicitly to October 21, 2025
through October 21, 2026, allowing at most one representative document per agency.
The October 7–21 windows above remain empty. It used the existing native discovery
and complete-material acquisition path, with frozen candidate parser/evidence
modules identified by digest. Only temporary diagnostic selection restricted
acquisition to the chosen actual notice; its graph explicitly says bounded
sample, incomplete discovery and incomplete source. No product code changed.

- DOL returned no eligible proposed rules in that year. The single publisher
  page contained all 60 historical rows and reached the older-than-window
  boundary. No actual recent DOL body could be selected within this bound;
  DOL body qualification remains unexercised, rather than passed.
- CFPB returned six eligible rows. The selected latest row was
  `CFPB-2026-0018-0001`, **Request for Information: Promoting Access to Mortgage
  Credit**, posted July 9, 2026. The publisher categorizes it as `Proposed Rule`,
  but its title identifies an RFI; it is not evidence of a newly finalized rule.
  Native primary HTML returned HTTP 403. The complete-material path retained
  that failure and refused the fallback with
  `unsupported_regulation_government_notice`: the government-copy contract
  currently supports EPA/FDA/FTC and separately SEC, not CFPB. The independently
  validated bounded source graph contains zero of one complete documents and
  does not admit model input.
- The same CFPB notice's authoritative
  [GovInfo body](https://www.govinfo.gov/content/pkg/FR-2026-07-09/html/2026-13834.htm)
  returned HTTP 200, 39,785 bytes and 38,666 visible characters. It contains the
  exact FR number `2026-13834`, docket `CFPB-2026-0018` and July 9 publication
  title. Detail metadata names pages 42382–42386 and **12 CFR Parts 1024 and
  1026**. This proves access to the actual official body, not current parser
  acceptance or complete-material qualification. CFPB would need its own exact
  agency/authority/notice-kind grammar and retained original regression corpus;
  the EPA-only multi-part repair must not silently authorize it.

The sanitized discovery, exact identities, native failure, body digest/access
observations and code digests are retained in
[`2026-10-22-dol-cfpb-representative-check.json`](2026-10-22-dol-cfpb-representative-check.json).
Production was mounted read only; the diagnostic made no model calls and enabled
no strategies or source policies. These findings preserve useful deferred
sources as concrete future qualification work, rather than classifying empty
windows or unsupported parser shapes as provider outages.
