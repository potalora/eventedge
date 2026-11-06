# OpenBB ETF profile admission — 2026-10-22

The candidate adapter now admits profiles whose native `issue_type` is exactly
`ETF`, while preserving the existing corporate profile contract. The native
provider returned SPY, GLD and VTI with names and ETF issuance type, but no
corporate sector, industry or market capitalization. Requiring a corporate
sector had incorrectly rejected these otherwise usable asset identities.

The inspected installed `openbb-yfinance` 1.6.3 model maps Yahoo `quoteType` to
`issue_type` and describes it as the asset's issuance type. The
[official profile reference](https://docs.openbb.co/odp/python/reference/equity/profile/)
documents this provider field; the inspected installed model and independently fetched native
rows are the evidence for this repair. No inference from ticker or fund name is
used. Native names are retained verbatim, including the provider's VTI name.

ETF results include `asset_type: ETF`, `corporate_sector_status: not_applicable`
and `lookthrough_sector_status: unknown`. They omit `sector` and `industry`.
Absent market capitalization remains null; supplied capitalization still must
be finite, nonnegative and nonboolean. Nonempty name, exact symbol, unique row,
and all ordinary corporate sector guards remain enforced. Other issuance types
receive no new exception. This supplies asset identity, without holdings,
sector weights or commodity exposure. The committee prompt renders this
distinction explicitly. Existing risk code continues to map the omitted sector
to `Unknown`; no risk limits or allocation policies changed.

The [sanitized receipt](2026-10-22-openbb-etf-profile-repair.json) contains both
the native identity probe and a fresh candidate adapter probe. All six requested
symbols succeeded in 4.829 seconds: AAPL, OMC and DE with corporate sectors;
SPY, GLD and VTI with explicit ETF classification. Provider descriptions were
omitted from the receipt. Versions were `openbb-yfinance` 1.6.3,
`openbb-core` 1.6.13 and `yfinance` 1.7.0. These are acquisition-time provider
observations, not issuer-verified holdings or a permanent availability claim.

Both probes used separate scratch directories, a new isolated HOME, an empty
environment and zero model calls. A mount namespace made production, the native
SDK environment, and the candidate code snapshot read-only. Only scratch
provider state was writable. No production code, state or service changed.
Scratch harnesses and raw receipts reside under
`/var/tmp/eventedge-etf-profile-research-20261022` and
`/var/tmp/eventedge-etf-profile-candidate-20261022`.

Verification followed a failing-first regression: three expected failures for
ETF admission, prompt context and risk integration; ten rejection cases already
passed. After the repair, 149 tests passed across `test_openbb_etf_profiles.py`,
`test_openbb_native_enrichment.py`, `test_openbb_source.py`,
`test_enrichment_diagnostics.py`, `test_committee_vehicle.py` and
`test_portfolio_policy.py`. The only warning was the existing websockets
deprecation. Tests cover native bulk/scalar agreement and cache reuse, invalid
identity/name/metrics/issuance, preserved corporate rendering, and the unchanged
Unknown-group correlated-short rejection. An older FINRA schema-v1 replay
fixture was corrected to omit the current factory's two schema-v2 fields,
preserving its original historical compatibility assertion.

The final combined run added the FINRA bulk, schedule, archive coverage, current
admission and scalar/bulk routing suites: 220 passed, with the same warning.
`git diff --check` also passed.

These are development changes, not deployed.
