from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import sqlite3

import pytest

from tradingagents.strategies.execution import (
    CorporateAction,
    Fill,
    MarketBar,
    OrderIntent,
    SignalRecord,
)
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


UTC = timezone.utc
SESSION = date(2026, 8, 3)
COHORT = "horizon_30d_size_5k"


def _signal(
    signal_id: str, ticker: str = "AAPL", direction: str = "long"
) -> SignalRecord:
    now = datetime(2026, 8, 1, 22, tzinfo=UTC)
    return SignalRecord(
        signal_id,
        "epoch",
        "policy",
        signal_id,
        "test",
        ticker,
        direction,
        now,
        now,
        date(2026, 8, 1),
        Decimal("100"),
        now,
        signal_id,
    )


def _intent(
    intent_id: str, signal_id: str, side: str, qty: int = 10, stop: str | None = None
) -> OrderIntent:
    return OrderIntent(
        intent_id,
        (signal_id,),
        COHORT,
        side,
        qty,
        datetime(2026, 8, 1, 22, tzinfo=UTC),
        SESSION,
        "resting_stop" if stop else "next_session_open",
        "pending",
        Decimal(stop) if stop else None,
        None,
    )


def _fill(intent: OrderIntent, price: str = "100") -> Fill:
    at = datetime(2026, 8, 3, 22, tzinfo=UTC)
    return Fill(
        f"fill-{intent.intent_id}",
        intent.intent_id,
        intent.side,
        SESSION,
        at,
        at,
        Decimal(price),
        Decimal(price),
        intent.requested_qty,
        Decimal("0"),
        Decimal("0"),
        Decimal("0"),
    )


def _action(
    action_id: str,
    action_type: str,
    *,
    verified: bool = True,
    ratio: str | None = None,
    cash: str | None = None,
) -> CorporateAction:
    return CorporateAction(
        action_id,
        "AAPL",
        SESSION,
        action_type,
        Decimal(ratio) if ratio else None,
        Decimal(cash) if cash else None,
        "fixture",
        datetime(2026, 8, 3, 22, tzinfo=UTC),
        verified,
    )


def _opened_long(ledger: PortfolioLedger) -> tuple[OrderIntent, OrderIntent]:
    entry = _intent("entry", "entry-signal", "buy")
    exit_ = _intent("exit", "exit-signal", "sell", stop="95")
    ledger.record_signal(_signal("entry-signal"))
    ledger.record_signal(_signal("exit-signal"))
    ledger.stage_intent(entry)
    ledger.apply_fill(entry, _fill(entry))
    ledger.stage_intent(exit_)
    return entry, exit_


def test_split_adjusts_lot_and_resting_exit_without_changing_total_basis_and_replays_deterministically(
    tmp_path,
):
    ledger = PortfolioLedger(tmp_path / "ledger.db", COHORT, Decimal("5000"))
    try:
        _, exit_ = _opened_long(ledger)
        events = ledger.apply_corporate_actions(
            SESSION, [_action("split-1", "split", ratio="2")]
        )
        lot = ledger.connection.execute(
            "SELECT original_qty, open_qty, entry_price, margin_reserved FROM lots"
        ).fetchone()
        pending = ledger.pending_intents(SESSION)
        assert tuple(lot) == (20, 20, "50", "0")
        assert pending == [
            OrderIntent(
                **{**exit_.__dict__, "requested_qty": 20, "stop_price": Decimal("47.5")}
            )
        ]
        assert events[0].amount == Decimal("0")
        assert (
            ledger.apply_corporate_actions(
                SESSION, [_action("split-1", "split", ratio="2")]
            )
            == []
        )
        ledger.stage_intent(exit_)
        assert ledger.pending_intents(SESSION)[0].requested_qty == 20
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM lot_action_applications"
            ).fetchone()[0]
            == 1
        )
    finally:
        ledger.close()


def test_cash_dividend_credits_long_debits_short_and_duplicate_is_noop(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.db", COHORT, Decimal("5000"))
    try:
        _opened_long(ledger)
        short = _intent("short", "short-signal", "short")
        ledger.record_signal(_signal("short-signal", direction="short"))
        ledger.stage_intent(short)
        ledger.apply_fill(short, _fill(short), borrow_rate=Decimal("0.01"))
        events = ledger.apply_corporate_actions(
            SESSION, [_action("dividend-1", "cash_dividend", cash="1.25")]
        )
        assert [event.amount for event in events] == [
            Decimal("12.5000"),
            Decimal("-12.5000"),
        ]
        assert ledger.account_state().cash == Decimal("5000.0000")
        assert (
            ledger.apply_corporate_actions(
                SESSION, [_action("dividend-1", "cash_dividend", cash="1.25")]
            )
            == []
        )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM dividend_events"
            ).fetchone()[0]
            == 2
        )
    finally:
        ledger.close()


