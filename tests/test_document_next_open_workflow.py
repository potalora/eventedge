"""Constructed transport, real intake/analysis/final cutoff/16 ledger boundaries."""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest
from test_source_reliability_pipeline import pipeline, run_campaign, accepted_input_bytes, GENERATION
from test_bounded_document_workflow import document_pipeline, _document_signals
from test_document_decision_workflow import automatic_decision_payload, _boundaries_and_protection
from tradingagents.strategies.document_review_clock import POLICY
from tradingagents.strategies.orchestration import decision_clock

pytestmark=pytest.mark.scheduled_acceptance


def next_open_payload(repo):
    # Construct current V4 request-bound responses at the external fixture boundary.
    payload=automatic_decision_payload(repo)
    from tradingagents.strategies.orchestration.bounded_document_inputs import load_retained_documents
    from tradingagents.strategies.bounded_document_research import prepare_recognition, SCOPED_REQUEST_VERSION
    source=load_retained_documents({'bounded_document_research_policy':'bounded_document_research_v1',
        'bounded_document_registry':[payload['registry_entry']]},'2026-10-12')
    request=prepare_recognition(source['bounded_documents'][0]['evidence'],session_as_of='2026-10-12',request_version=SCOPED_REQUEST_VERSION)
    raw=json.loads(payload['recognition_text']);raw['request_identity_sha256']=json.loads(request.context_json)['request_identity_sha256']
    payload['recognition_text']=json.dumps(raw);payload['verification_response']['recognition']=deepcopy(raw)
    import hashlib
    payload.update(system_sha256=hashlib.sha256(request.system.encode()).hexdigest(),user_sha256=hashlib.sha256(request.user.encode()).hexdigest())
    return payload


def enable(document_pipeline,monkeypatch):
    fixture,owner,config,repo=document_pipeline
    for target in (config,owner._base_config,*[c['engine'].config for c in owner.cohorts]):
        target['autoresearch'].update(bounded_document_decision_policy=POLICY,
            decision_clock_policy='prospective_next_open_v1',
            bounded_document_recognition_request_version='bounded-document-recognition-request-v4')
    import hashlib
    profile={'autoresearch':{k:deepcopy(v)for k,v in config['autoresearch'].items()if k in
        ('bounded_document_decision_policy','decision_clock_policy','research_resilience_policy',
         'bounded_document_research_policy','bounded_document_registry','bounded_document_recognition_request_version','llm_temperature')}}
    profile_path=Path(config['autoresearch']['state_dir'])/'generation-config.json'
    profile_bytes=json.dumps(profile,sort_keys=True).encode();profile_path.write_bytes(profile_bytes)
    profile_digest=hashlib.sha256(profile_bytes).hexdigest()
    for target in (config,owner._base_config,*[c['engine'].config for c in owner.cohorts]):
        target['autoresearch'].setdefault('paper_ledger',{})['policy_id']='generation-config-'+profile_digest
    manifest_path=repo/'data/generations/manifest.json'
    manifest=json.loads(manifest_path.read_text())
    manifest['generations'][0].update(config_file=str(profile_path),config_sha256=hashlib.sha256(profile_bytes).hexdigest())
    manifest_path.write_text(json.dumps(manifest))
    fixture.now=datetime(2026,10,13,4,20,tzinfo=timezone.utc)
    monkeypatch.setattr(decision_clock,'utc_now',lambda:fixture.now)
    _boundaries_and_protection(document_pipeline,monkeypatch)
    # Existing constructed thesis boundary omits billing provenance; native transport supplies it.
    from tradingagents.strategies import llm_utils
    original_model=llm_utils.call_analysis_model
    def model(*args,**kwargs):
        result=original_model(*args,**kwargs)
        if kwargs.get('provenance') is not None:
            kwargs['provenance']['billing_mode']='chatgpt_plan'
        return result
    monkeypatch.setattr(llm_utils,'call_analysis_model',model)
    return fixture,owner,config,repo


