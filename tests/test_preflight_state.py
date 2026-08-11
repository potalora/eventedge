from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tradingagents.strategies.execution import Fill, OrderIntent, SignalRecord
from tradingagents.strategies.metrics.models import METRIC_SCHEMA_VERSION, MetricEpoch
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


SESSION = date(2026, 8, 10)
UTC = timezone.utc
COHORTS = ("cohort-a", "cohort-b")


def _epoch(
    *,
    epoch_id: str = "epoch-current",
    start_session: date = date(2026, 8, 3),
) -> MetricEpoch:
    return MetricEpoch(
        epoch_id=epoch_id,
        generation_id="gen-test",
        generation_commit="abc123",
        behavior_hash="behavior",
        config_hash="config",
        metric_schema_version=METRIC_SCHEMA_VERSION,
        execution_clock_version="clock-v1",
        pricing_version="pricing-v1",
        cost_model_version="cost-v1",
        start_session=start_session,
        end_session=None,
        status="open",
        boundary_reason="initial",
    )


def _signal(
    ticker: str,
    *,
    signal_id: str,
    epoch_id: str = "epoch-current",
    reference_session: date = date(2026, 8, 7),
) -> SignalRecord:
    observed = datetime.combine(
        reference_session, datetime.min.time(), tzinfo=UTC
    ).replace(hour=20)
    return SignalRecord(
        signal_id=signal_id,
        epoch_id=epoch_id,
        policy_id="policy",
        event_key=f"event-{signal_id}",
        strategy="fixture",
        ticker=ticker,
        direction="long",
        event_at=observed,
        observed_at=observed,
        reference_session=reference_session,
        reference_close=Decimal("100"),
        decision_at=observed.replace(hour=21),
        evidence_hash=f"evidence-{signal_id}",
    )


def _intent(
    cohort_id: str,
    signal: SignalRecord,
    *,
    intent_id: str,
    eligible_session: date,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        signal_ids=(signal.signal_id,),
        cohort_id=cohort_id,
        side="buy",
        requested_qty=2,
        created_at=signal.decision_at,
        eligible_session=eligible_session,
        price_rule="next_session_open",
        status="pending",
        stop_price=None,
        external_order_id=None,
    )


def _initialize_state(
    state_dir: Path,
    *,
    cohort_ids: tuple[str, ...] = COHORTS,
    epoch: MetricEpoch | None = None,
) -> None:
    store = MetricStore(state_dir / "metrics_v2.sqlite3")
    store.save_epoch(epoch or _epoch())
    for cohort_id in cohort_ids:
        ledger = PortfolioLedger(
            state_dir / cohort_id / "portfolio.db", cohort_id, Decimal("5000")
        )
        ledger.close()


def _seed_clean_sidecars(database_path: Path) -> tuple[Path, Path]:
    """Create platform-independent clean sidecars for topology tests."""
    wal_path = Path(f"{database_path}-wal")
    shm_path = Path(f"{database_path}-shm")
    if wal_path.exists():
        assert wal_path.stat().st_size == 0
    else:
        wal_path.touch()
    if not shm_path.exists():
        shm_path.touch()
    return wal_path, shm_path


