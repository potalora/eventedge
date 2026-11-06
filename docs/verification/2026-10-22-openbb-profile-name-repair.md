# Missing OpenBB long name — 2026-10-22

ASIC was the sole rejected profile in the actual screened population plus four
benchmarks. Its native OpenBB row had the exact symbol, `EQUITY` issuance,
Financial Services sector, Insurance - Property & Casualty industry and finite
market capitalization, but `name` was null. An independent isolated probe found
Yahoo `longName` null and `shortName` present. Installed `openbb-yfinance` 1.6.3
maps `name` from `longName`; its extraction whitelist discards `shortName`.
The existing required-name rejection was correct for that projected row.

The candidate now completes that specific missing-field condition with an
additional same-provider quote. Only a null native name, explicit `EQUITY` or
`ETF` issuance, and otherwise valid native classification and metrics qualify.
Blank names, unsupported or absent issuance, invalid classification/metrics,
and mismatched native identities do not dispatch a lookup. Normal profiles
retain their shape and perform no extra request. Installed SDK files remain
unchanged.

The helper uses the inspected yfinance 1.7.0 `YfData.get_raw_json` path used by
its own quote scraper, requesting one symbol from Yahoo's quote endpoint. The
raw response must contain exactly one result, no provider error, the exact
requested symbol, matching `quoteType`, and a nonblank string `shortName`.
This bypasses `Ticker.get_info()` because its merge implementation overwrites
a returned symbol with the requested symbol. Neither ticker nor company names
are synthesized. Successful completion includes
`name_source: yfinance.quoteResponse.shortName` and
`name_fallback: native_long_name_missing`; native classification and metrics
remain authoritative for the profile. No alternate provider, ETF classification,
FINRA, factor logic or risk policy changed.

Each lookup uses the existing `openbb` provider budget and a five-second
operation cap. The raw helper receives a clipped timeout. A post-return
deadline check rejects late results before validation, success diagnostics or
cache admission. Transient failures, HTTP status and Retry-After use the shared
retry policy; typed diagnostics omit raw exception text and URLs. A scalar
completion failure returns its per-symbol error without retrying the completed
native profile call. Bulk siblings remain usable and unsuccessful completions
are not cached. Native cookie/consent negotiation is cooperative and may
outlast a socket timeout; this is not a hard process interruption guarantee.
An over-budget result still cannot be admitted. The raw helper is an internal
yfinance interface: incompatible upgrades fail observably rather than relaxing
identity validation.

The [sanitized receipt](2026-10-22-openbb-profile-name-repair.json) includes the
initial native/provider diagnosis and a fresh candidate run. All 15 requested
profiles succeeded in 5.321 seconds: AAPL, ADM, ASIC, BDN, CACI, CASH, DBA, MOO,
SLV, UNG, UNH, SPY, GLD, DE and VTI. Diagnostics recorded two native batches and
exactly one short-name lookup, each with one attempt. ASIC retained its native
sector, industry and market capitalization and used the attributed short name.
Provider descriptions are excluded from the receipt. Observations reflect
acquisition time and do not establish permanent provider availability.

Both diagnosis and candidate probes used new scratch HOME directories, an empty
environment and zero model calls. Production and SDK mounts were read-only;
the candidate snapshot was also mounted read-only. Only scratch provider state
was writable. Harnesses and raw results are under
`/var/tmp/eventedge-asic-profile-research-20261022` and
`/var/tmp/eventedge-asic-profile-candidate-20261022`.

Verification: 28 new tests in `test_openbb_profile_short_name.py` cover missing
names, bulk/scalar/cache agreement, normal/unsupported/invalid native rows,
wrong/missing/duplicate raw identities, wrong/missing issuance, invalid names,
retained siblings, expired and late inherited budgets, typed HTTP/timeout
failures, retry recovery, Retry-After and prevention of scalar retry
amplification. Initial tests failed before implementation; the additional
Retry-After regression also failed before response metadata was preserved.
Together with ETF/native/OpenBB/enrichment/committee/portfolio-policy tests,
177 passed; the only warning was the existing websockets deprecation.
Adding the five FINRA bulk/schedule/archive/admission/routing suites produced
248 passing tests with the same warning. `git diff --check` passed. No tests
depend on live missing-name requests.

This is an uncommitted development candidate, not deployed.
