"""Constructed model evidence: successor clock authority, never native acceptance."""
from copy import deepcopy
from datetime import date, datetime, timezone
import importlib
import json

import pytest
from test_document_decision_eligibility import automatic_fixture, build_auto
from tradingagents.strategies import document_decision_eligibility as eligibility

V2='verified_scoped_claims_next_open_v2'
CONFIG={'autoresearch':{'bounded_document_decision_policy':V2,
    'decision_clock_policy':'prospective_next_open_v1','research_resilience_policy':'dependency_isolation_v1'}}

def module():return importlib.import_module('tradingagents.strategies.document_review_clock')

def prepared(reviewed_at='2026-10-13T04:15:00+00:00'):
    primary,wrapper,request,scope,provenance=automatic_fixture()
    clock=module().review_clock(CONFIG,'2026-10-12')
    review=eligibility.build_automatic_review(primary,json.dumps(wrapper),request,scope,ticker='ACME',
        session_as_of='2026-10-12',verified_at=reviewed_at,model_provenance=provenance,review_clock=clock)
    value=eligibility.assess_decision_eligibility(primary,{**scope,'independent_review':review},ticker='ACME',
        session_as_of='2026-10-12',review_clock=clock)
    return primary,scope,review,value,clock

def test_legacy_after_midnight_still_rejects():
    with pytest.raises(ValueError,match='invalid_document_decision_review_time'):
        build_auto(*automatic_fixture(),verified_at='2026-10-13T04:15:00+00:00')

def test_next_open_review_preserves_real_time_scope_and_provisional_authority():
    primary,scope,review,value,clock=prepared()
    assert clock=={'version':'document-review-clock-next-open-v1','reference_session':'2026-10-12',
        'eligible_session':'2026-10-13','next_open':'2026-10-13T13:30:00+00:00'}
    assert review['reviewed_at']=='2026-10-13T04:15:00+00:00'
    assert review['version']=='automatic_review_next_open_v2'
    assert value['policy']==V2 and value['review_clock']==clock
    assert value['paper_decision_eligible']is False
    assert value['execution_authority']=='requires_final_decision_clock'
    assert all(value[k]==scope[k]for k in ('purpose','economic_date','context_limitations','effective_until','as_of'))
    assert eligibility.assess_decision_eligibility(primary,{**scope,'independent_review':review},ticker='ACME',
        session_as_of='2026-10-12',review_clock=clock)==value
    with pytest.raises(ValueError):
        eligibility.assess_decision_eligibility(primary,{**scope,'independent_review':review},ticker='ACME',session_as_of='2026-10-12')

@pytest.mark.parametrize('instant',['2026-10-13T13:30:00+00:00','2026-10-13T13:30:00.000001+00:00'])
def test_exact_or_after_open_rejected(instant):
    with pytest.raises(ValueError,match='invalid_document_decision_review_time'):prepared(instant)

@pytest.mark.parametrize('change',[{'reference_session':'2026-10-13'}, {'eligible_session':'2026-10-14'},
    {'next_open':'2026-10-13T14:30:00+00:00'}, {'extra':True}])
def test_clock_tamper_rejected(change):
    primary,scope,review,value,clock=prepared();clock.update(change)
    with pytest.raises(ValueError):
        eligibility.assess_decision_eligibility(primary,{**scope,'independent_review':review},ticker='ACME',
            session_as_of='2026-10-12',review_clock=clock)

@pytest.mark.parametrize('config',[{}, {'autoresearch':{'bounded_document_decision_policy':V2}},
    {'autoresearch':{'bounded_document_decision_policy':V2,'decision_clock_policy':'session_close_v1'}},
    {'autoresearch':{'bounded_document_decision_policy':'unknown','decision_clock_policy':'prospective_next_open_v1'}}])
def test_unsupported_missing_clock_configuration_rejected(config):
    with pytest.raises(ValueError):module().review_clock(config,'2026-10-12')

