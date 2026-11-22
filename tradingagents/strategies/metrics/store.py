"""Unified SQLite persistence for derived metrics-v2 records.

Backed by ``GenerationDB`` (``generation.sqlite3``) via ``open_sqlite`` /
``closing()`` with ``PRAGMA busy_timeout=10000``. Market-data bar-recovery
and candidate-input-issue evidence live in
``execution/recovery_evidence.py`` (``RecoveryEvidenceStore``).
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from tradingagents.strategies.execution.recovery_evidence import (
    RecoveryEvidenceStore,
    _decode_candidate_bar_recovery,
    _decode_candidate_input_issue,
)
from tradingagents.strategies.orchestration.candidate_inputs import CandidateInputIssue
from tradingagents.strategies.state.generation_db import (
    DEFAULT_BUSY_TIMEOUT_MS,
    GenerationDB,
)

from .calendar import XNYSCalendar
from .models import (
    CandidateBarRecoveryRecord,
    CandidateSignalIdentityBinding,
    CriticalGapMarker,
    GovernedBarRecoveryRecord,
    MetricEpoch,
    OutcomeRecord,
    StrategyHealthRecord,
)


_AUXILIARY_METRIC_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS outcome_inputs (
        input_id TEXT PRIMARY KEY,
        ticker TEXT NOT NULL,
        session TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        UNIQUE (ticker, session)
    )""",
    """CREATE TABLE IF NOT EXISTS critical_gap_markers (
        marker_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        gap_session TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_critical_gap_pending
        ON critical_gap_markers(status) WHERE status = 'pending'""",
)

_CRITICAL_GAP_REASON = "critical_market_data_gap"
_OUTCOME_GAP_REASONS = frozenset(
    {
        _CRITICAL_GAP_REASON,
        "missing_exit_bar",
        "stale_exit_bar",
        "invalid_exit_bar",
    }
)
_MAX_GAP_COHORTS = 64
_MAX_GAP_TICKERS = 2_048
_MAX_GAP_TEXT = 256
_MAX_GAP_DETAIL_TEXT = 4_096
_MAX_GAP_AUDIT_ITEMS = 2_048
_MAX_GAP_PAYLOAD_BYTES = 2_000_000


