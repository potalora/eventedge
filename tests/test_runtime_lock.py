from __future__ import annotations

import os
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

_INHERITED_BORROWER = """
import sys
from pathlib import Path
from tradingagents.strategies.orchestration.runtime_lock import runtime_lock

with runtime_lock(
    Path(sys.argv[1]),
    exclusive=True,
    inherited_fd=int(sys.argv[2]),
    inherited_exclusive=True,
) as lock:
    assert lock.inherited is True
print("borrowed", flush=True)
"""

_INHERITED_HOLDER = """
import sys
from pathlib import Path
from tradingagents.strategies.orchestration.runtime_lock import runtime_lock

with runtime_lock(
    Path(sys.argv[1]),
    exclusive=True,
    inherited_fd=int(sys.argv[2]),
    inherited_exclusive=True,
):
    print("borrowed", flush=True)
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


def test_runtime_lock_detects_lockfile_replacement_while_held(tmp_path: Path) -> None:
    from tradingagents.strategies.orchestration.runtime_lock import (
        RuntimeLockInvalid,
        runtime_lock,
    )

    lock_path = tmp_path / "eventedge.lock"
    with pytest.raises(RuntimeLockInvalid, match="identity changed"):
        with runtime_lock(lock_path, exclusive=True):
            lock_path.rename(tmp_path / "old-eventedge.lock")
            lock_path.touch()


def test_canonical_lock_path_resolves_main_repo_from_linked_worktree(
    tmp_path: Path,
) -> None:
    from tradingagents.strategies.orchestration.runtime_lock import (
        canonical_runtime_lock_path,
    )

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"], check=True
    )
    (repo / "tracked.txt").write_text("tracked\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True
    )
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(linked), "--detach"],
        check=True,
        capture_output=True,
    )

    assert canonical_runtime_lock_path(linked) == (
        repo / "data" / "operational" / "eventedge-runtime.lock"
    ).resolve()


def test_canonical_lock_rejects_symlinked_lock_or_parent(tmp_path: Path) -> None:
    from tradingagents.strategies.orchestration.runtime_lock import (
        RuntimeLockInvalid,
        canonical_runtime_lock_path,
    )

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    operational = repo / "data" / "operational"
    operational.mkdir(parents=True)
    lock_path = operational / "eventedge-runtime.lock"
    lock_path.symlink_to(outside / "external.lock")

    with pytest.raises(RuntimeLockInvalid, match="canonical runtime lock"):
        canonical_runtime_lock_path(repo)

    lock_path.unlink()
    operational.rmdir()
    (repo / "data").rmdir()
    (repo / "data").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeLockInvalid, match="canonical runtime lock"):
        canonical_runtime_lock_path(repo)


def test_inherited_fd_is_inode_verified_and_never_unlocks_parent(tmp_path: Path) -> None:
    from tradingagents.strategies.orchestration.runtime_lock import (
        RuntimeLockBusy,
        RuntimeLockInvalid,
        runtime_lock,
    )

    lock_path = tmp_path / "eventedge.lock"
    other_path = tmp_path / "other.lock"
    other_path.touch()
    wrong_fd = os.open(other_path, os.O_RDONLY)
    try:
        with pytest.raises(RuntimeLockInvalid, match="inherited runtime lock"):
            with runtime_lock(
                lock_path,
                exclusive=True,
                inherited_fd=wrong_fd,
                inherited_exclusive=True,
            ):
                raise AssertionError("unreachable")
    finally:
        os.close(wrong_fd)

    with runtime_lock(lock_path, exclusive=True) as parent:
        with runtime_lock(
            lock_path,
            exclusive=True,
            inherited_fd=parent.fd,
            inherited_exclusive=True,
        ) as child:
            assert child.inherited is True
        with pytest.raises(RuntimeLockBusy, match="runtime lock is busy"):
            with runtime_lock(lock_path, exclusive=False):
                raise AssertionError("unreachable")

    with runtime_lock(lock_path, exclusive=True):
        pass


def test_inherited_fd_rejects_lock_mode_mismatch(tmp_path: Path) -> None:
    from tradingagents.strategies.orchestration.runtime_lock import (
        RuntimeLockInvalid,
        runtime_lock,
    )

    lock_path = tmp_path / "eventedge.lock"
    with runtime_lock(lock_path, exclusive=False) as parent:
        with pytest.raises(RuntimeLockInvalid, match="mode"):
            with runtime_lock(
                lock_path,
                exclusive=True,
                inherited_fd=parent.fd,
                inherited_exclusive=False,
            ):
                raise AssertionError("unreachable")


def test_inherited_fd_handoff_crosses_process_without_unlocking_parent(
    tmp_path: Path,
) -> None:
    from tradingagents.strategies.orchestration.runtime_lock import (
        RuntimeLockBusy,
        runtime_lock,
    )

    lock_path = tmp_path / "eventedge.lock"
    with runtime_lock(lock_path, exclusive=True) as parent:
        child = subprocess.run(
            [sys.executable, "-c", _INHERITED_BORROWER, str(lock_path), str(parent.fd)],
            capture_output=True,
            text=True,
            pass_fds=(parent.fd,),
            check=True,
        )
        assert child.stdout.strip() == "borrowed"
        with pytest.raises(RuntimeLockBusy, match="runtime lock is busy"):
            with runtime_lock(lock_path, exclusive=False):
                raise AssertionError("unreachable")


def test_unlocked_inherited_fd_acquires_lock_across_processes(tmp_path: Path) -> None:
    from tradingagents.strategies.orchestration.runtime_lock import (
        RuntimeLockBusy,
        runtime_lock,
    )

    lock_path = tmp_path / "eventedge.lock"
    lock_path.touch()
    inherited_fd = os.open(lock_path, os.O_RDWR)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _INHERITED_HOLDER,
            str(lock_path),
            str(inherited_fd),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=(inherited_fd,),
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "borrowed"
        with pytest.raises(RuntimeLockBusy, match="runtime lock is busy"):
            with runtime_lock(lock_path, exclusive=True):
                raise AssertionError("unreachable")
    finally:
        assert child.stdin is not None
        child.stdin.write("release\n")
        child.stdin.flush()
        stdout, stderr = child.communicate(timeout=5)
        os.close(inherited_fd)
        assert child.returncode == 0, (stdout, stderr)

    with runtime_lock(lock_path, exclusive=True):
        pass
