# Local operational continuity: preparation and acceptance contract

Full live end-to-end acceptance remains pending. Engineering checks and the retained Luna document pilot do not establish reliable daily operation. The next step is a frozen local paper candidate running the ordinary generation lifecycle across real sessions on the same books.

## Runtime and scope

Use `scripts/run_local_continuity.py` in a separate standalone local clone. `prepare` calls `GenerationManager.start_generation(..., config_file=...)`, creates one detached generation worktree and freezes exact configuration bytes. `continue` calls the ordinary `GenerationManager.run_daily(session)` once per actual eligible session. Existing generation locks, accepted source/screening checkpoints, sixteen cohort ledgers, accounting, staging and run history remain authoritative for this local workload. Books persist between sessions; no reset is permitted.

The private runtime contract records the exact candidate commit, generation, profile digest, dependency/runtime identity, dotenv digest, allowed environment digest and reviewed Mac watchdog. It rejects changed files, injected application code, another generation or a changed runtime before launch. Credentials stay outside Git. Native account authentication may use its supported refresh lifecycle. The configuration file itself contains no credentials, and the manager owns the state directory.

Keep the existing approved source identity and `configs/research-without-bulk-filings.json`. Bulk EDGAR research stays explicitly unavailable under `disabled_v1`; all other configured sources and mandatory safeguards remain in scope. Luna/high handles bounded assessment, Sol/high handles thesis and portfolio committee work, all through native ChatGPT-plan billing. PAPER mode and real execution disabled remain mandatory. The optional Luna attachment pilot is unused here.

Source/model/per-call budgets remain 900/2400/120 seconds. Reuse the reviewed 3500-second outer watchdog with sampled aggregate RSS at 8 GiB, 96 processes, 1 MiB outer console output and three-second cleanup. Replay uses a separate 120-second outer watchdog and 110-second model budget. Existing source/document bounds remain unchanged; private retained data stays below the existing 8 GiB capture ceiling with 24 GiB minimum free disk before launch. Sampling is not a kernel memory/disk quota.

The first prospective reference is October 12, 2026, after its actual 16:00 Eastern close and before October 13's 09:30 Eastern opening. The same chat's weekday 16:05 continuation must re-evaluate the actual XNYS calendar each time. Local scheduling requires the Mac and app to be running. A missed or interrupted session remains visible and cannot be repaired by backdating, resetting books or chasing success with another same-day attempt.

## What counts

The collector reads retained artifacts under the existing shared runtime lock. A qualifying session needs all of these:

- One normal manifest daily attempt, exact candidate/profile/epoch identities and all sixteen books across four horizons.
- Strict persisted accounting and completed valid staging, including accepted prices, benchmarks and session phases.
- Accepted live source and research-screening checkpoints from within that attempt, matching durable strategy health and every book's screening receipt.
- Validated native model outcomes and completed native committee decisions, including legitimate evidence-informed abstention. A healthy no-event day can avoid unnecessary calls, but the five-session window must contain actual successful Luna and Sol work. Failed research, research holds and rule-only fallbacks do not qualify.
- Normal attempt start after the actual reference close and finish before the next actual opening, with a complete durable operational report.
- Independently supervised zero-call replay on a copy of the same persistent state. All sixteen results must report completed replay. Every SQLite table/schema, accepted source/screening document and original run artifact must remain unchanged. No financial or operational table exemptions apply. Transient SQLite coordination sidecars are not economic state.
- Owned processes reaped and frozen inputs unchanged. A failed claimed session is never automatically retried that day.

Only the documented strategy retirements and the precisely configured original EDGAR `filing_research_disabled` failure are accepted scope exclusions. Unexpected provider/strategy failures remain explicit. Production readiness checks are unchanged and may continue to reject reduced source coverage.

Initial local operational acceptance requires five distinct consecutive eligible sessions on the same generation, commit, configuration and epoch, plus observed native Luna/Sol success and a source failure recovering on a later normal qualifying attempt without code/configuration changes, book reset or operator repair. Keep the five-session streak, actual recovery, source coverage and carry/fill lifecycle as separate facts. Do not manufacture trades; unobserved position/fill behavior stays unobserved.

## Durable status and alerts

The clone's `data/local-continuity/STATUS.md` and `status.json` report observed sessions, the current streak, reasons for failure, approved exclusions, replay, recovery and lifecycle observations. Each session retains its ordinary manager evidence, report, supervision, copy/replay anchor and receipts. The collector validates the receipt/anchor contract; the separately supervised replay produces the underlying hash comparisons. These local records are evidence, not a promise of future provider availability.

`health` is read-only. It reports a scheduled attempt missing after 16:15 Eastern, an incomplete claimed session after the fixed execution/replay bounds, a nonqualifying observed session, continuation failure or frozen identity drift. It never starts a repair or retry. The scheduled chat runs the continuation, checks health and notifies only on a meaningful milestone, failure, recovery or decision; unchanged/waiting states stay quiet. Calendar/duplicate checks happen before provider/model acquisition.

## Later production decision

No merge, VPS deployment, production rerun, active-generation change, service/timer mutation or real trade is authorized by this work. Local completion cannot establish deployed host or scheduler reliability.

After local acceptance, present Pedro the exact reviewed commit/profile, observed session range, coverage/recovery/replay evidence, resource bounds and remaining lifecycle gaps. A later explicitly authorized production rollout must create a new generation through the maintained deployment workflow, retain the existing production books/history, verify server-specific native authentication and source access, and confirm the actual scheduler's first eligible run and health/alert path. Do not transplant local books or authentication state to production. Rollback means stopping the new generation through the maintained lifecycle and preserving evidence; no history rewrite or destructive reset.

Ongoing production health must separately report scheduler freshness, bounded process completion, native authentication/model success, mandatory accounting/prices/staging, optional source coverage, report freshness, replay/idempotency and resource headroom. Missing runs, unreaped processes and mandatory guard failures require actionable alerts. Optional outages should preserve safe exits/stops, expose the reduced coverage and recover on a later normal attempt. No alert should trigger an unbounded retry storm or automatic policy reduction. Verify alert delivery in the actual authorized runtime before claiming deployed operational acceptance.

## Verification disposition

Focused regressions cover exact profile freezing/legacy compatibility, secret and corruption rejection, accepted-artifact provenance, approved exclusions, real committee selection/abstention, missing native/replay evidence, distinct consecutive sessions, later recovery, standalone clone/environment boundaries, SQLite WAL copying and missed/incomplete scheduling. Full non-live CI is required for the final candidate before its first live-source continuation. Existing engineering and pilot evidence is reused; this is not another provider-success or fault-injection campaign.

The final focused selection passed 89 tests on October 12. It reported one installed `websockets.legacy` deprecation warning. The earlier generation/lock/CLI checks and retained Luna pilot evidence remain separate; their overlapping test counts are not additive.

Preparation does not count as a live session. The durable private runtime record identifies the final tested candidate and starts at 0/5. Real eligible sessions and later normal recovery remain necessary.
