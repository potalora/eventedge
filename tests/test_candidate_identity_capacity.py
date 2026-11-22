"""Preserve full four-horizon discovery bindings in normalized SQLite rows (no 64 MB blobs)."""
from dataclasses import replace
from datetime import date
import sqlite3

import pytest

from tradingagents.strategies.execution import recovery_evidence as evidence_module
from tradingagents.strategies.metrics.models import CandidateSignalIdentityBinding
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.orchestration.daily_pipeline import (
    _candidate_identity_conflict_tickers,
    _candidate_signal_identity_scope,
)

SESSION = date(2026, 10, 9)


def binding(identities):
    return CandidateSignalIdentityBinding(
        "binding-native-size", "epoch-native-size", SESSION, identities
    )


def identities(count):
    return tuple(
        {
            "horizon": "30d",
            "ticker": "TEST",
            "event_key": f"event-{i:06}",
            "strategy": "filing_analysis",
        }
        for i in range(count)
    )


def test_native_size_four_horizon_journal_discovery_round_trips_normalized(tmp_path):
    signals = [
        {
            "ticker": f"T{i % 1000:04}",
            "strategy": "filing_analysis",
            "journal_only": True,
            "metadata": {
                "accession_number": f"0000000000-26-{i:06}",
                "non_actionable_reason": "missing_source_text",
            },
        }
        for i in range(2117)
    ]
    scope = _candidate_signal_identity_scope(
        {h: (signals, {}, []) for h in ("30d", "3m", "6m", "1y")}, SESSION
    )
    assert len(scope) == 8468
    record = binding(scope)
    store = MetricStore(tmp_path / "metrics.sqlite3")
    store.save_candidate_signal_identity_binding(record)
    store.save_candidate_signal_identity_binding(record)

    # Confirm normalized storage: header + 8,468 rows, no monolithic payload_json blob column.
    with sqlite3.connect(store.path) as conn:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(candidate_signal_identity_bindings)")
        }
        assert "payload_json" not in cols
        row_count = conn.execute(
            "SELECT COUNT(*) FROM candidate_signal_identity_rows WHERE binding_id = ?",
            (record.binding_id,),
        ).fetchone()[0]
        assert row_count == 8468

    reopened = MetricStore.open_existing(store.path)
    loaded = reopened.read_candidate_signal_identity_binding(record.epoch_id, SESSION)
    assert loaded == record
    assert len(loaded.identities) == 8468
    assert _candidate_identity_conflict_tickers(scope, loaded.identities) == []
    assert _candidate_identity_conflict_tickers(scope[:-1], loaded.identities) == [
        scope[-1]["ticker"]
    ]

    mutated = replace(record, identities=scope[:-1])
    with pytest.raises(ValueError, match="immutable binding_id.*unequal payload"):
        store.save_candidate_signal_identity_binding(mutated)
    changed = (*scope[:-1], {**scope[-1], "event_key": "event_key_" + "f" * 32})
    assert _candidate_identity_conflict_tickers(changed, loaded.identities) == [
        scope[-1]["ticker"]
    ]
    with pytest.raises(ValueError, match="immutable binding_id.*unequal payload"):
        store.save_candidate_signal_identity_binding(replace(record, identities=changed))
    assert reopened.read_candidate_signal_identity_binding(record.epoch_id, SESSION) == record


def test_default_discovery_capacity_remains_finite():
    assert 4 * (6 * 10_000 + 64) <= evidence_module._MAX_CANDIDATE_SIGNAL_IDENTITIES == 262_144


def test_count_limit_accepts_exact_bound_and_rejects_next_identity_before_persistence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(evidence_module, "_MAX_CANDIDATE_SIGNAL_IDENTITIES", 8)
    store = MetricStore(tmp_path / "metrics.sqlite3")
    accepted = binding(identities(8))
    store.save_candidate_signal_identity_binding(accepted)
    with pytest.raises(ValueError, match="identity binding exceeds bound"):
        store.save_candidate_signal_identity_binding(binding(identities(9)))
    assert store.read_candidate_signal_identity_binding(accepted.epoch_id, SESSION) == accepted


@pytest.mark.parametrize(
    "invalid",
    [
        identities(2)[::-1],
        (identities(1)[0], identities(1)[0]),
        ({**identities(1)[0], "ticker": "test"},),
        ({**identities(1)[0], "event_key": "x" * 257},),
    ],
)
def test_capacity_preserves_canonical_and_field_validation(tmp_path, invalid):
    store = MetricStore(tmp_path / "metrics.sqlite3")
    with pytest.raises(ValueError, match="identity binding (is not canonical|is invalid)"):
        store.save_candidate_signal_identity_binding(binding(invalid))
    assert store.read_candidate_signal_identity_binding("epoch-native-size", SESSION) is None


def test_read_revalidates_persisted_identity_count(tmp_path, monkeypatch):
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = binding(identities(3))
    store.save_candidate_signal_identity_binding(record)
    monkeypatch.setattr(evidence_module, "_MAX_CANDIDATE_SIGNAL_IDENTITIES", 2)
    with pytest.raises(ValueError, match="identity binding exceeds bound"):
        MetricStore.open_existing(store.path).read_candidate_signal_identity_binding(
            record.epoch_id, SESSION
        )


def test_read_detects_tampered_normalized_row_and_does_not_write(tmp_path):
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = binding(identities(2))
    store.save_candidate_signal_identity_binding(record)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE candidate_signal_identity_rows SET ticker = 'bad_lowercase' WHERE event_key = 'event-000000'"
        )
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="digest|canonical|invalid"):
        MetricStore.open_existing(store.path).read_candidate_signal_identity_binding(
            record.epoch_id, SESSION
        )
    assert store.path.read_bytes() == before


def test_unicode_event_key_round_trips_cleanly(tmp_path):
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = binding(({**identities(1)[0], "event_key": "event-\u00e9-\U0001f680"},))
    store.save_candidate_signal_identity_binding(record)
    assert store.read_candidate_signal_identity_binding(record.epoch_id, SESSION) == record


def test_read_rejects_null_database_binding_id(tmp_path):
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = binding(identities(1))
    store.save_candidate_signal_identity_binding(record)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE candidate_signal_identity_bindings SET binding_id = NULL")
    with pytest.raises(ValueError, match="identity binding identifier is invalid"):
        MetricStore.open_existing(store.path).read_candidate_signal_identity_binding(
            record.epoch_id, SESSION
        )
