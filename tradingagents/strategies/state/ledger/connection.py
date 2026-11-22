"""SQLite connection, schema initialization, and canonical codec helpers.

Wraps ``GenerationDB`` (WAL mode, ``PRAGMA busy_timeout=10000``,
``PRAGMA foreign_keys=ON``, ``PRAGMA synchronous=FULL``) and enforces
deterministic resource cleanup with ``contextlib.closing`` semantics.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import sqlite3
from typing import Iterator, Mapping, TypeVar
from urllib.parse import quote

from tradingagents.strategies.execution import stable_id
from tradingagents.strategies.orchestration.trading_calendar import session_close
from tradingagents.strategies.state.generation_db import (
    DEFAULT_BUSY_TIMEOUT_MS,
    SCHEMA_VERSION as GENERATION_SCHEMA_VERSION,
    GenerationDB,
)


SCHEMA_VERSION = GENERATION_SCHEMA_VERSION
_OPENING_AT = datetime(1970, 1, 1)
_T = TypeVar("_T")


class LedgerConflictError(ValueError):
    """A stable ledger identity was reused with different content."""


class MissingMarkError(ValueError):
    """An authoritative valuation was attempted without every required mark."""


@dataclass(frozen=True)
class TradeProjectionRecord:
    """Typed, read-only lot/fill view for compatibility projections."""

    trade_id: str
    signal_ids: tuple[str, ...]
    intent_id: str
    execution_id: str
    exit_fill_ids: tuple[str, ...]
    strategy: str
    strategies: tuple[str, ...]
    ticker: str
    direction: str
    entry_session: date
    entry_price: Decimal
    shares: int
    original_shares: int
    closed_shares: int
    open_shares: int
    status: str
    exit_session: date | None
    exit_price: Decimal | None
    realized_pnl: Decimal
    slippage_cost: Decimal
    commission_cost: Decimal
    other_fees: Decimal


# Auxiliary ledger-engine tables initialized alongside GenerationDB's 31 tables.
# Every cohort-scoped auxiliary table carries cohort_id so all 16 cohorts share
# one generation.sqlite3 cleanly without interference.
_AUXILIARY_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS exit_intent_lots (
        intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        lot_id TEXT NOT NULL REFERENCES lots(lot_id),
        quantity INTEGER NOT NULL,
        PRIMARY KEY(intent_id, lot_id),
        UNIQUE(lot_id, intent_id)
    )""",
    """CREATE TABLE IF NOT EXISTS order_status_transitions (
        transition_id TEXT PRIMARY KEY,
        intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        status TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        reason TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fill_costs (
        fill_cost_id TEXT PRIMARY KEY,
        fill_id TEXT NOT NULL REFERENCES fills(fill_id),
        cost_type TEXT NOT NULL,
        amount TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS fee_events (
        fee_event_id TEXT PRIMARY KEY,
        fill_id TEXT REFERENCES fills(fill_id),
        session TEXT NOT NULL,
        fee_type TEXT NOT NULL,
        amount TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS dividend_payment_observations (
        action_id TEXT PRIMARY KEY REFERENCES corporate_actions(action_id),
        payment_date TEXT NOT NULL,
        source TEXT NOT NULL,
        reference TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        accepted_session TEXT NOT NULL,
        accepted_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS dividend_payment_terms (
        action_id TEXT PRIMARY KEY REFERENCES corporate_actions(action_id),
        payment_date TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS lot_action_applications (
        application_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL REFERENCES corporate_actions(action_id),
        lot_id TEXT NOT NULL REFERENCES lots(lot_id),
        applied_at TEXT NOT NULL,
        detail TEXT NOT NULL,
        UNIQUE(action_id, lot_id)
    )""",
    """CREATE TABLE IF NOT EXISTS intent_action_adjustments (
        adjustment_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL REFERENCES corporate_actions(action_id),
        intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        adjustment_sequence INTEGER NOT NULL,
        original_qty INTEGER NOT NULL,
        adjusted_qty INTEGER NOT NULL,
        original_stop_price TEXT,
        adjusted_stop_price TEXT,
        applied_at TEXT NOT NULL,
        UNIQUE(action_id, intent_id)
    )""",
    """CREATE TABLE IF NOT EXISTS session_invalidations (
        invalidation_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        session TEXT NOT NULL,
        reason TEXT NOT NULL,
        invalidated_at TEXT NOT NULL,
        UNIQUE(cohort_id, session, reason)
    )""",
    """CREATE TABLE IF NOT EXISTS ticker_quarantines (
        quarantine_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        ticker TEXT NOT NULL,
        reason TEXT NOT NULL,
        quarantined_at TEXT NOT NULL,
        UNIQUE(cohort_id, ticker, reason)
    )""",
    """CREATE TABLE IF NOT EXISTS corporate_action_conflicts (
        conflict_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        session TEXT NOT NULL,
        ticker TEXT NOT NULL,
        action_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        attempted_payload TEXT NOT NULL,
        detected_at TEXT NOT NULL,
        UNIQUE(cohort_id, action_id, content_hash)
    )""",
    """CREATE TABLE IF NOT EXISTS corporate_action_batch_rejections (
        batch_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        session TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        errors_json TEXT NOT NULL,
        rejected_at TEXT NOT NULL,
        UNIQUE(cohort_id, session, payload_hash)
    )""",
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


