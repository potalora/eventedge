# Congress recovery follow-up, October 31, 2026

The public publisher has repaired the previously observed House filing gap. Let
the next ordinary production acquisition establish accepted recovery under the
existing audit policy. No Congress parser change is indicated by this evidence.
This follow-up changes documentation only; it does not acquire production inputs,
refresh a report, reconcile a receipt, acknowledge an alert, or launch a service.

## Public qualification and remaining limits

The isolated public check completed at `2026-10-31T08:15:19.292594Z` against
publisher revision `5e4c1b6fbef3e29c7895d8893c503bf477cbdeb7`, generated and last
checked at `2026-10-31T07:41:31.863Z`. Unchanged native audit validation passed
for the inclusive September 30–October 30 filing window: 44 official House IDs
matched, with no missing or additional IDs. Filing `20035727` now has eight
publisher transaction rows, extraction status `ok`, and original-PDF SHA-256
`0d7865d4d8b7c9b6800368afeb40e196796a6d65b1fdfed4f802bef2017efa30`.

This qualifies a current public audit snapshot for that window. It does not
replace the accepted October 29/30 failures, independently validate extraction
accuracy or Senate completeness, or enable signals, model context or orders.
The public qualification receipt remains outside Git; the preceding failure and
admission boundary are documented in [the recovery assessment](../congress-source-recovery-20261031.md).

## Next ordinary opportunities

Read-only host inspection at approximately 08:22 UTC confirmed active
`trade.timer` and `trade-preflight.timer`, inactive successful services, and
healthy runtime recovery without a blocking incident. The frozen active
generation remains `gen_026` at
`5c6f9c33091998a02d9acccbb596578fe54cc75b`.

- Monday November 2, **16:45 UTC / 11:45 EST**: ordinary screen preflight. It
  fetches into disposable diagnostic state and cannot close the accepted-source
  incident.
- Monday November 2, **23:00 UTC / 18:00 EST**: next eligible ordinary daily
  cycle. Its screen and governed probes precede the daily accepted acquisition;
  recovery requires that later accepted input and completed report. Actual
  completion is subject to the existing locks, deadlines and gates.

The five-minute timer wakes between these opportunities select persisted
recovery and update observation status; they do not create a new weekend source
sample. Daylight-saving time ends before Monday, accounting for the UTC shift.

The qualified October 31 publisher snapshot exceeds its existing **48-hour**
hard freshness bound after `2026-11-02T07:41:31.863Z`. A subsequent publisher
refresh is therefore required even for Monday's diagnostic acquisition. The
daily audit window also advances to October 3–November 2, so 44/44 historical
reconciliation does not guarantee Monday's current reconciliation. Preserve a
new failure if freshness, integrity or current House reconciliation fails.

## Acceptance evidence to inspect after the ordinary cycle

Paths below are relative to the canonical production checkout, never its
development shadow. Inspect existing files only; do not run report writers,
provider probes, repair admission, consumer `poll`/reconciliation or a trading
entrypoint as validation.

1. Read `data/generations/manifest.json` and confirm the active generation,
   frozen commit, configuration and state directory. For unchanged `gen_026`,
   November 2 accepted input is
   `data/generations/gen_026/source_inputs/e6c7813c9242ea8ce0a901d3734d4306f79c99741028efa95da7ac5a414eefde.json`.
   The filename is the existing canonical fingerprint of generation/session.
   Validate its identity and content digest through the read-only
   `operational_report._accepted_envelope` helper. Independently replay the
   Congress raw artifacts using `validate_audit_snapshot`, the accepted
   acquisition instant, and the actual session's 30-day window. Require complete
   audit coverage, native integrity, current House reconciliation and no
   unrecovered Congress diagnostic. Do not disable the age bound to qualify
   acceptance; structural historical replay alone is insufficient.
2. Read the November 2 `gen_026` operational report JSON/Markdown pair in
   `docs/reports/`. Require `evidence_complete=true`, unchanged source
   configuration, same-session New York `sources.acquired_at`, explicit Congress
   observation `success`, `success_empty` or `complete`, no Congress unresolved
   acquisition or source-health failure, and no open Congress incident. With the
   valid October 30 prior report, `source_incidents.history_status=available`
   and a Congress `recovered` alert establish the normal transition. A later
   healthy report may corroborate recovery even if its transition alert was
   missed. An unrelated provider can still leave the overall report degraded.
3. Read the root-owned `docs/reports/source-repair/2026-11-02-gen_026-source-intervention.json`.
   Require `status=available`, exact report-pair SHA-256, matching identity, and
   no current Congress case. Missing, stale, invalid or contradictory projection
   is not recovery. The producer builds cases from current open incidents, so
   the retained terminal `evidence_unavailable` repair refusal cannot recreate a
   case after a positively corroborated source recovery.
4. Let the ordinary native consumer observe that proposal. Then read the
   existing Congress intervention receipt: the confirmed `gen_026` case should
   become `recovered` with a separate `eventedge-source-intervention-recovery-v1`
   proof binding its original case hash, later session, report-pair digest and
   observation time. Original case, deliveries, acknowledgements, preparation
   history and terminal refusal remain retained. The older acknowledged
   `gen_023` case remains acknowledged; a different generation's success cannot
   recover it. Unknown pending delivery also requires its existing separate
   reconciliation and is not silently cleared by healthy source data.

The native consumer's current positive-health rules were inspected against its
installed helper. They permit recovery of this `assessment_required` case;
independent semantic-change cases retain their separate review requirement.

## Read-only checks exercised

The scheduler and current generation/report/runtime metadata were read without
changing production. Focused application recovery tests passed: **15 passed,
65 deselected**. Native retained-Congress recovery tests passed: **15 passed,
29 subtests passed**, using disposable state and fake delivery. A separate
in-memory Friday-failure/Monday-success exercise retained a terminal refusal,
produced the normal recovery alert and zero current intervention cases, and
passed the native positive-health predicate. It invoked no producer, live state
store, consumer poll or delivery function. No latent closure defect was found.
