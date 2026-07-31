from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import sqlite3

import pytest

from tradingagents.strategies.execution import (
    Fill,
    MarketBar,
    OrderIntent,
    SignalRecord,
)
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


UTC = timezone.utc
SESSION = date(2026, 8, 3)
COHORT = "horizon_30d_size_5k"


def signal(signal_id: str = "signal-1", ticker: str = "AAPL") -> SignalRecord:
    now = datetime(2026, 7, 31, 22, tzinfo=UTC)
    return SignalRecord(
        signal_id,
        "epoch-1",
        "policy-1",
        signal_id,
        "test",
        ticker,
        "long",
        now,
        now,
        date(2026, 7, 31),
        Decimal("100"),
        now,
        f"evidence-{signal_id}",
    )


def intent(
    intent_id: str,
    side: str,
    quantity: int = 10,
    signal_ids: tuple[str, ...] = ("signal-1",),
) -> OrderIntent:
    return OrderIntent(
        intent_id,
        signal_ids,
        COHORT,
        side,
        quantity,
        datetime(2026, 7, 31, 22, tzinfo=UTC),
        SESSION,
        "next_session_open",
        "pending",
        None,
        None,
    )


def fill(
    fill_id: str,
    intent_id: str,
    side: str,
    price: str,
    quantity: int = 10,
    slippage: str = "0",
    commission: str = "0",
    other_fees: str = "0",
) -> Fill:
    when = datetime(2026, 8, 3, 22, tzinfo=UTC)
    return Fill(
        fill_id,
        intent_id,
        side,
        SESSION,
        when,
        when,
        Decimal("100"),
        Decimal(price),
        quantity,
        Decimal(slippage),
        Decimal(commission),
        Decimal(other_fees),
    )


def bar(ticker: str, close: str) -> MarketBar:
    when = datetime(2026, 8, 3, 22, tzinfo=UTC)
    value = Decimal(close)
    return MarketBar(
        ticker, SESSION, value, value, value, value, "fixture", when, False
    )


def stage(ledger: PortfolioLedger, order: OrderIntent, *signals: SignalRecord) -> None:
    for item in signals:
        ledger.record_signal(item)
    ledger.stage_intent(order)


