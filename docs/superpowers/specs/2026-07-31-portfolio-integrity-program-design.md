# Portfolio Integrity Program — P0 to P3 Design

**Date:** 2026-07-31
**Status:** Approved for implementation planning
**Branch:** `codex/p0-p3-portfolio-integrity`
**Legacy generation:** `gen_003` remains immutable and promotion-ineligible

## Objective

Make EventEdge's paper-trading evidence executable, auditable, and useful for
portfolio decisions. The program repairs the execution and accounting model,
centralizes performance measurement, introduces deterministic portfolio
constraints, and prevents promotion or learning until explicit evidence gates
are satisfied.

This design does not rewrite historical results. Existing `gen_003` artifacts
remain available as legacy diagnostic evidence, labeled with their known
same-bar-close fill model and missing cost/provenance limitations.

## Non-negotiable boundaries

- Production remains paper-trading only.
- Automated learning remains disabled and cannot be enabled by configuration or
  environment variables.
- A promotion decision is advisory and requires Pedro's manual approval.
- Existing generation history is never rewritten.
- A missing or stale price invalidates the affected valuation session.
- The 16 cohorts are scenario portfolios, not independent observations or one
  combined fund.
- Behavioral changes require a fresh generation and a clean performance epoch.
- No new live API calls are added to portfolio-policy evaluation; it reuses the
  shared price and enrichment cache.
- Peak memory must remain well below 8 GB on a 16 GB M4 MacBook Air.
- External APIs and LLMs remain mocked in unit tests.

## Approaches considered

### 1. Clean paired relaunch — selected

Build an execution/metrics foundation first and launch a fresh corrected
baseline. Add the behavioral portfolio policy in a second commit and launch a
fresh candidate before the same scheduled cycle. This creates two comparable
books with the same execution, accounting, cost, metric, and data-quality
semantics.

### 2. Patch `gen_003`, then launch a candidate

This is faster but invalid. `gen_003` already contains retroactive fills,
missing-mark sessions, and positions entered under incompatible semantics. An
epoch marker cannot reconstruct provenance that was never stored.

### 3. Launch one generation containing every change

This yields a clean book but no attribution between accounting repairs and
investment-policy changes.

## Release structure

### Foundation release

The foundation contains P0, P2, and P3, plus truthful dashboard and
documentation changes. After merge, it launches `gen_004` with fresh state.

### Candidate release

The candidate adds P1 behavior changes without changing the foundation's
execution, metric, or cost definitions. After merge, it launches `gen_005`
with fresh state before the same first scheduled run as `gen_004`.

`gen_003` is archived as legacy observation-only evidence. It is not used as
the promotion baseline.

## P0 — Executable orders and authoritative accounting

### Session lifecycle

The production paper-accounting cycle moves from 10:00 ET to 18:00 ET on US
market sessions, after the daily bar is expected to be final.

For session `D`, the engine performs these phases in order:

1. Load entry and exit intents created after session `D-1`.
2. Fetch raw, unadjusted OHLC and corporate actions for exact session `D`.
3. Validate that every required ticker has an exact `D` bar.
4. Execute due exits, then entries, at `D` Open with configured costs.
5. Accrue short borrow and financing costs exactly once.
6. Apply splits and cash dividends with direction-aware ledger entries.
7. Mark every open position at `D` Close.
8. Persist an immutable account snapshot and benchmark observation.
9. Screen only information available by the `D` close cutoff.
10. Create signal records and queue intents for the next market session.

The one-session delay is deliberate. A fill can only use a price that occurs
after the order intent existed.

### Market data contract

```python
@dataclass(frozen=True)
class MarketBar:
    ticker: str
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    source: str
    fetched_at: datetime
    adjusted: bool
```

`PriceSource.get_daily_bars()` accepts an inclusive session range, internally
handles yfinance's exclusive `end`, sets `auto_adjust=False`, and asserts that
the requested terminal session is present. Execution bars must be raw.

Missing, nonpositive, NaN, future-dated, adjusted, or stale bars fail closed.
There is no entry-price fallback.

Market-session calculations use an XNYS exchange calendar rather than a
manually maintained holiday list.

### Signal, intent, and fill provenance

