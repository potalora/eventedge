# Source recovery assessment and corrections, October 31, 2026

The October 30 gen_026 session completed accounting and staging for all sixteen
paper portfolios, while supported enrichment remained incomplete. Two separate
source limitations and two monitoring defects explain the intervention. Neither
the existing safety gates nor the completed paper workflow establish full source
coverage. These changes are prepared for review; this document is not deployment
or source-recovery evidence.

| Finding | Why unattended recovery stopped | Correction or recovery requirement |
| --- | --- | --- |
| Congress publisher omitted one official House filing | Original-document capture succeeded, but a PDF cannot establish complete publisher extraction. The first failure occurred in gen_026's first session; no preceding-session accepted baseline exists to corroborate repair admission. | Preserve the captured original and incomplete coverage. A fresh publisher snapshot must pass reconciliation. Do not invent a baseline or rewrite prior sessions. |
| Broker erased the exact admission qualification | Exporter stderr was discarded; missing baseline evidence became generic `evidence_unavailable`, which the alert described as an access-restoration task. No repair worker started. | Retain bounded private stderr; project only the three exact trusted qualification codes. Give evidence-specific instructions through both notification and resumed preparation. |
| Required FINRA October 15 archive remained inaccessible | Current exact-URL probes returned 403. The SDK silently discards non-200 responses; the accepted attempt retained only a missing-archive `provider_error`. Retrying or increasing a timeout cannot create an accessible archive. | Retain a separately identified later HTTP probe in future acquisitions, without attributing it to the earlier SDK response or changing source acceptance/retry behavior. Provider availability/access must still recover. |
| FINRA enrichment bypassed the source monitor | Source incidents cover main source acquisition; optional enrichment failures were retained elsewhere and did not enter that repair intake. | Publish a separate, bounded enrichment observation from revalidated accepted inputs and all sixteen book bindings. The native monitor reports its scope, preserves delivery uncertainty, and requires fresh positive evidence before recovery. It does not enqueue an unsupported repair worker. |

## What automatic recovery actually guarantees

Provider retries address selected transient transport/server failures within a
budget. Runtime recovery resumes interrupted accounting or staging from accepted
inputs. Source repair may prepare a narrowly validated topic PR when its evidence
requirements are met. These mechanisms do not authorize provider substitution,
credential changes, source-gate relaxation, historical input replacement, merging,
or production deployment. In this incident, the portfolio workflow was complete;
there was no unfinished accounting step for runtime recovery to resume.

The implementation had real observability gaps: discarded admission diagnostics,
misleading access instructions, and no dedicated enrichment notification path.
Correcting those gaps makes the required action visible. It does not turn absent
data into a successful observation.

## Preserved boundaries and rollout

Canonical historical JSON/Markdown reports, accepted source/decision inputs,
portfolio databases, generation epochs, and terminal repair receipts are not
rewritten. Supplemental enrichment artifacts are separately bound to the exact
report pair, generation, commit, frozen configuration and accepted evidence.
Unknown, stale or contradictory evidence cannot clear an outstanding notice.

The operational corrections span the application contract, the managed host
broker, and the native Hermes consumer. Install compatible consumers before
emitting new typed reasons. The trusted observer runtime can read gen_026's
existing schema 2 inputs; installing that runtime and the reporting helpers does
not require changing gen_026's trading code. The schema 3 acquisition probe is
for a separately reviewed application release. All production installation still
requires Pedro's explicit deployment instruction and the managed rollout checks.

An existing rejected receipt is terminal and remains unchanged. A future healthy
Congress acquisition can resolve the source incident normally; no receipt reset
or repeated worker launch is warranted for the missing original baseline.

## Evidence

- [Congress original capture, upstream gap and admission qualification](../congress-source-recovery-20261031.md).
- [FINRA exact-URL diagnosis and versioned diagnostic contract](2026-10-31-finra-http-diagnostics.md).
- Native FINRA September 30 positive control: the installed SDK downloaded and
  parsed the real archive into isolated scratch, and the candidate adapter/model
  returned validated data at a historical clock. This confirms the positive
  parser path; it does not qualify the missing October archive.
- Regression checks cover exact qualification propagation, private-output
  containment, successful large exporter manifests, terminal receipt deduplication,
  legacy acquisition replay, failed HTTP probes, report binding and notification
  recovery/uncertainty. Final acceptance results belong in the release PRs.

## Verified candidate checks

The final focused application selection passed 240 tests with one skip; the
composed scheduled-session monitor test passed separately. Host repair tests
passed 80 tests, and the native consumer suite passed 136. Independent review
covered the cross-repository contract and notification state transitions.

A network-disabled check against a protected copy of the actual October 30
state reported twelve FINRA failures with workflow completion true and continuity
readiness false. All 35 database connections targeted the copied state. Canonical
reports were unchanged, and an independent source-file verification passed.
The native consumer accepted the resulting observation, delivered once through
a fake transport, and suppressed the duplicate. No real notification was sent.
These are candidate validation results, not proof of production installation or
upstream recovery.