def _sqlite_identity(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    encoded = path.resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(encoded, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        version = int(connection.execute("PRAGMA data_version").fetchone()[0])
    finally:
        connection.close()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, version


def test_zero_stores_is_uninitialized_with_only_configured_benchmarks(
    tmp_path: Path,
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_preflight_state,
    )

    snapshot = inspect_preflight_state(
        state_dir=tmp_path / "missing",
        cohort_ids=COHORTS,
        session=SESSION,
        benchmark_tickers=("SPY", "BIL"),
    )

    assert snapshot.state_status == "uninitialized"
    assert snapshot.epoch_id.startswith("preflight-prospective-")
    assert snapshot.governed_tickers == ("BIL", "SPY")
    assert dict(snapshot.cohort_ids_by_ticker) == {
        "BIL": COHORTS,
        "SPY": COHORTS,
    }
    assert snapshot.metric_store_path is None
    assert snapshot.file_identities == ()

    with pytest.raises(PreflightStateError, match="normalization"):
        inspect_preflight_state(
            state_dir=tmp_path / "also-missing",
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("BRK/A", "BRK-A"),
        )


@pytest.mark.parametrize("missing", ["metric", "one-ledger"])
def test_partial_initialization_fails_closed(tmp_path: Path, missing: str) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_preflight_state,
    )

    state_dir = tmp_path / "state"
    if missing == "metric":
        ledger = PortfolioLedger(
            state_dir / COHORTS[0] / "portfolio.db", COHORTS[0], Decimal("5000")
        )
        ledger.close()
    else:
        _initialize_state(state_dir, cohort_ids=(COHORTS[0],))

    with pytest.raises(PreflightStateError, match="partially initialized"):
        inspect_preflight_state(
            state_dir=state_dir,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY", "BIL"),
        )


def test_complete_state_uses_read_only_openers_and_closes_ledgers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        inspect_preflight_state,
    )

    state_dir = tmp_path / "state"
    _initialize_state(state_dir)
    opened: list[str] = []
    opened_paths: list[Path] = []
    closed: list[str] = []
    metric_open = MetricStore.open_existing.__func__
    ledger_open = PortfolioLedger.open_existing.__func__
    ledger_close = PortfolioLedger.close
    sqlite_connect = sqlite3.connect
    sqlite_targets: list[str] = []

    def connect_snapshot(database, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        encoded = str(database)
        assert str(state_dir) not in encoded
        sqlite_targets.append(encoded)
        return sqlite_connect(database, *args, **kwargs)

    def forbid_metric_init(self, path):  # noqa: ANN001
        raise AssertionError(f"writable MetricStore opened: {path}")

    def forbid_ledger_init(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("writable PortfolioLedger opened")

    def open_metric(cls, path, *, immutable=False):  # noqa: ANN001
        assert immutable is True
        opened_path = Path(path)
        assert not opened_path.resolve().is_relative_to(state_dir.resolve())
        opened_paths.append(opened_path)
        opened.append("metric")
        return metric_open(cls, path, immutable=immutable)

    def open_ledger(cls, path, *, immutable=False):  # noqa: ANN001
        assert immutable is True
        opened_path = Path(path)
        assert not opened_path.resolve().is_relative_to(state_dir.resolve())
        opened_paths.append(opened_path)
        ledger = ledger_open(cls, path, immutable=immutable)
        opened.append(ledger.cohort_id)
        return ledger

    def close_ledger(self):  # noqa: ANN001
        closed.append(self.cohort_id)
        ledger_close(self)

    monkeypatch.setattr(MetricStore, "__init__", forbid_metric_init)
    monkeypatch.setattr(MetricStore, "open_existing", classmethod(open_metric))
    monkeypatch.setattr(PortfolioLedger, "__init__", forbid_ledger_init)
    monkeypatch.setattr(PortfolioLedger, "open_existing", classmethod(open_ledger))
    monkeypatch.setattr(PortfolioLedger, "close", close_ledger)
    monkeypatch.setattr(sqlite3, "connect", connect_snapshot)

    snapshot = inspect_preflight_state(
        state_dir=state_dir,
        cohort_ids=COHORTS,
        session=SESSION,
        benchmark_tickers=("SPY", "BIL"),
    )

    assert snapshot.state_status == "ready"
    assert snapshot.metric_store_path == state_dir / "metrics_v2.sqlite3"
    assert opened == ["metric", *COHORTS]
    assert sorted(closed) == list(COHORTS)
    assert sqlite_targets
    assert opened_paths and all(not path.exists() for path in opened_paths)


def test_exact_governed_tickers_and_all_relevant_cohort_membership(
    tmp_path: Path,
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        inspect_preflight_state,
    )

    state_dir = tmp_path / "state"
    _initialize_state(state_dir)

    first = PortfolioLedger.open_existing(
        state_dir / COHORTS[0] / "portfolio.db", immutable=True
    )
    first.close()
    writable_first = PortfolioLedger(
        state_dir / COHORTS[0] / "portfolio.db", COHORTS[0], Decimal("5000")
    )
    pending = _signal("PEND", signal_id="pending")
    writable_first.record_signal(pending)
    writable_first.stage_intent(
        _intent(
            COHORTS[0], pending, intent_id="pending-intent", eligible_session=SESSION
        )
    )
    writable_first.close()

    second = PortfolioLedger(
        state_dir / COHORTS[1] / "portfolio.db", COHORTS[1], Decimal("5000")
    )
    due = _signal(
        "DUE",
        signal_id="due",
        reference_session=date(2026, 8, 3),
    )
    second.record_signal(due)
    opened = _signal(
        "OPEN",
        signal_id="open",
        reference_session=date(2026, 7, 29),
    )
    second.record_signal(opened)
    open_intent = _intent(
        COHORTS[1],
        opened,
        intent_id="open-intent",
        eligible_session=date(2026, 7, 30),
    )
    second.stage_intent(open_intent)
    second.apply_fill(
        open_intent,
        Fill(
            "open-fill",
            open_intent.intent_id,
            "buy",
            date(2026, 7, 30),
            datetime(2026, 7, 30, 13, 30, tzinfo=UTC),
            datetime(2026, 7, 30, 22, tzinfo=UTC),
            Decimal("100"),
            Decimal("100"),
            2,
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
        ),
    )
    second.close()

    snapshot = inspect_preflight_state(
        state_dir=state_dir,
        cohort_ids=COHORTS,
        session=SESSION,
        benchmark_tickers=("SPY", "BIL"),
    )

    assert snapshot.governed_tickers == ("BIL", "DUE", "OPEN", "PEND", "SPY")
    assert dict(snapshot.cohort_ids_by_ticker) == {
        "BIL": COHORTS,
        "DUE": (COHORTS[1],),
        "OPEN": (COHORTS[1],),
        "PEND": (COHORTS[0],),
        "SPY": COHORTS,
    }


def test_closed_older_epoch_uses_nonmatching_prospective_identity(
    tmp_path: Path,
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        inspect_preflight_state,
    )

    state_dir = tmp_path / "state"
    _initialize_state(state_dir, epoch=_epoch(epoch_id="old-epoch"))
    MetricStore(state_dir / "metrics_v2.sqlite3").close_epoch(
        "old-epoch", date(2026, 8, 7), "semantic_hash_changed"
    )
    ledger = PortfolioLedger(
        state_dir / COHORTS[0] / "portfolio.db", COHORTS[0], Decimal("5000")
    )
    ledger.record_signal(
        _signal(
            "OLDOUTCOME",
            signal_id="old-due",
            epoch_id="old-epoch",
            reference_session=date(2026, 7, 31),
        )
    )
    ledger.close()

    snapshot = inspect_preflight_state(
        state_dir=state_dir,
        cohort_ids=COHORTS,
        session=SESSION,
        benchmark_tickers=("SPY",),
    )

    assert snapshot.epoch_id != "old-epoch"
    assert snapshot.governed_tickers == ("SPY",)


def test_already_invalid_requested_session_is_reported(tmp_path: Path) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        inspect_preflight_state,
    )

    state_dir = tmp_path / "state"
    _initialize_state(state_dir)
    MetricStore(state_dir / "metrics_v2.sqlite3").invalidate_epoch(
        "epoch-current", SESSION, "critical_market_data_gap"
    )

    snapshot = inspect_preflight_state(
        state_dir=state_dir,
        cohort_ids=COHORTS,
        session=SESSION,
        benchmark_tickers=("SPY",),
    )

    assert snapshot.state_status == "state_already_invalid"
    assert snapshot.epoch_id == "epoch-current"

    historical = inspect_preflight_state(
        state_dir=state_dir,
        cohort_ids=COHORTS,
        session=date(2026, 8, 7),
        benchmark_tickers=("SPY",),
    )
    assert historical.state_status == "state_already_invalid"


def test_future_or_unreadable_state_fails_closed(tmp_path: Path) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_preflight_state,
    )

    future = tmp_path / "future"
    _initialize_state(future, epoch=_epoch(start_session=date(2026, 8, 11)))
    with pytest.raises(PreflightStateError, match="epoch identity"):
        inspect_preflight_state(
            state_dir=future,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        )

    corrupt = tmp_path / "corrupt"
    _initialize_state(corrupt)
    ledger_path = corrupt / COHORTS[1] / "portfolio.db"
    ledger_path.write_bytes(b"not sqlite")
    with pytest.raises(PreflightStateError, match="state inspection failed"):
        inspect_preflight_state(
            state_dir=corrupt,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        )


