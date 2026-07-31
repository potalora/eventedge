"""Offline acceptance matrix for the authoritative P0 execution ledger."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tradingagents.execution import alpaca_broker as alpaca_module
from tradingagents.execution.alpaca_broker import AlpacaBroker
from tradingagents.strategies.execution import (
    CorporateAction,
    Fill,
    MarketBar,
    OrderIntent,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.execution.cost_model import PaperCostModel
from tradingagents.strategies.execution.price_source import AdjustedClose
from tradingagents.strategies.orchestration.session_executor import SessionExecutor
from tradingagents.strategies.orchestration.trading_calendar import (
    next_session,
    session_close,
    session_open,
)
from tradingagents.strategies.state.compatibility_projection import project_all
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
from tradingagents.strategies.state.state import StateManager
from tradingagents.strategies.trading.execution_bridge import ExecutionBridge
from tradingagents.strategies.trading.portfolio_committee import (
    PortfolioCommittee,
    TradeRecommendation,
)


UTC = timezone.utc
FRIDAY = date(2026, 7, 31)
MONDAY = date(2026, 8, 3)
COHORT = "acceptance"

CASES = (
    "close_signal_never_fills_same_session",
    "weekend_holiday_and_early_close_next_open",
    "late_event_cannot_create_intent",
    "adjusted_bar_fails_before_mutation",
    "long_and_short_pnl_reconcile",
    "long_and_short_stop_gap_reconcile",
    "costs_borrow_financing_apply_once_after_restart",
    "exits_restore_buying_power_before_entries",
    "missing_mark_invalidates_without_zero_pnl",
    "transaction_crash_rolls_back",
    "rerun_does_not_duplicate_economic_effects",
    "unresolved_external_order_reconciles_before_retry",
    "split_and_directional_dividend_apply_once",
    "compatibility_json_matches_ledger",
)


def _at_close(session: date, *, hours: int = 2) -> datetime:
    return session_close(session) + timedelta(hours=hours)


def _bar(
    ticker: str,
    session: date,
    *,
    open_: str = "100",
    close: str = "101",
    high: str | None = None,
    low: str | None = None,
    adjusted: bool = False,
) -> MarketBar:
    open_value = Decimal(open_)
    close_value = Decimal(close)
    return MarketBar(
        ticker,
        session,
        open_value,
        Decimal(high) if high is not None else max(open_value, close_value) + 1,
        Decimal(low) if low is not None else min(open_value, close_value) - 1,
        close_value,
        "acceptance-raw",
        _at_close(session),
        adjusted,
    )


def _signal(
    ticker: str,
    reference_session: date,
    *,
    direction: str = "long",
    epoch_id: str = "epoch-acceptance",
    policy_id: str = "policy-acceptance",
    suffix: str = "base",
    event_at: datetime | None = None,
) -> SignalRecord:
    cutoff = session_close(reference_session)
    observed = event_at or cutoff - timedelta(minutes=30)
    decision = observed
    event_key = f"{ticker}-{reference_session}-{suffix}"
    return SignalRecord(
        stable_id(
            "signal",
            epoch_id,
            policy_id,
            event_key,
            "acceptance_strategy",
            ticker,
            direction,
            reference_session,
        ),
        epoch_id,
        policy_id,
        event_key,
        "acceptance_strategy",
        ticker,
        direction,
        event_at or observed,
        observed,
        reference_session,
        Decimal("100"),
        decision,
        stable_id("evidence", event_key),
    )


def _intent(
    ledger: PortfolioLedger,
    signal: SignalRecord,
    side: str,
    quantity: int,
    eligible_session: date,
    *,
    price_rule: str = "next_session_open",
    stop_price: str | None = None,
    owned_lot: str | None = None,
) -> OrderIntent:
    ledger.record_signal(signal)
    order = OrderIntent(
        stable_id(
            "intent",
            ledger.cohort_id,
            signal.signal_id,
            side,
            quantity,
            eligible_session,
            price_rule,
            stop_price,
        ),
        (signal.signal_id,),
        ledger.cohort_id,
        side,
        quantity,
        signal.decision_at,
        eligible_session,
        price_rule,
        "pending",
        Decimal(stop_price) if stop_price is not None else None,
        None,
    )
    if owned_lot is None:
        ledger.stage_intent(order)
    else:
        ledger.stage_exit_intent(order, ((owned_lot, quantity),))
    return order


def _apply_fill(
    ledger: PortfolioLedger,
    order: OrderIntent,
    reference_price: str = "100",
    *,
    cost_model: PaperCostModel | None = None,
    borrow_rate: Decimal | None = None,
) -> Fill:
    model = cost_model or PaperCostModel()
    fill = model.fill(
        order,
        Decimal(reference_price),
        session_open(order.eligible_session),
        _at_close(order.eligible_session),
    )
    ledger.apply_fill(order, fill, borrow_rate=borrow_rate)
    return fill


class _PriceSource:
    def __init__(self, bars: dict[tuple[str, date], MarketBar]) -> None:
        self.bars = bars
        self.raw_requests: list[tuple[tuple[str, ...], date, date, bool]] = []

    def get_daily_bars(
        self,
        tickers: list[str],
        start_session: date,
        end_session_inclusive: date,
        adjusted: bool = False,
    ) -> dict[tuple[str, date], MarketBar]:
        self.raw_requests.append(
            (tuple(tickers), start_session, end_session_inclusive, adjusted)
        )
        return {
            key: value
            for key, value in self.bars.items()
            if key[0] in tickers and start_session <= key[1] <= end_session_inclusive
        }

    def get_corporate_actions(
        self, tickers: list[str], session: date
    ) -> list[object]:
        del tickers, session
        return []

    def get_total_return_closes(
        self,
        symbols: list[str],
        start_session: date,
        end_session_inclusive: date,
    ) -> dict[tuple[str, date], AdjustedClose]:
        assert start_session == end_session_inclusive
        return {
            (symbol, start_session): AdjustedClose(
                symbol,
                start_session,
                Decimal("100"),
                "acceptance-adjusted",
                _at_close(start_session),
            )
            for symbol in symbols
        }


def _config() -> dict[str, object]:
    return {
        "execution": {"mode": "paper"},
        "autoresearch": {
            "total_capital": 5000,
            "risk_gate": {
                "long_only": False,
                "min_position_value": 1,
                "max_position_pct": 1.0,
                "max_positions": 8,
                "per_strategy_max": 8,
            },
            "short_selling": {"borrow_cost_reject_above": "0.05"},
            "paper_ledger": {
                "slippage_bps": "10",
                "margin_financing_rate": "0",
                "benchmark_symbols": ["SPY", "BIL"],
            },
        },
    }


@pytest.mark.parametrize("case", CASES, ids=CASES)
def test_execution_ledger_acceptance(case: str, tmp_path, monkeypatch) -> None:
    def forbid_llm(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("execution acceptance must make zero LLM calls")

    monkeypatch.setattr(PortfolioCommittee, "synthesize", forbid_llm)
    ledger = PortfolioLedger(tmp_path / "portfolio.db", COHORT, Decimal("5000"))
    try:
        if case == "close_signal_never_fills_same_session":
            entry_signal = _signal("AAPL", date(2026, 7, 29), suffix="entry")
            entry = _intent(
                ledger, entry_signal, "buy", 2, date(2026, 7, 30)
            )
            entry_fill = _apply_fill(ledger, entry)
            lot_id = stable_id("lot", entry_fill.fill_id)
            close_signal = _signal("AAPL", FRIDAY, suffix="close")
            close_intent = _intent(
                ledger,
                close_signal,
                "sell",
                2,
                MONDAY,
                owned_lot=lot_id,
            )
            source = _PriceSource({("AAPL", FRIDAY): _bar("AAPL", FRIDAY)})
            result = SessionExecutor(ledger, _config()).execute_open_and_mark(
                FRIDAY,
                "epoch-acceptance",
                source,
                {},
                _at_close(FRIDAY),
            )

            assert result.valid and result.snapshot is not None
            assert ledger.read_fills(FRIDAY, FRIDAY) == []
            assert ledger.intent(close_intent.intent_id).status == "pending"
            assert ledger.connection.execute(
                "SELECT open_qty FROM lots WHERE lot_id = ?", (lot_id,)
            ).fetchone()[0] == 2
            assert source.raw_requests == [
                (("AAPL",), FRIDAY, FRIDAY, False)
            ]
            stored = ledger.read_signals(FRIDAY, FRIDAY)[0]
            assert (stored.epoch_id, stored.policy_id) == (
                "epoch-acceptance",
                "policy-acceptance",
            )
            assert stored.signal_id != _signal(
                "AAPL", FRIDAY, suffix="close", epoch_id="epoch-other"
            ).signal_id
            assert stored.signal_id != _signal(
                "AAPL", FRIDAY, suffix="close", policy_id="policy-other"
            ).signal_id
        elif case == "weekend_holiday_and_early_close_next_open":
            calendar_cases = (
                (date(2026, 7, 2), date(2026, 7, 6), "HOLIDAY"),
                (FRIDAY, MONDAY, "WEEKEND"),
                (date(2026, 11, 25), date(2026, 11, 27), "EARLY"),
            )
            assert [next_session(reference) for reference, _, _ in calendar_cases] == [
                eligible for _, eligible, _ in calendar_cases
            ]
            assert session_close(date(2026, 11, 27)) == datetime(
                2026, 11, 27, 18, tzinfo=UTC
            )
            bridge = ExecutionBridge(_config(), ledger=ledger)
            expected_effective = []
            for reference, eligible, ticker in calendar_cases:
                signal = _signal(ticker, reference, suffix="calendar")
                order = _intent(ledger, signal, "buy", 1, eligible)
                fill_result = bridge.execute_due_intent(
                    order,
                    _bar(ticker, eligible, open_="100", close="100"),
                    ledger.account_state(),
                    {
                        "processing_at": _at_close(eligible),
                        "opening_prices": {ticker: Decimal("100")},
                    },
                    PaperCostModel(),
                )
                assert fill_result.status == "filled"
                assert fill_result.fill is not None
                expected_effective.append(session_open(eligible))

            fills = ledger.read_fills()
            assert [fill.effective_at for fill in fills] == expected_effective
            assert len({fill.fill_id for fill in fills}) == 3
            assert ledger.connection.execute(
                "SELECT COUNT(*) FROM lots WHERE open_qty = 1"
            ).fetchone()[0] == 3
        elif case == "late_event_cannot_create_intent":
            late = session_close(FRIDAY) + timedelta(minutes=1)
            signal = _signal("AAPL", FRIDAY, suffix="late", event_at=late)
            ledger.record_signal(signal)
            with pytest.raises(ValueError, match="session cutoff"):
                ExecutionBridge(_config(), ledger=ledger).stage_intent(
                    TradeRecommendation(
                        "AAPL",
                        "long",
                        0.10,
                        0.8,
                        "late evidence must fail closed",
                        ["acceptance_strategy"],
                    ),
                    (signal,),
                    ledger.account_state(),
                    signal.decision_at,
                    MONDAY,
                )
            assert ledger.connection.execute(
                "SELECT COUNT(*) FROM signals"
            ).fetchone()[0] == 1
            assert ledger.connection.execute(
                "SELECT COUNT(*) FROM order_intents"
            ).fetchone()[0] == 0
            assert ledger.read_fills() == []
            assert ledger.account_state().cash == Decimal("5000")
        elif case == "adjusted_bar_fails_before_mutation":
            signal = _signal("AAPL", FRIDAY, suffix="adjusted")
            order = _intent(ledger, signal, "buy", 2, MONDAY)
            before = ledger.account_state()
            source = _PriceSource(
                {("AAPL", MONDAY): _bar("AAPL", MONDAY, adjusted=True)}
            )
            result = SessionExecutor(ledger, _config()).execute_open_and_mark(
                MONDAY,
                "epoch-acceptance",
                source,
                {},
                _at_close(MONDAY),
            )
            assert not result.valid and result.snapshot is None
            assert "adjusted" in result.invalid_reason
            assert ledger.account_state() == before
            assert ledger.read_fills() == []
            assert ledger.connection.execute(
                "SELECT COUNT(*) FROM session_phases"
            ).fetchone()[0] == 0
            assert ledger.intent(order.intent_id).status == "cancelled"
            assert source.raw_requests == [(("AAPL",), MONDAY, MONDAY, False)]
        elif case == "long_and_short_pnl_reconcile":
            long_signal = _signal("AAPL", date(2026, 7, 29), suffix="long-open")
            long_entry = _intent(
                ledger, long_signal, "buy", 2, date(2026, 7, 30)
            )
            long_fill = _apply_fill(ledger, long_entry, "100")
            long_exit = _intent(
                ledger,
                _signal("AAPL", FRIDAY, suffix="long-close"),
                "sell",
                2,
                MONDAY,
                owned_lot=stable_id("lot", long_fill.fill_id),
            )
            _apply_fill(ledger, long_exit, "110")

            short_signal = _signal(
                "TSLA",
                date(2026, 7, 29),
                direction="short",
                suffix="short-open",
            )
            short_entry = _intent(
                ledger, short_signal, "short", 2, date(2026, 7, 30)
            )
            short_fill = _apply_fill(
                ledger, short_entry, "200", borrow_rate=Decimal("0.01")
            )
            short_exit = _intent(
                ledger,
                _signal(
                    "TSLA", FRIDAY, direction="short", suffix="short-close"
                ),
                "cover",
                2,
                MONDAY,
                owned_lot=stable_id("lot", short_fill.fill_id),
            )
            _apply_fill(ledger, short_exit, "180")

            closures = ledger.connection.execute(
                "SELECT realized_pnl FROM lot_closures ORDER BY realized_pnl"
            ).fetchall()
            assert [Decimal(row[0]) for row in closures] == [
                Decimal("19.580"),
                Decimal("39.240"),
            ]
            state = ledger.account_state()
            assert state.cash == Decimal("5058.820")
            assert state.margin_used == Decimal("0.000")
            accounting = ledger.connection.execute(
                "SELECT realized_pnl, slippage_cost FROM accounting_state"
            ).fetchone()
            assert tuple(map(Decimal, accounting)) == (
                Decimal("58.820"),
                Decimal("1.1800"),
            )
            assert ledger.connection.execute(
                "SELECT COUNT(*) FROM lots WHERE open_qty != 0"
            ).fetchone()[0] == 0
        elif case == "long_and_short_stop_gap_reconcile":
            long_entry = _intent(
                ledger,
                _signal("AAPL", date(2026, 7, 29), suffix="stop-long-entry"),
                "buy",
                1,
                date(2026, 7, 30),
            )
            long_fill = _apply_fill(ledger, long_entry)
            short_entry = _intent(
                ledger,
                _signal(
                    "TSLA",
                    date(2026, 7, 29),
                    direction="short",
                    suffix="stop-short-entry",
                ),
                "short",
                1,
                date(2026, 7, 30),
            )
            short_fill = _apply_fill(
                ledger, short_entry, borrow_rate=Decimal("0.01")
            )
            long_stop = _intent(
                ledger,
                _signal("AAPL", FRIDAY, suffix="stop-long-exit"),
                "sell",
                1,
                MONDAY,
                price_rule="resting_stop",
                stop_price="95",
                owned_lot=stable_id("lot", long_fill.fill_id),
            )
            short_stop = _intent(
                ledger,
                _signal(
                    "TSLA", FRIDAY, direction="short", suffix="stop-short-exit"
                ),
                "cover",
                1,
                MONDAY,
                price_rule="resting_stop",
                stop_price="105",
                owned_lot=stable_id("lot", short_fill.fill_id),
            )
            bridge = ExecutionBridge(_config(), ledger=ledger)
            long_result = bridge.execute_due_intent(
                long_stop,
                _bar(
                    "AAPL",
                    MONDAY,
                    open_="90",
                    high="96",
                    low="89",
                    close="92",
                ),
                ledger.account_state(),
                {"processing_at": _at_close(MONDAY)},
                PaperCostModel(),
            )
            short_result = bridge.execute_due_intent(
                short_stop,
                _bar(
                    "TSLA",
                    MONDAY,
                    open_="110",
                    high="111",
                    low="104",
                    close="108",
                ),
                ledger.account_state(),
                {"processing_at": _at_close(MONDAY)},
                PaperCostModel(),
            )
            assert long_result.fill is not None and short_result.fill is not None
            assert (
                long_result.fill.reference_price,
                short_result.fill.reference_price,
            ) == (Decimal("90"), Decimal("110"))
            assert sorted(
                Decimal(row[0])
                for row in ledger.connection.execute(
                    "SELECT realized_pnl FROM lot_closures"
                )
            ) == [Decimal("-10.210"), Decimal("-10.190")]
            assert ledger.connection.execute(
                "SELECT COUNT(*) FROM lots WHERE open_qty = 0"
            ).fetchone()[0] == 2
            assert len({long_result.fill.fill_id, short_result.fill.fill_id}) == 2
        elif case == "costs_borrow_financing_apply_once_after_restart":
            charged_costs = PaperCostModel(
                {
                    "slippage_bps": "10",
                    "commission_per_fill": "1",
                    "other_fee_per_fill": "0.25",
                }
            )
            long_order = _intent(
                ledger,
                _signal("AAPL", date(2026, 7, 29), suffix="cost-long"),
                "buy",
                60,
                date(2026, 7, 30),
            )
            _apply_fill(ledger, long_order, cost_model=charged_costs)
            short_order = _intent(
                ledger,
                _signal(
                    "TSLA",
                    date(2026, 7, 29),
                    direction="short",
                    suffix="cost-short",
                ),
                "short",
                5,
                date(2026, 7, 30),
            )
            _apply_fill(
                ledger,
                short_order,
                cost_model=charged_costs,
                borrow_rate=Decimal("0.01"),
            )
            mark = _bar("TSLA", MONDAY, open_="100", close="100")
            borrow = ledger.accrue_borrow(
                MONDAY,
                {"TSLA": mark},
                {"TSLA": Decimal("0.365")},
                _at_close(MONDAY),
            )
            financing = ledger.accrue_financing(MONDAY, Decimal("0.365"))
            assert (borrow.amount, financing.amount) == (
                Decimal("0.5000"),
                Decimal("0.5095"),
            )
            before_restart = ledger.account_state()
            ledger_path = ledger.path
            ledger.close()
            ledger = PortfolioLedger(ledger_path, COHORT, Decimal("5000"))

            assert ledger.accrue_borrow(
                MONDAY,
                {"TSLA": mark},
                {"TSLA": Decimal("0.365")},
                _at_close(MONDAY),
            ) == borrow
            assert ledger.accrue_financing(MONDAY, Decimal("0.365")) == financing
            assert ledger.account_state() == before_restart
            assert ledger.connection.execute(
                "SELECT COUNT(*) FROM borrow_accruals"
            ).fetchone()[0] == 1
            assert ledger.connection.execute(
                "SELECT COUNT(*) FROM financing_accruals"
            ).fetchone()[0] == 1
            summary = ledger.connection.execute(
                """SELECT slippage_cost, commission_cost, other_fees,
                          borrow_cost, financing_cost FROM accounting_state"""
            ).fetchone()
            assert tuple(map(Decimal, summary)) == (
                Decimal("6.5000"),
                Decimal("2.0000"),
                Decimal("0.5000"),
                Decimal("0.5000"),
                Decimal("0.5095"),
            )
            assert ledger.connection.execute(
                "SELECT COUNT(*) FROM cash_events WHERE event_type != 'opening'"
            ).fetchone()[0] == 4
        elif case == "exits_restore_buying_power_before_entries":
            old_entry = _intent(
                ledger,
                _signal("OLD", date(2026, 7, 29), suffix="old-entry"),
                "buy",
                49,
                date(2026, 7, 30),
            )
            old_fill = _apply_fill(ledger, old_entry)
            assert ledger.account_state().buying_power == Decimal("95.100")
            old_exit = _intent(
                ledger,
                _signal("OLD", FRIDAY, suffix="old-exit"),
                "sell",
                49,
                MONDAY,
                owned_lot=stable_id("lot", old_fill.fill_id),
            )
            new_entry = _intent(
                ledger,
                _signal("NEW", FRIDAY, suffix="new-entry"),
                "buy",
                40,
                MONDAY,
            )
            source = _PriceSource(
                {
                    ("OLD", MONDAY): _bar(
                        "OLD", MONDAY, open_="100", close="100"
                    ),
                    ("NEW", MONDAY): _bar(
                        "NEW", MONDAY, open_="100", close="101"
                    ),
                }
            )
            result = SessionExecutor(ledger, _config()).execute_open_and_mark(
                MONDAY,
                "epoch-acceptance",
                source,
                {},
                _at_close(MONDAY),
            )
            assert result.valid and result.snapshot is not None
            execution_order = ledger.connection.execute(
                "SELECT side FROM fills WHERE session = ? ORDER BY rowid",
                (MONDAY.isoformat(),),
            ).fetchall()
            assert [row[0] for row in execution_order] == ["sell", "buy"]
            assert ledger.intent(old_exit.intent_id).status == "filled"
            assert ledger.intent(new_entry.intent_id).status == "filled"
            lots = ledger.connection.execute(
                "SELECT ticker, open_qty FROM lots ORDER BY ticker"
            ).fetchall()
            assert [tuple(row) for row in lots] == [("NEW", 40), ("OLD", 0)]
            assert result.snapshot.buying_power > Decimal("0")
            assert source.raw_requests == [
                (("NEW", "OLD"), MONDAY, MONDAY, False)
            ]
        elif case == "missing_mark_invalidates_without_zero_pnl":
            winner_entry = _intent(
                ledger,
                _signal("WIN", date(2026, 7, 28), suffix="winner-entry"),
                "buy",
                1,
                date(2026, 7, 29),
            )
            winner_fill = _apply_fill(ledger, winner_entry)
            winner_exit = _intent(
                ledger,
                _signal("WIN", date(2026, 7, 30), suffix="winner-exit"),
                "sell",
                1,
                FRIDAY,
                owned_lot=stable_id("lot", winner_fill.fill_id),
            )
            _apply_fill(ledger, winner_exit, "110")
            held_entry = _intent(
                ledger,
                _signal("HELD", date(2026, 7, 29), suffix="held-entry"),
                "buy",
                2,
                date(2026, 7, 30),
            )
            _apply_fill(ledger, held_entry)
            due = _intent(
                ledger,
                _signal("NEW", FRIDAY, suffix="missing-mark-entry"),
                "buy",
                1,
                MONDAY,
            )
            before = ledger.account_state()
            realized_before = Decimal(
                ledger.connection.execute(
                    "SELECT realized_pnl FROM accounting_state"
                ).fetchone()[0]
            )
            assert realized_before == Decimal("9.790")
            source = _PriceSource({("NEW", MONDAY): _bar("NEW", MONDAY)})
            result = SessionExecutor(ledger, _config()).execute_open_and_mark(
                MONDAY,
                "epoch-acceptance",
                source,
                {},
                _at_close(MONDAY),
            )
            assert not result.valid and result.snapshot is None
            assert "missing HELD/2026-08-03" in result.invalid_reason
            assert ledger.account_state() == before
            assert Decimal(
                ledger.connection.execute(
                    "SELECT realized_pnl FROM accounting_state"
                ).fetchone()[0]
            ) == realized_before
            assert ledger.read_snapshots(MONDAY, MONDAY) == []
            assert ledger.read_fills(MONDAY, MONDAY) == []
            assert ledger.intent(due.intent_id).status == "cancelled"
            assert source.raw_requests == [
                (("HELD", "NEW"), MONDAY, MONDAY, False)
            ]
        elif case == "transaction_crash_rolls_back":
            due = _intent(
                ledger,
                _signal("AAPL", FRIDAY, suffix="crash"),
                "buy",
                2,
                MONDAY,
            )
            source = _PriceSource({("AAPL", MONDAY): _bar("AAPL", MONDAY)})

            def crash_after_mutation(phase: str) -> None:
                if phase == "execute_entries":
                    raise RuntimeError("acceptance injected crash")

            with pytest.raises(RuntimeError, match="acceptance injected crash"):
                SessionExecutor(
                    ledger,
                    _config(),
                    after_phase_mutation=crash_after_mutation,
                ).execute_open_and_mark(
                    MONDAY,
                    "epoch-acceptance",
                    source,
                    {},
                    _at_close(MONDAY),
                )
            assert ledger.read_fills(MONDAY, MONDAY) == []
            assert ledger.connection.execute(
                "SELECT COUNT(*) FROM lots"
            ).fetchone()[0] == 0
            assert ledger.account_state().cash == Decimal("5000")
            assert ledger.intent(due.intent_id).status == "pending"
            assert not ledger.phase_completed(MONDAY, "execute_entries")
        elif case == "rerun_does_not_duplicate_economic_effects":
            due = _intent(
                ledger,
                _signal("AAPL", FRIDAY, suffix="rerun"),
                "buy",
                2,
                MONDAY,
            )
            source = _PriceSource({("AAPL", MONDAY): _bar("AAPL", MONDAY)})
            first = SessionExecutor(ledger, _config()).execute_open_and_mark(
                MONDAY,
                "epoch-acceptance",
                source,
                {},
                _at_close(MONDAY),
            )
            assert first.valid and first.snapshot is not None
            before = {
                table: ledger.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "fills",
                    "lots",
                    "cash_events",
                    "marks",
                    "account_snapshots",
                    "benchmark_observations",
                    "session_phases",
                )
            }
            before_state = ledger.account_state()

            class NoMarketIO:
                def __getattr__(self, name: str) -> object:
                    raise AssertionError(f"rerun attempted market I/O: {name}")

            replay = SessionExecutor(ledger, _config()).execute_open_and_mark(
                MONDAY,
                "epoch-acceptance",
                NoMarketIO(),
                {},
                _at_close(MONDAY),
            )
            after = {
                table: ledger.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in before
            }
            assert replay.valid and replay.snapshot == first.snapshot
            assert after == before == {
                "fills": 1,
                "lots": 1,
                "cash_events": 3,
                "marks": 1,
                "account_snapshots": 1,
                "benchmark_observations": 2,
                "session_phases": 9,
            }
            assert ledger.account_state() == before_state
            assert ledger.intent(due.intent_id).status == "filled"
            assert source.raw_requests == [(("AAPL",), MONDAY, MONDAY, False)]
        elif case == "unresolved_external_order_reconciles_before_retry":
            signal = _signal("AAPL", FRIDAY, suffix="external")
            order = _intent(ledger, signal, "buy", 2, MONDAY)

            class FakeClient:
                def __init__(self) -> None:
                    self.submit_count = 0
                    self.reconcile_count = 0

                def submit_order(self, _request: object) -> object:
                    self.submit_count += 1
                    return SimpleNamespace(
                        id="external-acceptance",
                        status="accepted",
                        filled_qty="0",
                        filled_avg_price=None,
                    )

                def get_order_by_client_id(self, client_order_id: str) -> object:
                    assert client_order_id == order.intent_id
                    self.reconcile_count += 1
                    return SimpleNamespace(
                        id="external-acceptance",
                        status="partially_filled",
                        filled_qty="1",
                        filled_avg_price="100.05",
                    )

            client = FakeClient()
            monkeypatch.setattr(
                alpaca_module, "TradingClient", lambda *_args, **_kwargs: client
            )
            monkeypatch.setattr(
                alpaca_module,
                "MarketOrderRequest",
                lambda **kwargs: SimpleNamespace(**kwargs),
            )
            monkeypatch.setattr(
                alpaca_module, "OrderSide", SimpleNamespace(BUY="buy", SELL="sell")
            )
            monkeypatch.setattr(
                alpaca_module, "TimeInForce", SimpleNamespace(DAY="day")
            )
            broker = AlpacaBroker("fixture-key", "fixture-secret", ledger=ledger)
            first = broker.submit_stock_order(
                "AAPL", "buy", 2, client_order_id=order.intent_id
            )
            second = broker.submit_stock_order(
                "AAPL", "buy", 2, client_order_id=order.intent_id
            )

            assert (first.status, second.status) == (
                "accepted",
                "partially_filled",
            )
            assert (client.submit_count, client.reconcile_count) == (1, 1)
            external = ledger.external_order_for_intent(order.intent_id)
            assert external is not None
            assert (
                external["external_order_id"],
                external["status"],
            ) == ("external-acceptance", "partially_filled")
            assert ledger.connection.execute(
                "SELECT COUNT(*) FROM external_orders"
            ).fetchone()[0] == 1
            assert ledger.intent(order.intent_id).status == "pending"
            assert ledger.read_fills() == []
            assert ledger.account_state().cash == Decimal("5000")
        elif case == "split_and_directional_dividend_apply_once":
            long_order = _intent(
                ledger,
                _signal("AAPL", date(2026, 7, 29), suffix="action-long"),
                "buy",
                2,
                date(2026, 7, 30),
            )
            _apply_fill(ledger, long_order)
            short_order = _intent(
                ledger,
                _signal(
                    "AAPL",
                    date(2026, 7, 29),
                    direction="short",
                    suffix="action-short",
                ),
                "short",
                1,
                date(2026, 7, 30),
            )
            _apply_fill(ledger, short_order, borrow_rate=Decimal("0.01"))
            split = CorporateAction(
                "a-split-acceptance",
                "AAPL",
                MONDAY,
                "split",
                Decimal("2"),
                None,
                "acceptance-action",
                _at_close(MONDAY),
                True,
            )
            dividend = CorporateAction(
                "b-dividend-acceptance",
                "AAPL",
                MONDAY,
                "cash_dividend",
                None,
                Decimal("1.25"),
                "acceptance-action",
                _at_close(MONDAY),
                True,
            )
            actions = [split, dividend]
            events = ledger.apply_corporate_actions(
                MONDAY, actions, _at_close(MONDAY)
            )
            assert len(events) == 3
            state_after_first = ledger.account_state()
            ledger_path = ledger.path
            ledger.close()
            ledger = PortfolioLedger(ledger_path, COHORT, Decimal("5000"))
            assert ledger.apply_corporate_actions(
                MONDAY, actions, _at_close(MONDAY)
            ) == []
            assert ledger.account_state() == state_after_first
            lots = ledger.connection.execute(
                """SELECT direction, original_qty, open_qty, entry_price
                   FROM lots ORDER BY direction"""
            ).fetchall()
            assert [tuple(row) for row in lots] == [
                ("long", 4, 4, "50.050"),
                ("short", 2, 2, "49.950"),
            ]
            dividends = ledger.connection.execute(
                "SELECT direction, amount FROM dividend_events ORDER BY direction"
            ).fetchall()
            assert [
                (row[0], Decimal(row[1])) for row in dividends
            ] == [("long", Decimal("5.0000")), ("short", Decimal("-2.5000"))]
            assert ledger.connection.execute(
                "SELECT COUNT(*) FROM corporate_actions"
            ).fetchone()[0] == 2
            assert ledger.connection.execute(
                "SELECT COUNT(*) FROM lot_action_applications"
            ).fetchone()[0] == 2
            assert Decimal(
                ledger.connection.execute(
                    "SELECT dividend_cash FROM accounting_state"
                ).fetchone()[0]
            ) == Decimal("2.5000")
        elif case == "compatibility_json_matches_ledger":
            long_order = _intent(
                ledger,
                _signal("AAPL", date(2026, 7, 29), suffix="projection-long"),
                "buy",
                2,
                date(2026, 7, 30),
            )
            long_fill = _apply_fill(ledger, long_order)
            long_exit = _intent(
                ledger,
                _signal("AAPL", FRIDAY, suffix="projection-close"),
                "sell",
                2,
                MONDAY,
                owned_lot=stable_id("lot", long_fill.fill_id),
            )
            _apply_fill(ledger, long_exit, "110")
            short_order = _intent(
                ledger,
                _signal(
                    "TSLA", FRIDAY, direction="short", suffix="projection-short"
                ),
                "short",
                1,
                MONDAY,
            )
            _apply_fill(
                ledger, short_order, "200", borrow_rate=Decimal("0.01")
            )
            snapshot = ledger.mark(
                MONDAY,
                {"TSLA": _bar("TSLA", MONDAY, open_="195", close="195")},
                "epoch-acceptance",
                _at_close(MONDAY),
            )
            assert snapshot.valid
            project_all(ledger, tmp_path)
            trade_path = tmp_path / "paper_trades.json"
            snapshot_path = tmp_path / "equity_snapshots.jsonl"
            first_trade_bytes = trade_path.read_bytes()
            first_snapshot_bytes = snapshot_path.read_bytes()
            projected_trades = json.loads(first_trade_bytes)
            projected_snapshots = [
                json.loads(line)
                for line in first_snapshot_bytes.decode().splitlines()
            ]
            authoritative_trades = ledger.read_trade_projections()
            authoritative_snapshots = ledger.read_snapshots()
            assert [row["trade_id"] for row in projected_trades] == [
                row.trade_id for row in authoritative_trades
            ]
            assert [row["realized_pnl"] for row in projected_trades] == [
                float(row.realized_pnl) for row in authoritative_trades
            ]
            assert projected_snapshots[-1]["snapshot_id"] == (
                authoritative_snapshots[-1].snapshot_id
            )
            assert projected_snapshots[-1]["portfolio_value"] == float(
                authoritative_snapshots[-1].net_equity
            )
            assert StateManager(str(tmp_path)).load_paper_trades() == projected_trades
            project_all(ledger, tmp_path)
            assert trade_path.read_bytes() == first_trade_bytes
            assert snapshot_path.read_bytes() == first_snapshot_bytes
        else:
            raise AssertionError(f"unknown acceptance case: {case}")
    finally:
        ledger.close()
