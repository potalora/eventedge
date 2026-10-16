"""Immutable all-horizon research decisions survive process/cohort interruptions."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingagents.strategies.metrics.identity import _stable_id
from tradingagents.strategies.metrics.models import StrategyHealthRecord
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.orchestration.source_inputs import SourceInputError, SourceInputStore
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

SESSION = date(2026, 10, 9)
HORIZONS = ('30d', '3m', '6m', '1y')
SOURCE = 'a' * 64


@pytest.fixture
def owner(tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError('screening checkpoint must not fetch or run models')
    value = SimpleNamespace(
        _base_config={'autoresearch': {'state_dir': str(tmp_path),
            'research_resilience_policy': 'dependency_isolation_v1'}},
        _metric_epoch_context=SimpleNamespace(generation_id='gen_test', generation_commit='b' * 40),
        _epoch_id='epoch-test', _active_strategy_names={'filing_analysis', 'weather_ag'},
        _metric_store=MetricStore(tmp_path / 'metrics.db'),
        _policy_id_for_horizon=lambda h: 'foundation-' + h,
        _screen_for_horizon=forbidden, _fetch_all_data=forbidden, cohorts=[])
    for horizon in HORIZONS:
        for size in ('5k', '10k', '50k', '100k'):
            name = f'horizon_{horizon}_size_{size}'
            ledger = PortfolioLedger(tmp_path / name / 'portfolio.db', name, Decimal('10000'))
            value.cohorts.append({'config': SimpleNamespace(name=name, horizon=horizon),
                'engine': SimpleNamespace(ar_config={'paper_ledger': {'policy_id': 'paper-' + horizon}}),
                'ledger': ledger})
    yield value
    for cohort in value.cohorts:
        cohort['ledger'].close()


def screening(owner):
    result = {}
    for horizon in HORIZONS:
        signals = [{'ticker': 'GOOD', 'strategy': 'weather_ag', 'direction': 'long', 'score': 0.7,
            'metadata': {'analysis_status': 'complete', 'source_reference': 'weather:good',
                'attempts': [{'attempt': 1, 'outcome': 'invalid_response'},
                             {'attempt': 2, 'outcome': 'accepted', 'response_sha256': 'c' * 64}]}}]
        health = []
        for strategy, status in [('filing_analysis', 'data_failure'), ('weather_ag', 'signals')]:
            policy = owner._policy_id_for_horizon(horizon)
            health.append(StrategyHealthRecord(
                _stable_id('health', owner._epoch_id, SESSION, policy, strategy),
                owner._epoch_id, SESSION, policy, strategy, status, int(status == 'signals'),
                {'failed_attempts': [{'source': 'sec:bad', 'reason': 'malformed_original',
                    'original_sha256': 'd' * 64, 'known_population': False}],
                 'source_coverage': {'complete': False, 'excluded': ['sec:bad']},
                 'admission_manifest': {'discovered': [{'source_reference': 'sec:bad'}],
                    'admitted': [], 'excluded': [{'source_reference': 'sec:bad', 'reason': 'dependency_failure'}]}}))
        result[horizon] = (signals, {'overall_regime': 'neutral', 'context_sha256': 'e' * 64}, health)
    return result


def api():
    return importlib.import_module('tradingagents.strategies.orchestration.research_screening')


def accepted_file(owner):
    return next((Path(owner._base_config['autoresearch']['state_dir']) / 'source_inputs' / 'screening').glob('*.json'))


def test_complete_freeze_restores_all_horizons_health_and_failed_attempts_without_calls(owner):
    module = api(); original = screening(owner)
    assert module.load_screening(owner, SESSION, SOURCE, HORIZONS) is None
    frozen = module.freeze_screening(owner, SESSION, SOURCE, original, {'complete': True})
    signals, coverage, receipt = frozen
    assert signals == original and coverage == {'complete': True}
    assert set(signals) == set(HORIZONS) and all(isinstance(row, StrategyHealthRecord)
        for _, _, health in signals.values() for row in health)
    assert receipt['source_digest'] == SOURCE
    assert receipt['counts'] == {h: {'signals': 1, 'health': 2, 'failed_health': 1} for h in HORIZONS}
    before = accepted_file(owner).read_bytes()
    original['30d'][0][0]['metadata']['attempts'].clear()
    restored = module.load_screening(owner, SESSION, SOURCE, HORIZONS)
    assert restored == frozen and accepted_file(owner).read_bytes() == before
    assert len(restored[0]['30d'][0][0]['metadata']['attempts']) == 2
    assert restored[0]['30d'][2][0].evidence['failed_attempts'][0]['reason'] == 'malformed_original'


def test_deadline_hold_freezes_every_attempt_and_cannot_turn_into_entries(owner):
    module = api(); value = screening(owner)
    for signals, _, _ in value.values():
        signals[0]['journal_only'] = True
        signals[0]['metadata'].update(analysis_status='failed', analysis_failure_reason='model_deadline_exhausted')
    coverage = {'complete': False, 'reason': 'model_deadline_exhausted'}
    saved = module.freeze_screening(owner, SESSION, SOURCE, value, coverage)
    with pytest.raises(SourceInputError, match='conflict'):
        module.freeze_screening(owner, SESSION, SOURCE, screening(owner), {'complete': True})
    assert module.load_screening(owner, SESSION, SOURCE, HORIZONS) == saved
    assert all(signals[0]['journal_only'] for signals, _, _ in saved[0].values())


def test_interruption_before_publication_leaves_no_checkpoint_and_allows_first_freeze(owner, monkeypatch):
    module = api()
    import os
    original = os.link
    def interrupted(*args, **kwargs): raise SystemExit('simulated process interruption')
    monkeypatch.setattr(os, 'link', interrupted)
    with pytest.raises(SystemExit):module.freeze_screening(owner, SESSION, SOURCE, screening(owner), {'complete': True})
    monkeypatch.setattr(os, 'link', original)
    assert module.load_screening(owner, SESSION, SOURCE, HORIZONS) is None
    assert module.freeze_screening(owner, SESSION, SOURCE, screening(owner), {'complete': True})[1] == {'complete': True}


def test_interruption_after_freeze_and_partial_health_or_cohort_commit_restores_identical_sample(owner):
    module = api(); saved = module.freeze_screening(owner, SESSION, SOURCE, screening(owner), {'complete': True})
    before = accepted_file(owner).read_bytes()
    # A crash while health is being saved is resumable before any committee.
    owner._metric_store.save_strategy_health(saved[0]['30d'][2][0])
    assert module.load_screening(owner, SESSION, SOURCE, HORIZONS) == saved
    for _, _, health in saved[0].values():
        for record in health:
            owner._metric_store.save_strategy_health(record)
    # All health precedes the first book commit; other books are uncommitted.
    cohort = owner.cohorts[0]
    cohort['ledger'].record_committee_decision(SESSION, owner._epoch_id, 'paper-30d', {'new_entries': [], 'hold': True})
    cohort['ledger'].complete_staging(SESSION, owner._epoch_id, 'paper-30d', datetime.now(timezone.utc),
        lambda: None, cohort['ledger'].execution_governed_state_digest(SESSION))
    assert module.load_screening(owner, SESSION, SOURCE, HORIZONS) == saved
    assert module.freeze_screening(owner, SESSION, SOURCE, saved[0], saved[1]) == saved
    assert accepted_file(owner).read_bytes() == before


@pytest.mark.parametrize('durable', ['health', 'committee', 'staging'])
def test_missing_checkpoint_after_durable_research_effect_is_integrity_failure(owner, durable):
    module = api(); cohort = owner.cohorts[0]
    if durable == 'health':owner._metric_store.save_strategy_health(screening(owner)['30d'][2][0])
    elif durable == 'committee':cohort['ledger'].record_committee_decision(SESSION, owner._epoch_id, 'paper-30d', {'hold': True})
    else:cohort['ledger'].complete_staging(SESSION, owner._epoch_id, 'paper-30d', datetime.now(timezone.utc),
        lambda: None, cohort['ledger'].execution_governed_state_digest(SESSION))
    with pytest.raises(SourceInputError, match='missing'):
        module.load_screening(owner, SESSION, SOURCE, HORIZONS)
    with pytest.raises(SourceInputError, match='missing'):
        module.freeze_screening(owner, SESSION, SOURCE, screening(owner), {'complete': True})
    assert not list((Path(owner._base_config['autoresearch']['state_dir']) / 'source_inputs' / 'screening').glob('*.json'))


@pytest.mark.parametrize('change', ['source', 'epoch', 'configuration', 'generation_commit', 'policy_id', 'horizons'])
def test_accepted_screen_cannot_cross_identity_or_partial_horizon_scope(owner, change):
    module = api();module.freeze_screening(owner, SESSION, SOURCE, screening(owner), {'complete': True})
    source, horizons = SOURCE, HORIZONS
    if change == 'source':source = 'f' * 64
    elif change == 'epoch':owner._epoch_id = 'epoch-new'
    elif change == 'configuration':owner._base_config['model'] = 'different'
    elif change == 'generation_commit':owner._metric_epoch_context.generation_commit = 'c' * 40
    elif change == 'policy_id':owner._policy_id_for_horizon = lambda h: 'changed-' + h
    else:horizons = ('30d',)
    with pytest.raises(SourceInputError):module.load_screening(owner, SESSION, source, horizons)


@pytest.mark.parametrize('damage', ['truncated', 'changed_payload', 'extra_schema', 'missing_horizon',
    'health_epoch', 'health_policy', 'health_status', 'health_id', 'health_count', 'health_fields',
    'missing_strategy', 'duplicate_strategy', 'receipt_counts', 'receipt_digest', 'coverage'])
def test_changed_or_resealed_accepted_screen_fails_closed(owner, damage):
    module = api();module.freeze_screening(owner, SESSION, SOURCE, screening(owner), {'complete': True})
    path = accepted_file(owner); original = path.read_bytes()
    if damage == 'truncated':path.write_bytes(original[:len(original)//2])
    else:
        envelope = SourceInputStore.decode(original.decode()); payload = envelope['payload']
        row = payload['horizons']['30d']['health'][0]
        if damage == 'changed_payload':payload['horizons']['30d']['signals'][0]['ticker'] = 'BAD'
        elif damage == 'extra_schema':payload['unbound'] = True
        elif damage == 'missing_horizon':payload['horizons'].pop('1y')
        elif damage == 'health_epoch':row['epoch_id'] = 'other'
        elif damage == 'health_policy':row['policy_id'] = 'other'
        elif damage == 'health_status':row['status'] = 'made_up_success'
        elif damage == 'health_id':row['health_id'] = 'other'
        elif damage == 'health_count':row['signal_count'] = True
        elif damage == 'health_fields':row['extra'] = True
        elif damage == 'missing_strategy':payload['horizons']['30d']['health'].pop()
        elif damage == 'duplicate_strategy':payload['horizons']['30d']['health'].append(deepcopy(row))
        elif damage == 'receipt_counts':payload['receipt']['counts']['30d']['failed_health'] = 0
        elif damage == 'receipt_digest':payload['receipt']['screening_sha256'] = '0' * 64
        else:payload['model_coverage']['complete'] = 1
        if damage != 'changed_payload':envelope['digest'] = hashlib.sha256(SourceInputStore.encode(payload).encode()).hexdigest()
        path.write_text(SourceInputStore.encode(envelope))
    damaged = path.read_bytes()
    with pytest.raises(SourceInputError):module.load_screening(owner, SESSION, SOURCE, HORIZONS)
    with pytest.raises(SourceInputError):module.freeze_screening(owner, SESSION, SOURCE, screening(owner), {'complete': True})
    assert path.read_bytes() == damaged


def test_conflicting_accepted_health_does_not_authorize_screen_reuse(owner):
    module = api();saved = module.freeze_screening(owner, SESSION, SOURCE, screening(owner), {'complete': True})
    record = saved[0]['30d'][2][0]
    owner._metric_store.save_strategy_health(replace(record, evidence={'error': 'different accepted attempt'}))
    with pytest.raises(SourceInputError, match='health'):
        module.load_screening(owner, SESSION, SOURCE, HORIZONS)


@pytest.mark.parametrize('damage', ['partial_horizons', 'missing_health', 'wrong_policy', 'coverage', 'source'])
def test_invalid_first_sample_never_publishes_accepted_checkpoint(owner, damage):
    module = api();value = screening(owner);coverage = {'complete': True};source = SOURCE
    if damage == 'partial_horizons':value.pop('1y')
    elif damage == 'missing_health':value['30d'][2].pop()
    elif damage == 'wrong_policy':value['30d'][2][0] = replace(value['30d'][2][0], policy_id='other')
    elif damage == 'coverage':coverage = {'complete': False}
    else:source = 'not-a-digest'
    with pytest.raises(SourceInputError):module.freeze_screening(owner, SESSION, source, value, coverage)
    assert not list((Path(owner._base_config['autoresearch']['state_dir']) / 'source_inputs' / 'screening').glob('*.json'))


def test_policy_absence_and_invalid_policy_do_not_create_screening_state(owner):
    module = api();owner._base_config['autoresearch'].pop('research_resilience_policy')
    assert module.load_screening(owner, SESSION, SOURCE, HORIZONS) is None
    with pytest.raises(SourceInputError):module.freeze_screening(owner, SESSION, SOURCE, screening(owner), {'complete': True})
    owner._base_config['autoresearch']['research_resilience_policy'] = True
    with pytest.raises(ValueError):module.load_screening(owner, SESSION, SOURCE, HORIZONS)
    assert not (Path(owner._base_config['autoresearch']['state_dir']) / 'source_inputs').exists()


@pytest.mark.parametrize('damage', ['signal_deadline', 'health_deadline', 'eligible_prefix'])
def test_unheld_incomplete_model_sample_cannot_be_frozen(owner, damage):
    module = api();value = screening(owner);coverage = {'complete': True}
    if damage == 'signal_deadline':
        value['30d'][0][0]['metadata']['analysis_failure_reason'] = 'model_deadline_exhausted'
    elif damage == 'health_deadline':
        value['30d'][2][0].evidence['model_coverage'] = {'complete': False, 'reason': 'model_deadline_exhausted'}
    else:coverage = {'complete': False, 'reason': 'model_deadline_exhausted'}
    with pytest.raises(SourceInputError, match='model sample'):
        module.freeze_screening(owner, SESSION, SOURCE, value, coverage)
    assert not list((Path(owner._base_config['autoresearch']['state_dir']) / 'source_inputs' / 'screening').glob('*.json'))


def test_interruption_at_publication_restores_the_complete_first_sample(owner, monkeypatch):
    module = api();value = screening(owner)
    import os
    original = os.link
    def interrupted_after_link(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit('process stopped after atomic publication')
    monkeypatch.setattr(os, 'link', interrupted_after_link)
    with pytest.raises(SystemExit):module.freeze_screening(owner, SESSION, SOURCE, value, {'complete': True})
    monkeypatch.setattr(os, 'link', original)
    restored = module.load_screening(owner, SESSION, SOURCE, HORIZONS)
    assert restored[0] == value
    before = accepted_file(owner).read_bytes()
    assert module.freeze_screening(owner, SESSION, SOURCE, value, {'complete': True}) == restored
    assert accepted_file(owner).read_bytes() == before


def test_checkpoint_inherits_codec_limit_without_publishing_oversize_sample(owner, monkeypatch):
    module = api()
    from tradingagents.strategies.orchestration import source_inputs
    monkeypatch.setattr(source_inputs, 'MAX_BYTES', 2048)
    with pytest.raises(SourceInputError, match='byte limit'):
        module.freeze_screening(owner, SESSION, SOURCE, screening(owner), {'complete': True})
    assert not list((Path(owner._base_config['autoresearch']['state_dir']) / 'source_inputs' / 'screening').glob('*.json'))


def test_missing_health_after_committee_is_integrity_failure(owner):
    module = api()
    saved = module.freeze_screening(owner, SESSION, SOURCE, screening(owner), {'complete': True})
    owner._metric_store.save_strategy_health(saved[0]['30d'][2][0])
    owner.cohorts[0]['ledger'].record_committee_decision(
        SESSION, owner._epoch_id, 'paper-30d', {'new_entries': [], 'hold': True})
    with pytest.raises(SourceInputError, match='health.*incomplete'):
        module.load_screening(owner, SESSION, SOURCE, HORIZONS)
