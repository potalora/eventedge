from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Iterable

from .models import (
    DeduplicationResult,
    Direction,
    SignalConflict,
    SignalMetricRecord,
)


def _stable_id(kind: str, *parts: object) -> str:
    payload = json.dumps(
        [kind, *parts], sort_keys=True, separators=(",", ":"), default=str
    )
    return f"{kind}_{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def event_key(
    source: str,
    source_event_id: str,
    ticker: str,
    event_at: datetime | None,
    evidence_hash: str,
) -> str:
    normalized_source_event_id = source_event_id.strip()
    if not normalized_source_event_id:
        raise ValueError("source_event_id is required")
    return _stable_id(
        "event",
        source.strip().lower(),
        normalized_source_event_id,
        ticker.strip().upper(),
        event_at.isoformat() if event_at else "",
        evidence_hash,
    )


def signal_id(
    epoch_id: str,
    strategy: str,
    policy_id: str,
    direction: Direction,
    event_key_value: str,
) -> str:
    return _stable_id(
        "signal", epoch_id, strategy, policy_id, direction, event_key_value
    )


def execution_id(cohort_id: str, signal_id_value: str, fill_id: str) -> str:
    return _stable_id("execution", cohort_id, signal_id_value, fill_id)


def deduplicate_signals(
    records: Iterable[SignalMetricRecord],
) -> DeduplicationResult:
    unique: dict[str, SignalMetricRecord] = {}
    for record in records:
        existing = unique.get(record.signal_id)
        if existing is not None and existing != record:
            raise ValueError(
                f"signal_id {record.signal_id!r} was reused for unequal payloads"
            )
        unique[record.signal_id] = record

    groups: dict[
        tuple[str, str, str, str], list[SignalMetricRecord]
    ] = {}
    for record in unique.values():
        key = (
            record.epoch_id,
            record.event_key,
            record.strategy,
            record.policy_id,
        )
        groups.setdefault(key, []).append(record)

    accepted: list[SignalMetricRecord] = []
    conflicts: list[SignalConflict] = []
    for key in sorted(groups):
        group = groups[key]
        directions = tuple(sorted({record.direction for record in group}))
        if len(directions) > 1:
            conflicts.append(
                SignalConflict(
                    epoch_id=key[0],
                    event_key=key[1],
                    strategy=key[2],
                    policy_id=key[3],
                    directions=directions,
                )
            )
        else:
            accepted.extend(group)
    return DeduplicationResult(
        records=tuple(sorted(accepted, key=lambda item: item.signal_id)),
        conflicts=tuple(conflicts),
    )
