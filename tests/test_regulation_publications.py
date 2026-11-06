"""Actual original/correction and historical-repost material, without models."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tradingagents.strategies.data_sources.regulation_evidence import original_receipt, digest

FIX = Path(__file__).parent / 'fixtures'
STAMP = '2026-10-22T23:30:00+00:00'
SEAL = '2026-10-22T23:31:00+00:00'
PUB = 'linked_publications_v1'
V4 = 'verified_government_copy_v4'


def capture(kind):
    names = {'original': ('regulation_detail_sec_interval.json', 'regulation_govinfo_sec_interval.htm'),
             'correction': ('regulation_detail_sec_correction_6502.json', 'regulation_govinfo_sec_correction_6502.htm'),
             'archived': ('regulation_linked_archived_detail.json', 'regulation_linked_archived_body.htm'),
             'cfpb': ('regulation_detail_cfpb_0018.json', 'regulation_govinfo_cfpb_0018.htm')}
    detail_name, body_name = names[kind]
    raw = (FIX / detail_name).read_bytes(); item = json.loads(raw)['data']; attrs = item['attributes']; docid = item['id']
    row = {'document_id': docid, 'title': attrs['title'], 'agency_id': attrs['agencyId'],
           'document_type': attrs['documentType'], 'posted_date': attrs['postedDate'], 'docket_id': attrs['docketId']}
    metadata = (FIX / ('regulation_linked_' + kind + '_metadata.json')).read_bytes()
    fr = json.loads(metadata); number = fr['document_number']
    url = 'https://www.govinfo.gov/content/pkg/FR-' + fr['publication_date'] + '/html/' + number + '.htm'
    selected = next((f for f in attrs['fileFormats'] if f['format'] == 'html'), attrs['fileFormats'][0])
    graphics = {}
    if kind == 'original':
        for key, image_url in fr['images'].items():
            graphics[key] = original_receipt((FIX / 'regulation_graphics' / (key + '.png')).read_bytes(),
                observed_at=STAMP, url=image_url['original_size'])
    return {'row': row,
        'detail': original_receipt(raw, observed_at=STAMP, url='https://api.regulations.gov/v4/documents/' + docid + '?include=attachments'),
        'metadata': original_receipt(metadata, observed_at=STAMP, url='https://www.federalregister.gov/api/v1/documents/' + number + '.json'),
        'bodies': {docid: original_receipt((FIX / body_name).read_bytes(), observed_at=STAMP, url=url)},
        'primary_attempt': {'resource_id':docid,'http_status':403,'complete':True,
            'receipt':original_receipt(b'Access denied', observed_at=STAMP, url=selected['fileUrl'])},
        'graphics': graphics}


def bundle(kind='correction'):
    from tradingagents.strategies.data_sources.regulation_publications import build_publication_node, build_publication_bundle
    captures = [capture('original'), capture('correction')] if kind == 'correction' else [capture('archived')]
    nodes = {}
    for c in captures:
        n = build_publication_node(c); nodes[n['manifest_sha256']] = n
    closing = captures[0]['metadata']
    return build_publication_bundle(captures[-1]['row'], nodes, closing, sealed_at=SEAL), nodes


def test_actual_correction_requires_complete_original_with_all_34_graphics():
    from tradingagents.strategies.data_sources.regulation_publications import validate_bundle
    value, nodes = bundle()
    validate_bundle(value, nodes)
    assert value['publication_kind'] == 'correction'
    assert value['available_at'] == SEAL
    assert len(value['edges']) == 1 and len(value['node_refs']) == 2
    assert {n['identity']['publication_date'] for n in nodes.values()} == {'2026-10-05','2026-10-20'}
    assert sum(len(u.get('government_copy',{}).get('graphics',[])) for n in nodes.values() for u in n['units']) == 34


def test_actual_archive_preserves_1997_publication_and_2026_repost():
    value, nodes = bundle('archived'); node = next(iter(nodes.values()))
    assert node['identity']['publication_date'] == '1997-03-11'
    assert node['catalogue_identity']['posted_date'] == '2026-10-21T04:00:00Z'
    assert node['identity']['fr_document_number'] == '97-5879'
    assert node['catalogue_identity']['docket_id'] == 'EPA-HQ-OW-1998-9208'
    assert value['publication_kind'] == 'historical_repost' and value['execution_authority'] == 'none'
    assert value['available_at'] == SEAL
    assert '\u2400\u2400Federal Register' in node['units'][0]['text']


@pytest.mark.parametrize('mutation',['missing_original','missing_correction','wrong_edge','early_seal','missing_graphic','changed_body','changed_metadata'])
def test_linked_bundle_rejects_missing_mutated_or_backdated_dependencies(mutation):
    from tradingagents.strategies.data_sources.regulation_publications import validate_bundle
    value, nodes = bundle(); value = deepcopy(value); nodes = deepcopy(nodes)
    original = next(k for k,v in nodes.items() if v['kind']=='original')
    correction = next(k for k,v in nodes.items() if v['kind']=='correction')
    if mutation=='missing_original': nodes.pop(original)
    elif mutation=='missing_correction': nodes.pop(correction)
    elif mutation=='wrong_edge': value['edges']=[]
    elif mutation=='early_seal': value['available_at']='2026-10-05T04:00:00+00:00'
    elif mutation=='missing_graphic': nodes[original]['capture']['graphics'].pop(next(iter(nodes[original]['capture']['graphics'])))
    elif mutation=='changed_body': nodes[original]['units'][0]['text']+='changed'
    else: nodes[original]['capture']['metadata']=capture('correction')['metadata']
    with pytest.raises(ValueError,match='regulation_'):
        validate_bundle(value,nodes)


def test_correction_alone_is_never_a_complete_original():
    from tradingagents.strategies.data_sources.regulation_publications import build_publication_node, build_publication_bundle
    c = capture('correction'); n = build_publication_node(c)
    with pytest.raises(ValueError,match='regulation_'):
        build_publication_bundle(c['row'],{n['manifest_sha256']:n},c['metadata'],sealed_at=SEAL)


def acquired(monkeypatch,kind='correction',mutation=None,two_roots=False):
    from tradingagents.strategies.data_sources import regulations_source as mod,request_policy
    from test_regulation_source_evidence import Response
    root=capture(kind);caps=[capture('original'),capture('correction')] if kind=='correction' else [root]
    table={}
    for c in caps:
        table[c['detail']['url'].split('?')[0]]=(__import__('base64').b64decode(c['detail']['raw_base64']),200)
        for r in [c['metadata'],*c['bodies'].values(),*c['graphics'].values()]:
            table[r['url']]=(__import__('base64').b64decode(r['raw_base64']),200)
        r=c['primary_attempt']['receipt'];table[r['url']]=(b'Access denied',403)
    if mutation=='missing_graphic':
        url=next(iter(caps[0]['graphics'].values()))['url'];table[url]=(b'Unavailable',404)
    elif mutation=='missing_original':table[caps[0]['detail']['url'].split('?')[0]]=(b'Unavailable',404)
    elif mutation=='missing_edge':
        key=caps[0]['metadata']['url'];value=json.loads(table[key][0]);value['corrections']=[];table[key]=(json.dumps(value).encode(),200)
    calls=[]
    def request(provider,method,url,**kwargs):
        assert provider=='regulations' and method=='GET' and kwargs['allow_redirects'] is False
        calls.append(url)
        if url==mod.BASE_URL+'/documents':
            item=json.loads(__import__('base64').b64decode(root['detail']['raw_base64']))['data']
            items=[item]
            if two_roots:items.append(json.loads(__import__('base64').b64decode(caps[0]['detail']['raw_base64']))['data'])
            raw=json.dumps({'data':items,'meta':{'totalPages':1,'totalElements':len(items)}}).encode();status=200
        else:raw,status=table[url]
        return Response(raw,status=status)
    monkeypatch.setattr(mod,'provider_request',request)
    monkeypatch.setattr(request_policy,'PROVIDER_LIMITS',{})
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    try:
        rows=mod.RegulationsSource(api_key='fixture-key').get_recent_proposed_rules(
            [root['row']['agency_id']],days_back=365,as_of='2026-10-22',
            evidence_policy='complete_document_text_v1',retrieval_policy=V4,publication_policy=PUB)
        payload={'proposed_rules':list(rows),'coverage':rows.coverage,'regulation_evidence':rows.regulation_evidence}
    except SourceFetchError as error:payload={**error.partial_data,'error':str(error)}
    return payload,calls


@pytest.mark.parametrize('kind',['correction','archived','cfpb'])
def test_native_acquisition_roundtrip_and_analysis_view(monkeypatch,kind):
    from tradingagents.strategies.data_sources.regulation_publications import validate_publication_source,analysis_view,validate_analysis_view
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    payload,calls=acquired(monkeypatch,kind)
    assert 'error' not in payload
    assert validate_publication_source(payload)['content_complete'] is True
    frozen=SourceInputStore.decode(SourceInputStore.encode(payload,max_bytes=32*1024**2),max_bytes=32*1024**2)
    assert validate_publication_source(frozen)['content_complete'] is True
    view=analysis_view(frozen,frozen['proposed_rules'][0]['document_id'])
    assert validate_analysis_view(view)==view
    assert len(view['units'])==(2 if kind=='correction' else 1)
    assert len({u['unit_id'] for u in view['units']})==len(view['units'])
    if kind=='correction':assert len([u for u in calls if u.startswith('https://img.federalregister.gov/')])==34


@pytest.mark.parametrize('mutation',['missing_graphic','missing_original','missing_edge'])
def test_failed_native_dependencies_replay_explicitly_without_complete_admission(monkeypatch,mutation):
    from tradingagents.strategies.data_sources.regulation_publications import validate_publication_source,analysis_view
    payload,_=acquired(monkeypatch,mutation=mutation)
    assert payload.get('error')
    assert validate_publication_source(payload)['content_complete'] is False
    assert not payload['regulation_evidence']['corpus']
    with pytest.raises(ValueError,match='incomplete_source'):
        analysis_view(payload,payload['proposed_rules'][0]['document_id'])


def test_archive_consumer_request_and_event_clock_use_forward_bundle(monkeypatch):
    from tradingagents.strategies.data_sources.regulation_publications import analysis_view
    from tradingagents.strategies.data_sources.regulation_assessment import prepare_request
    from tradingagents.strategies.orchestration.event_identity import canonical_observation_time
    payload,_=acquired(monkeypatch,'archived');view=analysis_view(payload,payload['proposed_rules'][0]['document_id'])
    request=prepare_request(view)
    user=json.loads(request.user)
    assert user['units'][0]['text']==view['units'][0]['text']
    assert user['publication_bundle']['publication_kind']=='historical_repost'
    metadata={'posted_date':'2026-10-21T04:00:00Z','available_at':view['bundle']['available_at'],
              'regulation_publication_policy':PUB}
    assert canonical_observation_time('regulatory_pipeline',metadata).isoformat()==view['bundle']['available_at']
    with pytest.raises(ValueError):canonical_observation_time('regulatory_pipeline',{**metadata,'available_at':None})


@pytest.mark.parametrize('kind,old,new',[
    ('archived',b'[[Page 11275]]',b''),
    ('archived',b'SUMMARY:',b'OMITTED SUMMARY:'),
    ('archived',b'[FR Doc. 97-5879 Filed 3-10-97;',b'[FR Doc. 97-5879 Filed 3-09-97;'),
    ('archived',b'[FR Doc. 97-5879 Filed',b'[FR Doc. 97-5879 Filed 3-10-97; 8:45 am]\n[FR Doc. 97-5879 Filed'),
    ('archived',b'RIN 2040-AC87',b'RIN 2040-AC88'),
    ('correction',b'RIN 3235-AN83',b'RIN 3235-AN84'),
    ('correction',b'File No. S7-2026-34',b'File No. S7-2026-35'),
    ('correction',b'DATES:',b'[TEXT OMITTED]\nDATES:'),
    ('correction',b'Filed 10-19-26;',b'Filed 10-18-26;'),
    ('correction',b'    1. On page 63388, in the first column, the DATES section is \ncorrected to read as follows:\n\nDATES: This release was published in the Federal Register on October 5, \n2026. Comments should be received on or before December 4, 2026.\n\n',b''),
])
def test_special_forms_reject_missing_pages_sections_footer_and_authority(kind,old,new):
    from tradingagents.strategies.data_sources.regulation_publications import build_publication_node
    from tradingagents.strategies.data_sources.regulation_evidence import receipt_bytes
    c=capture(kind);rid=c['row']['document_id'];r=c['bodies'][rid];raw=receipt_bytes(r,16*1024**2)
    assert old in raw
    c['bodies'][rid]=original_receipt(raw.replace(old,new),observed_at=r['observed_at'],url=r['url'])
    with pytest.raises(ValueError,match='regulation_'):build_publication_node(c)


def test_reciprocal_edges_also_bind_authority_not_just_dates_and_agency():
    from tradingagents.strategies.data_sources.regulation_publications import build_publication_node,build_publication_bundle
    c=capture('correction');m=json.loads(__import__('base64').b64decode(c['metadata']['raw_base64']))
    m['cfr_references']=[m['cfr_references'][0]]
    c['metadata']=original_receipt(json.dumps(m).encode(),observed_at=STAMP,url=c['metadata']['url'])
    r=c['bodies'][c['row']['document_id']];raw=__import__('base64').b64decode(r['raw_base64']).replace(b'17 CFR Parts 239, 249, 270, and 274',b'17 CFR Part 239')
    c['bodies'][c['row']['document_id']]=original_receipt(raw,observed_at=STAMP,url=r['url'])
    node=build_publication_node(c);o=build_publication_node(capture('original'))
    with pytest.raises(ValueError,match='regulation_'):
        build_publication_bundle(c['row'],{node['manifest_sha256']:node,o['manifest_sha256']:o},capture('original')['metadata'],sealed_at=SEAL)


def test_future_empty_selection_never_becomes_complete(monkeypatch):
    from tradingagents.strategies.data_sources.regulations_source import RegulationsSource
    from tradingagents.strategies.data_sources import regulation_publications as mod
    from tradingagents.strategies.data_sources.evidence import CoverageRecords
    monkeypatch.setattr(mod,'_today',lambda:'2026-10-22',raising=False)
    source=RegulationsSource(api_key='fixture-key')
    monkeypatch.setattr(source,'get_recent_proposed_rules',lambda *a,**k:CoverageRecords([],coverage={'complete':True}))
    with pytest.raises(ValueError,match='future'):
        mod.acquire_publication_rules(source,as_of='2026-10-23',retrieval_policy=V4)


def test_final_deadline_retains_replayable_failed_publication(monkeypatch):
    from tradingagents.strategies.data_sources import regulation_publications as mod
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    clock=[0.0];original=mod.build_publication_bundle
    def delayed(*a,**k):
        value=original(*a,**k);clock[0]=10.0;return value
    monkeypatch.setattr(mod,'build_publication_bundle',delayed)
    with provider_budget('regulations',10,clock=lambda:clock[0],limits=()):
        payload,_=acquired(monkeypatch,'archived')
    summary=mod.validate_publication_source(payload)
    assert summary['raw_content_complete'] is True and summary['content_complete'] is False
    assert summary['acquisition_failure']['boundary']=='source_publication'


@pytest.mark.parametrize('kind',['archived','correction'])
@pytest.mark.parametrize('marker',['EQUATION OMITTED','TIFF OMITTED','FORMULA OMITTED','equation omitted'])
def test_special_copy_rejects_other_omitted_material_markers(kind,marker):
    from tradingagents.strategies.data_sources.regulation_publications import build_publication_node
    from tradingagents.strategies.data_sources.regulation_evidence import receipt_bytes
    c=capture(kind);rid=c['row']['document_id'];r=c['bodies'][rid]
    raw=receipt_bytes(r,16*1024**2);anchor=b'SUMMARY:' if kind=='archived' else b'\nCorrection\n'
    c['bodies'][rid]=original_receipt(raw.replace(anchor,anchor+b'\n['+marker.encode()+b']\n'),observed_at=STAMP,url=r['url'])
    with pytest.raises(ValueError,match='regulation_'):build_publication_node(c)


@pytest.mark.parametrize('kind',['correction','archived','cfpb'])
def test_zero_model_qualification_prepares_actual_consumer_inputs(monkeypatch,kind):
    from scripts.qualify_regulatory_publications import qualification
    payload,_=acquired(monkeypatch,kind);report=qualification(payload)
    assert report['model_calls']==0 and report['source_complete'] is True
    assert report['semantic_assessment_complete'] is False
    doc=report['documents'][0]
    if kind=='correction':
        assert doc['graphics']==34 and doc['recognition_requests_prepared']==9
        assert doc['final_assessment_request_prepared'] is False
    else:assert doc['final_assessment_request_prepared'] is True


def test_discovery_original_prevents_omitting_all_actual_roots(monkeypatch):
    from tradingagents.strategies.data_sources.regulation_publications import validate_publication_source
    payload,_=acquired(monkeypatch,'archived');graph=payload['regulation_evidence']
    payload['proposed_rules']=[]
    for key in ['publication_nodes','corpus','acquisition_failures']:graph[key]={}
    graph['rows']=[]
    graph['coverage'].update(required_rows=0,complete_rows=0,failed_rows=0)
    graph['manifest_sha256']=digest({k:v for k,v in graph.items() if k!='manifest_sha256'})
    with pytest.raises(ValueError,match='regulation_'):validate_publication_source(payload)


@pytest.mark.parametrize('field,value',[
    ('cfr_references',[{'title':99,'part':'9999'}]),('docket_ids',['CFPB-2099-9999']),('action','Final rule.'),
    ('docket_ids',['CFPB-2026-0018','CFPB-2099-9999']),
    ('docket_ids',['CFPB-2026-0018','CFPB-2026-0018']),
])
def test_cfpb_metadata_cannot_contradict_validated_body_authority(field,value):
    from tradingagents.strategies.data_sources.regulation_publications import build_publication_node
    c=capture('cfpb');m=json.loads(__import__('base64').b64decode(c['metadata']['raw_base64']));m[field]=value
    c['metadata']=original_receipt(json.dumps(m).encode(),observed_at=STAMP,url=c['metadata']['url'])
    with pytest.raises(ValueError,match='regulation_'):build_publication_node(c)


def test_named_capture_replay_binds_bytes_code_and_performs_no_network(monkeypatch,tmp_path):
    import requests
    from scripts.qualify_regulatory_publications import capture_document,replay_document,qualification,CODEC
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    import hashlib
    payload,_=acquired(monkeypatch,'archived')
    encoded=SourceInputStore.encode(capture_document(payload),**CODEC);path=tmp_path/'capture.json';path.write_text(encoded)
    sha=hashlib.sha256(encoded.encode()).hexdigest()
    monkeypatch.setattr(requests.sessions.Session,'request',lambda *a,**k:pytest.fail('network during offline replay'))
    frozen=replay_document(path,sha)
    assert qualification(frozen)['source_complete'] is True
    with pytest.raises(ValueError,match='SHA256'):replay_document(path,'0'*64)
    changed=capture_document(payload);changed['code_sha256']={}
    raw=SourceInputStore.encode(changed,**CODEC);path.write_text(raw)
    with pytest.raises(ValueError,match='code differs'):replay_document(path,hashlib.sha256(raw.encode()).hexdigest())


def test_two_discoveries_share_one_exact_dependency_capture(monkeypatch):
    from tradingagents.strategies.data_sources.regulation_publications import validate_publication_source
    payload,calls=acquired(monkeypatch,two_roots=True)
    assert validate_publication_source(payload)['complete_rows']==2
    graph=payload['regulation_evidence']
    assert len(graph['publication_nodes'])==2 and len(graph['corpus'])==2
    assert len([u for u in calls if u.startswith('https://img.federalregister.gov/')])==34
    assert len({tuple(v['node_refs']) for v in graph['corpus'].values()})==1


def test_relationship_closing_clock_cannot_predate_dependency_acquisition():
    from tradingagents.strategies.data_sources.regulation_publications import build_publication_bundle
    from tradingagents.strategies.data_sources.regulation_evidence import receipt_bytes
    value,nodes=bundle('archived');r=value['closing_relationship_receipt']
    closing=original_receipt(receipt_bytes(r,2*1024**2),observed_at='2026-10-21T23:30:00+00:00',url=r['url'])
    with pytest.raises(ValueError,match='regulation_'):
        build_publication_bundle(value['identity'],nodes,closing,sealed_at=SEAL)
