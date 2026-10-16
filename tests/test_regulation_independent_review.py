"""Independent offline checks of regulatory retained-attempt authority."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from test_regulation_evidence import DOC, STAMP, fixture, build, module
from test_regulation_assessment import neutral
from test_regulation_failed_attempt import PROVENANCE, responder, assert_original
from test_regulation_integration import inputs
from test_regulation_replay_report import CONFIG, POLICIES, STRATEGIES
from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.orchestration.regulation_validation import validate_regulation_health
from tradingagents.strategies.modules.regulatory_pipeline import RegulatoryPipelineStrategy
from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded
from tradingagents.strategies.data_sources.regulation_assessment import MAX_RESPONSE_BYTES


def test_review_pdf_callback_claim_cannot_replace_independent_extraction():
    row, detail, bodies = fixture()
    raw = b'%PDF-1.7\nthis is not a PDF object graph\n%%EOF\n'
    text = 'No regulatory duties apply.'
    sha = lambda value: hashlib.sha256(value).hexdigest()
    bodies[DOC] = raw
    detail['data']['attributes']['fileFormats'] = [detail['data']['attributes']['fileFormats'][1]]
    detail['data']['attributes']['fileFormats'][0]['size'] = len(raw)
    proof = dict(version='sec-native-pdf-evidence-v1', encoding='pdf',
        input_sha256=sha(raw), input_bytes=len(raw), decoded_sha256=sha(raw), decoded_bytes=len(raw),
        native_text_complete=True, supported_text_layout=True, findings=[], support_limitations=[],
        library='pypdf', library_version='invented', page_count=1,
        pages=[dict(page_number=1, supported_text_layout=True, findings=[], support_limitations=[],
                    text=text, text_sha256=sha(text.encode()), text_chars=len(text))],
        text=text, text_sha256=sha(text.encode()), text_chars=len(text))
    with pytest.raises(ValueError):
        module().build_regulation_evidence(row, json.dumps(detail).encode(), bodies,
            observed_at=STAMP, pdf_results={DOC: proof})


@pytest.mark.parametrize('native', [False, True], ids=['injected-client', 'native-parent'])
def test_review_transport_returned_original_survives_inner_deadline(monkeypatch, native):
    from tradingagents.strategies import llm_utils
    analyzer, evidence = LLMAnalyzer(), build()
    response = json.dumps(neutral(evidence))
    if native:
        client = type('FixtureOpenAI', (), {'__module__': 'openai'})()
        client.api_key, client.base_url, client.default_headers = 'fixture', 'https://unused.invalid', {}
        client.organization = client.project = None
        monkeypatch.setattr(llm_utils, 'bounded_transport', lambda *a, **k:
            {'text': response, 'provenance': deepcopy(PROVENANCE)})
    else:
        client = object()
        def direct(*args, **kwargs):
            kwargs['provenance'].update(PROVENANCE)
            return response
        monkeypatch.setattr(llm_utils, '_call_model_direct', direct)
    monkeypatch.setattr(analyzer, '_get_client', lambda: client)
    calls = []
    def late(*args):
        calls.append(True)
        if len(calls) > 1:
            raise ModelDeadlineExceeded('model_deadline_exhausted')
        return 100
    monkeypatch.setattr(llm_utils, 'model_timeout', late)
    with pytest.raises(ModelDeadlineExceeded):
        analyzer.analyze_regulation_evidence(evidence)
    attempt = analyzer.last_regulation_attempt
    assert attempt['response_text'] == response
    assert attempt['response_bytes'] == len(response.encode())
    assert attempt['response_sha256'] == hashlib.sha256(response.encode()).hexdigest()
    assert attempt['failure_reason'] == 'model_deadline_exhausted'
    assert attempt['validation_status'] == 'failed'


@pytest.mark.parametrize('transport_error', [False, True])
def test_review_unavailable_transport_does_not_invent_empty_original(monkeypatch, transport_error):
    analyzer = LLMAnalyzer()
    if transport_error:
        from tradingagents.strategies import llm_utils
        monkeypatch.setattr(analyzer, '_get_client', lambda: object())
        def unavailable(*a, **k):
            raise RuntimeError('fixture transport unavailable')
        monkeypatch.setattr(llm_utils, '_call_model_direct', unavailable)
    else:
        monkeypatch.setattr(analyzer, '_get_client', lambda: None)
    with pytest.raises(ValueError):
        analyzer.analyze_regulation_evidence(build())
    attempt = analyzer.last_regulation_attempt
    assert attempt['failure_reason'] == ('analysis_unavailable' if transport_error else 'model_unavailable')
    assert attempt['response_received'] is False
    assert attempt['response_text'] is attempt['response_sha256'] is attempt['response_bytes'] is None


def test_review_actual_empty_arrival_is_retained_as_empty_original(monkeypatch):
    from tradingagents.strategies import llm_utils
    analyzer = LLMAnalyzer()
    monkeypatch.setattr(analyzer, '_get_client', lambda: object())
    monkeypatch.setattr(llm_utils, '_call_model_direct', lambda *a, **k: '')
    with pytest.raises(ValueError):
        analyzer.analyze_regulation_evidence(build())
    attempt = analyzer.last_regulation_attempt
    assert attempt['response_received'] is True and attempt['response_text'] == ''
    assert attempt['response_bytes'] == 0
    assert attempt['response_sha256'] == hashlib.sha256(b'').hexdigest()


@pytest.mark.parametrize('response', ['x' * (MAX_RESPONSE_BYTES + 1), 'invalid \ud800 original'],
    ids=['late-overlimit', 'late-surrogate'])
def test_review_late_invalid_original_remains_replayable_failure(monkeypatch, response):
    from tradingagents.strategies import llm_utils
    from tradingagents.strategies.orchestration.regulation_validation import _attempt
    analyzer, evidence = LLMAnalyzer(), build()
    monkeypatch.setattr(analyzer, '_get_client', lambda: object())
    monkeypatch.setattr(llm_utils, '_call_model_direct', lambda *a, **k: response)
    calls = []
    def late(*a):
        calls.append(True)
        if len(calls) > 1:
            raise ModelDeadlineExceeded('model_deadline_exhausted')
        return 100
    monkeypatch.setattr(llm_utils, 'model_timeout', late)
    with pytest.raises((ValueError, ModelDeadlineExceeded)):
        analyzer.analyze_regulation_evidence(evidence)
    attempt = analyzer.last_regulation_attempt
    raw = response.encode('utf-8', errors='surrogatepass')
    assert attempt['validation_status'] == 'failed'
    assert attempt['response_sha256'] == hashlib.sha256(raw).hexdigest()
    assert attempt['response_bytes'] == len(raw)
    assert _attempt({'model_attempt': attempt, 'model_provenance': analyzer.last_call_provenance},
                    evidence, None, {}) is None


def test_review_reused_response_survives_post_reuse_deadline(monkeypatch):
    from tradingagents.strategies import runtime_deadline, candidate_response_reuse
    analyzer, evidence = LLMAnalyzer(), build()
    response = json.dumps(neutral(evidence))
    monkeypatch.setattr(analyzer, '_get_client', lambda: object())
    monkeypatch.setattr(candidate_response_reuse, 'reused_candidate_response',
        lambda *a, **k: (response, deepcopy(PROVENANCE)))
    calls = []
    def late(*a):
        calls.append(True)
        if len(calls) == 4:
            raise ModelDeadlineExceeded('model_deadline_exhausted')
        return 100
    monkeypatch.setattr(runtime_deadline, 'model_timeout', late)
    with pytest.raises(ModelDeadlineExceeded):
        analyzer.analyze_regulation_evidence(evidence)
    assert_original(analyzer.last_regulation_attempt, response, evidence)
    assert analyzer.last_regulation_attempt['failure_reason'] == 'model_deadline_exhausted'


def failed_health(tmp_path, monkeypatch, response):
    _, data, assets = inputs(monkeypatch)
    data.update(yfinance={}, openbb={}, equity_universe={'snapshot': assets.evidence})
    engine = MultiStrategyEngine(config={'autoresearch': {**CONFIG, 'state_dir': str(tmp_path),
        'equity_universe_policy': 'us_exchange_listed_sip_v1'}}, strategies=[RegulatoryPipelineStrategy()])
    monkeypatch.setattr(engine, '_build_regime_model', lambda _: {})
    engine._analyzer = LLMAnalyzer()
    responder(engine._analyzer, monkeypatch, response)
    from tradingagents.strategies import candidate_response_reuse
    monkeypatch.setattr(candidate_response_reuse, 'commit_candidate_response',
        lambda: pytest.fail('invalid response committed for reuse'))
    signals, _, records = engine.screen_and_enrich('2026-10-09', data, epoch_id='epoch', policy_id='first')
    assert signals == [] and records[0].status == 'data_failure'
    return data, [{**deepcopy(vars(records[0])), 'policy_id': value} for value in sorted(POLICIES)]


@pytest.mark.parametrize('response', ['{not-json', '{"direction":"neutral"}',
    'é' * (MAX_RESPONSE_BYTES // 2 + 1), 'invalid \ud800 original'],
    ids=['json', 'schema', 'overlimit', 'surrogate'])
def test_review_failed_originals_replay_all_horizons_without_calls(tmp_path, monkeypatch, response):
    data, records = failed_health(tmp_path, monkeypatch, response)
    before = deepcopy((data, records))
    from test_regulation_source_evidence import source
    monkeypatch.setattr(LLMAnalyzer, '_call_llm', lambda *a, **k: pytest.fail('new model call'))
    monkeypatch.setattr(source, 'provider_request', lambda *a, **k: pytest.fail('new source call'))
    result = validate_regulation_health(data, records, POLICIES, STRATEGIES, CONFIG)
    assert result['acquisition_complete'] is True and result['analysis_complete'] is False
    assert len(result['horizons']) == 4
    assert all(row['failed_documents'] == 1 and row['assessed_documents'] == 0 for row in result['horizons'])
    assert (data, records) == before


@pytest.mark.parametrize('damage', ['missing', 'text', 'bytes', 'hash', 'request', 'provenance', 'healthy'])
def test_review_failed_original_receipt_tampering_is_rejected(tmp_path, monkeypatch, damage):
    data, records = failed_health(tmp_path, monkeypatch, '{not-json')
    row = records[0]['evidence']['regulation_assessments'][0]
    attempt = row['model_attempt']
    if damage == 'missing': row.pop('model_attempt')
    elif damage == 'text': attempt['response_text'] = '{}'
    elif damage == 'bytes': attempt['response_bytes'] += 1
    elif damage == 'hash': attempt['response_sha256'] = 'a' * 64
    elif damage == 'request': attempt['source_provenance']['request_sha256'] = 'a' * 64
    elif damage == 'provenance': attempt['model_provenance']['response_id'] = 'other'
    else: records[0]['status'] = 'signals'
    with pytest.raises(ValueError, match='invalid_regulation_health'):
        validate_regulation_health(data, records, POLICIES, STRATEGIES, CONFIG)


def test_review_parent_deadline_preserves_validated_attempt_without_commit(monkeypatch):
    from tradingagents.strategies import runtime_deadline, candidate_response_reuse
    candidate, data, assets = inputs(monkeypatch)
    analyzer = LLMAnalyzer()
    evidence = data['regulations']['regulation_evidence']['corpus'][DOC]
    response = json.dumps(neutral(evidence))
    responder(analyzer, monkeypatch, response)
    calls = []
    def late(*args):
        calls.append(True)
        if len(calls) == 6:
            raise ModelDeadlineExceeded('model_deadline_exhausted')
        return 100
    monkeypatch.setattr(runtime_deadline, 'model_timeout', late)
    monkeypatch.setattr(candidate_response_reuse, 'commit_candidate_response',
        lambda: pytest.fail('late assessment committed for reuse'))
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.ar_config = CONFIG
    engine.registry = SimpleNamespace(get=lambda _: None)
    engine._analyze_candidate(candidate, 'regulatory_pipeline', None, analyzer, filing_data=data, universe=assets)
    assert candidate.metadata['analysis_status'] == 'failed' and candidate.journal_only is True
    assert candidate.metadata['analysis_failure_reason'] == 'model_deadline_exhausted'
    assert_original(candidate.metadata['regulation_model_attempt'], response, evidence)


@pytest.mark.parametrize('invent_assessment', [False, True])
def test_review_commodity_context_cannot_substitute_for_pipeline_assessment(tmp_path, monkeypatch, invent_assessment):
    data, records = failed_health(tmp_path, monkeypatch, '{not-json')
    strategies = {**STRATEGIES, 'commodity_macro': ['regulations', 'cot']}
    for pipeline in list(records):
        context = deepcopy(pipeline)
        context.update(strategy='commodity_macro', status='no_candidates')
        context['evidence']['data_sources'] = sorted(strategies['commodity_macro'])
        if not invent_assessment:
            context['evidence']['regulation_assessments'] = []
        records.append(context)
    if invent_assessment:
        with pytest.raises(ValueError, match='invalid_regulation_health'):
            validate_regulation_health(data, records, POLICIES, strategies, CONFIG)
    else:
        result = validate_regulation_health(data, records, POLICIES, strategies, CONFIG)
        assert len(result['source_context_consumers']) == 4
        assert len(result['horizons']) == 4
        assert result['analysis_complete'] is False
