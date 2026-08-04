from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from .calendar import XNYSCalendar
from .models import (
    CandidateBarRecoveryRecord,
    CriticalGapMarker,
    MetricEpoch,
    OutcomeRecord,
    StrategyHealthRecord,
)

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS metric_epochs (
  epoch_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
  outcome_id TEXT PRIMARY KEY,
  epoch_id TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_health (
  health_id TEXT PRIMARY KEY,
  epoch_id TEXT NOT NULL,
  session TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS critical_gap_markers (
  marker_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  gap_session TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_bar_recoveries (
  recovery_id TEXT PRIMARY KEY,
  epoch_id TEXT NOT NULL,
  session TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidate_bar_recoveries_epoch_session
  ON candidate_bar_recoveries(epoch_id, session);
CREATE UNIQUE INDEX IF NOT EXISTS idx_critical_gap_pending
  ON critical_gap_markers(status) WHERE status = 'pending';
"""

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
_MAX_CANDIDATE_RECOVERY_TEXT = 256
_MAX_CANDIDATE_RECOVERY_DECIMAL_TEXT = 128
_MAX_CANDIDATE_RECOVERY_ATTEMPTS = 2
_MAX_CANDIDATE_RECOVERY_SIGNALS = 64
_CANDIDATE_ATTEMPT_KEYS = frozenset(
    {
        "ticker",
        "session",
        "attempt",
        "source",
        "fetched_at",
        "open",
        "high",
        "low",
        "close",
        "validation_error",
    }
)
_CANDIDATE_SIGNAL_IDENTITY_KEYS = frozenset({"event_key", "strategy"})


class MetricStore:
    """SQLite persistence for immutable, derived metrics-v2 records."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._calendar = XNYSCalendar()
        self._read_only = False
        self._has_candidate_bar_recoveries = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @classmethod
    def open_existing(cls, path: str | Path) -> "MetricStore":
        """Open an existing metric store without schema or journal mutations."""
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(target)
        store = cls.__new__(cls)
        store.path = target
        store._calendar = XNYSCalendar()
        store._read_only = True
        with store._connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        required = {
            "metric_epochs",
            "outcomes",
            "strategy_health",
            "critical_gap_markers",
        }
        if not required <= tables:
            raise ValueError("existing metric store schema is incomplete")
        store._has_candidate_bar_recoveries = "candidate_bar_recoveries" in tables
        return store

    @property
    def read_only(self) -> bool:
        return self._read_only

    def _connect(self) -> sqlite3.Connection:
        if self._read_only:
            encoded = quote(str(self.path.resolve()), safe="/")
            return sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
        return sqlite3.connect(self.path)

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
        data["entry_session"] = date.fromisoformat(data["entry_session"])
        data["exit_session"] = date.fromisoformat(data["exit_session"])
        for field in (
            "entry_price",
            "exit_price",
            "raw_return",
            "signed_return",
        ):
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
        if "affected_cohorts" not in data:
            data["affected_cohorts"] = {}
            data["detail_status"] = "legacy_unbound"
        return CriticalGapMarker(**data)

    @staticmethod
    def _candidate_bar_recovery(payload: str) -> CandidateBarRecoveryRecord:
        data = json.loads(payload)
        data["session"] = date.fromisoformat(data["session"])
        attempts: list[dict[str, object]] = []
        for raw_attempt in data["attempts"]:
            attempt = dict(raw_attempt)
            attempt["session"] = date.fromisoformat(attempt["session"])
            attempt["fetched_at"] = datetime.fromisoformat(attempt["fetched_at"])
            for field in ("open", "high", "low", "close"):
                if attempt[field] is not None:
                    attempt[field] = Decimal(attempt[field])
            attempts.append(attempt)
        data["attempts"] = tuple(attempts)
        data["signal_identities"] = tuple(
            dict(identity) for identity in data["signal_identities"]
        )
        return CandidateBarRecoveryRecord(**data)

    @staticmethod
    def _bounded_candidate_recovery_text(value: object) -> bool:
        return (
            isinstance(value, str)
            and bool(value.strip())
            and len(value) <= _MAX_CANDIDATE_RECOVERY_TEXT
        )

    def _validate_candidate_bar_recovery(
        self, record: CandidateBarRecoveryRecord
    ) -> None:
        for value in (record.recovery_id, record.epoch_id):
            if not self._bounded_candidate_recovery_text(value):
                raise ValueError("candidate bar recovery identifier is invalid")
        if not self._calendar.is_session(record.session):
            raise ValueError(f"{record.session} is not an XNYS session")
        if (
            not self._bounded_candidate_recovery_text(record.ticker)
            or record.ticker != record.ticker.upper()
        ):
            raise ValueError("candidate bar recovery ticker is invalid")
        if record.outcome not in {"recovered", "quarantined"}:
            raise ValueError("candidate bar recovery outcome is invalid")
        if not 1 <= len(record.attempts) <= _MAX_CANDIDATE_RECOVERY_ATTEMPTS:
            raise ValueError("candidate bar recovery attempt evidence count is invalid")
        for index, evidence in enumerate(record.attempts, start=1):
            if (
                not isinstance(evidence, dict)
                or set(evidence) != _CANDIDATE_ATTEMPT_KEYS
            ):
                raise ValueError(
                    "candidate bar recovery attempt evidence keys are invalid"
                )
            if (
                evidence["ticker"] != record.ticker
                or evidence["session"] != record.session
            ):
                raise ValueError(
                    "candidate bar recovery attempt evidence scope is invalid"
                )
            if type(evidence["attempt"]) is not int or evidence["attempt"] != index:
                raise ValueError(
                    "candidate bar recovery attempt evidence order is invalid"
                )
            if not self._bounded_candidate_recovery_text(evidence["source"]):
                raise ValueError(
                    "candidate bar recovery attempt evidence source is invalid"
                )
            fetched_at = evidence["fetched_at"]
            if (
                not isinstance(fetched_at, datetime)
                or fetched_at.tzinfo is None
                or fetched_at.utcoffset() is None
            ):
                raise ValueError(
                    "candidate bar recovery attempt evidence time is invalid"
                )
            for field in ("open", "high", "low", "close"):
                value = evidence[field]
                if value is not None and (
                    not isinstance(value, Decimal)
                    or not value.is_finite()
                    or len(str(value)) > _MAX_CANDIDATE_RECOVERY_DECIMAL_TEXT
                ):
                    raise ValueError(
                        "candidate bar recovery attempt evidence price is invalid"
                    )
            error = evidence["validation_error"]
            if error is not None and not self._bounded_candidate_recovery_text(error):
                raise ValueError(
                    "candidate bar recovery attempt evidence error is invalid"
                )
        if len(record.signal_identities) > _MAX_CANDIDATE_RECOVERY_SIGNALS:
            raise ValueError(
                "candidate bar recovery signal identity count exceeds bound"
            )
        for identity in record.signal_identities:
            if (
                not isinstance(identity, dict)
                or set(identity) != _CANDIDATE_SIGNAL_IDENTITY_KEYS
                or not all(
                    self._bounded_candidate_recovery_text(value)
                    for value in identity.values()
                )
            ):
                raise ValueError("candidate bar recovery signal identity is invalid")

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
        if not isinstance(marker.affected_cohorts, dict):
            raise ValueError("critical gap affected cohorts must be a mapping")
        if not marker.affected_cohorts:
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
        if len(set(marker.cohort_invalid_reasons) | set(audit_intents)) > (
            _MAX_GAP_COHORTS
        ):
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
            actions = intent["actions"]
            governed = intent["governed_tickers"]
            errors = intent["errors"]
            if not all(
                isinstance(value, list) for value in (actions, governed, errors)
            ):
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
            for error in errors:
                if (
                    not isinstance(error, str)
                    or not error.strip()
                    or len(error) > _MAX_GAP_DETAIL_TEXT
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
                    value = action[key]
                    if not isinstance(value, str) or len(value) > _MAX_GAP_TEXT:
                        raise ValueError(
                            "critical gap corporate action text is invalid"
                        )
                try:
                    date.fromisoformat(action["session"])
                    datetime.fromisoformat(action["fetched_at"])
                    for key in ("ratio", "cash_per_share"):
                        value = action[key]
                        if value is not None:
                            if not isinstance(value, str) or len(value) > _MAX_GAP_TEXT:
                                raise ValueError
                            Decimal(value)
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
        payload_size = len(
            json.dumps(
                asdict(marker), sort_keys=True, separators=(",", ":"), default=str
            ).encode("utf-8")
        )
        if payload_size > _MAX_GAP_PAYLOAD_BYTES:
            raise ValueError("critical gap payload exceeds byte bound")

    @staticmethod
    def _insert_immutable(
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        record_id: str,
        payload: str,
        values: tuple[object, ...],
        insert_sql: str,
    ) -> None:
        row = connection.execute(
            f"SELECT payload_json FROM {table} WHERE {id_column} = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            connection.execute(insert_sql, values)
            return
        if row[0] != payload:
            raise ValueError(f"immutable {id_column} {record_id!r} has unequal payload")

    def save_epoch(self, epoch: MetricEpoch) -> None:
        payload = self._json(epoch)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
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
            connection.execute(
                "INSERT INTO metric_epochs (epoch_id, payload_json) VALUES (?, ?)",
                (epoch.epoch_id, payload),
            )

    def load_epoch(self, epoch_id: str) -> MetricEpoch:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM metric_epochs WHERE epoch_id = ?",
                (epoch_id,),
            ).fetchone()
        if row is None:
            raise KeyError(epoch_id)
        return self._epoch(row[0])

    def current_epoch(self) -> MetricEpoch | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM metric_epochs
                ORDER BY json_extract(payload_json, '$.start_session') DESC,
                         epoch_id DESC
                LIMIT 1
                """
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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
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
            connection.execute(
                "UPDATE metric_epochs SET payload_json = ? WHERE epoch_id = ?",
                (self._json(updated), epoch_id),
            )
            return updated

    def invalidate_epoch(
        self, epoch_id: str, end_session: date, reason: str
    ) -> MetricEpoch:
        return self.close_epoch(epoch_id, end_session, reason, invalid=True)

    def begin_critical_gap(self, marker: CriticalGapMarker) -> CriticalGapMarker:
        """Persist the minimal blocker before any provider-owned recovery detail."""
        self._validate_critical_gap(marker)
        if marker.status != "pending":
            raise ValueError("new critical gap marker must be pending")
        if marker.detail_status != "minimal":
            raise ValueError("new critical gap marker must be minimal")
        payload = self._json(marker)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
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
                connection.execute(
                    """
                    INSERT INTO critical_gap_markers
                      (marker_id, status, gap_session, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
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
        """Atomically attach bounded replay detail to an existing blocker."""
        self._validate_critical_gap(marker)
        if marker.status != "pending" or marker.detail_status != "ready":
            raise ValueError("critical gap recovery detail must be ready and pending")
        payload = self._json(marker)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
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
                marker.status,
            )
            current_core = (
                current.marker_id,
                current.epoch_id,
                current.gap_session,
                current.reason,
                current.affected_cohorts,
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
            connection.execute(
                "UPDATE critical_gap_markers SET payload_json = ? WHERE marker_id = ?",
                (payload, marker.marker_id),
            )
        return marker

    def load_critical_gap(self, marker_id: str) -> CriticalGapMarker:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM critical_gap_markers WHERE marker_id = ?",
                (marker_id,),
            ).fetchone()
        if row is None:
            raise KeyError(marker_id)
        return self._critical_gap(row[0])

    def pending_critical_gap(self) -> CriticalGapMarker | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM critical_gap_markers
                WHERE status = 'pending'
                ORDER BY gap_session, marker_id
                LIMIT 1
                """
            ).fetchone()
        return self._critical_gap(row[0]) if row else None

    def complete_critical_gap(self, marker_id: str) -> CriticalGapMarker:
        """Durably complete a recovered marker; repeated completion is a no-op."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
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
            connection.execute(
                """
                UPDATE critical_gap_markers
                SET status = 'completed', payload_json = ?
                WHERE marker_id = ?
                """,
                (self._json(completed), marker_id),
            )
        return completed

    def save_candidate_bar_recovery(self, record: CandidateBarRecoveryRecord) -> None:
        self._validate_candidate_bar_recovery(record)
        payload = self._json(record)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_immutable(
                connection,
                table="candidate_bar_recoveries",
                id_column="recovery_id",
                record_id=record.recovery_id,
                payload=payload,
                values=(
                    record.recovery_id,
                    record.epoch_id,
                    record.session.isoformat(),
                    payload,
                ),
                insert_sql=(
                    "INSERT INTO candidate_bar_recoveries "
                    "(recovery_id, epoch_id, session, payload_json) "
                    "VALUES (?, ?, ?, ?)"
                ),
            )

    def read_candidate_bar_recoveries(
        self,
        epoch_id: str,
        session: date | None = None,
        *,
        limit: int = 1_000,
    ) -> tuple[CandidateBarRecoveryRecord, ...]:
        self._validate_limit(limit)
        if not self._has_candidate_bar_recoveries:
            return ()
        clauses = ["epoch_id = ?"]
        values: list[object] = [epoch_id]
        if session is not None:
            clauses.append("session = ?")
            values.append(session.isoformat())
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json
                FROM candidate_bar_recoveries
                WHERE {" AND ".join(clauses)}
                ORDER BY session, json_extract(payload_json, '$.ticker'), recovery_id
                LIMIT ?
                """,
                (*values, limit),
            ).fetchall()
        return tuple(self._candidate_bar_recovery(row[0]) for row in rows)

    def upsert_outcome(self, outcome: OutcomeRecord) -> None:
        payload = self._json(outcome)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_immutable(
                connection,
                table="outcomes",
                id_column="outcome_id",
                record_id=outcome.outcome_id,
                payload=payload,
                values=(outcome.outcome_id, outcome.epoch_id, payload),
                insert_sql=(
                    "INSERT INTO outcomes (outcome_id, epoch_id, payload_json) "
                    "VALUES (?, ?, ?)"
                ),
            )

    def load_outcome(self, outcome_id: str) -> OutcomeRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM outcomes WHERE outcome_id = ?",
                (outcome_id,),
            ).fetchone()
        if row is None:
            raise KeyError(outcome_id)
        return self._outcome(row[0])

    def read_outcomes(
        self, epoch_id: str, *, limit: int = 1_000
    ) -> tuple[OutcomeRecord, ...]:
        self._validate_limit(limit)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM outcomes
                WHERE epoch_id = ?
                ORDER BY json_extract(payload_json, '$.exit_session'), outcome_id
                LIMIT ?
                """,
                (epoch_id, limit),
            ).fetchall()
        return tuple(self._outcome(row[0]) for row in rows)

    def save_strategy_health(self, health: StrategyHealthRecord) -> None:
        payload = self._json(health)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_immutable(
                connection,
                table="strategy_health",
                id_column="health_id",
                record_id=health.health_id,
                payload=payload,
                values=(
                    health.health_id,
                    health.epoch_id,
                    health.session.isoformat(),
                    payload,
                ),
                insert_sql=(
                    "INSERT INTO strategy_health "
                    "(health_id, epoch_id, session, payload_json) "
                    "VALUES (?, ?, ?, ?)"
                ),
            )

    def load_strategy_health(self, health_id: str) -> StrategyHealthRecord:
        with self._connect() as connection:
            row = connection.execute(
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
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json
                FROM strategy_health
                {where}
                ORDER BY session, health_id
                LIMIT ?
                """,
                (*values, limit),
            ).fetchall()
        return tuple(self._health(row[0]) for row in rows)

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
