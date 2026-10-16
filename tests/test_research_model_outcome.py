"""Real candidate validation binds observed requests/responses without new calls."""
from copy import deepcopy
import hashlib
import json
import time

import pytest
from openai import OpenAI

from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.data_sources.registry import DataSourceRegistry
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.orchestration.research_model_attempt import validate_request_receipt
from tradingagents.strategies.runtime_deadline import model_budget


VALID = '{"direction":"long","conviction":0.8,"rationale":"Retained catalyst €"}'


@pytest.fixture
def worker(tmp_path, monkeypatch):
    calls, responses = [], []

    def send(payload, timeout):
        calls.append(deepcopy(payload['request']))
        response = responses.pop(0) if responses else VALID
        if isinstance(response, Exception):
            raise response
        return {'text': response, 'provenance': {
            'configured_model': payload['request']['model'], 'returned_model': payload['request']['model'],
            'returned_revision': None, 'identity_status': 'unpinned', 'response_id': 'offline-response',
            'reasoning_effort': payload['request']['effort']}}

    monkeypatch.setattr('tradingagents.strategies.llm_utils.bounded_transport', send)
    cfg = {'autoresearch': {'state_dir': str(tmp_path), 'autoresearch_model': 'gpt-6-astra',
            'thesis_model': 'gpt-6-astra', 'llm_effort': 'high', 'thesis_effort': 'high',
            'research_resilience_policy': 'dependency_isolation_v1'}}
    engine = MultiStrategyEngine(config=cfg, registry=DataSourceRegistry(), use_llm=True)
    with OpenAI(api_key='offline-private', base_url='https://offline.invalid/v1') as client:
        engine._analyzer._client = client
        yield engine, calls, responses


def candidate(**metadata):
    return Candidate(ticker='CFR', date='2026-10-12', metadata={
        'needs_llm_analysis': True, 'analysis_type': 'filing_change', 'current_text': 'retained source',
        'discovery_id': 'filing-discovery', **metadata})


def enrich(worker, value=None):
    return worker._enrich_with_llm([value or candidate()], 'filing_analysis')[0]


def assert_outcome(value, request=None, response=None, *, status='validated', reason=None):
    outcome = value.metadata['research_model_outcome']
    assert set(outcome) == {'version', 'model_request', 'response_received', 'response_sha256',
        'response_bytes', 'response_encoding', 'analysis_status', 'analysis_failure_reason', 'model_provenance'}
    assert outcome['version'] == 'research-model-outcome-v1'
    assert outcome['analysis_status'] == status and outcome['analysis_failure_reason'] == reason
    if request is None:
        assert outcome['model_request'] is None
    else:
        validate_request_receipt(outcome['model_request'], role='thesis',
            **{key: request[key] for key in ('model', 'effort', 'max_tokens', 'temperature', 'system', 'prompt')})
    assert outcome['response_received'] is (response is not None)
    if response is None:
        assert outcome['response_sha256'] is None and outcome['response_bytes'] is None
        assert outcome['response_encoding'] is None
    else:
        raw = response.encode('utf-8', errors='surrogatepass')
        assert outcome['response_sha256'] == hashlib.sha256(raw).hexdigest()
        assert outcome['response_bytes'] == len(raw)
        assert outcome['response_encoding'] == ('utf-8-surrogatepass' if '\ud800' in response else 'utf-8')
    assert 'offline-private' not in json.dumps(outcome)
    assert 'response_text' not in outcome and 'prompt' not in outcome
    return outcome


@pytest.mark.parametrize('response,reason', [(VALID, None), (' \n' + VALID + '\n ', None),
    ('', 'analysis_unavailable'), ('not-json', 'analysis_unavailable'),
    ('{"direction":"wrong","conviction":0.8,"rationale":"bad"}', 'invalid_direction'),
    ('{"direction":"long","conviction":0.8,"rationale":"\ud800"}', 'invalid_response_encoding'),
    (RuntimeError('offline failure'), 'analysis_unavailable')])
def test_actual_engine_retains_valid_invalid_and_failed_attempts(worker, response, reason):
    engine, calls, responses = worker
    responses.append(response)
    result = enrich(engine)
    assert len(calls) == 1
    outcome = assert_outcome(result, calls[0], None if isinstance(response, Exception) else response,
        status='validated' if reason is None else 'failed', reason=reason)
    assert result.journal_only is (reason is not None)
    if reason is None:
        assert outcome['model_provenance'] == result.metadata['model_provenance']
    assert outcome['model_provenance']['configured_model'] == 'gpt-6-astra'