def test_blind_request_version_binds_calendar_without_answers_or_legacy_byte_change():
    primary,wrapper,request,scope,provenance=automatic_fixture()
    before=eligibility.prepare_automatic_verification(request,scope)
    clock=module().review_clock(CONFIG,'2026-10-12')
    system,user=eligibility.prepare_automatic_verification(request,scope,review_clock=clock)
    assert 'review_clock' in user and clock['next_open']in user
    assert '1,204.50'not in system+user
    assert eligibility.prepare_automatic_verification(request,scope)==before
    assert (system,user)!=before


def test_review_effective_expiry_not_widened():
    primary,wrapper,request,scope,provenance=automatic_fixture()
    clock=module().review_clock(CONFIG,'2026-10-12')
    scope['effective_until']='2026-10-13T04:15:00+00:00'
    review=eligibility.build_automatic_review(primary,json.dumps(wrapper),request,scope,ticker='ACME',
        session_as_of='2026-10-12',verified_at=scope['effective_until'],model_provenance=provenance,review_clock=clock)
    assert review['reviewed_at']==scope['effective_until']
    with pytest.raises(ValueError,match='invalid_document_decision_window'):
        eligibility.build_automatic_review(primary,json.dumps(wrapper),request,scope,ticker='ACME',session_as_of='2026-10-12',
            verified_at='2026-10-13T04:15:00.000001+00:00',model_provenance=provenance,review_clock=clock)

from test_bounded_document_research import analyzer
from test_document_decision_workflow import decision_payload


def analyzed(analyzer,tmp_path,monkeypatch,*,effective_until=None):
    worker,_,_=analyzer
    payload=decision_payload(tmp_path)
    entry=payload['registry_entry'];entry['decision_scope'].pop('independent_review')
    if effective_until is not None:entry['decision_scope']['effective_until']=effective_until
    worker.config['autoresearch'].update(CONFIG['autoresearch'])
    worker.config['autoresearch']['bounded_document_registry']=[entry]
    worker.config['execution']={'mode':'paper','execution_enabled':False}
    from tradingagents.strategies.orchestration.bounded_document_inputs import load_retained_documents
    source=load_retained_documents(worker.config['autoresearch'],'2026-10-12')
    assert source.get('coverage')=={'complete':True}
    evidence=source['bounded_documents'][0]['evidence']
    def call(system,user,*,role='thesis',document_inputs=None,**kwargs):
        if 'Independent blind source verification'in system:
            output={'recognition':json.loads(payload['recognition_text']),'context_sufficient':True,'material_context_failures':[]}
        elif document_inputs is not None:output=json.loads(payload['recognition_text'])
        else:output=payload['thesis_response']
        text=json.dumps(output)
        worker.last_call_response={'received':True,'text':text}
        model='gpt-6-luna'if role=='bounded'else'gpt-6.1-sol'
        worker.last_call_provenance={'configured_model':model,'returned_model':model,'identity_status':'unpinned',
            'role':role,'billing_mode':'chatgpt_plan','reasoning_effort':'high'}
        if document_inputs is not None:
            from tradingagents.strategies.plan_document_inputs import encode_document_inputs,document_input_metadata
            worker.last_call_provenance['document_inputs']=document_input_metadata(encode_document_inputs(document_inputs))
        return text
    monkeypatch.setattr(worker,'_call_llm',call)
    result=worker.analyze_bounded_document_evidence(evidence,session_as_of='2026-10-12',ticker='MSFT',
        decision_scope=entry['decision_scope'],verification_clock=lambda:'2026-10-13T04:15:00+00:00')
    signal={'ticker':'MSFT','strategy':'bounded_document_research','direction':result['direction'],'score':result['score'],
        'journal_only':False,'metadata':{'analysis_status':'validated','bounded_document_decision_scope':entry['decision_scope'],
            'llm_analysis':result}}
    return worker.config,{'bounded_documents':source},signal


def test_actual_analyzer_new_policy_records_unmodified_completion(analyzer,tmp_path,monkeypatch):
    config,data,signal=analyzed(analyzer,tmp_path,monkeypatch)
    attempt=signal['metadata']['llm_analysis']['bounded_document_research']
    assert attempt['validation_status']=='validated'
    assert attempt['automatic_review']['reviewed_at']=='2026-10-13T04:15:00+00:00'
    assert attempt['decision_eligibility']['paper_decision_eligible']is False


