# Unattended runtime recovery

The reviewed candidate adds a bounded application controller to the existing
`trade.service` and `trade.timer`. This document describes candidate behavior;
it does not authorize installation, a production patch, or activation.

## Deployment prerequisites

Run the [scheduled workflow acceptance gate](scheduled-workflow-acceptance.md)
against the candidate before deployment acceptance. It rehearses the composed
entrypoints with the frozen profile, disposable state and external IO fixtures.
Idle heartbeats, installed unit metadata and component tests cannot replace that
check; isolated acceptance also does not establish live provider or model health.

Deploy through a new generation after Pedro explicitly authorizes deployment.
Both the application launcher and that generation's frozen worktree must contain
this controller, the persisted-only guards, and the inherited runtime-lock
support. The generation needs an exact frozen `generation-config.json` and
manifest `config_file` / `config_sha256`, prospective decision clocks, and the
accepted research-screening checkpoints used by the candidate profile.

An older implicit-configuration generation is not automatically migrated. A
missing worker, modified checkout, changed profile, wrong Git worktree backlink,
foreign state path, or unavailable accepted proof requires operator review. A
branch push or PR is not deployment, and these changes do not authorize an
in-place active-generation patch.

Install the checked unit files from the reviewed application revision through the
approved Ansible release workflow. The repair-runtime provisioning role does not
install or activate the ordinary daily units. The existing migration landing role
copies those units in a stopped, disabled state; it is not an ordinary application
upgrade. The application `deploy/systemd/install.sh` is a legacy installer that
also enables schedules, so it is not a read-only verification command. No unit
installation or activation is part of the current predeployment work.

## Schedule and limits

The existing timer retains the ordinary 18:00 New York daily window and adds a
wake every five minutes and two minutes after boot. The controller chooses work
from exact XNYS sessions. It does not create an overdue historical sample from
current sources. The ordinary `daily_trading.sh` still performs its screen probe,
hard governed gate, daily command, and exit report.

The controller records the exact active generation roster before launching a
normal sample. It holds the canonical exclusive runtime lock across the entire
wrapper and passes the verified descriptor through its child commands. Lifecycle
changes cannot alter the roster between the probes and the daily worker. Normal
work starts at most once per generation/session through this controller; adding a
new active generation does not replay another generation's already-started sample.

Older pending accounting is reconciled first. An unresolved retry or a
`requires_user` incident blocks the next ordinary cycle. A safely completed P0
with missed staging can advance to the next ordinary session. A complete miss
before any accounting context was accepted is recorded as a missed session.
Mixed bound and unbound books require operator review.

Each recovery has at most three worker attempts. Abandoned attempts remain
charged after reboot. The initial delay is five minutes; subsequent recovery
failures wait five and then thirty minutes before the remaining attempts.
Exhaustion requires operator review. Busy ordinary work defers recovery without
consuming an attempt.

The normal wrapper has a 3-hour-5-minute controller ceiling. One recovery worker
has a 15-minute ceiling. The whole systemd invocation has a 3-hour-30-minute
ceiling and a 30-second stop allowance, with `KillMode=control-group` and forced
termination enabled. The application also kills and reaps each spawned process
group on timeout or controller-wait failure. Captured subprocess output retains
at most a 64 KiB tail. A heartbeat callback runs at least every sixty seconds
while waiting; it never extends a worker's deadline.

## What can resume

| Persisted condition | Controller behavior |
| --- | --- |
| All sixteen execution contexts are intact, with a valid committed phase prefix and matching current state | Resume P0 through `SessionExecutor` with its exact persisted market and borrow inputs. No provider or model callback is allowed. |
| P0 is complete and staging is partial | Before the eligible next-session open, stage only from complete accepted source, screening, candidate, volatility, enrichment, and committee evidence. A missing callback input stops recovery. |
| The eligible open has passed | Finish safe bound P0 if necessary; record missed staging. Never publish a fresh historical decision or move its cutoff. |
| Accounting and staging are already complete | Validate the original prospective bindings and rebuild the report; do not replay outcome acquisition, financial effects, or optional shadow work. |
| Accepted source failure, model hold, or candidate quarantine | Preserve the accepted result. A later ordinary session can acquire a new sample under the existing source-incident policy. A frozen failed bundle is never replaced to make its original session pass. |
| Changed identity/configuration, corrupt evidence, invalid accounting, mixed book boundaries, strategy defect, unresolved authentication, or exhausted attempts | Record `requires_user`; no automatic generation, configuration, source, credential, or code change. |

