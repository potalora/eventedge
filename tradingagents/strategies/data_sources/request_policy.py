"""One bounded acquisition budget for HTTP and SDK provider operations.

A deadline is absolute monotonic time. Pacing, Retry-After and transient retries
consume that deadline; no subsequent operation starts after it. SDKs without a
timeout parameter are cooperatively bounded, never forcibly interrupted.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import math
import random
import re
import threading
import time
from typing import Callable

import requests

from .fetch_errors import SourceFetchError, source_fetch_error

PROVIDER_LIMITS = {
    "alpaca": ((200, 60),),
    "courtlistener": ((5, 60), (50, 3600), (125, 86400)),
    "edgar": ((1, 0.1),),
    "noaa": ((5, 1), (10000, 86400)),
    "finnhub": ((60, 60),),
    "regulations": ((1, 0.5),),
    "usaspending": ((1, 0.5),),
}
_RETRY_STATUS = {429, 500, 502, 503, 504}
_LOCK = threading.Lock()
_HISTORY: dict[tuple, deque] = {}
_CURRENT: ContextVar[object] = ContextVar("source_acquisition_budget", default=None)


@dataclass
class _Budget:
    provider: str
    deadline: float
    clock: Callable
    sleep: Callable
    random_fn: Callable
    max_attempts: int
    limits: tuple
    diagnostics: list


@contextmanager
def provider_budget(provider, deadline, *, clock=None, sleep=None,
                    random_fn=None, max_attempts=3, limits=None, diagnostics=None):
    """Scope provider operations to a deadline and yield safe diagnostics."""
    if not math.isfinite(deadline) or not 1 <= max_attempts <= 5:
        raise ValueError("invalid provider acquisition budget")
    budget = _Budget(provider, deadline, clock or time.monotonic, sleep or time.sleep,
                     random_fn or random.random, max_attempts,
                     tuple(PROVIDER_LIMITS.get(provider, ()) if limits is None else limits),
                     diagnostics if diagnostics is not None else [])
    token = _CURRENT.set(budget)
    try:
        yield budget.diagnostics
    finally:
        _CURRENT.reset(token)


def current_provider_deadline(provider):
    """Return the active absolute deadline, for adapters with existing policies."""
    budget = _CURRENT.get()
    return budget.deadline if budget is not None and budget.provider == provider else None


def provider_timeout(provider, maximum=15):
    """Clip an SDK's exposed inactivity timeout to its acquisition budget."""
    budget = _CURRENT.get()
    remaining = budget.deadline - budget.clock() if budget is not None and budget.provider == provider else maximum
    if remaining <= 0:
        raise SourceFetchError("Provider acquisition deadline exhausted", reason_code="timeout")
    return min(maximum, remaining)


def _safe_identity(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) else "unknown"


def _wait(budget, delay):
    remaining = budget.deadline - budget.clock()
    if remaining <= 0 or delay >= remaining:
        raise SourceFetchError("Provider acquisition deadline exhausted", reason_code="timeout")
    if delay > 0:
        budget.sleep(delay)
    if budget.clock() >= budget.deadline:
        raise SourceFetchError("Provider acquisition deadline exhausted", reason_code="timeout")


def _slot(budget):
    while True:
        _wait(budget, 0)
        with _LOCK:
            now = budget.clock()
            histories = []
            delay = 0.0
            for count, window in budget.limits:
                if count < 1 or window <= 0:
                    raise ValueError("invalid provider rolling limit")
                history = _HISTORY.setdefault((budget.provider, budget.clock, count, window), deque())
                while history and history[0] <= now - window:
                    history.popleft()
                if len(history) >= count:
                    delay = max(delay, history[0] + window - now)
                histories.append(history)
            if delay <= 0:
                for history in histories:
                    history.append(now)
                return
        _wait(budget, delay)


def _retry_after(response):
    value = getattr(response, "headers", {}).get("Retry-After")
    if not isinstance(value, str):
        return 0.0
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    return max(0.0, delay) if math.isfinite(delay) else 0.0


