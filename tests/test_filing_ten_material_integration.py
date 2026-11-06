"""Ten exact permissions stay bound across hydration, projection and IPC."""
from copy import deepcopy
import time

import pytest

from filing_ten_material_fixtures import TEN_POLICY, framed, identity, original
from test_filing_hydration import Source, evidence, hydrate, row
from test_filing_material_dispatch import Response
from tradingagents.strategies.data_sources import filing_material_policy as material
from tradingagents.strategies.data_sources import filing_acquisition as acquisition
from tradingagents.strategies.data_sources import filing_parser_dispatch as dispatch
from tradingagents.strategies.data_sources.filing_spool import submission_spool_scope
from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.orchestration.filing_material_validation import validate_filing_material_policy, signal_edgar

CONFIG={'filing_material_policy':TEN_POLICY,'filing_evidence_policy':'complete_submission_v1',
    'filing_acquisition_policy':'bounded_original_submission_v1','filing_parser_policy':'two_processes_v1'}


def prepared(*,broken=False,duplicate=False,wrong_envelope=False):
    records=[];envelopes={}
    for index in range(10):
        value=identity(index);frame,raw=framed(index)
        record={'adsh':value['accession'],'form_type':value['form'],'file_date':value['filing_date'],
            'file_url':value['source_url'],'ciks':[frame['roles'][0]['cik']]}
        records.append(record)
        envelopes[value['accession']]=material.quarantine_evidence(frame,submission_size=len(raw),policy=material.POLICY if wrong_envelope and index==0 else TEN_POLICY)
    ordinary=row(700);records.append(ordinary);source=Source(records);source.overrides.update(envelopes)
    if broken:
        bad=evidence(ordinary);bad.update(structural_status='insufficient',issues=[{'code':'missing_required_dependency'}]);source.overrides[ordinary['adsh']]=bad
    graph=hydrate(source,{'filings':records,'pqc_filings':[deepcopy(records[3])] if duplicate else [],'activist_13d':[],'passive_13g':[]},material_policy=TEN_POLICY)
    return {**graph['collections'],'filing_evidence':{k:v for k,v in graph.items() if k!='collections'}},source


def test_ten_hydration_scoped_status_projection_nonuse_and_prior_blocking():
    edgar,source=prepared(duplicate=True);before=deepcopy(edgar)
    scope=validate_filing_material_policy({'edgar':edgar},CONFIG)
    assert scope['policy']==TEN_POLICY and scope['strict_failed_rows']==scope['quarantined_rows']==11
    assert scope['strict_complete'] is False and scope['scoped_complete'] is True and scope['scoped_failed_rows']==0
    assert len(scope['approved_accessions'])==len(scope['quarantined_accessions'])==10
    assert source.history_calls==['0001512228'] and source.archive_calls==[]
    assert edgar['filings'][0]['prior_status']=='not_assessed_material_quarantine'
    assert all(r.get('prior_evidence_ref') is None for r in edgar['filings'][:10])
    projected=signal_edgar(edgar,scope)
    assert len(projected['filings'])==1 and projected['pqc_filings']==[]
    assert set(projected['filing_evidence']['corpus'])=={row(700)['adsh']} and edgar==before


def test_eleventh_incomplete_row_remains_a_failure_under_ten_permission():
    edgar,_=prepared(broken=True)
    with pytest.raises(ValueError):validate_filing_material_policy({'edgar':edgar},CONFIG)
    edgar['error']='full filing evidence incomplete';scope=validate_filing_material_policy({'edgar':edgar},CONFIG)
    assert scope['scoped_complete'] is False and scope['scoped_failed_rows']==1 and scope['quarantined_rows']==10


@pytest.mark.parametrize('damage',['config','coverage','scope','envelope'])
def test_cross_policy_evidence_configuration_scope_and_hash_cannot_mix(damage):
    edgar,_=prepared();config=dict(CONFIG);graph=edgar['filing_evidence']
    if damage=='config':config['filing_material_policy']=material.POLICY
    elif damage=='coverage':graph['coverage']['material_policy']=material.POLICY
    elif damage=='scope':graph['material_scope']['policy']=material.POLICY
    else:
        frame,raw=framed(0);graph['corpus'][identity(0)['accession']]=material.quarantine_evidence(frame,submission_size=len(raw))
    with pytest.raises(ValueError):validate_filing_material_policy({'edgar':edgar},config)


def test_hydration_rejects_an_old_policy_envelope_in_ten_mode():
    edgar,_=prepared(wrong_envelope=True)
    assert identity(0)['accession'] not in edgar['filing_evidence']['corpus']
    assert edgar['filing_evidence']['material_scope']['scoped_failed_rows']==1
    with pytest.raises(ValueError):validate_filing_material_policy({'edgar':edgar},CONFIG)


