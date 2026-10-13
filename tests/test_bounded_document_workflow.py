"""Bounded document research uses ordinary candidate and durable book phases."""
from types import SimpleNamespace
from datetime import date, datetime, timezone
import base64
from copy import deepcopy
import json
from pathlib import Path
import pytest

from test_source_reliability_pipeline import (
    COMMIT, GENERATION, accepted_input_bytes, pipeline, run_campaign,
)
from test_bounded_document_research import evidence, recognition

from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine


def test_bounded_document_candidate_retains_scoped_analysis_and_never_gains_order_authority():
    engine = object.__new__(MultiStrategyEngine)
    engine.ar_config = {}
    evidence = {'document_id': 'retained-original'}
    seen = []

    def analyze(value, *, session_as_of, regime_context):
        seen.append((value, session_as_of, regime_context))
        return {'direction': 'neutral', 'conviction': 0.0,
                'rationale': 'Selected historical page has no current directional thesis.',
                'journal_only': True, 'trading_authority': False,
                'assessment_scope': 'selected_pages',
                'bounded_document_research': {'receipt_sha256': 'a' * 64}}

    analyzer = SimpleNamespace(analyze_bounded_document_evidence=analyze)
    candidate = Candidate(ticker='CDRE', date='2026-10-12', journal_only=False,
        metadata={'analysis_type': 'bounded_document_research',
                  'bounded_document_evidence': evidence})
    engine._analyze_candidate(candidate, 'bounded_document_research', {}, analyzer)
    assert seen == [(evidence, '2026-10-12', {})]
    assert candidate.metadata['analysis_status'] == 'validated'
    assert candidate.metadata['llm_analysis']['bounded_document_research']['receipt_sha256'] == 'a' * 64
    assert candidate.journal_only
    assert 'bounded_document_evidence' not in candidate.metadata
    assert candidate.metadata['bounded_document_source']['document_id'] == 'retained-original'


@pytest.fixture
def document_pipeline(pipeline, monkeypatch, request):
    fixture, previous, config, repo = pipeline
    retained = getattr(request, 'param', None)
    if retained is not None:
        from tradingagents.strategies.orchestration.bounded_document_inputs import load_retained_documents
        entry = deepcopy(retained['registry_entry'])
        source = load_retained_documents({'bounded_document_research_policy':'bounded_document_research_v1',
            'bounded_document_registry':[entry]}, '2026-10-12')
        assert source['coverage']['complete']
        value = source['bounded_documents'][0]['evidence']
    else:
        value = evidence(image=True)
        value['residual_failures'].append('complete_document_unassessed')
        entry = {'ticker': 'AAPL', **{key: deepcopy(item) for key, item in value.items() if key != 'policy'}}
        for index, item in enumerate([entry['original'], *entry['attachments']]):
            path = repo / (str(index) + ('.pdf' if item['media_type'] == 'application/pdf' else '.png'))
            path.write_bytes(base64.b64decode(item.pop('data_base64')))
            item['path'] = str(path)
    config['autoresearch'].update(bounded_document_research_policy=value['policy'],
        bounded_document_registry=[entry], research_resilience_policy='dependency_isolation_v1',
        filing_research_policy='disabled_v1', llm_billing_mode='chatgpt_plan')
    from tradingagents.strategies import chatgpt_plan_client, llm_utils
    monkeypatch.setattr(chatgpt_plan_client, '_profile_identity', lambda path: 'offline-profile')
    from tradingagents.strategies.bounded_document_research import prepare_recognition
    prepared = prepare_recognition(value, session_as_of='2026-10-12')
    fixture.recognition_fault = None
    original_model = fixture.model

    def model(client, *, system, prompt, **kwargs):
        if kwargs.get('document_inputs') is not None:
            fixture.model_calls['document_recognition'] += 1
            if retained is not None:
                import hashlib
                assert hashlib.sha256(system.encode()).hexdigest() == retained['system_sha256']
                assert hashlib.sha256(prompt.encode()).hexdigest() == retained['user_sha256']
                return retained['recognition_text']
            response = recognition(prepared)
            if fixture.recognition_fault == 'uncertain':
                response['pages'][0].update(status='uncertain', uncertainties=['Unclear source cell'])
            if fixture.recognition_fault == 'malformed':
                return '{'
            return json.dumps(response)
        if 'Immutable scoped recognition receipt:' in prompt:
            fixture.model_calls['document_thesis'] += 1
            return json.dumps({'assessment_scope':'selected_pages','direction':'neutral','score':0,
                'rationale':'Selected historical page warrants abstention.',
                'evidence_claim':'The selected page prints Revenue in USD millions.'})
        return original_model(client, system=system, prompt=prompt, **kwargs)

    monkeypatch.setattr(llm_utils, 'call_analysis_model', model)
    for cohort in previous.cohorts:
        cohort['ledger'].close()
    from tradingagents.strategies.orchestration.cohort_orchestrator import CohortOrchestrator, build_default_cohorts
    owner = CohortOrchestrator(build_default_cohorts(config), config,
        generation_id=GENERATION, generation_commit=COMMIT)
    fixture.session = date(2026, 10, 12)
    fixture.now = datetime(2026, 10, 12, 22, tzinfo=timezone.utc)
    try:
        yield fixture, owner, config, repo
    finally:
        for cohort in owner.cohorts:
            cohort['ledger'].close()


