"""Immutable all-horizon screening before health, committee or staging writes.

The source codec retains complete signal and failure evidence. This checkpoint
provides no acquisition/model callback and never repairs accepted observations.
"""
from __future__ import annotations

from dataclasses import asdict, fields
from datetime import date
import hashlib
import re
from typing import Any, get_args

from tradingagents.strategies.metrics.identity import _stable_id
from tradingagents.strategies.metrics.models import HealthStatus, StrategyHealthRecord

from .research_resilience import POLICY, configured
from .source_inputs import MAX_DEPTH, SourceInputError, SourceInputStore, daily_source_store

VERSION = 'research-screening-v1'
_HORIZONS = frozenset(('30d', '3m', '6m', '1y'))
_HEALTH_FIELDS = frozenset(field.name for field in fields(StrategyHealthRecord))
Screening = dict[str, tuple[list[dict], dict, list[StrategyHealthRecord]]]


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise SourceInputError('research screening ' + reason)


def _ordered(value: Any, depth: int = 0) -> Any:
    """The source codec retains mapping order; checkpoint identities do not."""
    _require(depth <= MAX_DEPTH, 'document exceeds structural bounds')
    if type(value) is dict:
        _require(all(type(key) is str for key in value), 'mapping keys must be strings')
        return {key: _ordered(value[key], depth + 1) for key in sorted(value)}
    if type(value) is list:
        return [_ordered(item, depth + 1) for item in value]
    if type(value) is tuple:
        return tuple(_ordered(item, depth + 1) for item in value)
    return value


def _digest(store: SourceInputStore, value: Any) -> str:
    return hashlib.sha256(store.encode(_ordered(value), **store.codec_limits).encode()).hexdigest()


def _context(owner: Any, session: date, source_digest: str, horizons=None):
    _require(type(session) is date, 'session must be a date')
    _require(type(source_digest) is str and re.fullmatch('[0-9a-f]{64}', source_digest) is not None,
             'source digest invalid')
    _require(type(owner._epoch_id) is str and bool(owner._epoch_id), 'epoch missing')
    configured_horizons = tuple(sorted({cohort['config'].horizon for cohort in owner.cohorts}))
    _require(bool(configured_horizons) and set(configured_horizons) <= _HORIZONS, 'horizon scope invalid')
    if horizons is not None:
        _require(type(horizons) is tuple and len(horizons) == len(configured_horizons)
                 and all(type(horizon) is str for horizon in horizons)
                 and tuple(sorted(horizons)) == configured_horizons, 'complete horizon scope required')
    strategies = sorted(owner._active_strategy_names)
    _require(bool(strategies) and all(type(name) is str and name for name in strategies)
             and len(set(strategies)) == len(strategies), 'strategy scope invalid')
    policies = {horizon: owner._policy_id_for_horizon(horizon) for horizon in configured_horizons}
    _require(all(type(policy) is str and policy for policy in policies.values())
             and len(set(policies.values())) == len(policies), 'policy scope invalid')
    shared, source_identity = daily_source_store(owner, session.isoformat())
    store = SourceInputStore(shared.cache_dir, accepted_dir=shared.accepted_dir / 'screening',
        ttl_s=shared.ttl_s, **shared.codec_limits)
    binding = {'policy': POLICY, 'source_identity': source_identity, 'epoch_id': owner._epoch_id,
        'source_digest': source_digest, 'horizon_policies': policies, 'strategies': strategies}
    identity = {**source_identity, 'purpose': VERSION, 'epoch_id': owner._epoch_id,
        'research_resilience_policy': POLICY, 'screening_binding_sha256': _digest(store, binding)}
    return store, identity, binding


def _durable_health(owner: Any, session: date):
    return owner._metric_store.read_strategy_health(owner._epoch_id, session=session)