def bound(analyzer,tmp_path,monkeypatch):
    config,data,signal=analyzed(analyzer,tmp_path,monkeypatch)
    sample={'30d':([signal],{},[])}
    binding=module().build_final_binding(config,date(2026,10,12),data,sample,'a'*64,
        datetime(2026,10,13,4,20,tzinfo=timezone.utc),'b'*64)
    return config,data,signal,sample,binding


def test_final_binding_replays_raw_stages_without_minting_new_review(analyzer,tmp_path,monkeypatch):
    config,data,signal,sample,binding=bound(analyzer,tmp_path,monkeypatch)
    before=deepcopy(signal)
    module().validate_final_signals(config,date(2026,10,12),data,[signal],{},'30d',binding)
    assert signal==before
    assert binding['cutoff']=='2026-10-13T04:20:00+00:00'
    assert binding['screening_receipt_sha256']=='b'*64

@pytest.mark.parametrize('damage',['missing','cutoff','reference','scope','response','result','retained','missing_signal'])
def test_final_boundary_rejects_tampering_and_no_authority_reuse(analyzer,tmp_path,monkeypatch,damage):
    config,data,signal,sample,binding=bound(analyzer,tmp_path,monkeypatch)
    if damage=='missing':binding=None
    elif damage=='cutoff':binding['cutoff']='2026-10-13T04:00:00+00:00'
    elif damage=='reference':binding['reference_session']='2026-10-13'
    elif damage=='scope':signal['metadata']['bounded_document_decision_scope']['purpose']='changed'
    elif damage=='response':signal['metadata']['llm_analysis']['bounded_document_research']['verification_attempt']['response_text']='{}'
    elif damage=='result':signal['direction']='short'
    elif damage=='retained':signal['metadata']['retained_from_signal_id']='old-signal'
    with pytest.raises(ValueError):
        module().validate_final_signals(config,date(2026,10,12),data,[]if damage=='missing_signal'else[signal],{},'30d',binding)

@pytest.mark.parametrize('cutoff',['2026-10-13T04:14:59+00:00','2026-10-13T13:30:00+00:00'])
def test_final_cutoff_cannot_precede_review_or_reach_open(analyzer,tmp_path,monkeypatch,cutoff):
    config,data,signal=analyzed(analyzer,tmp_path,monkeypatch)
    with pytest.raises(ValueError):
        module().build_final_binding(config,date(2026,10,12),data,{'30d':([signal],{},[])},'a'*64,
            datetime.fromisoformat(cutoff),'b'*64)

from test_research_screening import owner, SESSION
from test_research_decision_inputs import source, checkpoint, NOW, accepted_path, forbid
from tradingagents.strategies.orchestration import decision_clock as decision


def test_shared_final_binding_freezes_and_replays_without_new_clock(owner):
    owner._base_config['autoresearch'].update(CONFIG['autoresearch'])
    data,digest=source(owner,True);checkpoint(owner,digest)
    enrichment,context=decision.prepare_decision_inputs(owner,SESSION,data,digest,lambda:{},now=lambda:NOW)
    assert context['document_review_binding']['screening_receipt_sha256']
    assert set(context['document_review_binding']['horizons'])=={'1y','30d','3m','6m'}
    before=accepted_path(owner).read_bytes()
    assert decision.prepare_decision_inputs(owner,SESSION,data,digest,forbid,now=forbid)==(enrichment,context)
    assert accepted_path(owner).read_bytes()==before
    altered=deepcopy(context);altered.pop('document_review_binding')
    with pytest.raises(ValueError):decision.validate_context(owner._base_config,SESSION,altered)


