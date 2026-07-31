from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tradingagents.strategies.execution import (
    AccountSnapshot,
    BenchmarkObservation,
    Fill,
    OrderIntent,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.state.portfolio_ledger import (
    LedgerConflictError,
    PortfolioLedger,
)


UTC = timezone.utc
REQUIRED_TABLES = {
    "schema_metadata",
    "metric_epochs",
    "session_runs",
    "session_phases",
    "signals",
    "order_intents",
    "intent_signals",
    "order_status_transitions",
    "external_orders",
    "fills",
    "fill_costs",
    "lots",
    "lot_closures",
    "corporate_actions",
    "lot_action_applications",
    "cash_events",
    "borrow_accruals",
    "financing_accruals",
    "dividend_events",
    "fee_events",
    "marks",
    "account_snapshots",
    "benchmark_observations",
}


def _signal() -> SignalRecord:
    return SignalRecord(
        signal_id="signal-1",
        epoch_id="epoch-1",
        policy_id="policy-1",
        event_key="event-1",
        strategy="litigation",
        ticker="AAPL",
        direction="long",
        event_at=datetime(2026, 7, 31, 20, tzinfo=UTC),
        observed_at=datetime(2026, 7, 31, 20, 5, tzinfo=UTC),
        reference_session=date(2026, 7, 31),
        reference_close=Decimal("100.00"),
        decision_at=datetime(2026, 7, 31, 22, tzinfo=UTC),
        evidence_hash="evidence-1",
    )


def _intent(*, requested_qty: int = 10) -> OrderIntent:
    return OrderIntent(
        intent_id="intent-1",
        signal_ids=("signal-1",),
        cohort_id="horizon_30d_size_5k",
        side="buy",
        requested_qty=requested_qty,
        created_at=datetime(2026, 7, 31, 22, 5, tzinfo=UTC),
        eligible_session=date(2026, 8, 3),
        price_rule="next_session_open",
        status="pending",
        stop_price=None,
        external_order_id=None,
    )


def test_initialization_creates_authoritative_schema_and_durable_settings(tmp_path):
    ledger = PortfolioLedger(
        tmp_path / "portfolio.db", "horizon_30d_size_5k", Decimal("5000.00")
    )
    try:
        tables = {
            row[0]
            for row in ledger.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert REQUIRED_TABLES <= tables
        assert ledger.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert ledger.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert ledger.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        ledger.close()


def test_signal_and_intent_idempotency_and_conflicts(tmp_path):
    ledger = PortfolioLedger(
        tmp_path / "portfolio.db", "horizon_30d_size_5k", Decimal("5000")
    )
    signal = _signal()
    intent = _intent()
    try:
        ledger.record_signal(signal)
        ledger.record_signal(signal)
        ledger.stage_intent(intent)
        ledger.stage_intent(intent)
        assert ledger.read_signals() == [signal]
        assert ledger.pending_intents(intent.eligible_session) == [intent]

        with pytest.raises(LedgerConflictError, match="signals identity signal-1"):
            ledger.record_signal(
                SignalRecord(**{**signal.__dict__, "evidence_hash": "changed-evidence"})
            )
        with pytest.raises(
            LedgerConflictError, match="order_intents identity intent-1"
        ):
            ledger.stage_intent(_intent(requested_qty=11))
    finally:
        ledger.close()


def test_intent_duplicate_with_different_signal_provenance_conflicts(tmp_path):
    ledger = PortfolioLedger(
        tmp_path / "portfolio.db", "horizon_30d_size_5k", Decimal("5000")
    )
    signal = _signal()
    alternate_signal = SignalRecord(
        **{**signal.__dict__, "signal_id": "signal-2", "event_key": "event-2"}
    )
    try:
        ledger.record_signal(signal)
        ledger.record_signal(alternate_signal)
        ledger.stage_intent(_intent())
        conflicting = OrderIntent(
            **{**_intent().__dict__, "signal_ids": (alternate_signal.signal_id,)}
        )
        with pytest.raises(
            LedgerConflictError, match="order_intents identity intent-1"
        ):
            ledger.stage_intent(conflicting)
    finally:
        ledger.close()


def test_reopen_preserves_pending_intent_and_single_deterministic_opening_cash(
    tmp_path,
):
    path = tmp_path / "portfolio.db"
    cohort_id = "horizon_30d_size_5k"
    ledger = PortfolioLedger(path, cohort_id, Decimal("5000.00"))
    try:
        ledger.record_signal(_signal())
        ledger.stage_intent(_intent())
    finally:
        ledger.close()

    reopened = PortfolioLedger(path, cohort_id, Decimal("5000.00"))
    try:
        assert reopened.pending_intents(date(2026, 8, 3)) == [_intent()]
        opening = reopened.connection.execute(
            "SELECT cash_event_id, amount, event_type FROM cash_events"
        ).fetchall()
        assert [tuple(row) for row in opening] == [
            (stable_id("cash", cohort_id, "opening"), "5000.00", "opening")
        ]
    finally:
        reopened.close()


def test_reopen_rejects_different_initial_cash_without_duplicating_opening_event(
    tmp_path,
):
    path = tmp_path / "portfolio.db"
    ledger = PortfolioLedger(path, "horizon_30d_size_5k", Decimal("5000.00"))
    ledger.close()

    with pytest.raises(LedgerConflictError, match="cash_events identity"):
        PortfolioLedger(path, "horizon_30d_size_5k", Decimal("4000.00"))

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM cash_events").fetchone()[0] == 1
    finally:
        connection.close()


def test_typed_ordered_reads_filter_sqlite_rows_by_session_and_epoch(tmp_path):
    ledger = PortfolioLedger(
        tmp_path / "portfolio.db", "horizon_30d_size_5k", Decimal("5000")
    )
    signal = _signal()
    intent = _intent()
    session = date(2026, 8, 3)
    timestamp = datetime(2026, 8, 3, 22, tzinfo=UTC)
    decimals = (
        "5000",
        "0",
        "0",
        "0",
        "0",
        "0",
        "5000",
        "0",
        "0",
        "5000",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "5000",
        "5000",
    )
    try:
        ledger.record_signal(signal)
        ledger.stage_intent(intent)
        with ledger.transaction() as connection:
            connection.execute(
                """INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "fill-1",
                    intent.intent_id,
                    "buy",
                    session.isoformat(),
                    timestamp.isoformat(),
                    timestamp.isoformat(),
                    "100",
                    "100.10",
                    10,
                    "1",
                    "0",
                    "0",
                ),
            )
            connection.execute(
                """INSERT INTO account_snapshots VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                (
                    "snapshot-2",
                    ledger.cohort_id,
                    "epoch-1",
                    "2026-08-04",
                    timestamp.isoformat(),
                    *decimals,
                    1,
                    "",
                ),
            )
            connection.execute(
                """INSERT INTO account_snapshots VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                (
                    "snapshot-1",
                    ledger.cohort_id,
                    "epoch-1",
                    session.isoformat(),
                    timestamp.isoformat(),
                    *decimals,
                    1,
                    "",
                ),
            )
            connection.execute(
                """INSERT INTO benchmark_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "benchmark-1",
                    ledger.cohort_id,
                    "epoch-1",
                    session.isoformat(),
                    "SPY",
                    "600.00",
                    "total_return_adjusted",
                    "yfinance",
                    timestamp.isoformat(),
                    1,
                    "",
                ),
            )

        snapshots = ledger.read_snapshots(epoch_id="epoch-1", valid_only=True)
        assert [snapshot.snapshot_id for snapshot in snapshots] == [
            "snapshot-1",
            "snapshot-2",
        ]
        assert all(isinstance(snapshot, AccountSnapshot) for snapshot in snapshots)
        assert snapshots[0].cash == Decimal("5000")
        assert ledger.read_snapshots(start_session=date(2026, 8, 4)) == [snapshots[1]]

        observations = ledger.read_benchmark_observations(epoch_id="epoch-1")
        assert len(observations) == 1
        assert isinstance(observations[0], BenchmarkObservation)
        assert observations[0].close == Decimal("600.00")

        fills = ledger.read_fills(epoch_id="epoch-1")
        assert fills == [
            Fill(
                "fill-1",
                "intent-1",
                "buy",
                session,
                timestamp,
                timestamp,
                Decimal("100"),
                Decimal("100.10"),
                10,
                Decimal("1"),
                Decimal("0"),
                Decimal("0"),
            )
        ]
    finally:
        ledger.close()