def _encode(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bool):
        return int(value)
    return value


def _canonical_json_value(value: object) -> object:
    """Return the small deterministic JSON domain accepted by policy bindings."""
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_json_value(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_json_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ),
        )
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("policy payload decimals must be finite")
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("policy payload floats must be finite")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported policy payload value: {type(value).__name__}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        _canonical_json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _require_timezone_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MissingMarkError(f"{label} must be timezone-aware")


def _session_timestamp(session: date) -> datetime:
    return datetime.combine(session, time.min, tzinfo=timezone.utc)


def _session_close_timestamp(session: date) -> datetime:
    return session_close(session)


def _open_ledger_connection(
    path: Path,
    *,
    readonly: bool,
    busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    immutable: bool = False,
) -> sqlite3.Connection:
    """Open a configured SQLite connection with WAL + busy_timeout=10000."""
    if readonly:
        encoded = quote(str(path.resolve()), safe="/")
        suffix = "&immutable=1" if immutable else ""
        conn = sqlite3.connect(
            f"file:{encoded}?mode=ro{suffix}",
            uri=True,
            isolation_level=None,
            timeout=busy_timeout_ms / 1000.0,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(path),
            isolation_level=None,
            timeout=busy_timeout_ms / 1000.0,
        )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys=ON")
        if readonly:
            conn.execute("PRAGMA query_only=ON")
        else:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
    except BaseException:
        conn.close()
        raise
    return conn


