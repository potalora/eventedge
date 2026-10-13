"""Finite recognition validates structure, never gold accuracy or trading authority."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib
import json

import pytest

POLICY='bounded_document_research_v1'
PDF=b'%PDF-1.7\nretained-native-fixture'
PNG=b'\x89PNG\r\n\x1a\nretained-page-fixture'
AS_OF='2026-10-12T20:01:00+00:00'


def wire(data,kind,name):
    return dict(media_type=kind,filename=name,sha256=hashlib.sha256(data).hexdigest(),
                data_base64=base64.b64encode(data).decode())


def evidence(*,image=False):
    original=wire(PDF,'application/pdf','retained.pdf')
    attachment=wire(PNG,'image/png','page-2.png') if image else deepcopy(original)
    attachment.update(source_sha256=original['sha256'],pages=[2],
                      render_identity='fixture-render-v1' if image else 'native-original-v1')
    return dict(policy=POLICY,document_id='retained-filing-1',as_of=AS_OF,
        original=original,original_pages=3,selected_pages=[2],attachments=[attachment],
        probes=[{'id':'revenue','page':2,'question':'Revenue in Current, with printed units'}],
        residual_failures=['unsupported_selected_visual_content'])


def module():
    return importlib.import_module('tradingagents.strategies.bounded_document_research')


def prepare(value=None):
    return module().prepare_recognition(value or evidence(),session_as_of='2026-10-12')


def recognition(request):
    identity=json.loads(request.context_json)
    return {'version':'bounded-document-recognition-v1','document_id':'retained-filing-1',
        'as_of':AS_OF,'original_sha256':hashlib.sha256(PDF).hexdigest(),
        'request_identity_sha256':identity['request_identity_sha256'],
        'pages':[{'page':2,'status':'readable',
            'transcription':'Revenue (USD millions)\nCurrent Prior\nRevenue 1,204.50 999.00',
            'probes':[{'id':'revenue','printed_value':'1,204.50','unit':'USD millions',
                'row':'Revenue','column':'Current','citation':'Revenue 1,204.50 999.00'}],
            'uncertainties':[]}]}


def test_valid_native_and_scanned_inputs_keep_original_failure_and_no_full_claim():
    for image in (False,True):
        request=prepare(evidence(image=image))
        result=module().validate_recognition(json.dumps(recognition(request)),request)
        assert result['validation_status']=='structurally_valid'
        assert result['extraction_accuracy']=='independently_unverified'
        assert result['assessment_scope']=='selected_pages'
        assert result['unassessed_pages']==[1,3]
        assert 'unsupported_selected_visual_content' in result['residual_failures']
        assert result['full_document_quality_accepted'] is False
        assert result['trading_authority'] is False
        assert request.document_inputs[0].data==(PNG if image else PDF)


@pytest.mark.parametrize('damage',['source_hash','as_of','missing_as_of','naive_as_of','future',
    'unknown_policy','extra','duplicate_page','missing_page','bad_render','wrong_original',
    'missing_probe','wrong_probe_page','extra_attachment','missing_bytes','duplicate_probe'])
def test_source_binding_faults_are_rejected_before_transport(damage):
    value=evidence()
    if damage=='source_hash': value['attachments'][0]['source_sha256']='0'*64
    elif damage=='as_of': value['as_of']='not-a-date'
    elif damage=='missing_as_of': del value['as_of']
    elif damage=='naive_as_of': value['as_of']='2026-10-12T20:01:00'
    elif damage=='future': value['as_of']='2026-10-13T20:01:00+00:00'
    elif damage=='unknown_policy': value['policy']='anything'
    elif damage=='extra': value['trusted']=True
    elif damage=='duplicate_page': value['selected_pages']=[2,2]
    elif damage=='missing_page': value['attachments'][0]['pages']=[1]
    elif damage=='bad_render': value['attachments'][0]['render_identity']=''
    elif damage=='wrong_original': value['original']['sha256']='0'*64
    elif damage=='missing_probe': value['probes']=[]
    elif damage=='wrong_probe_page': value['probes'][0]['page']=1
    elif damage=='extra_attachment': value['attachments'].append(deepcopy(value['attachments'][0]))
    elif damage=='missing_bytes': value['original']['data_base64']=''
    else: value['probes'].append(deepcopy(value['probes'][0]))
    with pytest.raises(ValueError,match='invalid_bounded_document'):
        prepare(value)


@pytest.mark.parametrize('damage',['source','request_identity','missing_page','extra_page','duplicate_page',
    'uncertain','uncertainties','printed_number_prefix','row_prefix','unit_missing','column_missing',
    'detached_citation','missing_probe','unknown_probe','extra_key','bool_page','numeric_value',
    'currency_split','percent_split','wrong_row_citation','blank_unrelated_citation','numeric_unit_null'])
def test_bad_recognition_cannot_become_structurally_valid(damage):
    request=prepare();value=recognition(request);page=value['pages'][0];probe=page['probes'][0]
    if damage=='source': value['original_sha256']='0'*64
    elif damage=='request_identity': value['request_identity_sha256']='0'*64
    elif damage=='missing_page': value['pages']=[]
    elif damage=='extra_page': value['pages'].append(dict(page,page=3))
    elif damage=='duplicate_page': value['pages'].append(deepcopy(page))
    elif damage=='uncertain': page['status']='uncertain'
    elif damage=='uncertainties': page['uncertainties']=['unclear header']
    elif damage=='printed_number_prefix': probe['printed_value']='1,204'
    elif damage=='row_prefix': probe['row']='Reven'
    elif damage=='unit_missing': probe['unit']='USD thousands'
    elif damage=='column_missing': probe['column']='Future'
    elif damage=='detached_citation': probe['citation']='Revenue 1,204.50 plus invented words'
    elif damage=='missing_probe': page['probes']=[]
    elif damage=='unknown_probe': probe['id']='not_requested'
    elif damage=='extra_key': probe['trusted']=True
    elif damage=='bool_page': page['page']=True
    elif damage=='currency_split':
        page['transcription']=page['transcription'].replace('1,204.50','$1,204.50')
        probe['citation']=probe['citation'].replace('1,204.50','$1,204.50')
    elif damage=='percent_split':
        page['transcription']=page['transcription'].replace('1,204.50','1,204.50%')
        probe['citation']=probe['citation'].replace('1,204.50','1,204.50%')
    elif damage=='wrong_row_citation':
        page['transcription']+='\nOther 1,204.50';probe['citation']='Other 1,204.50'
    elif damage=='blank_unrelated_citation':
        probe['printed_value']=None;probe['citation']='999.00'
    elif damage=='numeric_unit_null': probe['unit']=None
    else: probe['printed_value']=1204.50
    with pytest.raises(ValueError,match='invalid_bounded_document'):
        module().validate_recognition(json.dumps(value),request)


@pytest.mark.parametrize('raw',['{"pages":[],"pages":[]}','NaN','[', '['*1000+'0'+']'*1000])
def test_malformed_duplicate_and_nonfinite_recognition_is_rejected(raw):
    with pytest.raises(ValueError,match='invalid_bounded_document'):
        module().validate_recognition(raw,prepare())


def test_attachment_bytes_and_order_are_bound_into_request_identity():
    first=evidence(image=True);second=deepcopy(first)
    second['attachments'][0].update(wire(PNG+b'changed','image/png','page-2.png'))
    assert prepare(first).context_json!=prepare(second).context_json
    assert prepare(first).user!=prepare(second).user
    # Context is immutable and independent of caller mutation.
    frozen=prepare(first);first['probes'][0]['question']='mutated'
    assert 'mutated' not in frozen.user


def test_limits_reject_without_clipping_original_or_attachment():
    value=evidence(image=True)
    value['attachments'][0].update(wire(PNG+b'x'*(4*1024*1024),'image/png','page-2.png'))
    with pytest.raises(ValueError,match='invalid_bounded_document'): prepare(value)
    value=evidence();value['original'].update(wire(PDF+b'x'*(16*1024*1024),'application/pdf','retained.pdf'))
    with pytest.raises(ValueError,match='invalid_bounded_document'): prepare(value)


@pytest.fixture
def analyzer(monkeypatch):
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient
    from tradingagents.strategies import llm_utils
    monkeypatch.setattr('tradingagents.strategies.chatgpt_plan_client._profile_identity',lambda path:'profile-fixture')
    monkeypatch.setattr('tradingagents.strategies.plan_auth.read_profile_identity',lambda *args, **kwargs:'profile-fixture')
    config={'autoresearch':{'bounded_document_research_policy':POLICY,
        'research_resilience_policy':'dependency_isolation_v1', 'autoresearch_model':'gpt-6-luna',
        'llm_effort':'high','thesis_model':'gpt-6.1-sol','thesis_effort':'high','llm_billing_mode':'chatgpt_plan'}}
    worker=LLMAnalyzer(config);worker._client=ChatGPTPlanClient(None,'profile-fixture')
    calls=[];faults=[]
    def send(payload,timeout):
        calls.append(deepcopy(payload))
        if faults and isinstance(faults[0],Exception): raise faults.pop(0)
        request=payload['request'];model=request['model']
        if model=='gpt-6-luna':
            prepared=prepare(evidence(image=bool(request.get('document_inputs',[{}])[0].get('media_type')=='image/png')))
            output=recognition(prepared)
        else:
            output={'assessment_scope':'selected_pages','direction':'neutral','score':0.0,
                'rationale':'Only the requested revenue cell was reviewed.',
                'evidence_claim':'The selected page prints revenue of 1,204.50 USD millions.'}
        if faults:
            fault=faults.pop(0)
            if callable(fault): fault(output)
        provenance={'configured_model':model,'returned_model':model,'response_id':'resp-'+str(len(calls)),
            'reasoning_effort':'high','billing_mode':'chatgpt_plan','effective_output_limit':None,
            'identity_status':'unpinned','returned_revision':None}
        if model=='gpt-6-luna':
            from tradingagents.strategies.plan_document_inputs import document_input_metadata
            provenance['document_inputs']=document_input_metadata(request['document_inputs'])
        return {'text':json.dumps(output),'provenance':provenance}
    monkeypatch.setattr(llm_utils,'bounded_transport',send)
    return worker,calls,faults


def test_real_analyzer_forwards_document_then_scoped_sol_and_retains_both_attempts(analyzer):
    worker,calls,_=analyzer
    result=worker.analyze_bounded_document_evidence(evidence(),session_as_of='2026-10-12')
    assert [c['request']['model'] for c in calls]==['gpt-6-luna','gpt-6.1-sol']
    assert 'document_inputs' in calls[0]['request'] and 'document_inputs' not in calls[1]['request']
    assert result['analysis_status']=='validated' and result['journal_only'] is True
    assert result['trading_authority'] is False and result['assessment_scope']=='selected_pages'
    retained=result['bounded_document_research']
    assert retained['recognition']['extraction_accuracy']=='independently_unverified'
    assert retained['recognition_attempt']['model_request']['role']=='bounded'
    assert retained['thesis_attempt']['model_request']['role']=='thesis'
    assert retained['receipt_sha256']==hashlib.sha256(json.dumps({k:v for k,v in retained.items() if k!='receipt_sha256'}, sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()
    assert retained['recognition_attempt']['response_sha256']
    assert 'unsupported_selected_visual_content' in retained['recognition']['residual_failures']


def test_verified_claims_inform_scoped_thesis_without_admitting_unreviewed_transcription():
    receipt = {'eligible':True,'required_claim_ids':['revenue'],
               'verified_claims':[{'id':'revenue','printed_value':'1,204.50','unit':'USD millions'}],
               'context_limitations':['Selected revenue context only']}
    observed = module().validate_recognition(json.dumps(recognition(prepare())),prepare())
    observed['pages'][0]['transcription'] += '\nUNREVIEWED FACT 999999'
    system,user = module().prepare_thesis(observed, decision_eligibility=receipt)
    assert '1,204.50' in user and 'UNREVIEWED FACT' not in user
    assert 'ordinary' in system and 'context_sufficient' in system
    response = {'assessment_scope':'selected_pages','direction':'long','score':.9,
        'rationale':'Constructed directional thesis uses independently reviewed claim.',
        'evidence_claim':'Revenue is 1,204.50 USD millions.',
        'context_sufficient':True,'used_claim_ids':['revenue']}
    result = module().validate_thesis(json.dumps(response),decision_eligibility=receipt)
    assert result['journal_only'] is False and result['decision_eligibility'] == receipt
    assert 'trading_authority' not in result
    response['evidence_claim'] = 'Reviewed revenue is 1,204.50.'
    assert module().validate_thesis(json.dumps(response),decision_eligibility=receipt)['journal_only'] is False
    response['context_sufficient'] = False
    with pytest.raises(ValueError):
        module().validate_thesis(json.dumps(response),decision_eligibility=receipt)


@pytest.mark.parametrize('execution',[{'mode':'live','execution_enabled':False},
                                     {'mode':'paper','execution_enabled':True}])
def test_decision_policy_rejects_real_execution_before_any_model_call(analyzer,execution):
    worker,calls,_ = analyzer
    worker.config['autoresearch']['bounded_document_decision_policy'] = 'verified_scoped_claims_v1'
    worker.config['execution'] = execution
    with pytest.raises(ValueError,match='invalid_document_decision_paper_only'):
        worker.analyze_bounded_document_evidence(evidence(),session_as_of='2026-10-12',ticker='MSFT',decision_scope={})
    assert not calls


@pytest.mark.parametrize('number',['1,204.51','1e6'])
def test_scoped_thesis_cannot_replace_reviewed_numeric_source_facts(number):
    eligibility = {'required_claim_ids':['revenue'],
        'verified_claims':[{'id':'revenue','printed_value':'1,204.50','unit':'USD millions'}]}
    response = {'assessment_scope':'selected_pages','direction':'long','score':.9,
        'rationale':'Constructed directional analysis.', 'evidence_claim':'Revenue is '+number+' USD millions.',
        'context_sufficient':True,'used_claim_ids':['revenue']}
    with pytest.raises(ValueError,match='invalid_document_decision_claim'):
        module().validate_thesis(json.dumps(response),decision_eligibility=eligibility)


@pytest.mark.parametrize('after_cutoff',[False,True])
def test_unattended_analyzer_produces_review_from_blind_source_check_without_runtime_answers(analyzer,tmp_path,monkeypatch,after_cutoff):
    from test_document_decision_workflow import decision_payload
    from tradingagents.strategies.orchestration.bounded_document_inputs import load_retained_documents
    worker,_,_ = analyzer
    payload = decision_payload(tmp_path)
    scope = deepcopy(payload['registry_entry']['decision_scope'])
    scope.pop('independent_review')
    worker.config['autoresearch']['bounded_document_decision_policy'] = 'verified_scoped_claims_v1'
    worker.config['execution'] = {'mode':'paper','execution_enabled':False}
    source = load_retained_documents({'bounded_document_research_policy':POLICY,
        'bounded_document_registry':[payload['registry_entry']]},'2026-10-12')
    value = source['bounded_documents'][0]['evidence']
    raw = json.loads(payload['recognition_text'])
    seen = []
    clock = {'now':'2026-10-12T22:00:00+00:00'}
    def call(system,user,*,role='thesis',document_inputs=None,**kwargs):
        seen.append((system,user,role,document_inputs))
        if 'Independent blind source verification' in system:
            assert '1,204.50' not in user and '999.00' not in user
            if after_cutoff:
                clock['now'] = '2026-10-13T04:00:01+00:00'
            output = {'recognition':raw,'context_sufficient':True,'material_context_failures':[]}
        elif document_inputs is not None:
            output = raw
        else:
            output = payload['thesis_response']
        text = json.dumps(output)
        worker.last_call_response = {'received':True,'text':text}
        worker.last_call_provenance = {'configured_model':'gpt-6-luna'if role=='bounded'else'gpt-6.1-sol',
            'returned_model':'gpt-6-luna'if role=='bounded'else'gpt-6.1-sol',
            'identity_status':'unpinned','role':role,'billing_mode':'chatgpt_plan',
            'reasoning_effort':'high'}
        if document_inputs is not None:
            from tradingagents.strategies.plan_document_inputs import encode_document_inputs,document_input_metadata
            worker.last_call_provenance['document_inputs'] = document_input_metadata(encode_document_inputs(document_inputs))
        return text
    monkeypatch.setattr(worker,'_call_llm',call)
    if after_cutoff:
        with pytest.raises(ValueError,match='invalid_document_decision_review_time'):
            worker.analyze_bounded_document_evidence(value,session_as_of='2026-10-12',
                ticker='MSFT',decision_scope=scope,verification_clock=lambda:clock['now'])
        assert [stage[2]for stage in seen] == ['bounded','bounded']
        return
    result = worker.analyze_bounded_document_evidence(value,session_as_of='2026-10-12',
        ticker='MSFT',decision_scope=scope,verification_clock=lambda:clock['now'])
    assert [stage[2]for stage in seen] == ['bounded','bounded','thesis']
    assert seen[0][3] is not None and seen[1][3] is not None and seen[2][3] is None
    assert result['journal_only'] is False
    attempt = result['bounded_document_research']
    assert attempt['verification_attempt']['response_sha256']
    assert attempt['automatic_review']['reviewer']['kind'] == 'automatic_visual'
    assert result['decision_eligibility']['verification_basis'] == 'automatic_review_v1'


def test_failed_recognition_never_invokes_sol_and_retains_original_attempt(analyzer):
    worker,calls,faults=analyzer
    faults.append(lambda value:value['pages'][0].update(status='uncertain'))
    with pytest.raises(ValueError,match='invalid_bounded_document'):
        worker.analyze_bounded_document_evidence(evidence(),session_as_of='2026-10-12')
    assert len(calls)==1
    assert worker.last_bounded_document_attempt['validation_status']=='failed'
    assert worker.last_bounded_document_attempt['receipt_sha256']
    assert worker.last_bounded_document_attempt['recognition_attempt']['response_sha256']


def test_deadline_remains_deadline_and_never_falls_through_to_sol(analyzer):
    from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded
    worker,calls,faults=analyzer;faults.append(TimeoutError('deadline'))
    with pytest.raises(ModelDeadlineExceeded):
        worker.analyze_bounded_document_evidence(evidence(),session_as_of='2026-10-12')
    assert len(calls)==1 and worker.last_call_failure=='model_deadline_exhausted'


def test_policy_absence_or_bad_source_causes_zero_model_calls(analyzer):
    worker,calls,_=analyzer
    del worker.config['autoresearch']['bounded_document_research_policy']
    with pytest.raises(ValueError,match='invalid_bounded_document'):
        worker.analyze_bounded_document_evidence(evidence(),session_as_of='2026-10-12')
    assert calls==[]


def test_document_calls_bypass_candidate_reuse_and_clear_previous_pending_request(analyzer):
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo,begin_candidate_response,end_candidate_response,_CANDIDATE
    worker,calls,_=analyzer
    with candidate_response_memo():
        token=begin_candidate_response('bounded_document_research','bounded_document','discovery',False)
        try:
            worker._call_llm('s','plain request',role='thesis')
            assert _CANDIDATE.get().pending is not None
            inputs=prepare().document_inputs
            worker._call_llm('s',prepare().user,role='bounded',document_inputs=inputs)
            assert _CANDIDATE.get().key is None and _CANDIDATE.get().pending is None
            worker._call_llm('s',prepare().user,role='bounded',document_inputs=inputs)
        finally: end_candidate_response(token)
    assert len(calls)==3


def test_numeric_unit_null_never_invokes_sol(analyzer):
    worker,calls,faults=analyzer
    faults.append(lambda value:value['pages'][0]['probes'][0].update(unit=None))
    with pytest.raises(ValueError,match='invalid_bounded_document'):
        worker.analyze_bounded_document_evidence(evidence(),session_as_of='2026-10-12')
    assert len(calls)==1
    assert worker.last_bounded_document_attempt['validation_status']=='failed'
    assert worker.last_bounded_document_attempt['recognition'] is None
    assert worker.last_bounded_document_attempt['thesis_attempt'] is None
    assert worker.last_bounded_document_attempt['recognition_attempt']['response_sha256']


@pytest.mark.parametrize('blank',[True,False])
def test_blank_and_nonnumeric_probes_may_remain_unitless(blank):
    request=prepare();value=recognition(request);page=value['pages'][0];probe=page['probes'][0]
    probe['unit']=None
    if blank:
        probe.update(printed_value=None,row=None,citation='Current Prior')
    else:
        page['transcription']+='\nRevenue not reported'
        probe.update(printed_value='not reported',citation='Revenue not reported')
    result=module().validate_recognition(json.dumps(value),request)
    assert result['validation_status']=='structurally_valid'
    assert result['pages'][0]['probes'][0]['unit'] is None


@pytest.mark.parametrize('printed',['1e6','100M'])
def test_numeric_notation_without_unit_never_invokes_sol(analyzer,printed):
    worker,calls,faults=analyzer
    def omit_unit(value):
        page=value['pages'][0]
        page['transcription']+='\nRevenue '+printed
        page['probes'][0].update(printed_value=printed,unit=None,citation='Revenue '+printed)
    faults.append(omit_unit)
    with pytest.raises(ValueError,match='invalid_bounded_document'):
        worker.analyze_bounded_document_evidence(evidence(),session_as_of='2026-10-12')
    assert len(calls)==1
    assert worker.last_bounded_document_attempt['thesis_attempt'] is None
    assert worker.last_bounded_document_attempt['validation_status']=='failed'
