"""Acquisition owner receipts cannot be replaced by plausible supported copies."""
from copy import deepcopy
from datetime import datetime
import time

import pytest

from test_filing_evidence import OBSERVED
from test_filing_ownership_hydration import NativeSource, filing, record, supported
from test_filing_spool import Response
from tradingagents.strategies.data_sources import filing_spool
from tradingagents.strategies.data_sources.filing_acquisition import complete_submission_url
from tradingagents.strategies.orchestration.filing_acquisition_validation import (
    acquisition_scope, validate_filing_acquisition_policy,
)
from test_filing_acquisition_wiring import CONFIG as LEGACY_CONFIG

CONFIG={**LEGACY_CONFIG,'filing_material_policy':'verified_supported_documents_v1'}


def acquire(tmp_path,monkeypatch,*,reject_dependency=False):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls,tz=None):
            return datetime.fromisoformat(OBSERVED)
    monkeypatch.setattr(filing_spool,'datetime',FixedDatetime)
    base=filing(1,'2026-01-01'); current=filing(2,'2026-09-30',amendment=1)
    owner=filing_spool.SpoolScope(tmp_path,original_deadline=time.monotonic()+10,physical_limit=4*1024**3)
    class CapturedSource(NativeSource):
        def get_complete_submission(self,url,*,accession,form_type,filing_date,**kwargs):
            target=complete_submission_url(url,accession)
            owner.spool_response(Response((self.raws[accession],)),identity={
                'accession':accession,'form':form_type,'filing_date':filing_date,'source_url':target},max_bytes=1024*1024)
            return super().get_complete_submission(url,accession=accession,form_type=form_type,filing_date=filing_date,**kwargs)
    source=CapturedSource(base,current)
    if reject_dependency:
        raw=source.raws[base[0]['accession']]
        document=raw[raw.index(b'<DOCUMENT>'):raw.index(b'</DOCUMENT>')+len(b'</DOCUMENT>')]
        source.raws[base[0]['accession']]=raw.replace(b'<PUBLIC-DOCUMENT-COUNT>1',b'<PUBLIC-DOCUMENT-COUNT>2').replace(b'</SUBMISSION>',document+b'</SUBMISSION>')
    try:
        graph=supported(source,{'activist_13d':[record(current)]})
    finally:
        owner.close()
    graph['coverage'].update(acquisition_policy=LEGACY_CONFIG['filing_acquisition_policy'],parser_policy='two_processes_v1')
    graph['acquisition_scope']=acquisition_scope(owner)
    edgar={'filing_evidence':graph,**graph['collections']}
    if not graph['coverage']['complete']:
        edgar['error']='full filing evidence incomplete'
    return {'edgar':edgar},base[0]['accession'],current[0]['accession']


def test_exact_owner_originals_accept_supported_chain(tmp_path,monkeypatch):
    data,base,current=acquire(tmp_path,monkeypatch)
    result=validate_filing_acquisition_policy(data,CONFIG)
    assert result['completed_originals']==2 and result['spool_closed']
    graph=data['edgar']['filing_evidence']
    assert graph['supported_originals'][base] in graph['acquisition_scope']['originals']
    assert graph['supported_originals'][current] in graph['acquisition_scope']['originals']


def test_failed_dependency_parser_original_is_retained_as_failure(tmp_path,monkeypatch):
    data,base,current=acquire(tmp_path,monkeypatch,reject_dependency=True)
    graph=data['edgar']['filing_evidence']
    assert base not in graph['corpus'] and len(graph['acquisition_scope']['originals'])==2
    assert graph['ownership_acquisition_failures'][base]['failure']['code']=='source_failure'
    assert graph['ownership_lineages'][current]['status']=='acquisition_required'
    assert validate_filing_acquisition_policy(data,CONFIG)['completed_originals']==2
    assert graph['coverage']['complete'] is False and data['edgar']['error']


@pytest.mark.parametrize('damage',['omitted','empty_links','changed_current','changed_candidate','omitted_duty',
    'extra_target','current_target','unknown_code','empty_reason','bool_status','string_status','missing_error'])
def test_failed_dependency_exception_is_narrow_and_recomputed(tmp_path,monkeypatch,damage):
    data,base,current=acquire(tmp_path,monkeypatch,reject_dependency=True)
    graph=data['edgar']['filing_evidence']; failures=graph['ownership_acquisition_failures']; entry=failures[base]
    if damage=='omitted': del failures[base]
    elif damage=='empty_links': entry['required_by']=[]
    elif damage=='changed_current': entry['required_by'][0]['current_accession']=base
    elif damage=='changed_candidate': entry['required_by'][0]['candidate']=False
    elif damage=='omitted_duty': entry['required_by'][0]['obligations']=[]
    elif damage=='extra_target': failures['0001193125-26-999999']=deepcopy(entry)
    elif damage=='current_target': failures[current]=failures.pop(base)
    elif damage=='unknown_code': entry['failure']['code']='invented_success'
    elif damage=='empty_reason': entry['failure']['reason_code']=''
    elif damage=='bool_status': entry['failure']['http_status']=True
    elif damage=='string_status': entry['failure']['http_status']='422'
    else: data['edgar'].pop('error')
    with pytest.raises(ValueError,match='invalid_filing_acquisition_policy'):
        validate_filing_acquisition_policy(data,CONFIG)


