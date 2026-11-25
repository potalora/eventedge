from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Literal, Mapping, Sequence

import exchange_calendars
import pandas as pd

CandidateInputDependencyKind = Literal["reference_bar", "volatility_history"]
CandidateInputReasonCode = Literal[
    "provider_error", "missing_data", "stale_data", "invalid_data", "insufficient_history"
]

_DEPENDENCY_KINDS = frozenset({"reference_bar", "volatility_history"})
_REASON_CODES = frozenset(
    {"provider_error", "missing_data", "stale_data", "invalid_data", "insufficient_history"}
)
_SIGNAL_IDENTITY_KEYS = frozenset({"event_key", "strategy"})
_MAX_TEXT = 256
_MAX_SESSIONS = 512
_MAX_SIGNAL_IDENTITIES = 64
_MAX_COHORTS = 64
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CALENDAR = exchange_calendars.get_calendar("XNYS")


def isolated_insufficient_history(issues: Sequence[Mapping[str, object]]) -> bool:
    """One validated disposable-input skip, never a cohort-local subset.

    The historical name is retained for report/generation consumers of #76.
    Call only after canonical evidence/scope validation and exclusion from staging;
    this is not evidence of listing age, provider health, or economic safety.
    """
    return (
        len(issues) == 1
        and issues[0].get("dependency_kind") in _DEPENDENCY_KINDS
        and issues[0].get("reason_code") in _REASON_CODES
        and (issues[0].get("dependency_kind") != "reference_bar"
             or issues[0].get("reason_code") in {"missing_data", "stale_data", "invalid_data"})
        and (issues[0].get("reason_code") != "insufficient_history"
             or issues[0].get("dependency_kind") == "volatility_history")
    )


def qualifying_short_history(expected: Sequence[date], observed: Sequence[date]) -> bool:
    from .trading_calendar import previous_session

    return bool(
        expected and observed and len(observed) < len(expected)
        and tuple(observed) == tuple(expected[-len(observed):])
        and all(previous_session(later) == earlier
                for earlier, later in zip(expected, expected[1:]))
    )


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
        if reason_code == "insufficient_history":
            from .trading_calendar import previous_session

            expected_sessions = _session_dates(
                expected_sessions, field="expected_sessions", allow_duplicates=False
            )
            observed_sessions = _session_dates(
                observed_sessions, field="observed_sessions", allow_duplicates=False
            )
            if (dependency_kind != "volatility_history" or retryable
                    or not qualifying_short_history(expected_sessions, observed_sessions)
                    or expected_sessions[-1] != previous_session(canonical_session)):
                raise ValueError("candidate input issue insufficient history evidence is invalid")
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


# Receipts describe observations, including failed attempts. Acceptance must
# additionally bind a contained staging commitment, not reinterpret a receipt.
def containment_key(issue: CandidateInputIssue, cohort_id: str) -> str:
    return f"candidate_containment:v1:{cohort_id}:{issue.epoch_id}:{issue.session.isoformat()}:{issue.issue_id}"


def save_reference_health(connection, issue: CandidateInputIssue, reason: str, peer) -> None:
    """Bind positive acquisition evidence to immutable issue bytes for replay."""
    import hashlib
    payload = json.dumps({
        "version": 1, "issue_sha256": hashlib.sha256(issue.canonical_payload().encode()).hexdigest(),
        "reason": reason, "ticker": peer.ticker, "session": peer.session.isoformat(),
        "source": peer.source, "fetched_at": peer.fetched_at.isoformat(),
        "ohlc": [str(peer.open), str(peer.high), str(peer.low), str(peer.close)],
    }, sort_keys=True, separators=(",", ":"))
    key = f"candidate_reference_health:v1:{issue.issue_id}"
    row = connection.execute("SELECT metadata_value FROM schema_metadata WHERE metadata_key=?", (key,)).fetchone()
    if row is not None and row[0] != payload:
        raise ValueError("candidate reference health proof conflict")
    connection.execute("INSERT OR IGNORE INTO schema_metadata(metadata_key,metadata_value) VALUES (?,?)", (key, payload))
    connection.commit()
    validate_reference_health(connection, issue)


def _reference_health_row(connection, issue: CandidateInputIssue):
    return connection.execute("SELECT metadata_value FROM schema_metadata WHERE metadata_key=?",
                              (f"candidate_reference_health:v1:{issue.issue_id}",)).fetchone()


def reference_health_recorded(connection, issue: CandidateInputIssue) -> bool:
    """Presence only; acceptance must still validate the proof and obligations."""
    return _reference_health_row(connection, issue) is not None


