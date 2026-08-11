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
    _finalize_fixture_state(state_dir)


def _finalize_fixture_state(state_dir: Path) -> None:
    """Checkpoint fixture writers, then remove only their clean sidecars."""
    for path in sorted(state_dir.glob("**/*.db")) + sorted(
        state_dir.glob("metrics_v2.sqlite3")
    ):
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            connection.close()
        wal = Path(f"{path}-wal")
        assert not wal.exists() or wal.stat().st_size == 0
        for suffix in ("-wal", "-shm", "-journal"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)


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
        _finalize_fixture_state(state_dir)
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
    closed: list[str] = []
    metric_open = MetricStore.open_existing.__func__
    ledger_open = PortfolioLedger.open_existing.__func__
    ledger_close = PortfolioLedger.close

    def forbid_metric_init(self, path):  # noqa: ANN001
        raise AssertionError(f"writable MetricStore opened: {path}")

    def forbid_ledger_init(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("writable PortfolioLedger opened")

    def open_metric(cls, path, *, immutable=False):  # noqa: ANN001
        assert immutable is True
        opened.append("metric")
        return metric_open(cls, path, immutable=immutable)

    def open_ledger(cls, path, *, immutable=False):  # noqa: ANN001
        assert immutable is True
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

    snapshot = inspect_preflight_state(
        state_dir=state_dir,
        cohort_ids=COHORTS,
        session=SESSION,
        benchmark_tickers=("SPY", "BIL"),
    )

    assert snapshot.state_status == "ready"
    assert opened == ["metric", *COHORTS]
    assert sorted(closed) == list(COHORTS)


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
    _finalize_fixture_state(state_dir)

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
    _finalize_fixture_state(state_dir)

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
    _finalize_fixture_state(state_dir)

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
    _finalize_fixture_state(overlapping)
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
    _finalize_fixture_state(unexpected)
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
    _finalize_fixture_state(mismatched)
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
    _finalize_fixture_state(mismatched_epoch)
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
    _finalize_fixture_state(invalid)
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
    _finalize_fixture_state(invalid)
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


def test_immutable_openers_and_stale_wal_rejection_never_create_sidecars(
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

    metric = MetricStore.open_existing(metric_path, immutable=True)
    ledger = PortfolioLedger.open_existing(ledger_path, immutable=True)
    try:
        assert metric.read_only is True
        assert ledger.connection.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        ledger.close()
    for path in (metric_path, ledger_path):
        assert not Path(f"{path}-wal").exists()
        assert not Path(f"{path}-shm").exists()
        assert not Path(f"{path}-journal").exists()

    wal_path = Path(f"{metric_path}-wal")
    wal_path.write_bytes(b"stale-wal-evidence")
    os.utime(wal_path, ns=(1_700_000_000_000_000_000,) * 2)
    before = wal_path.stat()
    shm_path = Path(f"{metric_path}-shm")
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