def _reject_missing_after_commit(owner: Any, session: date) -> None:
    _require(not _durable_health(owner, session), 'checkpoint missing after accepted health')
    for cohort in owner.cohorts:
        horizon = cohort['config'].horizon
        policy = str(cohort['engine'].ar_config.get('paper_ledger', {}).get('policy_id',
            'foundation-' + horizon))
        ledger = cohort['ledger']
        _require(not ledger.staging_completed(session, owner._epoch_id, policy)
                 and ledger.committee_decision(session, owner._epoch_id, policy) is None,
                 'checkpoint missing after accepted committee or staging')


def _health(value: dict, binding: dict, horizon: str, session: date) -> StrategyHealthRecord:
    _require(type(value) is dict and set(value) == _HEALTH_FIELDS, 'health schema invalid')
    _require(value['epoch_id'] == binding['epoch_id'] and type(value['session']) is date
             and value['session'] == session and value['policy_id'] == binding['horizon_policies'][horizon],
             'health identity mismatch')
    _require(type(value['strategy']) is str and value['strategy'] in binding['strategies']
             and type(value['status']) is str and value['status'] in get_args(HealthStatus),
             'health classification invalid')
    _require(type(value['signal_count']) is int and value['signal_count'] >= 0
             and type(value['evidence']) is dict, 'health evidence invalid')
    _require(value['health_id'] == _stable_id('health', binding['epoch_id'], session,
        binding['horizon_policies'][horizon], value['strategy']), 'health ID invalid')
    return StrategyHealthRecord(**value)


def _receipt(store: SourceInputStore, payload: dict) -> dict:
    binding, horizons = payload['binding'], payload['horizons']
    return {'version': VERSION, 'policy': POLICY, 'source_digest': binding['source_digest'],
        'epoch_id': binding['epoch_id'], 'session': binding['source_identity']['session'],
        'binding_sha256': _digest(store, binding),
        'horizon_policies': binding['horizon_policies'],
        'counts': {horizon: {'signals': len(value['signals']), 'health': len(value['health']),
            'failed_health': sum(record['status'] in ('data_failure', 'strategy_defect')
                for record in value['health'])} for horizon, value in horizons.items()},
        'horizon_sha256': {horizon: _digest(store, value) for horizon, value in horizons.items()},
        'model_coverage_sha256': _digest(store, payload['model_coverage']),
        'screening_sha256': _digest(store, {key: value for key, value in payload.items() if key != 'receipt'})}


def _decode(store: SourceInputStore, payload: Any, binding: dict, session: date):
    _require(type(payload) is dict and set(payload) ==
        {'version', 'binding', 'horizons', 'model_coverage', 'receipt'}, 'checkpoint schema invalid')
    _require(payload['version'] == VERSION and payload['binding'] == binding, 'checkpoint binding mismatch')
    coverage = payload['model_coverage']
    _require(type(coverage) is dict and type(coverage.get('complete')) is bool,
             'model coverage invalid')
    if coverage['complete']:
        _require(set(coverage) == {'complete'}, 'model coverage schema invalid')
    else:
        _require(set(coverage) == {'complete', 'reason'} and type(coverage['reason']) is str
                 and bool(coverage['reason'].strip()), 'model coverage reason missing')
    horizons = payload['horizons']
    _require(type(horizons) is dict and set(horizons) == set(binding['horizon_policies']),
             'complete horizon scope required')
    result: Screening = {}
    for horizon, value in sorted(horizons.items()):
        _require(type(value) is dict and set(value) == {'signals', 'regime', 'health'},
                 'horizon schema invalid')
        _require(type(value['signals']) is list and all(type(signal) is dict for signal in value['signals'])
                 and type(value['regime']) is dict and type(value['health']) is list,
                 'screening values invalid')
        records = [_health(record, binding, horizon, session) for record in value['health']]
        _require(sorted(record.strategy for record in records) == binding['strategies'],
                 'complete strategy health required')
        result[horizon] = (value['signals'], value['regime'], records)
    from .multi_strategy_engine import model_sample_incomplete
    signals = [signal for rows, _, _ in result.values() for signal in rows]
    health = [record for _, _, rows in result.values() for record in rows]
    _require(all(signal.get('metadata') is None or type(signal['metadata']) is dict for signal in signals),
             'signal metadata invalid')
    _require(not model_sample_incomplete(signals, health) or coverage['complete'] is False,
             'incomplete model sample coverage conflict')
    _require(coverage['complete'] or all(signal.get('journal_only') is True for signal in signals),
             'incomplete model sample must retain the whole-sample hold')
    _require(type(payload['receipt']) is dict
             and _digest(store, payload['receipt']) == _digest(store, _receipt(store, payload)),
             'checkpoint receipt mismatch')
    return result, coverage, payload['receipt']