def validate_reference_health(connection, issue: CandidateInputIssue) -> None:
    import hashlib
    from decimal import Decimal
    from datetime import timedelta
    from tradingagents.strategies.execution.models import MarketBar
    from tradingagents.strategies.execution.price_source import validate_required_bars
    row = _reference_health_row(connection, issue)
    if row is None:
        raise ValueError("candidate reference health proof missing")
    proof = json.loads(row[0])
    if (proof["version"] != 1
            or proof["issue_sha256"] != hashlib.sha256(issue.canonical_payload().encode()).hexdigest()
            or proof["reason"] not in {"missing_data", "stale_data", "invalid_data"}
            or proof["source"] != issue.source or proof["ticker"] == issue.ticker
            or proof["session"] != issue.session.isoformat()):
        raise ValueError("candidate reference health proof invalid")
    peer = MarketBar(proof["ticker"], date.fromisoformat(proof["session"]),
                     *(Decimal(value) for value in proof["ohlc"]), proof["source"],
                     datetime.fromisoformat(proof["fetched_at"]), False)
    validate_required_bars({(peer.ticker, issue.session): peer}, {peer.ticker},
                           issue.session, max(issue.fetched_at, peer.fetched_at), timedelta(hours=24))


def validate_candidate_obligations(connection, issue: CandidateInputIssue) -> None:
    """Exact shared-DB links, all pending sides; no historical closed-trade ban."""
    issue.validate_integrity()
    ticker, session = issue.ticker, issue.session.isoformat()
    if issue.dependency_kind == "reference_bar":
        validate_reference_health(connection, issue)
    if connection.execute(
        "SELECT 1 FROM lots WHERE ticker=? AND open_qty>0 LIMIT 1", (ticker,)
    ).fetchone():
        raise ValueError("excluded candidate has open lots")
    if connection.execute(
        "SELECT 1 FROM signals WHERE ticker=? AND reference_session=? LIMIT 1",
        (ticker, session),
    ).fetchone():
        raise ValueError("excluded candidate has staging-session signals")
    identities = {(i["event_key"], i["strategy"]) for i in issue.affected_signal_identities}
    for intent in connection.execute("SELECT intent_id,status,created_at FROM order_intents"):
        links = connection.execute(
            "SELECT s.ticker,s.event_key,s.strategy FROM intent_signals l "
            "LEFT JOIN signals s ON s.signal_id=l.signal_id WHERE l.intent_id=?",
            (intent[0],),
        ).fetchall()
        created_at = datetime.fromisoformat(intent[2])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("candidate obligation session is unprovable")
        relevant_time = (
            intent[1] == "pending" or created_at.date().isoformat() == session
            or connection.execute(
                "SELECT 1 FROM fills WHERE intent_id=? AND session=? LIMIT 1",
                (intent[0], session),
            ).fetchone() is not None
        )
        if relevant_time and (not links or any(row[0] is None for row in links)):
            raise ValueError("candidate obligation ticker is unprovable")
        if any(row[0] == ticker and (relevant_time or (row[1], row[2]) in identities)
               for row in links):
            raise ValueError("excluded candidate has intent/order obligation")
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='policy_audit_records'").fetchone():
        for row in connection.execute(
            "SELECT payload_json FROM policy_audit_records WHERE session=? AND record_type='policy_candidate_decision'",
            (session,),
        ):
            if json.loads(row[0])["ticker"] == ticker:
                raise ValueError("excluded candidate has accepted candidate decision")


def candidate_acceptance_payload(connection, issue: CandidateInputIssue, cohort_id: str) -> str:
    import hashlib
    issue.validate_integrity()
    rows = connection.execute(
        "SELECT staging_run_id,cohort_id,session,epoch_id,policy_id,completed_at,post_state_digest "
        "FROM staging_runs WHERE cohort_id=? AND session=? AND epoch_id=?",
        (cohort_id, issue.session.isoformat(), issue.epoch_id),
    ).fetchall()
    if len(rows) != 1 or not rows[0][5] or not rows[0][6]:
        raise ValueError("candidate acceptance requires completed staging commitment")
    health_digest = None
    if issue.dependency_kind == "reference_bar":
        validate_reference_health(connection, issue)
        proof = connection.execute("SELECT metadata_value FROM schema_metadata WHERE metadata_key=?",
                                   (f"candidate_reference_health:v1:{issue.issue_id}",)).fetchone()[0]
        health_digest = hashlib.sha256(proof.encode()).hexdigest()
    payload = {"version": 1,
               "issue_sha256": hashlib.sha256(issue.canonical_payload().encode()).hexdigest(),
               "staging": list(rows[0])}
    if health_digest is not None:
        payload["reference_health_sha256"] = health_digest
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def validate_candidate_acceptance(
    connection, issue: CandidateInputIssue, cohort_id: str, *, allow_legacy_report: bool = False
) -> str | None:
    validate_candidate_obligations(connection, issue)
    row = connection.execute(
        "SELECT metadata_value FROM schema_metadata WHERE metadata_key=?",
        (containment_key(issue, cohort_id),),
    ).fetchone()
    if (allow_legacy_report and row is None and issue.dependency_kind == "volatility_history"
            and issue.reason_code == "insufficient_history"):
        # PR #76 reports predate acceptance records. Its canonical short-history
        # check plus the obligation check above is not a fallback for any new reason.
        return None
    expected = candidate_acceptance_payload(connection, issue, cohort_id)
    if row is None or row[0] != expected:
        raise ValueError("candidate containment acceptance missing or corrupt")
    return expected