def test_unverified_or_conflicting_action_durably_quarantines_without_mutating_lot(
    tmp_path,
):
    ledger = PortfolioLedger(tmp_path / "ledger.db", COHORT, Decimal("5000"))
    try:
        _opened_long(ledger)
        before = tuple(
            ledger.connection.execute(
                "SELECT open_qty, entry_price FROM lots"
            ).fetchone()
        )
        events = ledger.apply_corporate_actions(
            SESSION, [_action("unverified", "split", verified=False, ratio="2")]
        )
        assert events[0].flagged is True
        assert (
            tuple(
                ledger.connection.execute(
                    "SELECT open_qty, entry_price FROM lots"
                ).fetchone()
            )
            == before
        )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM session_invalidations"
            ).fetchone()[0]
            == 1
        )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM ticker_quarantines"
            ).fetchone()[0]
            == 1
        )

        ledger.apply_corporate_actions(
            SESSION, [_action("conflict", "split", ratio="2")]
        )
        ledger.apply_corporate_actions(
            SESSION, [_action("conflict", "split", ratio="3")]
        )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM session_invalidations"
            ).fetchone()[0]
            == 2
        )
        assert (
            ledger.connection.execute("SELECT open_qty FROM lots").fetchone()[0] == 20
        )
    finally:
        ledger.close()


def test_quarantine_publishes_invalid_snapshot_and_fail_closed_session_check(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.db", COHORT, Decimal("5000"))
    try:
        _opened_long(ledger)
        ledger.apply_corporate_actions(
            SESSION, [_action("unverified-split", "split", verified=False, ratio="2")]
        )
        assert ledger.session_is_valid(SESSION) is False
        with pytest.raises(ValueError, match="invalid"):
            ledger.assert_session_tradeable(SESSION, "AAPL")
        value = Decimal("100")
        snapshot = ledger.mark(
            SESSION,
            {
                "AAPL": MarketBar(
                    "AAPL",
                    SESSION,
                    value,
                    value,
                    value,
                    value,
                    "fixture",
                    datetime(2026, 8, 3, 23, tzinfo=UTC),
                    False,
                )
            },
            "epoch",
            datetime(2026, 8, 3, 23, tzinfo=UTC),
        )
        assert snapshot.valid is False
        assert "unverified" in snapshot.invalid_reason
    finally:
        ledger.close()


def test_unresolved_ticker_quarantine_invalidates_later_marks_across_restart(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(path, COHORT, Decimal("5000"))
    try:
        _opened_long(ledger)
        ledger.apply_corporate_actions(
            SESSION,
            [_action("quarantined", "split", verified=False, ratio="2")],
        )
        next_session = date(2026, 8, 4)
        value = Decimal("100")
        later = MarketBar(
            "AAPL",
            next_session,
            value,
            value,
            value,
            value,
            "fixture",
            datetime(2026, 8, 4, 23, tzinfo=UTC),
            False,
        )
        assert (
            ledger.mark(
                next_session,
                {"AAPL": later},
                "epoch",
                datetime(2026, 8, 4, 23, tzinfo=UTC),
            ).valid
            is False
        )
    finally:
        ledger.close()
    reopened = PortfolioLedger(path, COHORT, Decimal("5000"))
    try:
        assert (
            reopened.mark(
                next_session,
                {"AAPL": later},
                "epoch",
                datetime(2026, 8, 4, 23, tzinfo=UTC),
            ).valid
            is False
        )
    finally:
        reopened.close()


def test_same_timestamp_split_chain_replays_in_persisted_causal_order_across_restart(
    tmp_path,
):
    path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(path, COHORT, Decimal("5000"))
    try:
        _, exit_ = _opened_long(ledger)
        ledger.apply_corporate_actions(
            SESSION,
            [
                _action("a", "split", ratio="2"),
                _action("b", "split", ratio="3"),
            ],
        )
        assert ledger.pending_intents(SESSION)[0].requested_qty == 60
    finally:
        ledger.close()
    reopened = PortfolioLedger(path, COHORT, Decimal("5000"))
    try:
        reopened.stage_intent(exit_)
        assert reopened.pending_intents(SESSION)[0].requested_qty == 60
        assert [
            row[0]
            for row in reopened.connection.execute(
                "SELECT adjustment_sequence FROM intent_action_adjustments ORDER BY adjustment_sequence"
            )
        ] == [1, 2]
    finally:
        reopened.close()


def test_each_conflicting_action_payload_is_persisted_as_immutable_evidence(tmp_path):
    ledger = PortfolioLedger(tmp_path / "ledger.db", COHORT, Decimal("5000"))
    try:
        _opened_long(ledger)
        ledger.apply_corporate_actions(
            SESSION, [_action("conflict", "split", ratio="2")]
        )
        ledger.apply_corporate_actions(
            SESSION, [_action("conflict", "split", ratio="3")]
        )
        ledger.apply_corporate_actions(
            SESSION, [_action("conflict", "split", ratio="4")]
        )
        rows = ledger.connection.execute(
            "SELECT action_id, content_hash, attempted_payload FROM corporate_action_conflicts ORDER BY content_hash"
        ).fetchall()
        assert len(rows) == 2
        assert {row[0] for row in rows} == {"conflict"}
        assert all("ratio" in row[2] for row in rows)
    finally:
        ledger.close()


def test_corporate_action_default_audit_time_is_causal_and_earlier_time_rejects(
    tmp_path,
):
    ledger = PortfolioLedger(tmp_path / "ledger.db", COHORT, Decimal("5000"))
    try:
        _opened_long(ledger)
        action = _action("timed", "split", ratio="2")
        ledger.apply_corporate_actions(SESSION, [action])
        applied_at = ledger.connection.execute(
            "SELECT applied_at FROM lot_action_applications"
        ).fetchone()[0]
        assert datetime.fromisoformat(applied_at) >= action.fetched_at
        bad = _action("too-early", "split", ratio="2")
        with pytest.raises(ValueError, match="precedes"):
            ledger.apply_corporate_actions(
                SESSION, [bad], action.fetched_at.replace(hour=1)
            )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM corporate_actions WHERE action_id = 'too-early'"
            ).fetchone()[0]
            == 0
        )
    finally:
        ledger.close()


