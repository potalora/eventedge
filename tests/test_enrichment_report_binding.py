"""Canonical enrichment proof comes from immutable inputs and native bindings."""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace

import pytest

from tradingagents.strategies.execution.ids import stable_id
from tradingagents.strategies.metrics.models import CandidateSignalIdentityBinding
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.orchestration import decision_clock as clock
from tradingagents.strategies.orchestration import operational_report as reports
from tradingagents.strategies.orchestration.enrichment_capabilities import declaration
from tradingagents.strategies.orchestration.research_resilience import project_enrichment
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


SESSION = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 23, tzinfo=timezone.utc)
AR = {'enrichment_capability_policy': 'optional_enrichment_retirement_v1',
    'retired_enrichment_capabilities': {
        'commodity_futures_curves': 'maturity_identified_curve_evidence_unavailable'},
    'research_resilience_policy': 'dependency_isolation_v1',
    'decision_clock_policy': clock.PROSPECTIVE,
    'new_entry_eligibility_policy': 'reference_session_activity_v1',
    'paper_ledger': {'policy_id': 'reviewed-policy'}}


def _rewrite(path, mutate):
    envelope = SourceInputStore.decode(path.read_text())
    mutate(envelope['payload'])
    envelope['digest'] = hashlib.sha256(SourceInputStore.encode(envelope['payload']).encode()).hexdigest()
    path.write_text(SourceInputStore.encode(envelope))


@pytest.fixture(params=[clock.PROSPECTIVE, clock.HISTORICAL])
def accepted_enrichment(tmp_path, request):
    state = tmp_path / 'state'
    ar = {**deepcopy(AR), 'decision_clock_policy': request.param}
    profile = SimpleNamespace(config={'autoresearch': ar}, roster=reports.EXPECTED_COHORTS,
        ledger_policy_id='reviewed-policy')
    report = {'generation_id': 'gen_001', 'generation_commit': 'a' * 40,
        'session': SESSION.isoformat(), 'epoch_id': 'epoch-accepted'}
    source = {'research': {'observations': [1]}}
    if request.param == clock.PROSPECTIVE:
        source['_decision_acquisition'] = {'policy': clock.PROSPECTIVE,
            'reference_session': SESSION.isoformat(), 'started_at': NOW.isoformat(),
            'vintage_as_of': SESSION.isoformat()}
    identity = {'generation': 'gen_001', 'session': SESSION.isoformat(),
        'commit': 'a' * 40, 'configuration': 'b' * 64}
    source_store = SourceInputStore(tmp_path / 'cache', accepted_dir=state / 'source_inputs')
    source_store.freeze(identity, source, acquired_at=NOW)
    source_envelope = SourceInputStore.decode(next((state / 'source_inputs').glob('*.json')).read_text())
    enrichment = {'profiles': {'AAA': {'sector': 'Technology'}}, **declaration(ar, ['NG', 'SI'])}
    enrichment_digest = clock._decision_digest(source_store, enrichment)
    eligibility_digest = clock._decision_digest(source_store, None)
    context = (clock.create_context(profile.config, SESSION, acquired_at=NOW, cutoff=NOW,
        source_digest=source_envelope['digest'], enrichment_digest=enrichment_digest,
        eligibility_digest=eligibility_digest) if request.param == clock.PROSPECTIVE else None)
    accepted = {'context': context, 'enrichment': enrichment, 'research_binding': {
        'version': 'research-decision-inputs-v1', 'policy': 'dependency_isolation_v1',
        'clock_policy': request.param, 'epoch_id': report['epoch_id'],
        'source_digest': source_envelope['digest'], 'eligibility_digest': eligibility_digest,
        'enrichment_digest': enrichment_digest, 'hold_reasons': []}}
    store = SourceInputStore(tmp_path / 'cache', accepted_dir=state / 'source_inputs/decision_inputs')
    purpose = ('prospective-decision-inputs-v1' if request.param == clock.PROSPECTIVE
        else 'research-historical-decision-inputs-v1')
    store.freeze({**identity, 'purpose': purpose}, accepted, acquired_at=NOW)
    _, scope = project_enrichment(enrichment, capability_config=ar)
    committees = {}
    for name in profile.roster:
        ledger = PortfolioLedger(state / name / 'portfolio.db', name, Decimal('5000'))
        try:
            economic = {'market': {'required_tickers': [], 'benchmarks': [{'symbol': 'SPY'}]},
                'starting_state': {}}
            ledger.bind_session_execution_context(SESSION, report['epoch_id'],
                stable_id('session_economic_inputs', economic),
                stable_id('session_market_inputs', economic['market']), 'config',
                stable_id('session_borrow_inputs', {}), (), json.dumps(economic), '{}', NOW,
                stable_id('session_governed_state', {}), '{}')
            ledger.bind_policy_session_context(SESSION, epoch_id=report['epoch_id'],
                policy_version='test', policy_config={},
                context={'decision_clock': context} if context is not None else {}, bound_at=NOW)
            committees[name] = {'status': {'research_enrichment_scope': deepcopy(scope)}}
            ledger.record_committee_decision(SESSION, report['epoch_id'], profile.ledger_policy_id, committees[name])
            ledger.complete_staging(SESSION, report['epoch_id'], profile.ledger_policy_id, NOW,
                lambda: None, ledger.execution_governed_state_digest(SESSION))
        finally:
            ledger.close()
    MetricStore(state / 'metrics_v2.sqlite3').save_candidate_signal_identity_binding(
        CandidateSignalIdentityBinding('candidate-binding', report['epoch_id'], SESSION, ()))
    return SimpleNamespace(state=state, profile=profile, report=report, source=source_envelope,
        committees=committees, scope=scope,
        decision_path=next((state / 'source_inputs/decision_inputs').glob('*.json')))


