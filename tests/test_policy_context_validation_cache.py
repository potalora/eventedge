"""Repeated binding reads must avoid validation work without trusting stale rows."""
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import sqlite3

import pytest

from tradingagents.strategies.state.ledger import policy_audit as audit_module
from tradingagents.strategies.state.portfolio_ledger import (
    LedgerConflictError,
    PortfolioLedger,
)
from tradingagents.strategies.execution import stable_id


SESSION = date(2026, 10, 9)
BOUND_AT = datetime(2026, 10, 9, 22, tzinfo=timezone.utc)


@pytest.fixture
def ledger(tmp_path):
    result = PortfolioLedger(tmp_path / "portfolio.db", "book", Decimal("5000"))
    yield result
    result.close()


def bind(ledger, *, session=SESSION, context=None):
    return ledger.bind_policy_session_context(
        session,
        epoch_id="epoch",
        policy_version="v1",
        policy_config={"nested": {"items": [1]}},
        context=context or {"sectors": {"AAPL": "Technology"}},
        bound_at=BOUND_AT,
    )


def count_canonicalization(monkeypatch):
    calls = []
    original = audit_module._canonical_json

    def counted(value):
        calls.append(None)
        return original(value)

    monkeypatch.setattr(audit_module, "_canonical_json", counted)
    return calls


def test_repeated_reads_skip_full_context_revalidation(ledger, monkeypatch):
    bind(ledger)
    calls = count_canonicalization(monkeypatch)
    for _ in range(4):
        assert ledger.read_policy_session_context(SESSION)["context"] == {
            "sectors": {"AAPL": "Technology"}
        }
    assert len(calls) == 0


def test_hit_parses_all_documents_fresh_and_isolates_mutable_returns(ledger, monkeypatch):
    bind(ledger)
    loads = []
    original = json.loads

    def counted(value, *args, **kwargs):
        loads.append(value)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(audit_module.json, "loads", counted)
    first = ledger.read_policy_session_context(SESSION)
    first["context"]["sectors"]["AAPL"] = "Changed"
    first["policy_config"]["nested"]["items"].append(2)
    second = ledger.read_policy_session_context(SESSION)
    assert second["context"] == {"sectors": {"AAPL": "Technology"}}
    assert second["policy_config"] == {"nested": {"items": [1]}}
    assert len(loads) == 6


@pytest.mark.parametrize("other_connection", [False, True])
@pytest.mark.parametrize(
    "column,value",
    [
        ("cohort_id", "other"),
        ("session", "2026-10-08"),
        ("record_id", "2026-10-09:execution"),
        ("epoch_id", "other"),
        ("payload_json", '{"bad":1}'),
        ("payload_sha256", "bad"),
        ("created_at", "invalid-time"),
    ],
)
def test_any_changed_row_field_runs_validator_and_rejects_invalid_evidence(
    ledger, monkeypatch, column, value, other_connection
):
    bind(ledger)
    calls = count_canonicalization(monkeypatch)
    writer = (
        sqlite3.connect(ledger.path, isolation_level=None)
        if other_connection
        else ledger.connection
    )
    try:
        writer.execute(f"UPDATE policy_audit_records SET {column} = ?", (value,))
        row = ledger.connection.execute(
            "SELECT * FROM policy_audit_records"
        ).fetchone()
        for _ in range(2):
            with pytest.raises((LedgerConflictError, ValueError)):
                ledger._policy_session_context_from_row(row)
        if column not in {"payload_json", "payload_sha256"}:
            assert len(calls) >= 2
        if column in {"cohort_id", "record_id"}:
            assert ledger.read_policy_session_context(SESSION) is None
        else:
            with pytest.raises((LedgerConflictError, ValueError)):
                ledger.read_policy_session_context(SESSION)
    finally:
        if other_connection:
            writer.close()


def test_valid_timestamp_change_is_revalidated_then_reused(ledger, monkeypatch):
    bind(ledger)
    calls = count_canonicalization(monkeypatch)
    ledger.connection.execute(
        "UPDATE policy_audit_records SET created_at = ?",
        ("2026-10-09T23:00:00+00:00",),
    )
    assert ledger.read_policy_session_context(SESSION)["bound_at"].hour == 23
    first_count = len(calls)
    assert first_count > 0
    ledger.read_policy_session_context(SESSION)
    assert len(calls) == first_count


def test_rollback_delete_and_reinsert_never_return_stale_binding(ledger):
    original = bind(ledger)
    ledger.connection.execute("BEGIN IMMEDIATE")
    ledger.connection.execute(
        "UPDATE policy_audit_records SET payload_sha256='bad'"
    )
    with pytest.raises(LedgerConflictError):
        ledger.read_policy_session_context(SESSION)
    ledger.connection.execute("ROLLBACK")
    assert ledger.read_policy_session_context(SESSION) == original
    ledger.connection.execute("DELETE FROM policy_audit_records")
    assert ledger.read_policy_session_context(SESSION) is None
    replacement = bind(ledger, context={"sectors": {"AAPL": "Replacement"}})
    assert replacement["context"] == {"sectors": {"AAPL": "Replacement"}}
    assert ledger.read_policy_session_context(SESSION) == replacement