def test_prefixed_adjustment_schema_backfills_reversed_physical_chain(tmp_path):
    path = tmp_path / "legacy.db"
    ledger = PortfolioLedger(path, COHORT, Decimal("5000"))
    try:
        _, original_exit = _opened_long(ledger)
        ledger.apply_corporate_actions(
            SESSION,
            [_action("a", "split", ratio="2"), _action("b", "split", ratio="3")],
        )
        ledger.connection.execute("DROP INDEX ux_intent_adjustment_sequence")
        ledger.connection.execute(
            "ALTER TABLE intent_action_adjustments RENAME TO modern_adjustments"
        )
        ledger.connection.execute(
            """CREATE TABLE intent_action_adjustments (
        adjustment_id TEXT PRIMARY KEY, action_id TEXT NOT NULL, intent_id TEXT NOT NULL,
        original_qty INTEGER NOT NULL, adjusted_qty INTEGER NOT NULL,
        original_stop_price TEXT, adjusted_stop_price TEXT, applied_at TEXT NOT NULL,
        UNIQUE(action_id, intent_id))"""
        )
        ledger.connection.execute(
            """INSERT INTO intent_action_adjustments
               SELECT adjustment_id, action_id, intent_id, original_qty, adjusted_qty,
                      original_stop_price, adjusted_stop_price, applied_at
               FROM modern_adjustments ORDER BY adjustment_sequence DESC"""
        )
        ledger.connection.execute("DROP TABLE modern_adjustments")
    finally:
        ledger.close()
    ledger = PortfolioLedger(path, COHORT, Decimal("5000"))
    try:
        assert [
            row[0]
            for row in ledger.connection.execute(
                "SELECT adjustment_sequence FROM intent_action_adjustments ORDER BY adjustment_sequence"
            )
        ] == [1, 2]
        ledger.stage_intent(original_exit)
        current = ledger.pending_intents(SESSION)[0]
        assert current.requested_qty == 60
        assert current.stop_price == Decimal("15.83333333333333333333333333")
    finally:
        ledger.close()


def test_prefixed_adjustment_schema_ambiguous_chain_fails_closed(tmp_path):
    path = tmp_path / "ambiguous.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE intent_action_adjustments (
        adjustment_id TEXT PRIMARY KEY, action_id TEXT NOT NULL, intent_id TEXT NOT NULL,
        original_qty INTEGER NOT NULL, adjusted_qty INTEGER NOT NULL,
        original_stop_price TEXT, adjusted_stop_price TEXT, applied_at TEXT NOT NULL,
        UNIQUE(action_id, intent_id))"""
    )
    connection.executemany(
        "INSERT INTO intent_action_adjustments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("a", "a", "exit", 10, 20, "95", "47.5", "x"),
            ("b", "b", "exit", 10, 30, "95", "31.6", "x"),
        ],
    )
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="ambiguous chain"):
        PortfolioLedger(path, COHORT, Decimal("5000"))
