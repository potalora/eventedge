"""Absolute model budgets and killable native transport work.

Transport timeouts bound inactivity. A separately reaped process also bounds
DNS, streamed trickle responses and SDK work against elapsed wall time.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import math
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time

_CURRENT_MODEL_DEADLINE: ContextVar[float | None] = ContextVar("model_deadline", default=None)
_CURRENT_WORKER_POOL: ContextVar["TransportWorkerPool | None"] = ContextVar("transport_worker_pool", default=None)
DEFAULT_MODEL_BUDGET_S = 2400.0
MODEL_CALL_CAP_S = 120.0
DEFAULT_WORKER_POOL_SIZE = 32


def model_phase_budget_seconds(config: dict) -> float:
    return DEFAULT_MODEL_BUDGET_S


def graphics_call_seconds(config: dict) -> float:
    return MODEL_CALL_CAP_S


class ModelDeadlineExceeded(TimeoutError):
    """The shared model phase or individual transport exhausted its allowance."""


class ModelTransportError(RuntimeError):
    def __init__(self, reason: str, status_code: int | None = None, *, error_metadata=None):
        from tradingagents.strategies.transport_worker import validate_error_metadata
        self.error_metadata = validate_error_metadata(error_metadata)
        self.status_code = status_code
        super().__init__(f"Model transport failed ({reason}, status={status_code})")


class _PooledWorker:
    """One persistent transport worker process communicating via NDJSON lines."""

    def __init__(self, worker_path: Path) -> None:
        self.process = subprocess.Popen(
            [sys.executable, str(worker_path), "--serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        self._buffer = bytearray()
        assert self.process.stdin is not None and self.process.stdout is not None
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)

    def alive(self) -> bool:
        return self.process.poll() is None

    def kill(self) -> None:
        try:
            if self.process.poll() is None:
                self.process.kill()
        except OSError:
            pass
        try:
            self.process.wait(timeout=2)
        except Exception:
            pass
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def roundtrip(self, payload: dict, deadline: float) -> dict:
        assert self.process.stdin is not None and self.process.stdout is not None
        stdin_fd = self.process.stdin.fileno()
        stdout_fd = self.process.stdout.fileno()
        encoded = (json.dumps(payload) + "\n").encode("utf-8")
        written = 0
        while written < len(encoded):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("transport_deadline_exhausted")
            _, writable, _ = select.select([], [stdin_fd], [], remaining)
            if not writable:
                raise TimeoutError("transport_deadline_exhausted")
            try:
                written += os.write(stdin_fd, encoded[written:])
            except BlockingIOError:
                continue
            except OSError:
                raise RuntimeError("transport_worker_failed") from None
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[:newline])
                del self._buffer[:newline + 1]
                try:
                    return json.loads(line)
                except ValueError:
                    raise RuntimeError("transport_worker_failed") from None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("transport_deadline_exhausted")
            readable, _, _ = select.select([stdout_fd], [], [], remaining)
            if not readable:
                raise TimeoutError("transport_deadline_exhausted")
            try:
                chunk = os.read(stdout_fd, 65536)
            except BlockingIOError:
                continue
            except OSError:
                raise RuntimeError("transport_worker_failed") from None
            if not chunk:
                raise RuntimeError("transport_worker_failed")
            self._buffer.extend(chunk)


class TransportWorkerPool:
    """Bounded reusable worker process pool per phase with SIGKILL timeout enforcement."""

    def __init__(self, max_workers: int = DEFAULT_WORKER_POOL_SIZE) -> None:
        self._max_workers = max_workers
        self._worker_path = Path(__file__).with_name("transport_worker.py")
        self._lock = threading.Lock()
        self._semaphore = threading.BoundedSemaphore(max_workers)
        self._idle: deque[_PooledWorker] = deque()
        self._active: set[_PooledWorker] = set()
        self._closed = False
        self.spawn_count = 0

    def _checkout(self) -> _PooledWorker:
        with self._lock:
            if self._closed:
                raise RuntimeError("transport_pool_closed")
            while self._idle:
                candidate = self._idle.popleft()
                if candidate.alive():
                    self._active.add(candidate)
                    return candidate
                candidate.kill()
        worker = _PooledWorker(self._worker_path)
        with self._lock:
            if self._closed:
                worker.kill()
                raise RuntimeError("transport_pool_closed")
            self._active.add(worker)
            self.spawn_count += 1
        return worker

    def _checkin(self, worker: _PooledWorker) -> None:
        should_kill = False
        with self._lock:
            self._active.discard(worker)
            if self._closed or not worker.alive() or len(self._idle) >= self._max_workers:
                should_kill = True
            else:
                self._idle.append(worker)
        if should_kill:
            worker.kill()

    def _discard(self, worker: _PooledWorker) -> None:
        with self._lock:
            self._active.discard(worker)
        worker.kill()

    def execute(self, payload: dict, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._semaphore.acquire(timeout=remaining):
            raise TimeoutError("transport_deadline_exhausted")
        worker: _PooledWorker | None = None
        try:
            worker = self._checkout()
            result = worker.roundtrip(payload, deadline)
            self._checkin(worker)
            worker = None
            return result
        except TimeoutError:
            if worker is not None:
                self._discard(worker)
                worker = None
            raise TimeoutError("transport_deadline_exhausted") from None
        except Exception:
            if worker is not None:
                self._discard(worker)
                worker = None
            raise RuntimeError("transport_worker_failed") from None
        finally:
            self._semaphore.release()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            idle = list(self._idle)
            self._idle.clear()
        for worker in idle:
            worker.kill()


@contextmanager
def model_budget(deadline: float):
    if not math.isfinite(deadline):
        raise ValueError("invalid model deadline")
    outer = _CURRENT_MODEL_DEADLINE.get()
    token = _CURRENT_MODEL_DEADLINE.set(min(outer, deadline) if outer is not None else deadline)
    own_pool = _CURRENT_WORKER_POOL.get() is None
    pool = TransportWorkerPool() if own_pool else None
    pool_token = _CURRENT_WORKER_POOL.set(pool) if own_pool else None
    try:
        yield
    finally:
        if pool_token is not None:
            _CURRENT_WORKER_POOL.reset(pool_token)
        if pool is not None:
            pool.close()
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

    Model calls inside a `model_budget` reuse a bounded per-phase worker pool
    (enforcing hard wall-clock SIGKILL + clean worker respawn on timeout).
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise TimeoutError("transport_deadline_exhausted")
    pool = _CURRENT_WORKER_POOL.get()
    if pool is not None and payload.get("kind") == "model":
        return pool.execute(payload, timeout)
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
