# NOAA source failure diagnostics

An isolated native observation on October 18 exposed lost failure metadata. The
NOAA public bulk workload failed after its existing 180-second acceptance
deadline: 201 read timeouts and one HTTP 503 occurred across 202 weather HTTP
attempts. Both catalog requests succeeded. No weather JSON response or accepted
weather observation was available. These counts describe the retained diagnostic
trace, not values to infer from an aggregate exception.

The NOAA engine adapter previously retained a safe error string and partial
data but discarded the typed failure reason and per-state failures. Nested
provider budgets also allocated a separate diagnostic list. Consequently,
preflight could display `provider_error`, zero attempts and zero operations even
after actual requests failed. This is an observability defect; the failed source
was still rejected.

The adapter now preserves safe `_coverage` failure fields. A nested budget for
the same provider inherits the existing diagnostic sink when no explicit sink
is supplied. Unrelated providers and explicitly selected sinks remain isolated.
No deadline, retry, rate-limit, roster or source-acceptance rule changes.

This branch starts from `origin/main` at
`5611ad84c60712671d9fe0240f7f5f20caaf473a`. That base has the older sequential
NOAA CDO implementation. The public bulk source on candidate
`7813e2ef2565c135e5b62f21ff865a8f82ee3c80` additionally needs explicit context
propagation into each executor task. That separate applicability patch is
excluded from this PR because the bulk implementation is absent from its base.
This PR does not claim to repair the complete bulk-worker request rollup.

The regressions replace only HTTP responses with the observed 503 and timeout
classes. They exercise native acquisition, the NOAA adapter, screen preflight,
wire normalization and managed-result parsing. Source success remains false;
typed batch failure, bounded request attempts and operation counts survive.
Private-looking URLs, token headers and response cookies are excluded from
retained metadata. Separate checks cover narrowed-budget inheritance and sink
isolation. Raw native bodies, credentials and runtime state are not fixtures.

The test-only clock fixture in `test_primary_sip_price_source.py` matches the
existing stabilization candidate. It fixes two independently reproduced stale
fixture failures on this older base and does not change production clocks.

Focused validation:

```bash
python -B -m pytest -q -p no:cacheprovider \
  tests/test_noaa_source_diagnostics.py tests/test_request_policy.py \
  tests/test_preflight_source_diagnostics.py tests/test_primary_sip_price_source.py \
  tests/test_weather_ag.py tests/test_preflight.py tests/test_preflight_state.py \
  tests/test_shell_preflight_contract.py
```

The native observation remains failed. No production deployment or native
rerun is part of this metadata repair.