def test_new_policy_missing_inputs_diagnoses_without_trusting_committee_bits(tmp_path):
    profile = SimpleNamespace(config={'autoresearch': deepcopy(AR)}, roster=(),
        horizon_policies={}, strategies=())
    diagnostics = []
    facts = reports._completion_facts(tmp_path, profile,
        {'session': SESSION.isoformat(), 'epoch_id': 'epoch'}, [], {},
        lambda code, *args: diagnostics.append(code))
    assert 'accepted_enrichment_invalid' in diagnostics
    assert 'enrichment_scope' not in facts


def test_reconstructs_source_bound_scope_read_only(accepted_enrichment, monkeypatch):
    value = accepted_enrichment
    before = value.decision_path.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail('canonical proof must not create a ledger or metric store')
    monkeypatch.setattr(PortfolioLedger, '__init__', forbidden)
    monkeypatch.setattr(MetricStore, '__init__', forbidden)
    actual = reports._accepted_enrichment_scope(value.state, value.profile, value.report,
        value.source, value.committees)
    assert actual == value.scope
    assert actual['supported_enrichment_complete'] is True
    assert actual['research_coverage_complete'] is False
    assert value.decision_path.read_bytes() == before


@pytest.mark.parametrize('fault', ['missing_inputs', 'changed_enrichment', 'source_digest',
    'eligibility_digest', 'epoch', 'forged_committee_scope', 'missing_committee',
    'different_staging_clock', 'forged_eligibility'])
def test_rejects_forged_or_missing_accepted_bindings(accepted_enrichment, fault):
    value = accepted_enrichment
    first = value.profile.roster[0]
    if fault == 'missing_inputs':
        value.decision_path.unlink()
    elif fault == 'changed_enrichment':
        _rewrite(value.decision_path, lambda payload: payload['enrichment']['profiles'].update(BAD={'sector': 'Unknown'}))
    elif fault in {'source_digest', 'eligibility_digest', 'epoch'}:
        key = 'epoch_id' if fault == 'epoch' else fault
        _rewrite(value.decision_path, lambda payload: payload['research_binding'].update({key: 'f' * 64}))
    elif fault == 'forged_eligibility':
        eligibility = {'policy': 'reference_session_activity_v1', 'session': SESSION.isoformat(),
            'protected_tickers': [], 'candidate_tickers': ['BAD'], 'listing_snapshot': None,
            'decisions': {'BAD': {'status': 'excluded_from_new_entry'}}}
        store = SourceInputStore(value.state / 'cache', accepted_dir=value.state / 'source_inputs/new_entry_eligibility')
        store.freeze({**value.source['identity'], 'policy': 'reference_session_activity_v1'}, eligibility, acquired_at=NOW)
        digest = clock._decision_digest(store, eligibility)
        def rebind(payload):
            payload['research_binding']['eligibility_digest'] = digest
            if payload['context'] is not None:
                payload['context']['eligibility_digest'] = digest
        _rewrite(value.decision_path, rebind)
    else:
        ledger = PortfolioLedger(value.state / first / 'portfolio.db', first, Decimal('5000'))
        try:
            if fault == 'missing_committee':
                ledger._connection.execute('DELETE FROM committee_decisions')
                ledger._connection.commit()
            elif fault == 'forged_committee_scope':
                forged = deepcopy(value.committees[first])
                forged['status']['research_enrichment_scope']['original_sha256'] = 'f' * 64
                ledger._connection.execute('UPDATE committee_decisions SET payload_json=?', (json.dumps(forged),))
                ledger._connection.commit()
                value.committees[first] = forged
            else:
                ledger._connection.execute('DELETE FROM policy_session_contexts')
                ledger._connection.commit()
                ledger.bind_policy_session_context(SESSION, epoch_id=value.report['epoch_id'],
                    policy_version='test', policy_config={}, context={'decision_clock': {'forged': True}}, bound_at=NOW)
        finally:
            ledger.close()
    with pytest.raises((ValueError, OSError, KeyError)):
        reports._accepted_enrichment_scope(value.state, value.profile, value.report,
            value.source, value.committees)


