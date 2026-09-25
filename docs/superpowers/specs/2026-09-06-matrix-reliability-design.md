# Reliability for the retained strategy and portfolio matrix

## Decision and scope

Retain all 12 strategies and all 16 paper portfolios: four horizons by four
portfolio sizes. Simplify the machinery that supplies and runs this matrix.
The research goal remains to measure prospective event-strategy results and
the contribution of LLM interpretation under explicit execution costs, timing,
and risk rules. Scenario copies of an event are not independent observations.

This decision supersedes the one-family pilot and reduced-matrix recommendation
in the September 6 local diagnosis, `diagnosis-and-plan.md`, under
`/Users/potalora/Documents/Codex/reports/eventedge-2026-09-06/`. Its incident
findings remain relevant. The accounting and replay protections in the existing
reliability and portfolio-integrity designs remain constraints; their historical
deployment instructions do not authorize deployment now.

The first PR addresses operational evidence retention and actionable failure
reasons. The remaining stages below are proposed follow-up work. This document
does not claim that coverage, source fidelity, reliability, or research efficacy
has already been established.

## Evidence and unresolved causes

The September 6 diagnosis inspected production generation `gen_013` and found:

- Two of six scheduled sessions were labeled clean. September 3 nevertheless
  contained `data_failure` health records for `regulatory_pipeline` and
  `commodity_macro`, associated with regulations.gov HTTP 502 responses across
  the four horizons. A clean run label alone does not prove complete inputs.
- September 1 and 2 completed accounting in all 16 books but did not complete
  staging. Recorded candidate issues do not establish the missing staging
  exception; ordinary candidate-only quarantine should permit other candidates
  to proceed.
- September 4 stopped in governed preflight before the daily worker and left no
  daily-history record or retained detailed preflight report. A later read-only
  probe returned ready. It cannot recover the original failure or prove its cause.
- Replaced worker logs and discarded timeout output leave diagnostic gaps.
  Available host evidence did not show a memory kill; historical resource
  exhaustion was not excluded because journal access was unavailable.
- Source/code inspection identified earnings news used as a pseudo-transcript,
  insider buy-row counting that differs from the distinct-insider/two-day
  hypothesis, and a WARN candidate path without required layoff evidence.
  These findings do not prove that affected candidates were traded.

Preserve these uncertainties. Do not reconstruct missing historical exceptions,
replay old trading days to manufacture observations, or infer profitability from
the limited surviving execution evidence.

## Stage 1: Preserve operational evidence — current PR

Retain separate, uniquely identified attempt evidence for screen, governed
preflight, and daily workers, including preflight failures before the daily
worker starts. Each attempt should retain command/phase, session when known,
generation/code identity when available, timestamps, completion classification,
and diagnostic output. Record blocked prerequisites and distinguish timeout,
subprocess exit, malformed result, and provider or worker reasons where the
underlying command supplies them. Do not invent a dependency classification
from a generic exit code. Sanitize secrets and retain partial timeout output.

Store this evidence outside authoritative generation manifests, ledgers, metric
epochs, and accepted economic inputs. Read-only preflight may write operational
evidence without changing those authorities. Finalized attempt artifacts must
not be replaced by subsequent runs. Managed timeouts retain their partial output.
Host loss or SIGKILL before the manager can finish requires later reconciliation
of started and completed attempts; that is not covered by this first PR.
Existing compatibility logs may remain, but cannot be the only copy.

Persist daily worker evidence independently of separately invoked report rendering.
Archiving report-command attempts is follow-on work. CLI failures should identify the
failed phase, available reason, and evidence location. Logging failure itself
must be visible; it must not silently produce a claim that evidence was saved.

Acceptance uses offline command/subprocess fixtures for a preflight block,
worker failure, timeout with partial output, malformed result, and launch failure.
Verify that attempts remain distinct, sanitized evidence survives later runs,
and diagnostic writes do not mutate generation/economic authority. Rich provider
coverage classification and changed research eligibility belong to Stage 2.

## Stage 2: Record input coverage and eligibility

