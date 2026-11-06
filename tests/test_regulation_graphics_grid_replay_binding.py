"""Opt-in plan replay is version-bound; legacy batches cannot masquerade as V8."""
from copy import deepcopy
import pytest
from test_regulation_graphics_assessment import graphic_source,graphic_attempt
from tradingagents.strategies.orchestration.regulation_validation import _attempt


def test_planned_attempt_requires_explicit_configured_policy(graphic_source):
    attempt=graphic_attempt(graphic_source)
    attempt.update(version='regulation-model-attempt-v5',graphics_plan={},graphics_failures=[],prompt_budget_proof=None)
    with pytest.raises(ValueError,match='regulatory graphics execution policy differs'):
        _attempt({'model_attempt':attempt,'model_provenance':{}},graphic_source,None,{})


def test_legacy_graphical_attempt_cannot_enter_new_policy(graphic_source):
    attempt=graphic_attempt(graphic_source)
    with pytest.raises(ValueError,match='regulatory graphics execution policy differs'):
        _attempt({'model_attempt':attempt,'model_provenance':{}},graphic_source,None,{},
            graphics_execution_policy='single_target_compact_v1',prompt_budget_policy='o200k_base_planning_v1')



def test_prior_undeployed_v4_attempt_cannot_be_relabelled_as_compact(graphic_source):
    attempt=graphic_attempt(graphic_source)
    attempt.update(version='regulation-model-attempt-v4',graphics_plan={},graphics_failures=[],prompt_budget_proof=None)
    with pytest.raises(ValueError,match='regulatory graphics execution policy differs'):
        _attempt({'model_attempt':attempt,'model_provenance':{}},graphic_source,None,{},
            graphics_execution_policy='single_target_compact_v1',prompt_budget_policy='o200k_base_planning_v1')


def planned_attempt(evidence,rows):
    import json,hashlib
    from tradingagents.strategies.data_sources.regulation_graphics_grid import prepare_execution_plan
    from tradingagents.strategies.data_sources.regulation_assessment import prepare_request
    from test_regulation_graphics_grid import bundle
    from test_regulation_abstention import abstention,POLICY
    plan=prepare_execution_plan(rows);bundles=[bundle(rows,plan,i)for i in range(len(rows))]
    request=prepare_request(evidence,graphics_plan=plan,graphics_batches=bundles,assessment_policy=POLICY,prompt_budget_policy='o200k_base_planning_v1')
    value=abstention(evidence,graphical=True);value['document_id']=evidence['identity']['document_id']
    value['citations']=[{'unit_id':k,'quote':v[:15]}for k,v in request.context.unit_texts.items()]
    raw=json.dumps(value)
    return {'version':'regulation-model-attempt-v5','source_provenance':json.loads(request.context.provenance_json),
        'model_provenance':{},'model_call_started':True,'response_received':True,'response_text':raw,
        'response_sha256':hashlib.sha256(raw.encode()).hexdigest(),'response_bytes':len(raw.encode()),
        'response_encoding':'utf-8','response_over_limit':False,'validation_status':'validated','failure_reason':None,
        'graphics_plan':plan,'graphics_batches':bundles,'graphics_failures':[],
        'prompt_budget_proof':json.loads(request.context.provenance_json)['prompt_budget_proof']}


@pytest.fixture
def planned_source(monkeypatch):
    from test_regulation_graphics_grid import assets
    from test_regulation_evidence import build
    from tradingagents.strategies.data_sources import regulation_graphics_inputs
    rows=assets(4);evidence=build()
    monkeypatch.setattr(regulation_graphics_inputs,'graphics_assets',lambda _:rows)
    return evidence,rows,planned_attempt(evidence,rows)


def test_complete_v8_plan_replays_every_target_and_keeps_abstention_unresolved(planned_source):
    evidence,rows,attempt=planned_source
    result=_attempt({'model_attempt':attempt,'model_provenance':{}},evidence,None,{},
        assessment_policy='recorded_abstention_v1',graphics_execution_policy='single_target_compact_v1',prompt_budget_policy='o200k_base_planning_v1')
    assert result['assessment_complete'] and result['regulation_evidence_status']=='unresolved_decision'
    assert result['execution_authority']=='none'
    assert result['source_provenance']['source_material_proof']['graphics_plan_sha256']


