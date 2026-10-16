"""Explicit reviewed source exclusions never become generic parser fallbacks."""
from copy import deepcopy
import hashlib
import json

import pytest

from test_filing_evidence import OBSERVED, role, submission
from tradingagents.strategies.data_sources import filing_material_policy as material
from tradingagents.strategies.data_sources.filing_evidence import frame_submission

POLICY = 'verified_supported_documents_v4'
ACCESSION = '0001772351-26-000012'
SOURCE = 'https://www.sec.gov/Archives/edgar/data/1772351/000177235126000012/0001772351-26-000012.txt'
HISTORICAL = {
    'retained_three_material_gaps_v1': '168b10bd85e34e4dea0da792e0b79bf51b4adc1a1e8831b131310deb4f222123',
    'retained_ten_material_gaps_v1': 'bce800c178db0e2b06de4c0efcfe1f5bf4ec99a0c9326292353c29562a21ea9d',
    'verified_supported_documents_v1': '31e3f69172acf2f9cce8bffea925af6fdf4bcfaa1b5cd46063346eedb0e305e8',
    'verified_supported_documents_v2': '6c0fef7063e71f66328948bc19760b76db30831938b5e94f0099e48fe4084c7c',
    'verified_supported_documents_v3': '32dfcf2aa0de625a78fd6cd5065d0454bda0088a58ba5f9f0e1d8174a7ab820e',
}


def synthetic(monkeypatch):
    """Inject one complete synthetic variant; it is never a production source pin."""
    raw = submission([('SCHEDULE 13D/A', 'primary_doc.xml', '<ownershipDocument>Opaque</ownershipDocument>'),
        ('EX-99', 'ex994.pdf', '<PDF>\nopaque invalid font fixture\n</PDF>')], form='SCHEDULE 13D/A',
        roles=role('SUBJECT-COMPANY', '0001351636', 'SOUNDTHINKING, INC.')
            + role('FILED-BY', '0001772351', 'Veradace Capital Management LLC'))
    from test_filing_evidence import ACCESSION as FIXTURE_ACCESSION
    raw = raw.replace(FIXTURE_ACCESSION.encode(), ACCESSION.encode())
    frame = frame_submission(raw, expected_accession=ACCESSION, expected_form='SCHEDULE 13D/A',
        expected_date='2026-09-30', observed_at=OBSERVED,max_submission_bytes=1024*1024)
    row = deepcopy(material.KNOWN_LOW_SIGNAL_ROWS[0])
    row['original_variants'] = [{'submission_sha256':hashlib.sha256(raw).hexdigest(),
        'submission_size':len(raw), 'format':frame['format'], 'accepted_at':frame['accepted_at']}]
    monkeypatch.setattr(material, 'KNOWN_LOW_SIGNAL_ROWS', (row,))
    return frame, raw, row


def test_historical_policy_manifest_bytes_are_unchanged():
    for policy,digest in HISTORICAL.items():
        assert material.policy_manifest_sha256(policy) == digest


def test_new_registry_is_explicit_and_source_bound():
    manifest=material.policy_manifest(POLICY)
    assert manifest['supported_scope']['version']=='supported-document-disposition-v4'
    registry=manifest['known_low_signal_registry']
    assert registry['runtime_self_classification'] is False
    assert registry['unknown_parser_failures_remain_failures'] is True
    rows=[r for r in manifest['identities'] if r['accession']==ACCESSION]
    assert len(rows)==1
    row=rows[0]
    assert row['source_url']==SOURCE
    assert row['disposition']=='whole_filing_excluded'
    assert row['verification_basis']=='reviewed_complete_original_and_administrative_content'
    assert 'ex994.pdf is a one-page administrative Joint Filing Agreement' in row['content_review']
    assert 'primary XML' in row['content_review'] and 'unassessed merger terms' in row['content_review']
    assert any(v['submission_sha256']=='e63e7fcbff22a7fabf602b33759c4ab17d9f722eee00abc8e882757e9301ad16'
        and v['submission_size']==324850 and v['format']=='nc_submission' for v in row['original_variants'])
    assert row['original_variants']==[
        {'submission_sha256':'e63e7fcbff22a7fabf602b33759c4ab17d9f722eee00abc8e882757e9301ad16',
         'submission_size':324850,'format':'nc_submission','accepted_at':None},
        {'submission_sha256':'67c6b52a7deed7de595e0c4d3968de867d094229af4f8dcceb2f9c3ba4162aae',
         'submission_size':325036,'format':'sec_complete_submission','accepted_at':'20260930205927'}]
    assert row['gap_codes']==['known_low_signal_whole_filing_exclusion','pdf_invalid_embedded_font','invalid_native_ownership_facts']
    assert len(material.policy_manifest('verified_supported_documents_v3')['identities'])==10
    assert material.approved_identity({'accession':ACCESSION,'form':'SCHEDULE 13D/A',
        'filing_date':'2026-09-30','source_url':SOURCE},policy='verified_supported_documents_v3') is None