```python
@dataclass(frozen=True)
class SignalRecord:
    signal_id: str
    event_key: str
    strategy: str
    ticker: str
    direction: Literal["long", "short", "neutral"]
    event_at: datetime | None
    observed_at: datetime
    reference_session: date
    reference_close: Decimal
    decision_at: datetime
    evidence_hash: str


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    signal_ids: tuple[str, ...]
    cohort_id: str
    side: Literal["buy", "sell", "short", "cover"]
    requested_qty: int
    created_at: datetime
    eligible_session: date
    price_rule: Literal["next_session_open", "resting_stop"]
    status: Literal["pending", "filled", "rejected", "cancelled"]


@dataclass(frozen=True)
class Fill:
    fill_id: str
    intent_id: str
    session: date
    effective_at: datetime
    processed_at: datetime
    reference_price: Decimal
    fill_price: Decimal
    quantity: int
    slippage: Decimal
    commission: Decimal
    other_fees: Decimal
```

IDs are deterministic and idempotent. A rerun for the same cohort/session
cannot duplicate a fill, borrow charge, corporate action, or snapshot.

### Authoritative ledger

Each cohort gets a small SQLite ledger in its state directory. SQLite is the
authoritative source for:

- signals and source-event identities;
- order intents and status transitions;
- fills and cost components;
- cash and position-lot mutations;
- realized and unrealized P&L;
- borrow, financing, dividends, splits, and fees;
- closing marks and mark provenance;
- account equity, gross/net exposure, margin, and high-water mark;
- immutable metric epochs.

All mutations for a fill and account update occur in one transaction. A crash
cannot leave cash updated without the corresponding lot or fill.

`paper_trades.json` and `equity_snapshots.jsonl` remain generated,
read-compatible projections during migration. They are not accounting
authorities.

Cash and P&L use `Decimal` or integer minor units. Binary floats may be used for
analytics after ledger values are read.

### Cost model

The initial paper cost model is explicit and versioned:

- 10 basis points of adverse slippage per equity fill;
- zero equity commission, persisted explicitly;
- direction-aware regulatory/other fee hooks, defaulting to zero when no fee is
  configured;
- short borrow accrued ACT/365 from the existing conservative rate tiers;
- unknown borrow availability rejects a new short;
- configured margin financing accrued daily when applicable;
- idle-cash yield defaults to zero, a conservative assumption.

Every snapshot reconciles gross equity, each cost category, and net equity.

### Stops

Stops are persisted as resting intents. For a long:

- if the session opens below the stop, execute at the opening price;
- otherwise, if the session low crosses the stop, execute at the stop;
- apply adverse slippage to the observed execution basis.

Shorts use the mirrored rule. Reports call the threshold a stop trigger, never
a loss cap. Exits settle before new entries so same-session risk gates and
buying power see the realized outcome.

### Broker compatibility

`PaperBroker` becomes a ledger-backed adapter. `PaperTrader` becomes a
compatibility projection over authoritative fills.

`AlpacaBroker` gains stable client order IDs and pending-order reconciliation,
but live execution remains disabled. The engine never persists an unconfirmed
external order as a fill.

## P1 — Deterministic portfolio policy

### Policy placement

The committee remains responsible for ranking and initial sizing. A new
deterministic `PortfolioPolicy` runs after both LLM and rule-based committee
paths and sees the complete prospective book.

```python
class PortfolioPolicy:
    def apply(
        self,
        recommendations: list[TradeRecommendation],
        context: PortfolioRiskContext,
    ) -> list[TradeRecommendation]: ...
```

`RiskGate` then hard-revalidates each fill using current marks, pending intents,
and updated account state. Constraints cannot exist only in an LLM prompt.

### Attribution identities

`Candidate`, `TradeRecommendation`, intent, fill, and trade projections carry:

- `event_key` for the underlying catalyst;
- `signal_id` for the strategy/policy decision;
- `execution_id` for the cohort fill;
- `strategy_tags`;
- `risk_tags`.

The full position counts against every contributing strategy and risk tag.
Multi-tag positions cannot evade caps.

### Portfolio constraints

Constraints apply independently inside each cohort. Separate scenario cohorts
are not coupled through one execution cap.

Size-profile defaults:

| Constraint | $5k | $10k | $50k | $100k |
|---|---:|---:|---:|---:|
| Generic strategy exposure | 50% | 40% | 25% | 20% |
| Event-cluster exposure | 25% | 20% | 15% | 10% |
| Position risk contribution | 40% | 35% | 30% | 25% |

Risk-contribution enforcement begins after four positions to avoid a startup
deadlock. Initial risk units are transparent standalone volatility:

`abs(position_weight) * max(annualized_volatility, 15%)`

The engine uses 60 market sessions from the existing shared price cache and
adds no API calls. Covariance-based risk is deferred until the sample supports
it.

Prospective-book validation includes:

- ticker and direction duplication;
- position, sector, strategy, and event-cluster exposure;
- short, single-short, correlated-short, and total margin exposure;
- cash reserve and margin cash buffer;
- position risk contribution;
- earnings blackout and borrow availability.

