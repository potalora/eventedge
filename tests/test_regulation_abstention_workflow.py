"""Constructed transport through16 native books; no native semantic claim."""
from copy import deepcopy
from datetime import date,datetime,timezone
import hashlib
import json
from pathlib import Path
import pytest
from test_source_reliability_pipeline import pipeline,run_campaign,accepted_input_bytes
from test_regulation_evidence import fixture as rule_fixture,DOC
from test_regulation_abstention import POLICY
from regulation_plan_fixtures import inert_plan_identity

pytestmark=pytest.mark.scheduled_acceptance


@pytest.mark.parametrize('runtime_policy',['legacy','compact_budget'])
@pytest.mark.parametrize('profile_damage',['bytes','missing_evidence','wrong_retrieval'])
@pytest.mark.parametrize('disposition',['issuer','supporting'])
def test_abstention_completes_all_books_honest_report_and_frozen_replay(pipeline,monkeypatch,profile_damage,disposition,runtime_policy,inert_plan_identity):
    fixture,owner,config,repo=pipeline
    _,detail,bodies=rule_fixture(attachment=True)
    if disposition=='supporting':
        bodies[DOC]=bodies[DOC].replace(b'</h1>',b'</h1><p>See Supporting Technical Analysis in the separate EPA docket.</p>')
        detail['data']['attributes']['fileFormats'][0]['size']=len(bodies[DOC])
    original_http,original_model=fixture.http,fixture.model
    def http(method,url,**kwargs):
        if fixture.block_all:return original_http(method,url,**kwargs)
        if url=='https://api.regulations.gov/v4/documents':
            rows=[{'id':DOC,'attributes':detail['data']['attributes']}] if kwargs['params'].get('filter[agencyId]')=='EPA'else[]
            return fixture.response({'data':rows,'meta':{'totalPages':1,'totalElements':len(rows)}})
        if url=='https://api.regulations.gov/v4/documents/'+DOC:return fixture.response(detail)
        if url.startswith('https://downloads.regulations.gov/'):
            return fixture.response(bodies[url.split('/')[-2]])
        return original_http(method,url,**kwargs)
    def model(client,*,system,prompt,**kwargs):
        payload=json.loads(prompt)if prompt.startswith('{')else{}
        if payload.get('contract_version')=='regulation-assessment-v3':
            fixture.model_calls['regulation_abstention']+=1
            unit=payload['units'][0]
            text=json.dumps({'contract_version':'regulation-assessment-v3','document_id':DOC,
                'outcome':'assessed_abstention','direction':None,'conviction':None,
                'target_ticker':None,'target_binding_sha256':None,
                'rationale':'Complete supplied material assessed; no issuer trade because authoritative relationship is unestablished.',
                'evidence_claim':unit['text'][:30],
                'citations':[{'unit_id':u['unit_id'],'quote':u['text'][:15]}for u in payload['units']],
                'unresolved_material_dependencies':[],
                'unresolved_decision_dependencies':[{'kind':'issuer_applicability','reference':'Covered operation issuer bridge',
                    'reason':'A rule name is not a verified listed issuer relationship.',
                    'unit_id':unit['unit_id'],'quote':unit['text'][:30]}]})
            if disposition=='supporting':
                value=json.loads(text);reference='Supporting Technical Analysis'
                dependency={'kind':'external_supporting_material','reference':reference,
                    'reason':'Separate supporting analysis unavailable; no issuer trading authority.',
                    'unit_id':unit['unit_id'],'quote':unit['text'][unit['text'].index(reference):][:70]}
                value['unresolved_decision_dependencies']=[dependency]
                value['unresolved_material_dependencies']=[{k:dependency[k]for k in('reference','reason')}]
                text=json.dumps(value)
            if kwargs.get('response_observer') is not None:kwargs['response_observer'](text)
            return text
        return original_model(client,system=system,prompt=prompt,**kwargs)
    monkeypatch.setattr(fixture,'http',http);monkeypatch.setattr(fixture,'model',model)
    from tradingagents.strategies import llm_utils
    monkeypatch.setattr(llm_utils,'call_analysis_model',model)
    for target in(config,owner._base_config,*[c['engine'].config for c in owner.cohorts]):
        target['autoresearch'].update(regulation_evidence_policy='complete_document_text_v1',regulation_assessment_policy=POLICY)
    profile={'autoresearch':{'regulation_evidence_policy':'complete_document_text_v1','regulation_assessment_policy':POLICY}}
    if runtime_policy=='compact_budget':
        from test_regulation_prompt_budget_integration import config as compact_config
        profile=compact_config()
        profile['autoresearch']['chatgpt_plan_auth_path']=inert_plan_identity.auth_path
        for target in(config,owner._base_config,*[c['engine'].config for c in owner.cohorts]):
            target['autoresearch'].update(profile['autoresearch'])
            target.setdefault('execution',{}).update(profile['execution'])
    state=Path(config['autoresearch']['state_dir']);path=state/'generation-config.json'
    raw=json.dumps(profile,sort_keys=True).encode();path.write_bytes(raw)
    manifest_path=state.parent/'manifest.json';manifest=json.loads(manifest_path.read_text())
    manifest['generations'][0].update(config_file=str(path),config_sha256=hashlib.sha256(raw).hexdigest());manifest_path.write_text(json.dumps(manifest))
    fixture.session=date(2026,10,9);fixture.now=datetime(2026,10,9,22,tzinfo=timezone.utc)
    result,wire,report=run_campaign(pipeline,fixture.session)
    assert result['outcome']=='clean' and report['accounting_valid'] and report['staging_complete']
    assert len(report['cohorts'])==16 and fixture.model_calls['regulation_abstention']==1
    assert bool(inert_plan_identity.reads) is (runtime_policy=='compact_budget')
    coverage=report['sources']['regulation_analysis_coverage']
    assert coverage['analysis_complete'] and all(row['abstained_documents']==1 for row in coverage['horizons'])
    assert coverage['assessment_complete'] and not coverage['decision_evidence_complete']
    assert report['performance_claims_withheld'] is True
    if disposition=='supporting':
        assert all(row['abstentions'][0]['decision_evidence_status']=='unresolved_supporting_material'
            and row['abstentions'][0]['unresolved_material_dependencies']for row in coverage['horizons'])
    assert not any(s.get('strategy')=='regulatory_pipeline'for book in wire.values()for s in book['signals'])
    before=accepted_input_bytes(config);calls=fixture.model_calls.copy();fixture.block_all=True
    replay,_,replayed=run_campaign(pipeline,fixture.session)
    assert replay['outcome']=='clean' and replayed['sources']['regulation_analysis_coverage']==coverage
    assert fixture.model_calls==calls and accepted_input_bytes(config)==before and not fixture.blocked_calls
    # Independent profile pin, not assessment self-description, controls report acceptance.
    from tradingagents.strategies.orchestration.operational_report import build_operational_report
    if profile_damage=='bytes':path.write_bytes(raw+b' ')
    else:
        changed=deepcopy(profile)
        if profile_damage=='missing_evidence':changed['autoresearch'].pop('regulation_evidence_policy')
        else:changed['autoresearch']['regulation_retrieval_policy']=('verified_government_copy_v2'
            if runtime_policy=='compact_budget'else'verified_government_copy_v3')
        altered=json.dumps(changed,sort_keys=True).encode();path.write_bytes(altered)
        manifest=json.loads(manifest_path.read_text());manifest['generations'][0]['config_sha256']=hashlib.sha256(altered).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
    damaged=build_operational_report(repo,'gen_099',str(fixture.session),snapshot_guaranteed=True)
    assert not damaged['evidence_complete']
    assert any(row['code']=='regulation_evidence_conflict'for row in damaged['diagnostics'])


