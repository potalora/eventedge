"""Bounded transactional projection tests for the authoritative signal journal."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from tradingagents.strategies.execution import SignalRecord, stable_id
from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal
from tradingagents.strategies.state.portfolio_ledger import (
    LedgerConflictError,
    PortfolioLedger,
)


UTC = timezone.utc
SESSION = date(2026, 7, 31)
CUTOFF = datetime(2026, 7, 31, 20, tzinfo=UTC)


def _record(ordinal: int) -> SignalRecord:
    signal_id = stable_id("outbox_signal", ordinal)
    return SignalRecord(
        signal_id,
        "epoch",
        "policy",
        f"event-{ordinal}",
        "earnings_call",
        f"T{ordinal}",
        "long",
        CUTOFF,
        CUTOFF,
        SESSION,
        Decimal("100"),
        CUTOFF,
        stable_id("evidence", ordinal),
    )


def _payload(record: SignalRecord, *, score: float = 1.0) -> dict[str, object]:
    return asdict(
        JournalEntry(
            timestamp=SESSION.isoformat(),
            strategy=record.strategy,
            ticker=record.ticker,
            direction=record.direction,
            score=score,
            signal_id=record.signal_id,
            regime="normal",
            status="timely",
        )
    )


def _ledger(tmp_path) -> PortfolioLedger:
    return PortfolioLedger(tmp_path / "ledger.db", "cohort", Decimal("1000"))


def test_large_legacy_history_append_does_not_scan_prior_lines(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        journal_path = state_dir / "signal_journal.jsonl"
        legacy_line = b'{"signal_id":"legacy","strategy":"old"}\n'
        journal_path.write_bytes(legacy_line * 100_000)
        record = _record(1)
        ledger.record_signal_with_journal(record, _payload(record), CUTOFF)
        journal = SignalJournal(str(state_dir), ledger=ledger)

        with patch.object(
            type(journal_path),
            "read_text",
            side_effect=AssertionError("hot append scanned history"),
        ):
            assert journal.drain_outbox() == 1

        row = ledger.connection.execute(
            "SELECT state, journal_offset FROM signal_journal_outbox"
        ).fetchone()
        assert row["state"] == "mirrored"
        assert row["journal_offset"] == len(legacy_line) * 100_000
    finally:
        ledger.close()


def test_crash_after_fsync_before_db_mark_recovers_tail_without_duplicate(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        record = _record(1)
        payload = _payload(record)
        ledger.record_signal_with_journal(record, payload, CUTOFF)

        def crash():
            raise RuntimeError("crash after append")

        journal = SignalJournal(
            str(tmp_path / "state"), ledger=ledger, after_append=crash
        )
        with pytest.raises(RuntimeError, match="crash after append"):
            journal.drain_outbox()
        assert (
            ledger.connection.execute(
                "SELECT state FROM signal_journal_outbox"
            ).fetchone()["state"]
            == "pending"
        )

        recovered = SignalJournal(str(tmp_path / "state"), ledger=ledger)
        assert recovered.drain_outbox() == 1
        assert len(recovered._path.read_text().splitlines()) == 1
        assert (
            ledger.connection.execute(
                "SELECT state FROM signal_journal_outbox"
            ).fetchone()["state"]
            == "mirrored"
        )
    finally:
        ledger.close()


def test_signal_and_exact_journal_payload_conflict_atomically(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        record = _record(1)
        ledger.record_signal_with_journal(record, _payload(record), CUTOFF)
        with pytest.raises(LedgerConflictError, match="journal payload"):
            ledger.record_signal_with_journal(
                record, _payload(record, score=2.0), CUTOFF
            )
        assert (
            ledger.connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
        )
        assert (
            ledger.connection.execute(
                "SELECT COUNT(*) FROM signal_journal_outbox"
            ).fetchone()[0]
            == 1
        )
    finally:
        ledger.close()


def test_projection_batch_is_hard_bounded_to_256_rows(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        for ordinal in range(300):
            record = _record(ordinal)
            ledger.record_signal_with_journal(record, _payload(record), CUTOFF)
        journal = SignalJournal(str(tmp_path / "state"), ledger=ledger)
        assert journal.drain_outbox() == 256
        counts = dict(
            ledger.connection.execute(
                "SELECT state, COUNT(*) AS count FROM signal_journal_outbox GROUP BY state"
            ).fetchall()
        )
        assert counts == {"mirrored": 256, "pending": 44}
    finally:
        ledger.close()