def test_overlapping_epochs_unexpected_ledger_and_embedded_identity_fail_closed(
    tmp_path: Path,
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_preflight_state,
    )

    overlapping = tmp_path / "overlapping"
    _initialize_state(overlapping)
    MetricStore(overlapping / "metrics_v2.sqlite3").save_epoch(
        _epoch(epoch_id="second-open", start_session=date(2026, 8, 4))
    )
    with pytest.raises(PreflightStateError, match="epoch identity"):
        inspect_preflight_state(
            state_dir=overlapping,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        )

    unexpected = tmp_path / "unexpected"
    _initialize_state(unexpected)
    extra = PortfolioLedger(
        unexpected / "retired-cohort" / "portfolio.db",
        "retired-cohort",
        Decimal("5000"),
    )
    extra.close()
    with pytest.raises(PreflightStateError, match="unexpected cohort ledgers"):
        inspect_preflight_state(
            state_dir=unexpected,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        )

    mismatched = tmp_path / "mismatched"
    store = MetricStore(mismatched / "metrics_v2.sqlite3")
    store.save_epoch(_epoch())
    for cohort_id in COHORTS:
        embedded = "wrong-cohort" if cohort_id == COHORTS[0] else cohort_id
        ledger = PortfolioLedger(
            mismatched / cohort_id / "portfolio.db", embedded, Decimal("5000")
        )
        ledger.close()
    with pytest.raises(PreflightStateError, match="cohort identity"):
        inspect_preflight_state(
            state_dir=mismatched,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        )


