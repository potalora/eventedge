"""Absolute model budgets and killable native transport work.

Transport timeouts bound inactivity. A separately reaped process also bounds
DNS, streamed trickle responses and SDK work against elapsed wall time.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import math
from pathlib import Path
import subprocess
import sys
import time

_CURRENT_MODEL_DEADLINE: ContextVar[float | None] = ContextVar("model_deadline", default=None)
DEFAULT_MODEL_BUDGET_S = 2400.0
MODEL_CALL_CAP_S = 120.0


FULL_REGULATION_GRAPHICS_POLICY = "full_regulation_graphics_v1"


def _full_graphics_policy(config: dict) -> bool:
    """One frozen prospective timing contract; absent policy retains history."""
    if not isinstance(config, dict):
        raise ValueError("invalid_model_runtime_policy")
    ar = config.get("autoresearch", {})
    if not isinstance(ar, dict):
        raise ValueError("invalid_model_runtime_policy")
    if "model_runtime_policy" not in ar:
        return False
    execution = config.get("execution", {})
    temperature = ar.get("llm_temperature", 0.0)
    if (ar.get("model_runtime_policy") != FULL_REGULATION_GRAPHICS_POLICY
            or ar.get("regulation_retrieval_policy") != "verified_government_copy_v3"
            or ar.get("llm_billing_mode") != "chatgpt_plan"
            or ar.get("autoresearch_model") != "gpt-6-luna"
            or ar.get("llm_effort") != "high"
            or ar.get("thesis_model") != "gpt-6.1-sol"
            or ar.get("thesis_effort") != "high"
            or type(temperature) is not float
            or temperature != 0.0
            or not isinstance(execution, dict) or execution.get("mode") != "paper"
            or execution.get("execution_enabled") is not False):
        raise ValueError("invalid_model_runtime_policy")
    return True


def model_phase_budget_seconds(config: dict) -> float:
    return 9000.0 if _full_graphics_policy(config) else DEFAULT_MODEL_BUDGET_S


def graphics_call_seconds(config: dict) -> float:
    return 240.0 if _full_graphics_policy(config) else MODEL_CALL_CAP_S


class ModelDeadlineExceeded(TimeoutError):
    """The shared model phase or individual transport exhausted its allowance."""


class ModelTransportError(RuntimeError):
    def __init__(self, reason: str, status_code: int | None = None, *, error_metadata=None):
        from tradingagents.strategies.transport_worker import validate_error_metadata
        self.error_metadata = validate_error_metadata(error_metadata)
        self.status_code = status_code
        super().__init__(f"Model transport failed ({reason}, status={status_code})")


@contextmanager
def model_budget(deadline: float):
    if not math.isfinite(deadline):
        raise ValueError("invalid model deadline")
    outer = _CURRENT_MODEL_DEADLINE.get()
    token = _CURRENT_MODEL_DEADLINE.set(min(outer, deadline) if outer is not None else deadline)
    try:
        yield
    finally:
        _CURRENT_MODEL_DEADLINE.reset(token)


def current_model_deadline() -> float | None:
    return _CURRENT_MODEL_DEADLINE.get()


def model_timeout(maximum: float = MODEL_CALL_CAP_S) -> float:
    deadline = current_model_deadline()
    remaining = maximum if deadline is None else min(maximum, deadline - time.monotonic())
    if remaining <= 0:
        raise ModelDeadlineExceeded("model_deadline_exhausted")
    return remaining


def model_backoff(delay: float) -> None:
    if delay >= model_timeout(float("inf")):
        raise ModelDeadlineExceeded("model_deadline_exhausted")
    time.sleep(delay)
    model_timeout()


def bounded_transport(payload: dict, timeout: float) -> dict:
    """Send credentials/evidence only through private pipes; reap on every exit.

    The worker has no persistent state, retries or descendants. Never forward
    its stderr or raw provider errors: these can include request credentials.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise TimeoutError("transport_deadline_exhausted")
    deadline = time.monotonic() + timeout
    worker = Path(__file__).with_name("transport_worker.py")
    process = subprocess.Popen([sys.executable, str(worker)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        stdout, _ = process.communicate(json.dumps(payload).encode(), timeout=max(.001, deadline - time.monotonic()))
        if process.returncode:
            raise RuntimeError("transport_worker_failed")
        return json.loads(stdout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise TimeoutError("transport_deadline_exhausted") from None
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def bounded_model_phase(function):
    """Give standalone callers the same aggregate ceiling as the daily pipeline."""
    @wraps(function)
    def bounded(*args, **kwargs):
        allowance = model_phase_budget_seconds(getattr(args[0], "config", {}) if args else {})
        deadline = current_model_deadline()
        with model_budget(deadline if deadline is not None else time.monotonic() + allowance):
            return function(*args, **kwargs)
    return bounded
