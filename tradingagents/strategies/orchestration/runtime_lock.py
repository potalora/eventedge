"""Non-blocking process coordination for EventEdge operational entrypoints."""

from __future__ import annotations

import fcntl
import os
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


_MAX_LOCK_PATH = 4_096


class RuntimeLockBusy(RuntimeError):
    """The operational runtime lock is already held incompatibly."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = str(lock_path)[:_MAX_LOCK_PATH]
        super().__init__("runtime lock is busy")


class RuntimeLockInvalid(RuntimeError):
    """An inherited runtime lock cannot be proven to match the canonical lock."""


@dataclass(frozen=True)
class RuntimeLockHandle:
    path: Path
    fd: int
    exclusive: bool
    inherited: bool


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _reject_existing_symlinks(root: Path, target: Path) -> None:
    current = root
    for component in target.relative_to(root).parts:
        current /= component
        try:
            entry = os.lstat(current)
        except FileNotFoundError:
            return
        except OSError as error:
            raise RuntimeLockInvalid("canonical runtime lock is unavailable") from error
        if stat.S_ISLNK(entry.st_mode):
            raise RuntimeLockInvalid("canonical runtime lock path is invalid")


def _open_parent_directory(target: Path, *, create: bool) -> int:
    """Open the lock parent through no-follow directory-relative traversal."""
    parent = target.parent
    current_fd = os.open("/", _directory_open_flags())
    try:
        for component in parent.parts[1:]:
            child_fd: int | None = None
            try:
                child_fd = os.open(
                    component, _directory_open_flags(), dir_fd=current_fd
                )
            except FileNotFoundError:
                if not create:
                    raise RuntimeLockInvalid("inherited runtime lock is invalid")
                try:
                    os.mkdir(component, 0o755, dir_fd=current_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(
                    component, _directory_open_flags(), dir_fd=current_fd
                )
            try:
                child_stat = os.fstat(child_fd)
                relative_stat = os.stat(
                    component, dir_fd=current_fd, follow_symlinks=False
                )
                if (
                    not stat.S_ISDIR(child_stat.st_mode)
                    or stat.S_ISLNK(relative_stat.st_mode)
                    or (child_stat.st_dev, child_stat.st_ino)
                    != (relative_stat.st_dev, relative_stat.st_ino)
                ):
                    raise RuntimeLockInvalid(
                        "canonical runtime lock path is invalid"
                    )
            except BaseException:
                os.close(child_fd)
                raise
            os.close(current_fd)
            current_fd = child_fd
        parent_stat = os.stat(parent, follow_symlinks=False)
        fd_stat = os.fstat(current_fd)
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or (parent_stat.st_dev, parent_stat.st_ino)
            != (fd_stat.st_dev, fd_stat.st_ino)
        ):
            raise RuntimeLockInvalid("canonical runtime lock path is invalid")
        return current_fd
    except RuntimeLockInvalid:
        os.close(current_fd)
        raise
    except OSError as error:
        os.close(current_fd)
        raise RuntimeLockInvalid("canonical runtime lock path is invalid") from error


def _verify_lock_identity(target: Path, parent_fd: int, lock_fd: int) -> None:
    try:
        fd_stat = os.fstat(lock_fd)
        relative_stat = os.stat(
            target.name, dir_fd=parent_fd, follow_symlinks=False
        )
        path_stat = os.stat(target, follow_symlinks=False)
    except OSError as error:
        raise RuntimeLockInvalid("runtime lock identity changed") from error
    identities = {
        (value.st_dev, value.st_ino)
        for value in (fd_stat, relative_stat, path_stat)
    }
    if (
        len(identities) != 1
        or not stat.S_ISREG(fd_stat.st_mode)
        or not stat.S_ISREG(relative_stat.st_mode)
        or not stat.S_ISREG(path_stat.st_mode)
    ):
        raise RuntimeLockInvalid("runtime lock identity changed")


def canonical_runtime_lock_path(repo_path: Path) -> Path:
    """Resolve the one operational lock in the canonical main checkout."""
    start = Path(repo_path).resolve()
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(start),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeLockInvalid("canonical runtime lock is unavailable") from error
    common_dir = Path(result.stdout.strip()).resolve()
    if common_dir.name != ".git":
        raise RuntimeLockInvalid("canonical runtime lock is unavailable")
    repo_root = common_dir.parent
    target = repo_root / "data" / "operational" / "eventedge-runtime.lock"
    _reject_existing_symlinks(repo_root, target)
    return target


@contextmanager
def runtime_lock(
    lock_path: Path,
    *,
    exclusive: bool,
    inherited_fd: int | None = None,
    inherited_exclusive: bool | None = None,
) -> Iterator[RuntimeLockHandle]:
    """Acquire one non-blocking shared or exclusive process lock."""
    target = _absolute_lexical(lock_path)
    if inherited_fd is not None:
        if inherited_exclusive is not exclusive:
            raise RuntimeLockInvalid("inherited runtime lock mode is invalid")
        parent_fd = _open_parent_directory(target, create=False)
        try:
            try:
                _verify_lock_identity(target, parent_fd, inherited_fd)
            except RuntimeLockInvalid as error:
                raise RuntimeLockInvalid("inherited runtime lock is invalid") from error
            try:
                yield RuntimeLockHandle(
                    path=target,
                    fd=inherited_fd,
                    exclusive=exclusive,
                    inherited=True,
                )
            finally:
                _verify_lock_identity(target, parent_fd, inherited_fd)
        finally:
            os.close(parent_fd)
        return

    parent_fd = _open_parent_directory(target, create=True)
    open_flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        lock_fd = os.open(target.name, open_flags, 0o600, dir_fd=parent_fd)
    except OSError as error:
        os.close(parent_fd)
        raise RuntimeLockInvalid("canonical runtime lock path is invalid") from error
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        _verify_lock_identity(target, parent_fd, lock_fd)
        try:
            fcntl.flock(lock_fd, mode | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeLockBusy(target) from error
        try:
            try:
                yield RuntimeLockHandle(
                    path=target,
                    fd=lock_fd,
                    exclusive=exclusive,
                    inherited=False,
                )
            finally:
                _verify_lock_identity(target, parent_fd, lock_fd)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
        os.close(parent_fd)