def _run(provider, operation, attempt):
    budget = _CURRENT.get()
    if budget is None or budget.provider != provider:
        with provider_budget(provider, time.monotonic() + 60):
            return _run(provider, operation, attempt)
    error = None
    attempts = 0
    response = None
    for index in range(budget.max_attempts):
        try:
            _slot(budget)
            attempts += 1
            response = None
            response = attempt(budget.deadline - budget.clock())
            status = getattr(response, "status_code", None)
            if isinstance(status, int) and not 200 <= status < 300:
                raise SourceFetchError("Provider HTTP request failed", reason_code="http_error", http_status=status)
            budget.diagnostics.append({"provider": _safe_identity(provider), "operation": _safe_identity(operation),
                                       "reason_code": None, "http_status": status if isinstance(status, int) else None,
                                       "attempts": attempts, "recovered": attempts > 1})
            return response
        except Exception as exc:
            error = source_fetch_error("Provider acquisition failed", exc)
            response = getattr(exc, "response", response)
            retry_after = _retry_after(response)
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass  # Cleanup must not replace the provider failure.
            retryable = error.reason_code in {"timeout", "transport_error"} or (
                error.reason_code == "http_error" and error.http_status in _RETRY_STATUS)
            if not retryable or index + 1 >= budget.max_attempts:
                break
            delay = max(0.5 * 2 ** index + 0.25 * budget.random_fn(), retry_after)
            try:
                _wait(budget, delay)
            except SourceFetchError:
                break
    error = error or SourceFetchError("Provider acquisition deadline exhausted", reason_code="timeout")
    error.attempts = attempts
    budget.diagnostics.append({"provider": _safe_identity(provider), "operation": _safe_identity(operation),
                               "reason_code": error.reason_code, "http_status": error.http_status,
                               "attempts": attempts, "recovered": False})
    raise error from None


def provider_request(provider, method, url, *, operation=None, transport=None, **kwargs):
    """Request with bounded retries, retaining requests.get/post monkeypatches.

    transport optionally supplies a session's bound get/post for providers with
    required transport adapters. It has the same (url, **kwargs) protocol.
    """
    method = method.lower()
    request = transport or getattr(requests, method)
    def attempt(remaining):
        options = dict(kwargs)
        timeout = options.get("timeout", 15)
        if isinstance(timeout, tuple):
            # Both connect and read allocations together fit remaining budget.
            total = sum(timeout)
            options["timeout"] = tuple(max(0.001, min(value, remaining * value / total)) for value in timeout)
        else:
            options["timeout"] = max(0.001, min(timeout or 15, remaining))
        return request(url, **options)
    return _run(provider, operation or method, attempt)


def provider_call(provider, operation, callable, *, maximum_seconds=None):
    """Retry one SDK operation within its cap and any inherited deadline.

    SDK callbacks remain cooperative. The operation cap limits subsequent
    retries; it does not forcibly interrupt an already running SDK call.
    """
    if maximum_seconds is None:
        return _run(provider, operation, lambda remaining: callable())
    if (isinstance(maximum_seconds, bool) or not isinstance(maximum_seconds, (int, float))
            or not math.isfinite(maximum_seconds) or maximum_seconds <= 0):
        raise ValueError("invalid SDK operation budget")
    parent = _CURRENT.get()
    if parent is not None and parent.provider == provider:
        deadline = min(parent.deadline, parent.clock() + maximum_seconds)
        options = dict(clock=parent.clock, sleep=parent.sleep, random_fn=parent.random_fn,
                       max_attempts=parent.max_attempts, limits=parent.limits,
                       diagnostics=parent.diagnostics)
    else:
        deadline = time.monotonic() + min(60, maximum_seconds)
        options = {}
    with provider_budget(provider, deadline, **options):
        return _run(provider, operation, lambda remaining: callable())
