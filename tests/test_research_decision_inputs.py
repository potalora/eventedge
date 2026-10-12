"""Immutable research enrichment and bounded local hold finalization."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import time

import pytest

from test_research_screening import owner, screening, SESSION, HORIZONS
from tradingagents.strategies.orchestration import decision_clock as clock
from tradingagents.strategies.orchestration.source_inputs import SourceInputError, SourceInputStore, daily_source_store

NOW = datetime(2026, 10, 10, 18, tzinfo=timezone.utc)


def source(owner, prospective=False):
    if prospective:
        owner._base_config['autoresearch']['decision_clock_policy'] = clock.PROSPECTIVE
    data = {'research': {'observations': ['accepted original']}}
    if prospective:
        data['_decision_acquisition'] = {'policy': clock.PROSPECTIVE,
            'reference_session': SESSION.isoformat(), 'started_at': NOW.isoformat(), 'vintage_as_of': '2026-10-10'}
    store, identity = daily_source_store(owner, SESSION.isoformat())
    frozen = store.freeze(identity, data)
    digest = hashlib.sha256(store.encode(frozen, **store.codec_limits).encode()).hexdigest()
    return frozen, digest


def checkpoint(owner, source_digest, holds=(), *, model_incomplete=False):
    from tradingagents.strategies.orchestration.research_screening import freeze_screening
    from tradingagents.strategies.orchestration.research_resilience import hold_signals
    from tradingagents.strategies.orchestration.multi_strategy_engine import hold_incomplete_model_sample
    sample = screening(owner)
    for horizon, (signals, regime, health) in sample.items():
        regime['research_hold_reasons'] = list(holds)
        if holds:
            hold_signals(signals, holds[0])
        if model_incomplete:
            signals, health = hold_incomplete_model_sample(signals, health, incomplete=True)
            sample[horizon] = (signals, regime, health)
    coverage = {'complete': False, 'reason': 'model_deadline_exhausted'} if model_incomplete else {'complete': True}
    return freeze_screening(owner, SESSION, source_digest, sample, coverage)


def accepted_path(owner):
    paths = list((Path(owner._base_config['autoresearch']['state_dir'])/'source_inputs'/'decision_inputs').glob('*.json'))
    assert len(paths) == 1
    return paths[0]


def forbid(*args, **kwargs):
    pytest.fail('replay/held finalization must not acquire enrichment or invoke a model')


@pytest.mark.parametrize('prospective', [False, True])
def test_research_enrichment_replays_original_without_new_acquisition_or_clock(owner, prospective):
    data, digest = source(owner, prospective)
    calls = []
    original = {'profiles': {'AAA': {'sector': 'Technology'}}, 'errors': {'profiles': {'BAD': {'reason_code': 'timeout'}}}}
    first = clock.prepare_decision_inputs(owner, SESSION, data, digest,
        lambda: calls.append('fetch') or deepcopy(original), eligibility={'bound': 'scope'}, now=lambda: NOW)
    before = accepted_path(owner).read_bytes()
    assert first[0] == original
    assert (first[1] is not None) is prospective
    repeated = clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid,
        eligibility={'bound': 'scope'}, deadline=time.monotonic()-1, now=forbid)
    assert repeated == first and calls == ['fetch']
    assert accepted_path(owner).read_bytes() == before


@pytest.mark.parametrize('damage', ['source_digest','source_data','eligibility','epoch','commit'])
def test_research_decision_replay_rejects_changed_binding_before_fetch(owner, damage):
    data, digest = source(owner)
    clock.prepare_decision_inputs(owner, SESSION, data, digest, lambda: {}, eligibility={'scope': 1})
    before = accepted_path(owner).read_bytes()
    eligibility = {'scope': 1}
    if damage == 'source_digest': digest = 'f'*64
    elif damage == 'source_data': data['research']['observations'].append('different')
    elif damage == 'eligibility': eligibility = {'scope': 2}
    elif damage == 'epoch': owner._epoch_id = 'another-epoch'
    else: owner._metric_epoch_context.generation_commit = 'c'*40
    with pytest.raises(ValueError):
        clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid, eligibility=eligibility)
    assert accepted_path(owner).read_bytes() == before


@pytest.mark.parametrize('durable', ['policy_context','committee','staging'])
def test_missing_decision_checkpoint_after_durable_effect_is_hard_failure(owner, durable):
    data, digest = source(owner)
    cohort = owner.cohorts[0]; ledger = cohort['ledger']
    if durable == 'policy_context':
        ledger.bind_policy_session_context(SESSION, epoch_id=owner._epoch_id, policy_version='test',
            policy_config={}, context={}, bound_at=NOW)
    elif durable == 'committee':
        ledger.record_committee_decision(SESSION, owner._epoch_id, 'paper-30d', {'hold': True})
    else:
        ledger.complete_staging(SESSION, owner._epoch_id, 'paper-30d', NOW, lambda: None,
            ledger.execution_governed_state_digest(SESSION))
    with pytest.raises(ValueError, match='decision inputs'):
        clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid)


@pytest.mark.parametrize('prospective', [False, True])
@pytest.mark.parametrize('hold', ['shared_regime_unavailable','required_universe_unavailable','model_deadline_exhausted'])
def test_verified_hold_finishes_after_expired_deadline_without_fetch(owner, prospective, hold):
    data, digest = source(owner, prospective)
    checkpoint(owner, digest, () if hold == 'model_deadline_exhausted' else (hold,),
        model_incomplete=hold == 'model_deadline_exhausted')
    enrichment, context = clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid,
        hold_reasons=(hold,), deadline=time.monotonic()-1, now=lambda: NOW)
    assert enrichment == {'research_hold_reasons': [hold]}
    assert (context is not None) is prospective
    before = accepted_path(owner).read_bytes()
    assert clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid,
        hold_reasons=(), now=forbid) == (enrichment, context)
    assert accepted_path(owner).read_bytes() == before


def test_deadline_expiring_after_complete_screening_allows_local_hold(owner):
    data, digest = source(owner); checkpoint(owner, digest)
    enrichment, context = clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid,
        hold_reasons=('model_deadline_exhausted',), deadline=time.monotonic()-1)
    assert enrichment == {'research_hold_reasons': ['model_deadline_exhausted']} and context is None


@pytest.mark.parametrize('case', ['unexpired','missing_screen','only_one_horizon','wrong_reason','invalid_deadline'])
def test_unproven_hold_cannot_bypass_acquisition_deadline(owner, case):
    data, digest = source(owner)
    reasons = ('model_deadline_exhausted',)
    deadline = time.monotonic()+60
    if case != 'missing_screen':
        if case == 'only_one_horizon':
            from tradingagents.strategies.orchestration.research_screening import freeze_screening
            sample = screening(owner)
            for horizon, (signals, regime, health) in sample.items():
                regime['research_hold_reasons'] = ['shared_regime_unavailable'] if horizon == '30d' else []
            freeze_screening(owner, SESSION, digest, sample, {'complete': True})
            reasons = ('shared_regime_unavailable',)
        else: checkpoint(owner, digest)
    else: deadline = time.monotonic()-1
    if case == 'wrong_reason': reasons = ('ignore_everything',)
    if case == 'invalid_deadline': deadline = float('nan')
    with pytest.raises(ValueError):
        clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid,
            hold_reasons=reasons, deadline=deadline)
    assert not list((Path(owner._base_config['autoresearch']['state_dir'])/'source_inputs'/'decision_inputs').glob('*.json'))


def test_expired_deadline_without_verified_hold_never_fetches(owner):
    data, digest = source(owner)
    with pytest.raises(ValueError, match='deadline'):
        clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid, deadline=time.monotonic()-1)


@pytest.mark.parametrize('prospective', [False, True])
def test_decision_store_inherits_capacity_for_encoding_publication_and_loading(owner, prospective, monkeypatch):
    import tradingagents.strategies.orchestration.source_inputs as inputs
    data, digest = source(owner, prospective)
    shared, identity = daily_source_store(owner, SESSION.isoformat())
    bounded = SourceInputStore(shared.cache_dir, accepted_dir=shared.accepted_dir, ttl_s=shared.ttl_s,
        max_bytes=4096, max_nodes=1000)
    monkeypatch.setattr(inputs, 'daily_source_store', lambda *a, **k: (bounded, identity))
    with pytest.raises(SourceInputError, match='byte limit'):
        clock.prepare_decision_inputs(owner, SESSION, data, digest, lambda: {'oversize': 'x'*6000}, now=lambda: NOW)


@pytest.mark.parametrize('damage', ['schema','binding_source','binding_enrichment','binding_holds','payload_type','outer_hash'])
def test_corrupt_accepted_decision_is_never_replaced(owner, damage):
    data, digest = source(owner)
    clock.prepare_decision_inputs(owner, SESSION, data, digest, lambda: {'profiles': {'AAA': {'sector': 'Technology'}}})
    path = accepted_path(owner); document = SourceInputStore.decode(path.read_text())
    payload = document['payload']
    if damage == 'schema': payload['extra'] = True
    elif damage == 'binding_source': payload['research_binding']['source_digest'] = 'f'*64
    elif damage == 'binding_enrichment': payload['enrichment']['profiles']['AAA']['sector'] = 'Changed'
    elif damage == 'binding_holds': payload['research_binding']['hold_reasons'] = ['model_deadline_exhausted']
    elif damage == 'payload_type': payload['enrichment'] = ['not', 'mapping']
    else: document['digest'] = 'f'*64
    if damage != 'outer_hash':
        document['digest'] = hashlib.sha256(SourceInputStore.encode(payload).encode()).hexdigest()
    path.write_text(SourceInputStore.encode(document)); before = path.read_bytes()
    with pytest.raises(ValueError): clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid)
    assert path.read_bytes() == before


def test_historical_legacy_behavior_has_no_checkpoint_or_hold_bypass(owner):
    owner._base_config['autoresearch'].pop('research_resilience_policy')
    data, digest = source(owner)
    calls = []
    for _ in range(2):
        assert clock.prepare_decision_inputs(owner, SESSION, data, digest, lambda: calls.append(True) or {}) == ({}, None)
    assert len(calls) == 2
    assert not (Path(owner._base_config['autoresearch']['state_dir'])/'source_inputs'/'decision_inputs').exists()


@pytest.mark.parametrize('prospective', [False, True])
def test_deadline_expiring_during_enrichment_preserves_original_and_publishes_hold(owner, prospective, monkeypatch):
    data, digest = source(owner, prospective); checkpoint(owner, digest)
    deadline = time.monotonic()+60
    returned = {'profiles': {'AAA': {'sector': 'Technology'}},
        'errors': {'profiles': {'BAD': {'reason_code': 'timeout'}}}}
    calls = []
    def fetch():
        calls.append(True)
        monkeypatch.setattr(time, 'monotonic', lambda: deadline+1)
        return deepcopy(returned)
    enrichment, context = clock.prepare_decision_inputs(owner, SESSION, data, digest, fetch,
        deadline=deadline, now=lambda: NOW)
    assert calls == [True]
    assert enrichment == {**returned, 'research_hold_reasons': ['model_deadline_exhausted']}
    assert returned == {'profiles': {'AAA': {'sector': 'Technology'}},
        'errors': {'profiles': {'BAD': {'reason_code': 'timeout'}}}}, 'do not mutate returned source object'
    assert clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid, deadline=deadline,
        now=forbid) == (enrichment, context)


def test_unclassified_enrichment_exception_remains_hard_failure(owner):
    data, digest = source(owner); checkpoint(owner, digest)
    def invalid(): raise ValueError('invalid accepted enrichment proof')
    with pytest.raises(ValueError, match='invalid accepted enrichment proof'):
        clock.prepare_decision_inputs(owner, SESSION, data, digest, invalid)
    assert not list((Path(owner._base_config['autoresearch']['state_dir'])/'source_inputs'/'decision_inputs').glob('*.json'))


@pytest.mark.parametrize('damage', [None, 'delete','corrupt'])
def test_completed_historical_research_revalidates_original_decision_inputs(owner, damage):
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    data, digest = source(owner)
    enrichment, _ = clock.prepare_decision_inputs(owner, SESSION, data, digest, lambda: {'profiles': {}})
    _, scope = project_enrichment(enrichment)
    cohort = owner.cohorts[0]
    cohort['ledger'].record_committee_decision(SESSION, owner._epoch_id, 'paper-30d',
        {'status': {'research_enrichment_scope': scope}})
    cohort['ledger'].complete_staging(SESSION, owner._epoch_id, 'paper-30d', NOW, lambda: None,
        cohort['ledger'].execution_governed_state_digest(SESSION))
    path = accepted_path(owner)
    if damage == 'delete': path.unlink()
    elif damage == 'corrupt': path.write_text('{}')
    if damage is None:
        assert clock.validate_completed_replay(owner, SESSION, owner._epoch_id, [cohort]) is None
    else:
        with pytest.raises(ValueError):
            clock.validate_completed_replay(owner, SESSION, owner._epoch_id, [cohort])
        assert not path.exists() if damage == 'delete' else path.read_text() == '{}'


def test_custom_capacity_applies_when_reading_accepted_enrichment(owner, monkeypatch):
    import tradingagents.strategies.orchestration.source_inputs as inputs
    data, digest = source(owner)
    clock.prepare_decision_inputs(owner, SESSION, data, digest, lambda: {'blob': 'x'*6000})
    shared, identity = daily_source_store(owner, SESSION.isoformat())
    bounded = SourceInputStore(shared.cache_dir, accepted_dir=shared.accepted_dir, ttl_s=shared.ttl_s,
        max_bytes=4096, max_nodes=1000)
    monkeypatch.setattr(inputs, 'daily_source_store', lambda *a, **k: (bounded, identity))
    with pytest.raises(SourceInputError, match='byte limit'):
        clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid)


def test_invalid_deadline_cannot_be_hidden_by_a_verified_sample_hold(owner):
    data, digest = source(owner); checkpoint(owner, digest, model_incomplete=True)
    with pytest.raises(ValueError, match='deadline'):
        clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid,
            hold_reasons=('model_deadline_exhausted',), deadline=float('nan'))


def test_verified_hold_cannot_publish_past_prospective_eligible_open(owner):
    data, digest = source(owner, prospective=True)
    checkpoint(owner, digest, ('shared_regime_unavailable',))
    after_open = datetime(2026, 10, 12, 15, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match='open'):
        clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid,
            hold_reasons=('shared_regime_unavailable',), deadline=time.monotonic()-1, now=lambda: after_open)
    assert not list((Path(owner._base_config['autoresearch']['state_dir'])/'source_inputs'/'decision_inputs').glob('*.json'))


def test_prospective_legacy_decision_store_also_inherits_shared_capacity(owner, monkeypatch):
    import tradingagents.strategies.orchestration.source_inputs as inputs
    owner._base_config['autoresearch'].pop('research_resilience_policy')
    data, digest = source(owner, prospective=True)
    shared, identity = daily_source_store(owner, SESSION.isoformat())
    bounded = SourceInputStore(shared.cache_dir, accepted_dir=shared.accepted_dir, ttl_s=shared.ttl_s,
        max_bytes=4096, max_nodes=1000)
    monkeypatch.setattr(inputs, 'daily_source_store', lambda *a, **k: (bounded, identity))
    with pytest.raises(SourceInputError, match='byte limit'):
        clock.prepare_decision_inputs(owner, SESSION, data, digest, lambda: {'oversize': 'x'*6000}, now=lambda: NOW)


@pytest.mark.parametrize('damage', ['resealed_enrichment','missing_scope','changed_scope','missing_completed_committee'])
def test_completed_research_replay_checks_every_durable_committee_scope(owner, damage):
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    data, digest = source(owner)
    enrichment = {'profiles': {'AAA': {'sector': 'Technology'}}}
    clock.prepare_decision_inputs(owner, SESSION, data, digest, lambda: deepcopy(enrichment))
    _, scope = project_enrichment(enrichment)
    cohort = owner.cohorts[-1]; ledger = cohort['ledger']
    policy_id = cohort['engine'].ar_config['paper_ledger']['policy_id']
    if damage != 'missing_completed_committee':
        status = {'research_enrichment_scope': deepcopy(scope)}
        if damage == 'missing_scope': status.clear()
        elif damage == 'changed_scope': status['research_enrichment_scope']['original_sha256'] = 'f'*64
        ledger.record_committee_decision(SESSION, owner._epoch_id, policy_id, {'status': status})
    ledger.complete_staging(SESSION, owner._epoch_id, policy_id, NOW, lambda: None,
        ledger.execution_governed_state_digest(SESSION))
    path = accepted_path(owner)
    if damage == 'resealed_enrichment':
        document = SourceInputStore.decode(path.read_text()); payload = document['payload']
        payload['enrichment']['profiles']['AAA']['sector'] = 'Changed'
        payload['research_binding']['enrichment_digest'] = hashlib.sha256(SourceInputStore.encode(payload['enrichment']).encode()).hexdigest()
        document['digest'] = hashlib.sha256(SourceInputStore.encode(payload).encode()).hexdigest()
        path.write_text(SourceInputStore.encode(document))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='enrichment'):
        clock.validate_completed_replay(owner, SESSION, owner._epoch_id, [cohort])
    with pytest.raises(ValueError, match='enrichment'):
        clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid)
    assert path.read_bytes() == before


def test_matching_partial_committee_scope_does_not_require_unfinished_books(owner):
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    data, digest = source(owner)
    first = clock.prepare_decision_inputs(owner, SESSION, data, digest, lambda: {'profiles': {}})
    _, scope = project_enrichment(first[0])
    cohort = owner.cohorts[-1]
    cohort['ledger'].record_committee_decision(SESSION, owner._epoch_id,
        cohort['engine'].ar_config['paper_ledger']['policy_id'], {'status': {'research_enrichment_scope': scope}})
    assert clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid) == first


@pytest.mark.parametrize('field,value', [('policy', clock.HISTORICAL), ('reference_session', '2026-10-08'),
    ('vintage_as_of', '2026-10-09'), ('started_at', 'not-a-timestamp')])
def test_invalid_prospective_acquisition_is_rejected_before_enrichment(owner, field, value):
    data, _ = source(owner, prospective=True)
    data['_decision_acquisition'][field] = value
    shared, _ = daily_source_store(owner, SESSION.isoformat())
    digest = hashlib.sha256(shared.encode(data, **shared.codec_limits).encode()).hexdigest()
    with pytest.raises(ValueError):
        clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid, now=lambda: NOW)
