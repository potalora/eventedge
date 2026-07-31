"""Transactional, per-cohort SQLite persistence for the paper ledger.

This module intentionally owns only persistence and typed reads.  Economic
mutations (fills, marks, and accruals) are added by later P0 tasks.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from tradingagents.strategies.execution import (
    AccountSnapshot,
    BenchmarkObservation,
    Fill,
    OrderIntent,
    SignalRecord,
    stable_id,
)


SCHEMA_VERSION = 1
_OPENING_AT = datetime(1970, 1, 1)


class LedgerConflictError(ValueError):
    """A stable ledger identity was reused with different content."""


# All schema statements are complete so initialization is deterministic and
# reviewable.  Decimals and timestamps are stored as canonical text.
_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS schema_metadata (
        metadata_key TEXT PRIMARY KEY, metadata_value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS metric_epochs (
        epoch_id TEXT PRIMARY KEY, generation_id TEXT NOT NULL,
        schema_version INTEGER NOT NULL, status TEXT NOT NULL,
        start_session TEXT NOT NULL, end_session TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS session_runs (
        session_run_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL,
        session TEXT NOT NULL, valid INTEGER NOT NULL, invalid_reason TEXT NOT NULL,
        started_at TEXT, completed_at TEXT,
        UNIQUE(cohort_id, session)
    )""",
    """CREATE TABLE IF NOT EXISTS session_phases (
        session_phase_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL,
        session TEXT NOT NULL, phase TEXT NOT NULL, started_at TEXT,
        completed_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS signals (
        signal_id TEXT PRIMARY KEY, epoch_id TEXT NOT NULL, policy_id TEXT NOT NULL,
        event_key TEXT NOT NULL, strategy TEXT NOT NULL, ticker TEXT NOT NULL,
        direction TEXT NOT NULL, event_at TEXT, observed_at TEXT NOT NULL,
        reference_session TEXT NOT NULL, reference_close TEXT NOT NULL,
        decision_at TEXT NOT NULL, evidence_hash TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS order_intents (
        intent_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, side TEXT NOT NULL,
        requested_qty INTEGER NOT NULL, created_at TEXT NOT NULL,
        eligible_session TEXT NOT NULL, price_rule TEXT NOT NULL,
        status TEXT NOT NULL, stop_price TEXT, external_order_id TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS intent_signals (
        intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        signal_id TEXT NOT NULL REFERENCES signals(signal_id),
        signal_order INTEGER NOT NULL,
        PRIMARY KEY(intent_id, signal_id),
        UNIQUE(intent_id, signal_order)
    )""",
    """CREATE TABLE IF NOT EXISTS order_status_transitions (
        transition_id TEXT PRIMARY KEY,
        intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        status TEXT NOT NULL, occurred_at TEXT NOT NULL, reason TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS external_orders (
        external_order_id TEXT PRIMARY KEY,
        intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        broker TEXT NOT NULL, status TEXT NOT NULL, submitted_at TEXT NOT NULL,
        reconciled_at TEXT, detail TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fills (
        fill_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        side TEXT NOT NULL, session TEXT NOT NULL, effective_at TEXT NOT NULL,
        processed_at TEXT NOT NULL, reference_price TEXT NOT NULL,
        fill_price TEXT NOT NULL, quantity INTEGER NOT NULL, slippage TEXT NOT NULL,
        commission TEXT NOT NULL, other_fees TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fill_costs (
        fill_cost_id TEXT PRIMARY KEY, fill_id TEXT NOT NULL REFERENCES fills(fill_id),
        cost_type TEXT NOT NULL, amount TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS lots (
        lot_id TEXT PRIMARY KEY, fill_id TEXT NOT NULL REFERENCES fills(fill_id),
        cohort_id TEXT NOT NULL, ticker TEXT NOT NULL, direction TEXT NOT NULL,
        opened_session TEXT NOT NULL, entry_price TEXT NOT NULL,
        original_qty INTEGER NOT NULL, open_qty INTEGER NOT NULL,
        margin_reserved TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS lot_closures (
        closure_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES lots(lot_id),
        fill_id TEXT NOT NULL REFERENCES fills(fill_id), quantity INTEGER NOT NULL,
        realized_pnl TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS corporate_actions (
        action_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, session TEXT NOT NULL,
        action_type TEXT NOT NULL, ratio TEXT, cash_per_share TEXT,
        source TEXT NOT NULL, fetched_at TEXT NOT NULL, verified INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS lot_action_applications (
        application_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL REFERENCES corporate_actions(action_id),
        lot_id TEXT NOT NULL REFERENCES lots(lot_id), applied_at TEXT NOT NULL,
        detail TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cash_events (
        cash_event_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, session TEXT,
        event_type TEXT NOT NULL, amount TEXT NOT NULL, effective_at TEXT NOT NULL,
        detail TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS borrow_accruals (
        accrual_id TEXT PRIMARY KEY, lot_id TEXT REFERENCES lots(lot_id),
        session TEXT NOT NULL, amount TEXT NOT NULL, annual_rate TEXT NOT NULL,
        flagged INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS financing_accruals (
        accrual_id TEXT PRIMARY KEY, session TEXT NOT NULL, amount TEXT NOT NULL,
        annual_rate TEXT NOT NULL, flagged INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS dividend_events (
        dividend_event_id TEXT PRIMARY KEY,
        action_id TEXT REFERENCES corporate_actions(action_id), lot_id TEXT REFERENCES lots(lot_id),
        session TEXT NOT NULL, amount TEXT NOT NULL, direction TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fee_events (
        fee_event_id TEXT PRIMARY KEY, fill_id TEXT REFERENCES fills(fill_id),
        session TEXT NOT NULL, fee_type TEXT NOT NULL, amount TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS marks (
        mark_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, ticker TEXT NOT NULL,
        session TEXT NOT NULL, close TEXT NOT NULL, source TEXT NOT NULL,
        observed_at TEXT NOT NULL, adjusted INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS account_snapshots (
        snapshot_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, epoch_id TEXT NOT NULL,
        session TEXT NOT NULL, valuation_at TEXT NOT NULL, cash TEXT NOT NULL,
        long_market_value TEXT NOT NULL, short_liability TEXT NOT NULL,
        gross_exposure TEXT NOT NULL, net_exposure TEXT NOT NULL,
        margin_used TEXT NOT NULL, buying_power TEXT NOT NULL, realized_pnl TEXT NOT NULL,
        unrealized_pnl TEXT NOT NULL, gross_equity TEXT NOT NULL,
        slippage_cost TEXT NOT NULL, commission_cost TEXT NOT NULL,
        other_fees TEXT NOT NULL, borrow_cost TEXT NOT NULL, financing_cost TEXT NOT NULL,
        dividend_cash TEXT NOT NULL, net_equity TEXT NOT NULL, high_water_mark TEXT NOT NULL,
        valid INTEGER NOT NULL, invalid_reason TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS benchmark_observations (
        observation_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, epoch_id TEXT NOT NULL,
        session TEXT NOT NULL, symbol TEXT NOT NULL, close TEXT NOT NULL,
        return_basis TEXT NOT NULL, source TEXT NOT NULL, observed_at TEXT NOT NULL,
        valid INTEGER NOT NULL, invalid_reason TEXT NOT NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshots_cohort_session
        ON account_snapshots(cohort_id, session)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_benchmarks_epoch_session_symbol
        ON benchmark_observations(cohort_id, epoch_id, session, symbol)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_fills_intent_session
        ON fills(intent_id, session)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_session_phases
        ON session_phases(cohort_id, session, phase)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_action_lot_application
        ON lot_action_applications(action_id, lot_id)""",
)

