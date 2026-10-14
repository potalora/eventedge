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

## Bounded source-repair proposals (reviewed rollout required)

The report CLI now publishes an adjacent
`<session>-<generation>-source-repair.json` proposal after the canonical JSON and
Markdown pair. Its exact fields are `version`, `generation_id`,
`generation_commit`, `session`, `report_sha256`, `status`, `requests`, and
`requires_user`. The version is `eventedge-source-repair-proposal-v1`; status is
`eligible`, `stale`, or `unavailable`. Historical reports still publish normally,
with empty repair arrays. A missing/unknown source observation cannot propose a
worker. HTTP 401/403 failures appear only in `requires_user` and do not authorize
credential changes. The existing `source_incidents` and notification behavior
remain observational.

Each request uses the exact `eventedge-source-repair-request-v1` schema:
`version`, `incident_id`, `generation_id`, `generation_commit`, `session`,
`provider`, `configuration`, `first_failed_session`, `reason_code`, `http_status`,
and `report_sha256`. It is bounded to 4 KiB and cannot supply a path, URL, prompt,
command, remote or destination. The incident digest is SHA256 of the compact JSON
array `[version,generation_id,generation_commit,configuration,provider,
first_failed_session]`; it stays constant across report regeneration and
continued failures. Generation/configuration changes or a supported recovery
start a different incident. The report digest is SHA256 of the exact canonical
JSON bytes, a NUL byte, and the exact paired Markdown bytes. Canonical JSON uses
the existing report writer's sorted keys, two-space indentation and final newline.
Immediate failures are proposed directly from `repair_required` snapshots;
there need not be an `escalated` notification.

The source-owned broker interface is:

```sh
python scripts/source_repair.py requests --repo-root CANONICAL_CHECKOUT
python scripts/source_repair.py export --repo-root CANONICAL_CHECKOUT \
  --request-file BROKER_REQUEST_DIR/INCIDENT_SHA256.json \
  --output-dir NEW_PRIVATE_BUNDLE_DIR
```

Only the trusted broker chooses these command arguments. The CLI derives the
latest completed XNYS session from its actual aware UTC clock; there is no request
clock or historical override. It requires the canonical main checkout, the
manifest's active generation, canonical generation state/worktree paths, and the
full actual generation HEAD. It acquires an existing runtime lock through a read-only shared descriptor while
validating and capturing evidence. It never creates the lock or runtime directories. It does not start a worker or publish code.

The report pair is a proposal, even when its hash matches. Before admission,
export independently validates the source-owned accepted envelopes for every
session from the asserted first failure through the current session, plus the
preceding session that establishes the incident origin. That preceding source
must show the affected provider healthy or a different configuration; absence or
unknown evidence fails closed. A first-ever generation acquisition without this
boundary evidence requires operator assessment. It checks
source digest, generation, full commit, configuration and acquisition time, then
uses the existing source and incident projectors to corroborate the current
failure and its origin. A healthy intervening observation, invented persistence,
missing history, changed identity or unavailable projection prevents admission.
This deliberately fails closed where accepted source evidence cannot establish
the reported failure; a report-only or health-store-only failure needs operator
assessment.

The private bundle contains the canonical manifest, exact report pair, accepted
source envelopes and only the report's selected canonical attempt records. It
contains no ledger/database by default. SQLite export is not supported by this
MVP helper; a failure that needs a database requires a separately reviewed,
read-only backup procedure. All input files are opened through no-follow
descriptor traversal and checked for replacement/content changes before final
publication. Symlinks, traversal, special files, mixed identities and capture
races fail closed. Bounds are 256 files, 32 MiB per file and 256 MiB total;
capacity exhaustion is visible. The final bundle is atomically published with
read-only files/directories. Source bodies remain private and untrusted, and
must never become instructions or public PR content. The evidence manifest
records origin generation/commit/session/configuration, attempt identities,
source/report digests, acquisition time, file sizes and hashes.

One-attempt admission, durable accepted/running/terminal state, reconciliation,
400-day terminal retention, global worker exclusion and trusted publication
belong to the host broker. This helper's stable identity supports that boundary;
it does not implement another scheduler or retry mechanism. A repair still
requires a topic PR and normal review. No proposal, source edit, passing test or
branch push deploys a generation or authorizes an in-place production patch.

