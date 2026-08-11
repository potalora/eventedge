"""Non-blocking process coordination for EventEdge operational entrypoints."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


_MAX_LOCK_PATH = 4_096


class RuntimeLockBusy(RuntimeError):
    """The operational runtime lock is already held incompatibly."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = str(lock_path)[:_MAX_LOCK_PATH]
        super().__init__("runtime lock is busy")


@contextmanager
def runtime_lock(lock_path: Path, *, exclusive: bool) -> Iterator[None]:
    """Acquire one non-blocking shared or exclusive process lock."""
    target = Path(lock_path)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    with target.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeLockBusy(target) from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
