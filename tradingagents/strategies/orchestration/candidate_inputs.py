from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Literal, Mapping

import exchange_calendars
import pandas as pd

CandidateInputDependencyKind = Literal["reference_bar", "volatility_history"]
CandidateInputReasonCode = Literal[
    "provider_error", "missing_data", "stale_data", "invalid_data"
]

_DEPENDENCY_KINDS = frozenset({"reference_bar", "volatility_history"})
_REASON_CODES = frozenset(
    {"provider_error", "missing_data", "stale_data", "invalid_data"}
)
_SIGNAL_IDENTITY_KEYS = frozenset({"event_key", "strategy"})
_MAX_TEXT = 256
_MAX_SESSIONS = 512
_MAX_SIGNAL_IDENTITIES = 64
_MAX_COHORTS = 64
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CALENDAR = exchange_calendars.get_calendar("XNYS")


def _is_xnys_session(session: date) -> bool:
    try:
        return bool(_CALENDAR.is_session(pd.Timestamp(session.isoformat())))
    except (OverflowError, TypeError, ValueError):
        return False


def _bounded_text(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > _MAX_TEXT
    ):
        raise ValueError(f"candidate input issue {field} is invalid")
    return value


def _session_dates(
    value: object, *, field: str, allow_duplicates: bool
) -> tuple[date, ...]:
    if not isinstance(value, tuple) or len(value) > _MAX_SESSIONS:
        raise ValueError(f"candidate input issue {field} is invalid")
    sessions: list[date] = []
    for session in value:
        if not isinstance(session, date) or isinstance(session, datetime):
            raise ValueError(f"candidate input issue {field} is invalid")
        if not _is_xnys_session(session):
            raise ValueError(
                f"candidate input issue {field} contains a non-XNYS session"
            )
        sessions.append(session)
    if not allow_duplicates and tuple(sessions) != tuple(sorted(set(sessions))):
        raise ValueError(f"candidate input issue {field} is not canonical")
    return tuple(sessions)


def _canonical_identities(value: object) -> tuple[Mapping[str, str], ...]:
    if not isinstance(value, tuple) or len(value) > _MAX_SIGNAL_IDENTITIES:
        raise ValueError("candidate input issue signal identity collection is invalid")
    identities: list[tuple[str, str]] = []
    for identity in value:
        if not isinstance(identity, Mapping) or set(identity) != _SIGNAL_IDENTITY_KEYS:
            raise ValueError("candidate input issue signal identity is invalid")
        event_key = _bounded_text(identity["event_key"], field="signal identity")
        strategy = _bounded_text(identity["strategy"], field="signal identity")
        identities.append((event_key, strategy))
    return tuple(
        MappingProxyType({"event_key": event_key, "strategy": strategy})
        for event_key, strategy in sorted(set(identities))
    )


def _canonical_cohorts(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple) or len(value) > _MAX_COHORTS:
        raise ValueError("candidate input issue cohort collection is invalid")
    return tuple(sorted({_bounded_text(cohort, field="cohort") for cohort in value}))


