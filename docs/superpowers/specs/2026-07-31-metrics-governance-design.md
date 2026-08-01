# Metrics and Promotion Governance — P2/P3 Design

**Program:** `2026-07-31-portfolio-integrity-program-design.md`
**Release:** Foundation / `gen_004`
**Status:** Approved for implementation planning

## Goal

Create one versioned semantic source for portfolio and signal metrics, then
prevent learning or capital promotion until clean evidence gates pass.

## Metrics package

Add `tradingagents/strategies/metrics/`. All dashboards, reports, comparisons,
and promotion checks consume it.

The package owns:

- XNYS market-session offsets;
- event, signal, and execution identity;
- 5/10/20/30-session outcomes;
- portfolio daily returns, Sharpe, and drawdown;
- cost and benchmark reconciliation;
- common-window candidate/baseline comparison;
- immutable metric epochs;
- promotion decisions.

No report performs a live benchmark fetch.

## Identity and deduplication

- `event_key`: one underlying catalyst, generation-independent.
- `signal_id`: epoch, strategy, policy, direction, and event.
- `execution_id`: one cohort fill.

Distinct same-day catalysts remain distinct. Repeated horizon/size executions
do not masquerade as independent events. Direction conflicts are explicit
conflicts, not first-record wins.

## Metric definitions

- Directional accuracy uses actionable long/short signals and
  `signed_return > 0`.
- Direction is applied exactly once.
- Neutral signals are reported separately.
- Outcome entry is next-session open.
- N-session outcome exits at the close of the Nth held XNYS session.
- `PortfolioMetrics` is emitted only for one complete, contiguous, valid XNYS
  snapshot window for one cohort and epoch, with complete scoped persisted SPY
  and BIL coverage. An invalid or missing session makes full-window metrics
  unavailable rather than selecting or compounding a disjoint segment.
- Total return uses the net-equity endpoints of that complete window.
- Daily returns use consecutive valid sessions within one epoch.
- Sharpe annualizes daily excess returns by `sqrt(252)` and is hidden before 30
  actual return observations (31 contiguous snapshots).
- Drawdown uses the running peak of net equity in that complete window.
- Successful full-window metrics have zero missing/stale-mark counts; consumers
  must surface unavailable metrics instead of bridging a gap.
- Every target snapshot must satisfy the exact P0 Decimal identities for net
  equity, gross exposure, and net exposure before any return is emitted.
- Strategy contribution uses actual size, marks, and costs.
- Candidate comparisons use paired common-session returns.

## Epoch rules

Execution clock, pricing, cost, behavior, configuration, model, or metric
changes create a new epoch. A critical data gap invalidates/closes the epoch.
Returns never bridge an epoch or invalid session.

Legacy artifacts remain schema v1 and promotion-ineligible. No historical file
is rewritten.

## Benchmark

Persist total-return-adjusted SPY and BIL observations with the same session
and quality rules as the book; BIL is the initial cash proxy. Raw execution
bars are never reused as benchmark returns. For gross exposure `G` and net
exposure `N`:

`matched_return = N * SPY_return + max(0, 1 - G) * cash_return`

Reports show long, short, gross, net, and cash weights.

## Reporting scope

Headline:

- four separate $100k horizon books;
- explicitly labeled equal-weighted scenario-panel return;
- epoch, timestamps, costs, exposures, valid sessions, event/signal/fill/trade
  counts, data-quality flags, and confidence/sample disclosures.

Appendix:

- $5k/$10k/$50k concentration stress tests;
- all-16 heatmap;
- explicit dependent-scenario warning.

Never present summed scenario capital as fund AUM.

## Strategy health

Every strategy session records:

- `signals`;
- `legitimate_no_event` plus evidence;
- `data_failure` plus provider/error evidence;
- `strategy_defect` plus evidence.

Unclassified silence invalidates promotion evidence.

## Learning lock

Production accepts only `LearningPolicy(mode="disabled")`.

- Cohort construction fails closed for another mode.
- Production `run-learning` refuses mutation.
- Metrics and promotion code cannot import mutation-capable learning code.
- Promotion results never modify code, config, state, or generations.

## Promotion policy

Possible results: `WAIT`, `FAIL`, or `ELIGIBLE_FOR_MANUAL_REVIEW`.

Hard prerequisites:

- zero missing/stale marks;
- aligned candidate/baseline/benchmark sessions;
- stable epoch hashes;
- all 12 strategies classified;
- all cost categories present;
- no risk-limit breach.

Initial review:

- 30 clean common sessions;
- 30 independent completed ideas;
- 30 unique matured events for any strategy-specific claim.

Manual real-capital consideration:

- 30 clean common sessions;
- 50 independent completed ideas;
- positive net-of-cost matched excess return;
- closed winners from at least two strategies;
- maximum drawdown at most 15% and no more than two points worse than baseline;
- positive excess return with fills delayed one session;
- positive excess return at 20 basis points slippage per fill.

Passing creates evidence for Pedro's review only.

## Acceptance tests

- Holiday and early-close session offsets.
- Short direction applied once; neutrals excluded.
- Exact 5/10/20/30-session maturity.
- Missing entry/exit price remains invalid.
- Deterministic, order-independent identity and deduplication.
- Known daily Sharpe and drawdown sequences.
- Costs reconcile gross to net return.
- Invalid sessions and epoch boundaries never bridge.
- Candidate/baseline comparison uses common sessions only.
- Production learning cannot be enabled.
- Insufficient evidence waits; integrity/risk failures fail.
- Passing is advisory and produces no mutation.
