"""Market-data bar-recovery and candidate-input-issue evidence store.

Extracted from ``metrics/store.py`` and backed by ``generation.sqlite3``'s
``bar_recoveries`` and ``candidate_input_issues`` tables, plus normalized
per-row signal identity bindings (eliminating 64 MB JSON blob cells).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Mapping
from zoneinfo import ZoneInfo

from tradingagents.strategies.metrics.calendar import XNYSCalendar
from tradingagents.strategies.metrics.models import (
    GOVERNED_BAR_RECOVERY_CONTRACT,
    GOVERNED_SIP_RECOVERY_CONTRACT,
    CandidateBarRecoveryRecord,
    CandidateSignalIdentityBinding,
    GovernedBarRecoveryRecord,
)
from tradingagents.strategies.orchestration.candidate_inputs import CandidateInputIssue
from tradingagents.strategies.state.generation_db import (
    DEFAULT_BUSY_TIMEOUT_MS,
    GenerationDB,
)


_NEW_YORK = ZoneInfo("America/New_York")
_CANDIDATE_BAR_CONTRACT = "candidate_bar_recovery_v1"

_MAX_CANDIDATE_RECOVERY_TEXT = 256
_MAX_CANDIDATE_RECOVERY_DECIMAL_TEXT = 128
_MAX_CANDIDATE_RECOVERY_ATTEMPTS = 2
_MAX_CANDIDATE_RECOVERY_SIGNALS = 64
_MAX_CANDIDATE_SIGNAL_IDENTITIES = 262_144
_MAX_GOVERNED_RECOVERY_TEXT = 4_096
_MAX_GOVERNED_RECOVERY_ROWS = 7
_MAX_GOVERNED_RECOVERY_COHORTS = 64
_MAX_GOVERNED_RECOVERY_PAYLOAD_BYTES = 100_000
_MAX_CANDIDATE_INPUT_ISSUE_PAYLOAD_BYTES = 1_000_000
_MAX_CANDIDATE_SESSION_RECORD_BYTES = 1_000_000
_MAX_CANDIDATE_SESSION_TOTAL_BYTES = 16 * 1024 * 1024

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

_NORMALIZED_BINDING_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS candidate_signal_identity_bindings (
        binding_id TEXT PRIMARY KEY,
        epoch_id TEXT NOT NULL,
        session TEXT NOT NULL,
        UNIQUE(epoch_id, session)
    )""",
    """CREATE TABLE IF NOT EXISTS candidate_signal_identity_rows (
        binding_id TEXT NOT NULL REFERENCES candidate_signal_identity_bindings(binding_id),
        horizon TEXT NOT NULL,
        ticker TEXT NOT NULL,
        event_key TEXT NOT NULL,
        strategy TEXT NOT NULL,
        PRIMARY KEY(binding_id, horizon, ticker, event_key, strategy)
    )""",
)


