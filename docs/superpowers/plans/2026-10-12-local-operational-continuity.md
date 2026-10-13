# Local operational continuity implementation plan

This plan's real-clock observation is now optional and paused. The subsequent
[finite workflow acceptance plan](2026-10-12-luna-workflow-acceptance.md) supersedes
the five-session engineering prerequisite. Retain this implementation and its
evidence for a later explicitly chosen live observation.

> **For agentic workers:** Use the existing Superpowers test-first and verification workflows, with one bounded implementation agent for generation configuration. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish five consecutive eligible live-source paper sessions on persistent isolated books, with useful complete execution and immutable replay, before claiming initial operational acceptance.

**Architecture:** Reuse `GenerationManager` and `CohortOrchestrator`, their locks, worktrees, run history, accepted evidence and recovery mechanisms. Add optional frozen configuration-file plumbing to the ordinary CLI, plus a separate local acceptance record that reuses existing accounting/staging checks and explicitly classifies approved reduced scope. Production readiness semantics remain unchanged.

**Tech stack:** Existing Python CLI, SQLite paper ledgers, Git worktrees, XNYS calendar, local sampled process watchdog and Codex thread heartbeat.

**Spec:** Pedro's October 12 correction that full live end-to-end acceptance must remain stable over time, the existing five-session continuity target, and [the forward-test protocol](../../research/2026-10-09-forward-test-protocol.md).

## Constraints and starting evidence

- The verified engineering baseline is `a5ba9d9c53830353817c5c558e2985d18ed83e9c`. A supported configuration handoff is missing, so the acceptance candidate will be a new tested commit, explicitly identified before launch.
- Preserve Luna pilot results at `7401b4a`; no further extraction work or model calls for that pilot. Optional document input is not used by the acceptance workflow.
- Keep `configs/research-without-bulk-filings.json`: `filing_research_policy=disabled_v1` and `research_resilience_policy=dependency_isolation_v1`. Approved EDGAR exclusions remain explicit; no further source disabling.
- Keep Luna/high assessment and Sol/high thesis/committee, native ChatGPT-plan authentication, no paid API fallback, PAPER mode and real execution disabled.
- Keep source/model/per-call budgets at 900/2400/120 seconds. Reuse the reviewed outer 3500-second, 8 GiB sampled aggregate RSS, 96-process, 1 MiB console and three-second cleanup bounds. Replay retains its existing 110/120-second bounds. Do not increase storage limits.
- Use current clocks and sources. At most one planned live attempt per eligible session, no overlap, same-day success chasing, reset or fixture substitution. Replay is an independently labeled zero-call operation, not another live attempt.
- Preserve all mandatory accounting, held/due-price, time, risk, persistence and evidence guards, including exits/stops. Never manufacture trades.
- No merge, production/VPS deployment, production retry or real trades. Isolated local state and its manifest are separate from all production state.

## 1. Supported frozen generation configuration

- [ ] Add failing tests for optional `start --config-file`, exact byte/hash freezing, deep merge without mutating defaults, worker hash rejection, manager-owned state and legacy manifest compatibility.
- [ ] Add the smallest optional configuration handoff to `scripts/run_generations.py`, `GenerationManager` and `scripts/run_cohorts.py`, with a bounded shared loader if useful. No option preserves existing behavior. Never persist credentials from environment into the profile.
- [ ] Bind the frozen profile to the generation and existing metric semantic identity. Reject changed/missing profile bytes before work. Review and run only relevant configuration/manager/CLI tests.

## 2. Small local acceptance record

- [ ] Reuse existing read-only accounting/staging and identity validation. Distinguish approved filing exclusions from unexpected degradation without changing `release_readiness.py` production gates.
- [ ] Require one ordinary manifest daily attempt, all 16 books/four horizons, accepted live source and screening evidence, actual successful native model work, committee/staging/report completion, timely publication and anchored zero-call replay with unchanged economic effects.
- [ ] Require five consecutive actual eligible sessions on the same generation/configuration and continuous books. All-held/failed research, pings, fixtures, repeated same-day calls and empty-book accounting alone do not qualify. Record legitimate evidence-informed abstention separately from unavailable-research holds.
- [ ] Retain actual recovery on a later normal attempt without code/config edits, resets or operator repair. Reuse valid existing perturbation tests; do not launch another broad fault campaign. Mark carried-position/fill lifecycle unobserved until observed.
- [ ] Add narrow integrated regressions for approved exclusions, unexpected degradation, native-model/replay requirements, continuity and duplicate-session rejection.

## 3. Concrete persistent local continuation

- [ ] Freeze a tested candidate/configuration and one isolated local repository with ordinary `data/generations` and `.worktrees/gen_NNN` lifecycle. Record exact commit, profile hash, runtime/dependency identities and the reviewed watchdog.
- [ ] Prepare the ordinary manager daily path with current reference-session checks, durable per-session status/evidence, bounded cleanup and protected-state verification. Never require fresh books after initialization.
- [ ] Schedule the same chat's eligible local continuations. The first valid reference is October 12, 2026; the window opens at 16:00 Eastern and closes at the next XNYS opening, October 13 at 09:30 Eastern. Re-evaluate the actual calendar before every attempt.
- [ ] Keep a compact durable status with observed sessions, consecutive qualifying count, failures/degraded scope, recovery, replay, lifecycle observations and next action. Notify only a meaningful change, failure or decision.
- [ ] Prepare the production rollout and health/alert contract for Pedro's later explicit deployment decision. Local live-source proof cannot establish production scheduler/host reliability or future provider uptime.

## Verification and stopping rule

Preserve the 95 passing Luna focused checks and original engineering evidence; they do not satisfy operational acceptance. Run focused tests for new integration and the required CI once the final candidate changes are committed. Stop expansion after the concrete runner/status/schedule are verified; wait for real eligible sessions through scheduled continuations. Initial local acceptance needs five qualifying consecutive sessions plus recovery/replay evidence. Report the observed period and remaining production authorization/runtime limitations honestly.
