# Prospective FRED state unemployment policy

`state_unemployment_v1` is an explicit opt-in replacement for the retired national-data/ETF-momentum proxy. Default configuration remains disabled. This is code readiness, not deployment or performance evidence.

## Acquisition and clocks

The shared FRED acquisition requests all 50 states plus DC (`ALUR` through the explicit 51-entry registry), in addition to the existing nine national macro/credit series. The minimum request count is 60; pagination and transient retries consume the existing shared acquisition deadline and the official 120-request/minute pacing allowance. No source or model budget is increased.

Both native snapshots and vintage histories require FRED `count`, `offset`, `limit`, and every observation's `realtime_start`/`realtime_end`. Paging must exhaust a stable declared total. Truncation, repeated rows, changing totals, malformed dates and nonfinite values fail explicitly. Existing pandas Series return values retain observation/vintage/acquisition metadata in `attrs`, which the immutable codec preserves.

State-history requests start on the first day of the requested calendar month: FRED includes the containing monthly period even when a request starts midmonth. Native snapshots reject future observation dates and rows whose real-time interval excludes the requested vintage. Lower bounds use known monthly/daily frequencies; arbitrary series frequencies are not guessed.

State discovery uses original FRED vintage dates for monthly observations and revisions. Observation month, vintage date and acquisition availability remain separate. Date-only vintage dates never become invented publication timestamps. Availability is the actual forward acquisition time; freshly reconstructed historical acquisitions cannot create historical trades. Recent vintages are limited by an explicit 1–31-day release window, default seven days. An old observation is not bootstrapped into a fresh release. Baselines use exactly the preceding calendar month valid at the event vintage. Missing baselines remain non-actionable.

Coverage is accepted only for all 51 states. Failed state identities and safe failure codes remain visible, successful prefixes are retained for journal evidence, and incomplete state sources cannot enter the success cache. A state-specific failure does not rewrite independently valid national macro values. The FRED payload, including reviewed registry, must fit the unchanged default 16 MiB/500,000-node codec; overflow retains bounded explicit state failure and valid macro inputs.

## Reviewed issuer exposure

The registry is an explicit, reviewed assertion of operating geography bound to original sources. Keyword presence is not semantic proof, geographic association is not a causal return model, and headquarters/incorporation are not operating exposure.

A manifest has policy `verified_state_operating_exposure_v1`, `documents` keyed by accession, and `exposures`. Each document retains a complete original SEC submission as base64, original SHA256, exact SEC complete-submission URL, form (`10-K`, `10-Q`, or `8-K`), filing date and acquisition time. Optional `required_exhibits` declares exact filenames/types to the existing filing parser. At most eight originals and 8 MiB aggregate raw bytes are allowed; the full serialized FRED source must still fit 16 MiB. No registry network calls occur during screening/replay.

Each state/common-share symbol has exactly one reviewed exposure binding; multiple supporting-document entries cannot duplicate the same economic event. Each exposure binds state, exact FILER CIK, exact common-share symbol, source accession/unit/text hash, exact geography and common-stock quotations with offsets, a declared operating fact/type, `verification_basis=reviewed_source_registry`, review time, validity start and explicit prospective review expiry. Optional `security_unit_filename` and `security_text_sha256` bind a securities-cover quote separately from an earnings-exhibit geography quote. Complete originals reconstruct through the existing filing parser/evidence validator. Obvious negated/headquarters geography and preferred/depositary securities counterexamples fail; these checks do not replace review.

The frozen SEC symbol map carries its exact SHA256 and actual post-acquisition/copy availability clock; this dependency can delay state-event availability. Publication and acquisition must precede decision availability; exact symbol/CIK must match the current frozen SIP universe and SEC symbol map. Retained historical rosters are test fixtures, never current eligibility. Registry validity is checked even between releases, then checked again for directional state signals at the final staging cutoff. A small issuer registry does not claim nationwide issuer coverage.

## Analysis, health and replay