def test_long_buy_sell_closes_lot_with_explicit_costs_and_realized_pnl(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.db", COHORT, Decimal("5000"))
    try:
        buy = intent("buy", "buy")
        stage(ledger, buy, signal())
        assert ledger.apply_fill(
            buy, fill("buy-fill", "buy", "buy", "100.10", slippage="1.00")
        ).cash == Decimal("3999.00")
        assert [
            tuple(row)
            for row in ledger.connection.execute(
                "SELECT cost_type, amount FROM fill_costs WHERE fill_id = ? ORDER BY cost_type",
                ("buy-fill",),
            )
        ] == [("commission", "0"), ("other_fees", "0"), ("slippage", "1.00")]

        sell = intent("sell", "sell")
        stage(ledger, sell, signal("signal-sell"))
        state = ledger.apply_fill(sell, fill("sell-fill", "sell", "sell", "109.89"))
        assert state.cash == Decimal("5097.90")
        assert ledger.connection.execute("SELECT open_qty FROM lots").fetchone()[0] == 0
        assert tuple(
            ledger.connection.execute(
                "SELECT quantity, realized_pnl FROM lot_closures"
            ).fetchone()
        ) == (10, "97.90")
    finally:
        ledger.close()


def test_short_cover_releases_margin_and_realizes_direction_correct_pnl(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.db", COHORT, Decimal("5000"))
    try:
        opening = intent("short", "short")
        stage(ledger, opening, signal())
        state = ledger.apply_fill(
            opening,
            fill("short-fill", "short", "short", "99.90"),
            borrow_rate=Decimal("0.01"),
        )
        assert state.cash == Decimal("5999.00")
        assert state.margin_used == Decimal("1498.500")

        closing = intent("cover", "cover")
        stage(ledger, closing, signal("signal-cover"))
        state = ledger.apply_fill(
            closing, fill("cover-fill", "cover", "cover", "90.09")
        )
        assert state.cash == Decimal("5098.10")
        assert state.margin_used == Decimal("0.000")
        assert tuple(
            ledger.connection.execute(
                "SELECT quantity, realized_pnl FROM lot_closures"
            ).fetchone()
        ) == (10, "98.10")
    finally:
        ledger.close()


def test_fifo_closures_and_rollback_leave_no_partial_economic_mutation(
    monkeypatch, tmp_path
):
    ledger = PortfolioLedger(tmp_path / "ledger.db", COHORT, Decimal("5000"))
    try:
        first = intent("first", "buy", 4)
        second = intent("second", "buy", 6, ("signal-2",))
        stage(ledger, first, signal())
        stage(ledger, second, signal("signal-2"))
        ledger.apply_fill(first, fill("first-fill", "first", "buy", "100", 4))
        ledger.apply_fill(second, fill("second-fill", "second", "buy", "110", 6))
        exit_order = intent("exit", "sell", 10, ("signal-3",))
        stage(ledger, exit_order, signal("signal-3"))
        ledger.apply_fill(exit_order, fill("exit-fill", "exit", "sell", "120", 10))
        assert [
            tuple(row)
            for row in ledger.connection.execute(
                "SELECT quantity, realized_pnl FROM lot_closures ORDER BY closure_id"
            )
        ] == [(4, "80"), (6, "60")]

        failing = intent("failing", "buy", 1, ("signal-4",))
        stage(ledger, failing, signal("signal-4"))
        before_state = ledger.account_state()
        before_cash_events = ledger.connection.execute(
            "SELECT COUNT(*) FROM cash_events"
        ).fetchone()[0]
        before_summary = tuple(
            ledger.connection.execute("SELECT * FROM accounting_state").fetchone()
        )
        original = ledger._insert_cash_event

        def fail_after_cash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected cash failure")

        monkeypatch.setattr(ledger, "_insert_cash_event", fail_after_cash)
        with pytest.raises(RuntimeError, match="injected cash failure"):
            ledger.apply_fill(failing, fill("failing-fill", "failing", "buy", "50", 1))
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM fills WHERE fill_id = 'failing-fill'"
            ).fetchone()[0]
            == 0
        )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM lots WHERE fill_id = 'failing-fill'"
            ).fetchone()[0]
            == 0
        )
        assert (
            ledger.connection.execute(
                "SELECT status FROM order_intents WHERE intent_id = 'failing'"
            ).fetchone()[0]
            == "pending"
        )
        assert (
            ledger.connection.execute("SELECT COUNT(*) FROM cash_events").fetchone()[0]
            == before_cash_events
        )
        assert ledger.account_state() == before_state
        assert (
            tuple(
                ledger.connection.execute("SELECT * FROM accounting_state").fetchone()
            )
            == before_summary
        )
    finally:
        ledger.close()


@pytest.mark.parametrize("side, open_side", [("sell", "short"), ("cover", "buy")])
def test_close_side_without_matching_lot_fails_closed(tmp_path, side, open_side):
    ledger = PortfolioLedger(tmp_path / "ledger.db", COHORT, Decimal("5000"))
    try:
        opening = intent("open", open_side)
        stage(ledger, opening, signal())
        fill_kwargs = {"borrow_rate": Decimal("0.01")} if open_side == "short" else {}
        ledger.apply_fill(
            opening, fill("open-fill", "open", open_side, "100"), **fill_kwargs
        )
        closing = intent("close", side)
        stage(ledger, closing, signal("close-signal"))
        with pytest.raises(ValueError, match="matching open lots"):
            ledger.apply_fill(closing, fill("close-fill", "close", side, "100"))
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM fills WHERE fill_id = 'close-fill'"
            ).fetchone()[0]
            == 0
        )
    finally:
        ledger.close()


