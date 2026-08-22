from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from tradingagents.strategies.orchestration.candidate_inputs import CandidateInputIssue

from .calendar import XNYSCalendar
from .models import (
    CandidateBarRecoveryRecord,
    CandidateSignalIdentityBinding,
    CriticalGapMarker,
    GOVERNED_BAR_RECOVERY_CONTRACT,
    GovernedBarRecoveryRecord,
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
CREATE TABLE IF NOT EXISTS governed_bar_recoveries (
  recovery_id TEXT PRIMARY KEY,
  contract_version TEXT NOT NULL,
  evidence_digest TEXT NOT NULL,
  epoch_id TEXT NOT NULL,
  session TEXT NOT NULL,
  ticker TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (epoch_id, session, ticker)
);
CREATE TABLE IF NOT EXISTS candidate_signal_identity_bindings (
  binding_id TEXT PRIMARY KEY,
  epoch_id TEXT NOT NULL,
  session TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_input_issues (
  issue_id TEXT PRIMARY KEY,
  epoch_id TEXT NOT NULL,
  session TEXT NOT NULL,
  dependency_kind TEXT NOT NULL,
  ticker TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  UNIQUE (epoch_id, session, dependency_kind, ticker)
);
CREATE INDEX IF NOT EXISTS idx_candidate_bar_recoveries_epoch_session
  ON candidate_bar_recoveries(epoch_id, session);
CREATE INDEX IF NOT EXISTS idx_candidate_input_issues_epoch_session
  ON candidate_input_issues(epoch_id, session);
CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_signal_identity_scope
  ON candidate_signal_identity_bindings(epoch_id, session);
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
_MAX_CANDIDATE_SIGNAL_IDENTITIES = 4_096
_MAX_GOVERNED_RECOVERY_TEXT = 4_096
_MAX_GOVERNED_RECOVERY_ROWS = 7
_MAX_GOVERNED_RECOVERY_COHORTS = 64
_MAX_GOVERNED_RECOVERY_PAYLOAD_BYTES = 100_000
_MAX_CANDIDATE_INPUT_ISSUE_PAYLOAD_BYTES = 1_000_000
_NEW_YORK = ZoneInfo("America/New_York")
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
_CANDIDATE_SIGNAL_BINDING_KEYS = frozenset(
    {"horizon", "ticker", "event_key", "strategy"}
)


class MetricStore:
    """SQLite persistence for immutable, derived metrics-v2 records."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._calendar = XNYSCalendar()
        self._read_only = False
        self._immutable = False
        self._has_candidate_bar_recoveries = True
        self._has_candidate_signal_identity_bindings = True
        self._has_governed_bar_recoveries = True
        self._has_candidate_input_issues = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    @classmethod
    def open_existing(
        cls, path: str | Path, *, immutable: bool = False
    ) -> "MetricStore":
        """Open an existing metric store without schema or journal mutations."""
        target = Path(path)
        if not target.is_file():
            raise FileNotFoundError(target)
        store = cls.__new__(cls)
        store.path = target
        store._calendar = XNYSCalendar()
        store._read_only = True
        store._immutable = bool(immutable)
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
        store._has_candidate_signal_identity_bindings = (
            "candidate_signal_identity_bindings" in tables
        )
        store._has_governed_bar_recoveries = "governed_bar_recoveries" in tables
        store._has_candidate_input_issues = "candidate_input_issues" in tables
        return store

    @property
    def read_only(self) -> bool:
        return self._read_only

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._read_only:
            encoded = quote(str(self.path.resolve()), safe="/")
            immutable = "&immutable=1" if self._immutable else ""
            connection = sqlite3.connect(f"file:{encoded}?mode=ro{immutable}", uri=True)
            connection.execute("PRAGMA query_only=ON")
        else:
            connection = sqlite3.connect(self.path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

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
        data.setdefault("governed_failure_map", {})
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
    def _candidate_signal_identity_binding(
        payload: str,
    ) -> CandidateSignalIdentityBinding:
        data = json.loads(payload)
        data["session"] = date.fromisoformat(data["session"])
        data["identities"] = tuple(dict(identity) for identity in data["identities"])
        return CandidateSignalIdentityBinding(**data)

    @staticmethod
    def _candidate_input_issue(
        row: tuple[object, ...],
        *,
        expected_issue_id: str | None = None,
        expected_scope: tuple[str, date, str, str] | None = None,
    ) -> CandidateInputIssue:
        try:
            issue_id, epoch_id, session, dependency_kind, ticker, payload = row
            if not all(
                isinstance(value, str)
                for value in (issue_id, epoch_id, session, dependency_kind, ticker)
            ):
                raise ValueError
        except (TypeError, ValueError) as error:
            raise ValueError("candidate input issue payload is invalid") from error
        payload = MetricStore._bounded_candidate_input_issue_payload(payload)
        try:
            data = json.loads(payload)
            expected_fields = {
                "issue_id",
                "epoch_id",
                "session",
                "dependency_kind",
                "reason_code",
                "ticker",
                "source",
                "fetched_at",
                "requested_history_digest",
                "returned_history_digest",
                "expected_sessions",
                "observed_sessions",
                "retryable",
                "affected_signal_identities",
                "affected_cohorts",
            }
            if not isinstance(data, dict) or set(data) != expected_fields:
                raise ValueError
            data["session"] = date.fromisoformat(data["session"])
            data["fetched_at"] = datetime.fromisoformat(
                data["fetched_at"].replace("Z", "+00:00")
            )
            data["expected_sessions"] = tuple(
                date.fromisoformat(value) for value in data["expected_sessions"]
            )
            data["observed_sessions"] = tuple(
                date.fromisoformat(value) for value in data["observed_sessions"]
            )
            data["affected_signal_identities"] = tuple(
                dict(value) for value in data["affected_signal_identities"]
            )
            data["affected_cohorts"] = tuple(data["affected_cohorts"])
            record = CandidateInputIssue.create(**data)
        except (
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            RecursionError,
            json.JSONDecodeError,
        ) as error:
            raise ValueError("candidate input issue payload is invalid") from error
        if record.canonical_payload() != payload:
            raise ValueError("candidate input issue payload is not canonical")
        if (
            record.issue_id,
            record.epoch_id,
            record.session.isoformat(),
            record.dependency_kind,
            record.ticker,
        ) != (issue_id, epoch_id, session, dependency_kind, ticker):
            raise ValueError("candidate input issue metadata does not match payload")
        if expected_issue_id is not None and record.issue_id != expected_issue_id:
            raise ValueError("candidate input issue metadata does not match lookup")
        if expected_scope is not None and (
            record.epoch_id,
            record.session,
            record.dependency_kind,
            record.ticker,
        ) != (
            expected_scope[0],
            expected_scope[1],
            expected_scope[2],
            expected_scope[3].upper(),
        ):
            raise ValueError("candidate input issue metadata does not match lookup")
        record.validate_integrity()
        return record

    @staticmethod
    def _bounded_candidate_input_issue_payload(payload: object) -> str:
        if not isinstance(payload, str):
            raise ValueError("candidate input issue payload is invalid")
        try:
            payload_size = len(payload.encode("utf-8"))
        except UnicodeError as error:
            raise ValueError("candidate input issue payload is invalid") from error
        if payload_size > _MAX_CANDIDATE_INPUT_ISSUE_PAYLOAD_BYTES:
            raise ValueError("candidate input issue payload exceeds byte bound")
        return payload

    @staticmethod
    def _governed_payload_data(payload: object) -> dict[str, object]:
        if not isinstance(payload, str):
            raise ValueError("governed bar recovery payload is invalid")
        if len(payload.encode("utf-8")) > _MAX_GOVERNED_RECOVERY_PAYLOAD_BYTES:
            raise ValueError("governed bar recovery payload exceeds byte bound")
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError) as error:
            raise ValueError("governed bar recovery payload is invalid") from error
        record_fields = {
            "recovery_id",
            "contract_version",
            "evidence_digest",
            "epoch_id",
            "session",
            "ticker",
            "original_daily",
            "original_validation_error",
            "expected_starts",
            "observed_starts",
            "intraday_rows",
            "reconstructed_bar",
            "final_validation_error",
            "affected_cohort_ids",
        }
        if not isinstance(data, dict) or set(data) != record_fields:
            raise ValueError("governed bar recovery payload shape is invalid")
        text_fields = (
            "recovery_id",
            "contract_version",
            "evidence_digest",
            "epoch_id",
            "session",
            "ticker",
        )
        if not all(isinstance(data[field], str) for field in text_fields):
            raise ValueError("governed bar recovery payload shape is invalid")
        if data["original_validation_error"] is not None and not isinstance(
            data["original_validation_error"], str
        ):
            raise ValueError("governed bar recovery payload shape is invalid")
        if data["final_validation_error"] is not None and not isinstance(
            data["final_validation_error"], str
        ):
            raise ValueError("governed bar recovery payload shape is invalid")
        bar_keys = {"open", "high", "low", "close"}
        original_keys = bar_keys | {"source", "fetched_at"}
        row_keys = bar_keys | {"start", "fetched_at"}
        reconstructed_keys = bar_keys | {"source"}

        def scalar(value: object) -> bool:
            return value is None or isinstance(value, (str, int, float, bool))

        def shaped_mapping(value: object, keys: set[str]) -> bool:
            return (
                isinstance(value, dict)
                and set(value) == keys
                and all(scalar(item) for item in value.values())
            )

        if not shaped_mapping(data["original_daily"], original_keys):
            raise ValueError("governed bar recovery payload shape is invalid")
        if not shaped_mapping(data["reconstructed_bar"], reconstructed_keys):
            raise ValueError("governed bar recovery payload shape is invalid")
        if not isinstance(data["expected_starts"], list) or not all(
            isinstance(item, str) for item in data["expected_starts"]
        ):
            raise ValueError("governed bar recovery payload shape is invalid")
        if not isinstance(data["observed_starts"], list) or not all(
            isinstance(item, str) for item in data["observed_starts"]
        ):
            raise ValueError("governed bar recovery payload shape is invalid")
        if not isinstance(data["intraday_rows"], list) or not all(
            shaped_mapping(row, row_keys) for row in data["intraday_rows"]
        ):
            raise ValueError("governed bar recovery payload shape is invalid")
        if not isinstance(data["affected_cohort_ids"], list) or not all(
            isinstance(item, str) for item in data["affected_cohort_ids"]
        ):
            raise ValueError("governed bar recovery payload shape is invalid")
        return data

    def _governed_bar_recovery(
        self,
        row: tuple[object, ...],
        *,
        expected_scope: tuple[str, date, str] | None = None,
        expected_recovery_id: str | None = None,
    ) -> GovernedBarRecoveryRecord:
        if len(row) != 7:
            raise ValueError("governed bar recovery metadata is invalid")
        (
            stored_recovery_id,
            stored_contract_version,
            stored_evidence_digest,
            stored_epoch_id,
            stored_session,
            stored_ticker,
            payload,
        ) = row
        metadata = (
            stored_recovery_id,
            stored_contract_version,
            stored_evidence_digest,
            stored_epoch_id,
            stored_session,
            stored_ticker,
        )
        if not all(isinstance(value, str) for value in metadata):
            raise ValueError("governed bar recovery metadata is invalid")
        data = self._governed_payload_data(payload)
        try:
            record = GovernedBarRecoveryRecord.create(
                contract_version=data["contract_version"],
                epoch_id=data["epoch_id"],
                session=date.fromisoformat(data["session"]),
                ticker=data["ticker"],
                original_daily=data["original_daily"],
                original_validation_error=data["original_validation_error"],
                expected_starts=data["expected_starts"],
                observed_starts=data["observed_starts"],
                intraday_rows=data["intraday_rows"],
                reconstructed_bar=data["reconstructed_bar"],
                final_validation_error=data["final_validation_error"],
                affected_cohort_ids=data["affected_cohort_ids"],
                evidence_digest=data["evidence_digest"],
                recovery_id=data["recovery_id"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("governed bar recovery payload is invalid") from error
        if payload != record.canonical_payload():
            raise ValueError("governed bar recovery payload is not canonical")
        if metadata != (
            record.recovery_id,
            record.contract_version,
            record.evidence_digest,
            record.epoch_id,
            record.session.isoformat(),
            record.ticker,
        ):
            raise ValueError("governed bar recovery metadata does not match payload")
        if (
            expected_recovery_id is not None
            and record.recovery_id != expected_recovery_id
        ):
            raise ValueError("governed bar recovery metadata does not match lookup")
        if expected_scope is not None:
            epoch_id, session, ticker = expected_scope
            if (record.epoch_id, record.session, record.ticker) != (
                epoch_id,
                session,
                ticker.upper(),
            ):
                raise ValueError("governed bar recovery metadata does not match lookup")
        self._validate_governed_bar_recovery(record)
        return record

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
        if record.outcome not in {"accepted", "recovered", "quarantined"}:
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

    def _validate_candidate_signal_identity_binding(
        self, record: CandidateSignalIdentityBinding
    ) -> None:
        for value in (record.binding_id, record.epoch_id):
            if not self._bounded_candidate_recovery_text(value):
                raise ValueError(
                    "candidate signal identity binding identifier is invalid"
                )
        if not self._calendar.is_session(record.session):
            raise ValueError(f"{record.session} is not an XNYS session")
        if len(record.identities) > _MAX_CANDIDATE_SIGNAL_IDENTITIES:
            raise ValueError("candidate signal identity binding exceeds bound")
        canonical: list[tuple[str, str, str, str]] = []
        for identity in record.identities:
            if (
                not isinstance(identity, dict)
                or set(identity) != _CANDIDATE_SIGNAL_BINDING_KEYS
                or not all(
                    self._bounded_candidate_recovery_text(value)
                    for value in identity.values()
                )
                or identity["ticker"] != identity["ticker"].upper()
            ):
                raise ValueError("candidate signal identity binding is invalid")
            canonical.append(
                (
                    identity["horizon"],
                    identity["ticker"],
                    identity["event_key"],
                    identity["strategy"],
                )
            )
        if canonical != sorted(set(canonical)):
            raise ValueError("candidate signal identity binding is not canonical")

    @staticmethod
    def _bounded_governed_recovery_text(value: object) -> bool:
        return (
            isinstance(value, str)
            and bool(value.strip())
            and len(value) <= _MAX_GOVERNED_RECOVERY_TEXT
        )

    @classmethod
    def _validate_governed_json_value(cls, value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not cls._bounded_governed_recovery_text(key):
                    raise ValueError("governed bar recovery evidence key is invalid")
                cls._validate_governed_json_value(item)
            return
        if isinstance(value, tuple):
            for item in value:
                cls._validate_governed_json_value(item)
            return
        if isinstance(value, str):
            if len(value) > _MAX_GOVERNED_RECOVERY_TEXT:
                raise ValueError("governed bar recovery evidence text is invalid")
            return
        if isinstance(value, bool) or value is None or isinstance(value, int):
            return
        if isinstance(value, float) and math.isfinite(value):
            return
        raise ValueError("governed bar recovery evidence value is invalid")

    @staticmethod
    def _governed_ohlc_values(
        bar: object,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
        if not isinstance(bar, Mapping):
            return None
        try:
            values = []
            for field in ("open", "high", "low", "close"):
                value = bar[field]
                if isinstance(value, bool):
                    return None
                decimal = Decimal(str(value))
                if not decimal.is_finite() or decimal <= 0:
                    return None
                values.append(decimal)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return None
        return tuple(values)

    @classmethod
    def _governed_ohlc_is_positive_and_coherent(cls, bar: object) -> bool:
        values = cls._governed_ohlc_values(bar)
        if values is None:
            return False
        opening, high, low, close = values
        return (
            high >= max(opening, close) and low <= min(opening, close) and high >= low
        )

    def _validate_governed_bar_recovery(
        self, record: GovernedBarRecoveryRecord
    ) -> None:
        record.validate_integrity()
        if record.contract_version != GOVERNED_BAR_RECOVERY_CONTRACT:
            raise ValueError("governed bar recovery contract is unsupported")
        for value in (
            record.recovery_id,
            record.contract_version,
            record.evidence_digest,
            record.epoch_id,
            record.ticker,
        ):
            if not self._bounded_governed_recovery_text(value):
                raise ValueError("governed bar recovery identifier is invalid")
        if record.ticker != record.ticker.upper():
            raise ValueError("governed bar recovery ticker is invalid")
        row_starts = tuple(row["start"] for row in record.intraday_rows)
        try:
            session_open = self._calendar.session_open(record.session).astimezone(
                _NEW_YORK
            )
            session_close = self._calendar.session_close(record.session).astimezone(
                _NEW_YORK
            )
        except ValueError as error:
            raise ValueError(
                "governed bar recovery interval schedule is invalid"
            ) from error
        derived_starts: list[str] = []
        interval_start = session_open
        while interval_start < session_close:
            derived_starts.append(interval_start.isoformat())
            interval_start += timedelta(hours=1)
        if not record.expected_starts or (
            record.expected_starts != record.observed_starts
            or record.expected_starts != row_starts
            or record.expected_starts != tuple(derived_starts)
        ):
            raise ValueError("governed bar recovery interval schedule is invalid")
        if len(record.expected_starts) > _MAX_GOVERNED_RECOVERY_ROWS:
            raise ValueError(
                "governed bar recovery expected interval count exceeds bound"
            )
        if len(record.observed_starts) > _MAX_GOVERNED_RECOVERY_ROWS:
            raise ValueError(
                "governed bar recovery observed interval count exceeds bound"
            )
        if len(record.intraday_rows) > _MAX_GOVERNED_RECOVERY_ROWS:
            raise ValueError("governed bar recovery intraday row count exceeds bound")
        if len(record.affected_cohort_ids) > _MAX_GOVERNED_RECOVERY_COHORTS:
            raise ValueError(
                "governed bar recovery affected cohort count exceeds bound"
            )
        if record.expected_starts != tuple(sorted(set(record.expected_starts))):
            raise ValueError("governed bar recovery expected starts are not canonical")
        if record.observed_starts != tuple(sorted(set(record.observed_starts))):
            raise ValueError("governed bar recovery observed starts are not canonical")
        if not all(
            self._bounded_governed_recovery_text(cohort_id)
            for cohort_id in record.affected_cohort_ids
        ):
            raise ValueError("governed bar recovery affected cohort is invalid")
        if record.affected_cohort_ids != tuple(sorted(set(record.affected_cohort_ids))):
            raise ValueError("governed bar recovery affected cohorts are not canonical")
        if set(record.original_daily) != {
            "open",
            "high",
            "low",
            "close",
            "source",
            "fetched_at",
        }:
            raise ValueError("governed bar recovery original daily evidence is invalid")
        if set(record.reconstructed_bar) != {
            "open",
            "high",
            "low",
            "close",
            "source",
        }:
            raise ValueError("governed bar recovery reconstructed evidence is invalid")
        for row in record.intraday_rows:
            if set(row) != {"start", "open", "high", "low", "close", "fetched_at"}:
                raise ValueError("governed bar recovery intraday evidence is invalid")
            if not self._governed_ohlc_is_positive_and_coherent(row):
                raise ValueError(
                    "governed bar recovery intraday evidence is incoherent"
                )
        original_values = self._governed_ohlc_values(record.original_daily)
        if original_values is None:
            raise ValueError("governed bar recovery original daily evidence is invalid")
        if self._governed_ohlc_is_positive_and_coherent(record.original_daily):
            raise ValueError(
                "governed bar recovery original daily evidence is coherent"
            )
        if record.original_validation_error != (
            f"incoherent {record.ticker}/{record.session.isoformat()}"
        ):
            raise ValueError(
                "governed bar recovery original validation reason is invalid"
            )
        reconstructed_values = self._governed_ohlc_values(record.reconstructed_bar)
        if (
            reconstructed_values is None
            or not self._governed_ohlc_is_positive_and_coherent(
                record.reconstructed_bar
            )
        ):
            raise ValueError(
                "governed bar recovery reconstructed evidence is incoherent"
            )
        if record.original_daily["source"] != "yfinance":
            raise ValueError("governed bar recovery original source is invalid")
        if record.reconstructed_bar["source"] != "yfinance-60m-reconstruction":
            raise ValueError("governed bar recovery reconstructed source is invalid")
        original_open, original_high, original_low, original_close = original_values
        (
            reconstructed_open,
            reconstructed_high,
            reconstructed_low,
            reconstructed_close,
        ) = reconstructed_values
        if original_open != reconstructed_open:
            raise ValueError(
                "governed bar recovery original open does not match reconstruction"
            )
        if original_close != reconstructed_close:
            raise ValueError(
                "governed bar recovery original close does not match reconstruction"
            )
        high_broken = original_high < max(original_open, original_close)
        low_broken = original_low > min(original_open, original_close)
        if high_broken == low_broken:
            raise ValueError("governed bar recovery both envelope bounds are invalid")
        if high_broken and original_low != reconstructed_low:
            raise ValueError(
                "governed bar recovery unbroken low does not match reconstruction"
            )
        if low_broken and original_high != reconstructed_high:
            raise ValueError(
                "governed bar recovery unbroken high does not match reconstruction"
            )
        intraday_values = [
            self._governed_ohlc_values(row) for row in record.intraday_rows
        ]
        if any(values is None for values in intraday_values):  # pragma: no cover
            raise ValueError("governed bar recovery intraday evidence is invalid")
        first_open = intraday_values[0][0]
        maximum_high = max(values[1] for values in intraday_values)
        minimum_low = min(values[2] for values in intraday_values)
        final_close = intraday_values[-1][3]
        if (first_open, maximum_high, minimum_low, final_close) != reconstructed_values:
            raise ValueError("governed bar recovery intraday aggregation is invalid")
        if record.final_validation_error is not None:
            raise ValueError("governed bar recovery final validation is not accepted")
        self._validate_governed_json_value(record.original_daily)
        self._validate_governed_json_value(record.reconstructed_bar)
        self._validate_governed_json_value(record.intraday_rows)
        payload_size = len(record.canonical_payload().encode("utf-8"))
        if payload_size > _MAX_GOVERNED_RECOVERY_PAYLOAD_BYTES:
            raise ValueError("governed bar recovery payload exceeds byte bound")

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

    def save_governed_bar_recovery(self, record: GovernedBarRecoveryRecord) -> None:
        self._validate_governed_bar_recovery(record)
        payload = record.canonical_payload()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_by_id = connection.execute(
                """
                SELECT recovery_id, contract_version, evidence_digest, epoch_id,
                       session, ticker, payload_json
                FROM governed_bar_recoveries
                WHERE recovery_id = ?
                """,
                (record.recovery_id,),
            ).fetchone()
            if existing_by_id is not None:
                existing = self._governed_bar_recovery(
                    existing_by_id, expected_recovery_id=record.recovery_id
                )
                if existing != record:
                    raise ValueError(
                        f"immutable recovery_id {record.recovery_id!r} has unequal payload"
                    )
                return
            scoped = connection.execute(
                """
                SELECT recovery_id, contract_version, evidence_digest, epoch_id,
                       session, ticker, payload_json
                FROM governed_bar_recoveries
                WHERE epoch_id = ? AND session = ? AND ticker = ?
                """,
                (record.epoch_id, record.session.isoformat(), record.ticker),
            ).fetchone()
            if scoped is not None:
                self._governed_bar_recovery(
                    scoped,
                    expected_scope=(record.epoch_id, record.session, record.ticker),
                )
                raise ValueError(
                    "immutable governed bar recovery scope has unequal payload"
                )
            self._insert_immutable(
                connection,
                table="governed_bar_recoveries",
                id_column="recovery_id",
                record_id=record.recovery_id,
                payload=payload,
                values=(
                    record.recovery_id,
                    record.contract_version,
                    record.evidence_digest,
                    record.epoch_id,
                    record.session.isoformat(),
                    record.ticker,
                    payload,
                    datetime.now().astimezone().isoformat(),
                ),
                insert_sql=(
                    "INSERT INTO governed_bar_recoveries "
                    "(recovery_id, contract_version, evidence_digest, epoch_id, "
                    "session, ticker, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                ),
            )

    def load_governed_bar_recovery(
        self, *, epoch_id: str, session: date, ticker: str
    ) -> GovernedBarRecoveryRecord | None:
        if not self._has_governed_bar_recoveries:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT recovery_id, contract_version, evidence_digest, epoch_id,
                       session, ticker, payload_json
                FROM governed_bar_recoveries
                WHERE epoch_id = ? AND session = ? AND ticker = ?
                """,
                (epoch_id, session.isoformat(), ticker.upper()),
            ).fetchone()
        return (
            self._governed_bar_recovery(
                row,
                expected_scope=(epoch_id, session, ticker),
            )
            if row
            else None
        )

    def load_governed_bar_recovery_by_id(
        self, recovery_id: str
    ) -> GovernedBarRecoveryRecord | None:
        if not self._has_governed_bar_recoveries:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT recovery_id, contract_version, evidence_digest, epoch_id,
                       session, ticker, payload_json
                FROM governed_bar_recoveries
                WHERE recovery_id = ?
                """,
                (recovery_id,),
            ).fetchone()
        return (
            self._governed_bar_recovery(row, expected_recovery_id=recovery_id)
            if row
            else None
        )

    def save_candidate_input_issue(self, record: CandidateInputIssue) -> None:
        record.validate_integrity()
        payload = self._bounded_candidate_input_issue_payload(
            record.canonical_payload()
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_by_id = connection.execute(
                """
                SELECT issue_id, epoch_id, session, dependency_kind, ticker, payload_json
                FROM candidate_input_issues
                WHERE issue_id = ?
                """,
                (record.issue_id,),
            ).fetchone()
            if existing_by_id is not None:
                existing = self._candidate_input_issue(
                    existing_by_id, expected_issue_id=record.issue_id
                )
                if existing.canonical_payload() != payload:
                    raise ValueError(
                        f"immutable candidate input issue_id {record.issue_id!r} has unequal replay evidence"
                    )
                return
            scoped = connection.execute(
                """
                SELECT issue_id, epoch_id, session, dependency_kind, ticker, payload_json
                FROM candidate_input_issues
                WHERE epoch_id = ? AND session = ? AND dependency_kind = ? AND ticker = ?
                """,
                (
                    record.epoch_id,
                    record.session.isoformat(),
                    record.dependency_kind,
                    record.ticker,
                ),
            ).fetchone()
            if scoped is not None:
                self._candidate_input_issue(
                    scoped,
                    expected_scope=(
                        record.epoch_id,
                        record.session,
                        record.dependency_kind,
                        record.ticker,
                    ),
                )
                raise ValueError(
                    "immutable candidate input issue scope has unequal replay evidence"
                )
            connection.execute(
                """
                INSERT INTO candidate_input_issues
                  (issue_id, epoch_id, session, dependency_kind, ticker, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.issue_id,
                    record.epoch_id,
                    record.session.isoformat(),
                    record.dependency_kind,
                    record.ticker,
                    payload,
                ),
            )

    def load_candidate_input_issue(
        self,
        *,
        epoch_id: str,
        session: date,
        dependency_kind: str,
        ticker: str,
    ) -> CandidateInputIssue | None:
        if not self._has_candidate_input_issues:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT issue_id, epoch_id, session, dependency_kind, ticker, payload_json
                FROM candidate_input_issues
                WHERE epoch_id = ? AND session = ? AND dependency_kind = ? AND ticker = ?
                """,
                (epoch_id, session.isoformat(), dependency_kind, ticker.upper()),
            ).fetchone()
        return (
            self._candidate_input_issue(
                row,
                expected_scope=(epoch_id, session, dependency_kind, ticker),
            )
            if row
            else None
        )

    def load_candidate_input_issue_by_id(
        self, issue_id: str
    ) -> CandidateInputIssue | None:
        if not self._has_candidate_input_issues:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT issue_id, epoch_id, session, dependency_kind, ticker, payload_json
                FROM candidate_input_issues
                WHERE issue_id = ?
                """,
                (issue_id,),
            ).fetchone()
        return (
            self._candidate_input_issue(row, expected_issue_id=issue_id) if row else None
        )

    def read_candidate_input_issues(
        self,
        epoch_id: str,
        session: date | None = None,
        *,
        limit: int = 1_000,
    ) -> tuple[CandidateInputIssue, ...]:
        self._validate_limit(limit)
        if not self._has_candidate_input_issues:
            return ()
        clauses = ["epoch_id = ?"]
        values: list[object] = [epoch_id]
        if session is not None:
            clauses.append("session = ?")
            values.append(session.isoformat())
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT issue_id, epoch_id, session, dependency_kind, ticker, payload_json
                FROM candidate_input_issues
                WHERE {" AND ".join(clauses)}
                ORDER BY session, dependency_kind, ticker, issue_id
                LIMIT ?
                """,
                (*values, limit),
            ).fetchall()
        return tuple(self._candidate_input_issue(row) for row in rows)

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

    def read_candidate_bar_recovery_window(
        self,
        epoch_id: str,
        *,
        limit: int = 1_000,
    ) -> tuple[tuple[CandidateBarRecoveryRecord, ...], int]:
        """Return the newest bounded evidence and its complete durable count."""
        self._validate_limit(limit)
        if not self._has_candidate_bar_recoveries:
            return (), 0
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json, COUNT(*) OVER () AS total_records
                FROM candidate_bar_recoveries
                WHERE epoch_id = ?
                ORDER BY session DESC,
                         json_extract(payload_json, '$.ticker') DESC,
                         recovery_id DESC
                LIMIT ?
                """,
                (epoch_id, limit),
            ).fetchall()
        if not rows:
            return (), 0
        return (
            tuple(self._candidate_bar_recovery(row[0]) for row in rows),
            int(rows[0][1]),
        )

    def save_candidate_signal_identity_binding(
        self, record: CandidateSignalIdentityBinding
    ) -> None:
        self._validate_candidate_signal_identity_binding(record)
        payload = self._json(record)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_immutable(
                connection,
                table="candidate_signal_identity_bindings",
                id_column="binding_id",
                record_id=record.binding_id,
                payload=payload,
                values=(
                    record.binding_id,
                    record.epoch_id,
                    record.session.isoformat(),
                    payload,
                ),
                insert_sql=(
                    "INSERT INTO candidate_signal_identity_bindings "
                    "(binding_id, epoch_id, session, payload_json) "
                    "VALUES (?, ?, ?, ?)"
                ),
            )

    def read_candidate_signal_identity_binding(
        self, epoch_id: str, session: date
    ) -> CandidateSignalIdentityBinding | None:
        if not self._has_candidate_signal_identity_bindings:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM candidate_signal_identity_bindings
                WHERE epoch_id = ? AND session = ?
                """,
                (epoch_id, session.isoformat()),
            ).fetchone()
        return self._candidate_signal_identity_binding(row[0]) if row else None

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
