"""Every completed original is audited from one private local mapping at a time."""
from copy import deepcopy
import json
import mmap
import os
import time

import pytest

from test_filing_ownership_acquisition_validation import acquire,CONFIG
from test_filing_ownership_hydration import NativeSource,filing
from tradingagents.strategies.orchestration.filing_supported_audit import validate_supported_raw_capture


def capture(tmp_path,monkeypatch,*,failed=False):
    (tmp_path/'spool').mkdir()
    data,base,current=acquire(tmp_path/'spool',monkeypatch,reject_dependency=failed)
    source=NativeSource(filing(1,'2026-01-01'),filing(2,'2026-09-30',amendment=1))
    if failed:
        raw=source.raws[base]
        document=raw[raw.index(b'<DOCUMENT>'):raw.index(b'</DOCUMENT>')+len(b'</DOCUMENT>')]
        source.raws[base]=raw.replace(b'<PUBLIC-DOCUMENT-COUNT>1',b'<PUBLIC-DOCUMENT-COUNT>2').replace(b'</SUBMISSION>',document+b'</SUBMISSION>')
    root=tmp_path/'capture';root.mkdir()
    paths={}
    for accession,raw in source.raws.items():
        path=root/(accession+'.txt');path.write_bytes(raw);path.chmod(0o400);paths[accession]=path
    return data,paths


def audit(data,paths):
    deadline=time.monotonic()+10
    return validate_supported_raw_capture(data,CONFIG,paths,check=lambda:deadline-time.monotonic())


def test_all_originals_independently_verified_without_model_authority(tmp_path,monkeypatch):
    data,paths=capture(tmp_path,monkeypatch)
    result=audit(data,paths)
    assert result['raw_audit_complete'] is True
    assert result['completed_originals']==result['verified_source_proofs']==2
    assert result['failed_evidence_originals']==0
    assert result['completed_original_bytes']==sum(path.stat().st_size for path in paths.values())
    assert len(result['original_manifest_sha256'])==len(result['dispositions_sha256'])==64
    assert result['assessment_status']=='not_certified' and result['order_authority'] is False
    assert result['original_full_analysis_coverage'] is False
    json.dumps(result,allow_nan=False)


def test_failed_parse_original_is_still_hashed_and_failure_stays_failed(tmp_path,monkeypatch):
    data,paths=capture(tmp_path,monkeypatch,failed=True)
    result=audit(data,paths)
    assert result['failed_evidence_originals']==1 and result['verified_source_proofs']==1
    assert data['edgar']['filing_evidence']['coverage']['complete'] is False and data['edgar']['error']
    failed=next(path for accession,path in paths.items() if accession not in data['edgar']['filing_evidence']['corpus'])
    failed.chmod(0o600);raw=failed.read_bytes();failed.write_bytes(raw[:-1]+b'!');failed.chmod(0o400)
    with pytest.raises(ValueError,match='invalid_supported_raw_capture'):
        audit(data,paths)


@pytest.mark.parametrize('damage',['missing','orphan','relative','symlink','ancestor_symlink','directory',
    'world_readable','group_readable','executable','empty','changed_hash','changed_size'])
def test_raw_capture_requires_exact_private_regular_manifest(tmp_path,monkeypatch,damage):
    data,paths=capture(tmp_path,monkeypatch)
    key=next(iter(paths));path=paths[key]
    if damage=='missing': del paths[key]
    elif damage=='orphan': paths['0001193125-26-999999']=path
    elif damage=='relative': paths[key]=path.name
    elif damage=='symlink':
        link=tmp_path/'alias';link.symlink_to(path);paths[key]=link
    elif damage=='ancestor_symlink':
        link=tmp_path/'alias';link.symlink_to(path.parent,target_is_directory=True);paths[key]=link/path.name
    elif damage=='directory': paths[key]=path.parent
    elif damage=='world_readable': path.chmod(0o644)
    elif damage=='group_readable': path.chmod(0o640)
    elif damage=='executable': path.chmod(0o700)
    else:
        path.chmod(0o600);raw=path.read_bytes()
        path.write_bytes(b'' if damage=='empty' else raw[:-1]+b'!' if damage=='changed_hash' else raw+b'!')
        path.chmod(0o400)
    with pytest.raises(ValueError,match='invalid_supported_raw_capture'):
        audit(data,paths)