The September 7 combined release includes the source-fetch timeout repair from
PR #38. The following describes the historical defect now covered by regression
tests. A future
that finishes between the timeout decision and a later `done()` check can lose
either its successful payload or its exception, leaving `{}`. The deterministic
[reproducer](../../verification/reproduce_fetch_timeout_race.py) demonstrates both
cases; this does not establish the cause of a historical VPS incident. Define one
deadline partition and classify every source exactly once. Test completed success,
completed exception, and unfinished sources at that boundary without timing sleeps.
Treat this as a separate reviewed behavior change for a new generation.

Expose scheduler completion, accounting validity, required-input coverage,
decision/staging completion, and research eligibility separately. Keep the
existing operational outcome compatible while adding evidence-backed dimensions;
do not treat the presence of 12 health records as proof of healthy inputs.

Define required sources, timestamps, horizon requirements, and eligibility rules
for each strategy/portfolio cell. A successful feed with no qualifying events is
different from an unavailable feed. An outage should mark every dependent cell
and its shared cause without requiring all 12 strategies to be healthy for any
unaffected cell to contribute research evidence. Portfolio-level comparisons
still need their own complete accounting and decision requirements, especially
where strategies share allocation constraints.

Preserve candidate-only quarantine boundaries. Missing governed prices,
benchmarks, corporate actions, held-position obligations, or uncertain ledger
state must still block the affected economic computation. Do not weaken replay,
identity, metric-epoch, or execution guards to improve completion rates.

Acceptance covers a partial source outage, valid zero-event input,
candidate-only rejection, candidate/governed overlap, and a staging failure after
valid accounting. Reports retain the full scheduled-window denominator and
missingness reasons; they must not silently select only favorable clean sessions.

## Stage 3: Share immutable inputs and remove duplicate paths

Use a common daily flow across the retained matrix:

`validate governed inputs -> account for current session -> fetch/persist event inputs -> admit/analyze -> stage next-session intents -> report`

Share accepted source inputs where their cutoff and semantic requirements match.
Apply horizon-specific rules to the same source evidence when valid; do not force
different horizons to share incompatible price windows or decisions. Perform
instrument identity and required-data admission before expensive LLM enrichment.
Centralize bounded retry/deadline policy instead of layering independent fetch
and recovery loops across screen, preflight, and execution.

Governed validation must inspect the exact accepted inputs execution consumes.
Persist input identifiers, provenance, and decisions for offline reporting and
reproducibility. Late data belongs to an explicit new attempt under existing
admission rules; it must not replace already accepted economic inputs. Represent
shared incidents once and reference affected cells. Preserve per-book ledger
transactions, idempotency, costs, and next-session execution.

Acceptance demonstrates fewer provider/analysis calls without altered accepted
decisions or accounting under identical fixtures, and checks retries, crashes,
and repeated invocation against immutable-input and ledger invariants.

## Stage 4: Audit hypotheses and qualify across sessions

Audit every retained strategy against its claimed source, actual fields,
timestamp semantics, and admission rule, starting with the three discrepancies
above. Record proxy inputs explicitly. Each affected strategy needs source-fidelity
fixtures and a reviewed hypothesis before results support that hypothesis.

Build a deterministic consecutive-session incident corpus covering source outage,
candidate and governed-data failures, accounting-before-staging crash, recovery,
duplicate invocation, report failure, non-session dates, and untriggered resting
stops. Verify no duplicate fills/costs, changed prior inputs, hidden invalid
observations, or misleading coverage labels. Run focused tests, the required
non-live suite, lint, and review for each implementation PR.

Before prospective qualification, freeze the scheduled review window, each
cell's coverage rules, intervention budget, event-count requirements, and economic
comparison criteria. Track matrix availability and per-cell eligibility together.
No universal all-12-healthy gate is required for unaffected per-cell research;
broader comparative or promotion claims must meet their own declared coverage
requirements. Operational qualification and statistical evidence of benefit are
separate decisions, with dependence between scenario copies accounted for.

## Delivery boundary

Work proceeds through reviewed topic-branch PRs. No merge or production change is
authorized by this design. Production behavior changes require Pedro's explicit
deployment instruction in the current conversation and a new immutable
generation; the development shadow is never authoritative live state. Preserve
historical state and stop at the PR until deployment is explicitly authorized.