def test_epoch_row_identity_and_ledger_invalidation_topology_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_preflight_state,
    )

    mismatched_epoch = tmp_path / "epoch-row"
    _initialize_state(mismatched_epoch)
    connection = sqlite3.connect(mismatched_epoch / "metrics_v2.sqlite3")
    try:
        connection.execute(
            "UPDATE metric_epochs SET payload_json = "
            "json_set(payload_json, '$.epoch_id', 'payload-other') "
            "WHERE epoch_id = 'epoch-current'"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(PreflightStateError, match="epoch identity"):
        inspect_preflight_state(
            state_dir=mismatched_epoch,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        )

    invalid = tmp_path / "ledger-invalid"
    _initialize_state(invalid)
    first = PortfolioLedger(
        invalid / COHORTS[0] / "portfolio.db", COHORTS[0], Decimal("5000")
    )
    first.invalidate_session(SESSION, "critical_market_data_gap")
    first.close()
    closed: list[str] = []
    original_close = PortfolioLedger.close

    def tracked_close(self):  # noqa: ANN001
        closed.append(self.cohort_id)
        original_close(self)

    monkeypatch.setattr(PortfolioLedger, "close", tracked_close)
    with pytest.raises(PreflightStateError, match="invalidation.*inconsistent"):
        inspect_preflight_state(
            state_dir=invalid,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        )
    assert sorted(closed) == list(COHORTS)

    second = PortfolioLedger(
        invalid / COHORTS[1] / "portfolio.db", COHORTS[1], Decimal("5000")
    )
    second.invalidate_session(SESSION, "critical_market_data_gap")
    second.close()
    snapshot = inspect_preflight_state(
        state_dir=invalid,
        cohort_ids=COHORTS,
        session=SESSION,
        benchmark_tickers=("SPY",),
    )
    assert snapshot.state_status == "state_already_invalid"


def test_identity_and_data_version_remain_stable_and_context_detects_mutation(
    tmp_path: Path,
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_and_guard_preflight_state,
        inspect_preflight_state,
    )

    state_dir = tmp_path / "state"
    _initialize_state(state_dir)
    paths = [
        state_dir / "metrics_v2.sqlite3",
        *(state_dir / cohort / "portfolio.db" for cohort in COHORTS),
    ]
    before = {path: _sqlite_identity(path) for path in paths}
    inspect_preflight_state(
        state_dir=state_dir,
        cohort_ids=COHORTS,
        session=SESSION,
        benchmark_tickers=("SPY",),
    )
    after = {path: _sqlite_identity(path) for path in paths}
    assert after == before

    ledger_path = state_dir / COHORTS[0] / "portfolio.db"
    with pytest.raises(PreflightStateError, match="changed during preflight"):
        with inspect_and_guard_preflight_state(
            state_dir=state_dir,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        ) as (_, metric_store):
            assert metric_store is not None and metric_store.read_only
            writer = sqlite3.connect(ledger_path)
            try:
                writer.execute("PRAGMA user_version=7")
                writer.commit()
            finally:
                writer.close()


def test_certified_temp_snapshot_lives_through_guard_body_and_is_cleaned(
    tmp_path: Path,
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        inspect_and_guard_preflight_state,
    )

    state_dir = tmp_path / "state"
    _initialize_state(state_dir)
    snapshot_path: Path | None = None
    with inspect_and_guard_preflight_state(
        state_dir=state_dir,
        cohort_ids=COHORTS,
        session=SESSION,
        benchmark_tickers=("SPY",),
    ) as (snapshot, metric_store):
        assert snapshot.metric_store_path == state_dir / "metrics_v2.sqlite3"
        assert metric_store is not None
        snapshot_path = metric_store.path
        assert snapshot_path.exists()
        assert not snapshot_path.resolve().is_relative_to(state_dir.resolve())
        assert metric_store.current_epoch() == _epoch()
    assert snapshot_path is not None and not snapshot_path.exists()


@pytest.mark.parametrize(
    "mutation", ("appearance", "disappearance", "replacement", "change")
)
def test_sidecar_topology_and_identity_changes_fail_closed(
    tmp_path: Path, mutation: str
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_and_guard_preflight_state,
    )

    state_dir = tmp_path / mutation
    _initialize_state(state_dir)
    metric_path = state_dir / "metrics_v2.sqlite3"
    wal_path, shm_path = _seed_clean_sidecars(metric_path)
    target = wal_path if mutation == "appearance" else shm_path
    if mutation == "appearance":
        assert wal_path.stat().st_size == 0
        wal_path.unlink()
    mutated = False

    with pytest.raises(PreflightStateError, match="changed during preflight"):
        with inspect_and_guard_preflight_state(
            state_dir=state_dir,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        ):
            if mutation == "appearance":
                target.touch()
            elif mutation == "disappearance":
                target.unlink()
            elif mutation == "replacement":
                replacement = state_dir / "replacement.shm"
                shutil.copy2(target, replacement)
                os.replace(replacement, target)
            else:
                stat_result = target.stat()
                os.utime(
                    target,
                    ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1),
                )
            mutated = True
    assert mutated is True


