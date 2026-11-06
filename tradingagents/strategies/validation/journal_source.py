"""Build EventSpec lists from SignalJournal entries, deduped across cohorts."""
from __future__ import annotations

from typing import Any
import json

from tradingagents.strategies.validation.models import EventSpec


def events_from_journals(
    journals: list[Any],
    *,
    strategy: str | None = None,
    since: str | None = None,
) -> list[EventSpec]:
    """Read entries from one or more SignalJournals into deduped EventSpecs.

    Preserve event identity, direction and timing evidence. Identical canonical
    events dedupe across cohort projections; journal-date proxies are labeled.
    """
    seen: set[tuple[str, ...]] = set()
    events: list[EventSpec] = []

    for journal in journals:
        for entry in journal.get_entries(strategy=strategy, since=since):
            strat = entry.get("strategy", "")
            ticker = entry.get("ticker", "")
            ts = entry.get("timestamp", "")
            if not strat or not ticker or not ts:
                continue
            metadata = entry.get("metadata") or {}
            anchor = metadata.get("event_date") or metadata.get("published_at") or ts
            event_date = str(anchor)[:10]
            identity = str(metadata.get("event_key") or entry.get("event_key") or entry.get("signal_id") or json.dumps(entry, sort_keys=True, default=str))
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
                        "anchor_evidence": "source_event_time" if metadata.get("event_date") or metadata.get("published_at") else "journal_time_proxy",
                    },
                )
            )
    return events
