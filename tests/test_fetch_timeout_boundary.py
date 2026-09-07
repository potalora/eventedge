"""Deterministic timeout-boundary tests for the shared source fetch fan-out."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

from tradingagents.strategies.orchestration.multi_strategy_engine import (
    _gather_with_timeout,
)


@dataclass(frozen=True)
class _Completion:
    value: Any = None
    error: Exception | None = None

    def apply(self, future: Future) -> None:
        if self.error is None:
            future.set_result(self.value)
        else:
            future.set_exception(self.error)


class _SyntheticExecutor:
    """Supply real futures while keeping the timeout boundary deterministic."""

    def __init__(self) -> None:
        self.futures: list[Future] = []
        self.submissions: list[tuple[Callable[..., Any], tuple[Any, ...]]] = []
        self.shutdown_calls: list[tuple[bool, bool]] = []

    def submit(self, function: Callable[..., Any], *args: Any) -> Future:
        future: Future = _NonBlockingFuture()
        self.futures.append(future)
        self.submissions.append((function, args))
        return future

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        self.shutdown_calls.append((wait, cancel_futures))


class _NonBlockingFuture(Future):
    """Fail fast if the gather tries to consume a snapshot-pending future."""

    def result(self, timeout: float | None = None) -> Any:
        if not self.done():
            raise AssertionError("result() called for a pending future")
        return super().result(timeout=timeout)


def _run_boundary_scenario(
    fetches: dict[str, tuple[Callable[..., Any], tuple[Any, ...]]],
    *,
    completed_at_snapshot: dict[int, _Completion],
    completed_after_snapshot: dict[int, _Completion] | None = None,
    timeout_s: float = 0.125,
) -> tuple[dict[str, Any], _SyntheticExecutor, list[Future]]:
    """Drive both the old iterator and new partition-style timeout designs.

    ``wait`` receives a stable done/not-done snapshot. Futures in
    ``completed_after_snapshot`` finish immediately after that partition is
    decided, proving that later state changes cannot rewrite the snapshot.
    ``as_completed`` models the old race by applying all completions just before
    its timeout is raised.
    """
    executor = _SyntheticExecutor()
    completed_after_snapshot = completed_after_snapshot or {}
    wait_calls: list[tuple[tuple[Future, ...], float]] = []

    def synthetic_wait(futures, timeout):
        ordered = tuple(futures)
        wait_calls.append((ordered, timeout))
        for index, completion in completed_at_snapshot.items():
            completion.apply(executor.futures[index])

        done = {executor.futures[index] for index in completed_at_snapshot}
        not_done = set(ordered) - done

        # These futures complete after the returned partition has been fixed.
        for index, completion in completed_after_snapshot.items():
            assert executor.futures[index] in not_done
            completion.apply(executor.futures[index])
        return done, not_done

    def synthetic_as_completed(futures, timeout):
        assert tuple(futures) == tuple(executor.futures)
        assert timeout == timeout_s
        for index, completion in {
            **completed_at_snapshot,
            **completed_after_snapshot,
        }.items():
            completion.apply(executor.futures[index])
        raise FuturesTimeout("synthetic timeout boundary")
        yield  # pragma: no cover - preserve as_completed's iterator protocol

    with (
        patch("concurrent.futures.ThreadPoolExecutor", return_value=executor),
        patch("concurrent.futures.wait", new=synthetic_wait),
        patch("concurrent.futures.as_completed", new=synthetic_as_completed),
    ):
        output = _gather_with_timeout(fetches, timeout_s=timeout_s, max_workers=3)

    # The current implementation uses as_completed and the fixed implementation
    # uses wait. Exactly one deadline primitive should own the snapshot.
    assert len(wait_calls) <= 1
    if wait_calls:
        waited_futures, waited_timeout = wait_calls[0]
        assert waited_futures == tuple(executor.futures)
        assert waited_timeout == timeout_s
    return output, executor, executor.futures


def test_completed_at_timeout_snapshot_keeps_success_and_error() -> None:
    success = {"quotes": [{"ticker": "SPY", "close": 100.0}]}
    fetches = {
        "successful": (lambda: success, ()),
        "failed": (lambda: None, ()),
    }

    output, executor, futures = _run_boundary_scenario(
        fetches,
        completed_at_snapshot={
            0: _Completion(value=success),
            1: _Completion(error=RuntimeError("provider returned HTTP 500")),
        },
    )

    assert output == {
        "successful": success,
        "failed": {"error": "RuntimeError: provider returned HTTP 500"},
    }
    assert futures[0].result() is success
    assert isinstance(futures[1].exception(), RuntimeError)
    assert executor.shutdown_calls == [(False, True)]


def test_pending_snapshot_stays_timed_out_after_late_completion() -> None:
    fetches = {
        "late_success": (lambda marker: marker, ("success",)),
        "late_error": (lambda marker: marker, ("error",)),
    }

    output, executor, futures = _run_boundary_scenario(
        fetches,
        completed_at_snapshot={},
        completed_after_snapshot={
            0: _Completion(value={"v": "too late"}),
            1: _Completion(error=ValueError("late provider failure")),
        },
    )

    assert output == {
        "late_success": {"error": "timeout after 0.125s"},
        "late_error": {"error": "timeout after 0.125s"},
    }
    assert futures[0].result() == {"v": "too late"}
    assert str(futures[1].exception()) == "late provider failure"
    assert executor.shutdown_calls == [(False, True)]


def test_mixed_snapshot_preserves_source_order_and_never_runs_fetches() -> None:
    def must_not_run(label: str) -> dict[str, str]:
        raise AssertionError(f"synthetic executor unexpectedly ran {label}")

    fetches = {
        "ready": (must_not_run, ("ready",)),
        "empty": (must_not_run, ("empty",)),
        "pending": (must_not_run, ("pending",)),
        "broken": (must_not_run, ("broken",)),
    }

    output, executor, _ = _run_boundary_scenario(
        fetches,
        completed_at_snapshot={
            0: _Completion(value={"v": "ready"}),
            1: _Completion(value={}),
            3: _Completion(error=LookupError("missing payload")),
        },
    )

    assert list(output) == ["ready", "empty", "pending", "broken"]
    assert output == {
        "ready": {"v": "ready"},
        "empty": {},
        "pending": {"error": "timeout after 0.125s"},
        "broken": {"error": "LookupError: missing payload"},
    }
    assert executor.submissions == [
        (must_not_run, ("ready",)),
        (must_not_run, ("empty",)),
        (must_not_run, ("pending",)),
        (must_not_run, ("broken",)),
    ]
    assert executor.shutdown_calls == [(False, True)]


def test_empty_input_does_not_construct_an_executor() -> None:
    with patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=AssertionError("executor should not be constructed"),
    ):
        assert _gather_with_timeout({}, timeout_s=0.125) == {}
