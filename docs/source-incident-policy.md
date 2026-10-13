# Source breakage and recovery: MVP

The operating target is bounded completion, safe degraded operation, and visible
repair needs across repeated sessions. Finite tests cannot establish indefinite
uptime. External schema, access and availability changes remain maintenance
events. This policy uses the existing provider guards, source evidence and daily
operational report; it adds no service, database, provider calls or repair agent.

## Runtime behavior

| Situation | Automatic behavior | Operator action |
| --- | --- | --- |
| Timeout, disconnect, rate limit or eligible server error | Existing bounded retries/backoff consume the original acquisition deadline. Failed data stays failed; unaffected work can continue under its existing guards. | First unresolved session opens a degraded incident. Three consecutive observed failed sessions require repair. |
| Invalid response/contract, HTTP 400, 401, 403, 404 or 410 | Reject the affected input; mark repair required immediately. | Check schema/endpoint, credentials or access policy. A malformed response is evidence of a contract failure, not proof of a provider schema change. |
| Fresh validated source success | The next normal scheduled acquisition can recover automatically. Close an existing incident only with explicit healthy source observation, complete current evidence and no unresolved source/health failure. | Review the recovery event; no manual incident reset or historical replay is needed. |
| Source disappears, is disabled, or evidence is missing | Keep the previous incident unresolved. Missing observations cannot count as success or extend a consecutive-failure streak. | Check configuration and evidence; intentionally removing a dependency is a reviewed behavior change. |
| Required price, account, risk or time input fails | Existing mandatory gates remain blocking. | Repair the dependency; never relax a gate to obtain a clean report. |

There is no extra retry loop between daily sessions and no automatic provider
substitution, schema guessing, source disabling or code update. Existing scoped
exclusions and risk protections continue to determine which decisions are
permitted. The incident policy itself cannot authorize a trade or change a book.

## Report and alert contract

The existing `scripts/daily_trading.sh` report step publishes `source_incidents`
in the operational JSON and Markdown. The report CLI also includes it in stdout
for the existing monitor. Its bounded `open_incidents` snapshot is separate from
execution/accounting validity; a source failure need not mean accounting failed.

Only the exact preceding XNYS session's report is read, with matching generation,
commit and source configuration. Incident history is an operational projection,
never trading authority. Missing, invalid or mismatched history is explicit;
the report must not invent a continuous failure streak or recovery. Keep the
same report output directory between sessions. Same-day regeneration is
deterministic and does not increase a failure count. Incident counts are capped;
there is one current record per affected source, not an ever-growing event list.

If the current source envelope is unavailable, validated prior incidents and
their last known configuration are carried as unknown with assessment unavailable.
They cannot escalate or recover until evidence returns. A known different
configuration is not joined to the old history. A missing previous report breaks
continuity; it must be treated as unknown monitoring history, never proof that an
old repair was completed.

The `alerts` list contains transition events with stable `alert_id` values.
The callable `source_notifications` consumer deduplicates by `alert_id` and notifies on opening,
escalation, loss of observation and recovery. Re-reading the same report must not
send another message. Unchanged failures remain in `open_incidents` without a new
notification. An open repair-required incident stays visible until supported
recovery. Inspect the canonical source diagnostics for exact reason/status and
affected strategy/cohort scope; never put raw responses or credentials in alerts.

The consumer checks the exact expected generation, commit and session, and the
matching JSON/Markdown pair. Missing, invalid or mismatched reports emit one
report-unavailable event per expected session; a valid return emits recovery.
Its separate JSON receipt file binds generation, commit, recipient and test mode.
It records uncertainty before sending and acknowledges only a confirmed message
receipt. A proven rejection can retry on the next invocation; an uncertain send
requires destination reconciliation before any retry. It retains 90 days of
confirmed events, preserves unresolved uncertain sends, and stops safely at its
2,048-event/1 MiB limit. Keep this file across monitor invocations; never reset it
to work around an uncertain delivery. The caller supplies a bounded transport
and must surface state errors or uncertain delivery for operator attention.

The [isolated acceptance drill](verification/2026-10-13-source-acceptance.md)
verified five labeled TEST events through the existing Discord transport with
confirmed receipts and duplicate suppression. This does not install the consumer
in the scheduled monitor. During an explicitly authorized rollout, wire the
consumer into the existing monitor with a durable receipt path and verified
recipient, then verify the deployed report and scheduled invocation. Keep one
owner for these source alerts to avoid a second summary sender duplicating them.
A missing report means unknown health. Host, process,
authentication, disk capacity and report freshness remain separate operational
checks; this source policy does not replace them.

## Small repair procedure

1. Identify the source, affected decisions, first failure, fixed reason/status,
   and retained evidence. Leave unaffected operations and existing protective
   behavior running where their guards permit it.
2. Reproduce using one sanitized failing response fixture. Compare with the
   provider's current documented contract. Fix only the adapter or authorized
   credential/configuration boundary. Do not reclassify failed data as empty.
3. Run the focused adapter/contract test and one failure-to-recovery integration
   case; preserve time limits, malformed/stale rejection and replay invariants.
   Open a reviewed topic-branch PR with cause, scope and validation.
4. Production changes require Pedro's explicit instruction. Behavior changes
   require a new generation. An in-place repair additionally needs the existing
   verified-defect and approval criteria. Resume through the next ordinary
   session and verify the recovery event; preserve accepted historical inputs.

## Finite acceptance and closeout

The MVP is accepted when deterministic tests demonstrate transient escalation,
immediate contract/auth failure, explicit recovery, no recovery from missing
evidence, generation/configuration separation, bounded history, stable duplicate
event IDs, and publication through the real report CLI without ledger/input
mutation. Reuse the existing retry, source isolation, historical lifecycle and
replay tests. Do not require natural outages or weeks of market days, broaden the
provider campaign, or rerun unrelated passing suites without a relevant change.

Run `python scripts/run_source_notification_drill.py` for the safe local receipt
sink. Supplying a real transport callback is an explicit action: the reusable
drill never discovers recipients or sends external messages by default.
`scripts/check_target_host_smoke.py` collects bounded read-only host facts;
credential presence and an active timer do not prove authentication or a healthy
application run.

Code/test acceptance, isolated transport acceptance and scheduled production
acceptance are separate. This change is prepared for PR review; deployment and
monitor installation require their own explicit rollout and evidence.
