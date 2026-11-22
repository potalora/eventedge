"""Shared atomic I/O, strict JSON, SQLite, git worktree, and taxonomy primitives.

This module intentionally depends only on the Python standard library so every
layer of EventEdge (config, state, metrics, orchestration, scripts) can share
one durable, leak-free implementation without circular imports.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tempfile
from typing import Any, Iterator
from urllib.parse import quote


COHORT_HORIZONS: tuple[str, ...] = ("30d", "3m", "6m", "1y")
COHORT_SIZES: tuple[str, ...] = ("5k", "10k", "50k", "100k")
CANONICAL_COHORTS: tuple[str, ...] = tuple(
    f"horizon_{horizon}_size_{size}"
    for horizon in COHORT_HORIZONS
    for size in COHORT_SIZES
)

_COMMIT_HEX_RE = re.compile(r"^[0-9a-f]{7,40}$")
_FULL_COMMIT_HEX_RE = re.compile(r"^[0-9a-f]{40}$")
_DEFAULT_MAX_JSON_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class SourceTaxonomy:
    """Explicit Tier-1 / Tier-2 classification of active EventEdge sources."""

    accounting_sources: frozenset[str] = frozenset(
        {
            "alpaca_sip",
            "yfinance_actions_benchmarks",
        }
    )
    strategy_sources: frozenset[str] = frozenset(
        {
            "finnhub",
            "yfinance_research",
            "usaspending",
            "noaa",
            "usda",
            "drought_monitor",
            "cftc",
            "fred",
        }
    )
    optional_enrichment: frozenset[str] = frozenset(
        {
            "yfinance_profile",
            "finra_short_interest",
            "ken_french",
        }
    )

    @property
    def all_sources(self) -> frozenset[str]:
        return (
            self.accounting_sources
            | self.strategy_sources
            | self.optional_enrichment
        )

    def tier_for(self, source: str) -> str:
        if source in self.accounting_sources:
            return "tier1_accounting"
        if source in self.strategy_sources:
            return "tier2_strategy"
        if source in self.optional_enrichment:
            return "tier2_optional_enrichment"
        raise KeyError(f"Unknown or retired source: {source!r}")

    def is_tier1(self, source: str) -> bool:
        return source in self.accounting_sources

    def is_tier2(self, source: str) -> bool:
        return (
            source in self.strategy_sources
            or source in self.optional_enrichment
        )

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "accounting_sources": sorted(self.accounting_sources),
            "strategy_sources": sorted(self.strategy_sources),
            "optional_enrichment": sorted(self.optional_enrichment),
        }


SOURCE_TAXONOMY = SourceTaxonomy()


def _fsync_directory(directory: Path) -> None:
    """Flush directory entry metadata so os.replace survives power loss."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    dir_fd = os.open(str(directory), flags)
    try:
        os.fsync(dir_fd)
    except OSError:
        # Some virtual filesystems reject fsync on directory descriptors.
        pass
    finally:
        os.close(dir_fd)


def _validate_mode(mode: int) -> int:
    if type(mode) is not int or not 0 <= mode <= 0o7777:
        raise ValueError(f"invalid file mode: {mode!r}")
    return mode


def _validate_busy_timeout(busy_timeout_ms: int) -> int:
    if type(busy_timeout_ms) is not int or busy_timeout_ms < 0:
        raise ValueError(f"busy_timeout_ms must be a non-negative int, got {busy_timeout_ms!r}")
    return busy_timeout_ms


def atomic_write_text(
    path: str | Path,
    content: str,
    *,
    mode: int = 0o600,
) -> Path:
    """Atomically write UTF-8 text: temp file + fsync + chmod + os.replace + dir fsync."""
    if not isinstance(content, str):
        raise TypeError("content must be str")
    mode = _validate_mode(mode)
    target = Path(path)
    if target.is_symlink():
        raise ValueError(f"refusing atomic write to symlink: {target}")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
        _fsync_directory(parent)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    return target


def atomic_write_json(
    path: str | Path,
    payload: Any,
    *,
    mode: int = 0o600,
    indent: int | None = 2,
    sort_keys: bool = True,
) -> Path:
    """Atomically write strict JSON (NaN/Infinity forbidden) with trailing newline."""
    separators = (",", ":") if indent is None else None
    serialized = json.dumps(
        payload,
        indent=indent,
        sort_keys=sort_keys,
        separators=separators,
        ensure_ascii=True,
        allow_nan=False,
    )
    return atomic_write_text(path, serialized + "\n", mode=mode)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_nonstandard_constant(token: str) -> Any:
    raise ValueError(f"invalid JSON constant: {token!r}")


