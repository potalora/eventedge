"""Transactional, per-cohort SQLite persistence for the paper ledger.

All economic mutations in this module are durable SQLite transactions.  The
ledger deliberately derives instruments from persisted signal provenance: an
order or fill never carries an independently asserted ticker.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator, Mapping

from tradingagents.strategies.execution import (
    AccountSnapshot,
    AccountState,
    BenchmarkObservation,
    CorporateAction,
    Fill,
    LedgerEvent,
    MarketBar,
    OrderIntent,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.execution.cost_model import (
    PaperCostModel,
    quantize_cash,
    validate_new_short_borrow_rate,
)
from tradingagents.strategies.execution.price_source import (
    BarValidationError,
    validate_required_bars,
)
from tradingagents.strategies.orchestration.trading_calendar import session_close


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
    """CREATE TABLE IF NOT EXISTS intent_action_adjustments (
        adjustment_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL REFERENCES corporate_actions(action_id),
        intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        adjustment_sequence INTEGER NOT NULL,
        original_qty INTEGER NOT NULL, adjusted_qty INTEGER NOT NULL,
        original_stop_price TEXT, adjusted_stop_price TEXT, applied_at TEXT NOT NULL,
        UNIQUE(action_id, intent_id)
    )""",
    """CREATE TABLE IF NOT EXISTS session_invalidations (
        invalidation_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, session TEXT NOT NULL,
        reason TEXT NOT NULL, invalidated_at TEXT NOT NULL,
        UNIQUE(cohort_id, session, reason)
    )""",
    """CREATE TABLE IF NOT EXISTS ticker_quarantines (
        quarantine_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, ticker TEXT NOT NULL,
        reason TEXT NOT NULL, quarantined_at TEXT NOT NULL,
        UNIQUE(cohort_id, ticker, reason)
    )""",
    """CREATE TABLE IF NOT EXISTS corporate_action_conflicts (
        conflict_id TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, session TEXT NOT NULL,
        ticker TEXT NOT NULL, action_id TEXT NOT NULL, content_hash TEXT NOT NULL,
        attempted_payload TEXT NOT NULL, detected_at TEXT NOT NULL,
        UNIQUE(cohort_id, action_id, content_hash)
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
    """CREATE INDEX IF NOT EXISTS ix_lots_cohort_ticker_open
        ON lots(cohort_id, ticker, opened_session, lot_id) WHERE open_qty > 0""",
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

    def __init__(
        self,
        path: Path,
        cohort_id: str,
        initial_cash: Decimal,
        *,
        paper_ledger_config: Mapping[str, object] | None = None,
        short_selling_config: Mapping[str, object] | None = None,
    ) -> None:
        if not isinstance(initial_cash, Decimal):
            raise TypeError("initial_cash must be Decimal")
        self.path = Path(path)
        self.cohort_id = cohort_id
        self._cost_model = PaperCostModel(paper_ledger_config)
        short_values = short_selling_config or {}
        self._borrow_cost_reject_above = self._configured_decimal(
            short_values, "borrow_cost_reject_above", "0.05"
        )
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
            columns = {
                row["name"]
                for row in self._connection.execute(
                    "PRAGMA table_info(intent_action_adjustments)"
                )
            }
            if "adjustment_sequence" not in columns:
                self._connection.execute(
                    "ALTER TABLE intent_action_adjustments "
                    "ADD COLUMN adjustment_sequence INTEGER NOT NULL DEFAULT 0"
                )
            self._backfill_adjustment_sequences()
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_intent_adjustment_sequence "
                "ON intent_action_adjustments(intent_id, adjustment_sequence)"
            )
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

    def _backfill_adjustment_sequences(self) -> None:
        """Recover each legacy intent's only causal split chain or fail closed."""
        intents = self._connection.execute(
            "SELECT DISTINCT intent_id FROM intent_action_adjustments ORDER BY intent_id"
        ).fetchall()
        for item in intents:
            intent_id = item["intent_id"]
            rows = self._connection.execute(
                "SELECT * FROM intent_action_adjustments WHERE intent_id = ?",
                (intent_id,),
            ).fetchall()
            sequence = [int(row["adjustment_sequence"]) for row in rows]
            if sequence and min(sequence) >= 1 and len(set(sequence)) == len(rows):
                continue

            def endpoint(row: sqlite3.Row, prefix: str) -> tuple[int, str | None]:
                return int(row[f"{prefix}_qty"]), row[f"{prefix}_stop_price"]

            outgoing: dict[tuple[int, str | None], list[sqlite3.Row]] = {}
            targets: set[tuple[int, str | None]] = set()
            for row in rows:
                outgoing.setdefault(endpoint(row, "original"), []).append(row)
                targets.add(endpoint(row, "adjusted"))
            roots = [state for state in outgoing if state not in targets]
            if len(roots) != 1 or any(len(edges) != 1 for edges in outgoing.values()):
                raise LedgerConflictError(
                    f"intent adjustment migration ambiguous chain for {intent_id}"
                )
            ordered: list[sqlite3.Row] = []
            visited: set[str] = set()
            state = roots[0]
            while state in outgoing:
                row = outgoing[state][0]
                if row["adjustment_id"] in visited:
                    raise LedgerConflictError(
                        f"intent adjustment migration cyclic chain for {intent_id}"
                    )
                ordered.append(row)
                visited.add(row["adjustment_id"])
                state = endpoint(row, "adjusted")
            if len(ordered) != len(rows):
                raise LedgerConflictError(
                    f"intent adjustment migration disconnected chain for {intent_id}"
                )
            for value, row in enumerate(ordered, 1):
                self._connection.execute(
                    "UPDATE intent_action_adjustments SET adjustment_sequence = ? "
                    "WHERE adjustment_id = ?",
                    (value, row["adjustment_id"]),
                )

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

    @staticmethod
    def _configured_decimal(
        values: Mapping[str, object], key: str, default: str
    ) -> Decimal:
        # PaperCostModel owns common Decimal validation; this threshold is a
        # short-selling policy input rather than a paper-ledger cost field.
        value = PaperCostModel._configured_decimal(values, key, default)
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
            existing_row = self._connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent.intent_id,)
            ).fetchone()
            if existing_row is not None and self._matches_split_adjusted_intent(
                intent, self._intent_from_row(existing_row)
            ):
                return
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
               WHERE cohort_id = ? AND status = 'pending' AND (
                   (price_rule = 'next_session_open' AND eligible_session = ?)
                   OR (price_rule = 'resting_stop' AND eligible_session <= ?)
               )
               ORDER BY created_at, intent_id""",
            (self.cohort_id, self._encode(session), self._encode(session)),
        ).fetchall()
        return [self._intent_from_row(row) for row in rows]

    def reject_intent(
        self, intent_id: str, occurred_at: datetime, reason: str
    ) -> OrderIntent:
        """Durably terminalize a pending intent after a current risk rejection."""
        self._require_timezone_aware(occurred_at, "occurred_at")
        if not reason:
            raise ValueError("intent rejection reason is required")
        with self.transaction():
            row = self._connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ? AND cohort_id = ?",
                (intent_id, self.cohort_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown order intent {intent_id}")
            if row["status"] == "rejected":
                transition = self._connection.execute(
                    """SELECT occurred_at, reason FROM order_status_transitions
                       WHERE intent_id = ? AND status = 'rejected'""",
                    (intent_id,),
                ).fetchone()
                if (
                    transition is None
                    or transition["occurred_at"] != self._encode(occurred_at)
                    or transition["reason"] != reason
                ):
                    raise LedgerConflictError(
                        f"conflicting rejection replay for intent {intent_id}"
                    )
                return self._intent_from_row(row)
            if row["status"] != "pending":
                raise ValueError(
                    f"cannot reject terminal intent {intent_id}/{row['status']}"
                )
            self._connection.execute(
                "UPDATE order_intents SET status = 'rejected' WHERE intent_id = ?",
                (intent_id,),
            )
            self._connection.execute(
                """INSERT INTO order_status_transitions
                   (transition_id, intent_id, status, occurred_at, reason)
                   VALUES (?, ?, 'rejected', ?, ?)""",
                (
                    stable_id("order_status", intent_id, "rejected", reason),
                    intent_id,
                    self._encode(occurred_at),
                    reason,
                ),
            )
            updated = self._connection.execute(
                "SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if updated is None:  # pragma: no cover - same transaction invariant.
                raise RuntimeError("rejected intent disappeared")
            return self._intent_from_row(updated)

    def intent(self, intent_id: str) -> OrderIntent | None:
        """Return one persisted intent, including its ordered signal provenance."""
        row = self._connection.execute(
            "SELECT * FROM order_intents WHERE intent_id = ? AND cohort_id = ?",
            (intent_id, self.cohort_id),
        ).fetchone()
        return self._intent_from_row(row) if row is not None else None

    def signals_for_intent(self, intent_id: str) -> tuple[SignalRecord, ...]:
        """Return the exact persisted signals linked to an intent."""
        rows = self._connection.execute(
            """SELECT s.* FROM intent_signals isg
               JOIN signals s ON s.signal_id = isg.signal_id
               JOIN order_intents i ON i.intent_id = isg.intent_id
               WHERE isg.intent_id = ? AND i.cohort_id = ?
               ORDER BY isg.signal_order, s.signal_id""",
            (intent_id, self.cohort_id),
        ).fetchall()
        return tuple(self._signal_from_row(row) for row in rows)

    def open_positions(self) -> list[dict[str, object]]:
        """Return a bounded aggregate view over authoritative open lots."""
        rows = self._connection.execute(
            """SELECT ticker, direction, open_qty, entry_price
               FROM lots
               WHERE cohort_id = ? AND open_qty > 0
               ORDER BY ticker, direction, opened_session, lot_id""",
            (self.cohort_id,),
        ).fetchall()
        grouped: dict[tuple[str, str], tuple[int, Decimal]] = {}
        for row in rows:
            key = (row["ticker"], row["direction"])
            quantity, basis = grouped.get(key, (0, Decimal("0")))
            open_qty = int(row["open_qty"])
            grouped[key] = (
                quantity + open_qty,
                basis + _decimal(row["entry_price"]) * open_qty,
            )
        return [
            {
                "ticker": ticker,
                "quantity": quantity,
                "avg_price": basis / quantity,
                "instrument_type": "stock",
                "side": direction,
            }
            for (ticker, direction), (quantity, basis) in grouped.items()
        ]

    def external_order_for_intent(self, intent_id: str) -> dict[str, object] | None:
        """Return the single durable broker order associated with an intent."""
        row = self._connection.execute(
            """SELECT eo.* FROM external_orders eo
               JOIN order_intents i ON i.intent_id = eo.intent_id
               WHERE eo.intent_id = ? AND i.cohort_id = ?
               ORDER BY eo.submitted_at DESC, eo.external_order_id
               LIMIT 1""",
            (intent_id, self.cohort_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def prepare_external_order(
        self,
        *,
        intent_id: str,
        broker: str,
        submitted_at: datetime,
    ) -> None:
        """Persist retry authority before making an external submit call."""
        self._require_timezone_aware(submitted_at, "submitted_at")
        with self.transaction():
            intent = self._connection.execute(
                "SELECT external_order_id FROM order_intents "
                "WHERE intent_id = ? AND cohort_id = ?",
                (intent_id, self.cohort_id),
            ).fetchone()
            if intent is None:
                raise ValueError(f"unknown order intent {intent_id}")
            existing = self.external_order_for_intent(intent_id)
            if existing is not None:
                if existing["broker"] != broker:
                    raise LedgerConflictError(
                        f"conflicting external broker for intent {intent_id}"
                    )
                return
            self._connection.execute(
                """INSERT INTO external_orders
                   (external_order_id, intent_id, broker, status, submitted_at,
                    reconciled_at, detail)
                   VALUES (?, ?, ?, 'pending', ?, NULL, ?)""",
                (
                    intent_id,
                    intent_id,
                    broker,
                    self._encode(submitted_at),
                    "prepared before external submit",
                ),
            )
            self._connection.execute(
                "UPDATE order_intents SET external_order_id = ? WHERE intent_id = ?",
                (intent_id, intent_id),
            )

    def record_external_order(
        self,
        *,
        intent_id: str,
        external_order_id: str,
        broker: str,
        status: str,
        submitted_at: datetime,
        reconciled_at: datetime | None,
        detail: str,
    ) -> None:
        """Persist initial submission or reconciliation for one broker order."""
        if not external_order_id:
            raise ValueError("external_order_id is required")
        self._require_timezone_aware(submitted_at, "submitted_at")
        if reconciled_at is not None:
            self._require_timezone_aware(reconciled_at, "reconciled_at")
            if reconciled_at < submitted_at:
                raise ValueError("reconciled_at precedes submitted_at")
        with self.transaction():
            intent = self._connection.execute(
                "SELECT external_order_id FROM order_intents "
                "WHERE intent_id = ? AND cohort_id = ?",
                (intent_id, self.cohort_id),
            ).fetchone()
            if intent is None:
                raise ValueError(f"unknown order intent {intent_id}")
            linked_id = intent["external_order_id"]
            linked_row = (
                self._connection.execute(
                    "SELECT * FROM external_orders WHERE external_order_id = ?",
                    (linked_id,),
                ).fetchone()
                if linked_id is not None
                else None
            )
            row = self._connection.execute(
                "SELECT * FROM external_orders WHERE external_order_id = ?",
                (external_order_id,),
            ).fetchone()
            values = (
                intent_id,
                broker,
                status,
                self._encode(submitted_at),
                self._encode(reconciled_at) if reconciled_at is not None else None,
                detail,
                external_order_id,
            )
            if (
                linked_row is not None
                and linked_id == intent_id
                and external_order_id != linked_id
                and row is None
            ):
                if linked_row["broker"] != broker:
                    raise LedgerConflictError(
                        f"conflicting external broker for intent {intent_id}"
                    )
                self._validate_external_status_transition(linked_row["status"], status)
                self._connection.execute(
                    """UPDATE external_orders
                       SET external_order_id = ?, status = ?, reconciled_at = ?,
                           detail = ?
                       WHERE external_order_id = ?""",
                    (
                        external_order_id,
                        status,
                        self._encode(reconciled_at)
                        if reconciled_at is not None
                        else None,
                        detail,
                        linked_id,
                    ),
                )
                self._connection.execute(
                    "UPDATE order_intents SET external_order_id = ? "
                    "WHERE intent_id = ?",
                    (external_order_id, intent_id),
                )
                return
            if linked_id is not None and linked_id != external_order_id:
                raise LedgerConflictError(
                    f"intent {intent_id} already has external order {linked_id}"
                )
            if row is None:
                self._connection.execute(
                    """INSERT INTO external_orders
                       (intent_id, broker, status, submitted_at, reconciled_at,
                        detail, external_order_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
                self._connection.execute(
                    "UPDATE order_intents SET external_order_id = ? "
                    "WHERE intent_id = ?",
                    (external_order_id, intent_id),
                )
                return
            if row["intent_id"] != intent_id or row["broker"] != broker:
                raise LedgerConflictError(
                    f"conflicting external order identity {external_order_id}"
                )
            self._validate_external_status_transition(row["status"], status)
            self._connection.execute(
                """UPDATE external_orders SET status = ?, reconciled_at = ?,
                          detail = ?
                   WHERE external_order_id = ?""",
                (
                    status,
                    self._encode(reconciled_at)
                    if reconciled_at is not None
                    else row["reconciled_at"],
                    detail,
                    external_order_id,
                ),
            )

    @staticmethod
    def _validate_external_status_transition(old: str, new: str) -> None:
        if old == new:
            return
        allowed = {
            "pending": {
                "accepted",
                "partially_filled",
                "filled",
                "rejected",
                "cancelled",
            },
            "accepted": {
                "partially_filled",
                "filled",
                "rejected",
                "cancelled",
            },
            "partially_filled": {"filled", "rejected", "cancelled"},
            "filled": set(),
            "rejected": set(),
            "cancelled": set(),
        }
        if old not in allowed or new not in allowed[old]:
            raise LedgerConflictError(
                f"external order status cannot regress from {old} to {new}"
            )

    def session_is_valid(self, session: date, ticker: str | None = None) -> bool:
        """Public fail-closed state query for policy/entry execution callers."""
        return self._session_invalid_reason(session, ticker) is None

    def assert_session_tradeable(
        self, session: date, ticker: str | None = None
    ) -> None:
        reason = self._session_invalid_reason(session, ticker)
        if reason is not None:
            target = f"/{ticker}" if ticker is not None else ""
            raise ValueError(f"session {session}{target} is invalid: {reason}")

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

    def accrue_borrow(
        self,
        session: date,
        close_marks: dict[str, MarketBar],
        rates: dict[str, Decimal | None],
        valuation_at: datetime,
    ) -> LedgerEvent:
        """Accrue ACT/365 borrow once per short ticker at that session close."""
        self._require_timezone_aware(valuation_at, "valuation_at")
        with self.transaction():
            lots = self._open_lots_for_direction("short")
            tickers = sorted({lot["ticker"] for lot in lots})
            marks = self._validate_accrual_marks(
                session, close_marks, tickers, valuation_at
            )
            events: list[LedgerEvent] = []
            for ticker in tickers:
                rate = rates.get(ticker)
                flagged = rate is None
                annual_rate = (
                    self._cost_model.existing_short_missing_borrow_rate
                    if flagged
                    else rate
                )
                if (
                    not isinstance(annual_rate, Decimal)
                    or not annual_rate.is_finite()
                    or annual_rate < 0
                ):
                    raise ValueError(f"invalid borrow rate for existing short {ticker}")
                quantity = sum(
                    (
                        Decimal(lot["open_qty"])
                        for lot in lots
                        if lot["ticker"] == ticker
                    ),
                    Decimal("0"),
                )
                amount = self._cost_model.borrow_charge(
                    quantity * marks[ticker].close, annual_rate
                )
                accrual_id = stable_id("borrow", self.cohort_id, session, ticker)
                event = LedgerEvent(
                    accrual_id,
                    session,
                    "borrow",
                    amount,
                    flagged,
                    "existing short borrow fallback rate"
                    if flagged
                    else "short borrow ACT/365",
                )
                existing = self._connection.execute(
                    "SELECT * FROM borrow_accruals WHERE accrual_id = ?", (accrual_id,)
                ).fetchone()
                expected = (
                    accrual_id,
                    None,
                    self._encode(session),
                    self._encode(amount),
                    self._encode(annual_rate),
                    self._encode(flagged),
                )
                if existing is not None:
                    columns = (
                        "accrual_id",
                        "lot_id",
                        "session",
                        "amount",
                        "annual_rate",
                        "flagged",
                    )
                    if any(
                        existing[column] != value
                        for column, value in zip(columns, expected)
                    ):
                        raise LedgerConflictError(
                            f"conflicting borrow accrual {accrual_id}"
                        )
                    events.append(event)
                    continue
                self._connection.execute(
                    "INSERT INTO borrow_accruals VALUES (?, ?, ?, ?, ?, ?)", expected
                )
                self._insert_named_cash_event(
                    stable_id("cash", self.cohort_id, "borrow", accrual_id),
                    session,
                    "borrow",
                    -amount,
                    marks[ticker].fetched_at,
                    f"borrow {ticker} {accrual_id}",
                )
                self._update_accounting_summary(
                    cash_delta=-amount, borrow_cost_delta=amount
                )
                events.append(event)
        if not events:
            return LedgerEvent(
                stable_id("borrow", self.cohort_id, session, "none"),
                session,
                "borrow",
                Decimal("0.0000"),
                False,
                "no open short lots",
            )
        if len(events) == 1:
            return events[0]
        return LedgerEvent(
            stable_id("borrow_batch", self.cohort_id, session),
            session,
            "borrow",
            sum((event.amount for event in events), Decimal("0.0000")),
            any(event.flagged for event in events),
            "per-ticker short borrow ACT/365",
        )

    def accrue_financing(
        self,
        session: date,
        annual_rate: Decimal,
        processed_at: datetime | None = None,
    ) -> LedgerEvent:
        """Accrue debit-cash financing once; positive idle cash has a zero yield."""
        if (
            not isinstance(annual_rate, Decimal)
            or not annual_rate.is_finite()
            or annual_rate < 0
        ):
            raise ValueError("annual financing rate must be a non-negative Decimal")
        effective_at = processed_at or self._session_close_timestamp(session)
        self._require_timezone_aware(effective_at, "processed_at")
        accrual_id = stable_id("financing", self.cohort_id, session)
        with self.transaction():
            debit_balance = max(-self._accounting_summary()["cash"], Decimal("0"))
            amount = self._cost_model.financing_charge(debit_balance, annual_rate)
            event = LedgerEvent(
                accrual_id,
                session,
                "financing",
                amount,
                False,
                "debit financing ACT/365",
            )
            existing = self._connection.execute(
                "SELECT * FROM financing_accruals WHERE accrual_id = ?", (accrual_id,)
            ).fetchone()
            expected = (
                accrual_id,
                self._encode(session),
                self._encode(amount),
                self._encode(annual_rate),
                0,
            )
            if existing is not None:
                columns = ("accrual_id", "session", "amount", "annual_rate", "flagged")
                if any(
                    existing[column] != value
                    for column, value in zip(columns, expected)
                ):
                    raise LedgerConflictError(
                        f"conflicting financing accrual {accrual_id}"
                    )
                return event
            self._connection.execute(
                "INSERT INTO financing_accruals VALUES (?, ?, ?, ?, ?)", expected
            )
            self._insert_named_cash_event(
                stable_id("cash", self.cohort_id, "financing", accrual_id),
                session,
                "financing",
                -amount,
                effective_at,
                f"financing {accrual_id}",
            )
            self._update_accounting_summary(
                cash_delta=-amount, financing_cost_delta=amount
            )
            return event

    def apply_corporate_actions(
        self,
        session: date,
        actions: list[CorporateAction],
        processed_at: datetime | None = None,
    ) -> list[LedgerEvent]:
        """Apply verified actions once, or durably quarantine uncertain inputs."""
        processed = processed_at or max(
            (action.fetched_at for action in actions),
            default=self._session_close_timestamp(session),
        )
        self._require_timezone_aware(processed, "processed_at")
        if any(processed < action.fetched_at for action in actions):
            raise ValueError("processed_at precedes corporate action observation")
        events: list[LedgerEvent] = []
        with self.transaction():
            for action in sorted(actions, key=lambda item: item.action_id):
                existing = self._connection.execute(
                    "SELECT * FROM corporate_actions WHERE action_id = ?",
                    (action.action_id,),
                ).fetchone()
                if existing is not None:
                    if not self._same_corporate_action(existing, action):
                        self._record_corporate_action_conflict(
                            session, action, processed
                        )
                        events.append(
                            self._quarantine_action(
                                session,
                                action,
                                "conflicting corporate action identity",
                                processed,
                            )
                        )
                    continue
                self._connection.execute(
                    "INSERT INTO corporate_actions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    self._corporate_action_values(action),
                )
                if action.session != session:
                    events.append(
                        self._quarantine_action(
                            session,
                            action,
                            "corporate action session mismatch",
                            processed,
                        )
                    )
                elif not action.verified:
                    events.append(
                        self._quarantine_action(
                            session, action, "unverified corporate action", processed
                        )
                    )
                elif action.action_type == "split":
                    invalid_reason = self._invalid_split_reason(action)
                    if invalid_reason is not None:
                        events.append(
                            self._quarantine_action(
                                session, action, invalid_reason, processed
                            )
                        )
                    else:
                        events.extend(self._apply_split(action, processed))
                elif action.action_type == "cash_dividend":
                    if (
                        action.cash_per_share is None
                        or not action.cash_per_share.is_finite()
                        or action.cash_per_share < 0
                    ):
                        events.append(
                            self._quarantine_action(
                                session, action, "invalid cash dividend", processed
                            )
                        )
                    else:
                        events.extend(self._apply_dividend(action, processed))
                else:  # defensive against malformed direct construction
                    events.append(
                        self._quarantine_action(
                            session, action, "unsupported corporate action", processed
                        )
                    )
        return events

    def invalidate_session(self, session: date, reason: str) -> None:
        with self.transaction():
            self._invalidate_session(session, reason, self._session_timestamp(session))

    def quarantine_ticker(self, ticker: str, reason: str) -> None:
        with self.transaction():
            self._quarantine_ticker(
                ticker, reason, self._session_timestamp(date(1970, 1, 1))
            )

    def apply_fill(
        self,
        intent: OrderIntent,
        fill: Fill,
        *,
        borrow_rate: Decimal | None = None,
    ) -> AccountState:
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
            if (
                intent != stored_intent
                and not (
                    existing_fill is not None
                    and self._same_intent_except_status(intent, stored_intent)
                )
                and not self._matches_split_adjusted_intent(intent, stored_intent)
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
                self.assert_session_tradeable(fill.session, ticker)
                if stored_intent.status != "pending":
                    raise ValueError(
                        f"order intent {stored_intent.intent_id} is not pending"
                    )
                if stored_intent.side == "short":
                    validate_new_short_borrow_rate(
                        borrow_rate, self._borrow_cost_reject_above
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
            invalid_reason = self._session_invalid_reason(session)
            if invalid_reason is None:
                for ticker in sorted({lot["ticker"] for lot in open_lots}):
                    ticker_reason = self._session_invalid_reason(session, ticker)
                    if ticker_reason is not None:
                        invalid_reason = f"quarantined {ticker}: {ticker_reason}"
                        break
            snapshot = self._build_snapshot(
                session,
                epoch_id,
                valuation_at,
                open_lots,
                marks_by_ticker,
                valid=invalid_reason is None,
                invalid_reason=invalid_reason or "",
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
                if snapshot.valid:
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
        if (
            intent.price_rule == "next_session_open"
            and fill.session != intent.eligible_session
        ):
            raise ValueError("fill session does not match eligible session")
        if (
            intent.price_rule == "resting_stop"
            and fill.session < intent.eligible_session
        ):
            raise ValueError("resting stop fill session precedes eligible session")
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

    def _matches_split_adjusted_intent(
        self, supplied: OrderIntent, stored: OrderIntent
    ) -> bool:
        """Accept an idempotent replay of a pre-split exit intent, not a rewrite.

        Intent IDs intentionally remain tied to their pre-split signal/order
        payload.  The immutable adjustment rows provide the deterministic bridge
        to the current executable quantity and stop; a different replay still
        fails closed.
        """
        if (
            supplied.intent_id,
            supplied.signal_ids,
            supplied.cohort_id,
            supplied.side,
            supplied.created_at,
            supplied.eligible_session,
            supplied.price_rule,
            supplied.external_order_id,
        ) != (
            stored.intent_id,
            stored.signal_ids,
            stored.cohort_id,
            stored.side,
            stored.created_at,
            stored.eligible_session,
            stored.price_rule,
            stored.external_order_id,
        ):
            return False
        rows = self._connection.execute(
            """SELECT * FROM intent_action_adjustments WHERE intent_id = ?
               ORDER BY adjustment_sequence""",
            (stored.intent_id,),
        ).fetchall()
        if not rows:
            return False
        quantity = supplied.requested_qty
        stop = supplied.stop_price
        for row in rows:
            if quantity != int(row["original_qty"]):
                return False
            old_stop = (
                _decimal(row["original_stop_price"])
                if row["original_stop_price"] is not None
                else None
            )
            if stop != old_stop:
                return False
            quantity = int(row["adjusted_qty"])
            stop = (
                _decimal(row["adjusted_stop_price"])
                if row["adjusted_stop_price"] is not None
                else None
            )
        return quantity == stored.requested_qty and stop == stored.stop_price

    def _next_adjustment_sequence(self, intent_id: str) -> int:
        row = self._connection.execute(
            """SELECT COALESCE(MAX(adjustment_sequence), 0) AS latest
               FROM intent_action_adjustments WHERE intent_id = ?""",
            (intent_id,),
        ).fetchone()
        return int(row["latest"]) + 1

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
            fill.fill_price * fill.quantity * self._cost_model.margin_requirement
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
        self._insert_named_cash_event(
            stable_id("cash", self.cohort_id, "fill", fill.fill_id),
            fill.session,
            "fill",
            amount,
            fill.effective_at,
            f"fill {fill.fill_id}",
        )

    def _insert_named_cash_event(
        self,
        cash_event_id: str,
        session: date,
        event_type: str,
        amount: Decimal,
        effective_at: datetime,
        detail: str,
    ) -> None:
        """Write a caller-owned immutable cash row inside its active transaction."""
        self._connection.execute(
            "INSERT INTO cash_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                cash_event_id,
                self.cohort_id,
                self._encode(session),
                event_type,
                self._encode(amount),
                self._encode(effective_at),
                detail,
            ),
        )

    def _open_lots_for_direction(self, direction: str) -> list[sqlite3.Row]:
        if direction not in {"long", "short"}:
            raise ValueError("unsupported lot direction")
        return self._connection.execute(
            """SELECT * FROM lots WHERE cohort_id = ? AND direction = ? AND open_qty > 0
               ORDER BY ticker, opened_session, lot_id""",
            (self.cohort_id, direction),
        ).fetchall()

    def _validate_accrual_marks(
        self,
        session: date,
        close_marks: dict[str, MarketBar],
        tickers: list[str],
        valuation_at: datetime,
    ) -> dict[str, MarketBar]:
        if not tickers:
            return {}
        validated: dict[str, MarketBar] = {}
        for ticker in tickers:
            bar = close_marks.get(ticker)
            if bar is None or bar.ticker != ticker or bar.session != session:
                raise MissingMarkError(f"missing mark for {ticker}/{session}")
            self._require_timezone_aware(bar.fetched_at, "bar.fetched_at")
            validated[ticker] = bar
        try:
            validate_required_bars(
                {(ticker, session): bar for ticker, bar in validated.items()},
                set(tickers),
                session,
                valuation_at,
            )
        except BarValidationError as error:
            raise MissingMarkError(str(error)) from error
        return validated

    def _corporate_action_values(self, action: CorporateAction) -> tuple[object, ...]:
        return (
            action.action_id,
            action.ticker,
            self._encode(action.session),
            action.action_type,
            self._encode(action.ratio) if action.ratio is not None else None,
            self._encode(action.cash_per_share)
            if action.cash_per_share is not None
            else None,
            action.source,
            self._encode(action.fetched_at),
            self._encode(action.verified),
        )

    def _same_corporate_action(self, row: sqlite3.Row, action: CorporateAction) -> bool:
        columns = (
            "action_id",
            "ticker",
            "session",
            "action_type",
            "ratio",
            "cash_per_share",
            "source",
            "fetched_at",
            "verified",
        )
        return all(
            row[column] == value
            for column, value in zip(columns, self._corporate_action_values(action))
        )

    def _invalid_split_reason(self, action: CorporateAction) -> str | None:
        if action.ratio is None or not action.ratio.is_finite() or action.ratio <= 0:
            return "invalid split ratio"
        lots = self._connection.execute(
            "SELECT * FROM lots WHERE cohort_id = ? AND ticker = ? AND open_qty > 0",
            (self.cohort_id, action.ticker),
        ).fetchall()
        pending = self._pending_exit_rows_for_ticker(action.ticker)
        for row in [*lots, *pending]:
            quantity = int(
                row["open_qty"] if "open_qty" in row.keys() else row["requested_qty"]
            )
            if (
                Decimal(quantity) * action.ratio
                != (Decimal(quantity) * action.ratio).to_integral_value()
            ):
                return "split produces fractional share quantity"
        return None

    def _apply_split(
        self, action: CorporateAction, processed_at: datetime
    ) -> list[LedgerEvent]:
        assert action.ratio is not None
        lots = self._connection.execute(
            """SELECT * FROM lots WHERE cohort_id = ? AND ticker = ? AND open_qty > 0
               ORDER BY opened_session, lot_id""",
            (self.cohort_id, action.ticker),
        ).fetchall()
        for lot in lots:
            original_qty = int(lot["original_qty"])
            open_qty = int(lot["open_qty"])
            self._connection.execute(
                """UPDATE lots SET original_qty = ?, open_qty = ?, entry_price = ?,
                   margin_reserved = ? WHERE lot_id = ?""",
                (
                    int(Decimal(original_qty) * action.ratio),
                    int(Decimal(open_qty) * action.ratio),
                    self._encode(_decimal(lot["entry_price"]) / action.ratio),
                    # Margin is total reserved margin, so preserving economic
                    # exposure requires retaining (and explicitly rewriting) it.
                    self._encode(_decimal(lot["margin_reserved"])),
                    lot["lot_id"],
                ),
            )
            self._connection.execute(
                "INSERT INTO lot_action_applications VALUES (?, ?, ?, ?, ?)",
                (
                    stable_id("lot_action", action.action_id, lot["lot_id"]),
                    action.action_id,
                    lot["lot_id"],
                    self._encode(processed_at),
                    f"split ratio {format(action.ratio, 'f')}",
                ),
            )
        for row in self._pending_exit_rows_for_ticker(action.ticker):
            old_qty = int(row["requested_qty"])
            new_qty = int(Decimal(old_qty) * action.ratio)
            old_stop = (
                _decimal(row["stop_price"]) if row["stop_price"] is not None else None
            )
            new_stop = old_stop / action.ratio if old_stop is not None else None
            self._connection.execute(
                "UPDATE order_intents SET requested_qty = ?, stop_price = ? WHERE intent_id = ?",
                (
                    new_qty,
                    self._encode(new_stop) if new_stop is not None else None,
                    row["intent_id"],
                ),
            )
            self._connection.execute(
                "INSERT INTO intent_action_adjustments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stable_id("intent_action", action.action_id, row["intent_id"]),
                    action.action_id,
                    row["intent_id"],
                    self._next_adjustment_sequence(row["intent_id"]),
                    old_qty,
                    new_qty,
                    self._encode(old_stop) if old_stop is not None else None,
                    self._encode(new_stop) if new_stop is not None else None,
                    self._encode(processed_at),
                ),
            )
        return [
            LedgerEvent(
                stable_id("split", self.cohort_id, action.action_id),
                action.session,
                "split",
                Decimal("0"),
                False,
                f"applied split {action.action_id}",
            )
        ]

    def _apply_dividend(
        self, action: CorporateAction, processed_at: datetime
    ) -> list[LedgerEvent]:
        assert action.cash_per_share is not None
        lots = self._connection.execute(
            """SELECT * FROM lots WHERE cohort_id = ? AND ticker = ? AND open_qty > 0
               ORDER BY direction, opened_session, lot_id""",
            (self.cohort_id, action.ticker),
        ).fetchall()
        events: list[LedgerEvent] = []
        for lot in lots:
            gross = quantize_cash(Decimal(lot["open_qty"]) * action.cash_per_share)
            amount = gross if lot["direction"] == "long" else -gross
            event_id = stable_id("dividend", action.action_id, lot["lot_id"])
            self._connection.execute(
                "INSERT INTO dividend_events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    action.action_id,
                    lot["lot_id"],
                    self._encode(action.session),
                    self._encode(amount),
                    lot["direction"],
                ),
            )
            self._insert_named_cash_event(
                stable_id("cash", self.cohort_id, "dividend", event_id),
                action.session,
                "dividend",
                amount,
                processed_at,
                f"dividend {action.action_id} {lot['lot_id']}",
            )
            self._update_accounting_summary(
                cash_delta=amount, dividend_cash_delta=amount
            )
            events.append(
                LedgerEvent(
                    event_id,
                    action.session,
                    "dividend",
                    amount,
                    False,
                    lot["direction"],
                )
            )
        return events

    def _pending_exit_rows_for_ticker(self, ticker: str) -> list[sqlite3.Row]:
        return self._connection.execute(
            """SELECT DISTINCT i.* FROM order_intents i
               JOIN intent_signals isg ON isg.intent_id = i.intent_id
               JOIN signals s ON s.signal_id = isg.signal_id
               WHERE i.cohort_id = ? AND i.status = 'pending'
                 AND i.side IN ('sell', 'cover') AND s.ticker = ?
                 AND NOT EXISTS (
                    SELECT 1 FROM intent_signals other_isg
                    JOIN signals other_s ON other_s.signal_id = other_isg.signal_id
                    WHERE other_isg.intent_id = i.intent_id AND other_s.ticker != ?
                 ) ORDER BY i.intent_id""",
            (self.cohort_id, ticker, ticker),
        ).fetchall()

    def _quarantine_action(
        self,
        session: date,
        action: CorporateAction,
        reason: str,
        processed_at: datetime,
    ) -> LedgerEvent:
        self._invalidate_session(session, reason, processed_at)
        self._quarantine_ticker(action.ticker, reason, processed_at)
        return LedgerEvent(
            stable_id(
                "action_invalid", self.cohort_id, session, action.action_id, reason
            ),
            session,
            "corporate_action_invalid",
            Decimal("0"),
            True,
            reason,
        )

    def _invalidate_session(
        self, session: date, reason: str, invalidated_at: datetime
    ) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO session_invalidations VALUES (?, ?, ?, ?, ?)",
            (
                stable_id("session_invalid", self.cohort_id, session, reason),
                self.cohort_id,
                self._encode(session),
                reason,
                self._encode(invalidated_at),
            ),
        )

    def _quarantine_ticker(
        self, ticker: str, reason: str, quarantined_at: datetime
    ) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO ticker_quarantines VALUES (?, ?, ?, ?, ?)",
            (
                stable_id("ticker_quarantine", self.cohort_id, ticker, reason),
                self.cohort_id,
                ticker,
                reason,
                self._encode(quarantined_at),
            ),
        )

    def _record_corporate_action_conflict(
        self, session: date, action: CorporateAction, detected_at: datetime
    ) -> None:
        values = self._corporate_action_values(action)
        payload = json.dumps(
            {
                "action_id": action.action_id,
                "ticker": action.ticker,
                "session": self._encode(action.session),
                "action_type": action.action_type,
                "ratio": self._encode(action.ratio),
                "cash_per_share": self._encode(action.cash_per_share),
                "source": action.source,
                "fetched_at": self._encode(action.fetched_at),
                "verified": action.verified,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        content_hash = stable_id("action_content", values)
        self._connection.execute(
            "INSERT OR IGNORE INTO corporate_action_conflicts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stable_id(
                    "action_conflict", self.cohort_id, action.action_id, content_hash
                ),
                self.cohort_id,
                self._encode(session),
                action.ticker,
                action.action_id,
                content_hash,
                payload,
                self._encode(detected_at),
            ),
        )

    def _session_invalid_reason(
        self, session: date, ticker: str | None = None
    ) -> str | None:
        row = self._connection.execute(
            """SELECT reason FROM session_invalidations
               WHERE cohort_id = ? AND session = ? ORDER BY invalidation_id LIMIT 1""",
            (self.cohort_id, self._encode(session)),
        ).fetchone()
        if row is not None:
            return row["reason"]
        if ticker is not None:
            row = self._connection.execute(
                """SELECT reason FROM ticker_quarantines
                   WHERE cohort_id = ? AND ticker = ? ORDER BY quarantine_id LIMIT 1""",
                (self.cohort_id, ticker),
            ).fetchone()
            if row is not None:
                return row["reason"]
        return None

    @staticmethod
    def _session_timestamp(session: date) -> datetime:
        return datetime.combine(session, time.min, tzinfo=timezone.utc)

    @staticmethod
    def _session_close_timestamp(session: date) -> datetime:
        return session_close(session)

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
        *,
        valid: bool = True,
        invalid_reason: str = "",
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
            valid,
            invalid_reason,
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
