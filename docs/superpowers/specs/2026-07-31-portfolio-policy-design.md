# Deterministic Portfolio Policy — P1 Design

**Program:** `2026-07-31-portfolio-integrity-program-design.md`
**Release:** Candidate / `gen_005`
**Baseline:** Foundation / `gen_004`
**Status:** Approved for implementation planning

## Goal

Turn ranked trade recommendations into a prospective portfolio that respects
transparent name, strategy, cluster, short, margin, and risk-contribution
limits.

## Placement

`PortfolioCommittee` remains an idea-ranking and initial-sizing component.
`PortfolioPolicy.apply()` runs afterward for both LLM and fallback paths.
`RiskGate` performs final fill-time validation against the latest ledger state
and pending intents.

```python
class PortfolioPolicy:
    def apply(
        self,
        recommendations: list[TradeRecommendation],
        context: PortfolioRiskContext,
    ) -> list[TradeRecommendation]: ...
```

Policy is deterministic. It adds no API or LLM calls.

## Risk context

`PortfolioRiskContext` includes:

- marked portfolio value and cash;
- open lots and pending intents;
- ticker, direction, sector, strategy tags, and risk tags;
- current marked exposure;
- annualized volatility;
- earnings dates and short-interest/borrow state;
- margin and cash-buffer state.

It is built once per cohort from ledger state and the existing 60-session price
cache.

## Attribution

Candidates, recommendations, intents, fills, and projections preserve
`event_key`, `signal_ids`, `strategy_tags`, and `risk_tags`.

A position counts fully against every contributing strategy and risk tag.
Reporting deduplicates shared ideas across cohorts; execution caps never couple
separate scenario books.

## Constraints

| Constraint | $5k | $10k | $50k | $100k |
|---|---:|---:|---:|---:|
| Generic strategy exposure | 50% | 40% | 25% | 20% |
| Event-cluster exposure | 25% | 20% | 15% | 10% |
| Position risk contribution | 40% | 35% | 30% | 25% |
| Congressional exposure | 25% | 20% | 15% | 12% |

Risk contribution is:

`abs(weight) * max(60-session annualized volatility, 15%)`

normalized across the prospective book. Its cap activates at four positions.

Final validation covers:

- ticker/direction duplication;
- position, sector, strategy, and event-cluster exposure;
- single-short, correlated-short, total-short, and margin exposure;
- cash reserve and margin cash buffer;
- position risk contribution;
- earnings blackout and borrow availability.

## Congressional behavior

- Use disclosure publication/availability as event time.
- Require publication by the decision cutoff and within seven calendar days.
- Purchases require at least two distinct members.
- Minimum amount bucket: `$15,001 - $50,000`.
- Maximum two purchase candidates.
- Sale signals are journal-only.
- Stable event ID includes source, member, ticker, direction, transaction date,
  publication date, and amount.
- A consumed event cannot reopen a stopped position.
- Risk tags include strategy, normalized member, and disclosure week.

## Stops and order admission

P0 provides authoritative resting-stop execution. P1 requires exits to settle
before entries so the daily-loss, drawdown, cash, and margin gates influence
same-session admission.

No report describes an 8% trigger as an 8% loss cap.

## Covered calls

`options_overlays_enabled` remains false. Dashboard, README, and diagrams label
covered-call code as scaffolded and inactive.

Premium cash flows, assignment, expiry, contract selection, and contract marks
are out of scope and require a separate generation.

## Headline portfolio

The four $100k horizons are the headline scenario panel. They remain separate
books and may also be shown as an explicitly labeled equal-weighted panel.

Smaller books are concentration stress tests, not capacity estimates.

## Acceptance tests

- Policy runs for LLM and fallback committee paths.
- Current and pending positions affect the prospective book.
- Every constraint scales/rejects deterministically.
- Unknown volatility uses the 15% floor.
- Risk-contribution cap waits until four positions.
- Every contributing tag receives full exposure.
- Short interest, earnings, margin, and cash reach fill-time validation.
- Congressional publication cutoff prevents lookahead.
- Member, amount, recency, candidate, and consumed-event rules hold.
- Sale signals cannot create orders.
- Cohorts remain operationally independent.
- Covered-call capability is reported inactive.
