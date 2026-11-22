from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tradingagents.strategies.execution import Fill, OrderIntent, SignalRecord
from tradingagents.strategies.metrics.models import METRIC_SCHEMA_VERSION, MetricEpoch
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.orchestration.preflight_state import (
    PreflightStateError,
    inspect_and_guard_preflight_state,
    inspect_preflight_state,
)
from tradingagents.strategies.state.generation_db import GENERATION_DB_FILENAME, GenerationDB
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

SESSION = date(2026, 8, 10)
UTC = timezone.utc
COHORTS = ("cohort-a", "cohort-b")


def _epoch(
    *,
    epoch_id: str = "epoch-current",
    start_session: date = date(2026, 8, 3),
    status: str = "open",
    end_session: date | None = None,
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
        end_session=end_session,
        status=status,
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


def _initialize_legacy_state(
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


def test_zero_stores_is_uninitialized_with_only_configured_benchmarks(tmp_path: Path) -> None:
    snapshot = inspect_preflight_state(
        state_dir=tmp_path / "missing",
        cohort_ids=COHORTS,
        session=SESSION,
        benchmark_tickers=("SPY", "BIL"),
    )

    assert snapshot.state_status == "uninitialized"
    assert snapshot.epoch_id.startswith("preflight-prospective-")
    assert snapshot.governed_tickers == ("BIL", "SPY")
    assert dict(snapshot.cohort_ids_by_ticker) == {"BIL": COHORTS, "SPY": COHORTS}
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
    state_dir = tmp_path / "state"
    if missing == "metric":
        ledger = PortfolioLedger(
            state_dir / COHORTS[0] / "portfolio.db", COHORTS[0], Decimal("5000")
        )
        ledger.close()
    else:
        _initialize_legacy_state(state_dir, cohort_ids=(COHORTS[0],))

    with pytest.raises(PreflightStateError, match="partially initialized"):
        inspect_preflight_state(
            state_dir=state_dir,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY", "BIL"),
        )


def test_non_empty_wal_succeeds_cleanly_via_sqlite_backup(tmp_path: Path) -> None:
    """Regression: non-empty WAL sidecar must NOT cause a false-positive preflight failure."""
    state_dir = tmp_path / "state"
    db = GenerationDB.for_state_dir(state_dir)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO metric_epochs (epoch_id, generation_id, schema_version, status, start_session, end_session, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("epoch-wal", "gen_003", 2, "open", "2026-08-03", None, "{}"),
        )
        conn.execute(
            "INSERT INTO signals (signal_id, epoch_id, policy_id, event_key, strategy, ticker, direction, observed_at, reference_session, reference_close, decision_at, evidence_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sig-1", "epoch-wal", "pol", "evt-1", "earnings_call", "MSFT", "long", "2026-08-07T20:00:00+00:00", "2026-08-07", "400", "2026-08-07T21:00:00+00:00", "hash1"),
        )
        conn.execute(
            "INSERT INTO order_intents (intent_id, cohort_id, side, requested_qty, created_at, eligible_session, price_rule, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("int-1", "cohort-a", "buy", 5, "2026-08-07T21:00:00+00:00", "2026-08-10", "next_session_open", "pending"),
        )
        conn.execute(
            "INSERT INTO intent_signals (cohort_id, intent_id, signal_id, signal_order) VALUES (?, ?, ?, ?)",
            ("cohort-a", "int-1", "sig-1", 0),
        )

    # Keep a live writer connection open so -wal is actively non-empty!
    writer = sqlite3.connect(str(state_dir / GENERATION_DB_FILENAME))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO fills (fill_id, cohort_id, intent_id, side, session, effective_at, processed_at, reference_price, fill_price, quantity, slippage, commission, other_fees) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("fill-1", "cohort-b", "int-1", "buy", "2026-08-07", "2026-08-07T13:30:00+00:00", "2026-08-07T13:30:00+00:00", "150", "150", 3, "0", "0", "0"),
        )
        writer.execute(
            "INSERT INTO lots (lot_id, fill_id, cohort_id, ticker, direction, opened_session, entry_price, original_qty, open_qty, margin_reserved) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("lot-1", "fill-1", "cohort-b", "NVDA", "long", "2026-08-07", "150", 3, 3, "0"),
        )
        writer.commit()
        wal_path = state_dir / f"{GENERATION_DB_FILENAME}-wal"
        assert wal_path.exists() and wal_path.stat().st_size > 0

        snapshot = inspect_preflight_state(
            state_dir=state_dir,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY", "BIL"),
        )
    finally:
        writer.close()

    assert snapshot.state_status == "ready"
    assert snapshot.epoch_id == "epoch-wal"
    assert snapshot.governed_tickers == ("BIL", "MSFT", "NVDA", "SPY")
    assert dict(snapshot.cohort_ids_by_ticker) == {
        "BIL": COHORTS,
        "MSFT": ("cohort-a",),
        "NVDA": ("cohort-b",),
        "SPY": COHORTS,
    }


def test_legacy_state_discovers_lots_and_due_intents(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _initialize_legacy_state(state_dir)

    ledger_a = PortfolioLedger(state_dir / "cohort-a" / "portfolio.db", "cohort-a", Decimal("5000"))
    try:
        sig = _signal("AAPL", signal_id="sig-aapl")
        ledger_a.record_signal(sig)
        intent = _intent("cohort-a", sig, intent_id="int-aapl", eligible_session=SESSION)
        ledger_a.stage_intent(intent)
    finally:
        ledger_a.close()

    with inspect_and_guard_preflight_state(
        state_dir=state_dir,
        cohort_ids=COHORTS,
        session=SESSION,
        benchmark_tickers=("SPY", "BIL"),
    ) as (snapshot, metric_store):
        assert snapshot.state_status == "ready"
        assert snapshot.epoch_id == "epoch-current"
        assert snapshot.governed_tickers == ("AAPL", "BIL", "SPY")
        assert dict(snapshot.cohort_ids_by_ticker)["AAPL"] == ("cohort-a",)
        assert metric_store is not None


def test_invalid_epoch_reports_state_already_invalid(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _initialize_legacy_state(state_dir, epoch=_epoch(epoch_id="epoch-bad"))
    store = MetricStore(state_dir / "metrics_v2.sqlite3")
    store.invalidate_epoch("epoch-bad", SESSION, "corrupt_mark")

    snapshot = inspect_preflight_state(
        state_dir=state_dir,
        cohort_ids=COHORTS,
        session=SESSION,
        benchmark_tickers=("SPY",),
    )
    assert snapshot.state_status == "state_already_invalid"
    assert snapshot.epoch_id == "epoch-bad"


def test_symlink_state_dir_fails_closed(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir)

    with pytest.raises(PreflightStateError, match="symlink"):
        inspect_preflight_state(
            state_dir=link_dir,
            cohort_ids=COHORTS,
            session=SESSION,
            benchmark_tickers=("SPY",),
        )