def _read_regular_file_text(
    path: Path,
    *,
    max_bytes: int,
    reject_symlinks: bool,
) -> str:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive int")
    abs_path = path.absolute()
    if reject_symlinks and abs_path.is_symlink():
        raise ValueError(f"symlink not permitted: {path}")
    flags = os.O_RDONLY
    if reject_symlinks and hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    fd = os.open(str(abs_path), flags)
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"not a regular file: {path}")
            if info.st_size > max_bytes:
                raise ValueError(
                    f"JSON file exceeds max_bytes ({info.st_size} > {max_bytes}): {path}"
                )
            raw = handle.read(max_bytes + 1)
    except OSError:
        raise
    if len(raw) > max_bytes:
        raise ValueError(f"JSON file exceeds max_bytes ({max_bytes}): {path}")
    return raw.decode("utf-8")


def _assert_finite_numbers(value: Any) -> None:
    if type(value) is float and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, dict):
        for item in value.values():
            _assert_finite_numbers(item)
    elif isinstance(value, list):
        for item in value:
            _assert_finite_numbers(item)


def load_json_strict(
    path_or_text: str | Path,
    *,
    max_bytes: int = _DEFAULT_MAX_JSON_BYTES,
    reject_symlinks: bool = True,
) -> Any:
    """Parse JSON from a Path or text string, rejecting duplicate keys and NaN/Inf."""
    if isinstance(path_or_text, os.PathLike):
        text = _read_regular_file_text(
            Path(path_or_text),
            max_bytes=max_bytes,
            reject_symlinks=reject_symlinks,
        )
    elif isinstance(path_or_text, str):
        stripped = path_or_text.lstrip()
        if stripped.startswith(("{", "[")):
            if len(path_or_text.encode("utf-8")) > max_bytes:
                raise ValueError(f"JSON text exceeds max_bytes ({max_bytes})")
            text = path_or_text
        elif "\n" not in path_or_text and os.path.lexists(path_or_text):
            text = _read_regular_file_text(
                Path(path_or_text),
                max_bytes=max_bytes,
                reject_symlinks=reject_symlinks,
            )
        else:
            if len(path_or_text.encode("utf-8")) > max_bytes:
                raise ValueError(f"JSON text exceeds max_bytes ({max_bytes})")
            text = path_or_text
    else:
        raise TypeError(f"expected str or PathLike, got {type(path_or_text).__name__}")

    parsed = json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonstandard_constant,
    )
    _assert_finite_numbers(parsed)
    return parsed


