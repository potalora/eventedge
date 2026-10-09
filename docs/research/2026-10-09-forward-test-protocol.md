# Forward test after the assumption audit

This protocol is fixed before the next generation starts. Its purpose is to learn whether EventEdge produces economically meaningful excess returns under reproducible information and execution rules. The repair itself is not evidence of alpha. Deployment and its starting date remain unapproved.

## Decision and scope

The primary portfolio is `horizon_3m_size_100k`. The three other $100k horizons are dependent sensitivity scenarios; the twelve smaller books test concentration and minimum-ticket effects. Do not average their AUM or count them as independent replications. Do not select the best book after seeing results.

Preserve existing sizing, exposure, conviction and stop thresholds except repairs to their documented units or application. Freeze model identifiers, prompts, strategy configuration and source/clock contracts within a generation. Automatic learning and promotion remain disabled. A material change starts a new generation and evaluation window; earlier results stay labeled with their original contracts. Data failures remain visible and are not retrospectively filled with later knowledge.

The state-economics strategy is explicitly retired: national indicators plus sector momentum do not establish a state-level event. Filing occurrence, unrelated company-name matches and contractor momentum no longer substitute for the named catalysts. Deterministic rules require their specified evidence; content-dependent theses require source text and valid analysis. This may reduce trade count. That is preferable to evaluating an unlabeled mixture of hypotheses.

## Economic conventions

- Decisions use only information available by the recorded cutoff and enter at the next exact XNYS open. Prior next-open exits and open-gap stops can release cash first. Intraday high/low stops settle after opening entries and never finance them retroactively. Their close timestamp denotes unknown intraday timing, not an observed execution time.
- A complete candidate first acquired after the close waits in its immutable ledger observation. At the next eligible close, its original facts, direction, score and model analysis enter the normal price, volatility and portfolio checks, even if the source window has changed. Reacquisition never backdates new facts or replaces that queued thesis. Each canonical event receives one timely offer, including when the committee chooses no trade.
- Entry checks include adverse slippage and explicit fees, coherent opening valuation, marked short collateral and durably reconstructed net session losses. Existing limit percentages stay fixed. Calendar-day borrow/financing is charged on prior accepted closing exposure through the next session; new positions begin accrual at the following session.
- Corporate-action absence must be evidenced. Splits precede post-split per-share distributions. Missing action coverage blocks valid accounting or signal outcomes as applicable. Short collateral is a conservative paper reserve; this model does not establish real broker locates or forced-liquidation behavior.
- SPY and BIL returns use adjacent adjusted prices from the same acquisition vintage, retained as immutable evidence and accumulated into a coherent index. Earlier unpaired benchmark histories are not silently repaired or blended into the new results.
- Signal outcomes are next-open through Nth-session-close total shareholder returns, direction-signed and gross of hypothetical costs. Only executed portfolio ledgers establish net tradable performance. Directional accuracy and Clef support probabilities are diagnostics, not substitutes for portfolio returns.

EFTS filing searches and USAspending windows are exhaustive within their stated query bounds and acquisition budgets. Form 4 acquisition remains a bounded issuer sample: at most 40 recent filings per requested issuer, with date bounds, known counts, possible archived records and truncation retained in source health. CourtListener, Regulations and congressional feeds also report their bounded scope. None of these samples establishes exhaustive market coverage.

Continuous corporate-action coverage for untraded signal outcomes currently shares the governed market-input boundary with portfolio accounting. Missing evidence for an outcome-only ticker can therefore interrupt all books and close the metric epoch even when held assets are priceable. This conservative availability tradeoff is accepted for this release; track those gaps explicitly and count only valid periods. Separating outcome availability later requires its own durable evidence and recovery contract, not an assumption of zero corporate actions.

COT observation dates are not publication dates; the CFTC documents delayed publication and a release calendar. Current acquisitions use conservatively known availability, and historical reconstruction without release evidence is refused. FRED historical acquisitions bind a vintage. These choices follow the [CFTC release contract](https://www.cftc.gov/MarketReports/CommitmentsofTraders/ReleaseSchedule/index.htm) and [FRED real-time periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html).

## Fixed readouts and decision rule

At 30, 60 and 90 completed exchange sessions, review continuity, missing/invalid periods, source and analysis coverage, trade count, net return, benchmark excess, maximum drawdown, volatility, turnover, carrying costs, concentration, and dependence on single names/catalysts. Review problems and risk breaches immediately; do not change the thesis solely because a short window looks poor or strong. Report actual valid return count: 30 returns normally require 31 valid snapshots.

Treat the following as a **research hurdle**, not a forecast: at least 5 percentage points of annualized net excess versus SPY, positive excess versus the exposure-matched SPY/BIL benchmark, annualized net Sharpe of at least 1, and maximum drawdown no worse than the existing 15% drawdown limit. Meeting these after a short period only justifies continued observation.

Do not make a strong alpha claim before at least 252 valid out-of-sample daily returns. One year is a minimum review point, not proof: disclose uncertainty, market-regime concentration and dependence among trades. Before any capital decision, require uncertainty estimates that respect serial dependence/overlapping events, sensitivity to higher costs and removal of the largest contributor, and evidence that results are not just market/sector exposure. If the sample cannot support those checks, the conclusion is inconclusive. No automatic promotion follows from a threshold pass.

Descriptive event studies use a common total-return basis and contiguous exchange sessions. Catalyst-date returns may precede a recorded decision and cannot be called executable alpha. IID t-tests/confidence intervals over overlapping journal events are withheld; use the governed forward outcomes and primary net portfolio for the decision above.

## Release acceptance

Before an authorized launch: component regressions and native provider→strategy→ledger paths must pass, lifecycle/report commands must preserve authoritative state, and the whole non-live suite must pass. Preserve audit evidence and resolve independent review findings. Use a fresh generation/metric epoch; do not patch these behavior changes into the active experiment. Five clean sessions establish operational continuity only. They do not meet the performance hurdle.