### Congressional policy

Congressional disclosures use publication/availability time as the event
clock. Transaction date remains metadata.

Candidate defaults:

- disclosure observed no later than the decision cutoff;
- publication within seven calendar days;
- purchases require at least two distinct members;
- minimum amount bucket is `$15,001 - $50,000`;
- maximum two purchase candidates;
- sale signals remain journaled but cannot create orders;
- a stable source/member/ticker/direction/transaction/publication/amount event
  ID prevents the same disclosure from reopening a stopped position;
- $100k books cap congressional exposure at 12%;
- other size caps are 25%/$5k, 20%/$10k, and 15%/$50k.

Risk tags include strategy, normalized member, and disclosure week.

### Covered calls

Covered-call execution is explicitly inactive. UI, README, and diagrams say
that the scaffolding exists but premium, assignment, expiry, contract marks,
and authoritative accounting are not implemented.

Implementing covered calls requires a separate behavior generation.

## P2 — Versioned metrics

### Single semantic source

Add `tradingagents/strategies/metrics/`. Dashboard, reports, generation
comparison, learning-facing diagnostics, and promotion gates consume this
package rather than maintaining separate formulas.

### Metric epochs

```python
@dataclass(frozen=True)
class MetricEpoch:
    epoch_id: str
    generation_id: str
    generation_commit: str
    behavior_hash: str
    config_hash: str
    metric_schema_version: int
    execution_clock_version: str
    pricing_version: str
    cost_model_version: str
    start_session: date
    end_session: date | None
    status: Literal["open", "closed", "invalid"]
    boundary_reason: str
```

Execution, pricing, cost, behavior, configuration, or metric-schema changes
create a new epoch. A critical data gap closes the epoch. Returns never cross
an epoch boundary or invalid session.

Legacy artifacts are labeled
`metric_schema_version=1_legacy_calendar_signed`. `gen_004` and later write
schema v2 artifacts separately.

### Outcomes

Outcomes use 5, 10, 20, and 30 held XNYS sessions:

- entry is next-session open;
- exit is the close of the Nth held session;
- raw return and direction-signed return are separate fields;
- direction is applied exactly once;
- neutral signals are reported separately and excluded from directional
  accuracy;
- missing terminal prices remain invalid;
- catalyst, decision, and execution counts are reported separately;
- distinct same-day catalysts remain distinct;
- cross-horizon policies remain distinct.

### Portfolio metrics

- Total return: final net equity divided by initial net equity minus one.
- Daily return: consecutive valid net-equity sessions within one epoch.
- Sharpe: annualized mean daily excess return divided by sample standard
  deviation, hidden before 30 valid sessions and shown with its sample count.
- Drawdown: net equity divided by its running peak minus one.
- Strategy contribution: realized plus marked contribution after actual sizing
  and costs.
- Candidate comparison: paired, common-session net returns.

Closed-trade P&L is never called portfolio Sharpe or portfolio return.

### Benchmark

Each run persists SPY and cash-proxy observations with the same timestamps and
quality checks as portfolio marks.

For a portfolio with gross exposure `G` and net exposure `N`, the first-order
matched benchmark is:

`N * SPY_return + max(0, 1 - G) * cash_return`

Long and short notionals are also reported separately. Reports perform no
network fetches.

### Headline reporting

The headline view shows the four $100k horizon books separately and an
explicitly labeled equal-weighted scenario-panel return. It never sums them
into fund AUM.

The $5k/$10k/$50k cohorts remain in a stress-test appendix and heatmap.

Every performance surface shows:

- metric epoch and schema;
- valuation and benchmark timestamps;
- gross and net return;
- valid sessions;
- unique catalysts, strategy decisions, fills, and closed trades;
- gross/net exposure and costs;
- missing/stale-mark and strategy-health flags;
- a disclosure that cohorts are dependent scenarios.

## P3 — Learning lock and promotion governance

### Learning lock

Production uses `LearningPolicy(mode="disabled")`. Production cohort builders
reject any other mode. The `run-learning` command refuses to mutate production
generation state.

Metrics and promotion packages cannot import mutation-capable learning code.
Learning classes may only run against isolated temporary test state.

### Strategy coverage

Every strategy run records one of:

- `signals`;
- `legitimate_no_event`, with evidence;
- `data_failure`, with error/provider evidence;
- `strategy_defect`, with error evidence.

An unclassified silent strategy invalidates that session for promotion.

### Promotion decisions

`PromotionEvaluator` returns only:

- `WAIT`;
- `FAIL`;
- `ELIGIBLE_FOR_MANUAL_REVIEW`.

It cannot merge, deploy, retire, enable learning, or modify strategy
configuration.

