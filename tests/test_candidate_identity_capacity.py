"""Preserve full four-horizon discovery bindings within explicit resource limits."""
from dataclasses import asdict, replace
from datetime import date
import json
import sqlite3

import pytest

from tradingagents.strategies.metrics import store as store_module
from tradingagents.strategies.metrics.models import CandidateSignalIdentityBinding
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.orchestration.daily_pipeline import (
    _candidate_identity_conflict_tickers,
    _candidate_signal_identity_scope,
)

SESSION = date(2026, 10, 9)


def binding(identities):
    return CandidateSignalIdentityBinding("binding-native-size", "epoch-native-size", SESSION, identities)


def identities(count):
    return tuple({"horizon": "30d", "ticker": "TEST", "event_key": f"event-{i:06}",
                  "strategy": "filing_analysis"} for i in range(count))


def test_native_size_four_horizon_journal_discovery_round_trips_without_truncation(tmp_path):
    # Native final retained 2,117 distinct nonblank journal-only filings/horizon.
    # Synthetic source locators reproduce that composition without private data.
    signals = [{"ticker": f"T{i % 1000:04}", "strategy": "filing_analysis", "journal_only": True,
                "metadata": {"accession_number": f"0000000000-26-{i:06}",
                             "non_actionable_reason": "missing_source_text"}} for i in range(2117)]
    scope = _candidate_signal_identity_scope({h: (signals, {}, []) for h in ("30d", "3m", "6m", "1y")}, SESSION)
    assert len(scope) == 8468
    record = binding(scope)
    store = MetricStore(tmp_path / "metrics.sqlite3")
    store.save_candidate_signal_identity_binding(record)
    store.save_candidate_signal_identity_binding(record)
    reopened = MetricStore.open_existing(store.path)
    loaded = reopened.read_candidate_signal_identity_binding(record.epoch_id, SESSION)
    assert loaded == record
    assert len(loaded.identities) == 8468
    assert _candidate_identity_conflict_tickers(scope, loaded.identities) == []
    assert _candidate_identity_conflict_tickers(scope[:-1], loaded.identities) == [scope[-1]["ticker"]]
    mutated = replace(record, identities=scope[:-1])
    with pytest.raises(ValueError, match="immutable binding_id.*unequal payload"):
        store.save_candidate_signal_identity_binding(mutated)
    changed = (*scope[:-1], {**scope[-1], "event_key": "event_key_" + "f" * 32})
    assert _candidate_identity_conflict_tickers(changed, loaded.identities) == [scope[-1]["ticker"]]
    with pytest.raises(ValueError, match="immutable binding_id.*unequal payload"):
        store.save_candidate_signal_identity_binding(replace(record, identities=changed))
    assert reopened.read_candidate_signal_identity_binding(record.epoch_id, SESSION) == record


def test_default_discovery_capacity_remains_finite_and_covers_six_sec_windows():
    # Six 10,000-hit SEC windows repeated across four default horizons, plus
    # small bounded strategies, must fit the aggregate discovery envelope.
    assert 4 * (6 * 10_000 + 64) <= store_module._MAX_CANDIDATE_SIGNAL_IDENTITIES == 262_144
    assert store_module._MAX_CANDIDATE_SIGNAL_BINDING_PAYLOAD_BYTES == 64 * 1024 * 1024


def test_count_limit_accepts_exact_bound_and_rejects_next_identity_before_persistence(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "_MAX_CANDIDATE_SIGNAL_IDENTITIES", 8)
    store = MetricStore(tmp_path / "metrics.sqlite3")
    accepted = binding(identities(8))
    store.save_candidate_signal_identity_binding(accepted)
    with pytest.raises(ValueError, match="identity binding exceeds bound"):
        store.save_candidate_signal_identity_binding(binding(identities(9)))
    assert store.read_candidate_signal_identity_binding(accepted.epoch_id, SESSION) == accepted


def test_payload_byte_limit_accepts_exact_bound_and_rejects_one_byte_over(tmp_path, monkeypatch):
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = binding(identities(2))
    encoded = json.dumps(asdict(record), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    monkeypatch.setattr(store_module, "_MAX_CANDIDATE_SIGNAL_BINDING_PAYLOAD_BYTES", len(encoded), raising=False)
    store.save_candidate_signal_identity_binding(record)
    monkeypatch.setattr(store_module, "_MAX_CANDIDATE_SIGNAL_BINDING_PAYLOAD_BYTES", len(encoded) - 1)
    with pytest.raises(ValueError, match="identity binding payload exceeds bound"):
        store.save_candidate_signal_identity_binding(record)
    with pytest.raises(ValueError, match="identity binding payload exceeds bound"):
        store.read_candidate_signal_identity_binding(record.epoch_id, SESSION)


@pytest.mark.parametrize("invalid", [
    identities(2)[::-1],
    (identities(1)[0], identities(1)[0]),
    ({**identities(1)[0], "ticker": "test"},),
    ({**identities(1)[0], "event_key": "x" * 257},),
])
def test_capacity_increase_preserves_canonical_and_field_validation(tmp_path, invalid):
    store = MetricStore(tmp_path / "metrics.sqlite3")
    with pytest.raises(ValueError, match="identity binding (is not canonical|is invalid)"):
        store.save_candidate_signal_identity_binding(binding(invalid))
    assert store.read_candidate_signal_identity_binding("epoch-native-size", SESSION) is None


def test_read_rejects_oversized_persisted_payload_before_json_parsing(tmp_path, monkeypatch):
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = binding(identities(1))
    store.save_candidate_signal_identity_binding(record)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE candidate_signal_identity_bindings SET payload_json = ?", ("invalid-json" * 20,))
    monkeypatch.setattr(store_module, "_MAX_CANDIDATE_SIGNAL_BINDING_PAYLOAD_BYTES", 32)
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match="identity binding payload exceeds bound"):
        MetricStore.open_existing(store.path).read_candidate_signal_identity_binding(record.epoch_id, SESSION)
    assert store.path.read_bytes() == before


