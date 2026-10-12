# Research fault containment for the next generation

Status: approved for implementation following Pedro's request for a system that can finish safely despite unpredictable research-data quality and explicit answer, "Implement this resilience policy." This prospectively changes the earlier requirement that every parser failure block operational acceptance; original full-source/full-analysis acceptance remains false when gaps exist. Implementation and verification remain pending. No deployment is authorized. The separately approved SoundThinking whole-filing quarantine and next-generation Luna extraction pilot keep their own scopes.

## Intended behavior

A run should complete independent work when a research document, provider or model response is unusable. It must retain the failure, exclude the affected evidence and dependent decisions, and report incomplete coverage. Predicting whether every bad document is economically unimportant is unnecessary: unknown evidence grants no authority to trade. Known low-value exclusions reduce repeated work and alert noise; they are not the main fault-containment mechanism.

Operational completion and research completeness remain separate results. A completed degraded run may have safe accounting and staging with incomplete research. Neither that result nor an intentional no-entry decision establishes full-source/full-analysis acceptance, predictive performance or deployment readiness.

## Existing implementation

- `RunOutcome` already distinguishes `clean`, `degraded` and `failed`; both clean and degraded outcomes count as completed runs. Daily result aggregation marks a run failed for actual cohort errors rather than source-coverage warnings alone.
- Source acquisition retains partial data and provider failures; strategy screening catches individual strategy failures. Durable source health records `data_failure` and `strategy_defect` separately from execution validity.
- Candidate input/price quarantines, non-actionable journal records and committee abstention already exist. The source-reliability campaign tests an EDGAR outage as a completed degraded run with valid execution and incomplete coverage.
- Ledger phases, staged intents and fills have transactional completion markers and replay checks. Recovery coordinates separate stores; it is not one transaction across all cohorts.

The main coarse boundary is EDGAR: one incomplete required filing can make the entire source projection unavailable. The screen preflight also deliberately requires complete source coverage. Those gates currently answer stricter questions than whether the daily process can finish safely. Shared-source bundle identity, storage and replay failures are a different class and must not be casually downgraded.

## Proposed containment boundary

| Failure | Allowed continuation | Required record |
| --- | --- | --- |
| Unusable research document or invalid model assessment | Continue independent records; exclude the failed record and every dependent assessment, candidate and entry intent | Exact source/attempt identity, retained original or acquisition failure, reason, affected dependencies and coverage loss |
| Provider unavailable or discovery incomplete | Disable that provider's affected scope; continue strategies with independently satisfied requirements | Provider availability, incomplete/unknown discovery scope, attempts and affected strategies; never label it no-event |
| Known low-value evidence | Apply a prospectively defined exclusion, preserving evidence and suppressing repeated repair attempts | Versioned exclusion rule, exact scope, justification and all dependent restrictions |
| Shared model deadline exhausted | Preserve the existing sample-integrity hold; do not trade only the fastest completed prefix | Completed, rejected and unfinished attempts; durable no-new-entry decision |
| Corrupt accepted evidence, ambiguous dependency identity, replay conflict or failed proof persistence | Preserve the integrity failure and block affected execution/staging; recover through existing verified state | Failure identity, last committed phase and recovery requirements |
| Untrusted positions, cash, held/due prices or execution state | Block execution that requires those inputs | Missing/corrupt protected dependency; never substitute stale or invented values |

The source envelope must retain failed records even when the signal projection excludes them. An admitted candidate must have explicit references to the successful records it depends on. A strategy-level health warning alone is not a sufficient order veto. Where dependency independence cannot be proved, exclude the larger affected scope.

## Safe research hold

Use the existing `clean/degraded/failed` lifecycle and add a durable admission reason rather than inventing another competing success flag. A classified research outage may produce **completed with research gaps; no new entry intents**.

This must preserve validated exits and protective-stop staging through the existing ledger transaction. An early return from screening currently risks skipping that work. Previously staged entry intents may already have filled in the execution phase before today's research; report those actual fills rather than claiming that no trades occurred.

Unknown internal exceptions must not become successful runs merely because an `execution_valid` flag is true. Missing or corrupt accepted volatility evidence, provenance conflicts and staging defects remain real failures. When trusted state is unavailable, preserve the last committed state and recovery evidence rather than manufacturing a completed accounting result.

## Bounded operation and recovery

Keep the existing source/model/per-call and resource budgets. Retry only classified transient failures within those budgets. A persistently bad source record must not monopolize a run. Reuse an exclusion only for its bound identity or prospectively defined rule; changed evidence must be evaluated under the current policy.

Resume from durable phases and frozen evidence. Verify that a completed hold cannot silently turn into an entry decision on retry, and that neither fills nor staging are duplicated. Host/process failure cannot be prevented in every case; the required property is safe termination and verified recovery.

## Acceptance tests for implementation

1. Mix a valid SEC filing with a previously unseen malformed filing. Preserve both outcomes and exclude every dependency of the bad filing while independent eligible work completes.
2. Inject source outages, incomplete discovery, malformed model output and exhausted model budgets. Verify the correct contained scope and distinguish a gap from a legitimate no-event result.
3. Prove that a research hold commits zero new entry intents while still staging justified exits/protective stops with trusted inputs.
4. Corrupt a protected price, account state, accepted evidence digest or replay chain. Execution must remain blocked; no generic degraded-success conversion is allowed.
5. Interrupt before and after phase/staging commits, then resume from frozen evidence. Verify identical decisions and no duplicate economic effects.
6. Run all four horizons and sixteen paper books, then independently replay without new source/model calls. Report safe operational completion, coverage, exclusions, failures and full-coverage acceptance separately.

Current retained examples illustrate the distinction: Nova Minerals' standard CFO certification has low incremental information; a trivial signature tag in Lantern's financing agreement and a malformed image in Pinnacle West's financial presentation do not make those entire documents immaterial. Their failure should constrain the dependent decisions rather than trigger arbitrary document-wide assumptions.
