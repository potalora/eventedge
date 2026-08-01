from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .calendar import XNYSCalendar
from .models import (
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

class MetricStore:
    """SQLite persistence for immutable, derived metrics-v2 records."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._calendar = XNYSCalendar()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.executescript(_SCHEMA)

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
            if not all(isinstance(value, list) for value in (actions, governed, errors)):
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
                raise ValueError("critical gap corporate action lists are not canonical")
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
                        raise ValueError("critical gap corporate action text is invalid")
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
                    raise ValueError("critical gap corporate action verified is invalid")
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
        with sqlite3.connect(self.path) as connection:
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
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM metric_epochs WHERE epoch_id = ?",
                (epoch_id,),
            ).fetchone()
        if row is None:
            raise KeyError(epoch_id)
        return self._epoch(row[0])

    def current_epoch(self) -> MetricEpoch | None:
        with sqlite3.connect(self.path) as connection:
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
        with sqlite3.connect(self.path) as connection:
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
        with sqlite3.connect(self.path) as connection:
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
        with sqlite3.connect(self.path) as connection:
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
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM critical_gap_markers WHERE marker_id = ?",
                (marker_id,),
            ).fetchone()
        if row is None:
            raise KeyError(marker_id)
        return self._critical_gap(row[0])

    def pending_critical_gap(self) -> CriticalGapMarker | None:
        with sqlite3.connect(self.path) as connection:
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
        with sqlite3.connect(self.path) as connection:
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

    def upsert_outcome(self, outcome: OutcomeRecord) -> None:
        payload = self._json(outcome)
        with sqlite3.connect(self.path) as connection:
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
        with sqlite3.connect(self.path) as connection:
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
        with sqlite3.connect(self.path) as connection:
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
        with sqlite3.connect(self.path) as connection:
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
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT payload_json FROM strategy_health WHERE health_id = ?",
                (health_id,),
            ).fetchone()
        if row is None:
            raise KeyError(health_id)
        return self._health(row[0])

    def read_strategy_health(
        self, epoch_id: str, *, limit: int = 1_000
    ) -> tuple[StrategyHealthRecord, ...]:
        self._validate_limit(limit)
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM strategy_health
                WHERE epoch_id = ?
                ORDER BY session, health_id
                LIMIT ?
                """,
                (epoch_id, limit),
            ).fetchall()
        return tuple(self._health(row[0]) for row in rows)

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