def test_state_change_overrides_body_error_at_guard_exit(tmp_path: Path) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_and_guard_preflight_state,
    )

    state_dir = tmp_path / "state"
    _initialize_state(state_dir)
    _, shm_path = _seed_clean_sidecars(state_dir / "metrics_v2.sqlite3")
    with pytest.raises(PreflightStateError, match="changed during preflight"):
        with inspect_and_guard_preflight_state(
            state_dir=state_dir,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        ):
            shm_path.unlink()
            raise RuntimeError("resolver failed concurrently")


def test_clean_sidecars_are_preserved_and_stale_wal_never_creates_shm(
    tmp_path: Path,
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_preflight_state,
    )

    state_dir = tmp_path / "state"
    _initialize_state(state_dir)
    metric_path = state_dir / "metrics_v2.sqlite3"
    ledger_path = state_dir / COHORTS[0] / "portfolio.db"
    sidecars: list[Path] = []
    for path in (metric_path, ledger_path):
        wal, shm = _seed_clean_sidecars(path)
        assert wal.exists() and wal.stat().st_size == 0
        assert shm.exists()
        assert not Path(f"{path}-journal").exists()
        sidecars.extend((wal, shm))
    before_clean = {
        path: (
            path.stat().st_dev,
            path.stat().st_ino,
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in sidecars
    }

    snapshot = inspect_preflight_state(
        state_dir=state_dir,
        cohort_ids=COHORTS,
        session=SESSION,
        benchmark_tickers=("SPY",),
    )

    assert snapshot.state_status == "ready"
    assert {
        path: (
            path.stat().st_dev,
            path.stat().st_ino,
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in sidecars
    } == before_clean

    wal_path = Path(f"{metric_path}-wal")
    shm_path = Path(f"{metric_path}-shm")
    shm_path.unlink()
    wal_path.write_bytes(b"stale-wal-evidence")
    os.utime(wal_path, ns=(1_700_000_000_000_000_000,) * 2)
    before = wal_path.stat()
    assert not shm_path.exists()

    with pytest.raises(PreflightStateError, match="SQLite sidecar"):
        inspect_preflight_state(
            state_dir=state_dir,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        )

    after = wal_path.stat()
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert not shm_path.exists()


def test_symlink_and_atomic_database_replacement_fail_closed(tmp_path: Path) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_and_guard_preflight_state,
        inspect_preflight_state,
    )

    symlink_state = tmp_path / "symlink"
    _initialize_state(symlink_state)
    metric_path = symlink_state / "metrics_v2.sqlite3"
    target_path = symlink_state / "metrics-real.sqlite3"
    metric_path.rename(target_path)
    metric_path.symlink_to(target_path.name)
    with pytest.raises(PreflightStateError, match="symlink"):
        inspect_preflight_state(
            state_dir=symlink_state,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        )

    replacement_state = tmp_path / "replacement"
    _initialize_state(replacement_state)
    metric_path = replacement_state / "metrics_v2.sqlite3"
    with pytest.raises(PreflightStateError, match="identity.*changed"):
        with inspect_and_guard_preflight_state(
            state_dir=replacement_state,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        ):
            replacement = replacement_state / "replacement.sqlite3"
            shutil.copy2(metric_path, replacement)
            os.replace(replacement, metric_path)


@pytest.mark.parametrize("component", ("state-parent", "cohort"))
def test_symlinked_existing_path_components_fail_closed(
    tmp_path: Path, component: str
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_preflight_state,
    )

    real_parent = tmp_path / "real-parent"
    state_dir = real_parent / "state"
    _initialize_state(state_dir)
    if component == "state-parent":
        alias = tmp_path / "state-parent-alias"
        alias.symlink_to(real_parent, target_is_directory=True)
        inspected_state = alias / "state"
    else:
        real_cohort = state_dir / "cohort-a-real"
        (state_dir / COHORTS[0]).rename(real_cohort)
        (state_dir / COHORTS[0]).symlink_to(
            real_cohort.name, target_is_directory=True
        )
        inspected_state = state_dir

    with pytest.raises(PreflightStateError, match="symlink"):
        inspect_preflight_state(
            state_dir=inspected_state,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        )


def test_corrupt_second_database_closes_previously_certified_fds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_and_guard_preflight_state,
    )

    state_dir = tmp_path / "state"
    _initialize_state(state_dir)
    (state_dir / COHORTS[0] / "portfolio.db").write_bytes(b"not sqlite")
    real_open = os.open
    real_close = os.close
    opened: dict[int, Path] = {}
    closed: set[int] = set()

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):  # noqa: ANN001
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        candidate = Path(path)
        if candidate.name in {"metrics_v2.sqlite3", "portfolio.db"}:
            opened[fd] = candidate
        return fd

    def tracked_close(fd):  # noqa: ANN001
        closed.add(fd)
        real_close(fd)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "close", tracked_close)

    with pytest.raises(PreflightStateError):
        with inspect_and_guard_preflight_state(
            state_dir=state_dir,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        ):
            pass

    assert len(opened) >= 2
    assert set(opened) <= closed


