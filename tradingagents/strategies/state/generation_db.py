"""Unified single-SQLite-per-generation database schema and accessor.

Replaces the 17-database-per-generation layout (16 per-cohort ``portfolio.db``
files + 1 ``metrics_v2.sqlite3``) with a single WAL-mode ``generation.sqlite3``
separating shared once-per-session tables from cohort-scoped tables indexed by
``cohort_id``.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping

from tradingagents.strategies.orchestration._io import open_sqlite, snapshot_sqlite


GENERATION_DB_FILENAME: str = "generation.sqlite3"
SCHEMA_VERSION: int = 2
SCHEMA_NAME: str = "eventedge_generation_db_v2"
DEFAULT_BUSY_TIMEOUT_MS: int = 10000

SHARED_TABLES: frozenset[str] = frozenset(
    {
        "schema_metadata",
        "metric_epochs",
        "signals",
        "signal_candidate_contexts",
        "marks",
        "corporate_actions",
        "dividend_events",
        "benchmark_observations",
        "strategy_health",
        "regime_snapshots",
        "outcomes",
        "bar_recoveries",
        "candidate_input_issues",
        "session_shared_seals",
    }
)

COHORT_TABLES: frozenset[str] = frozenset(
    {
        "accounting_state",
        "session_runs",
        "session_phases",
        "session_execution_contexts",
        "committee_decisions",
        "order_intents",
        "intent_signals",
        "fills",
        "lots",
        "lot_closures",
        "cash_events",
        "borrow_accruals",
        "financing_accruals",
        "dividend_receivables",
        "account_snapshots",
        "staging_runs",
        "policy_audit_records",
    }
)

ALL_TABLES: frozenset[str] = SHARED_TABLES | COHORT_TABLES

# Canonical DDL: shared tables first, then cohort-scoped tables in FK order,
# followed by explicit covering/lookup indexes.
DDL_STATEMENTS: tuple[str, ...] = (
    # ------------------------------------------------------------------
    # Shared tables (14) — written once per session across all cohorts
    # ------------------------------------------------------------------
    """CREATE TABLE IF NOT EXISTS schema_metadata (
        metadata_key TEXT PRIMARY KEY,
        metadata_value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS metric_epochs (
        epoch_id TEXT PRIMARY KEY,
        generation_id TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        status TEXT NOT NULL,
        start_session TEXT NOT NULL,
        end_session TEXT,
        payload_json TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS signals (
        signal_id TEXT PRIMARY KEY,
        epoch_id TEXT NOT NULL,
        policy_id TEXT NOT NULL,
        event_key TEXT NOT NULL,
        strategy TEXT NOT NULL,
        ticker TEXT NOT NULL,
        direction TEXT NOT NULL,
        event_at TEXT,
        observed_at TEXT NOT NULL,
        reference_session TEXT NOT NULL,
        reference_close TEXT NOT NULL,
        decision_at TEXT NOT NULL,
        evidence_hash TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS signal_candidate_contexts (
        signal_id TEXT PRIMARY KEY REFERENCES signals(signal_id),
        payload_json TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        captured_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS marks (
        mark_id TEXT PRIMARY KEY,
        ticker TEXT NOT NULL,
        session TEXT NOT NULL,
        open TEXT,
        high TEXT,
        low TEXT,
        close TEXT NOT NULL,
        source TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        adjusted INTEGER NOT NULL CHECK(adjusted IN (0, 1)),
        UNIQUE(ticker, session)
    )""",
    """CREATE TABLE IF NOT EXISTS corporate_actions (
        action_id TEXT PRIMARY KEY,
        ticker TEXT NOT NULL,
        session TEXT NOT NULL,
        action_type TEXT NOT NULL,
        ratio TEXT,
        cash_per_share TEXT,
        payment_date TEXT,
        source TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        verified INTEGER NOT NULL CHECK(verified IN (0, 1))
    )""",
    """CREATE TABLE IF NOT EXISTS dividend_events (
        dividend_event_id TEXT PRIMARY KEY,
        action_id TEXT UNIQUE REFERENCES corporate_actions(action_id),
        ticker TEXT NOT NULL,
        ex_session TEXT NOT NULL,
        payment_date TEXT,
        cash_per_share TEXT NOT NULL,
        source TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS benchmark_observations (
        observation_id TEXT PRIMARY KEY,
        epoch_id TEXT NOT NULL,
        session TEXT NOT NULL,
        symbol TEXT NOT NULL,
        close TEXT NOT NULL,
        return_basis TEXT NOT NULL,
        source TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        valid INTEGER NOT NULL CHECK(valid IN (0, 1)),
        invalid_reason TEXT NOT NULL DEFAULT '',
        UNIQUE(epoch_id, session, symbol)
    )""",
    """CREATE TABLE IF NOT EXISTS strategy_health (
        health_id TEXT PRIMARY KEY,
        epoch_id TEXT NOT NULL,
        session TEXT NOT NULL,
        strategy TEXT NOT NULL,
        status TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS regime_snapshots (
        session TEXT PRIMARY KEY,
        epoch_id TEXT NOT NULL,
        regime_label TEXT NOT NULL,
        vix_close TEXT,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        captured_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS outcomes (
        outcome_id TEXT PRIMARY KEY,
        epoch_id TEXT NOT NULL,
        signal_id TEXT,
        strategy TEXT NOT NULL,
        ticker TEXT NOT NULL,
        entry_session TEXT NOT NULL,
        exit_session TEXT NOT NULL,
        horizon_sessions INTEGER NOT NULL,
        payload_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS bar_recoveries (
        recovery_id TEXT PRIMARY KEY,
        recovery_kind TEXT NOT NULL CHECK(recovery_kind IN ('candidate', 'governed')),
        contract_version TEXT NOT NULL,
        evidence_digest TEXT NOT NULL,
        epoch_id TEXT NOT NULL,
        session TEXT NOT NULL,
        ticker TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(recovery_kind, epoch_id, session, ticker)
    )""",
    """CREATE TABLE IF NOT EXISTS candidate_input_issues (
        issue_id TEXT PRIMARY KEY,
        epoch_id TEXT NOT NULL,
        session TEXT NOT NULL,
        dependency_kind TEXT NOT NULL,
        ticker TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(epoch_id, session, dependency_kind, ticker)
    )""",
    """CREATE TABLE IF NOT EXISTS session_shared_seals (
        session TEXT PRIMARY KEY,
        epoch_id TEXT NOT NULL,
        generation_id TEXT NOT NULL,
        config_sha256 TEXT NOT NULL,
        source_digest TEXT NOT NULL,
        regime_digest TEXT NOT NULL,
        signals_digest TEXT NOT NULL,
        marks_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        sealed_at TEXT NOT NULL
    )""",
    # ------------------------------------------------------------------
    # Cohort-scoped tables (17) — partitioned & indexed by cohort_id
    # ------------------------------------------------------------------
    """CREATE TABLE IF NOT EXISTS accounting_state (
        cohort_id TEXT PRIMARY KEY,
        cash TEXT NOT NULL,
        realized_pnl TEXT NOT NULL,
        slippage_cost TEXT NOT NULL,
        commission_cost TEXT NOT NULL,
        other_fees TEXT NOT NULL,
        borrow_cost TEXT NOT NULL,
        financing_cost TEXT NOT NULL,
        dividend_cash TEXT NOT NULL,
        high_water_mark TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS session_runs (
        session_run_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        session TEXT NOT NULL,
        valid INTEGER NOT NULL CHECK(valid IN (0, 1)),
        invalid_reason TEXT NOT NULL DEFAULT '',
        started_at TEXT,
        completed_at TEXT,
        UNIQUE(cohort_id, session)
    )""",
    """CREATE TABLE IF NOT EXISTS session_phases (
        session_phase_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        session TEXT NOT NULL,
        phase TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        pre_state_digest TEXT,
        post_state_digest TEXT,
        UNIQUE(cohort_id, session, phase)
    )""",
    """CREATE TABLE IF NOT EXISTS session_execution_contexts (
        execution_context_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        session TEXT NOT NULL,
        epoch_id TEXT NOT NULL,
        input_digest TEXT NOT NULL,
        market_digest TEXT NOT NULL,
        config_digest TEXT NOT NULL,
        borrow_digest TEXT NOT NULL,
        required_tickers_json TEXT NOT NULL,
        economic_inputs_json TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        provenance_digest TEXT NOT NULL,
        bound_at TEXT NOT NULL,
        starting_state_digest TEXT NOT NULL DEFAULT '',
        borrow_inputs_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(cohort_id, session)
    )""",
    """CREATE TABLE IF NOT EXISTS committee_decisions (
        cohort_id TEXT NOT NULL,
        session TEXT NOT NULL,
        epoch_id TEXT NOT NULL,
        policy_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(cohort_id, session, epoch_id, policy_id)
    )""",
    """CREATE TABLE IF NOT EXISTS order_intents (
        intent_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        side TEXT NOT NULL,
        requested_qty INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        eligible_session TEXT NOT NULL,
        price_rule TEXT NOT NULL,
        status TEXT NOT NULL,
        stop_price TEXT,
        external_order_id TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS intent_signals (
        cohort_id TEXT NOT NULL,
        intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        signal_id TEXT NOT NULL REFERENCES signals(signal_id),
        signal_order INTEGER NOT NULL,
        PRIMARY KEY(intent_id, signal_id),
        UNIQUE(intent_id, signal_order)
    )""",
    """CREATE TABLE IF NOT EXISTS fills (
        fill_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        intent_id TEXT NOT NULL REFERENCES order_intents(intent_id),
        side TEXT NOT NULL,
        session TEXT NOT NULL,
        effective_at TEXT NOT NULL,
        processed_at TEXT NOT NULL,
        reference_price TEXT NOT NULL,
        fill_price TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        slippage TEXT NOT NULL,
        commission TEXT NOT NULL,
        other_fees TEXT NOT NULL,
        UNIQUE(intent_id, session)
    )""",
    """CREATE TABLE IF NOT EXISTS lots (
        lot_id TEXT PRIMARY KEY,
        fill_id TEXT NOT NULL REFERENCES fills(fill_id),
        cohort_id TEXT NOT NULL,
        ticker TEXT NOT NULL,
        direction TEXT NOT NULL,
        opened_session TEXT NOT NULL,
        entry_price TEXT NOT NULL,
        original_qty INTEGER NOT NULL,
        open_qty INTEGER NOT NULL,
        margin_reserved TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS lot_closures (
        closure_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        lot_id TEXT NOT NULL REFERENCES lots(lot_id),
        fill_id TEXT NOT NULL REFERENCES fills(fill_id),
        quantity INTEGER NOT NULL,
        realized_pnl TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cash_events (
        cash_event_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        session TEXT,
        event_type TEXT NOT NULL,
        amount TEXT NOT NULL,
        effective_at TEXT NOT NULL,
        detail TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS borrow_accruals (
        accrual_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        lot_id TEXT REFERENCES lots(lot_id),
        session TEXT NOT NULL,
        amount TEXT NOT NULL,
        annual_rate TEXT NOT NULL,
        flagged INTEGER NOT NULL CHECK(flagged IN (0, 1))
    )""",
    """CREATE TABLE IF NOT EXISTS financing_accruals (
        accrual_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        session TEXT NOT NULL,
        amount TEXT NOT NULL,
        annual_rate TEXT NOT NULL,
        flagged INTEGER NOT NULL CHECK(flagged IN (0, 1))
    )""",
    """CREATE TABLE IF NOT EXISTS dividend_receivables (
        receivable_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        event_id TEXT NOT NULL REFERENCES dividend_events(dividend_event_id),
        lot_id TEXT REFERENCES lots(lot_id),
        session TEXT NOT NULL,
        direction TEXT NOT NULL,
        amount TEXT NOT NULL,
        payment_date TEXT,
        settled_session TEXT,
        UNIQUE(cohort_id, event_id, lot_id)
    )""",
    """CREATE TABLE IF NOT EXISTS account_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        epoch_id TEXT NOT NULL,
        session TEXT NOT NULL,
        valuation_at TEXT NOT NULL,
        cash TEXT NOT NULL,
        long_market_value TEXT NOT NULL,
        short_liability TEXT NOT NULL,
        gross_exposure TEXT NOT NULL,
        net_exposure TEXT NOT NULL,
        margin_used TEXT NOT NULL,
        buying_power TEXT NOT NULL,
        realized_pnl TEXT NOT NULL,
        unrealized_pnl TEXT NOT NULL,
        gross_equity TEXT NOT NULL,
        slippage_cost TEXT NOT NULL,
        commission_cost TEXT NOT NULL,
        other_fees TEXT NOT NULL,
        borrow_cost TEXT NOT NULL,
        financing_cost TEXT NOT NULL,
        dividend_cash TEXT NOT NULL,
        net_equity TEXT NOT NULL,
        high_water_mark TEXT NOT NULL,
        valid INTEGER NOT NULL CHECK(valid IN (0, 1)),
        invalid_reason TEXT NOT NULL DEFAULT '',
        UNIQUE(cohort_id, session)
    )""",
    """CREATE TABLE IF NOT EXISTS staging_runs (
        staging_run_id TEXT PRIMARY KEY,
        cohort_id TEXT NOT NULL,
        session TEXT NOT NULL,
        epoch_id TEXT NOT NULL,
        policy_id TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        post_state_digest TEXT NOT NULL,
        UNIQUE(cohort_id, session, epoch_id, policy_id)
    )""",
    """CREATE TABLE IF NOT EXISTS policy_audit_records (
        cohort_id TEXT NOT NULL,
        record_type TEXT NOT NULL,
        record_id TEXT NOT NULL,
        session TEXT NOT NULL,
        epoch_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(cohort_id, record_type, record_id)
    )""",
    # ------------------------------------------------------------------
    # Indexes — shared tables
    # ------------------------------------------------------------------
    """CREATE INDEX IF NOT EXISTS ix_signals_session_strategy
        ON signals(reference_session, strategy, ticker)""",
    """CREATE INDEX IF NOT EXISTS ix_signals_epoch_policy
        ON signals(epoch_id, policy_id, reference_session)""",
    """CREATE INDEX IF NOT EXISTS ix_corporate_actions_ticker_session
        ON corporate_actions(ticker, session)""",
    """CREATE INDEX IF NOT EXISTS ix_dividend_events_ticker_ex
        ON dividend_events(ticker, ex_session)""",
    """CREATE INDEX IF NOT EXISTS ix_strategy_health_epoch_session
        ON strategy_health(epoch_id, session, strategy)""",
    """CREATE INDEX IF NOT EXISTS ix_regime_snapshots_epoch
        ON regime_snapshots(epoch_id, session)""",
    """CREATE INDEX IF NOT EXISTS ix_outcomes_epoch_strategy
        ON outcomes(epoch_id, strategy, entry_session)""",
    """CREATE INDEX IF NOT EXISTS ix_bar_recoveries_epoch_session
        ON bar_recoveries(epoch_id, session)""",
    """CREATE INDEX IF NOT EXISTS ix_candidate_input_issues_epoch_session
        ON candidate_input_issues(epoch_id, session)""",
    # ------------------------------------------------------------------
    # Indexes — cohort-scoped tables (every table indexed by cohort_id)
    # ------------------------------------------------------------------
    """CREATE INDEX IF NOT EXISTS ix_session_runs_cohort_session
        ON session_runs(cohort_id, session)""",
    """CREATE INDEX IF NOT EXISTS ix_session_phases_cohort_session
        ON session_phases(cohort_id, session, phase)""",
    """CREATE INDEX IF NOT EXISTS ix_session_exec_ctx_cohort_session
        ON session_execution_contexts(cohort_id, session)""",
    """CREATE INDEX IF NOT EXISTS ix_committee_decisions_cohort_session
        ON committee_decisions(cohort_id, session)""",
    """CREATE INDEX IF NOT EXISTS ix_order_intents_cohort_pending
        ON order_intents(cohort_id, status, side, eligible_session, created_at, intent_id)""",
    """CREATE INDEX IF NOT EXISTS ix_intent_signals_cohort_intent
        ON intent_signals(cohort_id, intent_id)""",
    """CREATE INDEX IF NOT EXISTS ix_fills_cohort_session
        ON fills(cohort_id, session)""",
    """CREATE INDEX IF NOT EXISTS ix_lots_cohort_ticker_open
        ON lots(cohort_id, ticker, opened_session, lot_id) WHERE open_qty > 0""",
    """CREATE INDEX IF NOT EXISTS ix_lots_cohort_ticker_direction_open
        ON lots(cohort_id, ticker, direction, opened_session, lot_id) WHERE open_qty > 0""",
    """CREATE INDEX IF NOT EXISTS ix_lot_closures_cohort_lot
        ON lot_closures(cohort_id, lot_id)""",
    """CREATE INDEX IF NOT EXISTS ix_cash_events_cohort_event
        ON cash_events(cohort_id, event_type, session)""",
    """CREATE INDEX IF NOT EXISTS ix_borrow_accruals_cohort_session
        ON borrow_accruals(cohort_id, session)""",
    """CREATE INDEX IF NOT EXISTS ix_financing_accruals_cohort_session
        ON financing_accruals(cohort_id, session)""",
    """CREATE INDEX IF NOT EXISTS ix_dividend_receivables_cohort_session
        ON dividend_receivables(cohort_id, session, settled_session)""",
    """CREATE INDEX IF NOT EXISTS ix_account_snapshots_cohort_session
        ON account_snapshots(cohort_id, session)""",
    """CREATE INDEX IF NOT EXISTS ix_staging_runs_cohort_session
        ON staging_runs(cohort_id, session)""",
    """CREATE INDEX IF NOT EXISTS ix_policy_audit_cohort_type_session
        ON policy_audit_records(cohort_id, record_type, session, epoch_id)""",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json_and_sha256(payload: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    encoded = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return encoded, digest


class GenerationDB:
    """Unified single-SQLite-per-generation database handle.

    Every connection is opened through ``orchestration._io.open_sqlite`` with
    ``contextlib.closing()``, WAL mode, ``PRAGMA busy_timeout=10000``,
    ``PRAGMA foreign_keys=ON``, and ``PRAGMA synchronous=FULL``.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        readonly: bool = False,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        immutable: bool = False,
    ) -> None:
        self.path = Path(path)
        self.readonly = bool(readonly)
        self.busy_timeout_ms = busy_timeout_ms
        self.immutable = bool(immutable)
        if self.readonly:
            if not self.path.is_file():
                raise FileNotFoundError(self.path)
            present = self.table_names()
            missing = ALL_TABLES - present
            if missing:
                raise ValueError(
                    f"generation database schema incomplete; missing tables: {sorted(missing)}"
                )
        else:
            if self.immutable:
                raise ValueError("immutable=True requires readonly=True")
            self.initialize()

    @classmethod
    def for_state_dir(
        cls,
        state_dir: str | Path,
        *,
        readonly: bool = False,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        immutable: bool = False,
    ) -> GenerationDB:
        """Open ``<state_dir>/generation.sqlite3``."""
        return cls(
            Path(state_dir) / GENERATION_DB_FILENAME,
            readonly=readonly,
            busy_timeout_ms=busy_timeout_ms,
            immutable=immutable,
        )

    @classmethod
    def open_readonly(
        cls,
        path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        immutable: bool = False,
    ) -> GenerationDB:
        """Open an existing generation database in strict read-only mode."""
        return cls(
            path,
            readonly=True,
            busy_timeout_ms=busy_timeout_ms,
            immutable=immutable,
        )

    @contextmanager
    def connect(
        self,
        *,
        readonly: bool | None = None,
    ) -> Iterator[sqlite3.Connection]:
        """Yield a managed SQLite connection with closing() enforced."""
        use_ro = self.readonly if readonly is None else bool(readonly)
        if self.readonly and not use_ro:
            raise PermissionError("GenerationDB was opened readonly")
        with open_sqlite(
            self.path,
            readonly=use_ro,
            busy_timeout_ms=self.busy_timeout_ms,
            immutable=self.immutable if use_ro else False,
        ) as conn:
            yield conn

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Yield a read-write SQLite connection wrapped in an explicit transaction."""
        if self.readonly:
            raise PermissionError("cannot start write transaction on readonly GenerationDB")
        with open_sqlite(
            self.path,
            readonly=False,
            busy_timeout_ms=self.busy_timeout_ms,
            isolation_level=None,
        ) as conn:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            else:
                if conn.in_transaction:
                    conn.execute("COMMIT")

    def initialize(self) -> None:
        """Idempotently create all 31 tables, indexes, and schema_metadata rows."""
        if self.readonly:
            raise PermissionError("cannot initialize readonly GenerationDB")
        created_at = _utc_now_iso()
        with self.transaction(immediate=True) as conn:
            for statement in DDL_STATEMENTS:
                conn.execute(statement)
            conn.execute(
                "INSERT OR IGNORE INTO schema_metadata (metadata_key, metadata_value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_metadata (metadata_key, metadata_value) VALUES (?, ?)",
                ("schema_name", SCHEMA_NAME),
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_metadata (metadata_key, metadata_value) VALUES (?, ?)",
                ("created_at", created_at),
            )

    def schema_version(self) -> int:
        """Return integer schema_version stored in schema_metadata."""
        with self.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT metadata_value FROM schema_metadata WHERE metadata_key = 'schema_version'"
            ).fetchone()
            if row is None:
                raise ValueError("schema_version metadata missing")
            return int(row["metadata_value"])

    def table_names(self) -> frozenset[str]:
        """Return all user table names present in the database."""
        with self.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            return frozenset(row["name"] for row in rows)

    def snapshot(self, dst: str | Path, *, mode: int = 0o600) -> Path:
        """Take a consistent backup snapshot of this generation database."""
        return snapshot_sqlite(
            self.path,
            dst,
            busy_timeout_ms=self.busy_timeout_ms,
            mode=mode,
        )

    def check_integrity(self) -> bool:
        """Verify SQLite integrity_check and foreign_key_check are clean."""
        with self.connect(readonly=True) as conn:
            integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
            if [row[0] for row in integrity_rows] != ["ok"]:
                return False
            fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            return len(fk_rows) == 0

    def write_policy_audit_record(
        self,
        *,
        cohort_id: str,
        record_type: str,
        record_id: str,
        session: str,
        epoch_id: str,
        payload: Mapping[str, Any],
        created_at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Write an idempotent digest-verified policy audit envelope.

        Returns ``payload_sha256``. Rewriting the same ``(cohort_id, record_type,
        record_id)`` with identical ``session``, ``epoch_id``, and digest is a
        no-op; conflicting content raises ``ValueError``.
        """
        payload_json, payload_sha256 = _canonical_json_and_sha256(payload)
        timestamp = created_at or _utc_now_iso()

        def _apply(active_conn: sqlite3.Connection) -> str:
            existing = active_conn.execute(
                """SELECT session, epoch_id, payload_json, payload_sha256
                   FROM policy_audit_records
                   WHERE cohort_id = ? AND record_type = ? AND record_id = ?""",
                (cohort_id, record_type, record_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["session"] == session
                    and existing["epoch_id"] == epoch_id
                    and existing["payload_sha256"] == payload_sha256
                    and existing["payload_json"] == payload_json
                ):
                    return payload_sha256
                raise ValueError(
                    f"policy_audit_records conflict for ({cohort_id!r}, {record_type!r}, {record_id!r})"
                )
            active_conn.execute(
                """INSERT INTO policy_audit_records (
                       cohort_id, record_type, record_id, session, epoch_id,
                       payload_json, payload_sha256, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cohort_id,
                    record_type,
                    record_id,
                    session,
                    epoch_id,
                    payload_json,
                    payload_sha256,
                    timestamp,
                ),
            )
            return payload_sha256

        if conn is not None:
            return _apply(conn)
        with self.transaction(immediate=True) as tx_conn:
            return _apply(tx_conn)

    def read_policy_audit_records(
        self,
        *,
        cohort_id: str,
        record_type: str | None = None,
        session: str | None = None,
        epoch_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Read and digest-verify policy audit records for one cohort."""
        clauses = ["cohort_id = ?"]
        params: list[Any] = [cohort_id]
        if record_type is not None:
            clauses.append("record_type = ?")
            params.append(record_type)
        if session is not None:
            clauses.append("session = ?")
            params.append(session)
        if epoch_id is not None:
            clauses.append("epoch_id = ?")
            params.append(epoch_id)
        sql = (
            "SELECT cohort_id, record_type, record_id, session, epoch_id, "
            "payload_json, payload_sha256, created_at "
            f"FROM policy_audit_records WHERE {' AND '.join(clauses)} "
            "ORDER BY session, record_type, record_id"
        )

        def _query(active_conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = active_conn.execute(sql, params).fetchall()
            records: list[dict[str, Any]] = []
            for row in rows:
                actual_sha = hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()
                if actual_sha != row["payload_sha256"]:
                    raise ValueError(
                        f"policy_audit_records digest mismatch for {row['record_id']!r}"
                    )
                records.append(
                    {
                        "cohort_id": row["cohort_id"],
                        "record_type": row["record_type"],
                        "record_id": row["record_id"],
                        "session": row["session"],
                        "epoch_id": row["epoch_id"],
                        "payload": json.loads(row["payload_json"]),
                        "payload_sha256": row["payload_sha256"],
                        "created_at": row["created_at"],
                    }
                )
            return records

        if conn is not None:
            return _query(conn)
        with self.connect(readonly=True) as ro_conn:
            return _query(ro_conn)

    def write_session_shared_seal(
        self,
        *,
        session: str,
        epoch_id: str,
        generation_id: str,
        config_sha256: str,
        source_digest: str,
        regime_digest: str,
        signals_digest: str,
        marks_digest: str,
        payload: Mapping[str, Any],
        sealed_at: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Persist the immutable shared once-per-session seal."""
        payload_json, payload_sha256 = _canonical_json_and_sha256(payload)
        timestamp = sealed_at or _utc_now_iso()

        def _apply(active_conn: sqlite3.Connection) -> str:
            existing = active_conn.execute(
                """SELECT epoch_id, generation_id, config_sha256, source_digest,
                          regime_digest, signals_digest, marks_digest,
                          payload_json, payload_sha256
                   FROM session_shared_seals WHERE session = ?""",
                (session,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["epoch_id"] == epoch_id
                    and existing["generation_id"] == generation_id
                    and existing["config_sha256"] == config_sha256
                    and existing["source_digest"] == source_digest
                    and existing["regime_digest"] == regime_digest
                    and existing["signals_digest"] == signals_digest
                    and existing["marks_digest"] == marks_digest
                    and existing["payload_sha256"] == payload_sha256
                    and existing["payload_json"] == payload_json
                ):
                    return payload_sha256
                raise ValueError(f"session_shared_seals conflict for session {session!r}")
            active_conn.execute(
                """INSERT INTO session_shared_seals (
                       session, epoch_id, generation_id, config_sha256,
                       source_digest, regime_digest, signals_digest, marks_digest,
                       payload_json, payload_sha256, sealed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session,
                    epoch_id,
                    generation_id,
                    config_sha256,
                    source_digest,
                    regime_digest,
                    signals_digest,
                    marks_digest,
                    payload_json,
                    payload_sha256,
                    timestamp,
                ),
            )
            return payload_sha256

        if conn is not None:
            return _apply(conn)
        with self.transaction(immediate=True) as tx_conn:
            return _apply(tx_conn)

    def read_session_shared_seal(
        self,
        session: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        """Read and digest-verify the shared session seal if present."""
        sql = (
            "SELECT session, epoch_id, generation_id, config_sha256, source_digest, "
            "regime_digest, signals_digest, marks_digest, payload_json, "
            "payload_sha256, sealed_at "
            "FROM session_shared_seals WHERE session = ?"
        )

        def _query(active_conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = active_conn.execute(sql, (session,)).fetchone()
            if row is None:
                return None
            actual_sha = hashlib.sha256(row["payload_json"].encode("utf-8")).hexdigest()
            if actual_sha != row["payload_sha256"]:
                raise ValueError(f"session_shared_seals digest mismatch for {session!r}")
            return {
                "session": row["session"],
                "epoch_id": row["epoch_id"],
                "generation_id": row["generation_id"],
                "config_sha256": row["config_sha256"],
                "source_digest": row["source_digest"],
                "regime_digest": row["regime_digest"],
                "signals_digest": row["signals_digest"],
                "marks_digest": row["marks_digest"],
                "payload": json.loads(row["payload_json"]),
                "payload_sha256": row["payload_sha256"],
                "sealed_at": row["sealed_at"],
            }

        if conn is not None:
            return _query(conn)
        with self.connect(readonly=True) as ro_conn:
            return _query(ro_conn)


__all__ = [
    "GENERATION_DB_FILENAME",
    "SCHEMA_VERSION",
    "SCHEMA_NAME",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "SHARED_TABLES",
    "COHORT_TABLES",
    "ALL_TABLES",
    "DDL_STATEMENTS",
    "GenerationDB",
]
