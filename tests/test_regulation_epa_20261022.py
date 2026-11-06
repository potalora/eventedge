"""Real official EPA notices: complete identity and exact publisher notation.

HTTP is replaced only in wrapper tests. Originals are unmodified official
responses bound by regulation_20261022_manifest.json; no model is invoked.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from tradingagents.strategies.data_sources.regulation_government_copy import (
    extract_government_copy, government_copy_plan,
)

pytestmark = pytest.mark.scheduled_acceptance
FIXTURES = Path(__file__).parent/'fixtures'
V2 = 'verified_government_copy_v2'
CASES = ['epa_4291','epa_0505','epa_5581','epa_5119']


def originals(label):
    return ((FIXTURES/f'regulation_detail_{label}.json').read_bytes(),
            (FIXTURES/f'regulation_govinfo_{label}.htm').read_bytes())


def test_official_fixtures_are_exact_captured_bytes():
    manifest=json.loads((FIXTURES/'regulation_20261022_manifest.json').read_text())
    for filename, receipt in manifest['files'].items():
        raw=(FIXTURES/filename).read_bytes()
        assert len(raw)==receipt['bytes']
        assert hashlib.sha256(raw).hexdigest()==receipt['sha256']


@pytest.mark.parametrize('label',CASES)
def test_real_epa_notice_keeps_full_body_identity_and_boundaries(label):
    detail,body=originals(label)
    value=extract_government_copy(detail,body,retrieval_policy=V2)
    attributes=json.loads(detail)['data']['attributes']
    assert value['identity']['cfr_part']==attributes['cfrPart']
    assert value['identity']['document_id']==json.loads(detail)['data']['id']
    assert value['notice_metadata']['action']=='Proposed rule.'
    billing={'epa_4291':'6569-50-P','epa_0505':'6568-60-P','epa_5581':'6569-60-P','epa_5119':'6569-60-P'}
    assert value['text'].endswith('BILLING CODE '+billing[label]+'\n\n\n')
    assert all(section+':' in value['text'] for section in ['SUMMARY','DATES','ADDRESSES','SUPPLEMENTARY INFORMATION'])
    if label=='epa_5581':
        assert 'AGENCY: U.S. Environmental Protection Agency (EPA).' in value['text']
    if label=='epa_5119':
        spans=[x for x in value['transformations'] if x['kind']=='govinfo_inf_notation_v1' and x['value']=='2.5,']
        assert spans
        for span in spans:
            assert body.decode()[span['source_codepoint_start']:span['source_codepoint_end']]=='<INF>2.5,</INF>'
            assert value['text'][span['text_codepoint_start']:span['text_codepoint_end']]=='2.5,'


def acquire(monkeypatch,label):
    from tradingagents.strategies.data_sources import regulations_source as mod,request_policy
    from test_regulation_source_evidence import Response
    detail,body=originals(label)
    item=json.loads(detail)['data'];a=item['attributes'];docid=item['id']
    original_url=next(x['fileUrl'] for x in a['fileFormats'] if x['format']=='html')
    copy_url='https://www.govinfo.gov/content/pkg/FR-'+a['postedDate'][:10]+'/html/'+a['frDocNum']+'.htm'
    replies={mod.BASE_URL+'/documents':(json.dumps({'data':[item],'meta':{'totalPages':1,'totalElements':1}}).encode(),200),
        mod.BASE_URL+'/documents/'+docid:(detail,200),original_url:(b'Access denied',403),copy_url:(body,200)}
    def request(provider,method,url,**kwargs):
        assert provider=='regulations' and kwargs['allow_redirects'] is False
        if url==copy_url: assert kwargs.get('headers',{})=={}
        raw,status=replies[url]
        return Response(raw,status=status)
    monkeypatch.setattr(mod,'provider_request',request)
    monkeypatch.setattr(request_policy,'PROVIDER_LIMITS',{})
    return mod.RegulationsSource(api_key='fixture-key').get_recent_proposed_rules(
        agencies=['EPA'],days_back=14,as_of='2026-10-21',
        evidence_policy='complete_document_text_v1',retrieval_policy=V2)


@pytest.mark.parametrize('label',CASES)
def test_real_notice_passes_acquisition_and_immutable_replay(monkeypatch,label):
    from tradingagents.strategies.data_sources.regulation_evidence import validate_regulation_source
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    rows=acquire(monkeypatch,label)
    payload={'proposed_rules':list(rows),'coverage':rows.coverage,'regulation_evidence':rows.regulation_evidence}
    assert validate_regulation_source(payload)['content_complete'] is True
    frozen=SourceInputStore.decode(SourceInputStore.encode(payload))
    assert validate_regulation_source(frozen)['content_complete'] is True
    doc=next(iter(payload['regulation_evidence']['corpus'].values()))
    assert doc['unresolved_material_dependencies']==[]
    assert len(doc['units'])==1 and doc['units'][0]['primary_failure']['http_status']==403
    from tradingagents.strategies.data_sources.regulation_assessment import prepare_request
    request_unit=json.loads(prepare_request(doc).user)['units'][0]
    assert request_unit['text']==doc['units'][0]['text']
    if label=='epa_5119':
        assert any(request_unit['text'][span['text_codepoint_start']:span['text_codepoint_end']]=='2.5,'
            for span in request_unit['text_annotations'] if span['kind']=='subscript')
    changed=deepcopy(frozen);unit=next(iter(changed['regulation_evidence']['corpus'].values()))['units'][0]
    unit['text']=unit['text'][:-1]
    with pytest.raises(ValueError,match='invalid_regulation_'):
        validate_regulation_source(changed)


@pytest.mark.parametrize('value',[
    '40 CFR Parts 60, 60, and 63','40 CFR Parts 60-63','40 CFR Parts 60, 61, 63',
    '40 CFR Parts 60 and','40 CFR Parts 060 and 63','40 CFR Parts 60 and 0',
    '40 CFR Parts 60 and 63 trailing','40 CFR Part 60 and 63','17 CFR Parts 60 and 63',
])
def test_multi_part_metadata_does_not_admit_ambiguous_or_wrong_authority(value):
    detail,_=originals('epa_4291');item=json.loads(detail);item['data']['attributes']['cfrPart']=value
    with pytest.raises(ValueError,match='invalid_regulation_'):
        government_copy_plan(json.dumps(item).encode(),retrieval_policy=V2)


@pytest.mark.parametrize('label,old,new',[
    ('epa_4291',b'40 CFR Parts 60, 61, and 63',b'40 CFR Parts 60 and 63'),
    ('epa_0505',b'40 CFR Parts 80 and 1090',b'40 CFR Parts 80 and 1091'),
    ('epa_5581',b'AGENCY: U.S. Environmental Protection Agency (EPA).',b'AGENCY: U.S. Other Agency (EPA).'),
    ('epa_5119',b'<INF>2.5,</INF>',b'<INF>2.5,,</INF>'),
    ('epa_5119',b'<INF>2.5,</INF>',b'<INF>2.5;</INF>'),
    ('epa_5119',b'<INF>2.5,</INF>',b'<INF hidden="yes">2.5,</INF>'),
    ('epa_4291',b'[EPA-R10-OAR-2026-4291,',b'[EPA-R10-OAR-2026-4292,'),
])
def test_new_shapes_still_reject_dropped_parts_wrong_identity_or_unsupported_markup(label,old,new):
    detail,body=originals(label);assert old in body
    with pytest.raises(ValueError,match='(?:invalid|unsupported)_regulation_'):
        extract_government_copy(detail,body.replace(old,new),retrieval_policy=V2)


def test_real_sec_correction_is_not_accepted_as_a_complete_original_notice():
    detail,body=originals('sec_correction_6502')
    assert json.loads(detail)['data']['attributes']['frDocNum']=='C1-2026-20360'
    assert b'In proposed rules document 2026-20360 beginning on page 63388' in body
    with pytest.raises(ValueError,match='(?:invalid|unsupported)_regulation_'):
        extract_government_copy(detail,body,retrieval_policy='verified_government_copy_v3')


def test_correction_cannot_be_admitted_even_if_primary_transport_recovers():
    from tradingagents.strategies.data_sources.regulation_evidence import detail_plan
    detail,_=originals('sec_correction_6502');item=json.loads(detail)['data'];a=item['attributes']
    row={'document_id':item['id'],'title':a['title'],'agency_id':a['agencyId'],
         'document_type':a['documentType'],'posted_date':a['postedDate'],'docket_id':a['docketId']}
    with pytest.raises(ValueError,match='unsupported_regulation_correction_dependency'):
        detail_plan(row,detail,observed_at='2026-10-22T08:47:10+00:00')


@pytest.mark.parametrize('agency',['EPA','FDA','FTC','DOL','CFPB'])
def test_constructed_crossagency_correction_cannot_become_complete_inert_html(agency):
    from tradingagents.strategies.data_sources.regulation_evidence import build_regulation_evidence
    detail,_=originals('sec_correction_6502');item=json.loads(detail);data=item['data'];a=data['attributes']
    original_id=data['id'];docid=original_id.replace('SEC-',agency+'-',1)
    data['id']=docid;a['agencyId']=agency
    body=b'<html><body><h1>Correction</h1><p>This correction changes the original notice comment date.</p></body></html>'
    a['fileFormats']=[{'format':'html','fileUrl':'https://downloads.regulations.gov/'+docid+'/content.html','size':len(body)}]
    row={'document_id':docid,'title':a['title'],'agency_id':agency,'document_type':a['documentType'],
         'posted_date':a['postedDate'],'docket_id':a['docketId']}
    with pytest.raises(ValueError,match='unsupported_regulation_correction_dependency'):
        build_regulation_evidence(row,json.dumps(item).encode(),{docid:body},observed_at='2026-10-22T08:47:10+00:00')