def _check_accepted_health(owner: Any, store: SourceInputStore, session: date, sample: Screening) -> None:
    expected = {record.health_id: record for _, _, records in sample.values() for record in records}
    accepted = _durable_health(owner, session)
    for record in accepted:
        _require(record.health_id in expected and
            _digest(store, asdict(record)) == _digest(store, asdict(expected[record.health_id])),
            'accepted health conflict')
    # All horizons' health is durable before the first committee/staging write.
    # A subset is resumable only before that boundary, never after lost evidence.
    for cohort in owner.cohorts:
        policy = str(cohort['engine'].ar_config.get('paper_ledger', {}).get('policy_id',
            'foundation-' + cohort['config'].horizon))
        ledger = cohort['ledger']
        if (ledger.committee_decision(session, owner._epoch_id, policy) is not None
                or ledger.staging_completed(session, owner._epoch_id, policy)):
            _require({record.health_id for record in accepted} == set(expected),
                     'accepted health incomplete after committee or staging')
            break


def load_screening(owner: Any, session: date, source_digest: str,
                   horizons: tuple[str, ...]) -> tuple[Screening, dict, dict] | None:
    """Restore the entire immutable sample, or permit the first screen only."""
    if not configured(owner._base_config.get('autoresearch', {})):
        return None
    store, identity, binding = _context(owner, session, source_digest, horizons)
    payload = store.load_frozen(identity)
    if payload is None:
        _reject_missing_after_commit(owner, session)
        return None
    restored = _decode(store, payload, binding, session)
    _check_accepted_health(owner, store, session, restored[0])
    return restored


def freeze_screening(owner: Any, session: date, source_digest: str,
                     horizon_signals: Screening, model_coverage: dict) -> tuple[Screening, dict, dict]:
    """Publish one complete sample; concurrent or later conflicting samples fail."""
    _require(configured(owner._base_config.get('autoresearch', {})), 'policy not configured')
    store, identity, binding = _context(owner, session, source_digest)
    _require(type(horizon_signals) is dict and set(horizon_signals) == set(binding['horizon_policies']),
             'complete horizon scope required')
    horizons = {}
    for horizon, value in sorted(horizon_signals.items()):
        _require(type(value) is tuple and len(value) == 3, 'horizon tuple invalid')
        signals, regime, health = value
        _require(type(health) is list and all(type(record) is StrategyHealthRecord for record in health),
                 'health records required')
        horizons[horizon] = {'signals': signals, 'regime': regime, 'health': [asdict(record) for record in health]}
    payload = {'version': VERSION, 'binding': binding, 'horizons': horizons, 'model_coverage': model_coverage}
    # Encode and decode once before publication so no caller-owned mutable value
    # or unsupported Python object can become accepted screening evidence.
    payload = store.decode(store.encode(payload, **store.codec_limits), **store.codec_limits)
    payload['receipt'] = _receipt(store, payload)
    _decode(store, payload, binding, session)
    existing = store.load_frozen(identity)
    if existing is None:
        _reject_missing_after_commit(owner, session)
    else:
        _decode(store, existing, binding, session)
    accepted = store.freeze(identity, payload)
    restored = _decode(store, accepted, binding, session)
    _require(_digest(store, accepted) == _digest(store, payload), 'accepted checkpoint conflict')
    _check_accepted_health(owner, store, session, restored[0])
    return restored
