from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from types import MappingProxyType

import pytest

from tradingagents.strategies.orchestration.candidate_inputs import CandidateInputIssue


_DIGEST = "sha256:" + "a" * 64
_RETURNED_DIGEST = "sha256:" + "b" * 64


def _issue(**changes: object) -> CandidateInputIssue:
    values: dict[str, object] = {
        "issue_id": "candidate-input-issue-alx-reference-bar",
        "epoch_id": "epoch-1",
        "session": date(2026, 8, 3),
        "dependency_kind": "reference_bar",
        "reason_code": "invalid_data",
        "ticker": "ALX",
        "source": "yfinance",
        "fetched_at": datetime(2026, 8, 3, 20, 1, tzinfo=UTC),
        "requested_history_digest": _DIGEST,
        "returned_history_digest": _RETURNED_DIGEST,
        "expected_sessions": (date(2026, 7, 31), date(2026, 8, 3)),
        "observed_sessions": (date(2026, 7, 31),),
        "retryable": True,
        "affected_signal_identities": (
            {"event_key": "event-alx-2", "strategy": "litigation"},
            {"event_key": "event-alx-1", "strategy": "earnings_call"},
        ),
        "affected_cohorts": ("horizon_30d_size_5k", "horizon_10d_size_5k"),
    }
    values.update(changes)
    return CandidateInputIssue.create(**values)


def test_candidate_input_issue_canonicalizes_bounded_immutable_evidence() -> None:
    issue = _issue()

    assert issue.ticker == "ALX"
    assert issue.affected_signal_identities == (
        MappingProxyType({"event_key": "event-alx-1", "strategy": "earnings_call"}),
        MappingProxyType({"event_key": "event-alx-2", "strategy": "litigation"}),
    )
    assert issue.affected_cohorts == ("horizon_10d_size_5k", "horizon_30d_size_5k")
    with pytest.raises(TypeError):
        issue.affected_signal_identities[0]["strategy"] = "changed"  # type: ignore[index]


def test_candidate_input_issue_reference_is_deterministic_and_bounded() -> None:
    issue = _issue()

    assert issue.reference() == {
        "issue_id": "candidate-input-issue-alx-reference-bar",
        "epoch_id": "epoch-1",
        "session": "2026-08-03",
        "dependency_kind": "reference_bar",
        "reason_code": "invalid_data",
        "ticker": "ALX",
        "affected_cohorts": ("horizon_10d_size_5k", "horizon_30d_size_5k"),
    }


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("dependency_kind", "other", "dependency kind"),
        ("reason_code", "provider traceback", "reason code"),
        ("ticker", "alx", "ticker"),
        ("fetched_at", datetime(2026, 8, 3, 20, 1), "timezone-aware"),
        ("requested_history_digest", "not-a-digest", "digest"),
        ("session", date(2026, 8, 2), "XNYS session"),
        ("source", "x" * 257, "source"),
        ("expected_sessions", (date(2026, 8, 2),), "XNYS session"),
        ("affected_cohorts", ("x" * 257,), "cohort"),
    ],
)
def test_candidate_input_issue_rejects_invalid_bounded_fields(
    field: str, value: object, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        _issue(**{field: value})


def test_candidate_input_issue_preserves_exact_observed_sessions() -> None:
    observed = (date(2026, 8, 3), date(2026, 7, 31), date(2026, 8, 3))

    assert _issue(observed_sessions=observed).observed_sessions == observed


def test_candidate_input_issue_rejects_mutable_or_malformed_signal_identities() -> None:
    with pytest.raises(ValueError, match="signal identity"):
        _issue(affected_signal_identities=(["event-alx", "litigation"],))
    with pytest.raises(ValueError, match="signal identity"):
        _issue(affected_signal_identities=({"event_key": "event-alx"},))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session", date.min),
        ("session", date.max),
        ("expected_sessions", (date.min,)),
        ("observed_sessions", (date.max,)),
    ],
)
def test_candidate_input_issue_rejects_out_of_range_session_dates(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError, match="session"):
        _issue(**{field: value})


@pytest.mark.parametrize("field", ["dependency_kind", "reason_code"])
def test_candidate_input_issue_rejects_unhashable_vocabulary_values(field: str) -> None:
    with pytest.raises(ValueError, match=field.replace("_", " ")):
        _issue(**{field: []})


def test_candidate_input_issue_integrity_rejects_mutation_outside_create() -> None:
    issue = _issue()

    with pytest.raises(ValueError, match="ticker"):
        replace(issue, ticker="alx").validate_integrity()