@pytest.mark.parametrize('index',range(10))
def test_ten_real_two_child_acquisition_retains_exact_explicit_policy(tmp_path,monkeypatch,index):
    value=identity(index);response=Response(original(index),value['source_url'])
    monkeypatch.setattr(acquisition,'provider_request',lambda *a,**k:response)
    deadline=time.monotonic()+15
    with provider_budget('edgar',deadline,limits=()):
        with submission_spool_scope(tmp_path,original_deadline=deadline) as spool:
            with dispatch.parser_scope() as owner:
                result=acquisition.acquire_complete_submission('offline',value['source_url'],accession=value['accession'],
                    form_type=value['form'],filing_date=value['filing_date'],material_policy=TEN_POLICY)
                assert len(owner.children)==2 and spool.stats()['active_child_copies']==spool.stats()['total_bytes']==0
                assert result['material_quarantine']['policy']==TEN_POLICY and result['units']==[]
                material.validate_quarantined_evidence(result,policy=TEN_POLICY)
    assert response.closed


@pytest.mark.parametrize('index',[3,7])
def test_old_acquisition_policy_never_quarantines_a_new_identity(tmp_path,monkeypatch,index):
    value=identity(index);response=Response(original(index),value['source_url'])
    monkeypatch.setattr(acquisition,'provider_request',lambda *a,**k:response);deadline=time.monotonic()+15
    with provider_budget('edgar',deadline,limits=()):
        with submission_spool_scope(tmp_path,original_deadline=deadline):
            with dispatch.parser_scope():
                kwargs={'accession':value['accession'],'form_type':value['form'],'filing_date':value['filing_date'],'material_policy':material.POLICY}
                if value['form']=='DEF 14A':
                    with pytest.raises(SourceFetchError):acquisition.acquire_complete_submission('offline',value['source_url'],**kwargs)
                else:
                    result=acquisition.acquire_complete_submission('offline',value['source_url'],**kwargs)
                    assert 'material_quarantine' not in result and result['structural_status']=='complete'


@pytest.mark.parametrize('damage',['policy','manifest','fully_relabel'])
def test_ten_parent_rejects_cross_policy_child_result_and_closes_copies(tmp_path,monkeypatch,damage):
    read_frame=dispatch._read_frame
    def altered(*args,**kwargs):
        response=read_frame(*args,**kwargs);declaration=response['result']['material_quarantine']
        if damage in {'policy','fully_relabel'}:declaration['policy']=material.POLICY
        if damage in {'manifest','fully_relabel'}:declaration['policy_manifest_sha256']=material.policy_manifest_sha256()
        return response
    monkeypatch.setattr(dispatch,'_read_frame',altered)
    value=identity(0);response=Response(original(0),value['source_url'])
    monkeypatch.setattr(acquisition,'provider_request',lambda *a,**k:response);deadline=time.monotonic()+15
    with provider_budget('edgar',deadline,limits=()):
        with submission_spool_scope(tmp_path,original_deadline=deadline) as spool:
            with dispatch.parser_scope() as owner:
                with pytest.raises(SourceFetchError):
                    acquisition.acquire_complete_submission('offline',value['source_url'],accession=value['accession'],
                        form_type=value['form'],filing_date=value['filing_date'],material_policy=TEN_POLICY)
                assert owner._closed and not owner._child_leases
                assert spool.stats()['active_child_copies']==spool.stats()['total_bytes']==0
                assert all(child.poll() is not None for child in owner.children)


@pytest.mark.parametrize('damage',['envelope','health'])
def test_ten_codec_replay_revalidates_policy_and_all_horizon_health(monkeypatch,damage):
    from datetime import datetime,timezone
    from test_filing_material_report_replay import replay_owner,replay_health,SESSION
    from tradingagents.strategies.orchestration import scoped_replay,filing_acquisition_validation
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    # Synthetic hydration is isolated from acquisition attestation only; real
    # material validation, codec and per-horizon replay checks remain active.
    monkeypatch.setattr(filing_acquisition_validation,'validate_filing_acquisition_policy',lambda *a:None)
    monkeypatch.setattr(scoped_replay,'validate_portfolio_targets',lambda *a:None)
    monkeypatch.setattr(scoped_replay,'source_scope_evidence',lambda *a,**k:({}, {}, {}))
    edgar,_=prepared(duplicate=True)
    data=SourceInputStore.decode(SourceInputStore.encode({'edgar':edgar}))
    assert data=={'edgar':edgar}
    scope=validate_filing_material_policy(data,CONFIG);records=replay_health();owner=replay_owner(records)
    owner._base_config={'autoresearch':deepcopy(CONFIG)}
    for record in records:record.evidence['filing_material_scope']=deepcopy(scope)
    def replay():
        scoped_replay.validate_replay_source_scopes(data,owner,SESSION,'epoch',now=datetime(2026,10,10,tzinfo=timezone.utc))
    replay()
    if damage=='envelope':
        declaration=data['edgar']['filing_evidence']['corpus'][identity(0)['accession']]['material_quarantine']
        declaration.update(policy=material.POLICY,policy_manifest_sha256=material.policy_manifest_sha256())
    else:records[-1].evidence['filing_material_scope']['policy']=material.POLICY
    with pytest.raises(ValueError):replay()
