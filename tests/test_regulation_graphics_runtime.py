"""Constructed model transports exercise image gating; no native models or OCR."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingagents.strategies.data_sources import regulation_graphics as graphics
from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from test_regulation_graphics import assets as demo_assets, provenance, recognition_response
from test_regulation_evidence import build


def api():
    return importlib.import_module('tradingagents.strategies.learning.regulation_graphics_runtime')


def configuration():
    return {'execution':{'mode':'paper','execution_enabled':False}, 'autoresearch':{
        'autoresearch_model':'gpt-6-luna','llm_effort':'high', 'thesis_model':'gpt-6.1-sol',
        'thesis_effort':'high','llm_billing_mode':'chatgpt_plan','llm_temperature':0.0}}


def asset_population(count):
    rows = []
    import hashlib
    for i in range(count):
        row = deepcopy(demo_assets()[0]); row.update(id=f'DEMO.{i:03}',filename=f'DEMO.{i:03}.png')
        if i % 2:
            row['data'] = (Path(__file__).parent/'fixtures/regulation_graphics/EP05OC26.001.png').read_bytes()
            row['sha256'] = hashlib.sha256(row['data']).hexdigest()
        rows.append(row)
    return rows


@pytest.fixture
def harness(monkeypatch):
    from tradingagents.strategies.data_sources import regulation_graphics_inputs, regulation_assessment
    rows, evidence, calls, failures = asset_population(5), build(), [], {}
    monkeypatch.setattr(regulation_graphics_inputs,'graphics_assets',lambda _:deepcopy(rows))
    analyzer = LLMAnalyzer(configuration())

    def prepare(evidence, *, graphics_batches=(), **kwargs):
        materials = []
        prior_assets=prior_bundle=None
        for i,bundle in enumerate(graphics_batches):
            batch=rows[i*4:(i+1)*4]
            if prior_bundle is not None:
                batch=graphics.augment_batch_context(batch,prior_assets,prior_bundle)
            materials += graphics.replay_graphics_receipt(batch,bundle)
            prior_assets,prior_bundle=batch,bundle
        return SimpleNamespace(system='Sol assess every verified material',user=json.dumps(materials),
            context=SimpleNamespace(provenance_json=json.dumps({'all_graphics':len(materials)})))

    monkeypatch.setattr(regulation_assessment,'prepare_request',prepare)
    monkeypatch.setattr(regulation_assessment,'validate_assessment',lambda text,ctx:{'accepted':True})

    def call(system,user,*,role,max_tokens=4096,document_inputs=None):
        calls.append((role,system,user,document_inputs))
        analyzer.last_call_failure = ''
        if role == 'thesis':
            assert document_inputs is None
            analyzer.last_call_provenance = {'configured_model':'gpt-6.1-sol','returned_model':'gpt-6.1-sol'}
            text = '{"accepted":true}'
        else:
            payload = json.loads(user); context = payload['request']; stage=context['stage']
            request = SimpleNamespace(context_json=json.dumps(context),document_inputs=document_inputs)
            if stage=='recognition':
                raw=recognition_response(request)
                template=raw['graphics'][0]
                raw['graphics']=[dict(deepcopy(template),id=r['id'],sha256=r['sha256'])for r in context['assets']]
            else:
                raw={'version':graphics.REVIEW_VERSION,'request_identity_sha256':context['request_identity_sha256'],
                    'recognition_receipt_sha256':context['recognition_receipt_sha256'],
                    'graphics':[{'id':r['id'],'sha256':r['sha256'],'material_complete':True,
                        'checked_element_ids':[e['id']for e in recognized['elements']],
                        'checks':{name:True for name in graphics.REVIEW_CHECKS},'errors':[]}
                        for r,recognized in zip(context['assets'],payload['recognition']['graphics'])]}
            analyzer.last_call_provenance=provenance(request)
            fault=failures.get((len(calls),stage))
            if fault=='transport':
                raise RuntimeError('private transport detail')
            if fault=='partial':
                if stage=='recognition': raw['graphics'][0]['status']='partial'
                else: raw['graphics'][0]['material_complete']=False
            text = failures.get('raw',json.dumps(raw))
        analyzer.last_call_response={'received':True,'text':text}
        return text

    monkeypatch.setattr(analyzer,'_call_llm',call)
    return SimpleNamespace(rows=rows,evidence=evidence,calls=calls,failures=failures,analyzer=analyzer)


def test_all_mixed_images_have_recognition_and_original_image_review_before_sol(harness):
    h=harness
    assert h.analyzer.analyze_regulation_evidence(h.evidence)=={'accepted':True}
    assert [c[0]for c in h.calls]==['bounded']*4+['thesis']
    assert [row['id']for row in json.loads(h.calls[-1][2])]==[row['id']for row in h.rows]
    assert [[i.filename for i in c[3]]for c in h.calls[:4]]==[
        [r['filename']for r in h.rows[:4]]]*2+[[h.rows[4]['filename']]]*2
    for c in h.calls[:4]:
        for image in c[3]:
            expected=next(r for r in h.rows if r['filename']==image.filename)
            assert image.data==expected['data'] and image.sha256==expected['sha256']
    attempt=h.analyzer.last_regulation_attempt
    assert attempt['version']=='regulation-model-attempt-v2'
    assert len(attempt['graphics_batches'])==2 and attempt.get('graphics_failure') is None
    assert [r['id']for r in api().replay_graphics_attempt(h.evidence,attempt)]==[r['id']for r in h.rows]
    assert attempt['model_provenance']['configured_model']=='gpt-6.1-sol'
    context=attempt['graphics_batches'][1]['recognition']['request_identity']['assets'][0]['context']
    assert attempt['graphics_batches'][0]['review']['receipt_sha256'] in context


@pytest.mark.parametrize('number,stage',[(1,'recognition'),(2,'review'),(3,'recognition'),(4,'review')])
def test_any_graphic_failure_preserves_original_prefix_and_prevents_sol(harness,number,stage):
    h=harness;h.failures[(number,stage)]='partial'
    with pytest.raises(ValueError):h.analyzer.analyze_regulation_evidence(h.evidence)
    assert len(h.calls)==number and all(c[0]=='bounded'for c in h.calls)
    attempt=h.analyzer.last_regulation_attempt
    assert len(attempt['graphics_batches'])==(number-1)//2
    failure=attempt['graphics_failure']
    assert failure['stage']==stage and failure['response_text']
    assert ('validated_recognition'in failure)==(stage=='review')
    assert attempt['model_call_started'] is False and attempt['model_provenance']=={}
    assert h.analyzer.last_call_response is None and h.analyzer.last_call_provenance=={}
    assert h.analyzer.last_research_request is None
    assert api().replay_graphics_attempt(h.evidence,attempt) is None


@pytest.mark.parametrize('tamper',['prefix_order','missing_batch','extra_batch','failed_asset','failed_request','failed_response'])
def test_replay_rejects_wrong_prefix_binding_and_failed_original_tampering(harness,tamper):
    h=harness;h.failures[(4,'review')]='partial'
    with pytest.raises(ValueError):h.analyzer.analyze_regulation_evidence(h.evidence)
    attempt=deepcopy(h.analyzer.last_regulation_attempt)
    if tamper=='prefix_order':attempt['graphics_batches'][0]['recognition']['request_identity']['assets'].reverse()
    elif tamper=='missing_batch':attempt['graphics_batches']=[]
    elif tamper=='extra_batch':attempt['graphics_batches']*=2
    elif tamper=='failed_asset':attempt['graphics_failure']['asset_ids']=['other']
    elif tamper=='failed_request':attempt['graphics_failure']['model_request']['prompt_sha256']='0'*64
    else:attempt['graphics_failure']['response_text']+=' '
    with pytest.raises(ValueError):api().replay_graphics_attempt(h.evidence,attempt)


@pytest.mark.parametrize('fault',['billing','luna_effort','sol_model','live'])
def test_graphics_guards_fail_before_any_model(harness,fault):
    h=harness
    if fault=='billing':h.analyzer.config['autoresearch']['llm_billing_mode']='api'
    elif fault=='luna_effort':h.analyzer._effort='low'
    elif fault=='sol_model':h.analyzer._thesis_model='gpt-6-astra'
    else:h.analyzer.config['execution']['execution_enabled']=True
    with pytest.raises(ValueError):h.analyzer.analyze_regulation_evidence(h.evidence)
    assert h.calls==[]


@pytest.mark.parametrize('raw',['{not-json','\ud800', 'x'*(graphics.MAX_RESPONSE_BYTES+1)])
def test_invalid_original_bytes_remain_bounded_and_never_reach_sol(harness,raw):
    import hashlib
    h=harness;h.failures['raw']=raw
    with pytest.raises(ValueError):h.analyzer.analyze_regulation_evidence(h.evidence)
    value=h.analyzer.last_regulation_attempt['graphics_failure']
    encoded=raw.encode('utf-8',errors='surrogatepass')
    assert value['response_sha256']==hashlib.sha256(encoded).hexdigest()
    assert value['response_bytes']==len(encoded)
    assert value['response_text']==(raw if len(encoded)<=graphics.MAX_RESPONSE_BYTES else None)
    assert len(h.calls)==1 and api().replay_graphics_attempt(h.evidence,h.analyzer.last_regulation_attempt)is None


def test_failed_transport_preserves_attempt_without_private_exception_or_sol(harness):
    h=harness;h.failures[(2,'review')]='transport'
    with pytest.raises(RuntimeError):h.analyzer.analyze_regulation_evidence(h.evidence)
    value=h.analyzer.last_regulation_attempt['graphics_failure']
    assert value['validated_recognition']['response_text']
    assert value['model_call_started']is True and value['response_received']is False
    assert value['failure_reason']=='analysis_unavailable'
    assert 'private transport detail'not in json.dumps(h.analyzer.last_regulation_attempt)
    assert api().replay_graphics_attempt(h.evidence,h.analyzer.last_regulation_attempt)is None


def test_returned_original_is_retained_when_model_deadline_expires(harness,monkeypatch):
    from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded
    h=harness;call=h.analyzer._call_llm
    def late(*args,**kwargs):
        text=call(*args,**kwargs)
        h.analyzer.last_call_failure='model_deadline_exhausted'
        return text
    monkeypatch.setattr(h.analyzer,'_call_llm',late)
    with pytest.raises(ModelDeadlineExceeded):h.analyzer.analyze_regulation_evidence(h.evidence)
    attempt=h.analyzer.last_regulation_attempt
    assert attempt['graphics_failure']['response_received']is True
    assert attempt['graphics_failure']['response_text']
    assert attempt['graphics_failure']['failure_reason']=='model_deadline_exhausted'
    assert attempt['model_call_started']is False and attempt['model_provenance']=={}
    assert api().replay_graphics_attempt(h.evidence,attempt)is None


def test_continuation_preparation_limit_retains_prefix_without_inventing_call(harness,monkeypatch):
    h=harness
    def refused(*args):raise ValueError('invalid_regulation_graphics_context_limit')
    monkeypatch.setattr(api(),'augment_batch_context',refused)
    with pytest.raises(ValueError):h.analyzer.analyze_regulation_evidence(h.evidence)
    attempt=h.analyzer.last_regulation_attempt
    assert len(h.calls)==2 and len(attempt['graphics_batches'])==1
    failure=attempt['graphics_failure']
    assert failure['asset_ids']==[h.rows[-1]['id']]
    assert failure['model_call_started']is False and failure['response_received']is False
    assert failure['request_identity']is failure['model_request']is None
    assert attempt['model_provenance']=={} and attempt['model_call_started']is False
    assert api().replay_graphics_attempt(h.evidence,attempt)is None


def memo_client(h,monkeypatch,identity='profile-a'):
    from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient
    from tradingagents.strategies import plan_auth
    h.analyzer.config['autoresearch']['chatgpt_plan_auth_path']='/offline/auth.json'
    h.analyzer._client=ChatGPTPlanClient('/offline/auth.json',identity)
    monkeypatch.setattr(plan_auth,'read_profile_identity',lambda *args,**kwargs:identity)


def candidate_run(h,discovery='same-discovery'):
    from tradingagents.strategies.candidate_response_reuse import begin_candidate_response,end_candidate_response
    token=begin_candidate_response('regulatory_pipeline','regulation',discovery,False)
    try:return h.analyzer.analyze_regulation_evidence(h.evidence)
    finally:end_candidate_response(token)


def test_four_horizons_reuse_only_complete_reviewed_graphics_with_original_receipts(harness,monkeypatch):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo,_MEMO
    h=harness;memo_client(h,monkeypatch)
    with candidate_response_memo():
        from tradingagents.strategies.modules.regulatory_pipeline import RegulatoryPipelineStrategy
        strategy=RegulatoryPipelineStrategy()
        source={'regulations':{'proposed_rules':[{'document_id':'SEC-2026-6187-0001',
            'title':'Constructed complete source','agency_id':'SEC','posted_date':'2026-10-15',
            'regulation_evidence_ref':'SEC-2026-6187-0001','regulation_evidence_status':'complete'}],
            'regulation_evidence':{'policy':'complete_document_text_v1'}}}
        receipts=[];discoveries=[]
        for horizon in ('30d','3m','6m','1y'):
            candidate=strategy.screen(source,'2026-10-15',strategy.get_default_params(horizon))[0]
            discovery=candidate.metadata['discovery_id'];discoveries.append(discovery)
            assert candidate_run(h,discovery)=={'accepted':True}
            receipts.append(deepcopy(h.analyzer.last_regulation_attempt['graphics_batches']))
        assert len(set(discoveries))==1
        assert all(receipt==receipts[0]for receipt in receipts)
        assert [call[0]for call in h.calls].count('bounded')==4
        assert len(_MEMO.get().graphics_entries)==1
    assert _MEMO.get()is None
    assert [call[0]for call in h.calls].count('bounded')==4
    assert [call[0]for call in h.calls].count('thesis')==4


@pytest.mark.parametrize('change',['bytes','context','auth','discovery','prompt'])
def test_memo_misses_changed_source_client_candidate_or_contract(harness,monkeypatch,change):
    import hashlib
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    h=harness;memo_client(h,monkeypatch)
    with candidate_response_memo():
        candidate_run(h)
        if change=='bytes':
            h.rows[0]['data']=h.rows[1]['data'];h.rows[0]['sha256']=hashlib.sha256(h.rows[0]['data']).hexdigest()
        elif change=='context':h.rows[0]['context']+='\nChanged retained context.'
        elif change=='auth':memo_client(h,monkeypatch,'profile-b')
        elif change=='prompt':monkeypatch.setattr(graphics,'_REVIEW_SYSTEM',graphics._REVIEW_SYSTEM+'\nChanged review instruction.')
        candidate_run(h,'new-discovery'if change=='discovery'else'same-discovery')
    assert [call[0]for call in h.calls].count('bounded')==8


def test_memo_never_crosses_invocation_or_standalone_boundary(harness,monkeypatch):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    h=harness;memo_client(h,monkeypatch)
    for _ in range(2):
        with candidate_response_memo():candidate_run(h)
    candidate_run(h)
    with candidate_response_memo():h.analyzer.analyze_regulation_evidence(h.evidence)
    assert [call[0]for call in h.calls].count('bounded')==16


def test_failed_later_graphic_does_not_publish_even_successful_prefix(harness,monkeypatch):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo,_MEMO
    h=harness;memo_client(h,monkeypatch);h.failures[(4,'review')]='partial'
    with candidate_response_memo():
        with pytest.raises(ValueError):candidate_run(h)
        assert _MEMO.get().graphics_entries=={}
        h.failures.clear()
        candidate_run(h)
    assert [call[0]for call in h.calls].count('bounded')==8


def test_failed_sol_keeps_independently_complete_graphics_receipts(harness,monkeypatch):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo,_MEMO
    from tradingagents.strategies.data_sources import regulation_assessment
    h=harness;memo_client(h,monkeypatch)
    def failed(*args):raise ValueError('invalid_regulation_response')
    with candidate_response_memo():
        monkeypatch.setattr(regulation_assessment,'validate_assessment',failed)
        with pytest.raises(ValueError):candidate_run(h)
        assert len(_MEMO.get().graphics_entries)==1
        monkeypatch.setattr(regulation_assessment,'validate_assessment',lambda *args:{'accepted':True})
        candidate_run(h)
    assert [call[0]for call in h.calls].count('bounded')==4
    assert [call[0]for call in h.calls].count('thesis')==2


def test_cached_bundles_are_replayed_and_caller_mutation_does_not_poison_memo(harness,monkeypatch):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo,_MEMO
    h=harness;memo_client(h,monkeypatch)
    with candidate_response_memo():
        candidate_run(h)
        h.analyzer.last_regulation_attempt['graphics_batches'][0]['review']['response_text']='caller mutation'
        candidate_run(h)
        assert [call[0]for call in h.calls].count('bounded')==4
        cached=next(iter(_MEMO.get().graphics_entries.values()))
        cached[0]['review']['response_text']='corrupt cached receipt'
        candidate_run(h)
    assert [call[0]for call in h.calls].count('bounded')==8


def test_changed_model_never_accepts_previously_cached_graphics(harness,monkeypatch):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo
    h=harness;memo_client(h,monkeypatch)
    with candidate_response_memo():
        candidate_run(h)
        h.analyzer._model_name='gpt-6-astra'
        with pytest.raises(ValueError,match='invalid_regulation_graphics_policy'):candidate_run(h)
    assert len(h.calls)==5