@dataclass(frozen=True)
class CandidateInputIssue:
    """Immutable bounded evidence for one candidate-only input failure."""

    issue_id: str
    epoch_id: str
    session: date
    dependency_kind: CandidateInputDependencyKind
    reason_code: CandidateInputReasonCode
    ticker: str
    source: str
    fetched_at: datetime
    requested_history_digest: str
    returned_history_digest: str
    expected_sessions: tuple[date, ...]
    observed_sessions: tuple[date, ...]
    retryable: bool
    affected_signal_identities: tuple[Mapping[str, str], ...]
    affected_cohorts: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        issue_id: str,
        epoch_id: str,
        session: date,
        dependency_kind: CandidateInputDependencyKind,
        reason_code: CandidateInputReasonCode,
        ticker: str,
        source: str,
        fetched_at: datetime,
        requested_history_digest: str,
        returned_history_digest: str,
        expected_sessions: tuple[date, ...],
        observed_sessions: tuple[date, ...],
        retryable: bool,
        affected_signal_identities: tuple[Mapping[str, str], ...],
        affected_cohorts: tuple[str, ...],
    ) -> "CandidateInputIssue":
        canonical_session = _session_dates(
            (session,), field="session", allow_duplicates=False
        )[0]
        if (
            not isinstance(dependency_kind, str)
            or dependency_kind not in _DEPENDENCY_KINDS
        ):
            raise ValueError("candidate input issue dependency kind is invalid")
        if not isinstance(reason_code, str) or reason_code not in _REASON_CODES:
            raise ValueError("candidate input issue reason code is invalid")
        if not isinstance(ticker, str) or ticker != ticker.upper():
            raise ValueError("candidate input issue ticker is invalid")
        canonical_ticker = _bounded_text(ticker, field="ticker")
        if (
            not isinstance(fetched_at, datetime)
            or fetched_at.tzinfo is None
            or fetched_at.utcoffset() is None
        ):
            raise ValueError("candidate input issue fetched_at must be timezone-aware")
        requested = _bounded_text(requested_history_digest, field="requested digest")
        returned = _bounded_text(returned_history_digest, field="returned digest")
        if not _SHA256.fullmatch(requested) or not _SHA256.fullmatch(returned):
            raise ValueError("candidate input issue history digest is invalid")
        if type(retryable) is not bool:
            raise ValueError("candidate input issue retryable is invalid")
        return cls(
            issue_id=_bounded_text(issue_id, field="issue_id"),
            epoch_id=_bounded_text(epoch_id, field="epoch_id"),
            session=canonical_session,
            dependency_kind=dependency_kind,
            reason_code=reason_code,
            ticker=canonical_ticker,
            source=_bounded_text(source, field="source"),
            fetched_at=fetched_at,
            requested_history_digest=requested,
            returned_history_digest=returned,
            expected_sessions=_session_dates(
                expected_sessions, field="expected_sessions", allow_duplicates=False
            ),
            observed_sessions=_session_dates(
                observed_sessions, field="observed_sessions", allow_duplicates=True
            ),
            retryable=retryable,
            affected_signal_identities=_canonical_identities(affected_signal_identities),
            affected_cohorts=_canonical_cohorts(affected_cohorts),
        )

    def evidence_fields(self) -> dict[str, object]:
        return {
            "epoch_id": self.epoch_id,
            "session": self.session.isoformat(),
            "dependency_kind": self.dependency_kind,
            "reason_code": self.reason_code,
            "ticker": self.ticker,
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat(),
            "requested_history_digest": self.requested_history_digest,
            "returned_history_digest": self.returned_history_digest,
            "expected_sessions": tuple(
                session.isoformat() for session in self.expected_sessions
            ),
            "observed_sessions": tuple(
                session.isoformat() for session in self.observed_sessions
            ),
            "retryable": self.retryable,
            "affected_signal_identities": tuple(
                dict(identity) for identity in self.affected_signal_identities
            ),
            "affected_cohorts": self.affected_cohorts,
        }

    def canonical_payload(self) -> str:
        return json.dumps(
            {"issue_id": self.issue_id, **self.evidence_fields()},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    def reference(self) -> dict[str, object]:
        return {
            "issue_id": self.issue_id,
            "epoch_id": self.epoch_id,
            "session": self.session.isoformat(),
            "dependency_kind": self.dependency_kind,
            "reason_code": self.reason_code,
            "ticker": self.ticker,
            "affected_cohorts": self.affected_cohorts,
        }

    def validate_integrity(self) -> None:
        canonical = self.create(
            issue_id=self.issue_id,
            epoch_id=self.epoch_id,
            session=self.session,
            dependency_kind=self.dependency_kind,
            reason_code=self.reason_code,
            ticker=self.ticker,
            source=self.source,
            fetched_at=self.fetched_at,
            requested_history_digest=self.requested_history_digest,
            returned_history_digest=self.returned_history_digest,
            expected_sessions=self.expected_sessions,
            observed_sessions=self.observed_sessions,
            retryable=self.retryable,
            affected_signal_identities=self.affected_signal_identities,
            affected_cohorts=self.affected_cohorts,
        )
        if canonical != self or any(
            not isinstance(identity, MappingProxyType)
            for identity in self.affected_signal_identities
        ):
            raise ValueError("candidate input issue is not canonical")
