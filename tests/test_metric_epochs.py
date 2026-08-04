from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from tradingagents.strategies.metrics import epochs as epochs_module
from tradingagents.strategies.metrics.epochs import (
    EpochContext,
    EpochManager,
    _semantic_hash,
)
from tradingagents.strategies.metrics.models import (
    CriticalGapMarker,
    METRIC_SCHEMA_VERSION,
    MetricEpoch,
    OutcomeRecord,
    StrategyHealthRecord,
)
from tradingagents.strategies.metrics.store import MetricStore


def _context(**changes: str) -> EpochContext:
    values = {
        "generation_id": "gen_004",
        "generation_commit": "abc123",
        "behavior_hash": "behavior-a",
        "config_hash": "config-a",
        "execution_clock_version": "next_open_v1",
        "pricing_version": "raw_ohlc_v1",
        "cost_model_version": "paper_cost_v1",
    }
    values.update(changes)
    return EpochContext(**values)


def _epoch(
    epoch_id: str = "epoch-1",
    *,
    start_session: date = date(2026, 8, 3),
) -> MetricEpoch:
    return MetricEpoch(
        epoch_id=epoch_id,
        generation_id="gen_004",
        generation_commit="abc123",
        behavior_hash="behavior-a",
        config_hash="config-a",
        metric_schema_version=METRIC_SCHEMA_VERSION,
        execution_clock_version="next_open_v1",
        pricing_version="raw_ohlc_v1",
        cost_model_version="paper_cost_v1",
        start_session=start_session,
        end_session=None,
        status="open",
        boundary_reason="initial",
    )


def _outcome(outcome_id: str = "outcome-1") -> OutcomeRecord:
    return OutcomeRecord(
        outcome_id=outcome_id,
        signal_id="signal-1",
        event_key="event-1",
        epoch_id="epoch-1",
        strategy="litigation",
        policy_id="30d",
        ticker="AAPL",
        direction="long",
        holding_sessions=5,
        entry_session=date(2026, 8, 3),
        exit_session=date(2026, 8, 10),
        entry_price=Decimal("100.25"),
        exit_price=Decimal("105.50"),
        raw_return=Decimal("0.05236907730673316708"),
        signed_return=Decimal("0.05236907730673316708"),
        status="valid",
        invalid_reason="",
    )


def _health(health_id: str = "health-1") -> StrategyHealthRecord:
    return StrategyHealthRecord(
        health_id=health_id,
        epoch_id="epoch-1",
        session=date(2026, 8, 3),
        policy_id="30d",
        strategy="litigation",
        status="legitimate_no_event",
        signal_count=0,
        evidence={"provider": "courtlistener", "result_count": 0},
    )


def _critical_gap(status: str = "pending") -> CriticalGapMarker:
    return CriticalGapMarker(
        marker_id="gap-epoch-1-2026-08-10",
        epoch_id="epoch-1",
        gap_session=date(2026, 8, 10),
        reason="critical_market_data_gap",
        cohort_invalid_reasons={
            "cohort-a": {"AAPL": "missing_exit_bar"},
            "cohort-b": {"MSFT": "critical_market_data_gap"},
        },
        status=status,
        affected_cohorts={
            "cohort-a": "ledger_recovery_binding_a",
            "cohort-b": "ledger_recovery_binding_b",
        },
        detail_status="ready",
    )


def _minimal_critical_gap() -> CriticalGapMarker:
    return replace(
        _critical_gap(),
        cohort_invalid_reasons={},
        detail_status="minimal",
        corporate_action_rejections={},
    )


