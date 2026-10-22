# FRED maintenance and recovery evidence

On October 21, the scheduled source acquisition rejected all nine shared macro
series. The provider returned HTTP 200 with a branded St. Louis Fed maintenance
page instead of JSON. The old request layer recorded successful HTTP requests;
the adapter later rejected the body as `invalid_response`. That lost the useful
maintenance diagnosis and skipped the existing transient retry policy.

The candidate adapter recognizes the small branded HTML maintenance response
inside the shared request budget, before recording success. It raises the fixed
`provider_maintenance` reason and preserves the actual HTTP 200. The request
policy applies its existing maximum attempts, pacing, Retry-After and absolute
deadline. Unrecognized HTML, malformed JSON and invalid observation contracts
remain failures. Neither partial pages nor failed observations enter the cache.

Single-series, historical and batch wrappers retain attempts and per-operation
HTTP status. Preflight normalization, frozen capability validation, state-level
coverage and incident assessment retain the new reason. A transient maintenance
incident remains degraded initially; existing distinct-session escalation and
fresh accepted-session recovery rules continue to apply. A standalone probe
cannot close an incident or retroactively validate a failed trading session.

The manual observation command uses a fresh native adapter, all nine configured
shared macro series, an 18-month lookback and one 60-second budget:

```sh
python scripts/check_fred_source.py --as-of 2026-10-21
```

It reads `FRED_API_KEY` from the environment. Do not place credentials on the
command line. It prints fixed safe JSON with acquisition clocks, vintage,
observation counts and latest observation dates, and exits nonzero for missing
credentials, empty observations or any failed series. It does not instantiate
the trading engine, call a model, write generation state or alter scheduling.
Observed period age is reported separately; monthly economic observation dates
are not mistaken for daily publication dates.

The candidate passed a fresh nine-series request on October 22,
08:21:47–08:21:52 UTC, using vintage October 21. This establishes current source
availability under the adapter contract. The October 21 scheduled run remains
failed research evidence. The local sanitized result is retained with the
source audit handoff; no production code or state was changed.

Regression tests reproduce persistent maintenance, maintenance followed by valid
data, unrelated HTML, malformed JSON, history/batch propagation, secret-safe
diagnostics, retry/deadline bounds, preflight normalization, frozen capability
selection and accepted-session incident recovery. The broader focused source,
preflight and operational-report suite passed 374 tests.

No alternative macro provider is enabled. A replacement would need equivalent
series definitions, point-in-time vintages, release availability and explicit
source identity. Reusing stale values or changing providers silently would not
restore the documented input contract. Any deployment of this candidate belongs
to a separately authorized new generation.