def test_source_disappearance_during_certification_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_preflight_state,
    )

    state_dir = tmp_path / "state"
    _initialize_state(state_dir)
    disappearing = state_dir / COHORTS[0] / "portfolio.db"
    real_open = os.open
    removed = False

    def remove_then_open(path, flags, mode=0o777, *, dir_fd=None):  # noqa: ANN001
        nonlocal removed
        if Path(path).name == "portfolio.db" and not removed:
            disappearing.unlink()
            removed = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", remove_then_open)
    with pytest.raises(PreflightStateError, match="certification"):
        inspect_preflight_state(
            state_dir=state_dir,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        )
    assert removed is True


def test_directory_retarget_cannot_change_certified_query_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_and_guard_preflight_state,
    )

    state_dir = tmp_path / "state"
    alternate = tmp_path / "alternate"
    held = tmp_path / "held-original"
    _initialize_state(state_dir)
    _initialize_state(alternate, epoch=_epoch(epoch_id="alternate-epoch"))
    real_open = os.open
    swapped = False

    def retarget_before_metric_open(
        path, flags, mode=0o777, *, dir_fd=None  # noqa: ANN001
    ):
        nonlocal swapped
        if Path(path).name == "metrics_v2.sqlite3" and not swapped:
            state_dir.rename(held)
            alternate.rename(state_dir)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", retarget_before_metric_open)
    observed_epochs: list[str] = []
    with pytest.raises(PreflightStateError, match="identity.*changed"):
        with inspect_and_guard_preflight_state(
            state_dir=state_dir,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        ) as (snapshot, _metric_store):
            observed_epochs.append(snapshot.epoch_id)

    assert swapped is True
    # The retained directory FD prevents opening the replacement. The live-path
    # identity check then rejects the swap before any SQLite query is exposed.
    assert observed_epochs == []