def test_mark_snapshot_reconciles_costs_exposure_and_high_water_mark(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.db", COHORT, Decimal("5000"))
    try:
        order = intent("buy", "buy")
        stage(ledger, order, signal())
        ledger.apply_fill(
            order, fill("buy-fill", "buy", "buy", "100.10", slippage="1.00")
        )
        snapshot = ledger.mark(
            SESSION,
            {"AAPL": bar("AAPL", "105")},
            "epoch-1",
            datetime(2026, 8, 3, 22, tzinfo=UTC),
        )
        assert snapshot.long_market_value == Decimal("1050")
        assert snapshot.net_equity == Decimal("5049.00")
        assert snapshot.gross_equity == Decimal("5050.00")
        assert snapshot.gross_equity - snapshot.slippage_cost == snapshot.net_equity
        assert snapshot.high_water_mark == Decimal("5049.00")
        assert (
            ledger.connection.execute(
                "SELECT high_water_mark FROM accounting_state"
            ).fetchone()[0]
            == "5049.00"
        )
    finally:
        ledger.close()


def test_ambiguous_signal_ticker_provenance_fails_before_fill_mutation(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.db", COHORT, Decimal("5000"))
    try:
        order = intent("ambiguous", "buy", signal_ids=("signal-1", "signal-2"))
        stage(ledger, order, signal(), signal("signal-2", "MSFT"))
        with pytest.raises(ValueError, match="unambiguous ticker"):
            ledger.apply_fill(order, fill("ambiguous-fill", "ambiguous", "buy", "100"))
        assert (
            ledger.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
        )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM cash_events WHERE event_type = 'fill'"
            ).fetchone()[0]
            == 0
        )
        assert (
            ledger.connection.execute(
                "SELECT status FROM order_intents WHERE intent_id = 'ambiguous'"
            ).fetchone()[0]
            == "pending"
        )
    finally:
        ledger.close()


@pytest.mark.parametrize(
    ("close_marks", "valuation_at", "message"),
    [
        ({}, datetime(2026, 8, 3, 22, tzinfo=UTC), "missing"),
        (
            {"AAPL": bar("MSFT", "105")},
            datetime(2026, 8, 3, 22, tzinfo=UTC),
            "untrusted",
        ),
        (
            {
                "AAPL": MarketBar(
                    "AAPL",
                    date(2026, 8, 4),
                    *(Decimal("105"),) * 4,
                    "fixture",
                    datetime(2026, 8, 3, 22, tzinfo=UTC),
                    False,
                )
            },
            datetime(2026, 8, 3, 22, tzinfo=UTC),
            "untrusted",
        ),
        (
            {
                "AAPL": MarketBar(
                    "AAPL",
                    SESSION,
                    *(Decimal("105"),) * 4,
                    "fixture",
                    datetime(2026, 8, 3, 22, tzinfo=UTC),
                    True,
                )
            },
            datetime(2026, 8, 3, 22, tzinfo=UTC),
            "adjusted",
        ),
        (
            {
                "AAPL": MarketBar(
                    "AAPL",
                    SESSION,
                    Decimal("105"),
                    Decimal("105"),
                    Decimal("105"),
                    Decimal("0"),
                    "fixture",
                    datetime(2026, 8, 3, 22, tzinfo=UTC),
                    False,
                )
            },
            datetime(2026, 8, 3, 22, tzinfo=UTC),
            "invalid",
        ),
        (
            {
                "AAPL": MarketBar(
                    "AAPL",
                    SESSION,
                    *(Decimal("105"),) * 4,
                    "fixture",
                    datetime(2026, 8, 3, 22, 1, tzinfo=UTC),
                    False,
                )
            },
            datetime(2026, 8, 3, 22, tzinfo=UTC),
            "future",
        ),
        (
            {
                "AAPL": MarketBar(
                    "AAPL",
                    SESSION,
                    *(Decimal("105"),) * 4,
                    "fixture",
                    datetime(2026, 8, 2, 21, 59, 59, tzinfo=UTC),
                    False,
                )
            },
            datetime(2026, 8, 3, 22, tzinfo=UTC),
            "stale",
        ),
        ({"AAPL": bar("AAPL", "105")}, datetime(2026, 8, 3, 22), "timezone-aware"),
        (
            {
                "AAPL": MarketBar(
                    "AAPL",
                    SESSION,
                    *(Decimal("105"),) * 4,
                    "fixture",
                    datetime(2026, 8, 3, 22),
                    False,
                )
            },
            datetime(2026, 8, 3, 22, tzinfo=UTC),
            "timezone-aware",
        ),
    ],
)
def test_mark_rejects_untrusted_provenance_before_any_persistence(
    tmp_path, close_marks, valuation_at, message
):
    ledger = PortfolioLedger(tmp_path / "ledger.db", COHORT, Decimal("5000"))
    try:
        order = intent("buy", "buy")
        stage(ledger, order, signal())
        ledger.apply_fill(order, fill("buy-fill", "buy", "buy", "100"))
        with pytest.raises(ValueError, match=message):
            ledger.mark(SESSION, close_marks, "epoch-1", valuation_at)
        assert (
            ledger.connection.execute("SELECT COUNT(*) FROM marks").fetchone()[0] == 0
        )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM account_snapshots"
            ).fetchone()[0]
            == 0
        )
    finally:
        ledger.close()