@pytest.mark.parametrize('damage',['missing_target','extra_target','reordered','context_only','plan','response','unlisted_failure'])
def test_v8_plan_and_all_target_original_bindings_cannot_be_tampered(planned_source,damage):
    evidence,rows,attempt=planned_source
    if damage=='missing_target':attempt['graphics_batches'].pop()
    if damage=='extra_target':attempt['graphics_batches'].append(deepcopy(attempt['graphics_batches'][0]))
    if damage=='reordered':attempt['graphics_batches'].reverse()
    if damage=='context_only':attempt['graphics_batches']=attempt['graphics_batches'][:1]
    if damage=='plan':attempt['graphics_plan']['targets'][0]['context_ids'].reverse()
    if damage=='response':attempt['graphics_batches'][0]['rounds'][0]['review']['receipt']['response_text']+=' '
    if damage=='unlisted_failure':attempt['graphics_failures']=[{'target_index':0}]
    with pytest.raises(ValueError):_attempt({'model_attempt':attempt,'model_provenance':{}},evidence,None,{},
        assessment_policy='recorded_abstention_v1',graphics_execution_policy='single_target_compact_v1',prompt_budget_policy='o200k_base_planning_v1')


def test_failed_v8_original_remains_failed_and_missing_failure_cannot_be_erased(monkeypatch):
    from test_regulation_graphics_grid_runtime import Analyzer,state
    from test_regulation_graphics_grid import assets
    from test_regulation_evidence import build
    from tradingagents.strategies.data_sources import regulation_graphics_inputs
    from tradingagents.strategies.learning.regulation_graphics_grid_runtime import recognize_graphics
    rows=assets(4);evidence=build();worker=Analyzer(state(fail=1))
    # Observe the trusted request boundary; request identities are deliberately
    # absent from the model's content-only user payload.
    import json,threading
    from test_regulation_graphics_grid import response,review_response
    from test_regulation_graphics import provenance
    from tradingagents.strategies.data_sources import regulation_graphics_grid as contract
    local=threading.local()
    original=contract._make
    def capture(*args,**kwargs):
        request=original(*args,**kwargs);local.request=request;return request
    monkeypatch.setattr(contract,'_make',capture)
    def call(self,system,user,**kwargs):
        request=local.request;c=json.loads(request.context_json)
        assert (request.system,request.user,request.document_inputs)==(system,user,kwargs['document_inputs'])
        if c['stage']=='recognition':
            value=response(request,external=True)
            if c['target_index']==1:
                value.update(status='partial',unresolved=['Unreadable cell'])
        else:value=review_response(request,{'result':json.loads(user)['recognition']})
        with self.state['lock']:
            self.state['calls'].append((c['target_index'],c['stage']))
            response_id=f"constructed-partition-{len(self.state['calls'])}"
        self.last_call_provenance=provenance(request)
        self.last_call_provenance['response_id']=response_id
        raw=json.dumps(value);self.last_call_response={'received':True,'text':raw}
        return raw
    monkeypatch.setattr(Analyzer,'_call_llm',call)
    monkeypatch.setattr(regulation_graphics_inputs,'graphics_assets',lambda _:rows)
    attempt={'source_provenance':None,'model_provenance':{},'model_call_started':False,
        'response_received':False,'response_text':None,'response_sha256':None,'response_bytes':None,
        'response_encoding':None,'response_over_limit':False,'validation_status':'failed',
        'failure_reason':'invalid_regulation_graphics_incomplete'}
    with pytest.raises(ValueError):recognize_graphics(worker,rows,attempt)
    before=deepcopy(attempt)
    assert _attempt({'model_attempt':attempt,'model_provenance':{}},evidence,None,{},
        assessment_policy='recorded_abstention_v1',graphics_execution_policy='single_target_compact_v1',prompt_budget_policy='o200k_base_planning_v1')is None
    assert attempt==before
    failed=next(b for b in attempt['graphics_failures'] if b['target_index']==1)
    assert len(attempt['graphics_batches'])==3 and len(attempt['graphics_failures'])==1
    assert len(failed['rounds'])==2 and failed['accepted_round'] is None
    assert all('Unreadable cell' in r['recognition']['failure']['response_text'] for r in failed['rounds'])
    assert all(r['review'] is None for r in failed['rounds'])
    assert (1,'review') not in worker.state['calls']
    attempt['graphics_failures']=[]
    with pytest.raises(ValueError):_attempt({'model_attempt':attempt,'model_provenance':{}},evidence,None,{},
        assessment_policy='recorded_abstention_v1',graphics_execution_policy='single_target_compact_v1',prompt_budget_policy='o200k_base_planning_v1')