def test_uninitialized_unexpected_cohort_sidecar_is_fd_enumerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_preflight_state,
    )

    state_dir = tmp_path / "state"
    unexpected = state_dir / "unexpected-cohort"
    unexpected.mkdir(parents=True)
    (unexpected / "portfolio.db-wal").write_bytes(b"unsafe-sidecar-evidence")

    def forbid_path_discovery(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("sidecars must be enumerated through directory FDs")

    monkeypatch.setattr(Path, "glob", forbid_path_discovery)
    monkeypatch.setattr(os.path, "lexists", forbid_path_discovery)
    with pytest.raises(PreflightStateError, match="SQLite sidecar"):
        inspect_preflight_state(
            state_dir=state_dir,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        )


@pytest.mark.parametrize("suffix", ("-wal", "-journal"))
def test_parent_retarget_cannot_hide_unsafe_sidecar_during_initial_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_preflight_state,
    )

    state_dir = tmp_path / "state"
    alternate = tmp_path / "alternate"
    held = tmp_path / "held-original"
    _initialize_state(state_dir)
    _initialize_state(alternate)
    hidden = Path(f"{state_dir / 'metrics_v2.sqlite3'}{suffix}")
    alternate_hidden = Path(f"{alternate / 'metrics_v2.sqlite3'}{suffix}")
    alternate_hidden.unlink(missing_ok=True)
    hidden.write_bytes(b"unsafe-sidecar-evidence")
    real_lexists = os.path.lexists
    retargets = 0

    def hide_via_parent_retarget(path):  # noqa: ANN001
        nonlocal retargets
        if Path(path) != hidden:
            return real_lexists(path)
        state_dir.rename(held)
        alternate.rename(state_dir)
        try:
            retargets += 1
            return real_lexists(path)
        finally:
            state_dir.rename(alternate)
            held.rename(state_dir)

    monkeypatch.setattr(os.path, "lexists", hide_via_parent_retarget)
    with pytest.raises(PreflightStateError, match="SQLite sidecar"):
        inspect_preflight_state(
            state_dir=state_dir,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        )
    # The implementation never consults the retargetable pathname hook.
    assert retargets == 0


@pytest.mark.parametrize("suffix", ("-wal", "-journal"))
def test_parent_retarget_cannot_hide_unsafe_sidecar_at_final_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_and_guard_preflight_state,
    )

    state_dir = tmp_path / "state"
    alternate = tmp_path / "alternate"
    held = tmp_path / "held-original"
    _initialize_state(state_dir)
    _initialize_state(alternate)
    hidden = Path(f"{state_dir / 'metrics_v2.sqlite3'}{suffix}")
    alternate_hidden = Path(f"{alternate / 'metrics_v2.sqlite3'}{suffix}")
    alternate_hidden.unlink(missing_ok=True)
    if suffix == "-wal":
        hidden.unlink(missing_ok=True)
    real_lexists = os.path.lexists
    retargets = 0

    def hide_via_parent_retarget(path):  # noqa: ANN001
        nonlocal retargets
        if Path(path) != hidden:
            return real_lexists(path)
        state_dir.rename(held)
        alternate.rename(state_dir)
        try:
            retargets += 1
            return real_lexists(path)
        finally:
            state_dir.rename(alternate)
            held.rename(state_dir)

    with pytest.raises(PreflightStateError, match="changed during preflight"):
        with inspect_and_guard_preflight_state(
            state_dir=state_dir,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        ):
            hidden.write_bytes(b"unsafe-sidecar-evidence")
            monkeypatch.setattr(os.path, "lexists", hide_via_parent_retarget)
    # Final sidecar verification is also entirely relative to retained FDs.
    assert retargets == 0
