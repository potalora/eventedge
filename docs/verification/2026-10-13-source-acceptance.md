# Bounded source acceptance drills — 2026-10-13

Scope: test the remaining notification path, inspect the actual target host
without executing production work, and exercise real Luna recognition/blind
verification plus scoped Sol. No generation, scheduler, service, production
report or trading state was changed. No orders were attempted. This is PR
acceptance evidence, not a deployment or an indefinite-uptime claim.

## Notification delivery and recovery

`scripts/run_source_notification_drill.py` constructs reports with the real
source-incident projector and Markdown renderer. Accelerated sessions cover an
opening transient failure, an unchanged second failure, third-session escalation,
validated source recovery, a missing report and the report's valid return.

Both the default local receipt sink and an explicitly supplied existing Hermes
Discord transport confirmed exactly five events: opened, escalated, recovered,
report unavailable and report recovered. Every step was re-read against the
same durable receipt file, producing zero additional transport calls. Real
messages were labeled TEST and used synthetic reports in a separate local
directory. The scheduled monitor job was not invoked or edited. Its resolved
recipient and the native sender's recipient were checked for equality before
every send; only hashed receipt/recipient identifiers were retained.

The callable consumer is application code rather than a test-only deduplication
simulation. It validates report identity and its Markdown pair, bounds input and
receipt state, records uncertain delivery before sending, and acknowledges only
a valid confirmed receipt. Tests cover rejected/uncertain delivery, crashes,
identity separation, corrupt state, stale/malformed/missing reports, unsafe
persisted payloads and retention. Installing this consumer in the existing
scheduled monitor remains part of a separately authorized rollout.

Reproduce without external messages:

```sh
python scripts/run_source_notification_drill.py --output-dir /tmp/eventedge-drill-new
python -m pytest -q tests/test_source_notifications.py tests/test_source_incidents.py
```

The output directory must be new or empty. Do not delete an existing delivery
receipt file to repeat real sends. A real callback must distinguish a proven
rejection from an unknown result and enforce a finite transport deadline.

## Real model stages

One retained original scanned page supplied three narrowly scoped probes about
the issuer/date and two class-specific security counts. Runtime prompts carried
no expected extracted values. Recognition and the separate blind verifier each
used actual Luna/high calls; Sol/high was used only after scoped eligibility.

The first whole-worker attempt failed closed because the two Luna responses used
different unit phrases, despite agreeing on the numbers. A single corrective
attempt requested complete printed count labels without supplying answers or
relaxing validation. Its native reads agreed, but the test setup used a date-only
cutoff whose NY end-of-day fractional second exceeded the scope's final second.
Both whole-worker receipts remain failed. The reusable harness now rejects this
bad setup before constructing a model client; a regression covers the boundary.

A separately labeled continuation revalidated the exact corrective native bytes
and request bindings at a valid current UTC timestamp. That produced an automatic
review and eligible receipt, followed by one actual scoped Sol call. Sol returned
neutral, score zero and insufficient context for a current directional trade,
while referencing all three claims. No directional outcome was forced.

Totals: four real Luna calls and one real Sol call; three supervised executions
took about 144 seconds combined and each left zero descendant processes. Per-call
and shared deadlines and process/memory supervision remained in place. There is
native evidence for all stages, but no passing uninterrupted whole-worker native
receipt from this drill. Model aliases matched; provider revisions were unpinned.
One selected page does not establish unseen-document accuracy, full-document
coverage or independence between two calls to the same model.

The reusable `scripts/run_native_document_smoke.py` has no discovery, retries,
answer-key input or paid API fallback. It requires an explicit retained registry,
as-of timestamp, auth path and new private output directory, and is intended to
run under the existing external process supervisor. Its automated transport
fixtures are explicitly constructed and do not count as native evidence.

## Read-only target inspection

`scripts/check_target_host_smoke.py` collected bounded metadata through the
configured SSH route as the production owner. It checks host identity, canonical
production and active-worktree boundaries, readable commits, scheduler ownership,
report-directory access and credential presence. It neither runs preflight nor
calls providers/models, publishes reports, or changes services.

Production and active `gen_019` were still commit `5611ad84c60712671d9fe0240f7f5f20caaf473a`.
The candidate was not deployed. The daily timer was active, but preflight was
failed and the latest manifest daily outcome was unsuccessful. The October 12
operational report was degraded with complete evidence and lacked the new
`source_incidents` field. A successful systemd wrapper result was not treated as
an application success. Credential presence did not establish authentication.

Remaining rollout checks are concrete: authorize and deploy the intended new
generation, install the reviewed monitor consumer, verify native authentication
under its actual owner, and confirm the new generation's ordinary scheduled run
and report/alert publication. None is implied by this PR or these isolated drills.

## Evidence and regression checks

Private, untracked evidence is under
`data/forward-readiness/source-acceptance-20261013/`: local and Discord drill
results/receipt files, host inspection/snapshot, native failed attempts and
continuation receipts, and focused JUnit output. No credentials or raw native
responses are committed. Final combined focused verification passed 322 tests
with one existing websockets deprecation warning. Critical Ruff checks and diff
checks passed. Independent review found one host-boundary acceptance defect;
seven new regression cases covered its repair, and the narrow recheck found no
remaining P1/P2 issue. These suites cover the new consumers/harnesses and their
existing source/report/document contracts; the PR checks record the full
baseline and optional-SDK regression results for the final commit.