`tests/test_source_repair_endurance.py` runs 252 explicitly simulated XNYS
sessions through the real persisted report/incident/notification projectors and
proposal helper. It writes checkpoints at 20/40/60/252 and a reusable corpus for
host-broker acceptance. It seeds transient escalation, immediate schema/auth
failure, recovery, missing reports/observations, same-day regeneration, uncertain
notification delivery and configuration/generation changes. The fixture neither
calls providers/models nor claims native trading or worker runs. Host admission
and native worker/permission checks must be verified separately before rollout.

## Urgent manual source intervention

A current HTTP 401/403 source failure opens an urgent access case. A sanitized
trusted broker `failed` or `requires_user` outcome opens an urgent repair or
assessment case. Paid-only access, permanent unavailability and provider shutdown
are classified only from an explicit broker code with verified provider-contract
or operator evidence. Broader semantic changes require an explicit code with
independent-review or operator evidence. A timeout, HTTP 404/410, failed child
process or free-form worker explanation alone does not prove any of these causes.
Native worker sign-in failure does not establish a source-credential failure.

The application owns one deterministic case builder and renderer in
`source_repair_intervention.py`. Each case is version
`eventedge-source-intervention-v1`, has severity `urgent` and disposition
`requires_user`, and carries the current source/incident/generation identity,
concise fixed reason, observed report impact, recommendation and four numbered
choices:

1. **Restore current source/access:** prepare a recovery/access plan. Credential
   and production changes require explicit approval.
2. **Prepare replacement:** assess alternatives and draft a proposal/PR. No
   replacement provider is invented or selected by the alert; production
   replacement requires explicit approval.
3. **Retire source:** assess affected dependencies and draft a retirement
   proposal/PR. Production retirement requires explicit approval.
4. **Acknowledge:** record awareness only. Acknowledgment does not authorize any
   production change, relax a gate or mark the source recovered.

Impact names only source-health strategy/cohort references explicitly present in
the current validated report; absent scope remains unspecified. It does not claim
that all trading stopped. Raw source bodies, credentials, worker prose and
arbitrary links cannot enter cases. Cases are bounded to 64 KiB; rendering is
bounded to 4,096 characters. Fixed case schemas reject extra fields or modified
option instructions. The stable `case_id` is SHA256 of compact canonical JSON
`[version,incident_id,reason_code]`, without a newline. The full snapshot digest is
SHA256 of sorted compact JSON plus newline. Session-only updates preserve case
identity; material classification changes produce a new case.

The adjacent proposal is under
`docs/reports/source-repair/<session>-<generation>-source-intervention.json`.
Its exact fields are `version`, `generation_id`, `generation_commit`, `session`,
`report_sha256`, `status`, `cases` and `diagnostics`; version is
`eventedge-source-intervention-proposal-v1`. It binds the exact report pair and
uses `available`, `stale` or `unavailable` status. Broker outcomes are separate
`source-repair/<incident_id>.json` files with exact fields `version`,
`incident_id`, `state`, `reason_code`, `evidence_kind`, `evidence_sha256` and
`publication_url`. The latter is null except for a trusted published PR URL in
the fixed repository. A malformed outcome produces an explicit assessment case
and `broker_outcome_invalid` diagnostic, without echoing its content or inferring
a cause. A missing or unavailable proposal never proves that a retained case
recovered.

The ordinary report CLI invokes the same publisher after its repair proposal.
In production the `source-repair` subtree is root-owned: if the ordinary writer
cannot write it, stdout explicitly reports `publication_denied` while preserving
the normal report/proposal result. The trusted host broker observes the parent
report-directory change and invokes:

```sh
python scripts/source_repair.py interventions --repo-root CANONICAL_CHECKOUT
```

This command reads the canonical active manifest/HEAD, latest completed-session
report pair and sanitized outcomes under an existing read-only runtime lock. It
writes only fixed intervention artifacts in the authorized report subtree. It
never reruns trading, acquisition, tests or models. The broker refreshes the
same artifact immediately after publishing a worker outcome, so an urgent case
does not wait for the next daily report.

The separate managed notification consumer owns delivery receipts,
acknowledgment and reminder state. Pedro selected an urgent initial alert and
reminders **every four hours until acknowledgment**. An uncertain send must be
reconciled before another delivery attempt. Neither acknowledgment nor a chosen
proposal workflow is production approval. Publication, rollout and native
notification verification remain separate acceptance steps.

The intervention refresh reports an unavailable generation independently while
continuing other generations whose manifest/HEAD identities validate. Runtime
lock contention returns CLI exit 75 specifically for `interventions`, permitting
the host's bounded projection retry after a report event. Invalid evidence
returns 2; repair export/admission retain their no-automatic-retry contract.
