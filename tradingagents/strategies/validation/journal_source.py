"""Build EventSpec lists directly from unified ``generation.sqlite3`` signals.

Also accepts legacy ``SignalJournal`` objects for backwards-compatible callers.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

from tradingagents.strategies.state.generation_db import (
    GENERATION_DB_FILENAME,
    GenerationDB,
)
from tradingagents.strategies.validation.models import EventSpec


def _resolve_generation_db(db_or_path: GenerationDB | str | Path) -> GenerationDB:
    if isinstance(db_or_path, GenerationDB):
        return db_or_path
    path = Path(db_or_path)
    if path.is_dir():
        path = path / GENERATION_DB_FILENAME
    return GenerationDB.open_readonly(path)


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _passes_since(timestamp: str, cutoff: datetime | None, cutoff_raw: str | None) -> bool:
    if cutoff_raw is None:
        return True
    parsed = _parse_iso_dt(timestamp)
    if parsed is not None and cutoff is not None:
        # Compare aware vs aware or naive vs naive cleanly.
        if (parsed.tzinfo is None) != (cutoff.tzinfo is None):
            parsed = parsed.replace(tzinfo=None)
            cutoff = cutoff.replace(tzinfo=None)
        return parsed >= cutoff
    return timestamp >= cutoff_raw


def events_from_generation_db(
    db_or_path: GenerationDB | str | Path,
    *,
    strategy: str | None = None,
    since: str | None = None,
    epoch_id: str | None = None,
) -> list[EventSpec]:
    """Read canonical signal observations directly from ``generation.sqlite3``.

    Reads ``signals`` LEFT JOIN ``signal_candidate_contexts`` in read-only mode
    without globbing cohort-level ``signal_journal.jsonl`` files. Identical
    canonical events are deduped by ``(strategy, ticker, direction, identity)``.
    """
    db = _resolve_generation_db(db_or_path)
    cutoff = _parse_iso_dt(since)

    clauses: list[str] = []
    params: list[Any] = []
    if strategy:
        clauses.append("s.strategy = ?")
        params.append(strategy)
    if epoch_id:
        clauses.append("s.epoch_id = ?")
        params.append(epoch_id)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            f"""
            SELECT
                s.signal_id,
                s.epoch_id,
                s.policy_id,
                s.event_key,
                s.strategy,
                s.ticker,
                s.direction,
                s.event_at,
                s.observed_at,
                s.reference_session,
                s.reference_close,
                s.decision_at,
                c.payload_json AS candidate_context_json
            FROM signals s
            LEFT JOIN signal_candidate_contexts c
                ON c.signal_id = s.signal_id
            {where_sql}
            ORDER BY s.observed_at, s.signal_id
            """,
            tuple(params),
        ).fetchall()

    seen: set[tuple[str, ...]] = set()
    events: list[EventSpec] = []

    for row in rows:
        strat = str(row["strategy"] or "")
        ticker = str(row["ticker"] or "")
        observed_at = str(row["observed_at"] or "")
        if not strat or not ticker or not observed_at:
            continue
        if since is not None and not _passes_since(observed_at, cutoff, since):
            continue

        context: dict[str, Any] = {}
        journal_payload: dict[str, Any] = {}
        raw_ctx = row["candidate_context_json"]
        if raw_ctx:
            try:
                parsed_ctx = json.loads(raw_ctx)
                if isinstance(parsed_ctx, dict):
                    if parsed_ctx.get("__eventedge_signal_context_v2__") is True:
                        raw_cc = parsed_ctx.get("candidate_context")
                        raw_jp = parsed_ctx.get("journal_payload")
                        context = raw_cc if isinstance(raw_cc, dict) else {}
                        journal_payload = raw_jp if isinstance(raw_jp, dict) else {}
                    else:
                        context = parsed_ctx
            except json.JSONDecodeError:
                context = {}

        metadata: dict[str, Any] = {}
        if isinstance(context.get("metadata"), dict):
            metadata.update(context["metadata"])
        if isinstance(journal_payload.get("metadata"), dict):
            metadata.update(journal_payload["metadata"])
        event_at = row["event_at"]
        published_at = (
            metadata.get("published_at")
            or journal_payload.get("published_at")
            or context.get("published_at")
            or event_at
        )
        anchor = metadata.get("event_date") or published_at or observed_at
        event_date = str(anchor)[:10]
        identity = str(
            row["event_key"]
            or metadata.get("event_key")
            or row["signal_id"]
        )
        direction = str(row["direction"] or "")
        key = (strat, ticker, direction, identity)
        if key in seen:
            continue
        seen.add(key)

        score_raw = journal_payload.get(
            "score", context.get("score", metadata.get("score", 0.0))
        )
        try:
            score = float(score_raw)
        except (TypeError, ValueError):
            score = 0.0

        has_source_event_time = bool(metadata.get("event_date") or published_at)
        events.append(
            EventSpec(
                ticker=ticker,
                event_date=event_date,
                group=strat,
                metadata={
                    **metadata,
                    "direction": direction,
                    "score": score,
                    "journaled_at": observed_at,
                    "decision_at": str(row["decision_at"] or observed_at),
                    "publication_at": published_at,
                    "signal_id": str(row["signal_id"] or ""),
                    "epoch_id": str(row["epoch_id"] or ""),
                    "reference_session": str(row["reference_session"] or ""),
                    "event_identity": identity,
                    "anchor_evidence": (
                        "source_event_time"
                        if has_source_event_time
                        else "journal_time_proxy"
                    ),
                },
            )
        )
    return events


def events_from_journals(
    journals: GenerationDB | str | Path | list[Any],
    *,
    strategy: str | None = None,
    since: str | None = None,
    epoch_id: str | None = None,
) -> list[EventSpec]:
    """Read canonical signals from ``generation.sqlite3`` or legacy SignalJournals.

    When passed a ``GenerationDB``, file path, or generation state directory,
    reads directly from ``generation.sqlite3``'s ``signals`` table. When passed
    a list of legacy ``SignalJournal`` objects, preserves deduped event loading.
    """
    if isinstance(journals, (GenerationDB, str, Path)):
        return events_from_generation_db(
            journals, strategy=strategy, since=since, epoch_id=epoch_id
        )

    seen: set[tuple[str, ...]] = set()
    events: list[EventSpec] = []

    for journal in journals:
        if isinstance(journal, (GenerationDB, str, Path)):
            for event in events_from_generation_db(
                journal, strategy=strategy, since=since, epoch_id=epoch_id
            ):
                key = (
                    event.group,
                    event.ticker,
                    str(event.metadata.get("direction", "")),
                    str(event.metadata.get("event_identity", "")),
                )
                if key not in seen:
                    seen.add(key)
                    events.append(event)
            continue

        for entry in journal.get_entries(strategy=strategy, since=since):
            strat = entry.get("strategy", "")
            ticker = entry.get("ticker", "")
            ts = entry.get("timestamp", "")
            if not strat or not ticker or not ts:
                continue
            metadata = entry.get("metadata") or {}
            anchor = metadata.get("event_date") or metadata.get("published_at") or ts
            event_date = str(anchor)[:10]
            identity = str(
                metadata.get("event_key")
                or entry.get("event_key")
                or entry.get("signal_id")
                or json.dumps(entry, sort_keys=True, default=str)
            )
            key = (strat, ticker, str(entry.get("direction", "")), identity)
            if key in seen:
                continue
            seen.add(key)
            events.append(
                EventSpec(
                    ticker=ticker,
                    event_date=event_date,
                    group=strat,
                    metadata={
                        **metadata,
                        "direction": entry.get("direction", ""),
                        "score": entry.get("score", 0.0),
                        "journaled_at": ts,
                        "decision_at": metadata.get("decision_at", ts),
                        "publication_at": metadata.get("published_at"),
                        "signal_id": entry.get("signal_id", ""),
                        "event_identity": identity,
                        "anchor_evidence": (
                            "source_event_time"
                            if metadata.get("event_date") or metadata.get("published_at")
                            else "journal_time_proxy"
                        ),
                    },
                )
            )
    return events