def test_read_revalidates_persisted_identity_count(tmp_path, monkeypatch):
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = binding(identities(3))
    store.save_candidate_signal_identity_binding(record)
    monkeypatch.setattr(store_module, "_MAX_CANDIDATE_SIGNAL_IDENTITIES", 2)
    with pytest.raises(ValueError, match="identity binding exceeds bound"):
        MetricStore.open_existing(store.path).read_candidate_signal_identity_binding(record.epoch_id, SESSION)


@pytest.mark.parametrize("changes", [
    {"identities": list(identities(2)[::-1])},
    {"identities": [identities(1)[0], identities(1)[0]]},
    {"identities": [{**identities(1)[0], "ticker": "test"}]},
    {"identities": [list(identities(1)[0].items())]},
    {"identities": None},
    {"extra_field": "unrecognized"},
    {"session": "2026-10-10"},
])
def test_read_revalidates_persisted_shape_fields_and_canonical_order(tmp_path, changes):
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = binding(identities(2))
    store.save_candidate_signal_identity_binding(record)
    payload = {**asdict(record), "session": SESSION.isoformat(), **changes}
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE candidate_signal_identity_bindings SET payload_json = ?", (json.dumps(payload),))
    with pytest.raises(ValueError):
        MetricStore.open_existing(store.path).read_candidate_signal_identity_binding(record.epoch_id, SESSION)


@pytest.mark.parametrize("field,value", [
    ("epoch_id", "another-epoch"),
    ("session", "2026-10-08"),
    ("binding_id", "another-binding"),
])
def test_read_binds_payload_identity_to_lookup_and_stored_binding_id(tmp_path, field, value):
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = binding(identities(2))
    store.save_candidate_signal_identity_binding(record)
    payload = {**asdict(record), "session": SESSION.isoformat(), field: value}
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE candidate_signal_identity_bindings SET payload_json = ?", (json.dumps(payload),))
    with pytest.raises(ValueError, match="identity binding.*(scope|identifier).*invalid"):
        MetricStore.open_existing(store.path).read_candidate_signal_identity_binding(record.epoch_id, SESSION)


def test_streaming_binding_serializer_preserves_legacy_canonical_bytes(tmp_path):
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = binding(({**identities(1)[0], "event_key": "event-\u00e9-\U0001f680"},))
    expected = json.dumps(asdict(record), sort_keys=True, separators=(",", ":"), default=str)
    store.save_candidate_signal_identity_binding(record)
    with sqlite3.connect(store.path) as connection:
        payload, = connection.execute("SELECT payload_json FROM candidate_signal_identity_bindings").fetchone()
    assert payload == expected
    assert store.read_candidate_signal_identity_binding(record.epoch_id, SESSION) == record


def test_serializer_stops_consuming_chunks_when_unicode_field_exceeds_byte_budget(tmp_path, monkeypatch):
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = binding(({**identities(1)[0], "event_key": "\u00e9" * 256},))
    monkeypatch.setattr(store_module, "_MAX_CANDIDATE_SIGNAL_BINDING_PAYLOAD_BYTES", 256)
    original = json.JSONEncoder.iterencode
    fully_consumed = []

    def observed_chunks(self, *args, **kwargs):
        yield from original(self, *args, **kwargs)
        fully_consumed.append(True)

    monkeypatch.setattr(json.JSONEncoder, "iterencode", observed_chunks)
    with pytest.raises(ValueError, match="identity binding payload exceeds bound"):
        store.save_candidate_signal_identity_binding(record)
    assert not fully_consumed
    assert store.read_candidate_signal_identity_binding(record.epoch_id, SESSION) is None


def test_decode_helper_counts_unicode_bytes_before_parsing(tmp_path, monkeypatch):
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = binding(({**identities(1)[0], "event_key": "\u00e9" * 256},))
    payload = json.dumps(asdict(record), default=str, ensure_ascii=False)
    monkeypatch.setattr(store_module, "_MAX_CANDIDATE_SIGNAL_BINDING_PAYLOAD_BYTES", len(payload))

    def forbidden_parse(*args, **kwargs):
        raise AssertionError("oversized payload reached JSON parsing")

    monkeypatch.setattr(json, "loads", forbidden_parse)
    with pytest.raises(ValueError, match="identity binding payload exceeds bound"):
        store._candidate_signal_identity_binding(payload)


def test_read_rejects_null_database_binding_id(tmp_path):
    store = MetricStore(tmp_path / "metrics.sqlite3")
    record = binding(identities(1))
    store.save_candidate_signal_identity_binding(record)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE candidate_signal_identity_bindings SET binding_id = NULL")
    with pytest.raises(ValueError, match="identity binding identifier is invalid"):
        MetricStore.open_existing(store.path).read_candidate_signal_identity_binding(record.epoch_id, SESSION)