def test_complete_exact_source_is_excluded_without_analysis(monkeypatch):
    frame,raw,row=synthetic(monkeypatch)
    evidence=material.quarantine_evidence(frame,submission_size=len(raw),policy=POLICY,
        required_exhibits=['ex994.pdf'])
    assert material.validate_quarantined_evidence(evidence,policy=POLICY)==row
    assert evidence['structural_status']==evidence['analysis_adequacy']=='insufficient'
    assert evidence['structural_scope']=='complete_original_inventory_unselected'
    assert evidence['dependency_assessment']=='not_assessed'
    assert evidence['units']==evidence['dependencies']==[]
    assert len(evidence['document_inventory'])==2
    assert evidence['issues']==[{'code':c} for c in row['gap_codes']]
    assert evidence['material_quarantine']['required_exhibits']==['ex994.pdf']
    from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence
    with pytest.raises(ValueError):_validate_evidence(evidence)


@pytest.mark.parametrize('damage', ['hash','size','accession','form','date','url','format','accepted',
    'roles','issuer','document_count','missing_exhibit','inventory_hash','units','failure_erasure'])
def test_changed_or_forged_exclusion_remains_failure(monkeypatch,damage):
    frame,raw,row=synthetic(monkeypatch)
    if damage=='hash':frame['submission_sha256']='0'*64
    elif damage=='size':raw+=b' '
    elif damage=='accession':frame['accession']='0001772351-26-999999'
    elif damage=='form':frame['form']='SCHEDULE 13D'
    elif damage=='date':frame['filing_date']='2026-10-01'
    elif damage=='url':frame['source_url']=SOURCE+'wrong'
    elif damage=='format':frame['format']='sec_complete_submission'
    elif damage=='accepted':frame['accepted_at']='20260930120000'
    elif damage=='roles':frame['roles'].reverse()
    elif damage=='issuer':frame['issuer_candidates']=frame['roles']
    elif damage=='document_count':frame['documents'].pop()
    if damage in {'inventory_hash','units','failure_erasure'}:
        e=material.quarantine_evidence(frame,submission_size=len(raw),policy=POLICY)
        if damage=='inventory_hash':e['document_inventory'][1]['body_sha256']='0'*64
        elif damage=='units':e['units']=[{'text':'Invented analysis input'}]
        else:e['issues']=[]
        with pytest.raises(ValueError):material.validate_quarantined_evidence(e,policy=POLICY)
    else:
        with pytest.raises(ValueError):material.quarantine_evidence(frame,submission_size=len(raw),policy=POLICY,
            required_exhibits=['missing.pdf'] if damage=='missing_exhibit' else ())


def test_same_accession_unregistered_native_bytes_never_match(monkeypatch):
    frame,raw,row=synthetic(monkeypatch)
    changed=raw.replace(b'Opaque',b'Change')
    assert len(changed)==len(raw)
    other=frame_submission(changed,expected_accession=ACCESSION,expected_form='SCHEDULE 13D/A',
        expected_date='2026-09-30',observed_at=OBSERVED,max_submission_bytes=1024*1024)
    with pytest.raises(ValueError):material.quarantine_evidence(other,submission_size=len(changed),policy=POLICY)


def test_unknown_filing_never_becomes_low_signal(monkeypatch):
    frame,raw,row=synthetic(monkeypatch)
    unknown=dict(frame,accession='0001772351-26-999999')
    assert material.approved_identity(dict(unknown,source_url=SOURCE),policy=POLICY) is None
    with pytest.raises(ValueError):material.quarantine_evidence(unknown,submission_size=len(raw),policy=POLICY)


@pytest.mark.parametrize('body,expected', [('<p>Complete narrative.</p>','admitted'),
    ('<table><tr><td>Unresolved visual meaning</td></tr></table>','deferred')])
