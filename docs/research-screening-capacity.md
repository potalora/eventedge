# Research-screening checkpoint capacity

The immutable `research-screening-v1` checkpoint retains the complete sample
across all four horizons, including signal, model and failure evidence. Its
finite codec allowance is 512 MiB and 8,000,000 logical nodes; the source codec's
depth limit remains unchanged. The producer, operational report and local
continuity verifier use `research_screening.screening_codec_limits()` for this
checkpoint. Provider acquisition and MAIN source ingestion retain their existing
source-specific limits.

Congress retirement exposed an accidental dependency: the checkpoint had
inherited its larger allowance from the Congress audit policy. Removing that
policy reduced the aggregate allowance to 500,000 nodes, below the size of
previously accepted complete samples. The screening-specific allowance restores
the former bound without enabling Congress acquisition or expanding provider
ingestion.

A capacity failure before atomic publication does not create an accepted
screening sample. The earlier committed execution, fills, snapshots and existing
intents remain authoritative. Increasing capacity cannot recreate an unaccepted
model sample: candidate response reuse is ephemeral, persisted-only recovery
cannot invoke models, and missed prospective staging must not be backfilled
with fresh decisions. Preserve the original failed attempt and use the normal
recovery/session rules.

## Applying the verified defect repair

The guarded `scripts/apply_code_revision.py` records this specific repair after
the existing Congress retirement. It preserves the frozen profile and all books.
The original retirement record remains unchanged; an additive `code_revision`
records the fixed code's effective session and retains the preceding checkout.
Historical reports, accepted inputs and runtime incidents continue to select
their original code and profile. This is not a general configuration amendment.

Run the CLI as the application owner from the clean, pinned candidate checkout.
Default mode validates exact old/new commits, profile and incident pins and
returns deterministic plan and assessment digests. Applying that plan requires
`--expected-plan-sha256`, `--apply` and `--scheduler-quiesced`. Use the reviewed
scoped Ansible playbook to retain recovery material and manage the scheduling
and trusted-runtime boundaries.

An incident can transition to `wait_next_session/missed_staging` only after the
next-open deadline and proof of all sixteen completed execution contexts, phase
chains, snapshots and the shared execution seal. Missing research, committee or
staging stays missing. A separate immutable assessment retains the original
incident, journal, failed attempt/report and immutable financial proof. Its
validation remains independent of later account activity. No financial database
is changed by the repair transaction.

The CLI's durable pending journal permits only its own verified retry. An
ambiguous or foreign state leaves scheduling paused for reconciliation. Keep
the existing projection marker and failed report until a fresh ordinary session
produces valid evidence; then reconcile projections through the managed service
and restore the alert cron. An idle heartbeat does not establish successful
research, and a missed assessment never becomes a recovered sample.
