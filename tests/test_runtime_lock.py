from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest


_HOLDER = """
import sys
from pathlib import Path
from tradingagents.strategies.orchestration.runtime_lock import runtime_lock

with runtime_lock(Path(sys.argv[1]), exclusive=sys.argv[2] == "exclusive"):
    print("locked", flush=True)
    sys.stdin.readline()
"""


def _holder(lock_path: Path, *, exclusive: bool) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLDER,
            str(lock_path),
            "exclusive" if exclusive else "shared",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "locked"
    return process


def _release(process: subprocess.Popen[str]) -> None:
    assert process.stdin is not None
    process.stdin.write("release\n")
    process.stdin.flush()
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, (stdout, stderr)


def test_two_shared_runtime_locks_can_coexist(tmp_path: Path) -> None:
    from tradingagents.strategies.orchestration.runtime_lock import runtime_lock

    lock_path = tmp_path / "operational" / "eventedge.lock"
    lock_path.parent.mkdir()
    holder = _holder(lock_path, exclusive=False)
    try:
        with runtime_lock(lock_path, exclusive=False):
            assert holder.poll() is None
    finally:
        _release(holder)


def test_exclusive_lock_rejects_concurrent_shared_nonblocking(tmp_path: Path) -> None:
    from tradingagents.strategies.orchestration.runtime_lock import (
        RuntimeLockBusy,
        runtime_lock,
    )

    lock_path = tmp_path / "eventedge.lock"
    holder = _holder(lock_path, exclusive=False)
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeLockBusy, match="runtime lock is busy"):
            with runtime_lock(lock_path, exclusive=True):
                raise AssertionError("unreachable")
        assert time.monotonic() - started < 0.5
    finally:
        _release(holder)


def test_shared_lock_rejects_concurrent_exclusive_nonblocking(tmp_path: Path) -> None:
    from tradingagents.strategies.orchestration.runtime_lock import (
        RuntimeLockBusy,
        runtime_lock,
    )

    lock_path = tmp_path / "eventedge.lock"
    holder = _holder(lock_path, exclusive=True)
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeLockBusy, match="runtime lock is busy"):
            with runtime_lock(lock_path, exclusive=False):
                raise AssertionError("unreachable")
        assert time.monotonic() - started < 0.5
    finally:
        _release(holder)


def test_runtime_lock_releases_after_exception(tmp_path: Path) -> None:
    from tradingagents.strategies.orchestration.runtime_lock import runtime_lock

    lock_path = tmp_path / "eventedge.lock"
    with pytest.raises(RuntimeError, match="boom"):
        with runtime_lock(lock_path, exclusive=True):
            raise RuntimeError("boom")

    holder = _holder(lock_path, exclusive=True)
    _release(holder)