def _canonical_json(record: object) -> str:
    return json.dumps(
        asdict(record),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be positive")


def _bounded_text(value: object, max_len: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= max_len


def _governed_ohlc_values(
    bar: object,
) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    if not isinstance(bar, Mapping):
        return None
    try:
        values: list[Decimal] = []
        for field in ("open", "high", "low", "close"):
            value = bar[field]
            if isinstance(value, bool):
                return None
            dec = Decimal(str(value))
            if not dec.is_finite() or dec <= 0:
                return None
            values.append(dec)
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
    return tuple(values)  # type: ignore[return-value]


def _governed_ohlc_coherent(bar: object) -> bool:
    values = _governed_ohlc_values(bar)
    if values is None:
        return False
    opening, high, low, close = values
    return high >= max(opening, close) and low <= min(opening, close) and high >= low


def _validate_governed_json_value(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not _bounded_text(key, _MAX_GOVERNED_RECOVERY_TEXT):
                raise ValueError("governed bar recovery evidence key is invalid")
            _validate_governed_json_value(item)
        return
    if isinstance(value, tuple):
        for item in value:
            _validate_governed_json_value(item)
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


def _decode_candidate_bar_recovery(payload: str) -> CandidateBarRecoveryRecord:
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


def _decode_candidate_input_issue(
    row: tuple[object, ...],
    *,
    expected_issue_id: str | None = None,
    expected_scope: tuple[str, date, str, str] | None = None,
) -> CandidateInputIssue:
    try:
        issue_id, epoch_id, session, dependency_kind, ticker, payload = row
        if not all(
            isinstance(v, str)
            for v in (issue_id, epoch_id, session, dependency_kind, ticker)
        ):
            raise ValueError
    except (TypeError, ValueError) as error:
        raise ValueError("candidate input issue payload is invalid") from error
    payload = _bounded_candidate_input_issue_payload(payload)
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
            date.fromisoformat(v) for v in data["expected_sessions"]
        )
        data["observed_sessions"] = tuple(
            date.fromisoformat(v) for v in data["observed_sessions"]
        )
        data["affected_signal_identities"] = tuple(
            dict(v) for v in data["affected_signal_identities"]
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
    if (
        isinstance(data, dict)
        and data.get("contract_version") == GOVERNED_SIP_RECOVERY_CONTRACT
    ):
        record_fields |= {"yahoo_recovery_error", "alternate_daily"}
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
    if not all(isinstance(data[f], str) for f in text_fields):
        raise ValueError("governed bar recovery payload shape is invalid")
    for err_field in ("original_validation_error", "final_validation_error"):
        if data[err_field] is not None and not isinstance(data[err_field], str):
            raise ValueError("governed bar recovery payload shape is invalid")
    bar_keys = {"open", "high", "low", "close"}

    def scalar(v: object) -> bool:
        return v is None or isinstance(v, (str, int, float, bool))

    def shaped(v: object, keys: set[str]) -> bool:
        return (
            isinstance(v, dict)
            and set(v) == keys
            and all(scalar(item) for item in v.values())
        )

    if not shaped(data["original_daily"], bar_keys | {"source", "fetched_at"}):
        raise ValueError("governed bar recovery payload shape is invalid")
    if not shaped(data["reconstructed_bar"], bar_keys | {"source"}):
        raise ValueError("governed bar recovery payload shape is invalid")
    for list_field in ("expected_starts", "observed_starts", "affected_cohort_ids"):
        if not isinstance(data[list_field], list) or not all(
            isinstance(item, str) for item in data[list_field]
        ):
            raise ValueError("governed bar recovery payload shape is invalid")
    if not isinstance(data["intraday_rows"], list) or not all(
        shaped(r, bar_keys | {"start", "fetched_at"}) for r in data["intraday_rows"]
    ):
        raise ValueError("governed bar recovery payload shape is invalid")
    if data["contract_version"] == GOVERNED_SIP_RECOVERY_CONTRACT:
        alternate_keys = bar_keys | {
            "provider",
            "feed",
            "adjustment",
            "timeframe",
            "request_start",
            "request_end",
            "response_symbol",
            "response_timestamp",
            "fetched_at",
            "row_count",
            "pagination_complete",
        }
        if not isinstance(data["yahoo_recovery_error"], str) or not shaped(
            data["alternate_daily"], alternate_keys
        ):
            raise ValueError("governed SIP recovery payload shape is invalid")
    return data


class RecoveryEvidenceStore:
    """Normalized market-data recovery & candidate-input-issue persistence.

    Backed by ``generation.sqlite3``'s ``bar_recoveries`` and
    ``candidate_input_issues`` tables using ``GenerationDB`` (WAL +
    ``busy_timeout=10000`` + ``closing()``).
    """

    def __init__(
        self,
        db_or_path: GenerationDB | str | Path,
        *,
        read_only: bool = False,
        immutable: bool = False,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self._calendar = XNYSCalendar()
        if isinstance(db_or_path, GenerationDB):
            self.db = db_or_path
            self._read_only = read_only or db_or_path.readonly
        else:
            target = Path(db_or_path)
            if read_only:
                self.db = GenerationDB.open_readonly(
                    target, busy_timeout_ms=busy_timeout_ms, immutable=immutable
                )
            else:
                self.db = GenerationDB(target, busy_timeout_ms=busy_timeout_ms)
            self._read_only = read_only
        self.path = self.db.path
        if not self._read_only:
            with self.db.transaction(immediate=True) as conn:
                for stmt in _NORMALIZED_BINDING_DDL:
                    conn.execute(stmt)
        self._tables = self.db.table_names()

    @classmethod
    def open_existing(
        cls,
        db_or_path: GenerationDB | str | Path,
        *,
        immutable: bool = False,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> RecoveryEvidenceStore:
        return cls(
            db_or_path,
            read_only=True,
            immutable=immutable,
            busy_timeout_ms=busy_timeout_ms,
        )

    @property
    def read_only(self) -> bool:
        return self._read_only

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _validate_candidate_bar_recovery(
        self, record: CandidateBarRecoveryRecord
    ) -> None:
        for value in (record.recovery_id, record.epoch_id):
            if not _bounded_text(value, _MAX_CANDIDATE_RECOVERY_TEXT):
                raise ValueError("candidate bar recovery identifier is invalid")
        if not self._calendar.is_session(record.session):
            raise ValueError(f"{record.session} is not an XNYS session")
        if (
            not _bounded_text(record.ticker, _MAX_CANDIDATE_RECOVERY_TEXT)
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
            if not _bounded_text(evidence["source"], _MAX_CANDIDATE_RECOVERY_TEXT):
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
                val = evidence[field]
                if val is not None and (
                    not isinstance(val, Decimal)
                    or not val.is_finite()
                    or len(str(val)) > _MAX_CANDIDATE_RECOVERY_DECIMAL_TEXT
                ):
                    raise ValueError(
                        "candidate bar recovery attempt evidence price is invalid"
                    )
            error = evidence["validation_error"]
            if error is not None and not _bounded_text(
                error, _MAX_CANDIDATE_RECOVERY_TEXT
            ):
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
                    _bounded_text(v, _MAX_CANDIDATE_RECOVERY_TEXT)
                    for v in identity.values()
                )
            ):
                raise ValueError("candidate bar recovery signal identity is invalid")

    def _validate_candidate_signal_identity_binding(
        self, record: CandidateSignalIdentityBinding
    ) -> None:
        for value in (record.binding_id, record.epoch_id):
            if not _bounded_text(value, _MAX_CANDIDATE_RECOVERY_TEXT):
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
                    _bounded_text(v, _MAX_CANDIDATE_RECOVERY_TEXT)
                    for v in identity.values()
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

    def _validate_governed_sip_recovery(
        self, record: GovernedBarRecoveryRecord
    ) -> None:
        alternate = record.alternate_daily
        if alternate is None:
            raise ValueError("governed SIP evidence is missing")
        if (
            not record.epoch_id
            or not record.ticker
            or record.ticker != record.ticker.upper()
            or not record.affected_cohort_ids
            or len(record.affected_cohort_ids) > _MAX_GOVERNED_RECOVERY_COHORTS
            or record.affected_cohort_ids
            != tuple(sorted(set(record.affected_cohort_ids)))
            or any(
                not _bounded_text(c, _MAX_GOVERNED_RECOVERY_TEXT)
                for c in record.affected_cohort_ids
            )
            or record.final_validation_error is not None
            or record.original_validation_error
            != f"incoherent {record.ticker}/{record.session}"
            or record.yahoo_recovery_error
            != f"invalid {record.ticker}/{record.session}"
            or record.original_daily.get("source") != "yfinance"
            or _governed_ohlc_coherent(record.original_daily)
            or _governed_ohlc_values(record.original_daily) is None
        ):
            raise ValueError("governed SIP recovery scope is invalid")
        expected_keys = {
            "provider",
            "feed",
            "adjustment",
            "timeframe",
            "request_start",
            "request_end",
            "response_symbol",
            "response_timestamp",
            "fetched_at",
            "open",
            "high",
            "low",
            "close",
            "row_count",
            "pagination_complete",
        }
        if set(alternate) != expected_keys or (
            alternate["provider"] != "alpaca"
            or alternate["feed"] != "sip"
            or alternate["adjustment"] != "raw"
            or alternate["timeframe"] != "1Day"
            or alternate["response_symbol"] != record.ticker
            or type(alternate["row_count"]) is not int
            or alternate["row_count"] != 1
            or alternate["pagination_complete"] is not True
            or not _governed_ohlc_coherent(alternate)
            or record.reconstructed_bar.get("source") != "alpaca-sip-1d-raw"
            or _governed_ohlc_values(alternate)
            != _governed_ohlc_values(record.reconstructed_bar)
        ):
            raise ValueError("governed SIP observation is invalid")
        try:
            close_at = self._calendar.session_close(record.session)
            midnight = datetime.combine(record.session, datetime.min.time(), _NEW_YORK)
            request_start = datetime.fromisoformat(str(alternate["request_start"]))
            request_end = datetime.fromisoformat(str(alternate["request_end"]))
            response_timestamp = datetime.fromisoformat(
                str(alternate["response_timestamp"])
            )
            fetched_at = datetime.fromisoformat(str(alternate["fetched_at"]))
            original_fetched_at = datetime.fromisoformat(
                str(record.original_daily["fetched_at"])
            )
            if any(
                s.tzinfo is None or s.utcoffset() is None
                for s in (
                    request_start,
                    request_end,
                    response_timestamp,
                    fetched_at,
                    original_fetched_at,
                )
            ):
                raise ValueError("naive timestamp")
            if (
                request_start != midnight
                or response_timestamp != midnight
                or request_end < close_at
                or request_end > fetched_at - timedelta(minutes=15)
                or fetched_at < close_at + timedelta(minutes=15)
                or fetched_at.astimezone(_NEW_YORK).date() != record.session
                or original_fetched_at < close_at
                or original_fetched_at > fetched_at
            ):
                raise ValueError("timestamp scope")
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError("governed SIP timing is invalid") from error
        try:
            s_open = self._calendar.session_open(record.session).astimezone(_NEW_YORK)
            s_close = close_at.astimezone(_NEW_YORK)
            starts: list[str] = []
            cursor = s_open
            while cursor < s_close:
                starts.append(cursor.isoformat())
                cursor += timedelta(hours=1)
            if (
                record.expected_starts != tuple(starts)
                or record.observed_starts
                != tuple(row["start"] for row in record.intraday_rows)
                or len(record.intraday_rows) > len(starts)
                or any(
                    set(row) != {"start", "open", "high", "low", "close", "fetched_at"}
                    or not _governed_ohlc_coherent(row)
                    for row in record.intraday_rows
                )
            ):
                raise ValueError("hourly evidence")
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError("governed SIP Yahoo hourly evidence is invalid") from error

    def _validate_governed_bar_recovery(
        self, record: GovernedBarRecoveryRecord
    ) -> None:
        record.validate_integrity()
        if record.contract_version == GOVERNED_SIP_RECOVERY_CONTRACT:
            self._validate_governed_sip_recovery(record)
            return
        if record.contract_version != GOVERNED_BAR_RECOVERY_CONTRACT:
            raise ValueError("governed bar recovery contract is unsupported")
        if (
            record.yahoo_recovery_error is not None
            or record.alternate_daily is not None
        ):
            raise ValueError("Yahoo recovery contains alternate evidence")
        for value in (
            record.recovery_id,
            record.contract_version,
            record.evidence_digest,
            record.epoch_id,
            record.ticker,
        ):
            if not _bounded_text(value, _MAX_GOVERNED_RECOVERY_TEXT):
                raise ValueError("governed bar recovery identifier is invalid")
        if record.ticker != record.ticker.upper():
            raise ValueError("governed bar recovery ticker is invalid")
        row_starts = tuple(row["start"] for row in record.intraday_rows)
        try:
            s_open = self._calendar.session_open(record.session).astimezone(_NEW_YORK)
            s_close = self._calendar.session_close(record.session).astimezone(_NEW_YORK)
        except ValueError as error:
            raise ValueError(
                "governed bar recovery interval schedule is invalid"
            ) from error
        derived_starts: list[str] = []
        cursor = s_open
        while cursor < s_close:
            derived_starts.append(cursor.isoformat())
            cursor += timedelta(hours=1)
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
            _bounded_text(cid, _MAX_GOVERNED_RECOVERY_TEXT)
            for cid in record.affected_cohort_ids
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
            if not _governed_ohlc_coherent(row):
                raise ValueError(
                    "governed bar recovery intraday evidence is incoherent"
                )
        original_values = _governed_ohlc_values(record.original_daily)
        if original_values is None:
            raise ValueError("governed bar recovery original daily evidence is invalid")
        if _governed_ohlc_coherent(record.original_daily):
            raise ValueError(
                "governed bar recovery original daily evidence is coherent"
            )
        if (
            record.original_validation_error
            != f"incoherent {record.ticker}/{record.session.isoformat()}"
        ):
            raise ValueError(
                "governed bar recovery original validation reason is invalid"
            )
        reconstructed_values = _governed_ohlc_values(record.reconstructed_bar)
        if reconstructed_values is None or not _governed_ohlc_coherent(
            record.reconstructed_bar
        ):
            raise ValueError(
                "governed bar recovery reconstructed evidence is incoherent"
            )
        if record.original_daily["source"] != "yfinance":
            raise ValueError("governed bar recovery original source is invalid")
        if record.reconstructed_bar["source"] != "yfinance-60m-reconstruction":
            raise ValueError("governed bar recovery reconstructed source is invalid")
        o_open, o_high, o_low, o_close = original_values
        r_open, r_high, r_low, r_close = reconstructed_values
        if o_open != r_open:
            raise ValueError(
                "governed bar recovery original open does not match reconstruction"
            )
        if o_close != r_close:
            raise ValueError(
                "governed bar recovery original close does not match reconstruction"
            )
        high_broken = o_high < max(o_open, o_close)
        low_broken = o_low > min(o_open, o_close)
        if high_broken == low_broken:
            raise ValueError("governed bar recovery both envelope bounds are invalid")
        if high_broken and o_low != r_low:
            raise ValueError(
                "governed bar recovery unbroken low does not match reconstruction"
            )
        if low_broken and o_high != r_high:
            raise ValueError(
                "governed bar recovery unbroken high does not match reconstruction"
            )
        intraday_values = [
            _governed_ohlc_values(row) for row in record.intraday_rows
        ]
        if any(v is None for v in intraday_values):
            raise ValueError("governed bar recovery intraday evidence is invalid")
        agg = (
            intraday_values[0][0],  # type: ignore[index]
            max(v[1] for v in intraday_values),  # type: ignore[index]
            min(v[2] for v in intraday_values),  # type: ignore[index]
            intraday_values[-1][3],  # type: ignore[index]
        )
        if agg != reconstructed_values:
            raise ValueError("governed bar recovery intraday aggregation is invalid")
        if record.final_validation_error is not None:
            raise ValueError("governed bar recovery final validation is not accepted")
        _validate_governed_json_value(record.original_daily)
        _validate_governed_json_value(record.reconstructed_bar)
        _validate_governed_json_value(record.intraday_rows)
        if (
            len(record.canonical_payload().encode("utf-8"))
            > _MAX_GOVERNED_RECOVERY_PAYLOAD_BYTES
        ):
            raise ValueError("governed bar recovery payload exceeds byte bound")

    def _decode_governed_row(
        self,
        row: sqlite3.Row | tuple[object, ...],
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
        ) = tuple(row)
        metadata = (
            stored_recovery_id,
            stored_contract_version,
            stored_evidence_digest,
            stored_epoch_id,
            stored_session,
            stored_ticker,
        )
        if not all(isinstance(v, str) for v in metadata):
            raise ValueError("governed bar recovery metadata is invalid")
        data = _governed_payload_data(payload)
        try:
            record = GovernedBarRecoveryRecord.create(
                contract_version=data["contract_version"],  # type: ignore[arg-type]
                epoch_id=data["epoch_id"],  # type: ignore[arg-type]
                session=date.fromisoformat(data["session"]),  # type: ignore[arg-type]
                ticker=data["ticker"],  # type: ignore[arg-type]
                original_daily=data["original_daily"],  # type: ignore[arg-type]
                original_validation_error=data["original_validation_error"],  # type: ignore[arg-type]
                expected_starts=data["expected_starts"],  # type: ignore[arg-type]
                observed_starts=data["observed_starts"],  # type: ignore[arg-type]
                intraday_rows=data["intraday_rows"],  # type: ignore[arg-type]
                reconstructed_bar=data["reconstructed_bar"],  # type: ignore[arg-type]
                final_validation_error=data["final_validation_error"],  # type: ignore[arg-type]
                affected_cohort_ids=data["affected_cohort_ids"],  # type: ignore[arg-type]
                evidence_digest=data["evidence_digest"],  # type: ignore[arg-type]
                recovery_id=data["recovery_id"],  # type: ignore[arg-type]
                yahoo_recovery_error=data.get("yahoo_recovery_error"),  # type: ignore[arg-type]
                alternate_daily=data.get("alternate_daily"),  # type: ignore[arg-type]
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

    # ------------------------------------------------------------------
    # Candidate & Governed Bar Recoveries (bar_recoveries table)
    # ------------------------------------------------------------------

    def save_candidate_bar_recovery(
        self, record: CandidateBarRecoveryRecord
    ) -> None:
        self._validate_candidate_bar_recovery(record)
        payload = _canonical_json(record)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self.db.transaction(immediate=True) as conn:
            existing_id = conn.execute(
                "SELECT payload_json FROM bar_recoveries WHERE recovery_id = ?",
                (record.recovery_id,),
            ).fetchone()
            if existing_id is not None:
                if existing_id[0] != payload:
                    raise ValueError(
                        f"immutable recovery_id {record.recovery_id!r} has unequal payload"
                    )
                return
            scoped = conn.execute(
                """SELECT payload_json FROM bar_recoveries
                   WHERE recovery_kind = 'candidate' AND epoch_id = ?
                     AND session = ? AND ticker = ?""",
                (record.epoch_id, record.session.isoformat(), record.ticker),
            ).fetchone()
            if scoped is not None:
                if scoped[0] != payload:
                    raise ValueError(
                        "immutable candidate bar recovery scope has unequal payload"
                    )
                return
            conn.execute(
                """INSERT INTO bar_recoveries (
                       recovery_id, recovery_kind, contract_version, evidence_digest,
                       epoch_id, session, ticker, payload_json, created_at
                   ) VALUES (?, 'candidate', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.recovery_id,
                    _CANDIDATE_BAR_CONTRACT,
                    digest,
                    record.epoch_id,
                    record.session.isoformat(),
                    record.ticker,
                    payload,
                    _utc_now_iso(),
                ),
            )

    def save_governed_bar_recovery(self, record: GovernedBarRecoveryRecord) -> None:
        self._validate_governed_bar_recovery(record)
        payload = record.canonical_payload()
        with self.db.transaction(immediate=True) as conn:
            existing_by_id = conn.execute(
                """SELECT recovery_id, contract_version, evidence_digest, epoch_id,
                          session, ticker, payload_json
                   FROM bar_recoveries
                   WHERE recovery_id = ? AND recovery_kind = 'governed'""",
                (record.recovery_id,),
            ).fetchone()
            if existing_by_id is not None:
                existing = self._decode_governed_row(
                    existing_by_id, expected_recovery_id=record.recovery_id
                )
                if existing != record:
                    raise ValueError(
                        f"immutable recovery_id {record.recovery_id!r} has unequal payload"
                    )
                return
            scoped = conn.execute(
                """SELECT recovery_id, contract_version, evidence_digest, epoch_id,
                          session, ticker, payload_json
                   FROM bar_recoveries
                   WHERE recovery_kind = 'governed' AND epoch_id = ?
                     AND session = ? AND ticker = ?""",
                (record.epoch_id, record.session.isoformat(), record.ticker),
            ).fetchone()
            if scoped is not None:
                self._decode_governed_row(
                    scoped,
                    expected_scope=(record.epoch_id, record.session, record.ticker),
                )
                raise ValueError(
                    "immutable governed bar recovery scope has unequal payload"
                )
            conn.execute(
                """INSERT INTO bar_recoveries (
                       recovery_id, recovery_kind, contract_version, evidence_digest,
                       epoch_id, session, ticker, payload_json, created_at
                   ) VALUES (?, 'governed', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.recovery_id,
                    record.contract_version,
                    record.evidence_digest,
                    record.epoch_id,
                    record.session.isoformat(),
                    record.ticker,
                    payload,
                    _utc_now_iso(),
                ),
            )

    def load_governed_bar_recovery(
        self, *, epoch_id: str, session: date, ticker: str
    ) -> GovernedBarRecoveryRecord | None:
        with self.db.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT recovery_id, contract_version, evidence_digest, epoch_id,
                          session, ticker, payload_json
                   FROM bar_recoveries
                   WHERE recovery_kind = 'governed' AND epoch_id = ?
                     AND session = ? AND ticker = ?""",
                (epoch_id, session.isoformat(), ticker.upper()),
            ).fetchone()
        return (
            self._decode_governed_row(
                row, expected_scope=(epoch_id, session, ticker)
            )
            if row
            else None
        )

    def load_governed_bar_recovery_by_id(
        self, recovery_id: str
    ) -> GovernedBarRecoveryRecord | None:
        with self.db.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT recovery_id, contract_version, evidence_digest, epoch_id,
                          session, ticker, payload_json
                   FROM bar_recoveries
                   WHERE recovery_kind = 'governed' AND recovery_id = ?""",
                (recovery_id,),
            ).fetchone()
        return (
            self._decode_governed_row(row, expected_recovery_id=recovery_id)
            if row
            else None
        )

    def read_candidate_bar_recoveries(
        self,
        epoch_id: str,
        session: date | None = None,
        *,
        limit: int = 1_000,
    ) -> tuple[CandidateBarRecoveryRecord, ...]:
        _validate_limit(limit)
        clauses = ["recovery_kind = 'candidate'", "epoch_id = ?"]
        values: list[object] = [epoch_id]
        if session is not None:
            clauses.append("session = ?")
            values.append(session.isoformat())
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute(
                f"""SELECT payload_json FROM bar_recoveries
                    WHERE {' AND '.join(clauses)}
                    ORDER BY session, ticker, recovery_id
                    LIMIT ?""",
                (*values, limit),
            ).fetchall()
        return tuple(_decode_candidate_bar_recovery(row[0]) for row in rows)

    def read_candidate_bar_recovery_window(
        self,
        epoch_id: str,
        *,
        limit: int = 1_000,
    ) -> tuple[tuple[CandidateBarRecoveryRecord, ...], int]:
        """Return the newest bounded evidence and its complete durable count."""
        _validate_limit(limit)
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute(
                """SELECT payload_json, COUNT(*) OVER () AS total_records
                   FROM bar_recoveries
                   WHERE recovery_kind = 'candidate' AND epoch_id = ?
                   ORDER BY session DESC, ticker DESC, recovery_id DESC
                   LIMIT ?""",
                (epoch_id, limit),
            ).fetchall()
        if not rows:
            return (), 0
        return (
            tuple(_decode_candidate_bar_recovery(row[0]) for row in rows),
            int(rows[0][1]),
        )

    def read_session_candidate_bar_recoveries(
        self, epoch_id: str, session: date
    ) -> tuple[CandidateBarRecoveryRecord, ...]:
        """Return every validated candidate bar recovery for an exact session."""
        rows = self._complete_candidate_session_rows(
            "bar_recoveries",
            "recovery_id, epoch_id, session, payload_json",
            epoch_id,
            session,
            extra_where="recovery_kind = 'candidate'",
            max_records=_MAX_CANDIDATE_SIGNAL_IDENTITIES,
        )
        records: list[CandidateBarRecoveryRecord] = []
        tickers: set[str] = set()
        for recovery_id, row_epoch, row_session, payload in rows:
            try:
                record = _decode_candidate_bar_recovery(payload)
                self._validate_candidate_bar_recovery(record)
                canonical = _canonical_json(record)
            except (
                AttributeError,
                KeyError,
                TypeError,
                ValueError,
                RecursionError,
            ) as error:
                raise ValueError(
                    "candidate session recovery payload is invalid"
                ) from error
            if (
                canonical != payload
                or (record.recovery_id, record.epoch_id, record.session.isoformat())
                != (recovery_id, row_epoch, row_session)
                or record.epoch_id != epoch_id
                or record.session != session
            ):
                raise ValueError(
                    "candidate session recovery scope or canonical payload is invalid"
                )
            if record.ticker in tickers:
                raise ValueError("candidate session recovery has duplicate ticker scope")
            tickers.add(record.ticker)
            records.append(record)
        return tuple(
            sorted(records, key=lambda r: (r.session, r.ticker, r.recovery_id))
        )

    # ------------------------------------------------------------------
    # Candidate Input Issues (candidate_input_issues table)
    # ------------------------------------------------------------------

    def save_candidate_input_issue(self, record: CandidateInputIssue) -> None:
        record.validate_integrity()
        payload = _bounded_candidate_input_issue_payload(record.canonical_payload())
        with self.db.transaction(immediate=True) as conn:
            existing_by_id = conn.execute(
                """SELECT issue_id, epoch_id, session, dependency_kind, ticker, payload_json
                   FROM candidate_input_issues WHERE issue_id = ?""",
                (record.issue_id,),
            ).fetchone()
            if existing_by_id is not None:
                existing = _decode_candidate_input_issue(
                    tuple(existing_by_id), expected_issue_id=record.issue_id
                )
                if existing.canonical_payload() != payload:
                    raise ValueError(
                        f"immutable candidate input issue_id {record.issue_id!r} has unequal replay evidence"
                    )
                return
            scoped = conn.execute(
                """SELECT issue_id, epoch_id, session, dependency_kind, ticker, payload_json
                   FROM candidate_input_issues
                   WHERE epoch_id = ? AND session = ? AND dependency_kind = ? AND ticker = ?""",
                (
                    record.epoch_id,
                    record.session.isoformat(),
                    record.dependency_kind,
                    record.ticker,
                ),
            ).fetchone()
            if scoped is not None:
                _decode_candidate_input_issue(
                    tuple(scoped),
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
            conn.execute(
                """INSERT INTO candidate_input_issues (
                       issue_id, epoch_id, session, dependency_kind, ticker,
                       payload_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.issue_id,
                    record.epoch_id,
                    record.session.isoformat(),
                    record.dependency_kind,
                    record.ticker,
                    payload,
                    _utc_now_iso(),
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
        with self.db.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT issue_id, epoch_id, session, dependency_kind, ticker, payload_json
                   FROM candidate_input_issues
                   WHERE epoch_id = ? AND session = ? AND dependency_kind = ? AND ticker = ?""",
                (epoch_id, session.isoformat(), dependency_kind, ticker.upper()),
            ).fetchone()
        return (
            _decode_candidate_input_issue(
                tuple(row),
                expected_scope=(epoch_id, session, dependency_kind, ticker),
            )
            if row
            else None
        )

    def load_candidate_input_issue_by_id(
        self, issue_id: str
    ) -> CandidateInputIssue | None:
        with self.db.connect(readonly=True) as conn:
            row = conn.execute(
                """SELECT issue_id, epoch_id, session, dependency_kind, ticker, payload_json
                   FROM candidate_input_issues WHERE issue_id = ?""",
                (issue_id,),
            ).fetchone()
        return (
            _decode_candidate_input_issue(tuple(row), expected_issue_id=issue_id)
            if row
            else None
        )

    def read_candidate_input_issues(
        self,
        epoch_id: str,
        session: date | None = None,
        *,
        limit: int = 1_000,
    ) -> tuple[CandidateInputIssue, ...]:
        _validate_limit(limit)
        clauses = ["epoch_id = ?"]
        values: list[object] = [epoch_id]
        if session is not None:
            clauses.append("session = ?")
            values.append(session.isoformat())
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute(
                f"""SELECT issue_id, epoch_id, session, dependency_kind, ticker, payload_json
                    FROM candidate_input_issues
                    WHERE {' AND '.join(clauses)}
                    ORDER BY session, dependency_kind, ticker, issue_id
                    LIMIT ?""",
                (*values, limit),
            ).fetchall()
        return tuple(_decode_candidate_input_issue(tuple(row)) for row in rows)

    def read_session_candidate_input_issues(
        self, epoch_id: str, session: date
    ) -> tuple[CandidateInputIssue, ...]:
        rows = self._complete_candidate_session_rows(
            "candidate_input_issues",
            "issue_id, epoch_id, session, dependency_kind, ticker, payload_json",
            epoch_id,
            session,
            max_records=2 * _MAX_CANDIDATE_SIGNAL_IDENTITIES,
        )
        records = tuple(_decode_candidate_input_issue(tuple(row)) for row in rows)
        if any(r.epoch_id != epoch_id or r.session != session for r in records):
            raise ValueError("candidate session issue scope is invalid")
        return tuple(
            sorted(
                records,
                key=lambda r: (r.session, r.dependency_kind, r.ticker, r.issue_id),
            )
        )

    def _complete_candidate_session_rows(
        self,
        table: str,
        columns: str,
        epoch_id: str,
        session: date,
        *,
        extra_where: str | None = None,
        max_records: int,
    ) -> list[sqlite3.Row]:
        if (
            not _bounded_text(epoch_id, _MAX_CANDIDATE_RECOVERY_TEXT)
            or epoch_id != epoch_id.strip()
            or not isinstance(session, date)
            or isinstance(session, datetime)
            or not self._calendar.is_session(session)
        ):
            raise ValueError("candidate session evidence scope is invalid")
        where = "epoch_id = ? AND session = ?"
        if extra_where:
            where = f"{extra_where} AND {where}"
        with self.db.connect(readonly=True) as conn:
            if not self.db.immutable:
                conn.execute("BEGIN DEFERRED")
            try:
                count, total_bytes, largest, invalid = conn.execute(
                    f"""SELECT COUNT(*),
                               COALESCE(SUM(length(CAST(payload_json AS BLOB))), 0),
                               COALESCE(MAX(length(CAST(payload_json AS BLOB))), 0),
                               COALESCE(SUM(typeof(payload_json) != 'text'), 0)
                        FROM {table} WHERE {where}""",
                    (epoch_id, session.isoformat()),
                ).fetchone()
                if (
                    count > max_records
                    or total_bytes > _MAX_CANDIDATE_SESSION_TOTAL_BYTES
                    or largest > _MAX_CANDIDATE_SESSION_RECORD_BYTES
                    or invalid
                ):
                    raise ValueError(
                        "candidate session evidence exceeds bound or is invalid"
                    )
                return conn.execute(
                    f"SELECT {columns} FROM {table} WHERE {where} ORDER BY 1",
                    (epoch_id, session.isoformat()),
                ).fetchall()
            finally:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")

    # ------------------------------------------------------------------
    # Normalized Candidate Signal Identity Bindings (zero 64 MB blobs)
    # ------------------------------------------------------------------

    def save_candidate_signal_identity_binding(
        self, record: CandidateSignalIdentityBinding
    ) -> None:
        self._validate_candidate_signal_identity_binding(record)
        rows_to_store = [
            (
                record.binding_id,
                item["horizon"],
                item["ticker"],
                item["event_key"],
                item["strategy"],
            )
            for item in record.identities
        ]
        with self.db.transaction(immediate=True) as conn:
            existing = conn.execute(
                """SELECT binding_id, epoch_id, session
                   FROM candidate_signal_identity_bindings
                   WHERE binding_id = ? OR (epoch_id = ? AND session = ?)""",
                (record.binding_id, record.epoch_id, record.session.isoformat()),
            ).fetchone()
            if existing is not None:
                if (
                    existing["binding_id"] != record.binding_id
                    or existing["epoch_id"] != record.epoch_id
                    or existing["session"] != record.session.isoformat()
                ):
                    raise ValueError(
                        f"immutable binding_id {record.binding_id!r} has unequal payload"
                    )
                stored_rows = [
                    (
                        record.binding_id,
                        r["horizon"],
                        r["ticker"],
                        r["event_key"],
                        r["strategy"],
                    )
                    for r in conn.execute(
                        """SELECT horizon, ticker, event_key, strategy
                           FROM candidate_signal_identity_rows
                           WHERE binding_id = ?
                           ORDER BY horizon, ticker, event_key, strategy""",
                        (record.binding_id,),
                    ).fetchall()
                ]
                if stored_rows != rows_to_store:
                    raise ValueError(
                        f"immutable binding_id {record.binding_id!r} has unequal payload"
                    )
                return
            conn.execute(
                """INSERT INTO candidate_signal_identity_bindings
                       (binding_id, epoch_id, session)
                   VALUES (?, ?, ?)""",
                (record.binding_id, record.epoch_id, record.session.isoformat()),
            )
            if rows_to_store:
                conn.executemany(
                    """INSERT INTO candidate_signal_identity_rows
                           (binding_id, horizon, ticker, event_key, strategy)
                       VALUES (?, ?, ?, ?, ?)""",
                    rows_to_store,
                )

    def read_candidate_signal_identity_binding(
        self, epoch_id: str, session: date
    ) -> CandidateSignalIdentityBinding | None:
        if "candidate_signal_identity_bindings" not in self._tables:
            return None
        with self.db.connect(readonly=True) as conn:
            header = conn.execute(
                """SELECT binding_id, epoch_id, session
                   FROM candidate_signal_identity_bindings
                   WHERE epoch_id = ? AND session = ?""",
                (epoch_id, session.isoformat()),
            ).fetchone()
            if header is None:
                return None
            binding_id = header["binding_id"]
            if not _bounded_text(binding_id, _MAX_CANDIDATE_RECOVERY_TEXT):
                raise ValueError(
                    "candidate signal identity binding identifier is invalid"
                )
            rows = conn.execute(
                """SELECT horizon, ticker, event_key, strategy
                   FROM candidate_signal_identity_rows
                   WHERE binding_id = ?
                   ORDER BY horizon, ticker, event_key, strategy""",
                (binding_id,),
            ).fetchall()
        identities = tuple(
            {
                "horizon": row["horizon"],
                "ticker": row["ticker"],
                "event_key": row["event_key"],
                "strategy": row["strategy"],
            }
            for row in rows
        )
        record = CandidateSignalIdentityBinding(
            binding_id=binding_id,
            epoch_id=header["epoch_id"],
            session=date.fromisoformat(header["session"]),
            identities=identities,
        )
        self._validate_candidate_signal_identity_binding(record)
        return record