def _screened_signals(config):
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    documents = [body for name, body in accepted_input_bytes(config).items() if name.startswith('screening/')]
    assert len(documents) == 1
    return [signal for horizon in SourceInputStore.decode(documents[0].decode())['payload']['horizons'].values()
            for signal in horizon['signals']]


def _document_signals(config):
    return [signal for signal in _screened_signals(config)
            if signal['strategy'] == 'bounded_document_research']


def test_document_workflow_resumes_existing_commit_boundary_and_replays_without_calls(document_pipeline):
    fixture, owner, config, _ = document_pipeline
    fixture.interrupt_committee_at = 5
    interrupted = fixture.manager.run_daily(str(fixture.session))[GENERATION]
    assert interrupted['outcome'] == 'failed'
    assert sum(c['ledger']._connection.execute('SELECT COUNT(*) FROM staging_runs').fetchone()[0]
               for c in owner.cohorts) == 4
    frozen = accepted_input_bytes(config)
    recognition_calls = fixture.model_calls['document_recognition']
    assert recognition_calls == 4 and fixture.model_calls['document_thesis'] >= 1
    fixture.interrupt_committee_at = None
    fixture.block_all = True
    result, wire, report = run_campaign(document_pipeline, fixture.session)
    assert result['outcome'] == 'degraded'  # Existing full-filing research stays disabled.
    assert report['accounting_valid'] and report['staging_complete'] and len(wire) == 16
    signals = _document_signals(config)
    assert len(signals) == 4 and all(signal['journal_only'] for signal in signals)
    assert all(signal['metadata']['analysis_status'] == 'validated' for signal in signals)
    assert all(signal['metadata']['llm_analysis']['trading_authority'] is False for signal in signals)
    assert fixture.model_calls['document_recognition'] == recognition_calls
    assert accepted_input_bytes(config) == frozen
    assert not fixture.blocked_calls
    before = {c['config'].name: tuple(c['ledger']._connection.iterdump()) for c in owner.cohorts}
    counts = fixture.model_calls.copy()
    run_campaign(document_pipeline, fixture.session)
    assert fixture.model_calls == counts and accepted_input_bytes(config) == frozen
    assert before == {c['config'].name: tuple(c['ledger']._connection.iterdump()) for c in owner.cohorts}
    document_ids = {signal['signal_id'] for row in wire.values() for signal in row['signals']
                    if signal['strategy'] == 'bounded_document_research'}
    assert len(document_ids) == 4
    assert all(not set(intent.signal_ids) & document_ids for c in owner.cohorts
               for intent in c['ledger'].pending_intents(date(2026, 10, 13)))


