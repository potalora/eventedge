# October 1 alerts and missed integration boundaries

The October 1 gen_016 daily worker completed with `outcome=degraded`,
`execution_valid=true`, and exit status zero. All 16 cohorts had valid completed
sessions and account snapshots. The alert described a failed run and stopped
before writing its daily report. That was a monitoring error, distinct from
the candidate and source problems below. Do not replay the session.

## What the evidence establishes

- ORN's reference bar was quarantined after two invalid observations: open
  8.569999694824219, low 8.649999618530273, high 9.239999771118164, close
  9.1899995803833. The open was below the low. One shared issue affected all
  16 cohorts; it was not 16 independent provider failures. A fresh October 2
  probe returned a coherent bar with low 8.569999694824219. This establishes a
  later provider correction, not a proven explanation for the original error.
- Weather/agriculture's configured MOO ETF was dropped after model enrichment.
  The engine checked every enriched ticker against SEC's company-only registry,
  including a deterministic ETF that the model had not selected. Company
  membership is the wrong gate for that configured instrument.
- During the October 1 screen preflight, FRED's CPI request returned HTTP 502
  and USASpending timed out. The later daily attempt fetched FRED and
  USASpending successfully; these were transient screening failures. The adapters
  returned empty data on failure. CourtListener had the same failure-to-empty
  pattern. The caller therefore could not reliably distinguish source failure
  from a successful request containing no events.
- The installed monitor interpreted `success=false` as failed and stopped.
  Modern degraded results intentionally have `success=false`; their typed
  `outcome` and execution/accounting evidence must determine the report.

The preceding three daily results were labelled clean. That proves the worker
outcome, not full upstream-source coverage. A source failure can leave completed
accounting valid while reducing the strategy's opportunity set.

## Why passing tests and simulations did not catch this

| Existing check | What it exercised | What it omitted |
| --- | --- | --- |
| 30-day simulation | Synthetic prices, broker/session lifecycle, idempotency and two-cohort divergence | Real provider decoding, configured weather strategy, real model enrichment and SEC company validation; APIs and committee are mocked |
| Weather strategy tests | Deterministic candidate creation, including configured ETFs | The subsequent model-analysis and ticker-validation boundary |
| Model/enrichment tests | Response parsing, confidence limits, rejection paths | The actual company-only registry; isolated boundary tests deliberately used an empty registry to prevent network access |
| Source-health tests | Engine error-envelope handling | Real adapters swallowing exceptions before an error envelope could exist |
| Screen preflight | Shared fetch, deterministic screens, event identity and observation time | LLM enrichment, candidate reference bars, volatility history and full staging; it runs with `use_llm=False` |
| Governed preflight | Its documented governed-market-data gate | A complete provider-to-candidate-to-committee trading rehearsal |
| Worker outcome tests | Valid degraded accounting and typed outcome serialization | The separately installed monitor's interpretation and report-writing behavior |

The September release's recorded acceptance was the non-live suite, live
provider/SDK probes and governed preflight. It did not establish that a full
live-input simulation or a future scheduled trading session would succeed.
Calling those checks a full production rehearsal would overstate the evidence.

ORN is a different case: existing market-data, lifecycle and reporting tests
already cover repeated incoherent bars, durable quarantine and valid degraded
completion. Production followed that safety behavior. Tests cannot guarantee
that a provider will return coherent observations tomorrow; they can verify
that bad observations are rejected and accurately reported.

## Corrections and acceptance boundaries

Ticker validation now applies to a ticker newly inferred by the model. A
configured candidate retains its instrument identity through enrichment. The
integration regression uses the actual weather screen, analyzer/Responses
parser and company-only SEC cache, replacing only external delivery. The MOO
case fails with the original engine and passes with the repair. Controls retain
validation for valid and invalid newly inferred company tickers through both
resolution fields.

Provider adapters surface failed requests through explicit, sanitized errors,
keep successful partial data and avoid caching failed requests as empty success.
Tests inject failures below the adapter, through real fetch, health and preflight
paths, alongside successful-empty controls. Optional enrichment must remain a
warning; missing required sources must fail screen preflight. These checks do
not add a new rule suppressing all candidates whenever any source fails.

Screen preflight remains an integration warning in the scheduled shell flow;
the governed preflight remains the hard execution gate. Source-health failures
also prevent healthy-readiness acceptance. A nonzero screen preflight is not a
claim that the shell unconditionally halts all trading.

The private monitor contract separately requires exact-session evidence,
authoritative typed outcomes, per-cohort accounting validation, independent
source-health alerts and continued reporting for validated degraded runs.
Ledger counts must use distinct IDs at an explicit cohort/session scope;
compatibility projection rows and repeated scenario signals are not unique
lots or opportunities. Prompt review is a consumer-contract check, not proof of
the next scheduled agent's behavior.

The release therefore needs both the offline suite and operational evidence:
reviewed immutable generation code, an empty new generation, preserved old
state, live provider/preflight checks, and subsequent scheduled-session and
monitor output. Any failed or incomplete check must remain visible rather than
being summarized as a fully healthy system. The configured-ETF correction
changes the opportunity set, so deployment requires a new generation.

## Recorded offline validation

Final non-live suite: **2,403 passed, 4 live tests deselected** in 100.34 seconds,
with one existing websockets deprecation warning. This includes five ETF/model
identity cases and 40 provider-boundary cases. Independent review passed 133
focused tests and found no remaining blocker. Critical Ruff and whitespace
checks passed. The real `fredapi` XML-error and malformed-HTML wrapper cases
were reproduced below its HTTP boundary before repair; safe series/status
diagnostics now survive both wrappers without leaking provider bodies or keys.

These results are offline acceptance. They do not record a deployment, a live
trading replay, or a completed scheduled run of the new generation.
