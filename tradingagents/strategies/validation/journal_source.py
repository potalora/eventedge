"""Build EventSpec lists from SignalJournal entries, deduped across cohorts."""
from __future__ import annotations

from typing import Any

from tradingagents.strategies.validation.models import EventSpec


def events_from_journals(
    journals: list[Any],
    *,
    strategy: str | None = None,
    since: str | None = None,
) -> list[EventSpec]:
    """Read entries from one or more SignalJournals into deduped EventSpecs.

    Events are deduped by (strategy, ticker, event_date) where event_date is the
    date part of the entry timestamp. The journal's `strategy` becomes the group.
    """
    seen: set[tuple[str, str, str]] = set()
    events: list[EventSpec] = []

    for journal in journals:
        for entry in journal.get_entries(strategy=strategy, since=since):
            strat = entry.get("strategy", "")
            ticker = entry.get("ticker", "")
            ts = entry.get("timestamp", "")
            if not strat or not ticker or not ts:
                continue
            event_date = ts[:10]
            key = (strat, ticker, event_date)
            if key in seen:
                continue
            seen.add(key)
            events.append(
                EventSpec(
                    ticker=ticker,
                    event_date=event_date,
                    group=strat,
                    metadata={
                        "direction": entry.get("direction", ""),
                        "score": entry.get("score", 0.0),
                    },
                )
            )
    return events