Unmatched releases are neutral journal-only economic events with `no_verified_state_exposure`; complete economic coverage may still be healthy with no matched issuer. Reviewed matches start neutral with zero score and require a dedicated bounded assessment. There is no national ETF, price-momentum fallback or automatic direction heuristic.

The assessment contract binds the native event ID, supplied exact ticker and exact operating-geography quote. Neutral is a valid result. Missing, malformed, wrong-target or timed-out analysis remains journal-only failure. The existing shared 2400-second model deadline, admission, accounting and source capture mechanisms remain in use.

Native event identity is stable across acquisitions: state/series, observation period and original vintage. Availability uses the actual acquired evidence clock. Legacy identities remain readable for frozen historical records.

Every state health record retains complete source scope and economic-event reports, including no-match rows. Completed replay and operational reporting recompute source coverage, original-source exposure bindings, event rows and assessment contracts from frozen inputs with zero source/model calls. Mismatched, missing, duplicate or relabeled event evidence fails validation.

## Verification and primary sources

Offline tests cover strict native envelope completeness/pagination, original/revised metadata and codec round trips; all 51 series; missing states, empty series, exact-month baselines and historical reconstruction refusal; complete original/quote/CIK/common-share binding and counterexamples; 8-K primary/exhibit bindings; required neutral/malformed assessment; no-event registry failure; immutable zero-call replay; full-cutoff expiry; cache refusal; FRED pacing/deadline; and codec overflow without national macro loss.

Primary semantics: [FRED real-time periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html), [observations/pagination](https://fred.stlouisfed.org/docs/api/fred/series_observations.html), [vintage dates](https://fred.stlouisfed.org/docs/api/fred/series_vintagedates.html), [release dates caveat](https://fred.stlouisfed.org/docs/api/fred/releases_dates.html), [API errors/request limits](https://fred.stlouisfed.org/docs/api/fred/errors.html), [BLS LAUS FAQ](https://www.bls.gov/lau/laufaq.htm), and [BLS revision information](https://www.bls.gov/sae/tables/revisions/home.htm). These establish data/revision semantics, not a trading edge.

## October 11 source verification

The first fresh source diagnostic retained 60 successful HTTP responses but failed all 51 state histories. FRED includes the containing monthly period when a requested lower bound is midmonth. The state query now requests the first of that month; the evidence validator continues to require exact history bounds. Independent review also repaired contradictory snapshot observation/vintage acceptance and duplicate reviewed bindings for one state/security. All original failure evidence remains retained.

A new source diagnostic at 22:22 UTC then passed all **51 state/DC series and nine national series**, with 60 HTTP 200 responses in **25.46 seconds** and a 1,716,861-byte encoded source payload. The seven-day vintage window contained zero new releases. Complete raw response and request inventories, hashes, application hashes and codec output are retained privately in `data/forward-readiness/fred-state-repair/source-probe-20261011T222230Z/`. Its source payload SHA256 is `ba85e6d09a8c7254e9a9b164970fba394dd3e0136a607525fd64d6d8083fc143`.

A separate 22:24 UTC source-binding diagnostic acquired the complete 33,563-row SIP asset master and SEC company map and verified one reviewed exposure: Texas operations of Cullen/Frost common equity, **CFR**, CIK `0000039263`. The exact common-share and operating-geography quotations reconstruct from complete SEC 8-K `0000039263-26-000045`, including its required earnings exhibit. The company's preferred security is not substituted. The final dependency availability is the actual company-map acquisition time, `2026-10-11T22:24:33.347114+00:00`. The closed private evidence is `data/forward-readiness/fred-state-repair/exposure-probe-20261011T222431Z/`.

Independent review passed 523 broader offline tests and independently verified the fresh FRED response manifest. These diagnostics made **zero model calls**. They establish working state acquisition and one current reviewed issuer binding, not nationwide issuer coverage, a validated directional state assessment, the complete sixteen-book operational run, immutable full-run replay, profitability or deployment. Those acceptance gates remain separate.