def test_missing_final_binding_cannot_recreate_after_committee(owner):
    owner._base_config['autoresearch'].update(CONFIG['autoresearch'])
    data,digest=source(owner,True);checkpoint(owner,digest)
    cohort=owner.cohorts[0]
    cohort['ledger'].record_committee_decision(SESSION,owner._epoch_id,'paper-30d',{'hold':True})
    with pytest.raises(ValueError,match='decision inputs'):
        decision.prepare_decision_inputs(owner,SESSION,data,digest,forbid,now=lambda:NOW)

@pytest.mark.parametrize('damage',['returned_model','primary_image'])
def test_resealed_native_provenance_tamper_still_rejects(analyzer,tmp_path,monkeypatch,damage):
    config,data,signal=analyzed(analyzer,tmp_path,monkeypatch)
    attempt=signal['metadata']['llm_analysis']['bounded_document_research']
    if damage=='returned_model':attempt['thesis_attempt']['model_provenance']['returned_model']='gpt-4'
    else:attempt['recognition_attempt']['model_provenance']['document_inputs'][0]['sha256']='f'*64
    attempt['receipt_sha256']=eligibility._digest({k:v for k,v in attempt.items()if k!='receipt_sha256'})
    with pytest.raises(ValueError):
        module().build_final_binding(config,date(2026,10,12),data,{'30d':([signal],{},[])},'a'*64,
            datetime(2026,10,13,4,20,tzinfo=timezone.utc),'b'*64)


def test_invalid_new_clock_fails_before_any_model(analyzer,monkeypatch):
    worker,_,_=analyzer
    worker.config['autoresearch']['bounded_document_decision_policy']=V2
    worker.config['execution']={'mode':'paper','execution_enabled':False}
    monkeypatch.setattr(worker,'_call_llm',lambda *a,**k:pytest.fail('invalid policy must not invoke model'))
    from test_bounded_document_research import evidence
    with pytest.raises(ValueError):
        worker.analyze_bounded_document_evidence(evidence(),session_as_of='2026-10-12',ticker='ACME',decision_scope={})


def test_successful_source_cannot_silently_disappear_from_final_sample(analyzer,tmp_path,monkeypatch):
    config,data,signal=analyzed(analyzer,tmp_path,monkeypatch)
    with pytest.raises(ValueError,match='coverage'):
        module().build_final_binding(config,date(2026,10,12),data,{'30d':([],{},[])},'a'*64,
            datetime(2026,10,13,4,20,tzinfo=timezone.utc),'b'*64)


def test_scope_must_remain_effective_at_actual_final_cutoff(analyzer,tmp_path,monkeypatch):
    config,data,signal=analyzed(analyzer,tmp_path,monkeypatch,effective_until='2026-10-13T04:16:00+00:00')
    with pytest.raises(ValueError,match='window'):
        module().build_final_binding(config,date(2026,10,12),data,{'30d':([signal],{},[])},'a'*64,
            datetime(2026,10,13,4,20,tzinfo=timezone.utc),'b'*64)


def test_result_enrichment_reads_actual_profile_ledger_policy_not_health_policy(owner,monkeypatch):
    from tradingagents.strategies.orchestration import daily_pipeline, outcome_evidence
    policy_id='generation-config-'+'c'*64
    owner._policy_id_for_horizon=lambda h:policy_id+':health:'+h
    for cohort in owner.cohorts:
        cohort['engine'].ar_config['paper_ledger']['policy_id']=policy_id
        cohort['executor']=object()
        cohort['ledger'].record_committee_decision(SESSION,owner._epoch_id,policy_id,
            {'status':{'status':'selected','degraded':True}})
    state=daily_pipeline.DailyRunState(owner,SESSION.isoformat(),SESSION,NOW,owner._epoch_id)
    state.candidate_issues_hydrated=True
    monkeypatch.setattr(daily_pipeline,'apply_source_coverage',lambda *a:None)
    monkeypatch.setattr(outcome_evidence,'outcome_coverage',lambda *a:{'valid':True,'failures':[]})
    result=daily_pipeline.finalize_daily_results(state,{c['config'].name:{'error':False}for c in owner.cohorts})
    assert all(v['committee_decision_status']=={'status':'selected','degraded':True}and v['degraded']is True for v in result.values())
