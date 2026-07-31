from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tradingagents.strategies.execution import (
    AccountSnapshot,
    BenchmarkObservation,
    Fill,
    MarketBar,
    OrderIntent,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.state.portfolio_ledger import (
    LedgerConflictError,
    MissingMarkError,
    PortfolioLedger,
)
from tradingagents.strategies.state import portfolio_ledger


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


def _signal(
    *,
    signal_id: str = "signal-1",
    epoch_id: str = "epoch-1",
    policy_id: str = "policy-1",
) -> SignalRecord:
    return SignalRecord(
        signal_id=signal_id,
        epoch_id=epoch_id,
        policy_id=policy_id,
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


def _intent(
    *,
    requested_qty: int = 10,
    intent_id: str = "intent-1",
    signal_ids: tuple[str, ...] = ("signal-1",),
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        signal_ids=signal_ids,
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


def _fill(
    *,
    fill_id: str = "fill-1",
    intent_id: str = "intent-1",
    side: str = "buy",
    session: date = date(2026, 8, 3),
    fill_price: Decimal = Decimal("100.10"),
    quantity: int = 10,
    slippage: Decimal = Decimal("1.00"),
) -> Fill:
    timestamp = datetime(2026, 8, 3, 22, tzinfo=UTC)
    return Fill(
        fill_id,
        intent_id,
        side,
        session,
        timestamp,
        timestamp,
        Decimal("100.00"),
        fill_price,
        quantity,
        slippage,
        Decimal("0"),
        Decimal("0"),
    )


def _bar(ticker: str, session: date, close: Decimal) -> MarketBar:
    return MarketBar(
        ticker=ticker,
        session=session,
        open=close,
        high=close,
        low=close,
        close=close,
        source="fixture",
        fetched_at=datetime(2026, 8, 3, 22, tzinfo=UTC),
        adjusted=False,
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


@pytest.mark.parametrize(
    ("signal_ids", "signals", "message"),
    [
        ((), (), "at least one signal"),
        (("missing-signal",), (), "missing signal IDs"),
        (
            ("signal-1", "signal-2"),
            (_signal(), _signal(signal_id="signal-2", epoch_id="epoch-2")),
            "one epoch_id",
        ),
        (
            ("signal-1", "signal-2"),
            (_signal(), _signal(signal_id="signal-2", policy_id="policy-2")),
            "one policy_id",
        ),
    ],
)
def test_stage_intent_rejects_invalid_signal_provenance(
    tmp_path, signal_ids, signals, message
):
    ledger = PortfolioLedger(
        tmp_path / "portfolio.db", "horizon_30d_size_5k", Decimal("5000")
    )
    try:
        for signal in signals:
            ledger.record_signal(signal)
        with pytest.raises(ValueError, match=message):
            ledger.stage_intent(
                _intent(intent_id="invalid-intent", signal_ids=signal_ids)
            )
        assert ledger.pending_intents(date(2026, 8, 3)) == []
    finally:
        ledger.close()


def test_single_epoch_signal_provenance_returns_fill_from_exactly_one_epoch(tmp_path):
    ledger = PortfolioLedger(
        tmp_path / "portfolio.db", "horizon_30d_size_5k", Decimal("5000")
    )
    signal_one = _signal(signal_id="signal-1")
    signal_two = _signal(signal_id="signal-2")
    intent = _intent(
        intent_id="single-epoch-intent",
        signal_ids=(signal_two.signal_id, signal_one.signal_id),
    )
    session = date(2026, 8, 3)
    timestamp = datetime(2026, 8, 3, 22, tzinfo=UTC)
    try:
        ledger.record_signal(signal_one)
        ledger.record_signal(signal_two)
        ledger.stage_intent(intent)
        with ledger.transaction() as connection:
            connection.execute(
                "INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "single-epoch-fill",
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
        assert ledger.pending_intents(session) == [intent]
        assert [fill.fill_id for fill in ledger.read_fills(epoch_id="epoch-1")] == [
            "single-epoch-fill"
        ]
        assert ledger.read_fills(epoch_id="other-epoch") == []
    finally:
        ledger.close()


def test_intent_signals_enforce_unique_order_and_read_defensively(tmp_path):
    ledger = PortfolioLedger(
        tmp_path / "portfolio.db", "horizon_30d_size_5k", Decimal("5000")
    )
    first = _signal(signal_id="signal-1")
    second = _signal(signal_id="signal-2")
    third = _signal(signal_id="signal-3")
    intent = _intent(
        intent_id="ordered-intent", signal_ids=(second.signal_id, first.signal_id)
    )
    try:
        for signal in (first, second, third):
            ledger.record_signal(signal)
        ledger.stage_intent(intent)
        with pytest.raises(sqlite3.IntegrityError):
            with ledger.transaction() as connection:
                connection.execute(
                    "INSERT INTO intent_signals(intent_id, signal_id, signal_order) VALUES (?, ?, ?)",
                    (intent.intent_id, third.signal_id, 0),
                )
        assert ledger.pending_intents(intent.eligible_session) == [intent]
    finally:
        ledger.close()


def test_constructor_closes_connection_when_initialization_fails(monkeypatch, tmp_path):
    class TrackedConnection:
        closed = False
        row_factory = None

        def execute(self, *_args, **_kwargs):
            return None

        def close(self):
            self.closed = True

    connection = TrackedConnection()

    def fail_initialize(self, initial_cash):
        raise RuntimeError("initialization failure")

    monkeypatch.setattr(
        portfolio_ledger.sqlite3, "connect", lambda *_args, **_kwargs: connection
    )
    monkeypatch.setattr(PortfolioLedger, "_initialize", fail_initialize)

    with pytest.raises(RuntimeError, match="initialization failure"):
        PortfolioLedger(
            tmp_path / "portfolio.db", "horizon_30d_size_5k", Decimal("5000")
        )
    assert connection.closed is True


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


def test_fill_replay_after_restart_is_a_true_noop_and_divergence_conflicts(tmp_path):
    path = tmp_path / "portfolio.db"
    intent = _intent()
    fill = _fill()
    ledger = PortfolioLedger(path, intent.cohort_id, Decimal("5000"))
    try:
        ledger.record_signal(_signal())
        ledger.stage_intent(intent)
        first_state = ledger.apply_fill(intent, fill)
        first_counts = {
            table: ledger.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "fills",
                "fill_costs",
                "lots",
                "cash_events",
                "order_status_transitions",
            )
        }
        first_summary = tuple(
            ledger.connection.execute(
                """SELECT cash, realized_pnl, slippage_cost, commission_cost,
                   other_fees, high_water_mark FROM accounting_state"""
            ).fetchone()
        )
    finally:
        ledger.close()

    reopened = PortfolioLedger(path, intent.cohort_id, Decimal("5000"))
    try:
        assert reopened.apply_fill(intent, fill) == first_state
        assert {
            table: reopened.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in first_counts
        } == first_counts
        assert (
            tuple(
                reopened.connection.execute(
                    """SELECT cash, realized_pnl, slippage_cost, commission_cost,
                   other_fees, high_water_mark FROM accounting_state"""
                ).fetchone()
            )
            == first_summary
        )
        with pytest.raises(LedgerConflictError, match="fills identity fill-1"):
            reopened.apply_fill(
                intent, Fill(**{**fill.__dict__, "fill_price": Decimal("100.11")})
            )
    finally:
        reopened.close()


def test_mark_requires_every_open_lot_to_have_exact_raw_session_bar(tmp_path):
    ledger = PortfolioLedger(
        tmp_path / "portfolio.db", "horizon_30d_size_5k", Decimal("5000")
    )
    intent = _intent()
    try:
        ledger.record_signal(_signal())
        ledger.stage_intent(intent)
        ledger.apply_fill(intent, _fill())
        session = date(2026, 8, 3)
        with pytest.raises(MissingMarkError, match="AAPL"):
            ledger.mark(session, {}, "epoch-1", datetime(2026, 8, 3, 22, tzinfo=UTC))
        assert (
            ledger.connection.execute("SELECT COUNT(*) FROM marks").fetchone()[0] == 0
        )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM account_snapshots"
            ).fetchone()[0]
            == 0
        )

        snapshot = ledger.mark(
            session,
            {"AAPL": _bar("AAPL", session, Decimal("105.00"))},
            "epoch-1",
            datetime(2026, 8, 3, 22, tzinfo=UTC),
        )
        assert snapshot.net_equity == Decimal("5049.00")
        assert (
            ledger.mark(
                session,
                {"AAPL": _bar("AAPL", session, Decimal("105.00"))},
                "epoch-1",
                datetime(2026, 8, 3, 22, tzinfo=UTC),
            )
            == snapshot
        )
        with pytest.raises(LedgerConflictError):
            ledger.mark(
                session,
                {"AAPL": _bar("AAPL", session, Decimal("104.00"))},
                "epoch-1",
                datetime(2026, 8, 3, 22, tzinfo=UTC),
            )
    finally:
        ledger.close()