def test_one_retained_row_replaces_previous_session_validation(ledger, monkeypatch):
    bind(ledger)
    other = date(2026, 10, 8)
    bind(ledger, session=other)
    calls = count_canonicalization(monkeypatch)
    ledger.read_policy_session_context(SESSION)
    first_count = len(calls)
    assert first_count > 0
    ledger.read_policy_session_context(SESSION)
    assert len(calls) == first_count
    ledger.read_policy_session_context(other)
    assert len(calls) == first_count * 2


def test_oversize_valid_context_bypasses_retention(ledger, monkeypatch):
    bind(ledger, context={"text": "x" * 150_000})
    calls = count_canonicalization(monkeypatch)
    for _ in range(2):
        assert (
            len(ledger.read_policy_session_context(SESSION)["context"]["text"])
            == 150_000
        )
    assert len(calls) >= 8


def test_wide_unicode_context_is_bounded_by_retained_bytes(ledger, monkeypatch):
    bind(ledger, context={"text": "🌍" * 40_000})
    calls = count_canonicalization(monkeypatch)
    for _ in range(2):
        assert (
            len(ledger.read_policy_session_context(SESSION)["context"]["text"])
            == 40_000
        )
    assert len(calls) >= 8


def test_changed_malformed_json_is_never_memoized(ledger):
    bind(ledger)
    malformed = "{"
    ledger.connection.execute(
        "UPDATE policy_audit_records SET payload_json=?, payload_sha256=?",
        (malformed, hashlib.sha256(malformed.encode("utf-8")).hexdigest()),
    )
    for _ in range(2):
        with pytest.raises(LedgerConflictError):
            ledger.read_policy_session_context(SESSION)


def test_valid_uncommitted_row_then_rollback_revalidates_restored_row(
    ledger, monkeypatch
):
    original = bind(ledger)
    ledger.connection.execute("BEGIN IMMEDIATE")
    ledger.connection.execute(
        "UPDATE policy_audit_records SET created_at=?",
        ("2026-10-09T23:00:00+00:00",),
    )
    assert ledger.read_policy_session_context(SESSION)["bound_at"].hour == 23
    ledger.connection.execute("ROLLBACK")
    calls = count_canonicalization(monkeypatch)
    assert ledger.read_policy_session_context(SESSION) == original
    assert len(calls) > 0


def test_date_conversion_failure_cannot_publish_validation_fact(ledger, monkeypatch):
    bind(ledger)
    row = ledger.connection.execute(
        "SELECT * FROM policy_audit_records"
    ).fetchone()
    envelope = json.loads(row["payload_json"])
    payload = json.loads(envelope["payload_json"])
    payload["session"] = "invalid-date"
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    envelope["session"] = "invalid-date"
    envelope["payload_json"] = payload_json
    envelope["payload_digest"] = stable_id("policy_binding", payload_json)
    envelope_json = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    ledger.connection.execute(
        """UPDATE policy_audit_records
           SET session=?, record_id=?, payload_json=?, payload_sha256=?""",
        (
            "invalid-date",
            "invalid-date:staging",
            envelope_json,
            hashlib.sha256(envelope_json.encode("utf-8")).hexdigest(),
        ),
    )
    tampered_row = ledger.connection.execute(
        "SELECT * FROM policy_audit_records"
    ).fetchone()
    calls = count_canonicalization(monkeypatch)
    for _ in range(2):
        with pytest.raises(ValueError, match="Invalid isoformat"):
            ledger._policy_session_context_from_row(tampered_row)
    assert len(calls) >= 8


def test_read_only_audit_reuses_validation_without_filesystem_writes(
    tmp_path, monkeypatch
):
    source = PortfolioLedger(tmp_path / "portfolio.db", "book", Decimal("5000"))
    bind(source)
    source.close()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    audit = PortfolioLedger.open_existing(tmp_path / "portfolio.db", immutable=True)
    try:
        audit.read_policy_session_context(SESSION)
        calls = count_canonicalization(monkeypatch)
        assert audit.read_policy_session_context(SESSION)["epoch_id"] == "epoch"
        assert len(calls) == 0
    finally:
        audit.close()
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_report_pure_decoder_with_no_ledger_always_validates(ledger, monkeypatch):
    bind(ledger)
    row = ledger.connection.execute("SELECT * FROM policy_audit_records").fetchone()
    calls = count_canonicalization(monkeypatch)
    for _ in range(2):
        assert PortfolioLedger._policy_session_context_from_row(None, row)[
            "context"
        ] == {"sectors": {"AAPL": "Technology"}}
    assert len(calls) >= 8


def test_in_memory_ledgers_keep_validation_and_returned_context_isolated(monkeypatch):
    first = PortfolioLedger(":memory:", "book", Decimal("5000"))
    second = PortfolioLedger(":memory:", "book", Decimal("5000"))
    try:
        bind(first)
        bind(second, context={"sectors": {"AAPL": "Other"}})
        calls = count_canonicalization(monkeypatch)
        assert first.read_policy_session_context(SESSION)["context"] == {
            "sectors": {"AAPL": "Technology"}
        }
        assert second.read_policy_session_context(SESSION)["context"] == {
            "sectors": {"AAPL": "Other"}
        }
        assert len(calls) == 0
    finally:
        first.close()
        second.close()