@pytest.mark.parametrize('damage',['original_hash','original_size','original_observed','original_source',
    'proof_original','owner_copy_and_proof','cross_source','orphan','missing','missing_proof','proof_digest'])
def test_supported_originals_bind_exact_owner_and_source_proof(tmp_path,monkeypatch,damage):
    data,base,current=acquire(tmp_path,monkeypatch)
    graph=data['edgar']['filing_evidence']; receipts=graph['supported_originals']; proofs=graph['supported_source_proofs']
    if damage=='original_hash': receipts[current]['sha256']='a'*64
    elif damage=='original_size': receipts[current]['size']+=1
    elif damage=='original_observed': receipts[current]['observed_at']='2026-10-10T04:22:52+00:00'
    elif damage=='original_source': receipts[current]['identity']['source_url']=receipts[base]['identity']['source_url']
    elif damage=='proof_original': proofs[current]['original']=deepcopy(receipts[base])
    elif damage=='owner_copy_and_proof':
        forged=deepcopy(receipts[current]); forged['size']+=1
        receipts[current]=forged; proofs[current]['original']=deepcopy(forged)
    elif damage=='cross_source': receipts[current]=deepcopy(receipts[base])
    elif damage=='orphan': receipts['0001193125-26-999999']=deepcopy(receipts[current])
    elif damage=='missing': del receipts[current]
    elif damage=='missing_proof': del proofs[current]
    else: proofs[base]['evidence_sha256']='b'*64
    with pytest.raises(ValueError,match='invalid_filing_acquisition_policy'):
        validate_filing_acquisition_policy(data,CONFIG)


def test_legacy_cannot_declare_supported_originals(tmp_path,monkeypatch):
    from test_filing_acquisition_wiring import acquire as legacy_acquire
    graph,_=legacy_acquire(tmp_path,monkeypatch)
    graph['supported_originals']={}
    with pytest.raises(ValueError,match='invalid_filing_acquisition_policy'):
        validate_filing_acquisition_policy({'edgar':{'filing_evidence':graph}},LEGACY_CONFIG)


def test_consistently_resealed_source_original_still_must_equal_owner(tmp_path,monkeypatch):
    from tradingagents.strategies.data_sources.filing_supported_policy import _digest
    from tradingagents.strategies.orchestration.filing_supported_scope import build_supported_dispositions
    from tradingagents.strategies.orchestration.filing_material_validation import build_material_scope,validate_filing_material_policy
    data,_,current=acquire(tmp_path,monkeypatch)
    graph=data['edgar']['filing_evidence']
    forged=deepcopy(graph['supported_originals'][current]); forged['size']+=1
    graph['supported_originals'][current]=forged
    proof=graph['supported_source_proofs'][current]; proof['original']=deepcopy(forged)
    proof['source_proof_sha256']=_digest({k:v for k,v in proof.items() if k!='source_proof_sha256'})
    graph['supported_dispositions']=build_supported_dispositions(graph,graph['collections'])
    graph['material_scope']=build_material_scope(graph,graph['collections'])
    assert validate_filing_material_policy(data,CONFIG)['scoped_complete'] is True
    with pytest.raises(ValueError,match='invalid_filing_acquisition_policy'):
        validate_filing_acquisition_policy(data,CONFIG)


def test_failure_link_candidate_boolean_cannot_be_integer_alias(tmp_path,monkeypatch):
    data,base,_=acquire(tmp_path,monkeypatch,reject_dependency=True)
    data['edgar']['filing_evidence']['ownership_acquisition_failures'][base]['required_by'][0]['candidate']=1
    with pytest.raises(ValueError,match='invalid_filing_acquisition_policy'):
        validate_filing_acquisition_policy(data,CONFIG)


def test_cap_omission_cannot_make_forged_lineage_authorize_orphan_dependency(tmp_path,monkeypatch):
    from tradingagents.strategies.orchestration.filing_material_validation import build_material_scope,validate_filing_material_policy
    data,base,current=acquire(tmp_path,monkeypatch,reject_dependency=True)
    graph=data['edgar']['filing_evidence']; receipt=graph['ownership_lineages'][current]
    fake='0001193125-26-999999'
    receipt['candidate_accessions'].append(fake)
    obligation={'kind':'complete_submission','accession':fake}
    receipt['obligations'].append(obligation)
    graph['ownership_acquisition_failures'][fake]={
        'failure':{'code':'source_failure','reason_code':'transport_error'},
        'required_by':[{'current_accession':current,'candidate':True,'obligations':[obligation]}]}
    graph['supported_disposition_failures']={current:{'code':'evidence_byte_limit'}}
    graph['supported_dispositions']={}
    row=graph['collections']['activist_13d'][0]
    row['filing_failure']={'code':'evidence_byte_limit','reason_code':'invalid_response'}
    row['filing_material_disposition']='failed'
    graph['material_scope']=build_material_scope(graph,graph['collections'])
    validate_filing_material_policy(data,CONFIG)
    with pytest.raises(ValueError,match='invalid_filing_acquisition_policy'):
        validate_filing_acquisition_policy(data,CONFIG)
