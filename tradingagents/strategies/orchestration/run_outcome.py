from __future__ import annotations

from enum import Enum
from typing import Mapping


DAILY_RESULT_WIRE_VERSION = 1
DAILY_RESULT_PREFIX = "EVENTEDGE_DAILY_RESULT_V1="
DAILY_RESULT_ENVELOPE_KEYS = frozenset({"wire_version", "cohort_results"})


class RunOutcome(str, Enum):
    CLEAN = "clean"
    DEGRADED = "degraded"
    FAILED = "failed"


def run_outcome(
    result: Mapping[str, object], *, allow_legacy: bool = False
) -> RunOutcome:
    value = result.get("outcome")
    if isinstance(value, str):
        try:
            return RunOutcome(value)
        except ValueError:
            pass
    if allow_legacy and "outcome" not in result:
        for field in ("success", "degraded", "execution_valid"):
            if field in result and type(result[field]) is not bool:
                raise ValueError("invalid run outcome")
        if result.get("success") is True and (
            result.get("degraded") is True or result.get("execution_valid") is False
        ):
            raise ValueError("invalid run outcome")
        if result.get("success") is True:
            return RunOutcome.CLEAN
        if result.get("success") is False:
            return RunOutcome.FAILED
    raise ValueError("invalid run outcome")


def completed_run(result: Mapping[str, object]) -> bool:
    return run_outcome(result) in {RunOutcome.CLEAN, RunOutcome.DEGRADED}