A newer complete clean accepted sample can resolve an earlier preflight
authentication incident. The report retains the earlier incident as provenance.
Permanent source-contract and access incidents also retain their existing
[source incident policy](source-incident-policy.md) escalation and notification
rules. Runtime replay does not replace that policy or add provider retries.

## Durable state and observation

The application user owns:

- `data/runtime_recovery/journal.json`: exact identities, launch markers,
  attempt counts, backoff times, and dispositions. It is written before a launch
  and never silently reset after corruption.
- `data/runtime_recovery/status.json`: bounded versioned heartbeat for the
  existing Hermes monitor. It contains no credentials or raw error bodies.
- `data/generations/<generation>/runtime_recovery/<session>.json`: a distinct
  completion receipt after successful persisted recovery. Original daily
  attempts, interrupted output, and manifest history remain unchanged.

The status version is `eventedge-runtime-recovery-v1`. Its fixed dispositions are
`recovered`, `retrying`, `wait_next_session`, and `requires_user`. The status
projection includes only active generation identities and at most 64 incidents.
Any active `requires_user` or `retrying` incident affects current status. For
resolved or waiting incidents, only the latest session per active generation
determines the current summary; an older missed sample remains visible without
keeping a later healthy session degraded forever.

Missing, invalid, or more-than-fifteen-minute-old status is an observation
failure. A controller error has `status=operator_required` and a fixed
`controller_reason_code`; a fresh timestamp alone is not proof of successful
accounting, staging, sources, or reports. Existing Hermes monitoring consumes the
projection and its existing notification route. The controller sends no messages.

Read the latest projection without launching work:

```bash
.venv/bin/python scripts/recover_runtime.py --repo-root "$PWD" --status
```

The ordinary operational report validates recovery receipts independently against
all sixteen books, exact-session immutable economic rows and health evidence,
accepted source checkpoints, and the frozen profile. It distinguishes recovered
accounting/staging from the original interrupted daily result. A malformed or
changed proof remains incomplete; it cannot be hidden by a healthy heartbeat.
Report regeneration cannot fabricate a missing original success receipt.

The controller discovers persisted incomplete sessions and wholly missed expected
sessions within a bounded ten-calendar-day window. Pending journal incidents
remain durable beyond that discovery window. Before dismissing an operator
incident, inspect its exact generation/session report and the underlying accepted
proof. Do not delete the journal, refresh frozen inputs, change a profile, or
replay a past session merely to clear an alert.

## Verification scope

The focused runtime tests use real sixteen-book accounting/staging methods,
forbid live provider/model calls during recovery, and compare effect counts on
replay. A child process exits abruptly after a committed corporate-action phase;
a new process reopens the persisted books and finishes safe accounting without
duplicating fills, cash events, or snapshots. Separate tests exercise descendant
termination, corrupted journals/proofs, exact checkout identity, missing reports,
backoff/exhaustion, and launch ordering.

An accelerated four-session scenario jumps through October 14, 15, 16, and 19:
reboot before accounting, bounded next-day reconciliation, preservation of an
accepted source failure, then later healthy once-only daily cycles. These tests
prove the injected transitions and persistence boundaries. They do not prove
actual multi-day wall-clock endurance or production reliability.

The existing `check_target_host_smoke.py` remains a read-only metadata check. It
checks host/checkout boundaries, the service user and working directory, and timer
activity; it does not establish this controller's installed ExecStart, periodic
calendar, timeout/cgroup settings, authentication, or scheduled success. Its
positive result cannot replace a reviewed release's installed-unit verification
and the separate runtime/proof acceptance above.
