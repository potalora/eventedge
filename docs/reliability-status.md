# Current generation and reliability work

Policy updated November 1, 2026, America/New_York. Pedro's direction is to keep
the current generation running and deploy a successor only when necessary to
fix demonstrated run failures, or after the system is reliable and a specific
experiment is approved. CI improvements, test tooling and a ready PR do not
by themselves justify a generation change.

This is a dated operating record. Verify changing facts against the host
manifest, frozen worktree, native runtime status and canonical reports before
acting. Private state and credentials remain outside Git.

## Deployed baseline

| Field | Verified baseline on November 1 |
|---|---|
| Sole active generation | `gen_027` |
| Application commit | `dba04fbaf7bee827f5c61d5d8431677c3d65cb10` ([PR #64](https://github.com/potalora/eventedge/pull/64)) |
| Frozen profile SHA256 | `ecf94154a063fc1af5b160f51bb2a61acf8384f7a2571f47d41377d91a5b82a9` |
| Profile scope | Six supported strategy flows, four horizons and sixteen paper books; declared research exclusions remain visible |
| Authoritative checkout | Host `/home/hermes/trading_agents` |
| Frozen code | Host `/home/hermes/trading_agents/.worktrees/gen_027` |
| Authoritative state | Host `/home/hermes/trading_agents/data` |
| Ordinary runs | Manifest run history empty at inspection; no completed production session established here |
| Controller | Healthy heartbeat at November 1, 00:15 New York (04:15 UTC); this is an idle health observation |
| Next ordinary session | Monday, November 2; normal daily admission after 18:00 New York (23:00 UTC), with preflight at 11:45 New York (16:45 UTC) |

The FINRA official Query API repair is already in this deployed baseline.
gen_026 and earlier generations are historical, with state and evidence retained.
There is no approved successor generation, allocation of `gen_028`, or queued
production rollout in this record. The earlier generic deployment request was
superseded by Pedro's decision to observe gen_027 and use the gates below.

## Work tied to the current generation

| Work | Present evidence | Next action |
|---|---|---|
| Ordinary production run | gen_027 is deployed; its first completed session is still unobserved | Observe the normal November 2 invocation and retain its exact commit, process result, source failures and report identities |
| Accounting and staging | No completed gen_027 session is qualified here | Verify all sixteen books, ledger/accounting phases, staging and independently reproduced reports on actual outputs |
| Source reliability | The FINRA repair is deployed; future provider success is unproven | Inspect the real source evidence and bounded failures; preserve retries, timeouts, exclusions and missing coverage |
| Observer and delivery | The real native chat route passed one authorized labelled send and exact readback; an earlier prospective report was withheld | Verify native processing of a fresh gen_027 report and expected delivery/duplicate handling; transport alone does not qualify report consumption |
| Multi-session continuity | No qualifying gen_027 sequence established | Accumulate ordinary sessions in the same generation, then apply its exact-commit readiness checker and existing five-session gate |
| Paper fills | Positive gen_027 fill behavior is unobserved | Verify native fills against subsequent real market prices when eligible intents occur; a natural zero-fill session leaves this path unqualified |
| Diagnosis and regression tooling | Separate tooling PRs can support this work | Repair demonstrable harness defects and run focused checks without changing the frozen application, ledgers, clocks or acceptance rules |

Acceptance requires real sources, real data, configured live models and actual
calendar time. Offline tests and replay are diagnostic evidence. Do not fabricate
sessions or fills, weaken freshness/risk/source gates, reset acknowledgements, or
erase failures to make acceptance pass. Healthy current operation, historical
failure and unobserved coverage must remain distinguishable.

## Branches and PRs: where the work belongs

All PRs below were open and unmerged at inspection. Git integration status does
not identify deployed code: production is pinned to the exact commit above.

| Work | Relationship to gen_027 | Disposition |
|---|---|---|
| [#59 selection research](https://github.com/potalora/eventedge/pull/59) | Offline experiment; absent from the deployed application | Parked for a qualified future experiment |
| [#60 reliability assessment](https://github.com/potalora/eventedge/pull/60) | Historical gen_025 assessment, test-clock work and seventeen research ideas | Historical evidence retained; this document replaces its current-status claims |
| [#61 rounded-zero staging guard](https://github.com/potalora/eventedge/pull/61) | Equivalent guard inherited through #62 | Historical repair/integration work; not an outstanding deployment requirement |
| [#62 recovery release](https://github.com/potalora/eventedge/pull/62) | Application ancestor deployed as gen_026, inherited by gen_027 | Historical release; dated blocked-deployment wording is not current status |
| [#63 source recovery](https://github.com/potalora/eventedge/pull/63) | Application changes inherited by gen_027; observer has its own release identity | Current supporting repair capability; preserve separate application/observer pins |
| [#64 FINRA Query API](https://github.com/potalora/eventedge/pull/64) | Exact deployed application commit `dba04fb` | Current baseline; observe ordinary operation before declaring reliability |
| [#65 acceptance harness](https://github.com/potalora/eventedge/pull/65) | External tool with a separately pinned candidate | Current reliability support; no production-generation deployment implied |
| [#66 CI/startup optimization](https://github.com/potalora/eventedge/pull/66) | Undeployed candidate `e3c697e`; includes a runtime lazy-import change | Parked for application deployment; no demonstrated run failure currently requires it |
| [#67 timer/continuity checks](https://github.com/potalora/eventedge/pull/67) | External tool extending #65; not a successor application release | Current reliability support with separate candidate evidence |

PR #66's isolated real-data run passed sixteen books, report reproduction,
321 source calls and 39 live model responses, creating thirty pending paper
intents and zero fills. PR #67 verified two real non-session timer firings and
scheduled a separate isolated campaign for November 2–6 at 18:00 New York.
That campaign pins `e3c697e`, whereas production gen_027 pins `dba04fb`.
It cannot establish gen_027 production acceptance. Its transient timers do not
survive a host reboot; campaign state and receipts persist. No additional test
notification is authorized by this record.

## Gates for a successor

A successor is conditional work, not the next item in a deployment queue. Use
one of these two routes and record which route applies before proposing rollout.

**Necessary run repair.** Identify a concrete failure in actual production
evidence, its impact and the smallest justified correction. Explain why a code
change and generation replacement are needed; a provider outage, access problem
or harness failure may need a different remedy. Keep the current run's evidence,
add regression coverage and verify the corrected path with real inputs where
available. This route may proceed before five reliable sessions when the failure
prevents them. It does not authorize unrelated optimization or research changes.

**Experiment after reliability.** First document current-generation acceptance:
the existing five consecutive qualifying XNYS sessions with complete accounting,
staging, sources and reproducible reports across all sixteen books; observed
scheduling and fresh-report observer delivery; and explicit disposition of
remaining recovery, fill and coverage gaps. Zero fills do not prove the positive
fill path. No unresolved run blocker or unqualified required path may be hidden
behind a green CI result. Then define one experiment's hypothesis, frozen
comparison, budget, acceptance criteria and stopping rules.

For either route, record exact application/profile/dependency and observer pins,
remaining limits, backup and rollback evidence, and treatment of existing lots
and pending intents. Obtain Pedro's explicit instruction for that concrete
rollout. Intended behavior changes use a fresh generation. An in-place patch
requires a verified defect restoring documented intent, regression coverage and
separate explicit in-place authorization under AGENTS.md. Historical ledgers,
worktrees and failure evidence stay preserved.

## Maintaining this record

After a real observation, update the generation-bound evidence and outstanding
work here. Link original private receipts by location/hash when useful; do not
commit payloads, credentials or runtime state. When promoting a parked item,
update its [future-work disposition](future-generations.md) and the PR scope
together. Allocate a generation number only during an explicitly approved
deployment. Keep older assessments dated rather than rewriting their findings.