@pytest.mark.parametrize('fault', ['uncertain', 'malformed'])
def test_failed_recognition_never_reaches_thesis_or_orders_while_valid_work_continues(document_pipeline, fault):
    fixture, owner, config, _ = document_pipeline
    fixture.recognition_fault = fault
    result, wire, report = run_campaign(document_pipeline, fixture.session)
    assert result['outcome'] == 'degraded' and report['accounting_valid'] and report['staging_complete']
    signals = _document_signals(config)
    assert len(signals) == 4 and all(signal['journal_only'] for signal in signals)
    assert all(signal['metadata']['analysis_status'] == 'failed' for signal in signals)
    assert all('llm_analysis' not in signal['metadata'] for signal in signals)
    assert fixture.model_calls['document_recognition'] == 4 and fixture.model_calls['document_thesis'] == 0
    assert any(signal['strategy'] == 'earnings_call' and not signal['journal_only']
               for signal in _screened_signals(config))
    assert sum(len(c['ledger'].pending_intents(date(2026, 10, 13))) for c in owner.cohorts) > 0


def test_failed_document_candidate_is_excluded_without_affecting_next_valid_candidate():
    engine = object.__new__(MultiStrategyEngine)
    engine.ar_config = {}
    def analyze(value, **kwargs):
        raise ValueError('invalid_document_recognition')
    candidate = Candidate(ticker='CDRE', date='2026-10-12', metadata={
        'analysis_type': 'bounded_document_research', 'bounded_document_evidence': {}})
    engine._analyze_candidate(candidate, 'bounded_document_research', {},
        SimpleNamespace(analyze_bounded_document_evidence=analyze))
    assert candidate.journal_only
    assert candidate.metadata['analysis_status'] == 'failed'
    assert candidate.metadata['analysis_failure_reason'] == 'invalid_document_recognition'
    assert 'llm_analysis' not in candidate.metadata
    assert 'bounded_document_evidence' not in candidate.metadata


def test_pre_call_deadline_never_attributes_previous_document_attempt():
    import time
    from tradingagents.strategies.runtime_deadline import model_budget
    engine = object.__new__(MultiStrategyEngine)
    engine.ar_config = {}
    analyzer = SimpleNamespace(last_bounded_document_attempt={'source_identity': 'preceding-document'})
    candidate = Candidate(ticker='AAPL', date='2026-10-12', metadata={
        'analysis_type':'bounded_document_research','bounded_document_evidence':{}})
    with model_budget(time.monotonic() - 1):
        engine._analyze_candidate(candidate, 'bounded_document_research', {}, analyzer)
    assert candidate.metadata['analysis_status'] == 'failed'
    assert 'bounded_document_attempt' not in candidate.metadata


@pytest.mark.parametrize('hold', ['regime', 'dispatch'])
def test_skipped_document_analysis_preserves_identity_and_safe_sixteen_book_staging(document_pipeline, monkeypatch, hold):
    fixture, owner, config, _ = document_pipeline
    if hold == 'regime':
        from test_research_hold_staging import _hold_fault
        _hold_fault(fixture, monkeypatch, 'regime')
    else:
        from tradingagents.strategies import candidate_parallel
        def refused(*args, **kwargs):
            raise ValueError('invalid_candidate_workers')
        monkeypatch.setattr(candidate_parallel, 'analyze_candidates', refused)
    result, wire, report = run_campaign(document_pipeline, fixture.session)
    assert result['outcome'] == 'degraded' and report['accounting_valid'] and report['staging_complete']
    documents = _document_signals(config)
    assert len(documents) == 4
    assert all(signal['journal_only'] and 'bounded_document_evidence' not in signal['metadata'] for signal in documents)
    assert all(signal['metadata']['bounded_document_source']['original_sha256'] for signal in documents)
    assert fixture.model_calls['document_recognition'] == fixture.model_calls['document_thesis'] == 0
    assert all(not cohort['ledger'].pending_intents(date(2026, 10, 13)) for cohort in owner.cohorts)
