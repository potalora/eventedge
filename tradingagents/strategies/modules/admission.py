"""Observable, deterministic analysis admission over a discovered candidate set.

Discovery here means the screen's qualifying source-event hypotheses, not every
raw provider row. Provider acquisition windows/universes remain separate evidence.
The manifest is a pre-analysis snapshot and must be saved even at budget zero.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
import hashlib
import json
import math
from typing import Callable, Iterable

from .base import Candidate


_UNIVERSE = ContextVar('candidate_equity_universe', default=None)


@contextmanager
def candidate_universe(universe):
    """Scope the declared source-bound universe to one strategy screen."""
    token = _UNIVERSE.set(universe)
    try:
        yield
    finally:
        _UNIVERSE.reset(token)


def _universe_decision(universe, strategy, candidate):
    if strategy == 'filing_analysis':
        membership = universe.filing_decision(candidate.metadata.get('source_ciks', []))
        decision = ('outside_sip_exchange_universe' if membership['status'] == 'excluded'
                    else 'possible_eligible_issuer' if membership['status'] == 'eligible'
                    else 'unresolved_issuer')
    else:
        membership = None
        decision = universe.decision(candidate.ticker)
    evidence = {'policy': universe.evidence['policy'], 'assets_sha256': universe.evidence['assets_sha256'],
                'decision': decision, 'symbol': candidate.ticker}
    if membership is not None:
        evidence['issuer_membership'] = membership
    return evidence


class CandidatePopulation(list[Candidate]):
    """List-compatible admitted candidates with their complete discovery snapshot."""

    def __init__(self, candidates: Iterable[Candidate], admission_manifest: dict):
        super().__init__(candidates)
        self.admission_manifest = deepcopy(admission_manifest)


def _source_identity(strategy: str, candidate: Candidate) -> str:
    if candidate.event_key:
        return candidate.event_key
    # Use source-native identity before an analyst resolves an unknown issuer.
    from tradingagents.strategies.orchestration.event_identity import canonical_event_key
    try:
        return canonical_event_key(strategy, candidate.ticker or "UNRESOLVED",
                                   candidate.metadata, date.fromisoformat(candidate.date))
    except ValueError:
        # Invalid source identities remain visible; this ID never substitutes for
        # canonical ledger validation or makes an invalid candidate actionable.
        ignored = {"llm_analysis", "analysis_status", "analysis_failure_reason",
                   "non_actionable_reason", "discovery_id", "analysis_admitted", "equity_universe"}
        source = {key: value for key, value in candidate.metadata.items() if key not in ignored}
        payload = json.dumps([strategy, candidate.ticker, source], sort_keys=True, default=str)
        return "unresolved_source_" + hashlib.sha256(payload.encode()).hexdigest()[:24]


def admit_candidates(
    strategy: str,
    candidates: Iterable[Candidate],
    budget: int | dict[str, int] | None,
    *,
    rank_key: Callable[[Candidate], tuple] | None = None,
    policy: str = "score_desc_ticker_source_identity_v1",
) -> CandidatePopulation:
    """Snapshot every discovered hypothesis, then admit a deterministic budget.

    `budget=None` records an unbounded screen. Caller-specific ranking must append
    a stable identity tie-break (supplied here), never a provider-order index.
    """
    limits = budget.values() if isinstance(budget, dict) else (() if budget is None else (budget,))
    if any(type(limit) is not int or limit < 0 for limit in limits):
        raise ValueError("analysis admission budget must be a nonnegative integer")
    entries = []
    universe = _UNIVERSE.get()
    for candidate in candidates:
        source_id = _source_identity(strategy, candidate)
        payload = json.dumps([strategy, source_id, candidate.direction], separators=(",", ":"))
        discovery_id = "discovery_" + hashlib.sha256(payload.encode()).hexdigest()[:24]
        candidate.metadata["discovery_id"] = discovery_id
        valid_score = (not isinstance(candidate.score, bool)
                       and isinstance(candidate.score, (int, float))
                       and math.isfinite(candidate.score))
        row = {"discovery_id": discovery_id, "event_key": source_id,
               "ticker": candidate.ticker, "direction": candidate.direction,
               "score": candidate.score if valid_score else None,
               "journal_only": candidate.journal_only}
        if universe is not None:
            row['universe'] = _universe_decision(universe, strategy, candidate)
            candidate.metadata['equity_universe'] = deepcopy(row['universe'])
        candidate.metadata.pop('analysis_admitted', None)
        if valid_score:
            rank = rank_key(candidate) if rank_key else (-float(candidate.score), candidate.ticker)
            order = (0, *rank, discovery_id)
        else:
            # NaN is not ordered, so it must never reach even a custom comparator.
            # Preserve its diagnostic representation in JSON-safe immutable health.
            row["invalid_score_value"] = repr(candidate.score)
            order = (1, candidate.ticker, discovery_id, row["invalid_score_value"])
        entries.append((order, candidate, row, valid_score))
    entries.sort(key=lambda entry: entry[0])
    admitted, discovered, excluded, selected = [], [], [], []
    direction_counts: dict[str, int] = {}
    for _, candidate, row, valid_score in entries:
        limit = budget.get(candidate.direction, 0) if isinstance(budget, dict) else budget
        used = direction_counts.get(candidate.direction, 0) if isinstance(budget, dict) else len(selected)
        reason = "admitted" if limit is None or used < limit else "analysis_budget"
        if row.get('universe', {}).get('decision') in {
            'outside_sip_exchange_universe', 'inactive_asset', 'absent_from_asset_master'
        }:
            reason = 'equity_universe:' + row['universe']['decision']
        if not valid_score:
            reason = "invalid_score"
        snapshot = dict(row, reason=reason)
        discovered.append(snapshot)
        if reason == "admitted":
            candidate.metadata["analysis_admitted"] = True
            selected.append(candidate)
            direction_counts[candidate.direction] = direction_counts.get(candidate.direction, 0) + 1
            admitted.append(snapshot)
        else:
            excluded.append(snapshot)
    manifest = {"version": 1, "strategy": strategy, "budget": budget,
                "population": "screen_qualifying_hypotheses", "policy": policy,
                "discovered": discovered, "admitted": admitted, "excluded": excluded}
    if universe is not None:
        manifest['universe_policy'] = universe.evidence['policy']
    return CandidatePopulation(selected, manifest)
