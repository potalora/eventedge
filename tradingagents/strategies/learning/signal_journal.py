"""Append-only signal journal for autoresearch paper trading."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from tradingagents.strategies.execution import SignalRecord

if TYPE_CHECKING:
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

logger = logging.getLogger(__name__)


@dataclass
class JournalEntry:
    """One signal observation."""

    timestamp: str
    strategy: str
    ticker: str
    direction: str
    score: float
    signal_id: str = ""
    llm_conviction: float = 0.0
    regime: str = ""
    traded: bool = False
    entry_price: float | None = None
    return_5d: float | None = None
    return_10d: float | None = None
    return_30d: float | None = None
    prompt_version: str = ""
    enrichment_available: bool = True
    metadata: dict = field(default_factory=dict)
    status: str = "legacy"


class SignalJournal:
    """Append-only JSONL signal log.

    File lives at ``{state_dir}/signal_journal.jsonl``.
    """

    def __init__(
        self,
        state_dir: str,
        ledger: "PortfolioLedger | None" = None,
        *,
        after_append: Callable[[], None] | None = None,
    ) -> None:
        self._path = Path(state_dir) / "signal_journal.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._ledger = ledger
        self._after_append = after_append or (lambda: None)

    def log_signal(self, entry: JournalEntry) -> None:
        """Append a single signal entry."""
        with open(self._path, "a") as f:
            f.write(json.dumps(asdict(entry), default=str) + "\n")

    def log_signals(self, entries: list[JournalEntry]) -> None:
        """Append entries, using authoritative signal IDs whenever present."""
        existing_keys: set[tuple[str, ...]] = set()
        if self._path.exists():
            for line in self._path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    existing_keys.add(self._journal_key(e))
                except json.JSONDecodeError:
                    continue

        new_entries = []
        for entry in entries:
            d = asdict(entry)
            key = self._journal_key(d)
            if key not in existing_keys:
                new_entries.append(d)
                existing_keys.add(key)

        if new_entries:
            with open(self._path, "a") as f:
                for d in new_entries:
                    f.write(json.dumps(d, default=str) + "\n")

    def mirror_signals(
        self,
        records: list[SignalRecord],
        context_by_id: dict[str, dict[str, Any]],
    ) -> None:
        """Mirror persisted ledger records without inventing identity or price."""
        if self._ledger is not None:
            self.drain_outbox()
            return
        entries = []
        for record in records:
            context = context_by_id.get(record.signal_id, {})
            entries.append(
                JournalEntry(
                    timestamp=record.reference_session.isoformat(),
                    strategy=record.strategy,
                    ticker=record.ticker,
                    direction=record.direction,
                    score=float(context.get("score", 0.0)),
                    signal_id=record.signal_id,
                    llm_conviction=float(context.get("llm_conviction", 0.0)),
                    regime=str(context.get("regime", "")),
                    traded=False,
                    entry_price=None,
                    metadata=dict(context.get("metadata", {})),
                    status=str(context.get("status", "timely")),
                )
            )
        self.log_signals(entries)

    def drain_outbox(self, limit: int = 256) -> int:
        """Project one bounded ledger outbox batch with crash-safe tail recovery."""
        if self._ledger is None:
            return 0
        pending = self._ledger.pending_signal_journal_outbox(limit)
        if not pending:
            return 0
        now = datetime.now(timezone.utc)
        self._path.touch(exist_ok=True)
        verified = self._ledger.journal_projection_offset()
        if verified is None:
            verified = self._ledger.initialize_journal_projection(
                self._path.stat().st_size, now
            )

        expected_lines = [
            str(row["payload_json"]).encode("utf-8") + b"\n" for row in pending
        ]
        max_tail = sum(len(line) for line in expected_lines)
        projections: list[tuple[str, str, int, int]] = []
        recovered = 0
        cursor = verified

        with open(self._path, "r+b") as journal:
            journal.seek(0, os.SEEK_END)
            size = journal.tell()
            if size < verified:
                raise RuntimeError("signal journal is shorter than verified projection")
            tail_size = size - verified
            if tail_size > max_tail:
                raise RuntimeError("signal journal has an unbounded unverified tail")
            journal.seek(verified)
            tail = journal.read(tail_size)
            consumed = 0
            for row, expected in zip(pending, expected_lines):
                remaining = tail[consumed:]
                if remaining.startswith(expected):
                    projections.append(
                        (
                            str(row["signal_id"]),
                            str(row["payload_hash"]),
                            cursor,
                            len(expected),
                        )
                    )
                    consumed += len(expected)
                    cursor += len(expected)
                    recovered += 1
                    continue
                if remaining and expected.startswith(remaining):
                    journal.truncate(cursor)
                    tail = tail[:consumed]
                    break
                if remaining:
                    raise RuntimeError(
                        "signal journal tail conflicts with ledger outbox"
                    )
                break
            if consumed != len(tail):
                raise RuntimeError("signal journal contains an unmatched tail")

            journal.seek(cursor)
            for row, expected in zip(pending[recovered:], expected_lines[recovered:]):
                offset = cursor
                journal.write(expected)
                cursor += len(expected)
                projections.append(
                    (
                        str(row["signal_id"]),
                        str(row["payload_hash"]),
                        offset,
                        len(expected),
                    )
                )
            journal.flush()
            os.fsync(journal.fileno())

        self._after_append()
        self._ledger.mark_signal_journal_mirrored(projections, cursor, now)
        return len(projections)

    @staticmethod
    def _journal_key(entry: dict[str, Any]) -> tuple[str, ...]:
        signal_id = str(entry.get("signal_id", ""))
        if signal_id:
            return ("signal_id", signal_id)
        return (
            "legacy",
            str(entry.get("strategy", "")),
            str(entry.get("ticker", "")),
            str(entry.get("timestamp", "")),
        )

    def get_entries(
        self,
        strategy: str | None = None,
        ticker: str | None = None,
        since: str | None = None,
    ) -> list[dict]:
        """Read journal entries, optionally filtered."""
        if not self._path.exists():
            return []

        cutoff = datetime.fromisoformat(since) if since else None
        results: list[dict] = []

        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if strategy and entry.get("strategy") != strategy:
                continue
            if ticker and entry.get("ticker", "").upper() != ticker.upper():
                continue
            if cutoff:
                try:
                    ts = datetime.fromisoformat(entry["timestamp"])
                    if ts < cutoff:
                        continue
                except (KeyError, ValueError):
                    continue

            results.append(entry)

        return results
