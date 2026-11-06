"""Failed strict regulation assessments retain bounded original attempt evidence."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from test_regulation_evidence import build, DOC
from test_regulation_assessment import neutral
from test_regulation_integration import inputs
from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.modules.regulatory_pipeline import RegulatoryPipelineStrategy
from tradingagents.strategies.data_sources.regulation_assessment import MAX_RESPONSE_BYTES, prepare_request
from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded

PROVENANCE = {'configured_model': 'fixture-model', 'returned_model': 'fixture-model',
              'response_id': 'fixture-response', 'identity_status': 'exact'}


def responder(analyzer, monkeypatch, response):
    def call(*args, **kwargs):
        analyzer.last_call_provenance = deepcopy(PROVENANCE)
        return response
    monkeypatch.setattr(analyzer, '_call_llm', call)


def assert_original(attempt, response, evidence):
    assert attempt['version'] == 'regulation-model-attempt-v1'
    assert attempt['source_provenance'] == json.loads(prepare_request(evidence,
        regime_context=attempt['source_provenance']['regime_context']).context.provenance_json)
    assert attempt['model_provenance'] == PROVENANCE
    assert attempt['model_call_started'] is True and attempt['response_received'] is True
    assert attempt['response_encoding'] == 'utf-8'
    assert attempt['response_bytes'] == len(response.encode())
    assert attempt['response_sha256'] == hashlib.sha256(response.encode()).hexdigest()
    assert attempt['response_over_limit'] is (len(response.encode()) > MAX_RESPONSE_BYTES)
    assert attempt['response_text'] == (response if len(response.encode()) <= MAX_RESPONSE_BYTES else None)


@pytest.mark.parametrize('response', ['{not-json', '{"direction":"neutral"}', 'é' * (MAX_RESPONSE_BYTES // 2 + 1)],
                         ids=['invalid-json', 'invalid-schema', 'oversized-utf8'])
def test_invalid_original_and_model_identity_survive_validation_failure(monkeypatch, response):
    analyzer, evidence = LLMAnalyzer(), build()
    responder(analyzer, monkeypatch, response)
    expected = 'invalid_regulation_response_limit' if len(response.encode()) > MAX_RESPONSE_BYTES else 'invalid_regulation_response'
    with pytest.raises(ValueError, match=expected):
        analyzer.analyze_regulation_evidence(evidence)
    attempt = analyzer.last_regulation_attempt
    assert_original(attempt, response, evidence)
    assert attempt['validation_status'] == 'failed' and attempt['failure_reason'] == expected


def test_post_response_deadline_retains_full_original_before_failure(monkeypatch):
    from tradingagents.strategies import runtime_deadline
    analyzer, evidence = LLMAnalyzer(), build()
    response = json.dumps(neutral(evidence))
    responder(analyzer, monkeypatch, response)
    calls = []
    def timeout(*args):
        calls.append(True)
        if len(calls) == 3:
            raise ModelDeadlineExceeded('model_deadline_exhausted')
        return 100
    monkeypatch.setattr(runtime_deadline, 'model_timeout', timeout)
    with pytest.raises(ModelDeadlineExceeded):
        analyzer.analyze_regulation_evidence(evidence)
    assert_original(analyzer.last_regulation_attempt, response, evidence)
    assert analyzer.last_regulation_attempt['failure_reason'] == 'model_deadline_exhausted'
    assert analyzer.last_regulation_attempt['validation_status'] == 'failed'


def test_transport_exception_retains_provenance_without_invented_response(monkeypatch):
    analyzer, evidence = LLMAnalyzer(), build()
    def failed(*args, **kwargs):
        analyzer.last_call_provenance = deepcopy(PROVENANCE)
        raise RuntimeError('private transport message must not become failure reason')
    monkeypatch.setattr(analyzer, '_call_llm', failed)
    with pytest.raises(RuntimeError):
        analyzer.analyze_regulation_evidence(evidence)
    attempt = analyzer.last_regulation_attempt
    assert attempt['model_provenance'] == PROVENANCE
    assert attempt['failure_reason'] == 'analysis_unavailable'
    assert attempt['response_received'] is False
    assert attempt['response_encoding'] is None
    assert attempt['response_text'] is attempt['response_bytes'] is attempt['response_sha256'] is None


def test_native_health_keeps_failed_original_and_never_commits_reuse(tmp_path, monkeypatch):
    _, data, assets = inputs(monkeypatch)
    data.update(yfinance={}, openbb={}, equity_universe={'snapshot': assets.evidence})
    engine = MultiStrategyEngine(config={'autoresearch': {'state_dir': str(tmp_path),
        'regulation_evidence_policy': 'complete_document_text_v1',
        'equity_universe_policy': 'us_exchange_listed_sip_v1'}}, strategies=[RegulatoryPipelineStrategy()])
    monkeypatch.setattr(engine, '_build_regime_model', lambda _: {})
    engine._analyzer = LLMAnalyzer()
    responder(engine._analyzer, monkeypatch, '{not-json')
    from tradingagents.strategies import candidate_response_reuse
    monkeypatch.setattr(candidate_response_reuse, 'commit_candidate_response', lambda: pytest.fail('invalid response reuse commit'))
    signals, _, records = engine.screen_and_enrich('2026-10-09', data, epoch_id='epoch', policy_id='policy')
    assert signals == [] and records[0].status == 'data_failure'
    row = records[0].evidence['regulation_assessments'][0]
    assert row['analysis_status'] == 'failed' and row['journal_only'] is True
    assert row['model_provenance'] == PROVENANCE
    assert_original(row['model_attempt'], '{not-json', data['regulations']['regulation_evidence']['corpus'][DOC])
    assert row['model_attempt']['source_provenance']['evidence_sha256'] == row['evidence_sha256']


def test_source_rejection_does_not_copy_previous_analyzer_attempt(monkeypatch):
    candidate, data, assets = inputs(monkeypatch)
    analyzer = LLMAnalyzer()
    analyzer.last_regulation_attempt = {'stale': True}
    analyzer.last_call_provenance = deepcopy(PROVENANCE)
    data['regulations'].pop('regulation_evidence')
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.ar_config = {'regulation_evidence_policy': 'complete_document_text_v1'}
    engine.registry = SimpleNamespace(get=lambda _: None)
    engine._analyze_candidate(candidate, 'regulatory_pipeline', None, analyzer, filing_data=data, universe=assets)
    assert candidate.metadata['analysis_status'] == 'failed'
    assert candidate.metadata.get('regulation_model_attempt') is None
    assert candidate.metadata.get('model_provenance') is None


def test_non_utf8_scalar_is_retained_explicitly_as_invalid_encoding(monkeypatch):
    analyzer, evidence = LLMAnalyzer(), build()
    response = 'invalid \ud800 original'
    responder(analyzer, monkeypatch, response)
    with pytest.raises(ValueError, match='invalid_regulation_response_encoding'):
        analyzer.analyze_regulation_evidence(evidence)
    attempt = analyzer.last_regulation_attempt
    assert attempt['response_text'] == response and attempt['response_received'] is True
    assert attempt['response_encoding'] == 'utf-8-surrogatepass'
    assert attempt['response_bytes'] == len(response.encode('utf-8', errors='surrogatepass'))
    assert attempt['response_sha256'] == hashlib.sha256(response.encode('utf-8', errors='surrogatepass')).hexdigest()
    assert json.loads(json.dumps(attempt)) == attempt


def test_before_request_deadline_resets_previous_attempt(monkeypatch):
    from tradingagents.strategies import runtime_deadline
    analyzer, evidence = LLMAnalyzer(), build()
    analyzer.last_regulation_attempt = {'stale': True}
    analyzer.last_call_provenance = deepcopy(PROVENANCE)
    def expired(*args):
        raise ModelDeadlineExceeded('model_deadline_exhausted')
    monkeypatch.setattr(runtime_deadline, 'model_timeout', expired)
    with pytest.raises(ModelDeadlineExceeded):
        analyzer.analyze_regulation_evidence(evidence)
    attempt = analyzer.last_regulation_attempt
    assert attempt['source_provenance'] is None and attempt['model_call_started'] is False
    assert attempt['model_provenance'] == {} and attempt['response_received'] is False
    assert attempt['failure_reason'] == 'model_deadline_exhausted'


def test_exact_byte_limit_preserves_entire_response(monkeypatch):
    analyzer, evidence = LLMAnalyzer(), build()
    response = 'é' * (MAX_RESPONSE_BYTES // 2)
    responder(analyzer, monkeypatch, response)
    with pytest.raises(ValueError, match='invalid_regulation_response$'):
        analyzer.analyze_regulation_evidence(evidence)
    assert_original(analyzer.last_regulation_attempt, response, evidence)


def test_successful_reuse_receipt_keeps_attempt_and_health_provenance_equal(monkeypatch):
    from tradingagents.strategies import candidate_response_reuse
    candidate, data, assets = inputs(monkeypatch)
    analyzer = LLMAnalyzer()
    responder(analyzer, monkeypatch, json.dumps(neutral(data['regulations']['regulation_evidence']['corpus'][DOC])))
    reuse = {'reused': False, 'scope': 'horizon_screening', 'request_digest': 'a' * 64}
    monkeypatch.setattr(candidate_response_reuse, 'commit_candidate_response', lambda: deepcopy(reuse))
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.ar_config = {'regulation_evidence_policy': 'complete_document_text_v1'}
    engine.registry = SimpleNamespace(get=lambda _: None)
    engine._analyze_candidate(candidate, 'regulatory_pipeline', None, analyzer, filing_data=data, universe=assets)
    assert candidate.metadata['analysis_status'] == 'validated'
    assert candidate.metadata['model_provenance']['request_reuse'] == reuse
    assert candidate.metadata['regulation_model_attempt']['model_provenance'] == candidate.metadata['model_provenance']
