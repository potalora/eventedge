"""Measured final requests and no-call failures, through the real analyzer/replay."""
from copy import deepcopy
import hashlib,json
import pytest
from test_regulation_evidence import build,fixture,DOC,STAMP,module
from test_regulation_abstention import abstention
from tradingagents.strategies.data_sources import regulation_prompt_budget as b
from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.orchestration.regulation_validation import _attempt


def config():
    return {'autoresearch':{'graphics_execution_policy':'single_target_compact_v1',
        'regulation_prompt_budget_policy':b.POLICY,'regulation_assessment_policy':'recorded_abstention_v1',
        'model_runtime_policy':'full_regulation_graphics_v1','regulation_retrieval_policy':'verified_government_copy_v3',
        'regulation_evidence_policy':'complete_document_text_v1','llm_billing_mode':'chatgpt_plan',
        'autoresearch_model':'gpt-6-luna','llm_effort':'high','thesis_model':'gpt-6.1-sol',
        'thesis_effort':'high','llm_temperature':0.0},'execution':{'mode':'paper','execution_enabled':False}}


def replay(evidence,attempt):
    return _attempt({'model_attempt':attempt,'model_provenance':attempt['model_provenance']},evidence,None,{},
        assessment_policy='recorded_abstention_v1',graphics_execution_policy='single_target_compact_v1',
        prompt_budget_policy=b.POLICY)


def test_text_only_v8_binds_actual_budget_before_one_sol_call_and_pure_replay(monkeypatch):
    evidence=build();analyzer=LLMAnalyzer(config());calls=[]
    def call(system,user,**kwargs):
        calls.append((system,user));p=analyzer.last_regulation_attempt['prompt_budget_proof']
        assert p['system_sha256']==hashlib.sha256(system.encode()).hexdigest()
        assert p['user_sha256']==hashlib.sha256(user.encode()).hexdigest()
        assert analyzer.last_regulation_attempt['source_provenance']['prompt_budget_proof']==p
        return json.dumps(abstention(evidence))
    monkeypatch.setattr(analyzer,'_call_llm',call)
    result=analyzer.analyze_regulation_evidence(evidence,assessment_policy='recorded_abstention_v1')
    attempt=analyzer.last_regulation_attempt
    assert len(calls)==1 and attempt['version']=='regulation-model-attempt-v5'
    assert attempt['graphics_plan'] is None and attempt['graphics_batches']==attempt['graphics_failures']==[]
    assert result['assessment_complete'] and replay(evidence,attempt)==result
    changed=deepcopy(attempt);changed['prompt_budget_proof']['input_tokens']+=1
    with pytest.raises(ValueError):replay(evidence,changed)
    with pytest.raises(ValueError):_attempt({'model_attempt':attempt,'model_provenance':{}},evidence,None,{})


def oversized_evidence():
    row,detail,bodies=fixture();bodies[DOC]=b'<html><body><p>'+b'word '*475001+b'</p></body></html>'
    detail['data']['attributes']['fileFormats'][0]['size']=len(bodies[DOC])
    return module().build_regulation_evidence(row,json.dumps(detail).encode(),bodies,observed_at=STAMP)


@pytest.mark.parametrize('failure',['over_limit','vocabulary'])
def test_no_sol_call_and_complete_source_receipt_survives_budget_failure(monkeypatch,failure):
    evidence=oversized_evidence()if failure=='over_limit'else build()
    if failure=='vocabulary':monkeypatch.setattr(b,'_vocabulary_bytes',lambda:b'tampered')
    analyzer=LLMAnalyzer(config())
    monkeypatch.setattr(analyzer,'_call_llm',lambda *a,**kw:pytest.fail('Sol must not be called'))
    with pytest.raises(ValueError,match='invalid_regulation_prompt_'):
        analyzer.analyze_regulation_evidence(evidence,assessment_policy='recorded_abstention_v1')
    attempt=analyzer.last_regulation_attempt
    assert attempt['model_call_started'] is attempt['response_received'] is False
    assert attempt['source_provenance']['evidence_sha256']==evidence['manifest_sha256']
    assert attempt['source_provenance']['source_material_proof']['unit_ids']
    assert attempt['prompt_budget_proof']==attempt['source_provenance']['prompt_budget_proof']
    assert replay(evidence,attempt) is None
    changed=deepcopy(attempt);changed['validation_status']='validated';changed['failure_reason']=None
    with pytest.raises(ValueError):replay(evidence,changed)


def test_completed_graphics_and_all_source_units_survive_no_sol_budget_failure(monkeypatch):
    from test_regulation_graphics_grid import assets,response,review_response
    from test_regulation_graphics import provenance
    from tradingagents.strategies.data_sources import regulation_graphics_grid as contract
    from tradingagents.strategies.data_sources import regulation_graphics_inputs
    rows=assets(4);evidence=build();analyzer=LLMAnalyzer(config());stages=[]
    import threading
    prepared=threading.local()
    # Trusted runtime identities stay outside model content. Capture the prepared
    # request at that boundary while retaining the real analyzer native-call path.
    original=contract._make
    def capture(*args,**kwargs):
        request=original(*args,**kwargs);prepared.request=request;return request
    monkeypatch.setattr(contract,'_make',capture)
    monkeypatch.setattr(regulation_graphics_inputs,'graphics_assets',lambda _:rows)
    monkeypatch.setattr(b,'_vocabulary_bytes',lambda:b'tampered')
    def call(system,user,**kwargs):
        assert kwargs['role']=='bounded','Sol must never start'
        payload=json.loads(user);q=prepared.request;c=json.loads(q.context_json);stages.append(c['stage'])
        assert (q.system,q.user,q.document_inputs)==(system,user,kwargs['document_inputs'])
        if c['stage']=='recognition':value=response(q,external=True)
        else:value=review_response(q,{'receipt_sha256':c['recognition_receipt_sha256'],'result':payload['recognition']})
        raw=json.dumps(value);analyzer.last_call_provenance=provenance(q)
        analyzer.last_call_provenance['response_id']=f"constructed-{c['target_index']}-{c['stage']}-{len(stages)}"
        analyzer.last_call_response={'received':True,'text':raw}
        return raw
    monkeypatch.setattr(analyzer,'fork_for_parallel',lambda:analyzer)
    # Run one target worker so this deliberately shared test analyzer has no
    # concurrent mutable transport state; actual worker concurrency is tested separately.
    import tradingagents.strategies.learning.regulation_graphics_grid_runtime as runtime
    monkeypatch.setattr(runtime,'TARGET_WORKERS',1)
    monkeypatch.setattr(analyzer,'_call_llm',call)
    with pytest.raises(ValueError,match='invalid_regulation_prompt_vocabulary_hash'):
        analyzer.analyze_regulation_evidence(evidence,assessment_policy='recorded_abstention_v1')
    attempt=analyzer.last_regulation_attempt
    assert stages.count('recognition')==stages.count('review')==4
    assert len(attempt['graphics_batches'])==4 and attempt['graphics_failures']==[]
    assert len(attempt['source_provenance']['unit_ids'])==len(evidence['units'])+4
    assert attempt['model_call_started'] is attempt['response_received'] is False
    assert replay(evidence,attempt) is None
