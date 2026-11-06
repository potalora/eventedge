"""Official source bytes plus intentionally constructed model transport outputs.

This tests nonempty acquisition, image transport, all-horizon retention and report
replay together. The model responses below are explicit schema fixtures, NOT
recognition of these source images and NOT evidence of semantic accuracy. Native
image recognition and native full-generation acceptance are separate obligations.
"""
from functools import lru_cache
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
LABELS = ('sec_interval', 'sec_custody', 'sec_performance')


@lru_cache(maxsize=1)
def originals():
    rows, urls = [], {}
    for label in LABELS:
        detail = (FIXTURES / ('regulation_detail_' + label + '.json')).read_bytes()
        parsed = json.loads(detail)['data']
        attrs = parsed['attributes']
        rows.append({'id':parsed['id'], 'attributes':attrs})
        urls['https://api.regulations.gov/v4/documents/' + parsed['id']] = (detail, 200)
        for spec in attrs['fileFormats']:
            urls[spec['fileUrl']] = (b'Access denied', 403)
        urls['https://www.govinfo.gov/content/pkg/FR-' + attrs['postedDate'][:10] + '/html/'
             + attrs['frDocNum'] + '.htm'] = ((FIXTURES / ('regulation_govinfo_' + label + '.htm')).read_bytes(), 200)
        crosswalk = (FIXTURES / ('regulation_fr_crosswalk_' + label + '.json')).read_bytes()
        urls['https://www.federalregister.gov/api/v1/documents/' + attrs['frDocNum'] + '.json'] = (crosswalk, 200)
        for graphic, sizes in json.loads(crosswalk)['images'].items():
            urls[sizes['original_size']] = ((FIXTURES / 'regulation_graphics' / (graphic + '.png')).read_bytes(), 200)
    rows.sort(key=lambda row: row['attributes']['postedDate'], reverse=True)
    return rows, urls


def response(fixture, url, params, fault):
    rows, urls = originals()
    if url == 'https://api.regulations.gov/v4/documents':
        selected = rows if params.get('filter[agencyId]') == 'SEC' else []
        return fixture.response({'data':selected, 'meta':{'totalPages':1, 'totalElements':len(selected)}})
    if url not in urls:
        return None
    body, status = urls[url]
    if fault == 'regulation_graphics_asset_missing' and url.endswith('EP05OC26.007_original_size.png'):
        body, status = b'Unavailable', 404
    result = fixture.response(body, status)
    result.url = url
    return result


def model(payload, fault):
    from tradingagents.strategies.data_sources.regulation_graphics import (
        RECOGNITION_VERSION, REVIEW_VERSION, REVIEW_CHECKS, REQUEST_VERSIONS,
    )
    context = payload.get('request', {})
    if context.get('version') in REQUEST_VERSIONS:
        rows = context['assets']
        if context['stage'] == 'recognition':
            # A deliberately artificial transport body. It does not claim to
            # describe the official image in a fixture's scientific evidence.
            text = 'Constructed schema fixture; no native semantic assessment.'
            graphics = [{'id':row['id'], 'sha256':row['sha256'], 'status':'complete',
                'kind':'prose', 'transcript':text, 'elements':[{'id':'fixture','kind':'prose',
                    'text':text, 'source':'image','references':[], 'precision':'not_numeric'}],
                'unresolved':[]} for row in rows]
            return {'version':RECOGNITION_VERSION, 'request_identity_sha256':context['request_identity_sha256'],
                    'graphics':graphics}, 'regulation_graphics_recognition'
        failed = fault == 'regulation_graphics_review_partial'
        graphics = [{'id':row['id'], 'sha256':row['sha256'], 'material_complete':not failed,
            'checked_element_ids':[e['id'] for e in recognized['elements']],
            'checks':{name:not failed for name in REVIEW_CHECKS},
            'errors':[{'element_id':None,'issue':'Constructed material omission'}] if failed else []}
            for row,recognized in zip(rows,payload['recognition']['graphics'])]
        return {'version':REVIEW_VERSION, 'request_identity_sha256':context['request_identity_sha256'],
            'recognition_receipt_sha256':context['recognition_receipt_sha256'], 'graphics':graphics}, 'regulation_graphics_review'
    if payload.get('contract_version') == 'regulation-assessment-v2':
        return {'contract_version':'regulation-assessment-v2','document_id':payload['identity']['document_id'],
            'outcome':'assessed_no_directional_thesis','direction':'neutral','conviction':0.0,
            'target_ticker':None,'target_binding_sha256':None,
            'rationale':'Constructed transport fixture for complete-rule replay; no native semantic claim.',
            'evidence_claim':payload['units'][0]['text'][:40],
            'citations':[{'unit_id':u['unit_id'],'quote':u['text'][:40]} for u in payload['units']],
            'unresolved_material_dependencies':[]}, 'regulation_thesis'
    return None