@pytest.mark.parametrize('document_pipeline',[next_open_payload],indirect=True)
def test_midnight_review_requires_shared_final_clock_in_all_sixteen_books_and_replay(document_pipeline,monkeypatch):
    fixture,owner,config,repo=enable(document_pipeline,monkeypatch)
    result,wire,report=run_campaign(document_pipeline,date(2026,10,12))
    assert report['staging_complete'] and report['accounting_valid']
    assert report['document_review_clock']['valid']is True
    assert len(fixture.scoped_committee_inputs)==16
    documents=_document_signals(config)
    assert len(documents)==4
    assert all(row['metadata']['llm_analysis']['decision_eligibility']['paper_decision_eligible']is False for row in documents)
    assert all(row['metadata']['llm_analysis']['decision_eligibility']['reviewed_at'].startswith('2026-10-13T04:20:')for row in documents)
    contexts=[c['ledger'].read_policy_session_context(date(2026,10,12))['context']['decision_clock']for c in owner.cohorts]
    assert all(context==contexts[0]for context in contexts)
    assert 'document_review_binding'in contexts[0]
    assert set(contexts[0]['document_review_binding']['horizons'])=={'1y','30d','3m','6m'}
    original=accepted_input_bytes(config);calls=fixture.model_calls.copy()
    fixture.block_all=True
    repeated,_,_=run_campaign(document_pipeline,date(2026,10,12))
    assert repeated['outcome']==result['outcome'] and fixture.model_calls==calls
    assert accepted_input_bytes(config)==original
    # A source downgrade cannot hide an already-bound V2 ledger clock from reporting.
    import hashlib
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    from tradingagents.strategies.orchestration.operational_report import build_operational_report
    state=Path(config['autoresearch']['state_dir'])
    source_path=next((state/'source_inputs').glob('*.json'))
    source_before=source_path.read_text()
    source_envelope=SourceInputStore.decode(source_before)
    source_scope=source_envelope['payload']['bounded_documents']['bounded_document_scope']
    source_scope['decision_policy']='verified_scoped_claims_v1'
    source_scope.pop('review_clock_config')
    source_envelope['digest']=hashlib.sha256(SourceInputStore.encode(source_envelope['payload']).encode()).hexdigest()
    source_path.write_text(SourceInputStore.encode(source_envelope))
    downgraded=build_operational_report(repo,GENERATION,'2026-10-12',snapshot_guaranteed=True)
    source_path.write_text(source_before)
    assert not downgraded['evidence_complete']
    assert any(row['code']=='document_review_clock_invalid'for row in downgraded['diagnostics'])
    # Tamper the accepted final payload after all effects; reporting/replay must refuse.
    import hashlib
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    state=Path(config['autoresearch']['state_dir'])
    path=next((state/'source_inputs'/'decision_inputs').glob('*.json'))
    envelope=SourceInputStore.decode(path.read_text())
    envelope['payload']['context'].pop('document_review_binding')
    envelope['digest']=hashlib.sha256(SourceInputStore.encode(envelope['payload']).encode()).hexdigest()
    path.write_text(SourceInputStore.encode(envelope))
    from tradingagents.strategies.orchestration.operational_report import build_operational_report
    damaged=build_operational_report(repo,GENERATION,'2026-10-12',snapshot_guaranteed=True)
    assert not damaged['evidence_complete']
    assert any(row['code']=='document_review_clock_invalid'for row in damaged['diagnostics'])
    with pytest.raises(ValueError):
        decision_clock.validate_completed_replay(owner,date(2026,10,12),owner._epoch_id,owner.cohorts)


@pytest.mark.parametrize('document_pipeline',[next_open_payload],indirect=True)
@pytest.mark.parametrize('damage',['missing','minted','profile_drift','identity_commit','identity_configuration'])
def test_direct_staging_rejects_provisional_without_final_map_before_committee(document_pipeline,monkeypatch,damage):
    fixture,owner,config,repo=enable(document_pipeline,monkeypatch)
    original=decision_clock.prepare_decision_inputs
    def strip(*args,**kwargs):
        enrichment,context=original(*args,**kwargs)
        context=deepcopy(context)
        if damage=='missing':context.pop('document_review_binding')
        elif damage.startswith('identity_'):
            from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
            path=next((Path(config['autoresearch']['state_dir'])/'source_inputs'/'decision_inputs').glob('*.json'))
            envelope=SourceInputStore.decode(path.read_text())
            field=damage.removeprefix('identity_')
            envelope['identity'][field]='f'*(40 if field=='commit' else 64)
            path.write_text(SourceInputStore.encode(envelope))
        elif damage=='profile_drift':
            path=Path(config['autoresearch']['state_dir'])/'generation-config.json'
            path.write_bytes(path.read_bytes()+b' ')
        else:
            from datetime import timedelta
            from tradingagents.strategies.document_decision_eligibility import _digest
            context['cutoff']=(datetime.fromisoformat(context['cutoff'])+timedelta(seconds=1)).isoformat()
            context['document_review_binding']['cutoff']=context['cutoff']
            binding=context['document_review_binding']
            binding['receipt_sha256']=_digest({k:v for k,v in binding.items()if k!='receipt_sha256'})
            fixture.now+=timedelta(seconds=2)
        return enrichment,context
    monkeypatch.setattr(decision_clock,'prepare_decision_inputs',strip)
    result=fixture.manager.run_daily('2026-10-12')[GENERATION]
    assert result['outcome']=='failed'
    assert fixture.model_calls['committee']==0
    assert all(c['ledger'].committee_decision(date(2026,10,12),owner._epoch_id,
        str(c['engine'].ar_config.get('paper_ledger',{}).get('policy_id','foundation-'+c['config'].horizon)))is None for c in owner.cohorts)


def insufficient_context_payload(repo):
    payload=next_open_payload(repo)
    payload['verification_response'].update(context_sufficient=False,material_context_failures=['Required context absent.'])
    return payload


@pytest.mark.parametrize('document_pipeline',[insufficient_context_payload],indirect=True)
def test_semantic_failure_stays_failed_without_sol_or_document_entry(document_pipeline,monkeypatch):
    fixture,owner,config,repo=enable(document_pipeline,monkeypatch)
    result,wire,report=run_campaign(document_pipeline,date(2026,10,12))
    assert report['accounting_valid'] and report['staging_complete']
    assert any(row['strategy']=='bounded_document_research'for row in report['source_health_failures'])
    assert fixture.model_calls['document_thesis']==0
    assert all(row['metadata']['analysis_status']=='failed' and row['journal_only']is True for row in _document_signals(config))
    assert all(not any(row.get('strategy')=='bounded_document_research'for row in admitted)for admitted in fixture.scoped_committee_inputs)
    assert report['document_review_clock']['failed_document_decisions']==4