Hard integrity prerequisites:

- zero missing or stale marks;
- candidate, baseline, benchmark, and cash series cover identical sessions;
- stable epoch hashes;
- no return crosses an invalid session or epoch boundary;
- all 12 strategies classified;
- costs and borrow are present, even when explicitly zero;
- no risk-limit breach.

Initial research review requires:

- at least 30 clean common sessions;
- at least 30 independent completed ideas;
- strategy-specific claims supported by at least 30 unique matured events.

Manual real-capital consideration requires:

- at least 30 clean common sessions;
- at least 50 independent completed ideas;
- positive net-of-cost excess return versus the exposure-matched baseline;
- realized winners from at least two strategies;
- maximum drawdown no greater than 15% and no more than two percentage points
  worse than baseline;
- positive excess return after either delaying fills by one additional market
  session or increasing slippage to 20 basis points per fill.

Hit rate is supporting evidence, never a sufficient promotion gate. Passing
creates a review report for Pedro; it performs no external action.

## Error handling and recovery

- Missing required bars: fail the cohort/session before account mutation.
- Unknown short borrow: reject new short; existing short uses a conservative,
  flagged fallback rate.
- Transaction failure: roll back the entire ledger mutation.
- Process crash: rerun uses stable IDs and resumes without duplicate effects.
- Pending external order: reconcile before retry.
- Corporate action without trustworthy terms: quarantine the affected position
  and invalidate the session rather than guess.
- Unclassified strategy silence: finish diagnostic capture but mark the session
  promotion-invalid.
- Metric hash change: close the epoch and require a new one.

## Testing

All tests are deterministic and API-free.

### P0

- exact close-signal/next-open lifecycle;
- weekend, holiday, and early-close session transitions;
- event-cutoff enforcement;
- raw/unadjusted bar validation;
- missing/stale mark failure;
- side-aware slippage and exact cost reconciliation;
- long/short P&L, margin, borrow, dividends, and splits;
- exit-before-entry behavior;
- long/short stop touch and gap-through fills;
- transactional rollback and rerun idempotency;
- pending external-order reconciliation;
- equity, cash, lot, and high-water invariants.

### P1

- policy applies to both LLM and fallback recommendations;
- current and pending positions are included;
- ticker/sector/strategy/event/short/margin/risk-contribution limits;
- short-interest and earnings context reaches the fill-time gate;
- full attribution against every contributing tag;
- publication-time no-lookahead and seven-day window;
- congressional member, amount, position, and consumed-event rules;
- covered-call capability reports inactive.

### P2

- exact 5/10/20/30 market-session windows;
- no short double inversion and no neutral denominator pollution;
- deterministic catalyst/signal/fill identities;
- order-independent deduplication and conflict handling;
- known daily Sharpe and drawdown sequences;
- net/gross cost reconciliation;
- invalid sessions and epoch boundaries never bridge;
- common-session benchmark pairing;
- $100k headline scope and scenario labels.

### P3

- production learning cannot be enabled;
- insufficient evidence returns `WAIT`;
- missing marks, unclassified silence, or risk breaches return `FAIL`;
- eligible evidence returns only `ELIGIBLE_FOR_MANUAL_REVIEW`;
- evaluation cannot mutate code, config, generation, or state;
- fill-delay and slippage sensitivity gates are deterministic.

### System verification

- focused tests per task;
- complete offline suite;
- before/after wall time, peak RSS, and API/LLM-call counts;
- JSON compatibility projections;
- migration dry run against copied legacy state;
- clean `gen_004`/`gen_005` smoke run with mocked market data;
- live deployment only after merge and fresh verification.

## Documentation

Update:

- `README.md`;
- `AUTORESEARCH_ARCHITECTURE_MAP.md`;
- `assets/autoresearch.svg`;
- `assets/daily-cycle.svg`;
- dashboard explanatory copy;
- daily report format;
- deployment timer documentation.

Documentation must plainly distinguish implemented execution from dormant
scaffolding and scenario panels from investable AUM.

## Deployment and rollback

1. Merge the foundation release after full verification.
2. Create `gen_004` with clean state; do not run it yet.
3. Merge the candidate policy release.
4. Create `gen_005` with clean state.
5. Archive `gen_003` as legacy observation-only.
6. Change the production timer to 18:00 ET.
7. Run migration and startup checks without rewriting legacy state.
8. Let `gen_004` and `gen_005` begin on the same session.
9. Generate a direct state-backed daily report and verify all 12 strategies.

Rollback disables the two new generations, restores the prior timer, and
leaves all new ledger state intact for diagnosis. It never reactivates
`gen_003` as promotion evidence.