_IDENTITY_COLUMNS = {
    "schema_metadata": "metadata_key",
    "signals": "signal_id",
    "order_intents": "intent_id",
    "cash_events": "cash_event_id",
}
_ALLOWED_INSERT_COLUMNS = {
    "schema_metadata": frozenset({"metadata_key", "metadata_value"}),
    "signals": frozenset(
        {
            "signal_id",
            "epoch_id",
            "policy_id",
            "event_key",
            "strategy",
            "ticker",
            "direction",
            "event_at",
            "observed_at",
            "reference_session",
            "reference_close",
            "decision_at",
            "evidence_hash",
        }
    ),
    "order_intents": frozenset(
        {
            "intent_id",
            "cohort_id",
            "side",
            "requested_qty",
            "created_at",
            "eligible_session",
            "price_rule",
            "status",
            "stop_price",
            "external_order_id",
        }
    ),
    "cash_events": frozenset(
        {
            "cash_event_id",
            "cohort_id",
            "session",
            "event_type",
            "amount",
            "effective_at",
            "detail",
        }
    ),
}


def _decimal(value: str | Decimal) -> Decimal:
    return Decimal(value)


def _date(value: str | date) -> date:
    return (
        value
        if isinstance(value, date) and not isinstance(value, datetime)
        else date.fromisoformat(value)
    )


