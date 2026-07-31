# Executable Paper Ledger — P0 Design

**Program:** `2026-07-31-portfolio-integrity-program-design.md`
**Release:** Foundation / `gen_004`
**Status:** Approved for implementation planning

## Goal

Replace retroactive prior-close fills and reconstructed account balances with a
transactional paper ledger whose orders exist before their execution prices.

## Boundary

P0 changes execution semantics and therefore starts a fresh generation. It
does not rewrite `gen_003`, alter strategy selection, or enable live trading.

## Daily state machine

At 18:00 ET on market session `D`:

1. Load intents created after `D-1` close.
2. Fetch and validate exact raw OHLC for `D`.
3. Apply corporate actions.
4. Execute due exits and then entries at `D` Open.
5. Apply slippage, commission, fees, borrow, and financing.
6. Mark all positions at `D` Close.
7. Commit the account snapshot and benchmark observation.
8. Screen cutoff-safe information through `D` close.
9. Persist signals and queue next-session intents.

Every transition is idempotent by stable signal, intent, fill, accrual, and
snapshot IDs.

## Authoritative state

Each cohort has one SQLite ledger. Required tables cover:

- schema metadata and metric epochs;
- signals and source-event provenance;
- order intents and status transitions;
- fills and cost breakdowns;
- position lots and corporate actions;
- cash, borrow, financing, dividends, and fees;
- closing marks and account snapshots.

One database transaction applies a fill, lot mutation, cash mutation, and
order-state change. JSON trade/snapshot files become compatibility projections.

## Price requirements

- XNYS session calendar.
- Inclusive price-source interface over yfinance's exclusive `end`.
- `auto_adjust=False`.
- Decimal raw Open/High/Low/Close for execution and marks.
- Exact terminal-session assertion.
- Missing, stale, invalid, adjusted, or future bars fail closed.
- Splits adjust lots; cash dividends become direction-aware ledger entries.

## Execution and costs

- Next-session-open entries and scheduled exits.
- Resting stop execution from daily OHLC, including gap-through behavior.
- Exits settle before entries.
- Buy/cover slippage increases price; sell/short slippage decreases price.
- Initial slippage is 10 basis points per fill.
- Commission and other fees persist explicitly even when zero.
- Existing conservative borrow tiers accrue ACT/365.
- Unknown borrow rejects new shorts.
- Margin financing accrues when applicable; idle-cash yield starts at zero.

## Account invariants

- `equity = cash + long market value - short liability`.
- Net equity reconciles gross equity less every cost category.
- A session cannot publish a valid snapshot with an unmarked open lot.
- High-water mark, margin, and borrow survive restart.
- A rerun cannot duplicate economic effects.
- Pending external orders reconcile before retry.

## Interfaces

The canonical program design defines `MarketBar`, `SignalRecord`,
`OrderIntent`, and `Fill`.

Service boundaries:

```python
PriceSource.get_daily_bars(
    tickers, start_session, end_session_inclusive, adjusted=False
) -> dict[tuple[str, date], MarketBar]

ExecutionBridge.stage_intent(
    recommendation, signal_records, marked_account
) -> OrderIntent

ExecutionBridge.execute_due_intent(
    intent, opening_bar, marked_account, risk_context, cost_model
) -> FillResult

PortfolioLedger.apply_fill(intent, fill) -> AccountState
PortfolioLedger.accrue_borrow(session, close_marks, rates) -> LedgerEvent
PortfolioLedger.mark(session, close_marks) -> AccountSnapshot
```

## Failure behavior

- Required bar missing: no account mutation.
- Corporate action uncertain: quarantine position and invalidate session.
- Ledger write failure: full rollback.
- Process crash: resume from stable IDs.
- External order unresolved: reconcile; never infer a fill.
- Existing short with missing borrow rate: conservative flagged fallback.

## Acceptance tests

- Close signal never fills in the same session.
- Weekend/holiday next-session open is exact.
- Cutoff-late events cannot create an intent.
- Raw and adjusted prices cannot mix.
- Long/short P&L and stop gaps reconcile exactly.
- Costs and borrow apply once and survive restart.
- Exits affect same-session buying power before entries.
- Missing marks fail rather than produce zero P&L.
- Crash rollback and rerun idempotency hold.
- Compatibility JSON matches ledger totals.
