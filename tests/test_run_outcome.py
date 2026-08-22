from __future__ import annotations

import pytest

from tradingagents.strategies.orchestration.run_outcome import (
    RunOutcome,
    completed_run,
    run_outcome,
)


@pytest.mark.parametrize("value", ("clean", "degraded", "failed"))
def test_run_outcome_accepts_exact_wire_values(value: str) -> None:
    assert run_outcome({"outcome": value}) is RunOutcome(value)


@pytest.mark.parametrize("value", (None, "ok", "DEGRADED", 1, True))
def test_run_outcome_rejects_missing_or_malformed_authoritative_value(value) -> None:
    payload = {} if value is None else {"outcome": value}
    with pytest.raises(ValueError, match="invalid run outcome"):
        run_outcome(payload)


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        ({"success": True}, RunOutcome.CLEAN),
        ({"success": False, "degraded": True, "execution_valid": True}, RunOutcome.FAILED),
        ({"success": False}, RunOutcome.FAILED),
    ),
)
def test_legacy_outcome_is_available_only_when_requested(payload, expected) -> None:
    assert run_outcome(payload, allow_legacy=True) is expected
    with pytest.raises(ValueError, match="invalid run outcome"):
        run_outcome(payload)


@pytest.mark.parametrize(
    "payload",
    (
        {"success": True, "degraded": True},
        {"success": True, "execution_valid": False},
        {"success": "true"},
        {"success": False, "degraded": "true"},
        {"success": False, "execution_valid": 1},
        {"success": False, "degraded": True, "execution_valid": "true"},
    ),
)
def test_legacy_outcome_rejects_contradictory_or_non_bool_fields(payload) -> None:
    with pytest.raises(ValueError, match="invalid run outcome"):
        run_outcome(payload, allow_legacy=True)


def test_only_clean_and_degraded_are_completed_processes() -> None:
    assert completed_run({"outcome": "clean"}) is True
    assert completed_run({"outcome": "degraded"}) is True
    assert completed_run({"outcome": "failed"}) is False