def test_matching_forged_committee_bits_cannot_hide_accepted_profile_failure(accepted_enrichment):
    value = accepted_enrichment
    def retain_failure(payload):
        payload['enrichment']['errors'] = {'profiles': {'BAD': {'reason_code': 'timeout'}}}
        digest = clock._decision_digest(SourceInputStore(value.state / 'cache'), payload['enrichment'])
        payload['research_binding']['enrichment_digest'] = digest
        if payload['context'] is not None:
            payload['context']['enrichment_digest'] = digest
    _rewrite(value.decision_path, retain_failure)
    accepted = SourceInputStore.decode(value.decision_path.read_text())['payload']
    _, actual_scope = project_enrichment(accepted['enrichment'], capability_config=value.profile.config['autoresearch'])
    assert actual_scope['supported_enrichment_complete'] is False
    forged = {**actual_scope, 'supported_enrichment_complete': True, 'excluded': []}
    for name in value.profile.roster:
        ledger = PortfolioLedger(value.state / name / 'portfolio.db', name, Decimal('5000'))
        try:
            value.committees[name]['status']['research_enrichment_scope'] = deepcopy(forged)
            ledger._connection.execute('UPDATE committee_decisions SET payload_json=?',
                (json.dumps(value.committees[name]),))
            ledger._connection.commit()
            ledger._connection.execute('DELETE FROM policy_session_contexts')
            ledger._connection.commit()
            ledger.bind_policy_session_context(SESSION, epoch_id=value.report['epoch_id'],
                policy_version='test', policy_config={},
                context={'decision_clock': accepted['context']} if accepted['context'] is not None else {}, bound_at=NOW)
        finally:
            ledger.close()
    with pytest.raises(ValueError, match='enrichment differs from durable committee scope'):
        reports._accepted_enrichment_scope(value.state, value.profile, value.report,
            value.source, value.committees)


def _bind_candidate(value):
    store = MetricStore.open_existing(value.state / 'metrics_v2.sqlite3')
    record = CandidateSignalIdentityBinding('candidate-binding', value.report['epoch_id'], SESSION,
        ({'horizon': '30d', 'ticker': 'AAA', 'event_key': 'event-aaa', 'strategy': 'earnings_call'},))
    # Construct a second independent accepted fixture, rather than invoking a
    # production mutation from the report reader.
    import sqlite3
    with sqlite3.connect(store.path) as connection:
        connection.execute('UPDATE candidate_signal_identity_bindings SET payload_json=?', (store._json(record),))


def test_resolved_candidates_do_not_require_failed_subset_eligibility(accepted_enrichment):
    value = accepted_enrichment
    _bind_candidate(value)
    assert reports._accepted_enrichment_scope(value.state, value.profile, value.report,
        value.source, value.committees) == value.scope


def test_deleting_bound_eligibility_cannot_be_reinterpreted_as_no_failure(accepted_enrichment):
    value = accepted_enrichment
    _bind_candidate(value)
    eligibility = {'policy': 'reference_session_activity_v1', 'session': SESSION.isoformat(),
        'protected_tickers': ['SPY'], 'candidate_tickers': ['AAA'], 'listing_snapshot': None,
        'evidence': None, 'daily_attempts': {}, 'daily_evidence': {},
        'decisions': {'AAA': {'status': 'unresolved', 'reason': 'required_reference_price_unresolved',
            'daily_failure_reason': None}}}
    store = SourceInputStore(value.state / 'cache', accepted_dir=value.state / 'source_inputs/new_entry_eligibility')
    store.freeze({**value.source['identity'], 'policy': 'reference_session_activity_v1'}, eligibility, acquired_at=NOW)
    digest = clock._decision_digest(store, eligibility)
    def rebind(payload):
        payload['research_binding']['eligibility_digest'] = digest
        if payload['context'] is not None:
            payload['context']['eligibility_digest'] = digest
    _rewrite(value.decision_path, rebind)
    context = SourceInputStore.decode(value.decision_path.read_text())['payload']['context']
    for name in value.profile.roster:
        ledger = PortfolioLedger(value.state / name / 'portfolio.db', name, Decimal('5000'))
        try:
            ledger._connection.execute('DELETE FROM policy_session_contexts')
            ledger._connection.commit()
            ledger.bind_policy_session_context(SESSION, epoch_id=value.report['epoch_id'],
                policy_version='test', policy_config={},
                context={'decision_clock': context} if context is not None else {}, bound_at=NOW)
        finally:
            ledger.close()
    assert reports._accepted_enrichment_scope(value.state, value.profile, value.report,
        value.source, value.committees) == value.scope
    next((value.state / 'source_inputs/new_entry_eligibility').glob('*.json')).unlink()
    with pytest.raises(ValueError, match='research decision binding mismatch'):
        reports._accepted_enrichment_scope(value.state, value.profile, value.report,
            value.source, value.committees)