@pytest.mark.parametrize('damage',[None,'wrong_execution','unknown_graphics'])
def test_report_rejects_invalid_full_graphics_policy_from_real_frozen_profile(pipeline,monkeypatch,damage):
    fixture,owner,config,repo=pipeline
    # Empty regulation corpus still requires a valid explicit V8 profile. This
    # is an actual frozen-file binding check, not a model category exemption.
    profile={'autoresearch':{'graphics_execution_policy':'single_target_compact_v1','regulation_prompt_budget_policy':'o200k_base_planning_v1',
        'regulation_evidence_policy':'complete_document_text_v1','regulation_retrieval_policy':'verified_government_copy_v3',
        'model_runtime_policy':'full_regulation_graphics_v1','llm_billing_mode':'chatgpt_plan','autoresearch_model':'gpt-6-luna',
        'llm_effort':'high','thesis_model':'gpt-6.1-sol','thesis_effort':'high','llm_temperature':0.0},
        'execution':{'mode':'paper','execution_enabled':False}}
    # Generate an otherwise unchanged valid native-shaped constructed campaign.
    result,wire,report=run_campaign(pipeline)
    assert report['evidence_complete']
    from test_scheduled_regulation_report import empty_regulations
    from test_prospective_replay_integrity import rewrite_payload
    from tradingagents.strategies.orchestration.regulation_inputs import validate_regulation_policy
    import sqlite3
    state=Path(config['autoresearch']['state_dir'])
    regulations=empty_regulations();regulations['regulation_evidence']['retrieval_policy']='verified_government_copy_v3'
    from tradingagents.strategies.data_sources.regulation_evidence import digest
    graph=regulations['regulation_evidence'];graph['manifest_sha256']=digest({k:v for k,v in graph.items()if k!='manifest_sha256'})
    scope=validate_regulation_policy({'regulations':regulations},profile['autoresearch'])
    rewrite_payload(next((state/'source_inputs').glob('*.json')),lambda payload:payload.update(regulations=regulations))
    with sqlite3.connect(state/'metrics_v2.sqlite3')as db:
        for ident,raw in db.execute('SELECT health_id,payload_json FROM strategy_health').fetchall():
            health=json.loads(raw)
            if health['strategy']in('regulatory_pipeline','commodity_macro'):
                health['evidence'].update(regulation_source_coverage=scope,regulation_assessments=[])
                db.execute('UPDATE strategy_health SET payload_json=? WHERE health_id=?',(json.dumps(health),ident))
    if damage=='wrong_execution':profile['execution']['execution_enabled']=True
    elif damage=='unknown_graphics':profile['autoresearch']['graphics_execution_policy']='unknown'
    path=state/'generation-config.json';raw=json.dumps(profile,sort_keys=True).encode();path.write_bytes(raw)
    manifest_path=state.parent/'manifest.json';manifest=json.loads(manifest_path.read_text())
    manifest['generations'][0].update(config_file=str(path),config_sha256=hashlib.sha256(raw).hexdigest());manifest_path.write_text(json.dumps(manifest))
    from tradingagents.strategies.orchestration.operational_report import build_operational_report
    rebuilt=build_operational_report(repo,'gen_099','2026-10-02',snapshot_guaranteed=True)
    if damage is None:
        assert rebuilt['sources']['regulation_analysis_coverage']['analysis_complete'] is True
        assert not any(row['code']=='regulation_evidence_conflict'for row in rebuilt['diagnostics'])
    else:
        assert not rebuilt['evidence_complete']
        assert any(row['code']=='regulation_evidence_conflict'for row in rebuilt['diagnostics'])
