# Source repair acceptance — October 22, 2026

These are prospective development changes. They have not been deployed or
enabled in production. The native diagnostics used read-only production code,
state and SDK mounts, isolated writable scratch directories and no model calls.
The source/prompt/admission changes require a new generation after review and
explicit deployment authorization.

## Repairs

| Area | Result |
| --- | --- |
| FRED | Recognize the observed HTTP-200 maintenance page inside the bounded retry path; preserve the actual status and failed-attempt diagnostics through preflight, frozen source evidence and incidents. A manual source-only check exercises the nine configured series without changing accepted state. |
| USAspending | Reviewed recipient-specific ownership resolves seventeen of eighteen previously unresolved awards. The observed 37-award population has ten verified listed targets, 26 verified dispositions with no listed target, and one unresolved recipient. Older review dates and raw parent names are retained. |
| Congress | Separately retain bounded official originals for the two missing House filings while preserving the publisher discrepancy and its original snapshot. No extracted transactions, signal authority or historical availability are inferred from those PDFs. Repair commands now report missing or mismatched session evidence explicitly. |
| CFTC | Require the report due under the official holiday-aware release calendar; reject stale cache contents at a later release boundary and enforce the shared deadline during local processing. All five native commodity checks returned the expected October 13 observations. |
| FINRA | Separate current-cycle coverage from historical inventory, correct the inspected SDK's holiday dates, avoid unnecessary historical backfill, and route scalar callers through bounded bulk acquisition. Missing or stale symbols remain explicit failures. The reviewed 2026 publication calendar expires at the next calendar year. |
| OpenBB profiles and factors | Reject malformed numeric/classification evidence, preserve actual monthly factor dates, represent observed ETFs without invented corporate sectors or holdings, and retain the existing Unknown risk grouping. Missing native long names can use an explicitly labeled Yahoo short name only after independent exact-symbol/type validation under the inherited deadline. |
| Finnhub | Skip PQC requests when its consumer is explicitly disabled, retain every configured exclusion, and preserve active earnings/supply-chain acquisition. Earnings prompts now describe their actual news/EPS evidence. |
| Regulatory originals | Preserve complete material for the observed EPA multi-CFR, agency, FRL and subscript variants. Native complete coverage improved from four to eight of nine notices. Corrections across agencies remain explicitly unqualified until an original/correction dependency can be represented and replayed. |

The [source audit](2026-10-22-source-audit.md) maps every registered or auxiliary
source to its actual consumer and records the deferred capabilities separately.

## Native evidence and regression checks

- Final combined repair regression selection: 503 tests passed after the
  missing-name repair and independent review, with one existing websockets
  deprecation warning. It includes the FRED, awards, Congress, CFTC, FINRA,
  OpenBB profiles/factors, Finnhub exclusions and regulatory-original changes.
- [Candidate source preflight](2026-10-22-candidate-source-preflight.json): both
  screen and governed benchmark checks passed for the October 21 reference
  session, with the declared strategy exclusions intact. A forward diagnostic
  does not close an earlier canonical incident or prove historical availability.
- [Cold optional acquisition](2026-10-22-candidate-optional-sources.json): all six
  short-interest symbols returned the currently published September 30 cycle
  in 0.406 seconds; historical inventory remained explicitly incomplete.
  Fama–French returned twelve dated monthly rows ending August 2026.
- [ETF profile repair](2026-10-22-openbb-etf-profile-repair.json): all six stock
  and ETF profiles passed in a fresh isolated candidate check.
- [Actual screen population](2026-10-22-candidate-population-enrichment.json):
  eleven deterministic candidate symbols plus four governed benchmarks;
  all fifteen FINRA results passed. Fourteen profiles passed, while ASIC's
  missing native long name remained explicit in this observation. The subsequent
  [missing-name repair](2026-10-22-openbb-profile-name-repair.json) passed all
  fifteen profiles in 5.321 seconds, using exactly one attributed name lookup.
- [EPA candidate check](2026-10-22-epa-native-check.json): eight complete
  originals, one retained historical-repost gap, and a validated partial source
  graph. [Archived access evidence](2026-10-22-archived-epa-access.json) confirms
  that the 1997 original itself is available from GovInfo.