def test_v4_other_sources_keep_v3_verifiers_and_boundaries(body,expected):
    from test_filing_evidence import ACCESSION as OTHER_ACCESSION
    from tradingagents.strategies.data_sources.filing_evidence import select_primary,build_evidence_from_buffer
    from tradingagents.strategies.data_sources import filing_supported_policy as supported
    from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence
    raw=submission([('8-K','main.htm',body)])
    frame=frame_submission(raw,expected_accession=OTHER_ACCESSION,expected_form='8-K',
        expected_date='2026-09-30',observed_at=OBSERVED,max_submission_bytes=1024*1024)
    e=build_evidence_from_buffer(select_primary(frame,raw),raw,supported_document_policy=POLICY)
    e['source_url']=f'https://www.sec.gov/Archives/edgar/data/2065397/{OTHER_ACCESSION.replace("-","")}/{OTHER_ACCESSION}.txt'
    original={'identity':{k:e[k] for k in ('accession','form','filing_date','source_url')},
        'observed_at':OBSERVED,'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'complete':True}
    proof=supported.build_local_source_proof(e,original=original,raw_submission=raw)
    receipt=supported.evaluate_supported_disposition(e,source_proofs={OTHER_ACCESSION:proof},
        originals={OTHER_ACCESSION:original},occurrences=[{'collection':'filings','index':0}])
    assert receipt['status']==expected
    assert receipt['version']=='supported-document-disposition-v4'
    assert supported._verifiers(POLICY)==supported._verifiers('verified_supported_documents_v3')
    assert supported._bounds(POLICY)==supported._bounds('verified_supported_documents_v3')
    if expected=='deferred':
        with pytest.raises(ValueError):_validate_evidence(e)
    else:assert _validate_evidence(e)
    assert supported.validate_supported_disposition(receipt,e,source_proofs={OTHER_ACCESSION:proof},
        originals={OTHER_ACCESSION:original},occurrences=[{'collection':'filings','index':0}],
        raw_submissions={OTHER_ACCESSION:raw})==receipt


def captured_quarantine(tmp_path,monkeypatch):
    from test_filing_supported_audit import capture_extra
    from test_filing_ownership_hydration import NativeSource
    import test_filing_ownership_hydration as helper
    frame,raw,_=synthetic(monkeypatch)
    row={'adsh':ACCESSION,'form_type':'SCHEDULE 13D/A','file_date':'2026-09-30',
        'ciks':['0001351636'],'file_url':SOURCE}
    source=NativeSource(extra=((row,raw),))
    def quarantined(url,**kwargs):
        required=list(kwargs.get('required_exhibits',()))
        if '__all_native_exhibits__' in required:required=['ex994.pdf']
        return material.quarantine_evidence(frame,submission_size=len(raw),required_exhibits=required,policy=POLICY)
    source.get_complete_submission=quarantined
    monkeypatch.setattr(helper,'POLICY',POLICY)
    monkeypatch.setattr(helper,'SUBJECT','0001351636')
    return capture_extra(tmp_path,monkeypatch,source,{'activist_13d':[row],'pqc_filings':[deepcopy(row)]})


def test_full_original_acquired_and_audited_but_signals_exclude_every_occurrence(tmp_path,monkeypatch):
    import time
    from test_filing_ownership_acquisition_validation import CONFIG
    from tradingagents.strategies.orchestration.filing_material_validation import validate_filing_material_policy,signal_edgar
    from tradingagents.strategies.orchestration.filing_acquisition_validation import validate_filing_acquisition_policy
    from tradingagents.strategies.orchestration.filing_supported_audit import validate_supported_raw_capture
    data,paths=captured_quarantine(tmp_path,monkeypatch)
    config={**CONFIG,'filing_material_policy':POLICY}
    graph=data['edgar']['filing_evidence']
    assert graph['coverage']['complete'] is False
    assert graph['coverage']['scoped_complete'] is True
    assert graph['coverage']['quarantined_rows']==2
    assert graph['coverage']['required_rows']==graph['coverage']['failed_rows']==2
    assert graph['coverage']['scoped_failed_rows']==0
    assert graph['material_scope']['strict_failed_rows']==2
    assert graph['material_scope']['original_full_analysis_coverage'] is False
    assert len(graph['acquisition_scope']['originals'])==1
    assert ACCESSION in paths and paths[ACCESSION].stat().st_size>0
    assert graph['corpus'][ACCESSION]['issues'][1:]==[{'code':'pdf_invalid_embedded_font'},
        {'code':'invalid_native_ownership_facts'}]
    assert validate_filing_acquisition_policy(data,config)['completed_originals']==1
    scope=validate_filing_material_policy(data,config)
    projected=signal_edgar(data['edgar'],scope)
    assert projected['activist_13d']==projected['pqc_filings']==[]
    assert ACCESSION not in projected['filing_evidence']['corpus']
    deadline=time.monotonic()+10
    receipt=validate_supported_raw_capture(data,config,paths,check=lambda:deadline-time.monotonic())
    assert receipt['quarantined_originals']==receipt['completed_originals']==1
    assert receipt['raw_audit_complete'] and receipt['order_authority'] is False
    original=paths[ACCESSION].read_bytes();paths[ACCESSION].chmod(0o600)
    paths[ACCESSION].write_bytes(original[:-1]+b'!');paths[ACCESSION].chmod(0o400)
    with pytest.raises(ValueError):validate_supported_raw_capture(data,config,paths,check=lambda:deadline-time.monotonic())


def test_unknown_failed_source_cannot_gain_registry_scope(monkeypatch):
    from test_filing_hydration import Source,hydrate,row
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    r=row(99,'8-K');source=Source([r])
    source.overrides[r['adsh']]=SourceFetchError('Unregistered parser failure',reason_code='invalid_response')
    graph=hydrate(source,{'filings':[r]},material_policy=POLICY)
    assert graph['coverage']['complete'] is False and graph['coverage']['scoped_complete'] is False
    assert graph['coverage']['quarantined_rows']==0
    assert r['adsh'] not in graph['corpus']


def test_excluded_whole_filing_never_reaches_screen_or_model(tmp_path,monkeypatch):
    from test_filing_ownership_acquisition_validation import CONFIG
    from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    data,_=captured_quarantine(tmp_path,monkeypatch)
    before=deepcopy(data)
    strategy=FilingAnalysisStrategy()
    engine=MultiStrategyEngine(config={'autoresearch':dict(CONFIG,
        filing_material_policy=POLICY,state_dir=str(tmp_path/'state'))},strategies=[strategy])
    calls=[]
    def inspect(projected):
        assert projected['edgar']['activist_13d']==projected['edgar']['pqc_filings']==[]
        assert ACCESSION not in projected['edgar']['filing_evidence']['corpus']
        calls.append('checked')
    def regime(projected):inspect(projected);return {}
    def screen(projected,*args):inspect(projected);return []
    monkeypatch.setattr(engine,'_build_regime_model',regime)
    monkeypatch.setattr(strategy,'screen',screen)
    monkeypatch.setattr(engine,'_enrich_with_llm',lambda *a,**k:pytest.fail('Excluded filing reached model'))
    signals,_,health=engine.screen_and_enrich('2026-10-09',data,
        epoch_id='epoch',policy_id='foundation-30d')
    assert calls==['checked','checked'] and signals==[] and data==before
    scope=health[0].evidence['filing_material_scope']
    assert scope['strict_complete'] is False and scope['strict_failed_rows']==2
    assert scope['scoped_complete'] is True and scope['quarantined_rows']==2


def test_real_v4_children_keep_ordinary_source_verification(tmp_path,monkeypatch):
    import test_filing_supported_parser as helper
    monkeypatch.setattr(helper,'POLICY',POLICY)
    evidence,receipt=helper.parsed(tmp_path,submission([('8-K','main.htm','<p>Complete narrative.</p>')]))
    assert evidence['supported_document_policy']==POLICY
    assert 'material_quarantine' not in evidence
    assert receipt['version']=='supported-document-disposition-v4'
    assert receipt['status']=='admitted'


def test_real_children_reject_unregistered_bytes_for_known_accession(tmp_path):
    import time
    from test_filing_evidence import ACCESSION as FIXTURE_ACCESSION
    from test_filing_spool import Response
    from tradingagents.strategies.data_sources.filing_parser_dispatch import parser_scope,dispatch_evidence
    from tradingagents.strategies.data_sources.filing_spool import submission_spool_scope,spool_response
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    raw=submission([('SCHEDULE 13D/A','primary_doc.xml','<ownershipDocument>Unregistered</ownershipDocument>'),
        ('EX-99','ex994.pdf','<PDF>invalid fixture</PDF>')],form='SCHEDULE 13D/A',
        roles=role('SUBJECT-COMPANY','0001351636','SOUNDTHINKING, INC.')
            +role('FILED-BY','0001772351','Veradace Capital Management LLC'))
    raw=raw.replace(FIXTURE_ACCESSION.encode(),ACCESSION.encode())
    deadline=time.monotonic()+15
    with provider_budget('edgar',deadline,limits=()):
        with submission_spool_scope(tmp_path,original_deadline=deadline) as spool:
            response=Response((raw,))
            record=spool_response(response,identity=dict(accession=ACCESSION,
                form='SCHEDULE 13D/A',filing_date='2026-09-30',source_url=SOURCE))
            assert response.closed and record.complete and record.size==len(raw)
            with parser_scope() as parser:
                with pytest.raises(SourceFetchError) as error:
                    dispatch_evidence(record,expected_accession=ACCESSION,expected_form='SCHEDULE 13D/A',
                        expected_date='2026-09-30',observed_at=record.observed_at,
                        max_submission_bytes=512*1024**2,required_exhibits=['__all_native_exhibits__'],
                        material_policy=POLICY)
                assert error.value.reason_code=='invalid_response'
                assert record.path.read_bytes()==raw and spool.stats()['active_child_copies']==0
            assert all(child.poll() is not None for child in parser.children)
            spool.discard(record)
