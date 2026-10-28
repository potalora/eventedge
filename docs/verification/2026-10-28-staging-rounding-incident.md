# October 27 staging failure: rounded-zero policy approval

Reviewed October 28, 2026 UTC. The production incident remains open. This change
prevents newly evaluated recommendations from receiving a rounded-zero approval;
it does not repair frozen decisions or authorize a production deployment.

## Observed failure

The October 27 daily run for gen_025 at
`e33cc9eed38aa668dbde5f5e2844b7fbfb9391ac` failed staging in all four $10k paper
books. The other twelve books completed staging, and accounting was valid in all
sixteen. The four failures raised `ValueError: invalid intent sizing inputs`
from the execution bridge. Their staging transactions rolled back: read-only
ledger checks found no October 27 staging completion or newly created intent in
any of the four affected books.

The controller classified the failed daily report as
`requires_user / checkpoint_invalid` at 22:17:29 UTC. Its recovery attempt count
is zero. The companion cron's 62 consecutive errors, observed at 04:27 UTC on
October 28, are repeated observations of this terminal incident, not 62 trading
or recovery attempts. The cron uses a script with an empty prompt; editing a
prompt cannot resolve this condition. The controller blocks subsequent daily
launches while this active incident requires operator review.

There is also a separate retained USDA acquisition failure: three crop-condition
operations exhausted three physical attempts each with `transport_error` and no
HTTP status. Partial fallback content does not establish complete source coverage.
The weather strategy was held; the underlying transport cause remains unresolved.
Neither this source failure nor the generic cron error establishes expired model
credentials. No fresh provider or model request was made during this review.

## Deterministic cause and correction

The frozen staging contexts and committee sidecars reproduce the policy error:

| Step | Portfolio weight |
|---|---:|
| Existing earnings-call exposure | 0.3401170072654953 |
| Strategy cap | 0.4 |
| AMT raw available allocation | 0.0598829927345047 |
| AMT approval at existing eight-decimal precision | 0.05988299 |
| Remaining raw allocation for ACU | 0.0000000027345047337590245 |
| ACU approval after rounding | 0.0 |

The remaining raw allocation exceeds the existing `1e-9` tolerance, so the policy
accepted it before rounding. Rounding then produced an invalid zero approval.
All four affected books froze ACU as `trimmed`, with approved weight zero and
reason `strategy:earnings_call`. The bridge correctly rejected that input.

The fix checks the final approved weight before accepting a recommendation or
adding it to pending exposure. An unusable rounded allocation becomes a policy
rejection with its original requested weight and limiting reason preserved.
The existing raw-weight guard, precision, tolerance, cap calculation, positive
allocations and bridge validation remain unchanged. A valid positive allocation
that cannot buy a whole share retains its separate zero-share handling.

## Verification

Seven new regression cases cover the two retained request-size pairs, four
precision boundaries, and real committee/policy/bridge/ledger staging with an
unaffected surviving recommendation, rejection audit and idempotent replay.
Before the fix, five cases failed and the two existing-behavior controls passed.

The complete affected policy, policy-pipeline, policy-ledger, policy-execution,
bridge and session-executor test files pass: **261 tests**. Independent review
found no blockers and separately ran **20 regression and safeguard cases**.
Both runs reported only the existing `websockets.legacy` deprecation warning.
The repository-wide suite was not rerun for this narrow correction.

Compatibility checks used an isolated archive of deployed revision `e33cc9e`.
The same seven new cases first reproduced five failures and two passing controls.
After applying the policy correction, the six affected test files passed there
too: **263 tests**. These are local compatibility checks, not a production run.
The final read-only check confirmed clean tracked production code at `e33cc9e`
and unchanged hashes for all four frozen committee payloads.

## Production recovery boundary

The original committee decisions are already frozen. Existing replay loads them
directly and does not reevaluate policy; the ledger rejects conflicting
replacement payloads. The current controller also has no public operation to
reconcile a terminal incident. Deploying this upstream fix alone therefore
cannot complete the October 27 session or clear its operator-required state.

The [deployed recovery contract](https://github.com/potalora/eventedge/blob/e33cc9eed38aa668dbde5f5e2844b7fbfb9391ac/docs/runtime-recovery.md)
requires preservation of original evidence, exact identities and execution
boundaries. A separately reviewed correction/reconciliation capability would
need to bind any derived correction to the exact original decisions and risk
contexts, prove that only this defect is corrected, validate all sixteen books,
preserve the failed original attempt, and enforce the eligible-open cutoff.
The existing distinct recovery receipt can document legitimate completion; it
cannot itself correct the decision or release the terminal incident.

A separately authorized new generation can resume prospective research after
qualification, but cannot backfill this session or count it as successful.
Neither recovery route is implemented or authorized by this PR. No journal,
frozen input, ledger, acknowledgement, cron schedule, service or production code
was changed. Silencing the monitor would not restore the blocked workflow.