- Installed production-SDK environment: 186 focused FINRA/OpenBB/ETF and
  sixteen-book replay tests passed under read-only mounts, with three existing
  dependency deprecation warnings. No optional-SDK import skip occurred.
- Independent reviews reproduced and closed CFTC cache/deadline defects and a
  cross-agency correction-guard gap. The final regulatory/ETF review selection
  passed 203 tests with no remaining findings in that scope. The later profile
  name completion review passed 195 tests with no findings; these selections
  overlap and are not additive test totals.
- Recorded NOAA replay passed with no network or subprocess calls. Public
  historical source acceptance also passed with no network/subprocess calls;
  its private-corpus gap remains explicit, so it does not claim full supported
  historical-source acceptance.

## Broad suite results and fixture corrections

The complete remaining non-live invocation finished with **9,597 passed,
five skipped and four fixture failures** in 3,767.23 seconds. Three failures
used older FRED response doubles without byte content; those now use real
`requests.Response` bodies while retaining vintage, missing-value and safe-error
assertions. The fourth still required CFTC acquisition and availability clocks
to be identical; it now supplies an offline release calendar and verifies both
the original acquisition and later publication-validation availability.

All four failed cases passed after correction: the combined FRED/provider/runtime
selection passed **115 tests**, and the CFTC/source/transport selections passed
**189 tests**. Independent reviews found no issues. The five full-suite skips
were three unavailable retained/private fixture cases, the optional-SDK import
case in the local environment and a Linux-root cross-UID permission case. The
installed-SDK check above separately exercised that SDK environment. The 28 new
profile-name cases were added after this broad invocation collected its tests
and passed in the 503-test repair selection. These overlapping runs are not a
single clean full-suite invocation or additive test totals; no observed local
test failure remains unresolved.

The complete scheduled-workflow invocation initially passed 358 tests and failed
four: the three exact stabilization-profile variants and the bounded native
NG/SI fixture test. Its external-SDK fixtures still used an impossible FINRA
October 13 settlement, omitted the native current-archive download interface,
and supplied the old factor-field name. The corrected fixtures use the published
September 30 cycle, native date/field shapes and explicit incomplete historical
coverage. Unexpected archive downloads still fail.

The third profile also needed an explicit response for its constructed missing
House filing. That fixture now supplies synthetic PDF bytes only for its exact
fictional URL and configured audit-gap scenario. The missing publisher ID,
incomplete coverage and disabled signal/model/order authority remain unchanged;
unconfigured URLs still fail. No production validators or timeouts were relaxed.

After those corrections, all three exact profile variants passed their complete
two-session, positive-fill, continuity/readiness and zero-I/O replay tests:
v1 in 346.97 seconds, v2 in 389.86 seconds and v3 in 396.64 seconds. The two quick
native-SDK/original-acquisition checks passed together in 9.76 seconds. Independent
reviews found no issues and separately reproduced both quick checks. The 32
new correction-refusal cases were added after the original scheduled invocation
collected its tests; they passed in the 503-test repair selection above.

The hosted standard and installed-OpenBB jobs on the first PR commit each
passed 390 scheduled cases and failed the same four fixture cases. Their logs
contained no additional failures. The updated PR triggers both hosted jobs
again; their current outcome is available in the PR checks.

## Remaining qualification boundaries

Congress still has a publisher reconciliation gap and unverified Senate/extracted
transaction coverage. USAspending retains the unresolved Lydig recipient. The
1997 EPA repost needs distinct original-publication and posting identities;
corrections need a versioned original/dependency graph and availability clocks.
The representative CFPB original is accessible but outside the supported
government-copy identity contract. DOL had no eligible proposed rule in the
explicit one-year discovery window, so no representative body was qualified.

SEC originals were accessible for the tested issuer, but that does not qualify
bulk discovery, comparator histories, complete graphical material or model
interpretation. Futures curves and state-event exposure still lack their
required economic/identity contracts. These capabilities remain disabled.

Source observations and passing offline tests establish the recorded contracts
and cases. They do not establish strategy profitability or a completed trading
cycle for a future generation.
