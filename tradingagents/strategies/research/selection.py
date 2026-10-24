"""Pure fixed-budget selection over already-qualified, frozen discovery records.

Qualification belongs to the retained source manifest, not this experiment.
Ticker is an issuer proxy; source event keys are source groups rather than
independently verified economic-event clusters. No source or model calls occur.
"""
from __future__ import annotations

from copy import deepcopy
from collections import Counter
import math
import re
from typing import Iterable


_QUALIFIED_REASONS = frozenset({"admitted", "analysis_budget"})
_POLICIES = frozenset({"legacy_v1", "issuer_event_v1"})


def _legacy_rank(row: dict, strategy: str) -> tuple:
    prefix = (row["journal_only"],) if strategy == "govt_contracts" else ()
    return (*prefix, -row["score"], row["ticker"], row["discovery_id"])


def _validate_row(row: dict, seen: set[str]) -> None:
    if not isinstance(row, dict):
        raise ValueError("discovery record must be a dict")
    for field in ("discovery_id", "event_key", "reason"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ValueError(f"discovery record requires nonempty {field}")
    if row["discovery_id"] in seen:
        raise ValueError("duplicate discovery_id in retained records")
    seen.add(row["discovery_id"])
    if not isinstance(row.get("ticker"), str):
        raise ValueError("discovery ticker must be a string")
    if row.get("direction") not in ("long", "short"):
        raise ValueError("discovery direction must be long or short")
    if type(row.get("journal_only")) is not bool:
        raise ValueError("discovery journal_only must be a bool")
    if row["reason"] in _QUALIFIED_REASONS:
        score = row.get("score")
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score)):
            raise ValueError("qualified discovery requires a finite numeric score")
    if "source_event_key" in row:
        value = row["source_event_key"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("source_event_key must be a nonempty string when supplied")
    fingerprint = row.get("evidence_sha256")
    if fingerprint is not None and (
        not isinstance(fingerprint, str)
        or re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None
    ):
        raise ValueError("evidence_sha256 must be a lowercase SHA-256 digest")


def _issuer_event_order(qualified: list[dict], strategy: str) -> list[dict]:
    """Greedily prefer underrepresented symbols, then source-event groups."""
    representatives = {}
    remaining = []
    for row in sorted(qualified, key=lambda row: (row["journal_only"], _legacy_rank(row, strategy))):
        fingerprint = row.get("evidence_sha256")
        if fingerprint is not None:
            identity = (row["event_key"], row["ticker"], row["direction"], fingerprint)
            if identity in representatives:
                row["reason"] = "duplicate_source_hypothesis"
                row["duplicate_of"] = representatives[identity]
                continue
            representatives[identity] = row["discovery_id"]
        remaining.append(row)
    issuers, events = Counter(), Counter()
    ordered = []
    while remaining:
        selected = min(remaining, key=lambda row: (
            row["journal_only"], issuers[row["ticker"]],
            events[row.get("source_event_key", row["event_key"])],
            _legacy_rank(row, strategy),
        ))
        remaining.remove(selected)
        ordered.append(selected)
        issuers[selected["ticker"]] += 1
        events[selected.get("source_event_key", selected["event_key"])] += 1
    return ordered


def select_candidates(
    rows: Iterable[dict], budget: int, *, strategy: str, policy: str,
) -> dict:
    """Select at a scalar budget; preserve all discoveries and original reasons.

    ``legacy_v1`` reconstructs the three audited strategies' retained score rank
    (verified-first for government awards). ``issuer_event_v1`` first preserves
    the qualified/nonjournal tier, then greedily minimizes selected counts for
    the ticker and native source-event group, with the legacy rank breaking ties.
    Only a known evidence fingerprint permits exact-hypothesis deduplication.

    This function trusts retained qualification reasons. Callers must establish
    the manifest's provenance; the selector cannot establish source authority,
    economic equivalence, availability clocks or consolidated issuer identity.
    """
    if type(budget) is not int or budget < 0:
        raise ValueError("selection budget must be a nonnegative scalar integer")
    if not isinstance(strategy, str) or not strategy.strip():
        raise ValueError("selection strategy must be a nonempty string")
    if not isinstance(policy, str) or policy not in _POLICIES:
        raise ValueError("unknown selection policy")
    try:
        discovered = deepcopy(list(rows))
    except TypeError as error:
        raise ValueError("selection rows must be an iterable of discovery records") from error
    seen = set()
    for row in discovered:
        _validate_row(row, seen)
        row["original_reason"] = row["reason"]
    qualified = [row for row in discovered if row["reason"] in _QUALIFIED_REASONS]
    qualified.sort(key=lambda row: _legacy_rank(row, strategy))
    rejected = sorted((row for row in discovered if row["reason"] not in _QUALIFIED_REASONS),
                      key=lambda row: row["discovery_id"])
    ranked = _issuer_event_order(qualified, strategy) if policy == "issuer_event_v1" else qualified
    for index, row in enumerate(ranked):
        row["reason"] = "admitted" if index < budget else "analysis_budget"
    ordered = qualified + rejected
    return {"strategy": strategy, "policy": policy, "budget": budget,
            "discovered": ordered,
            "admitted": ranked[:budget],
            "excluded": [row for row in ordered if row["reason"] != "admitted"]}