def test_pre_call_source_failure_does_not_inherit_previous_request_response_or_provenance(worker):
    engine, calls, _ = worker
    good = enrich(engine)
    assert_outcome(good, calls[0], VALID)
    bad = enrich(engine, candidate(current_text=''))
    outcome = assert_outcome(bad, status='failed', reason='missing_source_text')
    assert len(calls) == 1 and outcome['model_provenance'] == {}
    assert engine._analyzer.last_call_failure == ''
    assert engine._analyzer.last_call_response == {'received': False, 'text': None}
    assert good.metadata['research_model_outcome']['model_request'] is not None


def test_unavailable_model_keeps_logical_request_but_no_received_response(worker, monkeypatch):
    engine, calls, _ = worker
    monkeypatch.setattr(engine._analyzer, '_get_client', lambda: None)
    result = enrich(engine)
    outcome = result.metadata['research_model_outcome']
    assert outcome['model_request'] is not None and len(calls) == 0
    assert outcome['analysis_status'] == 'failed' and outcome['analysis_failure_reason'] == 'analysis_unavailable'
    assert outcome['response_received'] is False and outcome['response_sha256'] is None


def test_pre_call_complete_filing_input_refusal_has_no_model_authority(worker):
    engine, calls, _ = worker
    result = enrich(engine, candidate(full_filing_evidence_policy='complete_submission_v1'))
    assert result.journal_only and len(calls) == 0
    assert_outcome(result, status='failed', reason=result.metadata['analysis_failure_reason'])


def test_pre_call_deadline_reset_does_not_inherit_successful_attempt(worker):
    engine, calls, _ = worker
    enrich(engine)
    result = candidate(discovery_id='second')
    with model_budget(time.monotonic() - 1):
        engine._analyze_candidate(result, 'filing_analysis', {}, engine._analyzer)
    assert len(calls) == 1
    outcome = assert_outcome(result, status='failed', reason='model_deadline_exhausted')
    assert outcome['model_provenance'] == {}


def test_validated_reuse_rebinds_response_without_an_additional_physical_call(worker):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    engine, calls, _ = worker
    with candidate_response_memo():
        first = enrich(engine)
        second = enrich(engine)
    assert len(calls) == 1
    original = assert_outcome(first, calls[0], VALID)
    reused = assert_outcome(second, calls[0], VALID)
    assert original['model_request'] == reused['model_request']
    assert original['model_provenance']['request_reuse']['reused'] is False
    assert reused['model_provenance']['request_reuse']['reused'] is True
    engine._analyzer.last_call_provenance['response_id'] = 'mutated'
    assert reused['model_provenance']['response_id'] == 'offline-response'


def test_later_timeout_holds_entire_sample_without_rewriting_earlier_assessment(worker, monkeypatch):
    engine, calls, _ = worker
    clock = [100.0]
    monkeypatch.setattr('tradingagents.strategies.runtime_deadline.time.monotonic', lambda: clock[0])
    analyze = engine._analyzer.analyze_filing_change

    def second_expires(*args, **kwargs):
        if len(calls) == 1:
            clock[0] = 111.0
        return analyze(*args, **kwargs)

    monkeypatch.setattr(engine._analyzer, 'analyze_filing_change', second_expires)
    with model_budget(110.0):
        result = engine._enrich_with_llm([candidate(), candidate(discovery_id='second')], 'filing_analysis')
    assert len(calls) == 1
    assert all(row.journal_only and row.metadata['analysis_failure_reason'] == 'model_deadline_exhausted' for row in result)
    assert_outcome(result[0], calls[0], VALID)
    assert result[1].metadata['research_model_outcome']['analysis_status'] == 'failed'
    assert result[1].metadata['research_model_outcome']['response_received'] is False


def test_absent_policy_preserves_legacy_candidate_metadata(worker):
    engine, calls, _ = worker
    engine.ar_config.pop('research_resilience_policy')
    result = enrich(engine)
    assert len(calls) == 1 and result.metadata['analysis_status'] == 'validated'
    assert 'research_model_outcome' not in result.metadata