def test_identical_context_reuses_open_epoch_across_later_sessions(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics_v2.sqlite3")
    manager = EpochManager(store)

    first = manager.ensure_epoch(_context(), date(2026, 8, 3))
    later = manager.ensure_epoch(_context(), date(2026, 8, 7))

    assert later == first
    assert store.load_epoch(first.epoch_id).status == "open"


def test_open_epoch_cannot_be_reused_for_an_earlier_session(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics_v2.sqlite3")
    manager = EpochManager(store)
    epoch = manager.ensure_epoch(_context(), date(2026, 8, 4))

    with pytest.raises(ValueError, match="precedes current epoch start"):
        manager.ensure_epoch(_context(), date(2026, 8, 3))

    assert store.load_epoch(epoch.epoch_id) == epoch


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("generation_id", "gen_005"),
        ("generation_commit", "def456"),
        ("behavior_hash", "behavior-b"),
        ("config_hash", "config-b"),
        ("execution_clock_version", "next_open_v2"),
        ("pricing_version", "adjusted_ohlc_v1"),
        ("cost_model_version", "paper_cost_v2"),
    ],
)
def test_every_semantic_context_field_participates_in_hash(
    field: str, changed: str
) -> None:
    baseline = _context()
    assert _semantic_hash(replace(baseline, **{field: changed})) != _semantic_hash(
        baseline
    )


def test_metric_schema_version_participates_in_hash(monkeypatch) -> None:
    before = _semantic_hash(_context())
    monkeypatch.setattr(
        epochs_module, "METRIC_SCHEMA_VERSION", METRIC_SCHEMA_VERSION + 1
    )
    assert _semantic_hash(_context()) != before


def test_hash_change_closes_old_epoch_on_previous_session_and_opens_new(
    tmp_path,
) -> None:
    store = MetricStore(tmp_path / "metrics_v2.sqlite3")
    manager = EpochManager(store)
    first = manager.ensure_epoch(_context(), date(2026, 8, 3))

    second = manager.ensure_epoch(_context(config_hash="config-b"), date(2026, 8, 5))

    assert first.epoch_id != second.epoch_id
    assert (
        second.epoch_id
        == manager.ensure_epoch(
            _context(config_hash="config-b"), date(2026, 8, 6)
        ).epoch_id
    )
    assert store.load_epoch(first.epoch_id) == replace(
        first,
        end_session=date(2026, 8, 4),
        status="closed",
        boundary_reason="semantic_hash_changed",
    )


def test_reuse_requires_full_stored_context_not_only_matching_id_suffix(
    tmp_path,
) -> None:
    context = _context()
    digest = _semantic_hash(context)
    store = MetricStore(tmp_path / "metrics_v2.sqlite3")
    mismatched = replace(
        _epoch(f"gen_004-2026-08-03-{digest[:16]}"),
        config_hash="tampered-config",
    )
    store.save_epoch(mismatched)

    current = EpochManager(store).ensure_epoch(context, date(2026, 8, 4))

    assert current.epoch_id != mismatched.epoch_id
    assert store.load_epoch(mismatched.epoch_id).status == "closed"