class MetricStore:
    """SQLite persistence for immutable, derived metrics-v2 records."""

    def __init__(
        self,
        db_or_path: GenerationDB | str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self._calendar = XNYSCalendar()
        self._read_only = False
        self._immutable = False
        if isinstance(db_or_path, GenerationDB):
            if db_or_path.readonly:
                raise PermissionError(
                    "cannot initialize writable MetricStore on readonly GenerationDB"
                )
            self.db = db_or_path
        else:
            self.db = GenerationDB(Path(db_or_path), busy_timeout_ms=busy_timeout_ms)
        self.path = self.db.path
        with self.db.transaction(immediate=True) as conn:
            for stmt in _AUXILIARY_METRIC_DDL:
                conn.execute(stmt)
        self.recovery_evidence = RecoveryEvidenceStore(
            self.db, read_only=False, busy_timeout_ms=busy_timeout_ms
        )

    @classmethod
    def open_existing(
        cls,
        db_or_path: GenerationDB | str | Path,
        *,
        immutable: bool = False,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> MetricStore:
        """Open an existing metric store in strict read-only mode."""
        if isinstance(db_or_path, GenerationDB):
            db = db_or_path
        else:
            target = Path(db_or_path)
            if not target.is_file():
                raise FileNotFoundError(target)
            db = GenerationDB.open_readonly(
                target, busy_timeout_ms=busy_timeout_ms, immutable=immutable
            )
        store = cls.__new__(cls)
        store.db = db
        store.path = db.path
        store._calendar = XNYSCalendar()
        store._read_only = True
        store._immutable = bool(immutable or db.immutable)
        store.recovery_evidence = RecoveryEvidenceStore.open_existing(
            db, immutable=store._immutable, busy_timeout_ms=busy_timeout_ms
        )
        return store

    @property
    def read_only(self) -> bool:
        return self._read_only

    def _connect(self):
        return self.db.connect(readonly=self._read_only)

    @staticmethod
    def _json(record: object) -> str:
        return json.dumps(
            asdict(record),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _epoch(payload: str) -> MetricEpoch:
        data = json.loads(payload)
        data["start_session"] = date.fromisoformat(data["start_session"])
        if data["end_session"] is not None:
            data["end_session"] = date.fromisoformat(data["end_session"])
        return MetricEpoch(**data)

    @staticmethod
    def _outcome(payload: str) -> OutcomeRecord:
        data = json.loads(payload)
        if "analysis_status" not in data:
            data.update(
                analysis_status="legacy_unknown",
                analysis_valid=False,
                analysis_admitted=False,
            )
        data["entry_session"] = date.fromisoformat(data["entry_session"])
        data["exit_session"] = date.fromisoformat(data["exit_session"])
        for field in ("entry_price", "exit_price", "raw_return", "signed_return"):
            if data[field] is not None:
                data[field] = Decimal(data[field])
        return OutcomeRecord(**data)

    @staticmethod
    def _health(payload: str) -> StrategyHealthRecord:
        data = json.loads(payload)
        data["session"] = date.fromisoformat(data["session"])
        return StrategyHealthRecord(**data)

    @staticmethod
    def _critical_gap(payload: str) -> CriticalGapMarker:
        data = json.loads(payload)
        data["gap_session"] = date.fromisoformat(data["gap_session"])
        data.setdefault("governed_failure_map", {})
        if "affected_cohorts" not in data:
            data["affected_cohorts"] = {}
            data["detail_status"] = "legacy_unbound"
        return CriticalGapMarker(**data)

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be positive")

    @staticmethod
    def _insert_immutable(
        conn: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        record_id: str,
        payload: str,
        values: tuple[object, ...],
        insert_sql: str,
    ) -> None:
        row = conn.execute(
            f"SELECT payload_json FROM {table} WHERE {id_column} = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            conn.execute(insert_sql, values)
            return
        if row[0] != payload:
            raise ValueError(
                f"immutable {id_column} {record_id!r} has unequal payload"
            )

    # ------------------------------------------------------------------
    # Metric Epochs
    # ------------------------------------------------------------------

    def save_epoch(self, epoch: MetricEpoch) -> None:
        payload = self._json(epoch)
        with self.db.transaction(immediate=True) as conn:
            existing = conn.execute(
                "SELECT payload_json FROM metric_epochs WHERE epoch_id = ?",
                (epoch.epoch_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != payload:
                    raise ValueError(
                        f"immutable epoch_id {epoch.epoch_id!r} has unequal payload"
                    )
                return
            if epoch.status != "open" or epoch.end_session is not None:
                raise ValueError("new metric epoch must be open with no end session")
            conn.execute(
                """INSERT INTO metric_epochs (
                       epoch_id, generation_id, schema_version, status,
                       start_session, end_session, payload_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    epoch.epoch_id,
                    epoch.generation_id,
                    int(epoch.metric_schema_version),
                    epoch.status,
                    epoch.start_session.isoformat(),
                    None,
                    payload,
                ),
            )

    def load_epoch(self, epoch_id: str) -> MetricEpoch:
        with self.db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT payload_json FROM metric_epochs WHERE epoch_id = ?",
                (epoch_id,),
            ).fetchone()
        if row is None:
            raise KeyError(epoch_id)
        return self._epoch(row[0])

    def current_epoch(self) -> MetricEpoch | None:
        with self.db.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT payload_json FROM metric_epochs
                   ORDER BY start_session DESC, epoch_id DESC LIMIT 1"""
            ).fetchone()
        return self._epoch(row[0]) if row else None

    def close_epoch(
        self,
        epoch_id: str,
        end_session: date,
        reason: str,
        invalid: bool = False,
    ) -> MetricEpoch:
        if not self._calendar.is_session(end_session):
            raise ValueError(f"{end_session} is not an XNYS session")
        target_status = "invalid" if invalid else "closed"
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT payload_json FROM metric_epochs WHERE epoch_id = ?",
                (epoch_id,),
            ).fetchone()
            if row is None:
                raise KeyError(epoch_id)
            current = self._epoch(row[0])
            updated = replace(
                current,
                end_session=end_session,
                status=target_status,
                boundary_reason=reason,
            )
            if current == updated:
                return current
            if current.status != "open":
                raise ValueError(f"conflicting epoch closure for {epoch_id!r}")
            if end_session < current.start_session:
                raise ValueError("epoch end session cannot precede start session")
            conn.execute(
                """UPDATE metric_epochs
                   SET status = ?, end_session = ?, payload_json = ?
                   WHERE epoch_id = ?""",
                (target_status, end_session.isoformat(), self._json(updated), epoch_id),
            )
            return updated

    def invalidate_epoch(
        self, epoch_id: str, end_session: date, reason: str
    ) -> MetricEpoch:
        return self.close_epoch(epoch_id, end_session, reason, invalid=True)

    # ------------------------------------------------------------------
    # Critical Gap Markers
    # ------------------------------------------------------------------

    def _validate_critical_gap(self, marker: CriticalGapMarker) -> None:
        for label, value in (
            ("marker_id", marker.marker_id),
            ("epoch_id", marker.epoch_id),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > _MAX_GAP_TEXT
            ):
                raise ValueError(f"critical gap {label} is invalid")
        if not self._calendar.is_session(marker.gap_session):
            raise ValueError(f"{marker.gap_session} is not an XNYS session")
        if marker.reason != _CRITICAL_GAP_REASON:
            raise ValueError("critical gap reason must be stable")
        if marker.status not in {"pending", "completed"}:
            raise ValueError("critical gap status is invalid")
        if marker.detail_status not in {"minimal", "ready", "legacy_unbound"}:
            raise ValueError("critical gap detail status is invalid")
        if not isinstance(marker.affected_cohorts, dict) or not marker.affected_cohorts:
            raise ValueError("critical gap affected cohorts are required")
        if len(marker.affected_cohorts) > _MAX_GAP_COHORTS:
            raise ValueError("critical gap affected cohort count exceeds bound")
        bindings: list[str] = []
        for cohort, binding in marker.affected_cohorts.items():
            if (
                not isinstance(cohort, str)
                or not cohort.strip()
                or len(cohort) > _MAX_GAP_TEXT
                or not isinstance(binding, str)
                or not binding.strip()
                or len(binding) > _MAX_GAP_TEXT
            ):
                raise ValueError("critical gap affected cohort binding is invalid")
            bindings.append(binding)
        if len(set(bindings)) != len(bindings):
            raise ValueError("critical gap affected cohort bindings must be unique")
        if not isinstance(marker.cohort_invalid_reasons, dict):
            raise ValueError("critical gap cohort reasons must be a mapping")
        if not isinstance(marker.corporate_action_rejections, dict):
            raise ValueError("critical gap corporate action intents must be a mapping")
        if not isinstance(marker.governed_failure_map, dict):
            raise ValueError("critical gap governed failure map must be a mapping")
        if len(marker.governed_failure_map) > _MAX_GAP_TICKERS:
            raise ValueError("critical gap governed failure count exceeds bound")
        if list(marker.governed_failure_map) != sorted(marker.governed_failure_map):
            raise ValueError("critical gap governed failure map is not canonical")
        for ticker, failure in marker.governed_failure_map.items():
            if (
                not isinstance(ticker, str)
                or not ticker
                or ticker != ticker.strip().upper()
                or len(ticker) > _MAX_GAP_TEXT
                or not isinstance(failure, str)
                or len(failure) > _MAX_GAP_DETAIL_TEXT
            ):
                raise ValueError("critical gap governed failure is invalid")
            parts = failure.split(" ")
            if len(parts) != 2 or parts[0] not in {
                "missing",
                "incoherent",
                "invalid",
                "invalid_benchmark",
            }:
                raise ValueError("critical gap governed failure is invalid")
            if parts[1] != f"{ticker}/{marker.gap_session.isoformat()}":
                raise ValueError("critical gap governed failure scope is invalid")
        if marker.detail_status == "minimal":
            if marker.cohort_invalid_reasons or marker.corporate_action_rejections:
                raise ValueError("minimal critical gap cannot contain recovery detail")
            return
        if marker.detail_status != "ready":
            raise ValueError("critical gap recovery detail is not ready")
        if len(marker.cohort_invalid_reasons) > _MAX_GAP_COHORTS:
            raise ValueError("critical gap cohort count exceeds bound")
        ticker_count = 0
        for cohort, reasons in marker.cohort_invalid_reasons.items():
            if (
                not isinstance(cohort, str)
                or not cohort.strip()
                or len(cohort) > _MAX_GAP_TEXT
                or not isinstance(reasons, dict)
            ):
                raise ValueError("critical gap cohort payload is invalid")
            ticker_count += len(reasons)
            for ticker, invalid_reason in reasons.items():
                if (
                    not isinstance(ticker, str)
                    or not ticker.strip()
                    or len(ticker) > _MAX_GAP_TEXT
                    or invalid_reason not in _OUTCOME_GAP_REASONS
                ):
                    raise ValueError("critical gap ticker payload is invalid")
        if ticker_count > _MAX_GAP_TICKERS:
            raise ValueError("critical gap ticker count exceeds bound")
        if not set(marker.cohort_invalid_reasons) <= set(marker.affected_cohorts):
            raise ValueError("critical gap outcome cohort is not affected")
        audit_intents = marker.corporate_action_rejections
        if len(set(marker.cohort_invalid_reasons) | set(audit_intents)) > _MAX_GAP_COHORTS:
            raise ValueError("critical gap total cohort count exceeds bound")
        if not set(audit_intents) <= set(marker.affected_cohorts):
            raise ValueError("critical gap audit cohort is not affected")
        audit_items = 0
        for cohort, intent in audit_intents.items():
            if (
                not isinstance(cohort, str)
                or not cohort.strip()
                or len(cohort) > _MAX_GAP_TEXT
                or not isinstance(intent, dict)
                or set(intent) != {"actions", "governed_tickers", "errors"}
            ):
                raise ValueError("critical gap corporate action intent is invalid")
            actions, governed, errors = (
                intent["actions"],
                intent["governed_tickers"],
                intent["errors"],
            )
            if not all(isinstance(v, list) for v in (actions, governed, errors)):
                raise ValueError("critical gap corporate action lists are invalid")
            if not errors:
                raise ValueError("critical gap corporate action errors are required")
            audit_items += len(actions) + len(governed) + len(errors)
            for ticker in governed:
                if (
                    not isinstance(ticker, str)
                    or not ticker.strip()
                    or len(ticker) > _MAX_GAP_TEXT
                ):
                    raise ValueError("critical gap governed ticker is invalid")
            for err in errors:
                if (
                    not isinstance(err, str)
                    or not err.strip()
                    or len(err) > _MAX_GAP_DETAIL_TEXT
                ):
                    raise ValueError("critical gap corporate action error is invalid")
            if governed != sorted(set(governed)) or errors != sorted(set(errors)):
                raise ValueError(
                    "critical gap corporate action lists are not canonical"
                )
            expected_action_keys = {
                "action_id",
                "ticker",
                "session",
                "action_type",
                "ratio",
                "cash_per_share",
                "source",
                "fetched_at",
                "verified",
            }
            for action in actions:
                if not isinstance(action, dict) or set(action) != expected_action_keys:
                    raise ValueError("critical gap corporate action is invalid")
                for key in (
                    "action_id",
                    "ticker",
                    "session",
                    "action_type",
                    "source",
                    "fetched_at",
                ):
                    val = action[key]
                    if not isinstance(val, str) or len(val) > _MAX_GAP_TEXT:
                        raise ValueError(
                            "critical gap corporate action text is invalid"
                        )
                try:
                    date.fromisoformat(action["session"])
                    datetime.fromisoformat(action["fetched_at"])
                    for key in ("ratio", "cash_per_share"):
                        val = action[key]
                        if val is not None:
                            if not isinstance(val, str) or len(val) > _MAX_GAP_TEXT:
                                raise ValueError
                            Decimal(val)
                except (TypeError, ValueError, ArithmeticError) as error:
                    raise ValueError(
                        "critical gap corporate action value is invalid"
                    ) from error
                if not isinstance(action["verified"], bool):
                    raise ValueError(
                        "critical gap corporate action verified is invalid"
                    )
        if audit_items > _MAX_GAP_AUDIT_ITEMS:
            raise ValueError("critical gap corporate action item count exceeds bound")
        if len(self._json(marker).encode("utf-8")) > _MAX_GAP_PAYLOAD_BYTES:
            raise ValueError("critical gap payload exceeds byte bound")

    def begin_critical_gap(self, marker: CriticalGapMarker) -> CriticalGapMarker:
        self._validate_critical_gap(marker)
        if marker.status != "pending":
            raise ValueError("new critical gap marker must be pending")
        if marker.detail_status != "minimal":
            raise ValueError("new critical gap marker must be minimal")
        payload = self._json(marker)
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT payload_json FROM critical_gap_markers WHERE marker_id = ?",
                (marker.marker_id,),
            ).fetchone()
            if row is not None:
                existing = self._critical_gap(row[0])
                if existing == marker:
                    return existing
                raise ValueError(
                    f"critical gap marker {marker.marker_id!r} has unequal payload"
                )
            try:
                conn.execute(
                    """INSERT INTO critical_gap_markers
                           (marker_id, status, gap_session, payload_json)
                       VALUES (?, ?, ?, ?)""",
                    (
                        marker.marker_id,
                        marker.status,
                        marker.gap_session.isoformat(),
                        payload,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("another critical gap recovery is pending") from error
        return marker

    def attach_critical_gap_details(
        self, marker: CriticalGapMarker
    ) -> CriticalGapMarker:
        self._validate_critical_gap(marker)
        if marker.status != "pending" or marker.detail_status != "ready":
            raise ValueError("critical gap recovery detail must be ready and pending")
        payload = self._json(marker)
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT payload_json FROM critical_gap_markers WHERE marker_id = ?",
                (marker.marker_id,),
            ).fetchone()
            if row is None:
                raise KeyError(marker.marker_id)
            current = self._critical_gap(row[0])
            core = (
                marker.marker_id,
                marker.epoch_id,
                marker.gap_session,
                marker.reason,
                marker.affected_cohorts,
                marker.governed_failure_map,
                marker.status,
            )
            current_core = (
                current.marker_id,
                current.epoch_id,
                current.gap_session,
                current.reason,
                current.affected_cohorts,
                current.governed_failure_map,
                current.status,
            )
            if current_core != core:
                raise ValueError("critical gap recovery detail has unequal blocker")
            if current.detail_status == "ready":
                if current == marker:
                    return current
                raise ValueError(
                    f"critical gap marker {marker.marker_id!r} has unequal detail"
                )
            if current.detail_status != "minimal":
                raise ValueError("critical gap blocker cannot accept recovery detail")
            conn.execute(
                "UPDATE critical_gap_markers SET payload_json = ? WHERE marker_id = ?",
                (payload, marker.marker_id),
            )
        return marker

    def load_critical_gap(self, marker_id: str) -> CriticalGapMarker:
        with self.db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT payload_json FROM critical_gap_markers WHERE marker_id = ?",
                (marker_id,),
            ).fetchone()
        if row is None:
            raise KeyError(marker_id)
        return self._critical_gap(row[0])

    def pending_critical_gap(self) -> CriticalGapMarker | None:
        with self.db.connect(readonly=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='critical_gap_markers'"
            ).fetchone()
            if exists is None:
                return None
            row = conn.execute(
                """SELECT payload_json FROM critical_gap_markers
                   WHERE status = 'pending'
                   ORDER BY gap_session, marker_id LIMIT 1"""
            ).fetchone()
        return self._critical_gap(row[0]) if row else None

    def complete_critical_gap(self, marker_id: str) -> CriticalGapMarker:
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT payload_json FROM critical_gap_markers WHERE marker_id = ?",
                (marker_id,),
            ).fetchone()
            if row is None:
                raise KeyError(marker_id)
            current = self._critical_gap(row[0])
            if current.status == "completed":
                return current
            if current.detail_status != "ready":
                raise ValueError("critical gap recovery detail is not ready")
            completed = replace(current, status="completed")
            conn.execute(
                """UPDATE critical_gap_markers
                   SET status = 'completed', payload_json = ?
                   WHERE marker_id = ?""",
                (self._json(completed), marker_id),
            )
        return completed

    # ------------------------------------------------------------------
    # Outcomes & Strategy Health
    # ------------------------------------------------------------------

    def upsert_outcome(self, outcome: OutcomeRecord) -> None:
        payload = self._json(outcome)
        with self.db.transaction(immediate=True) as conn:
            self._insert_immutable(
                conn,
                table="outcomes",
                id_column="outcome_id",
                record_id=outcome.outcome_id,
                payload=payload,
                values=(
                    outcome.outcome_id,
                    outcome.epoch_id,
                    outcome.signal_id,
                    outcome.strategy,
                    outcome.ticker,
                    outcome.entry_session.isoformat(),
                    outcome.exit_session.isoformat(),
                    int(outcome.holding_sessions),
                    payload,
                ),
                insert_sql=(
                    "INSERT INTO outcomes ("
                    "outcome_id, epoch_id, signal_id, strategy, ticker, "
                    "entry_session, exit_session, horizon_sessions, payload_json"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
            )

    def save_outcome_input(self, ticker: str, session: date, record: dict) -> None:
        identity = f"{ticker}/{session.isoformat()}"
        payload = json.dumps(
            record, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
        )
        with self.db.transaction(immediate=True) as conn:
            self._insert_immutable(
                conn,
                table="outcome_inputs",
                id_column="input_id",
                record_id=identity,
                payload=payload,
                values=(identity, ticker, session.isoformat(), payload),
                insert_sql="INSERT INTO outcome_inputs VALUES (?, ?, ?, ?)",
            )

    def load_outcome_input(self, ticker: str, session: date) -> dict | None:
        with self.db.connect(readonly=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcome_inputs'"
            ).fetchone()
            if exists is None:
                return None
            row = conn.execute(
                "SELECT payload_json FROM outcome_inputs WHERE ticker = ? AND session = ?",
                (ticker, session.isoformat()),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def outcome_ids(self) -> set[str]:
        with self.db.connect(readonly=True) as conn:
            return {row[0] for row in conn.execute("SELECT outcome_id FROM outcomes")}

    def load_outcome(self, outcome_id: str) -> OutcomeRecord:
        with self.db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT payload_json FROM outcomes WHERE outcome_id = ?",
                (outcome_id,),
            ).fetchone()
        if row is None:
            raise KeyError(outcome_id)
        return self._outcome(row[0])

    def read_outcomes(
        self, epoch_id: str, *, limit: int | None = None
    ) -> tuple[OutcomeRecord, ...]:
        if limit is not None:
            self._validate_limit(limit)
        sql = (
            "SELECT payload_json FROM outcomes WHERE epoch_id = ? "
            "ORDER BY exit_session, outcome_id"
        )
        params: tuple[object, ...] = (epoch_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(self._outcome(row[0]) for row in rows)

    def save_strategy_health(self, health: StrategyHealthRecord) -> None:
        payload = self._json(health)
        with self.db.transaction(immediate=True) as conn:
            self._insert_immutable(
                conn,
                table="strategy_health",
                id_column="health_id",
                record_id=health.health_id,
                payload=payload,
                values=(
                    health.health_id,
                    health.epoch_id,
                    health.session.isoformat(),
                    health.strategy,
                    health.status,
                    payload,
                ),
                insert_sql=(
                    "INSERT INTO strategy_health ("
                    "health_id, epoch_id, session, strategy, status, payload_json"
                    ") VALUES (?, ?, ?, ?, ?, ?)"
                ),
            )

    def load_strategy_health(self, health_id: str) -> StrategyHealthRecord:
        with self.db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT payload_json FROM strategy_health WHERE health_id = ?",
                (health_id,),
            ).fetchone()
        if row is None:
            raise KeyError(health_id)
        return self._health(row[0])

    def read_strategy_health(
        self,
        epoch_id: str | None = None,
        *,
        session: date | None = None,
        limit: int = 1_000,
    ) -> tuple[StrategyHealthRecord, ...]:
        self._validate_limit(limit)
        clauses: list[str] = []
        values: list[object] = []
        if epoch_id is not None:
            clauses.append("epoch_id = ?")
            values.append(epoch_id)
        if session is not None:
            clauses.append("session = ?")
            values.append(session.isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM strategy_health {where} "
                "ORDER BY session, health_id LIMIT ?",
                (*values, limit),
            ).fetchall()
        return tuple(self._health(row[0]) for row in rows)

    # ------------------------------------------------------------------
    # Thin delegators to RecoveryEvidenceStore (for existing callers)
    # ------------------------------------------------------------------

    def save_candidate_bar_recovery(
        self, record: CandidateBarRecoveryRecord
    ) -> None:
        self.recovery_evidence.save_candidate_bar_recovery(record)

    def save_governed_bar_recovery(self, record: GovernedBarRecoveryRecord) -> None:
        self.recovery_evidence.save_governed_bar_recovery(record)

    def _validate_governed_bar_recovery(
        self, record: GovernedBarRecoveryRecord
    ) -> None:
        evidence = getattr(self, "recovery_evidence", None)
        if evidence is not None:
            evidence._validate_governed_bar_recovery(record)
            return
        validator = RecoveryEvidenceStore.__new__(RecoveryEvidenceStore)
        validator._calendar = getattr(self, "_calendar", XNYSCalendar())
        validator._validate_governed_bar_recovery(record)

    def load_governed_bar_recovery(
        self, *, epoch_id: str, session: date, ticker: str
    ) -> GovernedBarRecoveryRecord | None:
        return self.recovery_evidence.load_governed_bar_recovery(
            epoch_id=epoch_id, session=session, ticker=ticker
        )

    def load_governed_bar_recovery_by_id(
        self, recovery_id: str
    ) -> GovernedBarRecoveryRecord | None:
        return self.recovery_evidence.load_governed_bar_recovery_by_id(recovery_id)

    def save_candidate_input_issue(self, record: CandidateInputIssue) -> None:
        self.recovery_evidence.save_candidate_input_issue(record)

    def load_candidate_input_issue(
        self,
        *,
        epoch_id: str,
        session: date,
        dependency_kind: str,
        ticker: str,
    ) -> CandidateInputIssue | None:
        return self.recovery_evidence.load_candidate_input_issue(
            epoch_id=epoch_id,
            session=session,
            dependency_kind=dependency_kind,
            ticker=ticker,
        )

    def load_candidate_input_issue_by_id(
        self, issue_id: str
    ) -> CandidateInputIssue | None:
        return self.recovery_evidence.load_candidate_input_issue_by_id(issue_id)

    def read_candidate_input_issues(
        self,
        epoch_id: str,
        session: date | None = None,
        *,
        limit: int = 1_000,
    ) -> tuple[CandidateInputIssue, ...]:
        return self.recovery_evidence.read_candidate_input_issues(
            epoch_id, session, limit=limit
        )

    def read_session_candidate_input_issues(
        self, epoch_id: str, session: date
    ) -> tuple[CandidateInputIssue, ...]:
        return self.recovery_evidence.read_session_candidate_input_issues(
            epoch_id, session
        )

    def read_session_candidate_bar_recoveries(
        self, epoch_id: str, session: date
    ) -> tuple[CandidateBarRecoveryRecord, ...]:
        return self.recovery_evidence.read_session_candidate_bar_recoveries(
            epoch_id, session
        )

    def read_candidate_bar_recoveries(
        self,
        epoch_id: str,
        session: date | None = None,
        *,
        limit: int = 1_000,
    ) -> tuple[CandidateBarRecoveryRecord, ...]:
        return self.recovery_evidence.read_candidate_bar_recoveries(
            epoch_id, session, limit=limit
        )

    def read_candidate_bar_recovery_window(
        self,
        epoch_id: str,
        *,
        limit: int = 1_000,
    ) -> tuple[tuple[CandidateBarRecoveryRecord, ...], int]:
        return self.recovery_evidence.read_candidate_bar_recovery_window(
            epoch_id, limit=limit
        )

    def save_candidate_signal_identity_binding(
        self, record: CandidateSignalIdentityBinding
    ) -> None:
        self.recovery_evidence.save_candidate_signal_identity_binding(record)

    def read_candidate_signal_identity_binding(
        self, epoch_id: str, session: date
    ) -> CandidateSignalIdentityBinding | None:
        return self.recovery_evidence.read_candidate_signal_identity_binding(
            epoch_id, session
        )

    _candidate_bar_recovery = staticmethod(_decode_candidate_bar_recovery)
    _candidate_input_issue = staticmethod(_decode_candidate_input_issue)