class LedgerConnection:
    """Base connection & transaction management backed by ``GenerationDB``."""

    db: GenerationDB
    path: Path
    cohort_id: str
    _connection: sqlite3.Connection
    _read_only: bool

    def _init_connection(
        self,
        path_or_db: Path | str | GenerationDB,
        cohort_id: str,
        initial_cash: Decimal,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        if not isinstance(initial_cash, Decimal):
            raise TypeError("initial_cash must be Decimal")
        if not isinstance(cohort_id, str) or not cohort_id.strip():
            raise ValueError("cohort_id must be a nonempty string")
        if isinstance(path_or_db, GenerationDB):
            if path_or_db.readonly:
                raise PermissionError("cannot initialize writable ledger on readonly GenerationDB")
            self.db = path_or_db
            self.path = path_or_db.path
            self.cohort_id = cohort_id
            self._read_only = False
            self._connection = _open_ledger_connection(
                self.path, readonly=False, busy_timeout_ms=busy_timeout_ms
            )
        elif str(path_or_db) == ":memory:":
            from tradingagents.strategies.state.generation_db import DDL_STATEMENTS

            self.path = Path(":memory:")
            self.db = None  # type: ignore[assignment]
            self.cohort_id = cohort_id
            self._read_only = False
            conn = sqlite3.connect(":memory:", isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            conn.execute("PRAGMA foreign_keys=ON")
            for stmt in DDL_STATEMENTS:
                conn.execute(stmt)
            self._connection = conn
        else:
            self.path = Path(path_or_db)
            self.db = GenerationDB(self.path, busy_timeout_ms=busy_timeout_ms)
            self.cohort_id = cohort_id
            self._read_only = False
            self._connection = _open_ledger_connection(
                self.path, readonly=False, busy_timeout_ms=busy_timeout_ms
            )
        try:
            self._initialize_cohort(initial_cash)
        except BaseException:
            self.close()
            raise

    @classmethod
    def _open_readonly_connection(
        cls,
        path_or_db: Path | str | GenerationDB,
        *,
        cohort_id: str | None = None,
        immutable: bool = False,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> tuple[GenerationDB, Path, str, sqlite3.Connection]:
        if isinstance(path_or_db, GenerationDB):
            db = path_or_db
            path = db.path
            use_immutable = immutable or db.immutable
        else:
            path = Path(path_or_db)
            if not os.path.lexists(path):
                raise FileNotFoundError(path)
            try:
                db = GenerationDB.open_readonly(
                    path, busy_timeout_ms=busy_timeout_ms, immutable=immutable
                )
            except ValueError:
                db = None
            use_immutable = immutable
        conn = _open_ledger_connection(
            path,
            readonly=True,
            busy_timeout_ms=busy_timeout_ms,
            immutable=use_immutable,
        )
        try:
            resolved_cohort = cohort_id
            if resolved_cohort is None:
                cohort_rows = conn.execute(
                    "SELECT cohort_id FROM accounting_state ORDER BY cohort_id"
                ).fetchall()
                if len(cohort_rows) == 1:
                    resolved_cohort = str(cohort_rows[0]["cohort_id"])
                else:
                    meta_row = conn.execute(
                        "SELECT metadata_value FROM schema_metadata WHERE metadata_key = 'cohort_id'"
                    ).fetchone()
                    if meta_row is not None and len(cohort_rows) <= 1:
                        resolved_cohort = str(meta_row["metadata_value"])
                    elif not cohort_rows:
                        raise ValueError(f"not an initialized PortfolioLedger: {path}")
                    else:
                        raise ValueError(
                            f"cohort_id is required when opening multi-cohort database: {path}"
                        )
            cash_row = conn.execute(
                """SELECT amount FROM cash_events
                   WHERE cohort_id = ? AND event_type = 'opening'
                   ORDER BY cash_event_id LIMIT 1""",
                (resolved_cohort,),
            ).fetchone()
            if cash_row is None:
                raise ValueError(
                    f"ledger has no opening cash event for {resolved_cohort!r}: {path}"
                )
            Decimal(str(cash_row["amount"]))
        except BaseException:
            conn.close()
            raise
        return db, path, resolved_cohort, conn

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the connection for bounded administrative inspection/tests."""
        return self._connection

    def close(self) -> None:
        conn = getattr(self, "_connection", None)
        if conn is not None:
            with closing(conn):
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self._connection.in_transaction:
            yield self._connection
            return
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        else:
            if self._connection.in_transaction:
                self._connection.execute("COMMIT")

    def _initialize_cohort(self, initial_cash: Decimal) -> None:
        with self.transaction():
            for statement in _AUXILIARY_DDL:
                self._connection.execute(statement)
            snapshot_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(account_snapshots)")
            }
            if "dividend_receivable" not in snapshot_columns:
                self._connection.execute(
                    "ALTER TABLE account_snapshots ADD COLUMN dividend_receivable TEXT NOT NULL DEFAULT '0'"
                )
            opening_id = stable_id("cash", self.cohort_id, "opening")
            self._insert_idempotent(
                "cash_events",
                "cash_event_id",
                opening_id,
                {
                    "cash_event_id": opening_id,
                    "cohort_id": self.cohort_id,
                    "session": None,
                    "event_type": "opening",
                    "amount": initial_cash,
                    "effective_at": _OPENING_AT,
                    "detail": "initial cohort capital",
                },
            )
            self._initialize_accounting_state(initial_cash)

    def _initialize_accounting_state(self, initial_cash: Decimal) -> None:
        existing = self._connection.execute(
            "SELECT cohort_id FROM accounting_state WHERE cohort_id = ?",
            (self.cohort_id,),
        ).fetchone()
        if existing is not None:
            return
        cash_rows = self._connection.execute(
            "SELECT event_type, amount FROM cash_events WHERE cohort_id = ?",
            (self.cohort_id,),
        ).fetchall()
        cash = sum((_decimal(r["amount"]) for r in cash_rows), Decimal("0"))
        dividend_cash = sum(
            (_decimal(r["amount"]) for r in cash_rows if r["event_type"] == "dividend"),
            Decimal("0"),
        )
        realized_pnl = sum(
            (
                _decimal(r["realized_pnl"])
                for r in self._connection.execute(
                    "SELECT realized_pnl FROM lot_closures WHERE cohort_id = ?",
                    (self.cohort_id,),
                )
            ),
            Decimal("0"),
        )
        fill_rows = self._connection.execute(
            "SELECT slippage, commission, other_fees FROM fills WHERE cohort_id = ?",
            (self.cohort_id,),
        ).fetchall()
        slippage_cost = sum((_decimal(r["slippage"]) for r in fill_rows), Decimal("0"))
        commission_cost = sum((_decimal(r["commission"]) for r in fill_rows), Decimal("0"))
        other_fees = sum((_decimal(r["other_fees"]) for r in fill_rows), Decimal("0"))
        borrow_cost = sum(
            (
                _decimal(r["amount"])
                for r in self._connection.execute(
                    "SELECT amount FROM borrow_accruals WHERE cohort_id = ?",
                    (self.cohort_id,),
                )
            ),
            Decimal("0"),
        )
        financing_cost = sum(
            (
                _decimal(r["amount"])
                for r in self._connection.execute(
                    "SELECT amount FROM financing_accruals WHERE cohort_id = ?",
                    (self.cohort_id,),
                )
            ),
            Decimal("0"),
        )
        hwm_rows = self._connection.execute(
            "SELECT high_water_mark FROM account_snapshots WHERE cohort_id = ? AND valid = 1",
            (self.cohort_id,),
        ).fetchall()
        high_water_mark = max(
            (_decimal(r["high_water_mark"]) for r in hwm_rows),
            default=initial_cash,
        )
        self._connection.execute(
            "INSERT INTO accounting_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.cohort_id,
                _encode(cash),
                _encode(realized_pnl),
                _encode(slippage_cost),
                _encode(commission_cost),
                _encode(other_fees),
                _encode(borrow_cost),
                _encode(financing_cost),
                _encode(dividend_cash),
                _encode(max(high_water_mark, initial_cash)),
            ),
        )

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
        encoded = {key: _encode(value) for key, value in values.items()}
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
        return _encode(value)

    @staticmethod
    def _require_timezone_aware(value: datetime, label: str) -> None:
        _require_timezone_aware(value, label)

    @staticmethod
    def _session_timestamp(session: date) -> datetime:
        return _session_timestamp(session)

    @staticmethod
    def _session_close_timestamp(session: date) -> datetime:
        return _session_close_timestamp(session)
