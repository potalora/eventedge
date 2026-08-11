"""Read-only state topology discovery for the governed preflight probe."""

from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import stat
import tempfile
from collections.abc import Collection, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType

from tradingagents.strategies.data_sources.yfinance_source import normalize_tickers
from tradingagents.strategies.metrics.calendar import XNYSCalendar
from tradingagents.strategies.metrics.models import OUTCOME_WINDOWS, MetricEpoch
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


_MAX_COHORTS = 64
_MAX_TICKERS = 2_048
_MAX_EPOCHS = 1_024
_MAX_LEDGER_ROWS = 4_096
_MAX_TEXT = 4_096


class PreflightStateError(RuntimeError):
    """State topology cannot be proven safe without mutation."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason)[:_MAX_TEXT]
        super().__init__(self.reason)


@dataclass(frozen=True)
class PreflightFileIdentity:
    path: Path
    dev: int
    ino: int
    size: int
    mtime_ns: int
    data_version: int


@dataclass(frozen=True)
class PreflightStateSnapshot:
    state_status: str
    epoch_id: str
    governed_tickers: tuple[str, ...]
    cohort_ids_by_ticker: Mapping[str, tuple[str, ...]]
    metric_store_path: Path | None
    file_identities: tuple[PreflightFileIdentity, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cohort_ids_by_ticker",
            MappingProxyType(dict(self.cohort_ids_by_ticker)),
        )


@dataclass
class _Observer:
    path: Path
    connection: sqlite3.Connection

    @classmethod
    def open(cls, path: Path) -> _Observer:
        target = _absolute_path(path)
        try:
            connection = sqlite3.connect(
                f"{target.as_uri()}?mode=ro&immutable=1",
                uri=True,
                isolation_level=None,
            )
            connection.execute("PRAGMA query_only=ON")
            connection.execute("SELECT 1").fetchone()
        except BaseException:
            if "connection" in locals():
                connection.close()
            raise
        return cls(target, connection)

    def identity(self) -> tuple[int, int, int, int, int]:
        file_stat = self.path.stat()
        row = self.connection.execute("PRAGMA data_version").fetchone()
        if row is None:
            raise PreflightStateError("state data version is unavailable")
        return (
            file_stat.st_dev,
            file_stat.st_ino,
            file_stat.st_size,
            file_stat.st_mtime_ns,
            int(row[0]),
        )

    def close(self) -> None:
        self.connection.close()


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        stat.S_IFMT(value.st_mode),
    )


def _open_flags(*, directory: bool) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    return flags


def _symlink_error(
    *, parent_fd: int | None, name: str | Path, error: OSError
) -> PreflightStateError:
    try:
        target_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        target_stat = None
    if error.errno == errno.ELOOP or (
        target_stat is not None and stat.S_ISLNK(target_stat.st_mode)
    ):
        return PreflightStateError("state path symlink is invalid")
    return PreflightStateError("state path topology is invalid")


def _require_fd_path_identity(
    path: Path, fd: int, *, directory: bool
) -> os.stat_result:
    fd_stat = os.fstat(fd)
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(fd_stat.st_mode):
        kind = "directory" if directory else "file"
        raise PreflightStateError(f"state {kind} has invalid type")
    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as error:
        kind = "directory" if directory else "file"
        raise PreflightStateError(f"state {kind} identity changed") from error
    if stat.S_ISLNK(path_stat.st_mode):
        raise PreflightStateError("state path symlink is invalid")
    if (path_stat.st_dev, path_stat.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
        kind = "directory" if directory else "file"
        raise PreflightStateError(f"state {kind} identity changed")
    return fd_stat


@dataclass
class _CertifiedDirectory:
    path: Path
    fd: int
    baseline: os.stat_result

    @classmethod
    def open_root(cls, path: Path) -> _CertifiedDirectory:
        target = _absolute_path(path)
        try:
            fd = os.open(target, _open_flags(directory=True))
        except OSError as error:
            raise _symlink_error(parent_fd=None, name=target, error=error) from error
        try:
            baseline = _require_fd_path_identity(target, fd, directory=True)
        except BaseException:
            os.close(fd)
            raise
        return cls(target, fd, baseline)

    @classmethod
    def open_child(
        cls, parent: _CertifiedDirectory, name: str, path: Path
    ) -> _CertifiedDirectory:
        target = _absolute_path(path)
        try:
            fd = os.open(name, _open_flags(directory=True), dir_fd=parent.fd)
        except FileNotFoundError:
            raise
        except OSError as error:
            raise _symlink_error(
                parent_fd=parent.fd, name=name, error=error
            ) from error
        try:
            baseline = _require_fd_path_identity(target, fd, directory=True)
        except BaseException:
            os.close(fd)
            raise
        return cls(target, fd, baseline)

    def verify(self, *, contents: bool = False) -> None:
        current = _require_fd_path_identity(self.path, self.fd, directory=True)
        identity_changed = (
            current.st_dev,
            current.st_ino,
            stat.S_IFMT(current.st_mode),
        ) != (
            self.baseline.st_dev,
            self.baseline.st_ino,
            stat.S_IFMT(self.baseline.st_mode),
        )
        if identity_changed or (
            contents and _stat_identity(current) != _stat_identity(self.baseline)
        ):
            raise PreflightStateError("state directory identity changed")

    def close(self) -> None:
        os.close(self.fd)


@contextmanager
def _certified_directory_chain(
    target: Path,
) -> Iterator[
    tuple[
        _CertifiedDirectory | None,
        tuple[_CertifiedDirectory, ...],
        tuple[_CertifiedDirectory, str] | None,
    ]
]:
    """Open every existing component relative to its retained parent FD."""
    absolute = _absolute_path(target)
    handles: list[_CertifiedDirectory] = []
    try:
        current = _CertifiedDirectory.open_root(Path(absolute.anchor))
        handles.append(current)
        complete = True
        missing: tuple[_CertifiedDirectory, str] | None = None
        current_path = Path(absolute.anchor)
        for component in absolute.parts[1:]:
            current_path /= component
            try:
                current = _CertifiedDirectory.open_child(
                    current, component, current_path
                )
            except FileNotFoundError:
                complete = False
                missing = (current, component)
                break
            handles.append(current)
        yield current if complete else None, tuple(handles), missing
    finally:
        for handle in reversed(handles):
            handle.close()


@dataclass
class _CertifiedFile:
    path: Path
    fd: int
    baseline: os.stat_result

    @classmethod
    def open(
        cls, path: Path, *, parent: _CertifiedDirectory
    ) -> _CertifiedFile:
        target = _absolute_path(path)
        try:
            fd = os.open(
                target.name,
                _open_flags(directory=False),
                dir_fd=parent.fd,
            )
        except OSError as error:
            classified = _symlink_error(
                parent_fd=parent.fd, name=target.name, error=error
            )
            if "symlink" in classified.reason:
                raise classified from error
            raise PreflightStateError("state file certification failed") from error
        try:
            baseline = _require_fd_path_identity(target, fd, directory=False)
        except BaseException:
            os.close(fd)
            raise
        return cls(target, fd, baseline)

    def verify(self) -> os.stat_result:
        current = _require_fd_path_identity(self.path, self.fd, directory=False)
        if _stat_identity(current) != _stat_identity(self.baseline):
            raise PreflightStateError("state file identity changed")
        return current

    def verify_relative(self, parent: _CertifiedDirectory) -> os.stat_result:
        current = os.fstat(self.fd)
        entry = _entry_stat(parent, self.path.name)
        if entry is None or not stat.S_ISREG(entry.st_mode):
            raise PreflightStateError("state file identity changed")
        if (entry.st_dev, entry.st_ino) != (current.st_dev, current.st_ino) or (
            _stat_identity(current) != _stat_identity(self.baseline)
        ):
            raise PreflightStateError("state file identity changed")
        return current

    def copy_to(self, destination: Path) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        output_fd = os.open(destination, flags, 0o600)
        try:
            offset = 0
            while True:
                chunk = os.pread(self.fd, 1024 * 1024, offset)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(output_fd, view)
                    if written <= 0:
                        raise OSError("short write while copying certified state")
                    view = view[written:]
                offset += len(chunk)
            os.fsync(output_fd)
        finally:
            os.close(output_fd)
        self.verify()

    def close(self) -> None:
        os.close(self.fd)


def _sqlite_sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))


def _canonical_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise PreflightStateError(f"{label} is invalid")
    result = value.strip()
    if not result or len(result) > _MAX_TEXT:
        raise PreflightStateError(f"{label} is invalid")
    return result


def _canonical_cohorts(values: Collection[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise PreflightStateError("cohort IDs are invalid")
    raw = tuple(values)
    cohorts = tuple(sorted(_canonical_text(value, "cohort ID") for value in raw))
    if (
        not cohorts
        or len(cohorts) > _MAX_COHORTS
        or len(set(cohorts)) != len(cohorts)
        or any(
            "/" in value or "\\" in value or value in {".", ".."} for value in cohorts
        )
    ):
        raise PreflightStateError("cohort IDs are invalid")
    return cohorts


def _canonical_tickers(values: Collection[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise PreflightStateError("benchmark tickers are invalid")
    tickers = tuple(
        sorted(_canonical_text(value, "benchmark ticker").upper() for value in values)
    )
    if not tickers or len(tickers) > _MAX_TICKERS or len(set(tickers)) != len(tickers):
        raise PreflightStateError("benchmark tickers are invalid")
    if len(set(normalize_tickers(list(tickers)))) != len(tickers):
        raise PreflightStateError("benchmark ticker normalization is ambiguous")
    return tickers


def _ticker(value: object) -> str:
    raw = _canonical_text(value, "governed ticker")
    result = raw.upper()
    if len(result) > 64 or raw != result:
        raise PreflightStateError("governed ticker is invalid")
    return result


def _prospective_epoch_id(
    state_dir: Path, session: date, epochs: Collection[MetricEpoch]
) -> str:
    occupied = {epoch.epoch_id for epoch in epochs}
    digest = hashlib.sha256(
        f"{_absolute_path(state_dir)}|{session.isoformat()}".encode()
    ).hexdigest()[:16]
    candidate = f"preflight-prospective-{session.isoformat()}-{digest}"
    if candidate in occupied:
        candidate = f"{candidate}-unbound"
    if candidate in occupied:
        raise PreflightStateError("prospective epoch identity is ambiguous")
    return candidate


def _load_epochs(
    metric_store: MetricStore, observer: _Observer
) -> tuple[MetricEpoch, ...]:
    rows = observer.connection.execute(
        "SELECT epoch_id, payload_json FROM metric_epochs "
        "ORDER BY json_extract(payload_json, '$.start_session'), epoch_id "
        "LIMIT ?",
        (_MAX_EPOCHS + 1,),
    ).fetchall()
    if not rows or len(rows) > _MAX_EPOCHS:
        raise PreflightStateError("metric epoch topology is unbounded")
    try:
        epochs = tuple(metric_store._epoch(str(row[1])) for row in rows)
    except Exception as error:
        raise PreflightStateError("metric epoch topology is malformed") from error
    if any(
        str(row[0]) != epoch.epoch_id for row, epoch in zip(rows, epochs, strict=True)
    ):
        raise PreflightStateError("metric epoch identity is inconsistent")
    return epochs


def _select_epoch(
    *, state_dir: Path, session: date, epochs: tuple[MetricEpoch, ...]
) -> tuple[str, str]:
    ordered = tuple(
        sorted(epochs, key=lambda item: (item.start_session, item.epoch_id))
    )
    for epoch in ordered:
        if epoch.status == "open":
            if epoch.end_session is not None:
                raise PreflightStateError("metric epoch identity is ambiguous")
        elif epoch.status in {"closed", "invalid"}:
            if epoch.end_session is None or epoch.end_session < epoch.start_session:
                raise PreflightStateError("metric epoch identity is ambiguous")
        else:
            raise PreflightStateError("metric epoch identity is ambiguous")
    for previous, following in zip(ordered, ordered[1:], strict=False):
        if (
            previous.end_session is None
            or following.start_session <= previous.end_session
        ):
            raise PreflightStateError("metric epoch identity is ambiguous")

    covering = tuple(
        epoch
        for epoch in ordered
        if epoch.start_session <= session
        and (epoch.end_session is None or session <= epoch.end_session)
    )
    if len(covering) > 1:
        raise PreflightStateError("metric epoch identity is ambiguous")
    if covering:
        epoch = covering[0]
        if epoch.status == "invalid":
            return epoch.epoch_id, "state_already_invalid"
        return epoch.epoch_id, "ready"
    if any(epoch.start_session > session for epoch in ordered):
        raise PreflightStateError("metric epoch identity is ambiguous")
    return _prospective_epoch_id(state_dir, session, ordered), "ready"


def _bounded_count(
    observer: _Observer,
    sql: str,
    values: tuple[object, ...],
    label: str,
) -> None:
    row = observer.connection.execute(sql, values).fetchone()
    if row is None or int(row[0]) > _MAX_LEDGER_ROWS:
        raise PreflightStateError(f"{label} is unbounded")


def _outcome_tickers(
    ledger: PortfolioLedger,
    observer: _Observer,
    *,
    session: date,
    epoch_id: str,
) -> set[str]:
    calendar = XNYSCalendar()
    earliest = session
    for _ in range(max(OUTCOME_WINDOWS)):
        earliest = calendar.previous_session(earliest)
    _bounded_count(
        observer,
        "SELECT COUNT(*) FROM signals WHERE epoch_id = ? "
        "AND reference_session >= ? AND reference_session <= ?",
        (epoch_id, earliest.isoformat(), session.isoformat()),
        "outcome signal set",
    )
    tickers: set[str] = set()
    for signal in ledger.read_signals(earliest, session, epoch_id=epoch_id):
        entry_session = calendar.next_session(signal.reference_session)
        if entry_session == session or any(
            calendar.held_session(entry_session, window) == session
            for window in OUTCOME_WINDOWS
        ):
            tickers.add(_ticker(signal.ticker))
    return tickers


def _cohort_tickers(
    ledger: PortfolioLedger,
    observer: _Observer,
    *,
    session: date,
    epoch_id: str,
) -> set[str]:
    cohort_id = ledger.cohort_id
    encoded_session = session.isoformat()
    _bounded_count(
        observer,
        "SELECT COUNT(*) FROM lots WHERE cohort_id = ? AND open_qty > 0",
        (cohort_id,),
        "open lot set",
    )
    _bounded_count(
        observer,
        "SELECT COUNT(*) FROM order_intents WHERE cohort_id = ? "
        "AND status = 'pending' AND ((price_rule = 'next_session_open' "
        "AND eligible_session = ?) OR (price_rule = 'resting_stop' "
        "AND eligible_session <= ?))",
        (cohort_id, encoded_session, encoded_session),
        "pending intent set",
    )
    _bounded_count(
        observer,
        "SELECT COUNT(*) FROM intent_signals isg JOIN order_intents i "
        "ON i.intent_id = isg.intent_id WHERE i.cohort_id = ? "
        "AND i.status = 'pending' AND ((i.price_rule = 'next_session_open' "
        "AND i.eligible_session = ?) OR (i.price_rule = 'resting_stop' "
        "AND i.eligible_session <= ?))",
        (cohort_id, encoded_session, encoded_session),
        "pending intent provenance set",
    )
    tickers = {_ticker(position["ticker"]) for position in ledger.open_positions()}
    for intent in ledger.pending_intents(session):
        signals = ledger.signals_for_intent(intent.intent_id)
        provenance = {_ticker(signal.ticker) for signal in signals}
        if len(provenance) != 1:
            raise PreflightStateError("pending intent ticker provenance is ambiguous")
        tickers.update(provenance)
    tickers.update(
        _outcome_tickers(ledger, observer, session=session, epoch_id=epoch_id)
    )
    if len(tickers) > _MAX_TICKERS:
        raise PreflightStateError("governed ticker set is unbounded")
    if len(set(normalize_tickers(sorted(tickers)))) != len(tickers):
        raise PreflightStateError("governed ticker normalization is ambiguous")
    return tickers


def _entry_stat(
    parent: _CertifiedDirectory, name: str
) -> os.stat_result | None:
    try:
        result = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PreflightStateError("state path topology is invalid") from error
    if stat.S_ISLNK(result.st_mode):
        raise PreflightStateError("state path symlink is invalid")
    return result


def _unexpected_ledgers(
    state_dir: _CertifiedDirectory | None, expected_cohorts: set[str]
) -> bool:
    if state_dir is None:
        return False
    discovered: set[str] = set()
    try:
        children = os.listdir(state_dir.fd)
    except OSError as error:
        raise PreflightStateError("state topology is invalid") from error
    for name in children:
        entry = _entry_stat(state_dir, name)
        if entry is None or not stat.S_ISDIR(entry.st_mode):
            continue
        child_path = state_dir.path / name
        child = _CertifiedDirectory.open_child(state_dir, name, child_path)
        try:
            portfolio = _entry_stat(child, "portfolio.db")
        finally:
            child.close()
        if portfolio is None:
            continue
        if not stat.S_ISREG(portfolio.st_mode):
            raise PreflightStateError("state file has invalid type")
        discovered.add(name)
    return not discovered.issubset(expected_cohorts)


def _sidecars_in_directory(
    directory: _CertifiedDirectory, database_name: str
) -> set[Path]:
    try:
        names = set(os.listdir(directory.fd))
    except OSError as error:
        raise PreflightStateError("SQLite sidecar topology is not safe") from error
    discovered: set[Path] = set()
    for suffix in ("-wal", "-shm", "-journal"):
        name = f"{database_name}{suffix}"
        if name not in names:
            continue
        entry = _entry_stat(directory, name)
        if entry is None:
            raise PreflightStateError("SQLite sidecar topology is not safe")
        if not stat.S_ISREG(entry.st_mode):
            raise PreflightStateError("SQLite sidecar type is not safe")
        discovered.add(directory.path / name)
    return discovered


def _enumerate_sidecars(
    state_directory: _CertifiedDirectory | None,
    cohort_directories: Mapping[str, _CertifiedDirectory],
) -> set[Path]:
    """Enumerate all governed SQLite sidecars through retained directory FDs."""
    if state_directory is None:
        return set()
    discovered = _sidecars_in_directory(
        state_directory, "metrics_v2.sqlite3"
    )
    try:
        children = os.listdir(state_directory.fd)
    except OSError as error:
        raise PreflightStateError("SQLite sidecar topology is not safe") from error
    for name in children:
        entry = _entry_stat(state_directory, name)
        if entry is None or not stat.S_ISDIR(entry.st_mode):
            continue
        directory = cohort_directories.get(name)
        temporary = directory is None
        if directory is None:
            directory = _CertifiedDirectory.open_child(
                state_directory, name, state_directory.path / name
            )
        try:
            discovered.update(_sidecars_in_directory(directory, "portfolio.db"))
        finally:
            if temporary:
                directory.close()
    return discovered


def _verify_sidecars(
    *,
    state_directory: _CertifiedDirectory | None,
    cohort_directories: Mapping[str, _CertifiedDirectory],
    database_paths: tuple[Path, ...],
    database_parents: Mapping[Path, _CertifiedDirectory],
    certified: Mapping[Path, _CertifiedFile],
    during_probe: bool,
) -> None:
    current = _enumerate_sidecars(state_directory, cohort_directories)
    if any(path.name.endswith("-journal") for path in current):
        if during_probe:
            raise PreflightStateError("state changed during preflight")
        raise PreflightStateError("SQLite sidecar journal is not safe")
    if current != set(certified):
        if during_probe:
            raise PreflightStateError("state changed during preflight")
        raise PreflightStateError("SQLite sidecar topology is not safe")
    for database_path in database_paths:
        parent = database_parents[database_path]
        for path in _sqlite_sidecars(database_path):
            handle = certified.get(path)
            if handle is None:
                continue
            try:
                current_stat = handle.verify_relative(parent)
            except PreflightStateError as error:
                if during_probe:
                    raise PreflightStateError("state changed during preflight") from error
                raise
            if path.name.endswith("-wal") and current_stat.st_size != 0:
                if during_probe:
                    raise PreflightStateError("state changed during preflight")
                raise PreflightStateError("SQLite sidecar WAL is not clean")


@contextmanager
def _certified_copies(
    *,
    state_directory: _CertifiedDirectory | None,
    cohort_directories: Mapping[str, _CertifiedDirectory],
    database_paths: tuple[Path, ...],
    database_parents: Mapping[Path, _CertifiedDirectory],
    destination_dir: Path,
) -> Iterator[
    tuple[
        Mapping[Path, Path],
        Mapping[Path, _CertifiedFile],
        Mapping[Path, _CertifiedFile],
    ]
]:
    handles: list[_CertifiedFile] = []
    sources: dict[Path, _CertifiedFile] = {}
    sidecars: dict[Path, _CertifiedFile] = {}
    copies: dict[Path, Path] = {}
    try:
        discovered = _enumerate_sidecars(state_directory, cohort_directories)
        if any(path.name.endswith("-journal") for path in discovered):
            raise PreflightStateError("SQLite sidecar journal is not safe")
        expected = {
            sidecar
            for database_path in database_paths
            for sidecar in _sqlite_sidecars(database_path)
        }
        if not discovered.issubset(expected):
            raise PreflightStateError("SQLite sidecar topology is not safe")
        for index, raw_path in enumerate(database_paths):
            path = _absolute_path(raw_path)
            parent = database_parents[path]
            source = _CertifiedFile.open(path, parent=parent)
            handles.append(source)
            sources[path] = source
            wal, shm, _journal = _sqlite_sidecars(path)
            for sidecar_path in (wal, shm):
                if sidecar_path not in discovered:
                    continue
                sidecar = _CertifiedFile.open(sidecar_path, parent=parent)
                handles.append(sidecar)
                sidecars[sidecar.path] = sidecar
                if sidecar_path == wal and sidecar.baseline.st_size != 0:
                    raise PreflightStateError("SQLite sidecar WAL is not clean")
            snapshot_path = destination_dir / f"database-{index:03d}.sqlite3"
            source.copy_to(snapshot_path)
            copies[path] = snapshot_path
        _verify_sidecars(
            state_directory=state_directory,
            cohort_directories=cohort_directories,
            database_paths=database_paths,
            database_parents=database_parents,
            certified=sidecars,
            during_probe=False,
        )
        yield MappingProxyType(copies), MappingProxyType(sources), MappingProxyType(
            sidecars
        )
    finally:
        for handle in reversed(handles):
            handle.close()


def _source_identity(
    source: _CertifiedFile, observer: _Observer
) -> PreflightFileIdentity:
    source_stat = source.verify()
    data_version = observer.identity()[-1]
    return PreflightFileIdentity(
        path=source.path,
        dev=source_stat.st_dev,
        ino=source_stat.st_ino,
        size=source_stat.st_size,
        mtime_ns=source_stat.st_mtime_ns,
        data_version=data_version,
    )


def _inspect_complete_snapshot(
    *,
    target: Path,
    cohorts: tuple[str, ...],
    benchmarks: tuple[str, ...],
    session: date,
    metric_path: Path,
    ledger_paths: Mapping[str, Path],
    copies: Mapping[Path, Path],
    observers: Mapping[Path, _Observer],
    identities: tuple[PreflightFileIdentity, ...],
) -> tuple[PreflightStateSnapshot, MetricStore]:
    ledgers: list[PortfolioLedger] = []
    try:
        metric_observer = observers[metric_path]
        metric_store = MetricStore.open_existing(copies[metric_path], immutable=True)
        epochs = _load_epochs(metric_store, metric_observer)
        epoch_id, state_status = _select_epoch(
            state_dir=target, session=session, epochs=epochs
        )
        membership: dict[str, set[str]] = {
            ticker: set(cohorts) for ticker in benchmarks
        }
        invalid_reasons: dict[str, str] = {}
        for cohort in cohorts:
            original_path = ledger_paths[cohort]
            ledger = PortfolioLedger.open_existing(
                copies[original_path], immutable=True
            )
            ledgers.append(ledger)
            if ledger.cohort_id != cohort:
                raise PreflightStateError("ledger cohort identity is inconsistent")
            observer = observers[original_path]
            _bounded_count(
                observer,
                "SELECT COUNT(*) FROM session_invalidations "
                "WHERE cohort_id = ? AND session = ?",
                (cohort, session.isoformat()),
                "session invalidation set",
            )
            stored_reasons = {
                str(row[0])
                for row in observer.connection.execute(
                    "SELECT reason FROM session_invalidations "
                    "WHERE cohort_id = ? AND session = ?",
                    (cohort, session.isoformat()),
                ).fetchall()
            }
            invalid_reason = ledger.session_invalid_reason(session)
            if len(stored_reasons) > 1 or bool(stored_reasons) != bool(invalid_reason):
                raise PreflightStateError("cohort session invalidation is inconsistent")
            if invalid_reason:
                if stored_reasons != {invalid_reason}:
                    raise PreflightStateError(
                        "cohort session invalidation is inconsistent"
                    )
                invalid_reasons[cohort] = invalid_reason
            for ticker in _cohort_tickers(
                ledger,
                observer,
                session=session,
                epoch_id=epoch_id,
            ):
                membership.setdefault(ticker, set()).add(cohort)
        if invalid_reasons:
            if (
                len(invalid_reasons) != len(cohorts)
                or len(set(invalid_reasons.values())) != 1
            ):
                raise PreflightStateError(
                    "cohort session invalidation topology is inconsistent"
                )
            state_status = "state_already_invalid"
        if len(membership) > _MAX_TICKERS:
            raise PreflightStateError("governed ticker set is unbounded")
        frozen_membership = {
            ticker: tuple(sorted(cohort_set))
            for ticker, cohort_set in sorted(membership.items())
        }
        governed_tickers = tuple(sorted(frozen_membership))
        if len(set(normalize_tickers(list(governed_tickers)))) != len(governed_tickers):
            raise PreflightStateError("governed ticker normalization is ambiguous")
        snapshot = PreflightStateSnapshot(
            state_status=state_status,
            epoch_id=epoch_id,
            governed_tickers=governed_tickers,
            cohort_ids_by_ticker=frozen_membership,
            metric_store_path=metric_path,
            file_identities=identities,
        )
        return snapshot, metric_store
    finally:
        for ledger in reversed(ledgers):
            ledger.close()


def inspect_preflight_state(
    *,
    state_dir: Path,
    cohort_ids: Collection[str],
    session: date,
    benchmark_tickers: Collection[str],
) -> PreflightStateSnapshot:
    """Discover exact governed scope through a certified temporary snapshot."""
    with inspect_and_guard_preflight_state(
        state_dir=state_dir,
        cohort_ids=cohort_ids,
        session=session,
        benchmark_tickers=benchmark_tickers,
    ) as (snapshot, _metric_store):
        return snapshot


@contextmanager
def inspect_and_guard_preflight_state(
    *,
    state_dir: Path,
    cohort_ids: Collection[str],
    session: date,
    benchmark_tickers: Collection[str],
) -> Iterator[tuple[PreflightStateSnapshot, MetricStore | None]]:
    """Hold certified sources while SQLite reads only temporary copies."""
    if not isinstance(session, date) or not XNYSCalendar().is_session(session):
        raise PreflightStateError("session is invalid")
    target = _absolute_path(state_dir)
    cohorts = _canonical_cohorts(cohort_ids)
    benchmarks = _canonical_tickers(benchmark_tickers)
    metric_path = target / "metrics_v2.sqlite3"
    ledger_paths = {cohort: target / cohort / "portfolio.db" for cohort in cohorts}
    database_paths = (metric_path, *ledger_paths.values())
    yielded = False
    verification_error: PreflightStateError | None = None
    with ExitStack() as stack:
        temporary_dir = Path(
            stack.enter_context(
                tempfile.TemporaryDirectory(prefix="eventedge-preflight-state-")
            )
        )
        state_directory, directory_chain, missing_directory = stack.enter_context(
            _certified_directory_chain(target)
        )
        cohort_directories: dict[str, _CertifiedDirectory] = {}
        if state_directory is not None:
            for cohort in cohorts:
                cohort_entry = _entry_stat(state_directory, cohort)
                if cohort_entry is None:
                    continue
                if not stat.S_ISDIR(cohort_entry.st_mode):
                    raise PreflightStateError("state cohort path has invalid type")
                try:
                    cohort_directory = _CertifiedDirectory.open_child(
                        state_directory, cohort, target / cohort
                    )
                except FileNotFoundError as error:
                    raise PreflightStateError(
                        "state topology changed during certification"
                    ) from error
                cohort_directories[cohort] = cohort_directory
                stack.callback(cohort_directory.close)

        metric_entry = (
            _entry_stat(state_directory, metric_path.name)
            if state_directory is not None
            else None
        )
        if metric_entry is not None and not stat.S_ISREG(metric_entry.st_mode):
            raise PreflightStateError("state file has invalid type")
        metric_exists = metric_entry is not None
        existing_ledgers: set[str] = set()
        for cohort, directory in cohort_directories.items():
            ledger_entry = _entry_stat(directory, "portfolio.db")
            if ledger_entry is None:
                continue
            if not stat.S_ISREG(ledger_entry.st_mode):
                raise PreflightStateError("state file has invalid type")
            existing_ledgers.add(cohort)
        if _unexpected_ledgers(state_directory, set(cohorts)):
            raise PreflightStateError("state topology has unexpected cohort ledgers")
        if metric_exists != bool(existing_ledgers) or (
            existing_ledgers and existing_ledgers != set(cohorts)
        ):
            raise PreflightStateError("state is partially initialized")
        complete = metric_exists and existing_ledgers == set(cohorts)
        active_database_paths = database_paths if complete else ()
        database_parents: dict[Path, _CertifiedDirectory] = {}
        if complete:
            if state_directory is None:
                raise PreflightStateError("state topology is invalid")
            database_parents[metric_path] = state_directory
            for cohort, path in ledger_paths.items():
                database_parents[path] = cohort_directories[cohort]

        copies, sources, sidecars = stack.enter_context(
            _certified_copies(
                state_directory=state_directory,
                cohort_directories=cohort_directories,
                database_paths=active_database_paths,
                database_parents=database_parents,
                destination_dir=temporary_dir,
            )
        )
        observers: dict[Path, _Observer] = {}
        try:
            if complete:
                for original_path in database_paths:
                    observer = _Observer.open(copies[original_path])
                    observers[original_path] = observer
                    stack.callback(observer.close)
                observer_before = tuple(
                    observers[path].identity() for path in database_paths
                )
                identities = tuple(
                    _source_identity(sources[path], observers[path])
                    for path in database_paths
                )
                snapshot, metric_store = _inspect_complete_snapshot(
                    target=target,
                    cohorts=cohorts,
                    benchmarks=benchmarks,
                    session=session,
                    metric_path=metric_path,
                    ledger_paths=ledger_paths,
                    copies=copies,
                    observers=observers,
                    identities=identities,
                )
                if tuple(observers[path].identity() for path in database_paths) != (
                    observer_before
                ) or tuple(
                    _source_identity(sources[path], observers[path])
                    for path in database_paths
                ) != identities:
                    raise PreflightStateError(
                        "state changed during preflight inspection"
                    )
            else:
                if _enumerate_sidecars(state_directory, cohort_directories):
                    raise PreflightStateError("SQLite sidecar topology is not safe")
                prospective = _prospective_epoch_id(target, session, ())
                snapshot = PreflightStateSnapshot(
                    state_status="uninitialized",
                    epoch_id=prospective,
                    governed_tickers=benchmarks,
                    cohort_ids_by_ticker={ticker: cohorts for ticker in benchmarks},
                    metric_store_path=None,
                    file_identities=(),
                )
                metric_store = None
                observer_before = ()
        except PreflightStateError:
            raise
        except Exception as error:
            raise PreflightStateError("state inspection failed") from error
        try:
            yielded = True
            yield snapshot, metric_store
        finally:
            if yielded:
                try:
                    for directory in (
                        *directory_chain,
                        *cohort_directories.values(),
                    ):
                        try:
                            directory.verify()
                        except PreflightStateError as error:
                            raise PreflightStateError(
                                "state directory identity changed during preflight"
                            ) from error
                    if missing_directory is not None:
                        missing_parent, missing_name = missing_directory
                        if _entry_stat(missing_parent, missing_name) is not None:
                            raise PreflightStateError(
                                "state topology changed during preflight"
                            )
                    expected_existing = (
                        {_absolute_path(path) for path in database_paths}
                        if complete
                        else set()
                    )
                    current_existing = {
                        _absolute_path(path)
                        for path in database_paths
                        if os.path.lexists(path)
                    }
                    if current_existing != expected_existing or _unexpected_ledgers(
                        state_directory, set(cohorts)
                    ):
                        raise PreflightStateError(
                            "state topology changed during preflight"
                        )
                    for source in sources.values():
                        try:
                            source.verify()
                        except PreflightStateError as error:
                            raise PreflightStateError(
                                "state file identity changed during preflight"
                            ) from error
                    _verify_sidecars(
                        state_directory=state_directory,
                        cohort_directories=cohort_directories,
                        database_paths=active_database_paths,
                        database_parents=database_parents,
                        certified=sidecars,
                        during_probe=True,
                    )
                    if state_directory is not None:
                        try:
                            state_directory.verify(contents=True)
                        except PreflightStateError as error:
                            raise PreflightStateError(
                                "state changed during preflight"
                            ) from error
                    for directory in cohort_directories.values():
                        try:
                            directory.verify(contents=True)
                        except PreflightStateError as error:
                            raise PreflightStateError(
                                "state changed during preflight"
                            ) from error
                    if complete and tuple(
                        observers[path].identity() for path in database_paths
                    ) != observer_before:
                        raise PreflightStateError("state changed during preflight")
                except PreflightStateError as error:
                    verification_error = error
                except Exception:
                    verification_error = PreflightStateError(
                        "state changed during preflight"
                    )
                if verification_error is not None:
                    raise verification_error
