"""Read-only state topology discovery for the governed preflight probe."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
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
        target = Path(path)
        try:
            connection = sqlite3.connect(
                f"{target.resolve().as_uri()}?mode=ro",
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

    def identity(self) -> PreflightFileIdentity:
        stat = self.path.stat()
        row = self.connection.execute("PRAGMA data_version").fetchone()
        if row is None:
            raise PreflightStateError("state data version is unavailable")
        return PreflightFileIdentity(
            path=self.path,
            dev=stat.st_dev,
            ino=stat.st_ino,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            data_version=int(row[0]),
        )

    def close(self) -> None:
        self.connection.close()


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


def _identity_equal(left: PreflightFileIdentity, right: PreflightFileIdentity) -> bool:
    return left == right


def _prospective_epoch_id(
    state_dir: Path, session: date, epochs: Collection[MetricEpoch]
) -> str:
    occupied = {epoch.epoch_id for epoch in epochs}
    digest = hashlib.sha256(
        f"{state_dir.resolve()}|{session.isoformat()}".encode()
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


def _unexpected_ledgers(state_dir: Path, expected: set[Path]) -> bool:
    if not state_dir.is_dir():
        return False
    discovered = {
        path.absolute()
        for path in state_dir.glob("*/portfolio.db")
        if os.path.lexists(path)
    }
    return discovered != {path.absolute() for path in expected if os.path.lexists(path)}


def inspect_preflight_state(
    *,
    state_dir: Path,
    cohort_ids: Collection[str],
    session: date,
    benchmark_tickers: Collection[str],
) -> PreflightStateSnapshot:
    """Discover the exact governed scope without initializing or mutating state."""
    if not isinstance(session, date) or not XNYSCalendar().is_session(session):
        raise PreflightStateError("session is invalid")
    target = Path(state_dir)
    cohorts = _canonical_cohorts(cohort_ids)
    benchmarks = _canonical_tickers(benchmark_tickers)
    metric_path = target / "metrics_v2.sqlite3"
    ledger_paths = {cohort: target / cohort / "portfolio.db" for cohort in cohorts}
    expected_ledgers = set(ledger_paths.values())

    metric_exists = os.path.lexists(metric_path)
    existing_ledgers = {
        cohort for cohort, path in ledger_paths.items() if os.path.lexists(path)
    }
    if _unexpected_ledgers(target, expected_ledgers):
        raise PreflightStateError("state topology has unexpected cohort ledgers")
    if not metric_exists and not existing_ledgers:
        prospective = _prospective_epoch_id(target, session, ())
        membership = {ticker: cohorts for ticker in benchmarks}
        return PreflightStateSnapshot(
            state_status="uninitialized",
            epoch_id=prospective,
            governed_tickers=benchmarks,
            cohort_ids_by_ticker=membership,
            metric_store_path=None,
            file_identities=(),
        )
    if not metric_exists or existing_ledgers != set(cohorts):
        raise PreflightStateError("state is partially initialized")

    observers: list[_Observer] = []
    ledgers: list[PortfolioLedger] = []
    try:
        metric_observer = _Observer.open(metric_path)
        observers.append(metric_observer)
        observer_by_cohort: dict[str, _Observer] = {}
        for cohort in cohorts:
            observer = _Observer.open(ledger_paths[cohort])
            observers.append(observer)
            observer_by_cohort[cohort] = observer
        before = tuple(observer.identity() for observer in observers)

        metric_store = MetricStore.open_existing(metric_path)
        epochs = _load_epochs(metric_store, metric_observer)
        epoch_id, state_status = _select_epoch(
            state_dir=target, session=session, epochs=epochs
        )

        membership: dict[str, set[str]] = {
            ticker: set(cohorts) for ticker in benchmarks
        }
        invalid_reasons: dict[str, str] = {}
        for cohort in cohorts:
            ledger = PortfolioLedger.open_existing(ledger_paths[cohort])
            ledgers.append(ledger)
            if ledger.cohort_id != cohort:
                raise PreflightStateError("ledger cohort identity is inconsistent")
            observer = observer_by_cohort[cohort]
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

        after = tuple(observer.identity() for observer in observers)
        if before != after:
            raise PreflightStateError("state changed during preflight inspection")
        frozen_membership = {
            ticker: tuple(sorted(cohort_set))
            for ticker, cohort_set in sorted(membership.items())
        }
        governed_tickers = tuple(sorted(frozen_membership))
        if len(set(normalize_tickers(list(governed_tickers)))) != len(governed_tickers):
            raise PreflightStateError("governed ticker normalization is ambiguous")
        return PreflightStateSnapshot(
            state_status=state_status,
            epoch_id=epoch_id,
            governed_tickers=governed_tickers,
            cohort_ids_by_ticker=frozen_membership,
            metric_store_path=metric_path,
            file_identities=before,
        )
    except PreflightStateError:
        raise
    except Exception as error:
        raise PreflightStateError("state inspection failed") from error
    finally:
        for ledger in reversed(ledgers):
            ledger.close()
        for observer in reversed(observers):
            observer.close()


@contextmanager
def inspect_and_guard_preflight_state(
    *,
    state_dir: Path,
    cohort_ids: Collection[str],
    session: date,
    benchmark_tickers: Collection[str],
) -> Iterator[tuple[PreflightStateSnapshot, MetricStore | None]]:
    """Inspect state while one observer set spans discovery and resolution."""
    target = Path(state_dir)
    cohorts = _canonical_cohorts(cohort_ids)
    metric_path = target / "metrics_v2.sqlite3"
    expected_ledgers = tuple(target / cohort / "portfolio.db" for cohort in cohorts)
    observed_paths = tuple(
        path for path in (metric_path, *expected_ledgers) if os.path.lexists(path)
    )
    observers: list[_Observer] = []
    verification_error: PreflightStateError | None = None
    yielded = False
    try:
        observers = [_Observer.open(path) for path in observed_paths]
        before = tuple(observer.identity() for observer in observers)
        snapshot = inspect_preflight_state(
            state_dir=target,
            cohort_ids=cohorts,
            session=session,
            benchmark_tickers=benchmark_tickers,
        )
        if tuple(observer.identity() for observer in observers) != before:
            raise PreflightStateError("state changed during preflight inspection")
        if snapshot.file_identities:
            if snapshot.file_identities != before:
                raise PreflightStateError("state identity is inconsistent")
            if snapshot.metric_store_path is None:
                raise PreflightStateError("preflight metric store identity is missing")
            metric_store = MetricStore.open_existing(snapshot.metric_store_path)
            if not metric_store.read_only:
                raise PreflightStateError("governed preflight metric store is writable")
        else:
            if observed_paths or snapshot.metric_store_path is not None:
                raise PreflightStateError(
                    "uninitialized state identity is inconsistent"
                )
            metric_store = None
        try:
            yielded = True
            yield snapshot, metric_store
        finally:
            try:
                if tuple(observer.identity() for observer in observers) != before:
                    verification_error = PreflightStateError(
                        "state changed during preflight"
                    )
                expected_existing = {
                    identity.path.absolute() for identity in snapshot.file_identities
                }
                current_existing = {
                    path.absolute()
                    for path in (metric_path, *expected_ledgers)
                    if os.path.lexists(path)
                }
                if current_existing != expected_existing or _unexpected_ledgers(
                    target, set(expected_ledgers)
                ):
                    verification_error = PreflightStateError(
                        "state topology changed during preflight"
                    )
            except Exception:
                verification_error = PreflightStateError(
                    "state changed during preflight"
                )
    except PreflightStateError:
        raise
    except Exception as error:
        if yielded:
            raise
        raise PreflightStateError("state guard failed") from error
    finally:
        for observer in reversed(observers):
            observer.close()
        if verification_error is not None:
            raise verification_error


@contextmanager
def guard_preflight_state(
    snapshot: PreflightStateSnapshot,
) -> Iterator[MetricStore | None]:
    """Hold RO observers across governed resolution and reject concurrent writes."""
    if not snapshot.file_identities:
        if snapshot.metric_store_path is not None:
            raise PreflightStateError("preflight snapshot identity is incomplete")
        yield None
        return

    observers: list[_Observer] = []
    verification_error: PreflightStateError | None = None
    try:
        for expected in snapshot.file_identities:
            observer = _Observer.open(expected.path)
            observers.append(observer)
            if not _identity_equal(observer.identity(), expected):
                raise PreflightStateError("state changed before governed preflight")
        if snapshot.metric_store_path is None:
            raise PreflightStateError("preflight metric store identity is missing")
        metric_store = MetricStore.open_existing(snapshot.metric_store_path)
        if not metric_store.read_only:
            raise PreflightStateError("governed preflight metric store is writable")
        try:
            yield metric_store
        finally:
            for expected, observer in zip(
                snapshot.file_identities, observers, strict=True
            ):
                if not _identity_equal(observer.identity(), expected):
                    verification_error = PreflightStateError(
                        "state changed during preflight"
                    )
                    break
    except PreflightStateError:
        raise
    except Exception as error:
        raise PreflightStateError("state guard failed") from error
    finally:
        for observer in reversed(observers):
            observer.close()
        if verification_error is not None:
            raise verification_error