def test_critical_gap_invalidation_is_durable_and_idempotent(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics_v2.sqlite3")
    manager = EpochManager(store)
    epoch = manager.ensure_epoch(_context(), date(2026, 8, 3))

    invalid = manager.invalidate_current(date(2026, 8, 4), "missing_mark")
    repeated = store.invalidate_epoch(epoch.epoch_id, date(2026, 8, 4), "missing_mark")

    assert repeated == invalid
    assert MetricStore(store.path).load_epoch(epoch.epoch_id) == invalid
    with pytest.raises(ValueError, match="conflicting epoch closure"):
        store.close_epoch(epoch.epoch_id, date(2026, 8, 4), "other", False)
    with pytest.raises(ValueError, match="immutable epoch_id"):
        store.save_epoch(epoch)


@pytest.mark.parametrize("session", [date(2026, 8, 2), date(2026, 9, 7)])
def test_non_xnys_epoch_start_rejects(tmp_path, session: date) -> None:
    manager = EpochManager(MetricStore(tmp_path / "metrics_v2.sqlite3"))
    with pytest.raises(ValueError, match="not an XNYS session"):
        manager.ensure_epoch(_context(), session)


def test_non_xnys_invalidation_boundary_rejects_without_mutation(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics_v2.sqlite3")
    manager = EpochManager(store)
    epoch = manager.ensure_epoch(_context(), date(2026, 8, 3))

    with pytest.raises(ValueError, match="not an XNYS session"):
        manager.invalidate_current(date(2026, 8, 8), "missing_mark")

    assert store.load_epoch(epoch.epoch_id) == epoch


def test_store_reopen_and_current_selection_are_deterministic(tmp_path) -> None:
    path = tmp_path / "metrics_v2.sqlite3"
    store = MetricStore(path)
    store.save_epoch(_epoch("epoch-z", start_session=date(2026, 8, 3)))
    store.save_epoch(_epoch("epoch-a", start_session=date(2026, 8, 4)))

    assert MetricStore(path).current_epoch().epoch_id == "epoch-a"
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert tables == {
            "candidate_bar_recoveries",
            "candidate_signal_identity_bindings",
            "critical_gap_markers",
            "metric_epochs",
            "outcomes",
            "strategy_health",
        }
    assert journal_mode == "wal"


def test_pending_critical_gap_round_trips_and_completes_idempotently(tmp_path) -> None:
    path = tmp_path / "metrics_v2.sqlite3"
    store = MetricStore(path)
    minimal = _minimal_critical_gap()
    marker = _critical_gap()

    assert store.begin_critical_gap(minimal) == minimal
    assert store.begin_critical_gap(minimal) == minimal
    assert MetricStore(path).pending_critical_gap() == minimal
    assert store.attach_critical_gap_details(marker) == marker
    assert store.attach_critical_gap_details(marker) == marker
    assert MetricStore(path).pending_critical_gap() == marker

    completed = store.complete_critical_gap(marker.marker_id)
    assert completed == replace(marker, status="completed")
    assert store.complete_critical_gap(marker.marker_id) == completed
    assert MetricStore(path).pending_critical_gap() is None
    assert MetricStore(path).load_critical_gap(marker.marker_id) == completed


def test_critical_gap_schema_migrates_existing_store_and_indexes_pending(
    tmp_path,
) -> None:
    path = tmp_path / "metrics_v2.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE metric_epochs (epoch_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
            CREATE TABLE outcomes (
              outcome_id TEXT PRIMARY KEY, epoch_id TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            CREATE TABLE strategy_health (
              health_id TEXT PRIMARY KEY, epoch_id TEXT NOT NULL,
              session TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            """
        )

    store = MetricStore(path)
    store.begin_critical_gap(_minimal_critical_gap())
    store.attach_critical_gap_details(_critical_gap())

    with sqlite3.connect(path) as connection:
        indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list('critical_gap_markers')")
        }
        payload = connection.execute(
            "SELECT payload_json FROM critical_gap_markers"
        ).fetchone()[0]
    assert "idx_critical_gap_pending" in indexes
    assert "prices" not in payload
    assert "positions" not in payload
    assert "state_dir" not in payload


@pytest.mark.parametrize(
    "changes",
    (
        {"reason": "raw provider error details"},
        {"cohort_invalid_reasons": {"cohort-a": {"AAPL": "secret-token"}}},
        {"cohort_invalid_reasons": {"": {"AAPL": "missing_exit_bar"}}},
        {"cohort_invalid_reasons": {"cohort-a": {"": "missing_exit_bar"}}},
    ),
)
def test_critical_gap_marker_rejects_unbounded_or_unstable_payload(
    tmp_path, changes
) -> None:
    store = MetricStore(tmp_path / "metrics_v2.sqlite3")
    store.begin_critical_gap(_minimal_critical_gap())
    with pytest.raises(ValueError):
        store.attach_critical_gap_details(replace(_critical_gap(), **changes))


def test_minimal_critical_gap_requires_unique_opaque_ledger_bindings(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics_v2.sqlite3")
    with pytest.raises(ValueError, match="affected cohorts are required"):
        store.begin_critical_gap(replace(_minimal_critical_gap(), affected_cohorts={}))
    with pytest.raises(ValueError, match="bindings must be unique"):
        store.begin_critical_gap(
            replace(
                _minimal_critical_gap(),
                affected_cohorts={"cohort-a": "same", "cohort-b": "same"},
            )
        )


def test_legacy_pending_gap_without_bindings_is_loaded_but_cannot_complete(
    tmp_path,
) -> None:
    path = tmp_path / "metrics_v2.sqlite3"
    store = MetricStore(path)
    legacy = _critical_gap().__dict__.copy()
    legacy.pop("affected_cohorts")
    legacy.pop("detail_status")
    payload = store._json(_critical_gap())
    parsed = json.loads(payload)
    parsed.pop("affected_cohorts")
    parsed.pop("detail_status")
    legacy_payload = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO critical_gap_markers
              (marker_id, status, gap_session, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                legacy["marker_id"],
                legacy["status"],
                legacy["gap_session"].isoformat(),
                legacy_payload,
            ),
        )

    pending = store.pending_critical_gap()
    assert pending is not None
    assert pending.detail_status == "legacy_unbound"
    assert pending.affected_cohorts == {}
    with pytest.raises(ValueError, match="recovery detail is not ready"):
        store.complete_critical_gap(pending.marker_id)


def test_immutable_epoch_outcome_and_health_writes_are_idempotent(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics_v2.sqlite3")
    epoch = _epoch()
    outcome = _outcome()
    health = _health()

    store.save_epoch(epoch)
    store.save_epoch(epoch)
    store.upsert_outcome(outcome)
    store.upsert_outcome(outcome)
    store.save_strategy_health(health)
    store.save_strategy_health(health)

    assert store.load_epoch(epoch.epoch_id) == epoch
    assert store.load_outcome(outcome.outcome_id) == outcome
    assert store.load_strategy_health(health.health_id) == health
    assert store.read_outcomes("epoch-1") == (outcome,)
    assert store.read_strategy_health("epoch-1") == (health,)

    with pytest.raises(ValueError, match="immutable epoch_id"):
        store.save_epoch(replace(epoch, config_hash="changed"))
    with pytest.raises(ValueError, match="immutable outcome_id"):
        store.upsert_outcome(replace(outcome, ticker="MSFT"))
    with pytest.raises(ValueError, match="immutable health_id"):
        store.save_strategy_health(replace(health, evidence={"provider": "other"}))


def test_round_trip_optional_decimals_evidence_status_and_ordering(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics_v2.sqlite3")
    later = replace(
        _outcome("outcome-z"),
        entry_price=None,
        exit_price=None,
        raw_return=None,
        signed_return=None,
        status="invalid",
        invalid_reason="missing_exit_price",
    )
    earlier = replace(
        _outcome("outcome-a"),
        exit_session=date(2026, 8, 7),
    )
    health_z = replace(
        _health("health-z"), session=date(2026, 8, 4), status="data_failure"
    )
    health_a = replace(_health("health-a"), session=date(2026, 8, 3))
    for outcome in (later, earlier):
        store.upsert_outcome(outcome)
    for health in (health_z, health_a):
        store.save_strategy_health(health)

    assert store.read_outcomes("epoch-1") == (earlier, later)
    assert store.read_strategy_health("epoch-1") == (health_a, health_z)


def test_closure_allows_only_exact_open_transition(tmp_path) -> None:
    store = MetricStore(tmp_path / "metrics_v2.sqlite3")
    store.save_epoch(_epoch())

    closed = store.close_epoch("epoch-1", date(2026, 8, 4), "manual_boundary", False)
    assert (
        store.close_epoch("epoch-1", date(2026, 8, 4), "manual_boundary", False)
        == closed
    )
    with pytest.raises(ValueError, match="conflicting epoch closure"):
        store.invalidate_epoch("epoch-1", date(2026, 8, 4), "critical_gap")


def test_direct_store_close_rejects_weekend_boundary_without_mutation(
    tmp_path,
) -> None:
    store = MetricStore(tmp_path / "metrics_v2.sqlite3")
    epoch = _epoch()
    store.save_epoch(epoch)

    with pytest.raises(ValueError, match="not an XNYS session"):
        store.close_epoch(
            epoch.epoch_id,
            date(2026, 8, 8),
            "weekend_boundary",
        )

    assert store.load_epoch(epoch.epoch_id) == epoch


def test_direct_store_invalidate_rejects_exchange_holiday_without_mutation(
    tmp_path,
) -> None:
    store = MetricStore(tmp_path / "metrics_v2.sqlite3")
    epoch = _epoch()
    store.save_epoch(epoch)

    with pytest.raises(ValueError, match="not an XNYS session"):
        store.invalidate_epoch(
            epoch.epoch_id,
            date(2026, 9, 7),
            "holiday_boundary",
        )

    assert store.load_epoch(epoch.epoch_id) == epoch