def test_accounting_summary_survives_restart_and_hot_reads_do_not_scan_history(
    tmp_path,
):
    path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(path, COHORT, Decimal("5000"))
    try:
        opening = intent("buy", "buy")
        stage(ledger, opening, signal())
        ledger.apply_fill(
            opening, fill("buy-fill", "buy", "buy", "100.10", slippage="1")
        )
        closing = intent("sell", "sell")
        stage(ledger, closing, signal("sell-signal"))
        ledger.apply_fill(closing, fill("sell-fill", "sell", "sell", "109.89"))
        expected_summary = tuple(
            ledger.connection.execute("SELECT * FROM accounting_state").fetchone()
        )
        traces: list[str] = []
        ledger.connection.set_trace_callback(traces.append)
        assert ledger.account_state().cash == Decimal("5097.90")
        ledger.connection.set_trace_callback(None)
        assert not any(
            f"FROM {table}" in statement.upper()
            for table in (
                "CASH_EVENTS",
                "FILL_COSTS",
                "LOT_CLOSURES",
                "ACCOUNT_SNAPSHOTS",
            )
            for statement in traces
        )
    finally:
        ledger.close()

    reopened = PortfolioLedger(path, COHORT, Decimal("5000"))
    try:
        assert (
            tuple(
                reopened.connection.execute("SELECT * FROM accounting_state").fetchone()
            )
            == expected_summary
        )
        assert reopened.account_state().cash == Decimal("5097.90")
    finally:
        reopened.close()

    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TABLE accounting_state")
    finally:
        connection.close()

    migrated = PortfolioLedger(path, COHORT, Decimal("5000"))
    try:
        assert (
            tuple(
                migrated.connection.execute("SELECT * FROM accounting_state").fetchone()
            )
            == expected_summary
        )
    finally:
        migrated.close()


def test_mark_hot_path_reads_summary_not_historical_accounting_detail(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.db", COHORT, Decimal("5000"))
    try:
        order = intent("buy", "buy")
        stage(ledger, order, signal())
        ledger.apply_fill(order, fill("buy-fill", "buy", "buy", "100.10", slippage="1"))
        traces: list[str] = []
        ledger.connection.set_trace_callback(traces.append)
        snapshot = ledger.mark(
            SESSION,
            {"AAPL": bar("AAPL", "105")},
            "epoch-1",
            datetime(2026, 8, 3, 22, tzinfo=UTC),
        )
        ledger.connection.set_trace_callback(None)
        assert snapshot.cash == Decimal("3999.00")
        assert snapshot.realized_pnl == Decimal("0")
        assert snapshot.slippage_cost == Decimal("1")
        assert not any(
            f"FROM {table}" in statement.upper()
            for table in ("CASH_EVENTS", "FILL_COSTS", "LOT_CLOSURES")
            for statement in traces
        )
        assert all(
            "WHERE SNAPSHOT_ID" in statement.upper()
            or (
                "WHERE COHORT_ID =" in statement.upper()
                and "AND SESSION =" in statement.upper()
            )
            for statement in traces
            if "FROM ACCOUNT_SNAPSHOTS" in statement.upper()
        )
    finally:
        ledger.close()
