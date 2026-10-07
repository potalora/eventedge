# Clef evidence shadow verification

Clef classifies whether retained public evidence supports a candidate claim and
company attribution after every scenario book has finished staging. It records a
bounded sample separately from financial state. Its output cannot change trade
selection, sizing, account validity, required-source health or accepted inputs.

The hosted request follows Cloudflare's documented
[`@cf/cloudflare/clef` contract](https://developers.cloudflare.com/workers-ai/models/clef/).
Three explicit answer options cover support, contradiction and insufficient
evidence. Source text is data rather than instructions; generated rationales are
claims to check. Numeric probability validation cannot establish semantic accuracy.

## Verification

- Full non-live suite: `python -m pytest -q -m 'not live' tests` — **2,680 passed,
  four deselected**. One existing websockets deprecation warning remains.
- Actual 16-book pipeline comparisons produced identical financial results and
  accepted source bytes with the shadow disabled, enabled and returning HTTP
  failures. Shadow settings are excluded from source and metric-epoch identity.
- Adapter and persistence tests cover credentials, response identity, option
  coverage, finite probabilities, request/body limits, immutable attempted
  records, saved-input retries, invalid sidecars and incomplete resumed scope.
- Independent review reproduced a nested-JSON failure that could escape into
  financial reporting. Reader handling and an outer optional-report boundary
  now contain it; regressions preserve the original corrupt bytes and a clean
  financial report. The review found no remaining blocking issue.
- Python compilation, shell syntax and `git diff --check` passed. No dependency
  declarations changed.

## Operating limits

The default sample is at most five events, with a 20-second session budget and a
three-second request limit. Attempt reservations are persisted before a paid
request, so interruption may sacrifice an answer but cannot silently repeat the
request. Missing credentials and exhausted budgets can retry saved unattempted
inputs. API errors and timeouts remain recorded attempts.

An interrupted session that resumes without every horizon records
`incomplete_sampling` unless it already has a complete saved sample. Missing
source evidence is explicit and does not trigger a model call. The operational
report keeps these statuses separate from financial validity.

No live Cloudflare call or semantic-quality evaluation was performed for this
verification. Credentials, entitlement, deployed response shape and latency still
require a credentialed check. The hosted model alias is recorded but does not
pin an immutable weights revision. These observations establish safe integration,
not better trade decisions or returns.
