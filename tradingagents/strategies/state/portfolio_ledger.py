"""Transactional, per-cohort SQLite persistence for the paper ledger.

All economic mutations in this module are durable SQLite transactions.  The
ledger deliberately derives instruments from persisted signal provenance: an
order or fill never carries an independently asserted ticker.
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
    AccountState,
    BenchmarkObservation,
    Fill,
    MarketBar,
    OrderIntent,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.execution.price_source import (
    BarValidationError,
    validate_required_bars,
)


SCHEMA_VERSION = 1
_OPENING_AT = datetime(1970, 1, 1)


class LedgerConflictError(ValueError):
    """A stable ledger identity was reused with different content."""


class MissingMarkError(ValueError):
    """An authoritative valuation was attempted without every required mark."""


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
    """CREATE TABLE IF NOT EXISTS accounting_state (
        cohort_id TEXT PRIMARY KEY, cash TEXT NOT NULL, realized_pnl TEXT NOT NULL,
        slippage_cost TEXT NOT NULL, commission_cost TEXT NOT NULL,
        other_fees TEXT NOT NULL, borrow_cost TEXT NOT NULL,
        financing_cost TEXT NOT NULL, dividend_cash TEXT NOT NULL,
        high_water_mark TEXT NOT NULL
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
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_marks_cohort_ticker_session
        ON marks(cohort_id, ticker, session)""",
    """CREATE INDEX IF NOT EXISTS ix_lots_cohort_ticker_direction_open
        ON lots(cohort_id, ticker, direction, opened_session, lot_id)
        WHERE open_qty > 0""",
    """CREATE INDEX IF NOT EXISTS ix_cash_events_cohort_event
        ON cash_events(cohort_id, event_type, session)""",
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
            self._initialize_accounting_state()

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

    def _initialize_accounting_state(self) -> None:
        """Create or deterministically migrate the one-row cohort summary."""
        existing = self._connection.execute(
            "SELECT cohort_id FROM accounting_state WHERE cohort_id = ?",
            (self.cohort_id,),
        ).fetchone()
        if existing is not None:
            return
        summary = self._historical_summary_for_migration()
        self._connection.execute(
            """INSERT INTO accounting_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self.cohort_id,
                self._encode(summary["cash"]),
                self._encode(summary["realized_pnl"]),
                self._encode(summary["slippage_cost"]),
                self._encode(summary["commission_cost"]),
                self._encode(summary["other_fees"]),
                self._encode(summary["borrow_cost"]),
                self._encode(summary["financing_cost"]),
                self._encode(summary["dividend_cash"]),
                self._encode(summary["high_water_mark"]),
            ),
        )

    def _historical_summary_for_migration(self) -> dict[str, Decimal]:
        """Read audit detail once only when upgrading a pre-summary ledger."""
        opening = self._connection.execute(
            """SELECT amount FROM cash_events WHERE cohort_id = ?
               AND event_type = 'opening'""",
            (self.cohort_id,),
        ).fetchone()
        if opening is None:
            raise LedgerConflictError("missing deterministic opening cash event")
        cash = self._decimal_column_total(
            "cash_events", "amount", "cohort_id = ?", (self.cohort_id,)
        )
        realized_pnl = self._decimal_column_total("lot_closures", "realized_pnl")
        slippage_cost = self._fill_cost_total_for_migration("slippage")
        commission_cost = self._fill_cost_total_for_migration("commission")
        other_fees = self._fill_cost_total_for_migration("other_fees")
        borrow_cost = self._decimal_column_total("borrow_accruals", "amount")
        financing_cost = self._decimal_column_total("financing_accruals", "amount")
        dividend_cash = self._decimal_column_total("dividend_events", "amount")
        high_water_mark = _decimal(opening["amount"])
        snapshot_rows = self._connection.execute(
            "SELECT high_water_mark FROM account_snapshots WHERE cohort_id = ?",
            (self.cohort_id,),
        ).fetchall()
        for row in snapshot_rows:
            high_water_mark = max(high_water_mark, _decimal(row["high_water_mark"]))
        return {
            "cash": cash,
            "realized_pnl": realized_pnl,
            "slippage_cost": slippage_cost,
            "commission_cost": commission_cost,
            "other_fees": other_fees,
            "borrow_cost": borrow_cost,
            "financing_cost": financing_cost,
            "dividend_cash": dividend_cash,
            "high_water_mark": high_water_mark,
        }

    def _decimal_column_total(
        self,
        table: str,
        column: str,
        where: str = "1 = 1",
        values: tuple[object, ...] = (),
    ) -> Decimal:
        approved = {
            ("cash_events", "amount"),
            ("lot_closures", "realized_pnl"),
            ("borrow_accruals", "amount"),
            ("financing_accruals", "amount"),
            ("dividend_events", "amount"),
        }
        if (table, column) not in approved or where not in {"1 = 1", "cohort_id = ?"}:
            raise ValueError("unapproved accounting migration query")
        rows = self._connection.execute(
            f"SELECT {column} FROM {table} WHERE {where}", values
        ).fetchall()
        return sum((_decimal(row[column]) for row in rows), Decimal("0"))

    def _fill_cost_total_for_migration(self, cost_type: str) -> Decimal:
        if cost_type not in {"slippage", "commission", "other_fees"}:
            raise ValueError("unapproved fill cost type")
        rows = self._connection.execute(
            "SELECT amount FROM fill_costs WHERE cost_type = ?", (cost_type,)
        ).fetchall()
        return sum((_decimal(row["amount"]) for row in rows), Decimal("0"))

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

    def apply_fill(self, intent: OrderIntent, fill: Fill) -> AccountState:
        """Apply one fully validated fill and every linked mutation atomically."""
        with self.transaction():
            stored_row = self._connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent.intent_id,)
            ).fetchone()
            if stored_row is None:
                raise ValueError(f"unknown order intent {intent.intent_id}")
            stored_intent = self._intent_from_row(stored_row)
            existing_fill = self._connection.execute(
                "SELECT * FROM fills WHERE fill_id = ?", (fill.fill_id,)
            ).fetchone()
            if intent != stored_intent and not (
                existing_fill is not None
                and self._same_intent_except_status(intent, stored_intent)
            ):
                raise LedgerConflictError(
                    f"conflicting order_intents identity {intent.intent_id}"
                )
            self._validate_fill(stored_intent, fill)
            if existing_fill is not None:
                self._require_identical_fill(existing_fill, fill)
                stored_status = self._connection.execute(
                    "SELECT status FROM order_intents WHERE intent_id = ?",
                    (fill.intent_id,),
                ).fetchone()
                if stored_status is None or stored_status["status"] != "filled":
                    raise LedgerConflictError(
                        f"fill replay {fill.fill_id} has incomplete intent state"
                    )
            else:
                ticker = self._ticker_for_intent(stored_intent.intent_id)
                if stored_intent.status != "pending":
                    raise ValueError(
                        f"order intent {stored_intent.intent_id} is not pending"
                    )
                duplicate_session = self._connection.execute(
                    "SELECT fill_id FROM fills WHERE intent_id = ? AND session = ?",
                    (stored_intent.intent_id, self._encode(fill.session)),
                ).fetchone()
                if duplicate_session is not None:
                    raise LedgerConflictError(
                        "conflicting fills for intent/session "
                        f"{stored_intent.intent_id}/{fill.session}"
                    )
                self._insert_fill(fill)
                self._insert_fill_costs(fill)
                realized_pnl = Decimal("0")
                if stored_intent.side in {"buy", "short"}:
                    self._insert_open_lot(stored_intent, fill, ticker)
                else:
                    realized_pnl = self._close_lots(stored_intent, fill, ticker)
                cash_delta = self._cash_delta(stored_intent.side, fill)
                self._insert_cash_event(fill, cash_delta)
                self._update_accounting_summary(
                    cash_delta=cash_delta,
                    realized_pnl_delta=realized_pnl,
                    slippage_cost_delta=fill.slippage,
                    commission_cost_delta=fill.commission,
                    other_fees_delta=fill.other_fees,
                )
                self._connection.execute(
                    "UPDATE order_intents SET status = 'filled' WHERE intent_id = ?",
                    (stored_intent.intent_id,),
                )
                self._connection.execute(
                    """INSERT INTO order_status_transitions
                    (transition_id, intent_id, status, occurred_at, reason)
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        stable_id(
                            "order_status", stored_intent.intent_id, fill.fill_id
                        ),
                        stored_intent.intent_id,
                        "filled",
                        self._encode(fill.processed_at),
                        "authoritative fill applied",
                    ),
                )
        return self.account_state()

    def mark(
        self,
        session: date,
        close_marks: dict[str, MarketBar],
        epoch_id: str,
        valuation_at: datetime,
    ) -> AccountSnapshot:
        """Persist exact raw marks and an immutable, reconciled snapshot."""
        self._require_timezone_aware(valuation_at, "valuation_at")
        with self.transaction():
            open_lots = self._open_lots()
            marks_by_ticker = self._validate_marks(
                session, close_marks, open_lots, valuation_at
            )
            for ticker, bar in marks_by_ticker.items():
                self._insert_mark(ticker, bar)
            snapshot = self._build_snapshot(
                session, epoch_id, valuation_at, open_lots, marks_by_ticker
            )
            existing = self._connection.execute(
                "SELECT * FROM account_snapshots WHERE snapshot_id = ?",
                (snapshot.snapshot_id,),
            ).fetchone()
            if existing is not None:
                self._require_identical_snapshot(existing, snapshot)
            else:
                occupied = self._connection.execute(
                    "SELECT snapshot_id FROM account_snapshots WHERE cohort_id = ? AND session = ?",
                    (self.cohort_id, self._encode(session)),
                ).fetchone()
                if occupied is not None:
                    raise LedgerConflictError(
                        f"conflicting account_snapshots session {self.cohort_id}/{session}"
                    )
                self._connection.execute(
                    """INSERT INTO account_snapshots VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )""",
                    self._snapshot_values(snapshot),
                )
                self._update_accounting_summary(
                    high_water_mark=snapshot.high_water_mark
                )
        return snapshot

    def account_state(self) -> AccountState:
        """Return bounded cohort state, using a lot's entry until its first mark."""
        open_lots = self._open_lots(include_latest_mark=True)
        summary = self._accounting_summary()
        long_value = Decimal("0")
        short_liability = Decimal("0")
        margin_used = Decimal("0")
        for lot in open_lots:
            mark = (
                _decimal(lot["mark_close"])
                if lot["mark_close"] is not None
                else _decimal(lot["entry_price"])
            )
            value = _decimal(lot["open_qty"]) * mark
            if lot["direction"] == "long":
                long_value += value
            else:
                short_liability += value
                margin_used += _decimal(lot["margin_reserved"])
        net_equity = summary["cash"] + long_value - short_liability
        return AccountState(
            self.cohort_id,
            summary["cash"],
            long_value,
            short_liability,
            margin_used,
            summary["cash"] - margin_used,
            net_equity,
            max(summary["high_water_mark"], net_equity),
        )

    def _validate_fill(self, intent: OrderIntent, fill: Fill) -> None:
        if fill.intent_id != intent.intent_id:
            raise ValueError("fill intent_id does not match order intent")
        if fill.session != intent.eligible_session:
            raise ValueError("fill session does not match eligible session")
        if fill.side != intent.side:
            raise ValueError("fill side does not match order intent")
        if fill.quantity != intent.requested_qty:
            raise ValueError("fill quantity does not match requested quantity")
        if fill.quantity <= 0:
            raise ValueError("fill quantity must be positive")
        for label, value, strictly_positive in (
            ("reference_price", fill.reference_price, True),
            ("fill_price", fill.fill_price, True),
            ("slippage", fill.slippage, False),
            ("commission", fill.commission, False),
            ("other_fees", fill.other_fees, False),
        ):
            if not value.is_finite() or (
                value <= 0 if strictly_positive else value < 0
            ):
                raise ValueError(f"fill {label} is invalid")

    def _ticker_for_intent(self, intent_id: str) -> str:
        rows = self._connection.execute(
            """SELECT DISTINCT s.ticker FROM intent_signals isg
               JOIN signals s ON s.signal_id = isg.signal_id
               WHERE isg.intent_id = ? ORDER BY s.ticker""",
            (intent_id,),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError(
                f"intent {intent_id} must have exactly one unambiguous ticker provenance"
            )
        return rows[0]["ticker"]

    @staticmethod
    def _same_intent_except_status(supplied: OrderIntent, stored: OrderIntent) -> bool:
        return (
            supplied.intent_id,
            supplied.signal_ids,
            supplied.cohort_id,
            supplied.side,
            supplied.requested_qty,
            supplied.created_at,
            supplied.eligible_session,
            supplied.price_rule,
            supplied.stop_price,
            supplied.external_order_id,
        ) == (
            stored.intent_id,
            stored.signal_ids,
            stored.cohort_id,
            stored.side,
            stored.requested_qty,
            stored.created_at,
            stored.eligible_session,
            stored.price_rule,
            stored.stop_price,
            stored.external_order_id,
        )

    def _insert_fill(self, fill: Fill) -> None:
        self._connection.execute(
            "INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._fill_values(fill),
        )

    def _insert_fill_costs(self, fill: Fill) -> None:
        for cost_type, amount in (
            ("slippage", fill.slippage),
            ("commission", fill.commission),
            ("other_fees", fill.other_fees),
        ):
            self._connection.execute(
                "INSERT INTO fill_costs VALUES (?, ?, ?, ?)",
                (
                    stable_id("fill_cost", fill.fill_id, cost_type),
                    fill.fill_id,
                    cost_type,
                    self._encode(amount),
                ),
            )

    def _insert_open_lot(self, intent: OrderIntent, fill: Fill, ticker: str) -> None:
        direction = "long" if intent.side == "buy" else "short"
        margin = (
            fill.fill_price * fill.quantity * Decimal("1.5")
            if direction == "short"
            else Decimal("0")
        )
        self._connection.execute(
            "INSERT INTO lots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stable_id("lot", fill.fill_id),
                fill.fill_id,
                self.cohort_id,
                ticker,
                direction,
                self._encode(fill.session),
                self._encode(fill.fill_price),
                fill.quantity,
                fill.quantity,
                self._encode(margin),
            ),
        )

    def _close_lots(self, intent: OrderIntent, fill: Fill, ticker: str) -> Decimal:
        direction = "long" if intent.side == "sell" else "short"
        lots = self._connection.execute(
            """SELECT * FROM lots WHERE cohort_id = ? AND ticker = ? AND direction = ?
               AND open_qty > 0 ORDER BY opened_session, lot_id""",
            (self.cohort_id, ticker, direction),
        ).fetchall()
        if sum(int(lot["open_qty"]) for lot in lots) < fill.quantity:
            raise ValueError("close fill has insufficient matching open lots")
        remaining = fill.quantity
        realized_total = Decimal("0")
        for lot in lots:
            if remaining == 0:
                break
            lot_open_qty = int(lot["open_qty"])
            close_qty = min(remaining, lot_open_qty)
            entry_price = _decimal(lot["entry_price"])
            realized = (
                (fill.fill_price - entry_price) * close_qty
                if direction == "long"
                else (entry_price - fill.fill_price) * close_qty
            )
            existing_margin = _decimal(lot["margin_reserved"])
            released_margin = (
                existing_margin * Decimal(close_qty) / Decimal(lot_open_qty)
            )
            self._connection.execute(
                "INSERT INTO lot_closures VALUES (?, ?, ?, ?, ?)",
                (
                    stable_id("lot_closure", fill.fill_id, lot["lot_id"]),
                    lot["lot_id"],
                    fill.fill_id,
                    close_qty,
                    self._encode(realized),
                ),
            )
            self._connection.execute(
                "UPDATE lots SET open_qty = ?, margin_reserved = ? WHERE lot_id = ?",
                (
                    lot_open_qty - close_qty,
                    self._encode(existing_margin - released_margin),
                    lot["lot_id"],
                ),
            )
            remaining -= close_qty
            realized_total += realized
        return realized_total

    @staticmethod
    def _cash_delta(side: str, fill: Fill) -> Decimal:
        notional = fill.fill_price * fill.quantity
        fees = fill.commission + fill.other_fees
        return {
            "buy": -notional - fees,
            "sell": notional - fees,
            "short": notional - fees,
            "cover": -notional - fees,
        }[side]

    def _insert_cash_event(self, fill: Fill, amount: Decimal) -> None:
        self._connection.execute(
            "INSERT INTO cash_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                stable_id("cash", self.cohort_id, "fill", fill.fill_id),
                self.cohort_id,
                self._encode(fill.session),
                "fill",
                self._encode(amount),
                self._encode(fill.effective_at),
                f"fill {fill.fill_id}",
            ),
        )

    def _open_lots(self, *, include_latest_mark: bool = False) -> list[sqlite3.Row]:
        mark_column = (
            """, (
                SELECT m.close FROM marks m
                WHERE m.cohort_id = l.cohort_id AND m.ticker = l.ticker
                  AND m.session >= l.opened_session
                ORDER BY m.session DESC, m.mark_id DESC LIMIT 1
            ) AS mark_close"""
            if include_latest_mark
            else ""
        )
        return self._connection.execute(
            """SELECT l.*"""
            + mark_column
            + """ FROM lots l WHERE cohort_id = ? AND open_qty > 0
                 ORDER BY ticker, direction, opened_session, lot_id""",
            (self.cohort_id,),
        ).fetchall()

    def _validate_marks(
        self,
        session: date,
        close_marks: dict[str, MarketBar],
        open_lots: list[sqlite3.Row],
        valuation_at: datetime,
    ) -> dict[str, MarketBar]:
        required_tickers = sorted({lot["ticker"] for lot in open_lots})
        validated: dict[str, MarketBar] = {}
        for ticker in required_tickers:
            bar = close_marks.get(ticker)
            if bar is None:
                raise MissingMarkError(f"missing mark for {ticker}/{session}")
            if bar.ticker != ticker or bar.session != session:
                raise MissingMarkError(f"untrusted mark for {ticker}/{session}")
            self._require_timezone_aware(bar.fetched_at, "bar.fetched_at")
            validated[ticker] = bar
        try:
            validate_required_bars(
                {(ticker, session): bar for ticker, bar in validated.items()},
                set(required_tickers),
                session,
                valuation_at,
            )
        except BarValidationError as error:
            raise MissingMarkError(str(error)) from error
        return validated

    def _insert_mark(self, ticker: str, bar: MarketBar) -> None:
        values = (
            stable_id("mark", self.cohort_id, ticker, bar.session),
            self.cohort_id,
            ticker,
            self._encode(bar.session),
            self._encode(bar.close),
            bar.source,
            self._encode(bar.fetched_at),
            0,
        )
        existing = self._connection.execute(
            "SELECT * FROM marks WHERE mark_id = ?", (values[0],)
        ).fetchone()
        columns = (
            "mark_id",
            "cohort_id",
            "ticker",
            "session",
            "close",
            "source",
            "observed_at",
            "adjusted",
        )
        if existing is not None:
            if any(existing[column] != value for column, value in zip(columns, values)):
                raise LedgerConflictError(f"conflicting marks identity {values[0]}")
            return
        self._connection.execute(
            "INSERT INTO marks VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values
        )

    def _build_snapshot(
        self,
        session: date,
        epoch_id: str,
        valuation_at: datetime,
        open_lots: list[sqlite3.Row],
        marks_by_ticker: dict[str, MarketBar],
    ) -> AccountSnapshot:
        summary = self._accounting_summary()
        long_market_value = Decimal("0")
        short_liability = Decimal("0")
        unrealized_pnl = Decimal("0")
        margin_used = Decimal("0")
        for lot in open_lots:
            quantity = Decimal(lot["open_qty"])
            entry = _decimal(lot["entry_price"])
            close = marks_by_ticker[lot["ticker"]].close
            if lot["direction"] == "long":
                long_market_value += quantity * close
                unrealized_pnl += (close - entry) * quantity
            else:
                short_liability += quantity * close
                unrealized_pnl += (entry - close) * quantity
                margin_used += _decimal(lot["margin_reserved"])
        net_equity = summary["cash"] + long_market_value - short_liability
        gross_exposure = long_market_value + short_liability
        net_exposure = long_market_value - short_liability
        slippage_cost = summary["slippage_cost"]
        commission_cost = summary["commission_cost"]
        other_fees = summary["other_fees"]
        borrow_cost = summary["borrow_cost"]
        financing_cost = summary["financing_cost"]
        dividend_cash = summary["dividend_cash"]
        cumulative_costs = (
            slippage_cost + commission_cost + other_fees + borrow_cost + financing_cost
        )
        gross_equity = net_equity + cumulative_costs
        assert net_equity == summary["cash"] + long_market_value - short_liability
        assert gross_equity - cumulative_costs == net_equity
        return AccountSnapshot(
            stable_id("snapshot", self.cohort_id, epoch_id, session),
            self.cohort_id,
            epoch_id,
            session,
            valuation_at,
            summary["cash"],
            long_market_value,
            short_liability,
            gross_exposure,
            net_exposure,
            margin_used,
            summary["cash"] - margin_used,
            summary["realized_pnl"],
            unrealized_pnl,
            gross_equity,
            slippage_cost,
            commission_cost,
            other_fees,
            borrow_cost,
            financing_cost,
            dividend_cash,
            net_equity,
            max(summary["high_water_mark"], net_equity),
            True,
            "",
        )

    def _accounting_summary(self) -> dict[str, Decimal]:
        row = self._connection.execute(
            "SELECT * FROM accounting_state WHERE cohort_id = ?", (self.cohort_id,)
        ).fetchone()
        if row is None:
            raise LedgerConflictError(
                f"missing accounting summary for {self.cohort_id}"
            )
        return {
            column: _decimal(row[column])
            for column in (
                "cash",
                "realized_pnl",
                "slippage_cost",
                "commission_cost",
                "other_fees",
                "borrow_cost",
                "financing_cost",
                "dividend_cash",
                "high_water_mark",
            )
        }

    def _update_accounting_summary(
        self,
        *,
        cash_delta: Decimal = Decimal("0"),
        realized_pnl_delta: Decimal = Decimal("0"),
        slippage_cost_delta: Decimal = Decimal("0"),
        commission_cost_delta: Decimal = Decimal("0"),
        other_fees_delta: Decimal = Decimal("0"),
        borrow_cost_delta: Decimal = Decimal("0"),
        financing_cost_delta: Decimal = Decimal("0"),
        dividend_cash_delta: Decimal = Decimal("0"),
        high_water_mark: Decimal | None = None,
    ) -> None:
        """Apply one exact-Decimal summary mutation inside the caller transaction.

        Task 5 accrual/dividend writers extend this hook with their audit-row
        insert and the matching cost/cash delta in the same transaction.
        """
        deltas = {
            "cash": cash_delta,
            "realized_pnl": realized_pnl_delta,
            "slippage_cost": slippage_cost_delta,
            "commission_cost": commission_cost_delta,
            "other_fees": other_fees_delta,
            "borrow_cost": borrow_cost_delta,
            "financing_cost": financing_cost_delta,
            "dividend_cash": dividend_cash_delta,
        }
        if any(not isinstance(value, Decimal) for value in deltas.values()):
            raise TypeError("accounting summary deltas must be Decimal")
        if high_water_mark is not None and not isinstance(high_water_mark, Decimal):
            raise TypeError("high_water_mark must be Decimal")
        summary = self._accounting_summary()
        next_high_water_mark = max(
            summary["high_water_mark"],
            high_water_mark
            if high_water_mark is not None
            else summary["high_water_mark"],
        )
        self._connection.execute(
            """UPDATE accounting_state SET cash = ?, realized_pnl = ?,
               slippage_cost = ?, commission_cost = ?, other_fees = ?,
               borrow_cost = ?, financing_cost = ?, dividend_cash = ?,
               high_water_mark = ? WHERE cohort_id = ?""",
            (
                self._encode(summary["cash"] + deltas["cash"]),
                self._encode(summary["realized_pnl"] + deltas["realized_pnl"]),
                self._encode(summary["slippage_cost"] + deltas["slippage_cost"]),
                self._encode(summary["commission_cost"] + deltas["commission_cost"]),
                self._encode(summary["other_fees"] + deltas["other_fees"]),
                self._encode(summary["borrow_cost"] + deltas["borrow_cost"]),
                self._encode(summary["financing_cost"] + deltas["financing_cost"]),
                self._encode(summary["dividend_cash"] + deltas["dividend_cash"]),
                self._encode(next_high_water_mark),
                self.cohort_id,
            ),
        )

    @staticmethod
    def _require_timezone_aware(value: datetime, label: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise MissingMarkError(f"{label} must be timezone-aware")

    @classmethod
    def _fill_values(cls, fill: Fill) -> tuple[object, ...]:
        return (
            fill.fill_id,
            fill.intent_id,
            fill.side,
            cls._encode(fill.session),
            cls._encode(fill.effective_at),
            cls._encode(fill.processed_at),
            cls._encode(fill.reference_price),
            cls._encode(fill.fill_price),
            fill.quantity,
            cls._encode(fill.slippage),
            cls._encode(fill.commission),
            cls._encode(fill.other_fees),
        )

    def _require_identical_fill(self, row: sqlite3.Row, fill: Fill) -> None:
        columns = (
            "fill_id",
            "intent_id",
            "side",
            "session",
            "effective_at",
            "processed_at",
            "reference_price",
            "fill_price",
            "quantity",
            "slippage",
            "commission",
            "other_fees",
        )
        if any(
            row[column] != value
            for column, value in zip(columns, self._fill_values(fill))
        ):
            raise LedgerConflictError(f"conflicting fills identity {fill.fill_id}")

    def _require_identical_snapshot(
        self, row: sqlite3.Row, snapshot: AccountSnapshot
    ) -> None:
        columns = (
            "snapshot_id",
            "cohort_id",
            "epoch_id",
            "session",
            "valuation_at",
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
            "valid",
            "invalid_reason",
        )
        if any(
            row[column] != value
            for column, value in zip(columns, self._snapshot_values(snapshot))
        ):
            raise LedgerConflictError(
                f"conflicting account_snapshots identity {snapshot.snapshot_id}"
            )

    @classmethod
    def _snapshot_values(cls, snapshot: AccountSnapshot) -> tuple[object, ...]:
        return (
            snapshot.snapshot_id,
            snapshot.cohort_id,
            snapshot.epoch_id,
            cls._encode(snapshot.session),
            cls._encode(snapshot.valuation_at),
            cls._encode(snapshot.cash),
            cls._encode(snapshot.long_market_value),
            cls._encode(snapshot.short_liability),
            cls._encode(snapshot.gross_exposure),
            cls._encode(snapshot.net_exposure),
            cls._encode(snapshot.margin_used),
            cls._encode(snapshot.buying_power),
            cls._encode(snapshot.realized_pnl),
            cls._encode(snapshot.unrealized_pnl),
            cls._encode(snapshot.gross_equity),
            cls._encode(snapshot.slippage_cost),
            cls._encode(snapshot.commission_cost),
            cls._encode(snapshot.other_fees),
            cls._encode(snapshot.borrow_cost),
            cls._encode(snapshot.financing_cost),
            cls._encode(snapshot.dividend_cash),
            cls._encode(snapshot.net_equity),
            cls._encode(snapshot.high_water_mark),
            cls._encode(snapshot.valid),
            snapshot.invalid_reason,
        )

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