@pytest.mark.scheduled_acceptance
@pytest.mark.parametrize('label,count',[('sec_interval',34),('sec_custody',50),('sec_performance',14)])
def test_all_98_official_originals_are_each_targeted_not_credited_as_context(monkeypatch,label,count):
    from test_regulation_government_graphics_v3 import acquire
    from tradingagents.strategies.data_sources.regulation_graphics_inputs import graphics_assets
    payload,_=acquire(monkeypatch,label)
    evidence=next(iter(payload['regulation_evidence']['corpus'].values()))
    rows=graphics_assets(evidence);attempt=planned_attempt(evidence,rows)
    assert len(rows)==len(attempt['graphics_batches'])==len(attempt['graphics_plan']['targets'])==count
    result=_attempt({'model_attempt':attempt,'model_provenance':{}},evidence,None,{},
        assessment_policy='recorded_abstention_v1',graphics_execution_policy='single_target_compact_v1',prompt_budget_policy='o200k_base_planning_v1')
    assert result['assessment_complete'] and result['execution_authority']=='none'
    from tradingagents.strategies.orchestration.regulation_validation import validate_regulation_health
    from tradingagents.strategies.orchestration.regulation_inputs import validate_regulation_policy
    config={'regulation_evidence_policy':'complete_document_text_v1','regulation_retrieval_policy':'verified_government_copy_v3',
        'regulation_assessment_policy':'recorded_abstention_v1','graphics_execution_policy':'single_target_compact_v1','regulation_prompt_budget_policy':'o200k_base_planning_v1'}
    data={'regulations':payload};scope=validate_regulation_policy(data,config)
    policies={'p:health:'+h for h in('30d','3m','6m','1y')};doc=evidence['identity']['document_id']
    graph=payload['regulation_evidence']
    row={'document_id':doc,'regulation_evidence_ref':doc,'source_rows':graph['rows'],
        'source_manifest_sha256':graph['manifest_sha256'],'evidence_sha256':evidence['manifest_sha256'],
        'model_attempt':attempt,'model_provenance':{},'assessment':result,'document_assessment':result['document_assessment'],
        'regulation_evidence_status':result['regulation_evidence_status'],'analysis_status':'validated',
        'analysis_failure_reason':None,'ticker':'','journal_only':True}
    records=[{'policy_id':p,'strategy':'regulatory_pipeline','status':'signals','signal_count':1,
        'evidence':{'regulation_source_coverage':scope,'regulation_assessments':[deepcopy(row)],
            'data_sources':['regulations']}}for p in policies]
    health=validate_regulation_health(data,records,policies,{'regulatory_pipeline':['regulations']},config)
    assert health['assessment_complete'] and not health['decision_evidence_complete']
    assert len(health['horizons'])==4 and all(h['abstained_documents']==1 for h in health['horizons'])


def test_pure_compact_request_keeps_all_target_literals_foreign_sources_and_budget(planned_source):
    import json
    from tradingagents.strategies.data_sources.regulation_graphics_material import decode_material
    from tradingagents.strategies.data_sources.regulation_assessment import prepare_request
    from tradingagents.strategies.data_sources.regulation_prompt_budget import validate_proof
    evidence,rows,attempt=planned_source
    request=prepare_request(evidence,graphics_plan=attempt['graphics_plan'],
        graphics_batches=attempt['graphics_batches'],assessment_policy='recorded_abstention_v1',
        prompt_budget_policy='o200k_base_planning_v1')
    units=[u for u in json.loads(request.user)['units'] if u['role']=='graphic']
    assert len(units)==len(rows)
    for index,unit in enumerate(units):
        material=decode_material(unit['text'])
        assert material['id']==rows[index]['id'] and material['sha256']==rows[index]['sha256']
        assert material['content']['tables'][0]['matrix'][1]==['Itemᵃ','1']
        assert material['content']['notes'][0]['image_position']==2
        assert material['sources'][0]['id']==rows[index]['id']
        assert material['sources'][1]['id']!=rows[index]['id']
    provenance=json.loads(request.context.provenance_json)
    proof=validate_proof(request.system,request.user,provenance['prompt_budget_proof'])
    assert proof['status']=='measured' and proof['planning_limit']==475000
    assert proof['within_planning_limit'] is True
