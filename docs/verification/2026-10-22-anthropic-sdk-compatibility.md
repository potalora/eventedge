# Anthropic SDK compatibility — 2026-10-22

The application now declares `anthropic>=0.86.0,<2`. The corresponding
`uv.lock` requirement metadata has the same bound; its resolved Anthropic
package remains **1.11.0**, as does `requirements-ci-openbb.txt`. The existing
lower bound is unchanged. This is a development dependency repair, not a
production deployment.

## Evidence and scope

The standard CI environment for PR #56 resolved Anthropic 2.1.0 and failed three
existing regressions. Its pinned OpenBB environment used 1.11.0 and passed.
The three failures reproduce locally with Python 3.11 and isolated 2.1.0:

- `test_anthropic_existing_auth_and_mixed_provider_map_remain_exact` rejects
  simultaneous `api_key` and `auth_token` during SDK construction.
- `test_anthropic_auth_configuration_is_part_of_exact_client_namespace` fails
  for the same reason before it can verify credential separation.
- `test_native_anthropic_transport_preserves_configured_client_and_provenance`
  reaches its synthetic localhost server, but the expected `x-api-key` header
  is absent.

The upstream [2.0.0 release](https://github.com/anthropics/anthropic-sdk-python/releases/tag/v2.0.0)
explicitly announces a breaking API-version change. The tagged constants move
from [2023-06-01 in 1.17.0](https://github.com/anthropics/anthropic-sdk-python/blob/v1.17.0/src/anthropic/_constants.py#L7)
to [2026-10-21 in 2.1.0](https://github.com/anthropics/anthropic-sdk-python/blob/v2.1.0/src/anthropic/_constants.py#L7).
Comparing the tagged [1.17.0 client](https://github.com/anthropics/anthropic-sdk-python/blob/v1.17.0/src/anthropic/_client.py)
with the [2.1.0 client](https://github.com/anthropics/anthropic-sdk-python/blob/v2.1.0/src/anthropic/_client.py)
also confirms that 2.x sends API keys through Bearer authorization and makes
`api_key`, `auth_token`, `credentials`, `config`, and `profile` mutually
exclusive. In 1.x the supported static API key/token combination remains valid
and sends `X-Api-Key` and Bearer-token headers independently.

Selecting the existing API generation avoids changing these semantics to make
tests pass. No runtime code or tests were changed: exact credential namespaces,
configured endpoints and headers, deadline handling, provenance, and redaction
retain their existing regression coverage. Native custom HTTP-client objects
are not newly supported by this dependency repair; it does not expand the
disposable worker's existing constructor-property boundary.

## Verification

Run from this isolated repair checkout with `PYTHONPATH="$PWD"` so disposable
transport workers import the same checkout:

```sh
PYTHONPATH="$PWD" /path/to/python -m pytest -q \
  tests/test_candidate_parallel.py \
  tests/test_candidate_response_reuse.py \
  tests/test_runtime_model_budget.py
uv lock --check --offline
```

| Anthropic version | Result |
| --- | --- |
| 2.1.0, original three regressions | 3 failed, reproducing CI |
| 1.11.0, all three affected test modules | 82 passed |
| 1.15.0, all three affected test modules | 82 passed |
| 1.17.0, fresh resolver selection, all three affected test modules | 82 passed |

A fresh standard-CI resolver rehearsal with
`python -m pip install --dry-run --ignore-installed --report /tmp/eventedge-anthropic-compat-standard-resolution.json -e '.[chatgpt-plan]' pytest`
completed successfully, selecting **1.17.0** and reporting the application's
requirement as `anthropic<2,>=0.86.0`. Its generated report and log are local
scratch evidence; they are not project artifacts. The 1.17.0 row above tests
that actual SDK selection alongside the existing local application dependencies,
rather than claiming a full fresh-environment suite run.

The passing module runs each report one existing `websockets.legacy`
deprecation warning. The lock consistency check passes without changing any
locked package. Tests use synthetic credentials and mocked or localhost
transport; no authenticated provider or model request was made. Disposable
SDK environments leave the shared development and production environments
unchanged.

Future Anthropic 2.x support needs a separate API/auth migration with explicit
credential precedence, worker serialization, exact reuse identity, and wire
contract coverage. This verification does not qualify that migration, every
historical version above the retained floor, or the full repository test suite.
