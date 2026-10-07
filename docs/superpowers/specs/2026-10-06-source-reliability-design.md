# Source reliability and verifiable daily completion

## Approved scope

Pedro approved the October 6 recommendation to qualify Alpaca SIP for critical
daily prices, repair EDGAR, add coordinated retries and persistent input caching,
remove the fragile congressional scraping fallback, and require complete source
evidence before calling a run clean. The earlier recommendation also includes
incident replay through production orchestration and deterministic reporting.
Keep all twelve strategies and sixteen scenario books. Research Clef and Jev
separately; this release does not change decision models or prompts.

This implements the source and reporting portions of the September 6 matrix
reliability design. Preserve ledger authority, accepted economic inputs, exact session identity, and
risk constraints. Retain historical evidence without maintaining old behavior.

## Evidence

The September 21–October 6 audit found repeated EDGAR search HTTP 500s,
CourtListener timeouts, older FRED 502s, USASpending and NOAA timeouts, and
incoherent Yahoo bars. The older USDA 500 incident was reproduced as a malformed
request and already repaired. No explicit 429 was found in the inspected logs.
On October 6, a read-only quota check confirmed CourtListener limits of 5/minute,
50/hour and 125/day; recent daily use was usually nine calls. Exact historical
Alpaca SIP probes returned coherent raw bars for AYI/BR on October 2 and AYI on
October 5. This verifies present coverage, not availability at the earlier run.

An offline HTTP-500 fixture reproduced EDGAR becoming empty healthy data and a
passing preflight. Other adapters contain equivalent error-to-empty paths.
Daily health persistence currently checks identities without carrying all failed
statuses into the outcome; the monitor separately interprets logs and can omit
incidents. These are application defects independent of upstream availability.

## Provider boundary and recovery

Every required operation distinguishes successful empty data, successful data,
partial data and failure. Preserve valid partial results and safe structured
failure identities. Never cache a failed/partial fetch as successful empty data.
Existing source-specific payload shapes remain compatible with strategy inputs.

Use one small shared request policy with a monotonic acquisition deadline,
bounded attempts, provider pacing and jitter. Retry transport failures, timeouts,
429 and eligible 5xx; respect Retry-After and configured rolling limits. Stop on
authentication/authorization errors, malformed requests and invalid payloads.
No retry begins after its deadline; request timeouts and waits consume the same
budget. Do not blindly layer retries over an adapter's existing retry loop.
Diagnostics contain provider, operation, status/reason, attempts and recovery;
never raw exception URLs, credentials or response bodies.

EDGAR uses filing-type filters separately from full-text keywords and validates
the response schema. Preserve CIK/submissions/XML paths for Form 4. Prefer the
working FMP stable congressional feed; do not silently fall back to parsing
CapitolTrades' generated website internals. Missing FMP access becomes explicit
coverage failure, not a fabricated empty disclosure list.

## Price policy

Introduce an explicit versioned Alpaca SIP raw daily-bar policy for new
generations. The existing adapter enforces exact symbol/session, raw SIP,
coherent OHLC, no remapping or pagination, and close plus fifteen minutes.
Use it for critical raw daily execution/reference and candidate bars. Preserve
the current Yahoo dividend-adjusted benchmark close series and corporate actions;
do not mix SIP raw prices with a separately sourced adjustment factor.
Do not silently fall back to Yahoo for those prices. Prioritize the current pipeline; do not add compatibility paths for retired
generations. Existing historical evidence remains untouched.
Yahoo may still supply research history, volatility and VIX where declared;
document those remaining dependencies. Accepted primary SIP bars are normal
inputs under the new policy, not retrospective recovery of old observations.

## Reusable and immutable inputs

Persist successful provider inputs using a bounded, versioned JSON codec that
supports the existing pandas/date/decimal payloads without executable pickle.
Cache identity includes source, exact requested session/window, configuration
fingerprint and contract version. Freshness and the acquisition cutoff must be
checked before reuse. Corrupt, mismatched, future-dated or expired cache entries
never authorize data use. Operational preflight may populate a separate cache;
it must not write manifests, generation ledgers or accepted economic inputs.

The daily pipeline freezes its accepted shared source bundle once, before model
analysis, under the generation/session identity. A permitted resume reuses that
bundle. It never substitutes later source values into an accepted session. Additional
validated volatility history is frozen separately before the first staging
context is accepted and must be present on an interrupted resume.
Unresolved source failures remain visible; affected coverage is degraded while
valid existing-position accounting can remain valid. Required governed price or
ledger failures retain their stronger blocking behavior.

## Outcome and reporting

Carry required-source and strategy-health failures from the actual daily inputs
into every affected cohort and the generation summary. Keep accounting validity,
staging completion and input coverage separate. Optional enrichment may warn
without becoming a required-input failure. Legitimate no-event requires a
successful source result. A recovered transient request is disclosed separately
from unresolved missing coverage; do not rewrite historical outcomes.

Generate the daily operational report from exact-session attempt artifacts,
manifest identity, ledger rows and strategy health with deterministic code.
Count distinct attempts and fills, retain preflight incidents separately from
the daily outcome, verify all sixteen books, and write reports for valid
completed degraded runs. Missing or conflicting evidence is explicit. Existing
metric reports remain available; this report adds exact-session operational
truth and can be consumed by the monitor without asking an LLM to count rows.

## Acceptance and delivery

Offline tests replace external transport and model responses at their boundaries,
not the production adapters or orchestration. Cover HTTP 500 then recovery,
exhausted timeout, throttling, malformed successful responses, successful empty
results, partial data, cache corruption/expiry, invalid prices, unsupported symbols,
interrupted/resumed work and duplicate invocation. Check source evidence,
strategy health, cohort outcomes, ledger validity, CLI output and report agree.
No duplicate fills/costs or modified accepted historical inputs are permitted.

Run focused tests, the complete non-live suite, appropriate lint, independent
review and PR CI. Keep live probes separate from deterministic test evidence.
Prepare a reviewed topic-branch PR. A production release requires a new frozen
generation and the repository's explicit deployment authorization; the October 2
merge/deployment of PR45 is already complete. Prospective continuity requires
observed sessions and cannot be asserted from offline tests or from time passing.