def test_private_writable_file_mode_is_allowed_read_only(tmp_path,monkeypatch):
    data,paths=capture(tmp_path,monkeypatch)
    for path in paths.values():path.chmod(0o600)
    assert audit(data,paths)['raw_audit_complete']


def test_foreign_uid_capture_rejected(tmp_path,monkeypatch):
    data,paths=capture(tmp_path,monkeypatch)
    monkeypatch.setattr(os,'getuid',lambda:next(iter(paths.values())).stat().st_uid+1)
    with pytest.raises(ValueError,match='invalid_supported_raw_capture'):
        audit(data,paths)


@pytest.mark.parametrize('check',[None,lambda:None,lambda:False,lambda:0,lambda:-1,lambda:float('nan'),lambda:float('inf')])
def test_caller_must_supply_positive_remaining_absolute_budget(tmp_path,monkeypatch,check):
    data,paths=capture(tmp_path,monkeypatch)
    with pytest.raises(ValueError,match='invalid_supported_raw_capture'):
        validate_supported_raw_capture(data,CONFIG,paths,check=check)


def test_same_caller_budget_is_checked_repeatedly_without_reset(tmp_path,monkeypatch):
    data,paths=capture(tmp_path,monkeypatch)
    calls=[]
    def check():
        calls.append(True)
        return 10 if len(calls)<25 else 0
    with pytest.raises(ValueError,match='invalid_supported_raw_capture'):
        validate_supported_raw_capture(data,CONFIG,paths,check=check)
    assert len(calls)==25


def test_one_readonly_mapping_at_a_time_and_all_closed(tmp_path,monkeypatch):
    data,paths=capture(tmp_path,monkeypatch)
    original=mmap.mmap;counts={'active':0,'peak':0,'total':0};mapped=[]
    class CountedMap(original):
        def __new__(cls,*args,**kwargs):
            assert kwargs['access']==mmap.ACCESS_READ
            value=super().__new__(cls,*args,**kwargs);mapped.append(value)
            counts['active']+=1;counts['total']+=1;counts['peak']=max(counts['peak'],counts['active'])
            return value
        def __exit__(self,*args):
            try:return super().__exit__(*args)
            finally:counts['active']-=1
    monkeypatch.setattr(mmap,'mmap',CountedMap)
    assert audit(data,paths)['raw_audit_complete']
    assert counts=={'active':0,'peak':1,'total':2}
    assert all(value.closed for value in mapped)


def test_file_metadata_change_during_proof_is_rejected(tmp_path,monkeypatch):
    from tradingagents.strategies.data_sources import filing_supported_policy
    data,paths=capture(tmp_path,monkeypatch)
    original=filing_supported_policy.build_local_source_proof
    def touched(evidence,**kwargs):
        result=original(evidence,**kwargs)
        path=paths[evidence['accession']];info=path.stat()
        os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns+1))
        return result
    monkeypatch.setattr(filing_supported_policy,'build_local_source_proof',touched)
    with pytest.raises(ValueError,match='invalid_supported_raw_capture'):
        audit(data,paths)


def test_resealed_parser_assertion_can_pass_provisional_scope_but_not_raw_audit(tmp_path,monkeypatch):
    from tradingagents.strategies.data_sources.filing_supported_policy import _digest
    from tradingagents.strategies.orchestration.filing_supported_scope import build_supported_dispositions
    from tradingagents.strategies.orchestration.filing_material_validation import build_material_scope
    from tradingagents.strategies.orchestration.filing_acquisition_validation import validate_filing_acquisition_policy
    data,paths=capture(tmp_path,monkeypatch)
    graph=data['edgar']['filing_evidence'];current=graph['collections']['activist_13d'][0]['adsh']
    proof=graph['supported_source_proofs'][current]
    proof['residual_failures'].append({'code':'invented_raw_only_failure'})
    proof['source_proof_sha256']=_digest({key:value for key,value in proof.items() if key!='source_proof_sha256'})
    graph['supported_dispositions']=build_supported_dispositions(graph,graph['collections'])
    graph['collections']['activist_13d'][0]['filing_material_disposition']='failed'
    graph['coverage'].update(complete=False,failed_rows=1)
    data['edgar']['error']='full filing evidence incomplete'
    graph['material_scope']=build_material_scope(graph,graph['collections'])
    for key in ('scoped_complete','scoped_failed_rows','quarantined_rows'):
        graph['coverage'][key]=graph['material_scope'][key]
    assert validate_filing_acquisition_policy(data,CONFIG)['completed_originals']==2
    with pytest.raises(ValueError,match='invalid_supported_raw_capture'):
        audit(data,paths)


