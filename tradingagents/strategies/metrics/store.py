from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from .models import MetricEpoch, OutcomeRecord, StrategyHealthRecord

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
"""

class MetricStore:
    """SQLite persistence for immutable, derived metrics-v2 records."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
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
