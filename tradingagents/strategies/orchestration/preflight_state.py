"""Read-only state topology discovery for the governed preflight probe.

Snapshots SQLite state into a temporary directory via ``_io.snapshot_sqlite``
(``sqlite3.Connection.backup``) so active or non-empty WAL files are captured
consistently without ever mutating or blocking live state.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Collection, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType

from tradingagents.strategies.data_sources.yfinance_source import normalize_tickers
from tradingagents.strategies.metrics.calendar import XNYSCalendar
from tradingagents.strategies.metrics.models import MetricEpoch
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.orchestration._io import open_sqlite, snapshot_sqlite
from tradingagents.strategies.state.generation_db import GENERATION_DB_FILENAME
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

_MAX_COHORTS = 64
_MAX_TICKERS = 2_048
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
    data_version: int = 0


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


def _canonical_cohorts(cohort_ids: Collection[str]) -> tuple[str, ...]:
    items = tuple(sorted({str(cid).strip() for cid in cohort_ids if str(cid).strip()}))
    if not items or len(items) != len(tuple(cohort_ids)) or len(items) > _MAX_COHORTS:
        raise PreflightStateError("cohort roster is invalid")
    if any("/" in cid or "\\" in cid or cid in {".", ".."} for cid in items):
        raise PreflightStateError("cohort roster is invalid")
    return items


def _canonical_tickers(tickers: Collection[str]) -> tuple[str, ...]:
    cleaned = tuple(sorted({str(t).strip().upper() for t in tickers if str(t).strip()}))
    if not cleaned or len(cleaned) > _MAX_TICKERS:
        raise PreflightStateError("governed ticker set is invalid")
    normalized = normalize_tickers(list(cleaned))
    if len(set(normalized)) != len(cleaned):
        raise PreflightStateError("governed ticker normalization is ambiguous")
    return cleaned


def _prospective_epoch_id(state_dir: Path, session: date, epochs: tuple[MetricEpoch, ...]) -> str:
    payload = "|".join(
        [str(state_dir), session.isoformat(), *(e.epoch_id for e in epochs)]
    )
    return "preflight-prospective-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _select_epoch(
    *, state_dir: Path, session: date, epochs: tuple[MetricEpoch, ...]
) -> tuple[str, str]:
    ordered = tuple(sorted(epochs, key=lambda e: (e.start_session, e.epoch_id)))
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


def _file_identity(path: Path) -> PreflightFileIdentity:
    st = path.stat()
    return PreflightFileIdentity(
        path=path,
        dev=st.st_dev,
        ino=st.st_ino,
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
    )


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise PreflightStateError("state path symlink is invalid")


def _discover_unified_db(
    *,
    db_path: Path,
    tmp_dir: Path,
    target: Path,
    cohorts: tuple[str, ...],
    benchmarks: tuple[str, ...],
    session: date,
) -> tuple[PreflightStateSnapshot, MetricStore | None]:
    _reject_symlink(db_path)
    snap_path = snapshot_sqlite(db_path, tmp_dir / GENERATION_DB_FILENAME)
    membership: dict[str, set[str]] = {t: set(cohorts) for t in benchmarks}
    session_iso = session.isoformat()
    state_status = "ready"
    epochs: list[MetricEpoch] = []
    metric_store: MetricStore | None = None

    with open_sqlite(snap_path, readonly=True) as conn:
        epoch_rows = conn.execute(
            "SELECT epoch_id, generation_id, schema_version, status, start_session, end_session "
            "FROM metric_epochs ORDER BY start_session, epoch_id"
        ).fetchall()
        for row in epoch_rows:
            epochs.append(
                MetricEpoch(
                    epoch_id=row["epoch_id"],
                    generation_id=row["generation_id"],
                    generation_commit="",
                    behavior_hash="",
                    config_hash="",
                    metric_schema_version=int(row["schema_version"]),
                    execution_clock_version="",
                    pricing_version="",
                    cost_model_version="",
                    start_session=date.fromisoformat(row["start_session"]),
                    end_session=date.fromisoformat(row["end_session"]) if row["end_session"] else None,
                    status=row["status"],
                    boundary_reason="",
                )
            )
        epoch_id, state_status = _select_epoch(
            state_dir=target, session=session, epochs=tuple(epochs)
        )
        # Open lots across cohorts
        for row in conn.execute(
            "SELECT DISTINCT cohort_id, ticker FROM lots WHERE open_qty > 0"
        ).fetchall():
            if row["cohort_id"] in cohorts:
                membership.setdefault(str(row["ticker"]).upper(), set()).add(row["cohort_id"])
        # Pending order intents due for this session
        for row in conn.execute(
            "SELECT DISTINCT i.cohort_id, s.ticker "
            "FROM order_intents i "
            "JOIN intent_signals isg ON isg.intent_id = i.intent_id "
            "JOIN signals s ON s.signal_id = isg.signal_id "
            "WHERE i.status = 'pending' AND ("
            "  (i.price_rule = 'next_session_open' AND i.eligible_session = ?) "
            "  OR (i.price_rule = 'resting_stop' AND i.eligible_session <= ?)"
            ")",
            (session_iso, session_iso),
        ).fetchall():
            if row["cohort_id"] in cohorts:
                membership.setdefault(str(row["ticker"]).upper(), set()).add(row["cohort_id"])
        # Invalidated sessions
        invalid_cohorts = {
            row["cohort_id"]
            for row in conn.execute(
                "SELECT cohort_id FROM session_runs WHERE session = ? AND valid = 0",
                (session_iso,),
            ).fetchall()
            if row["cohort_id"] in cohorts
        }
        if invalid_cohorts:
            state_status = "state_already_invalid"

    try:
        metric_store = MetricStore.open_existing(snap_path, immutable=True)
    except Exception:
        metric_store = None

    governed_tickers = _canonical_tickers(membership.keys())
    frozen_membership = {t: tuple(sorted(membership[t])) for t in governed_tickers}
    return (
        PreflightStateSnapshot(
            state_status=state_status,
            epoch_id=epoch_id,
            governed_tickers=governed_tickers,
            cohort_ids_by_ticker=frozen_membership,
            metric_store_path=db_path,
            file_identities=(_file_identity(db_path),),
        ),
        metric_store,
    )


def _discover_legacy_dbs(
    *,
    metric_path: Path,
    ledger_paths: Mapping[str, Path],
    tmp_dir: Path,
    target: Path,
    cohorts: tuple[str, ...],
    benchmarks: tuple[str, ...],
    session: date,
) -> tuple[PreflightStateSnapshot, MetricStore]:
    from .market_dependencies import portfolio_tickers

    snap_metric = snapshot_sqlite(metric_path, tmp_dir / "metrics_v2.sqlite3")
    metric_store = MetricStore.open_existing(snap_metric, immutable=True)
    with open_sqlite(snap_metric, readonly=True) as conn:
        epoch_ids = [
            row[0] for row in conn.execute("SELECT epoch_id FROM metric_epochs").fetchall()
        ]
    epochs = tuple(metric_store.load_epoch(eid) for eid in epoch_ids)
    epoch_id, state_status = _select_epoch(state_dir=target, session=session, epochs=epochs)
    membership: dict[str, set[str]] = {t: set(cohorts) for t in benchmarks}
    identities: list[PreflightFileIdentity] = [_file_identity(metric_path)]
    invalid_reasons: dict[str, str] = {}

    for idx, cohort in enumerate(cohorts):
        src = ledger_paths[cohort]
        _reject_symlink(src)
        snap_ledger = snapshot_sqlite(src, tmp_dir / f"cohort-{idx:03d}.db")
        identities.append(_file_identity(src))
        ledger = PortfolioLedger.open_existing(snap_ledger, immutable=True)
        try:
            if ledger.cohort_id != cohort:
                raise PreflightStateError("ledger cohort identity is inconsistent")
            reason = ledger.session_invalid_reason(session)
            if reason:
                invalid_reasons[cohort] = reason
            for ticker in portfolio_tickers(ledger, session):
                membership.setdefault(str(ticker).upper(), set()).add(cohort)
        finally:
            ledger.close()

    if invalid_reasons:
        if len(invalid_reasons) != len(cohorts) or len(set(invalid_reasons.values())) != 1:
            raise PreflightStateError("cohort session invalidation topology is inconsistent")
        state_status = "state_already_invalid"

    governed_tickers = _canonical_tickers(membership.keys())
    frozen_membership = {t: tuple(sorted(membership[t])) for t in governed_tickers}
    return (
        PreflightStateSnapshot(
            state_status=state_status,
            epoch_id=epoch_id,
            governed_tickers=governed_tickers,
            cohort_ids_by_ticker=frozen_membership,
            metric_store_path=metric_path,
            file_identities=tuple(identities),
        ),
        metric_store,
    )


def inspect_preflight_state(
    *,
    state_dir: Path,
    cohort_ids: Collection[str],
    session: date,
    benchmark_tickers: Collection[str],
) -> PreflightStateSnapshot:
    """Discover exact governed scope through a consistent SQLite backup snapshot."""
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
    """Snapshot SQLite state via Connection.backup() and yield read-only state."""
    if not isinstance(session, date) or not XNYSCalendar().is_session(session):
        raise PreflightStateError("session is invalid")
    target = Path(os.path.abspath(os.fspath(state_dir)))
    _reject_symlink(target)
    cohorts = _canonical_cohorts(cohort_ids)
    benchmarks = _canonical_tickers(benchmark_tickers)

    unified_path = target / GENERATION_DB_FILENAME
    metric_path = target / "metrics_v2.sqlite3"
    ledger_paths = {cohort: target / cohort / "portfolio.db" for cohort in cohorts}

    with ExitStack() as stack:
        tmp_dir = Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix="eventedge-preflight-state-"))
        )
        try:
            if unified_path.exists() or unified_path.is_symlink():
                snapshot, metric_store = _discover_unified_db(
                    db_path=unified_path,
                    tmp_dir=tmp_dir,
                    target=target,
                    cohorts=cohorts,
                    benchmarks=benchmarks,
                    session=session,
                )
            else:
                metric_exists = metric_path.exists() or metric_path.is_symlink()
                existing_ledgers = {
                    c for c, p in ledger_paths.items() if p.exists() or p.is_symlink()
                }
                if metric_exists:
                    _reject_symlink(metric_path)
                if not metric_exists and not existing_ledgers:
                    snapshot = PreflightStateSnapshot(
                        state_status="uninitialized",
                        epoch_id=_prospective_epoch_id(target, session, ()),
                        governed_tickers=benchmarks,
                        cohort_ids_by_ticker={t: cohorts for t in benchmarks},
                        metric_store_path=None,
                        file_identities=(),
                    )
                    metric_store = None
                elif not metric_exists or existing_ledgers != set(cohorts):
                    raise PreflightStateError("state is partially initialized")
                else:
                    snapshot, metric_store = _discover_legacy_dbs(
                        metric_path=metric_path,
                        ledger_paths=ledger_paths,
                        tmp_dir=tmp_dir,
                        target=target,
                        cohorts=cohorts,
                        benchmarks=benchmarks,
                        session=session,
                    )
        except PreflightStateError:
            raise
        except Exception as error:
            raise PreflightStateError(f"state inspection failed: {error}") from error
        yield snapshot, metric_store
        # Verify state topology did not change while preflight executed.
        if snapshot.state_status == "uninitialized":
            watched = (unified_path, metric_path, *ledger_paths.values())
            if any(p.exists() or p.is_symlink() for p in watched):
                raise PreflightStateError("state topology changed during preflight")
        else:
            for ident in snapshot.file_identities:
                if ident.path.is_symlink() or not ident.path.exists():
                    raise PreflightStateError("state topology changed during preflight")
                current = _file_identity(ident.path)
                if (current.dev, current.ino, current.size, current.mtime_ns) != (
                    ident.dev,
                    ident.ino,
                    ident.size,
                    ident.mtime_ns,
                ):
                    raise PreflightStateError("state topology changed during preflight")