def test_does_not_mutate_frozen_graph(tmp_path,monkeypatch):
    data,paths=capture(tmp_path,monkeypatch)
    before=deepcopy(data)
    audit(data,paths)
    assert data==before


def capture_extra(tmp_path,monkeypatch,source,collections):
    from datetime import datetime
    from test_filing_evidence import OBSERVED
    from test_filing_spool import Response
    from test_filing_ownership_hydration import supported
    from tradingagents.strategies.data_sources import filing_spool
    from tradingagents.strategies.data_sources.filing_acquisition import complete_submission_url
    from tradingagents.strategies.orchestration.filing_acquisition_validation import acquisition_scope
    class FixedDatetime(datetime):
        @classmethod
        def now(cls,tz=None):return datetime.fromisoformat(OBSERVED)
    monkeypatch.setattr(filing_spool,'datetime',FixedDatetime)
    owner=filing_spool.SpoolScope(tmp_path,original_deadline=time.monotonic()+10,physical_limit=4*1024**3)
    original=source.get_complete_submission
    def captured(url,*,accession,form_type,filing_date,**kwargs):
        owner.spool_response(Response((source.raws[accession],)),identity={'accession':accession,
            'form':form_type,'filing_date':filing_date,'source_url':complete_submission_url(url,accession)},max_bytes=1024*1024)
        return original(url,accession=accession,form_type=form_type,filing_date=filing_date,**kwargs)
    source.get_complete_submission=captured
    try:graph=supported(source,collections)
    finally:owner.close()
    graph['coverage'].update(acquisition_policy=CONFIG['filing_acquisition_policy'],parser_policy='two_processes_v1')
    graph['acquisition_scope']=acquisition_scope(owner)
    data={'edgar':{'filing_evidence':graph,**graph['collections']}}
    if not graph['coverage']['complete']:data['edgar']['error']='full filing evidence incomplete'
    paths={}
    for acc,raw in source.raws.items():
        path=tmp_path/(acc+'.txt');path.write_bytes(raw);path.chmod(0o400);paths[acc]=path
    return data,paths


def test_image_only_pdf_deferral_is_independently_regenerated(tmp_path,monkeypatch):
    from test_filing_ownership_hydration import ordinary
    from test_filing_pdf import pdf_bytes
    row,raw=ordinary(99,'8-K','2026-09-30',documents=[('8-K','main.htm','<a href="selected.pdf">Exhibit</a>'),
        ('EX-99','selected.pdf','PLACEHOLDER')])
    raw=raw.replace(b'PLACEHOLDER',b'<PDF>\n'+pdf_bytes((b'',),image=True)+b'</PDF>')
    source=NativeSource(extra=((row,raw),))
    data,paths=capture_extra(tmp_path,monkeypatch,source,{'filings':[row]})
    assert data['edgar']['filing_evidence']['supported_dispositions'][row['adsh']]['status']=='deferred'
    result=audit(data,paths)
    assert result['raw_audit_complete'] and result['verified_source_proofs']==1
    assert result['order_authority'] is False


def test_exact_quarantine_raw_inventory_is_reframed_not_merely_hashed(tmp_path,monkeypatch):
    from filing_material_fixtures import framed,identity
    from tradingagents.strategies.data_sources.filing_material_policy import quarantine_evidence
    frame,raw=framed(1);ident=identity(1)
    row={'adsh':ident['accession'],'form_type':ident['form'],'file_date':ident['filing_date'],
        'ciks':['0000016732'],'file_url':ident['source_url']}
    source=NativeSource(extra=((row,raw),))
    def quarantined(url,**kwargs):
        return quarantine_evidence(frame,submission_size=len(raw),policy=CONFIG['filing_material_policy'])
    source.get_complete_submission=quarantined
    data,paths=capture_extra(tmp_path,monkeypatch,source,{'filings':[row]})
    result=audit(data,paths)
    assert result['quarantined_originals']==1 and result['verified_source_proofs']==0
    assert result['completed_originals']==1 and result['raw_audit_complete']
