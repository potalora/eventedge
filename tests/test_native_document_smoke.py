"""Acceptance drill guards; fixtures are constructed and never native evidence."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest
from test_document_decision_eligibility import automatic_fixture


def harness():
    path = Path(__file__).resolve().parents[1] / 'scripts/run_native_document_smoke.py'
    assert path.exists(), 'native smoke harness is missing'
    spec = importlib.util.spec_from_file_location('native_document_smoke', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def registry(tmp_path):
    _, _, request, scope, _ = automatic_fixture()
    context = json.loads(request.context_json)
    source = tmp_path / 'source.pdf'
    source.write_bytes(request.document_inputs[0].data)
    item = {k: deepcopy(context[k]) for k in ('document_id','as_of','original_pages','selected_pages','probes','residual_failures')}
    item.update(ticker='ACME', decision_scope=scope)
    item['original'] = {k: context['original'][k] for k in ('media_type','filename','sha256')}
    item['original']['path'] = str(source)
    item['attachments'] = [{k:v for k,v in context['attachments'][0].items() if k != 'size_bytes'}]
    item['attachments'][0]['path'] = str(source)
    return [item]


def test_preflight_forces_supported_plan_models_and_disabled_paper_execution(tmp_path):
    module = harness()
    config, record = module.prepare_smoke(registry(tmp_path), '2026-10-12', '/private/auth.json')
    assert config['execution'] == {'mode':'paper', 'execution_enabled':False}
    ar = config['autoresearch']
    assert (ar['autoresearch_model'], ar['thesis_model'], ar['llm_effort'], ar['thesis_effort']) == (
        'gpt-6-luna', 'gpt-6.1-sol', 'high', 'high')
    assert ar['llm_billing_mode'] == 'chatgpt_plan'
    assert record['decision_scope']['required_claim_ids'] == ['count']


@pytest.mark.parametrize('fault', ['two_documents','review','gold','source_changed'])
def test_preflight_rejects_manual_gold_or_bad_sources_before_any_model_call(tmp_path, fault):
    module = harness()
    items = registry(tmp_path)
    if fault == 'two_documents': items *= 2
    elif fault == 'review': items[0]['decision_scope']['independent_review'] = {}
    elif fault == 'gold': items[0]['decision_scope']['expected_values'] = ['1,204.50']
    else: Path(items[0]['original']['path']).write_bytes(b'changed')
    with pytest.raises(ValueError):
        module.prepare_smoke(items, '2026-10-12', '/private/auth.json')


def test_smoke_receipt_requires_every_real_native_stage_and_eligible_automatic_review():
    module = harness()
    attempt = {'validation_status':'validated', 'automatic_review':{'version':'automatic_review_v1'},
        'decision_eligibility':{'eligible':True,'verification_basis':'automatic_review_v1'}}
    for stage, model, role in [('recognition_attempt','gpt-6-luna','bounded'),
            ('verification_attempt','gpt-6-luna','bounded'),('thesis_attempt','gpt-6.1-sol','thesis')]:
        attempt[stage] = {'model_call_started':True,'response_received':True,'validation_status':'validated',
            'model_provenance':{'configured_model':model,'returned_model':model,
                'reasoning_effort':'high','role':role,'billing_mode':'chatgpt_plan'}}
    result = {'analysis_status':'validated','direction':'neutral','score':0}
    assert module.summarize(attempt, result, None)['status'] == 'passed'
    for fault in ('missing_verifier','api','wrong_model','ineligible','failed_result'):
        damaged = deepcopy(attempt)
        output = result
        if fault == 'missing_verifier': damaged.pop('verification_attempt')
        elif fault == 'api': damaged['thesis_attempt']['model_provenance']['billing_mode'] = 'api'
        elif fault == 'wrong_model': damaged['verification_attempt']['model_provenance']['returned_model'] = 'other'
        elif fault == 'ineligible': damaged['decision_eligibility']['eligible'] = False
        else: output = None
        assert module.summarize(damaged, output, None)['status'] == 'failed'


def test_worker_retains_failed_blind_capture_and_stops_before_sol(tmp_path, monkeypatch):
    module = harness()
    items = registry(tmp_path)
    path = tmp_path / 'registry.json'
    path.write_text(json.dumps(items))
    output = tmp_path / 'run'
    from tradingagents.strategies import chatgpt_plan_client, llm_utils
    from tradingagents.strategies.bounded_document_research import prepare_recognition
    _, record = module.prepare_smoke(items, '2026-10-12', '/private/auth.json')
    request = prepare_recognition(record['evidence'], session_as_of='2026-10-12')
    _, wrapper, _, _, _ = automatic_fixture()
    raw = deepcopy(wrapper['recognition'])
    raw['request_identity_sha256'] = json.loads(request.context_json)['request_identity_sha256']
    monkeypatch.setattr(chatgpt_plan_client, '_profile_identity', lambda path: 'offline-native-boundary')
    calls = []

    def transport(client, *, model, system, prompt, provenance, response_observer, document_inputs, **kwargs):
        calls.append(model)
        assert model == 'gpt-6-luna' and document_inputs
        provenance.update(configured_model=model, returned_model=model, reasoning_effort='high',
            billing_mode='chatgpt_plan', identity_status='unpinned')
        from tradingagents.strategies.plan_document_inputs import document_input_metadata, encode_document_inputs
        provenance['document_inputs'] = document_input_metadata(encode_document_inputs(document_inputs))
        if 'Independent blind source verification' in system:
            assert '1,204.50' not in prompt
            response = {'recognition':raw, 'context_sufficient':False,
                'material_context_failures':['Material issuer context is absent.']}
        else:
            response = raw
        text = json.dumps(response)
        response_observer(text)
        return text

    monkeypatch.setattr(llm_utils, 'call_analysis_model', transport)
    import logging
    import os
    previous_logging = logging.root.manager.disable
    previous_umask = os.umask(0o027)
    logging.disable(logging.ERROR)
    try:
        assert module.main(['--registry',str(path),'--auth-path','/private/auth.json',
            '--as-of','2026-10-12','--output',str(output)]) == 1
        observed_umask = os.umask(0o027)
        assert (logging.root.manager.disable, observed_umask) == (logging.ERROR, 0o027)
    finally:
        logging.disable(previous_logging)
        os.umask(previous_umask)
    attempt = json.loads((output / 'analyzer-attempt.json').read_text())
    receipt = json.loads((output / 'smoke-receipt.json').read_text())
    assert calls == ['gpt-6-luna','gpt-6-luna']
    assert attempt['recognition_attempt']['validation_status'] == 'validated'
    assert attempt['verification_attempt']['response_received']
    assert attempt['thesis_attempt'] is None
    assert receipt['status'] == 'failed' and receipt['native_calls_attempted'] == 2
    assert receipt['sol_outcome'] is None and receipt['registry_unchanged']
    assert not (output / 'analysis-result.json').exists()


def test_preflight_rejects_date_cutoff_beyond_scope_before_native_calls(tmp_path):
    module = harness()
    items = registry(tmp_path)
    items[0]['decision_scope']['effective_until'] = '2026-10-13T03:59:59+00:00'
    with pytest.raises(ValueError, match='scope_window_excludes_session'):
        module.prepare_smoke(items, '2026-10-12', '/private/auth.json')
    _, record = module.prepare_smoke(items, '2026-10-12T23:00:00+00:00', '/private/auth.json')
    assert record['decision_scope']['economic_date'] == '2026-10-12'


def test_worker_restores_callers_logging_and_umask_on_preflight_exception(tmp_path):
    import logging
    import os
    module = harness()
    path = tmp_path / 'invalid-registry.json'
    path.write_text('[]')
    previous_logging = logging.root.manager.disable
    previous_umask = os.umask(0o027)
    logging.disable(logging.ERROR)
    try:
        with pytest.raises(ValueError, match='one_retained_document_required'):
            module.main(['--registry',str(path),'--auth-path','/private/auth.json',
                '--as-of','2026-10-12','--output',str(tmp_path / 'run')])
        observed_umask = os.umask(0o027)
        assert (logging.root.manager.disable, observed_umask) == (logging.ERROR, 0o027)
    finally:
        logging.disable(previous_logging)
        os.umask(previous_umask)


def test_worker_restores_callers_logging_and_umask_on_successful_return(tmp_path, monkeypatch):
    import logging
    import os
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    module = harness()
    path = tmp_path / 'registry.json'
    path.write_text(json.dumps(registry(tmp_path)))

    def constructed_analysis(analyzer, evidence, **kwargs):
        attempt = {'validation_status':'validated', 'automatic_review':{'version':'automatic_review_v1'},
            'decision_eligibility':{'eligible':True,'verification_basis':'automatic_review_v1'}}
        for stage, model, role in [('recognition_attempt','gpt-6-luna','bounded'),
                ('verification_attempt','gpt-6-luna','bounded'),('thesis_attempt','gpt-6.1-sol','thesis')]:
            attempt[stage] = {'model_call_started':True,'response_received':True,'validation_status':'validated',
                'model_provenance':{'configured_model':model,'returned_model':model,
                    'reasoning_effort':'high','role':role,'billing_mode':'chatgpt_plan'}}
        analyzer.last_bounded_document_attempt = attempt
        return {'analysis_status':'validated','direction':'neutral','score':0}

    monkeypatch.setattr(LLMAnalyzer, 'analyze_bounded_document_evidence', constructed_analysis)
    previous_logging = logging.root.manager.disable
    previous_umask = os.umask(0o027)
    logging.disable(logging.ERROR)
    try:
        assert module.main(['--registry',str(path),'--auth-path','/private/auth.json',
            '--as-of','2026-10-12','--output',str(tmp_path / 'run')]) == 0
        observed_umask = os.umask(0o027)
        assert (logging.root.manager.disable, observed_umask) == (logging.ERROR, 0o027)
    finally:
        logging.disable(previous_logging)
        os.umask(previous_umask)