@contextmanager
def open_sqlite(
    path: str | Path,
    *,
    readonly: bool = False,
    busy_timeout_ms: int = 10000,
    immutable: bool = False,
    isolation_level: str | None = "",
) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with guaranteed closing() and hardened pragmas.

    Read-write mode enforces WAL, foreign_keys=ON, synchronous=FULL, and
    busy_timeout. Read-only mode opens with URI mode=ro and query_only=ON.
    """
    timeout_ms = _validate_busy_timeout(busy_timeout_ms)
    timeout_s = timeout_ms / 1000.0
    target = Path(path)

    if readonly:
        if not target.is_file():
            raise FileNotFoundError(target)
        encoded = quote(str(target.resolve()), safe="/")
        suffix = "&immutable=1" if immutable else ""
        conn = sqlite3.connect(
            f"file:{encoded}?mode=ro{suffix}",
            uri=True,
            timeout=timeout_s,
            isolation_level=isolation_level,
        )
    else:
        if immutable:
            raise ValueError("immutable=True requires readonly=True")
        if target.is_symlink():
            raise ValueError(f"refusing to open symlink SQLite database for write: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(target),
            timeout=timeout_s,
            isolation_level=isolation_level,
        )

    with closing(conn):
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={timeout_ms}")
        conn.execute("PRAGMA foreign_keys=ON")
        if readonly:
            conn.execute("PRAGMA query_only=ON")
            yield conn
        else:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
            else:
                if conn.in_transaction:
                    conn.commit()


def snapshot_sqlite(
    src: str | Path,
    dst: str | Path,
    *,
    busy_timeout_ms: int = 10000,
    mode: int = 0o600,
) -> Path:
    """Create a crash-safe SQLite snapshot via sqlite3.Connection.backup().

    Works safely even while WAL writers are active on ``src``.
    """
    mode = _validate_mode(mode)
    timeout_ms = _validate_busy_timeout(busy_timeout_ms)
    src_path = Path(src)
    dst_path = Path(dst)
    if not src_path.is_file():
        raise FileNotFoundError(src_path)
    if dst_path.is_symlink():
        raise ValueError(f"refusing to write snapshot to symlink: {dst_path}")
    if dst_path.exists() and src_path.resolve() == dst_path.resolve():
        raise ValueError("snapshot src and dst must refer to distinct files")

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(dst_path.parent),
        prefix=f".{dst_path.name}.",
        suffix=".tmp",
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with open_sqlite(src_path, readonly=True, busy_timeout_ms=timeout_ms) as src_conn:
            with closing(
                sqlite3.connect(str(tmp_path), timeout=timeout_ms / 1000.0)
            ) as dst_conn:
                src_conn.backup(dst_conn)
                dst_conn.commit()
        # Flush backup bytes before atomic rename.
        sync_fd = os.open(str(tmp_path), os.O_RDONLY)
        try:
            os.fsync(sync_fd)
        finally:
            os.close(sync_fd)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, dst_path)
        _fsync_directory(dst_path.parent)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    return dst_path


def _commit_matches(actual: str, expected: str) -> bool:
    actual_norm = actual.strip().lower()
    expected_norm = expected.strip().lower()
    if not _COMMIT_HEX_RE.fullmatch(expected_norm):
        return False
    if not _COMMIT_HEX_RE.fullmatch(actual_norm):
        return False
    if len(expected_norm) == len(actual_norm):
        return actual_norm == expected_norm
    if len(expected_norm) < len(actual_norm):
        return actual_norm.startswith(expected_norm)
    return expected_norm.startswith(actual_norm)


def _read_container_commit(worktree: Path) -> str | None:
    env_commit = os.environ.get("EVENTEDGE_GENERATION_COMMIT", "").strip().lower()
    if env_commit and _COMMIT_HEX_RE.fullmatch(env_commit):
        return env_commit
    marker = worktree / ".generation-commit"
    if marker.is_file() and not marker.is_symlink():
        candidate = marker.read_text(encoding="utf-8").strip().lower()
        if _COMMIT_HEX_RE.fullmatch(candidate):
            return candidate
    return None


def verify_git_worktree(
    worktree_path: str | Path,
    expected_commit: str | None = None,
    *,
    require_clean: bool = False,
) -> dict[str, Any]:
    """Verify a repository/worktree commit on host or inside a container.

    On a host checkout or git worktree, runs ``git rev-parse HEAD`` and checks
    cleanliness. Inside a container without accessible ``.git`` metadata, falls
    back to ``EVENTEDGE_GENERATION_COMMIT`` or ``<worktree>/.generation-commit``.
    Fails closed (raises ValueError) if ``expected_commit`` is provided and
    cannot be matched.
    """
    target = Path(worktree_path)
    if not target.is_dir():
        raise ValueError(f"worktree path does not exist or is not a directory: {target}")
    if expected_commit is not None and (
        type(expected_commit) is not str
        or not _COMMIT_HEX_RE.fullmatch(expected_commit.strip().lower())
    ):
        raise ValueError(f"invalid expected_commit: {expected_commit!r}")

    git_error: str | None = None
    try:
        head_proc = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if head_proc.returncode == 0:
            commit = head_proc.stdout.strip().lower()
            if not _FULL_COMMIT_HEX_RE.fullmatch(commit):
                raise ValueError(f"unexpected git HEAD output: {commit!r}")
            status_proc = subprocess.run(
                ["git", "-C", str(target), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            clean = status_proc.returncode == 0 and not status_proc.stdout.strip()
            if expected_commit is not None and not _commit_matches(commit, expected_commit):
                raise ValueError(
                    f"worktree commit mismatch: expected {expected_commit}, got {commit}"
                )
            if require_clean and not clean:
                raise ValueError(f"worktree is dirty: {target}")
            return {
                "verified": True,
                "mode": "git",
                "worktree_path": str(target.resolve()),
                "commit": commit,
                "clean": clean,
            }
        git_error = (head_proc.stderr or head_proc.stdout).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        git_error = str(exc)

    container_commit = _read_container_commit(target)
    if container_commit is not None:
        if expected_commit is not None and not _commit_matches(container_commit, expected_commit):
            raise ValueError(
                f"container commit mismatch: expected {expected_commit}, got {container_commit}"
            )
        return {
            "verified": True,
            "mode": "container",
            "worktree_path": str(target.resolve()),
            "commit": container_commit,
            "clean": None,
        }

    if expected_commit is not None:
        raise ValueError(
            f"unable to verify expected commit {expected_commit} for {target}: "
            f"{git_error or 'no git metadata or container commit marker'}"
        )
    return {
        "verified": False,
        "mode": "unavailable",
        "worktree_path": str(target.resolve()),
        "commit": None,
        "clean": None,
    }


__all__ = [
    "COHORT_HORIZONS",
    "COHORT_SIZES",
    "CANONICAL_COHORTS",
    "SOURCE_TAXONOMY",
    "SourceTaxonomy",
    "atomic_write_text",
    "atomic_write_json",
    "load_json_strict",
    "open_sqlite",
    "snapshot_sqlite",
    "verify_git_worktree",
]