def _datetime(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


class PortfolioLedger:
    """One bounded SQLite connection for one authoritative cohort ledger."""

    def __init__(self, path: Path, cohort_id: str, initial_cash: Decimal) -> None:
        if not isinstance(initial_cash, Decimal):
            raise TypeError("initial_cash must be Decimal")
        self.path = Path(path)
        self.cohort_id = cohort_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._initialize(initial_cash)
        except BaseException:
            self._connection.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the connection for bounded administrative inspection/tests."""
        return self._connection

    def close(self) -> None:
        self._connection.close()

    def _initialize(self, initial_cash: Decimal) -> None:
        with self.transaction():
            for statement in _DDL:
                self._connection.execute(statement)
            self._insert_idempotent(
                "schema_metadata",
                "metadata_key",
                "schema_version",
                {
                    "metadata_key": "schema_version",
                    "metadata_value": str(SCHEMA_VERSION),
                },
            )
            self._insert_idempotent(
                "schema_metadata",
                "metadata_key",
                "cohort_id",
                {"metadata_key": "cohort_id", "metadata_value": self.cohort_id},
            )
            self._insert_idempotent(
                "cash_events",
                "cash_event_id",
                stable_id("cash", self.cohort_id, "opening"),
                {
                    "cash_event_id": stable_id("cash", self.cohort_id, "opening"),
                    "cohort_id": self.cohort_id,
                    "session": None,
                    "event_type": "opening",
                    "amount": initial_cash,
                    "effective_at": _OPENING_AT,
                    "detail": "deterministic opening cash",
                },
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    def _insert_idempotent(
        self,
        table: str,
        identity_column: str,
        identity: str,
        values: dict[str, object],
    ) -> bool:
        if _IDENTITY_COLUMNS.get(table) != identity_column:
            raise ValueError(f"unapproved ledger table or identity column: {table}")
        if (
            set(values) != _ALLOWED_INSERT_COLUMNS[table]
            or values.get(identity_column) != identity
        ):
            raise ValueError(f"unapproved ledger columns for {table}")
        existing = self._connection.execute(
            f"SELECT * FROM {table} WHERE {identity_column} = ?", (identity,)
        ).fetchone()
        encoded = {key: self._encode(value) for key, value in values.items()}
        if existing is not None:
            if any(existing[key] != encoded[key] for key in encoded):
                raise LedgerConflictError(f"conflicting {table} identity {identity}")
            return False
        columns = ", ".join(encoded)
        marks = ", ".join("?" for _ in encoded)
        self._connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(encoded.values())
        )
        return True

    @staticmethod
    def _encode(value: object) -> object:
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, bool):
            return int(value)
        return value

    def record_signal(self, signal: SignalRecord) -> None:
        with self.transaction():
            self._insert_idempotent(
                "signals",
                "signal_id",
                signal.signal_id,
                {
                    "signal_id": signal.signal_id,
                    "epoch_id": signal.epoch_id,
                    "policy_id": signal.policy_id,
                    "event_key": signal.event_key,
                    "strategy": signal.strategy,
                    "ticker": signal.ticker,
                    "direction": signal.direction,
                    "event_at": signal.event_at,
                    "observed_at": signal.observed_at,
                    "reference_session": signal.reference_session,
                    "reference_close": signal.reference_close,
                    "decision_at": signal.decision_at,
                    "evidence_hash": signal.evidence_hash,
                },
            )

    def stage_intent(self, intent: OrderIntent) -> None:
        if intent.cohort_id != self.cohort_id:
            raise ValueError("intent cohort_id does not match ledger cohort_id")
        with self.transaction():
            if not intent.signal_ids:
                raise ValueError("intent must reference at least one signal")
            if len(set(intent.signal_ids)) != len(intent.signal_ids):
                raise ValueError("intent signal_ids must be unique")
            signal_placeholders = ", ".join("?" for _ in intent.signal_ids)
            provenance_rows = self._connection.execute(
                "SELECT signal_id, epoch_id, policy_id FROM signals WHERE signal_id IN "
                f"({signal_placeholders})",
                intent.signal_ids,
            ).fetchall()
            provenance_by_id = {row["signal_id"]: row for row in provenance_rows}
            missing_signal_ids = [
                signal_id
                for signal_id in intent.signal_ids
                if signal_id not in provenance_by_id
            ]
            if missing_signal_ids:
                raise ValueError(
                    "intent references missing signal IDs: "
                    + ", ".join(missing_signal_ids)
                )
            epoch_ids = {
                provenance_by_id[signal_id]["epoch_id"]
                for signal_id in intent.signal_ids
            }
            if len(epoch_ids) != 1:
                raise ValueError("intent signals must share one epoch_id")
            policy_ids = {
                provenance_by_id[signal_id]["policy_id"]
                for signal_id in intent.signal_ids
            }
            if len(policy_ids) != 1:
                raise ValueError("intent signals must share one policy_id")
            inserted = self._insert_idempotent(
                "order_intents",
                "intent_id",
                intent.intent_id,
                {
                    "intent_id": intent.intent_id,
                    "cohort_id": intent.cohort_id,
                    "side": intent.side,
                    "requested_qty": intent.requested_qty,
                    "created_at": intent.created_at,
                    "eligible_session": intent.eligible_session,
                    "price_rule": intent.price_rule,
                    "status": intent.status,
                    "stop_price": intent.stop_price,
                    "external_order_id": intent.external_order_id,
                },
            )
            if not inserted:
                existing_signal_ids = tuple(
                    item[0]
                    for item in self._connection.execute(
                        "SELECT signal_id FROM intent_signals WHERE intent_id = ? "
                        "ORDER BY signal_order, signal_id",
                        (intent.intent_id,),
                    )
                )
                if existing_signal_ids != intent.signal_ids:
                    raise LedgerConflictError(
                        f"conflicting order_intents identity {intent.intent_id}"
                    )
                return
            for signal_order, signal_id in enumerate(intent.signal_ids):
                self._connection.execute(
                    "INSERT INTO intent_signals(intent_id, signal_id, signal_order) VALUES (?, ?, ?)",
                    (intent.intent_id, signal_id, signal_order),
                )

    def pending_intents(self, session: date) -> list[OrderIntent]:
        rows = self._connection.execute(
            """SELECT * FROM order_intents
               WHERE cohort_id = ? AND eligible_session = ? AND status = 'pending'
               ORDER BY eligible_session, intent_id""",
            (self.cohort_id, self._encode(session)),
        ).fetchall()
        return [self._intent_from_row(row) for row in rows]

    def read_snapshots(
        self,
        start_session: date | None = None,
        end_session: date | None = None,
        epoch_id: str | None = None,
        valid_only: bool = False,
    ) -> list[AccountSnapshot]:
        clauses, values = self._session_filters(
            "account_snapshots", start_session, end_session, epoch_id
        )
        if valid_only:
            clauses.append("valid = 1")
        rows = self._connection.execute(
            "SELECT * FROM account_snapshots WHERE cohort_id = ? AND "
            + " AND ".join(clauses)
            + " ORDER BY session, snapshot_id",
            (self.cohort_id, *values),
        ).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    def read_benchmark_observations(
        self,
        start_session: date | None = None,
        end_session: date | None = None,
        epoch_id: str | None = None,
    ) -> list[BenchmarkObservation]:
        clauses, values = self._session_filters(
            "benchmark_observations", start_session, end_session, epoch_id
        )
        rows = self._connection.execute(
            "SELECT * FROM benchmark_observations WHERE cohort_id = ? AND "
            + " AND ".join(clauses)
            + " ORDER BY session, observation_id",
            (self.cohort_id, *values),
        ).fetchall()
        return [self._benchmark_from_row(row) for row in rows]

    def read_signals(
        self,
        start_session: date | None = None,
        end_session: date | None = None,
        epoch_id: str | None = None,
        policy_id: str | None = None,
    ) -> list[SignalRecord]:
        clauses = ["1 = 1"]
        values: list[object] = []
        if start_session is not None:
            clauses.append("reference_session >= ?")
            values.append(self._encode(start_session))
        if end_session is not None:
            clauses.append("reference_session <= ?")
            values.append(self._encode(end_session))
        if epoch_id is not None:
            clauses.append("epoch_id = ?")
            values.append(epoch_id)
        if policy_id is not None:
            clauses.append("policy_id = ?")
            values.append(policy_id)
        rows = self._connection.execute(
            "SELECT * FROM signals WHERE "
            + " AND ".join(clauses)
            + " ORDER BY reference_session, signal_id",
            values,
        ).fetchall()
        return [self._signal_from_row(row) for row in rows]

    def read_fills(
        self,
        start_session: date | None = None,
        end_session: date | None = None,
        epoch_id: str | None = None,
    ) -> list[Fill]:
        clauses = ["i.cohort_id = ?"]
        values: list[object] = [self.cohort_id]
        if start_session is not None:
            clauses.append("f.session >= ?")
            values.append(self._encode(start_session))
        if end_session is not None:
            clauses.append("f.session <= ?")
            values.append(self._encode(end_session))
        if epoch_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM intent_signals isg JOIN signals s "
                "ON s.signal_id = isg.signal_id WHERE isg.intent_id = "
                "f.intent_id AND s.epoch_id = ?)"
            )
            values.append(epoch_id)
        rows = self._connection.execute(
            "SELECT f.* FROM fills f JOIN order_intents i ON i.intent_id = f.intent_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY f.session, f.fill_id",
            values,
        ).fetchall()
        return [self._fill_from_row(row) for row in rows]

    def _session_filters(
        self,
        table: str,
        start_session: date | None,
        end_session: date | None,
        epoch_id: str | None,
    ) -> tuple[list[str], list[object]]:
        if table not in {"account_snapshots", "benchmark_observations"}:
            raise ValueError("unapproved session table")
        clauses = ["1 = 1"]
        values: list[object] = []
        if start_session is not None:
            clauses.append("session >= ?")
            values.append(self._encode(start_session))
        if end_session is not None:
            clauses.append("session <= ?")
            values.append(self._encode(end_session))
        if epoch_id is not None:
            clauses.append("epoch_id = ?")
            values.append(epoch_id)
        return clauses, values

    def _intent_from_row(self, row: sqlite3.Row) -> OrderIntent:
        signal_ids = tuple(
            item[0]
            for item in self._connection.execute(
                "SELECT signal_id FROM intent_signals WHERE intent_id = ? "
                "ORDER BY signal_order, signal_id",
                (row["intent_id"],),
            )
        )
        return OrderIntent(
            row["intent_id"],
            signal_ids,
            row["cohort_id"],
            row["side"],
            row["requested_qty"],
            _datetime(row["created_at"]),
            _date(row["eligible_session"]),
            row["price_rule"],
            row["status"],
            _decimal(row["stop_price"]) if row["stop_price"] is not None else None,
            row["external_order_id"],
        )

    @staticmethod
    def _signal_from_row(row: sqlite3.Row) -> SignalRecord:
        return SignalRecord(
            row["signal_id"],
            row["epoch_id"],
            row["policy_id"],
            row["event_key"],
            row["strategy"],
            row["ticker"],
            row["direction"],
            _datetime(row["event_at"]) if row["event_at"] is not None else None,
            _datetime(row["observed_at"]),
            _date(row["reference_session"]),
            _decimal(row["reference_close"]),
            _datetime(row["decision_at"]),
            row["evidence_hash"],
        )

    @staticmethod
    def _fill_from_row(row: sqlite3.Row) -> Fill:
        return Fill(
            row["fill_id"],
            row["intent_id"],
            row["side"],
            _date(row["session"]),
            _datetime(row["effective_at"]),
            _datetime(row["processed_at"]),
            _decimal(row["reference_price"]),
            _decimal(row["fill_price"]),
            row["quantity"],
            _decimal(row["slippage"]),
            _decimal(row["commission"]),
            _decimal(row["other_fees"]),
        )

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> AccountSnapshot:
        decimal_columns = (
            "cash",
            "long_market_value",
            "short_liability",
            "gross_exposure",
            "net_exposure",
            "margin_used",
            "buying_power",
            "realized_pnl",
            "unrealized_pnl",
            "gross_equity",
            "slippage_cost",
            "commission_cost",
            "other_fees",
            "borrow_cost",
            "financing_cost",
            "dividend_cash",
            "net_equity",
            "high_water_mark",
        )
        values = [_decimal(row[column]) for column in decimal_columns]
        return AccountSnapshot(
            row["snapshot_id"],
            row["cohort_id"],
            row["epoch_id"],
            _date(row["session"]),
            _datetime(row["valuation_at"]),
            *values,
            bool(row["valid"]),
            row["invalid_reason"],
        )

    @staticmethod
    def _benchmark_from_row(row: sqlite3.Row) -> BenchmarkObservation:
        return BenchmarkObservation(
            row["observation_id"],
            row["cohort_id"],
            row["epoch_id"],
            _date(row["session"]),
            row["symbol"],
            _decimal(row["close"]),
            row["return_basis"],
            row["source"],
            _datetime(row["observed_at"]),
            bool(row["valid"]),
            row["invalid_reason"],
        )
