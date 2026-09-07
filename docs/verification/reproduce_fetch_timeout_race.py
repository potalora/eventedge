"""Historical PR #37 reproducer; requires pre-fix commit 897be39.

Do not run as acceptance for the combined release containing PR #38. The fixed
implementation is covered by tests/test_fetch_timeout_boundary.py instead.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FuturesTimeout
from unittest.mock import patch

from tradingagents.strategies.orchestration.multi_strategy_engine import (
    _gather_with_timeout,
)


class SyntheticExecutor:
    """Return one real Future without running a worker thread."""

    def __init__(self, future: Future):
        self.future = future
        self.shutdown_call = None

    def submit(self, function, *args):
        del function, args
        return self.future

    def shutdown(self, *, wait, cancel_futures):
        self.shutdown_call = (wait, cancel_futures)


def reproduce(name: str, *, value=None, error: Exception | None = None):
    future = Future()
    executor = SyntheticExecutor(future)

    def complete_between_timeout_and_handler(futures, timeout):
        assert list(futures) == [future]
        assert timeout == 0.001
        if error is None:
            future.set_result(value)
        else:
            future.set_exception(error)
        raise FuturesTimeout("synthetic timeout snapshot")
        yield  # pragma: no cover - makes this an iterator like as_completed()

    with (
        patch(
            "concurrent.futures.ThreadPoolExecutor",
            return_value=executor,
        ),
        patch(
            "concurrent.futures.as_completed",
            new=complete_between_timeout_and_handler,
        ),
    ):
        output = _gather_with_timeout(
            {name: (lambda: "executor never invokes this", ())},
            timeout_s=0.001,
        )

    assert future.done()
    assert executor.shutdown_call == (False, True)
    return future, output


def main() -> None:
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")

    success_value = {"quotes": [{"ticker": "SPY", "close": 100.0}]}
    success, success_output = reproduce("successful", value=success_value)
    print("successful future result:", success.result())
    print("successful helper output:", success_output)
    assert success_output == {"successful": {}}

    failure, failure_output = reproduce(
        "failed", error=RuntimeError("provider returned HTTP 500")
    )
    try:
        failure.result()
    except RuntimeError as exc:
        print("failed future exception:", f"{type(exc).__name__}: {exc}")
    print("failed helper output:", failure_output)
    assert failure_output == {"failed": {}}

    print("REPRODUCED: both completed outcomes were discarded as healthy empty data")


if __name__ == "__main__":
    main()
