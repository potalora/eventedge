"""Full regulatory content cannot be replaced by discovery titles or abstracts."""
from copy import deepcopy
import importlib
import json
from pathlib import Path

import pytest


DOC = 'EPA-R03-OAR-2026-6205-0001'
STAMP = '2026-10-11T16:00:00+00:00'
HTML = b'<html><body><h1>Proposed rule</h1><p>This action applies to ACME CORPORATION.</p><p>' + b'Full text. ' * 600 + b'</p></body></html>'


def fixture(*, attachment=False):
    row = dict(document_id=DOC, title='Fixture rule', agency_id='EPA',
               document_type='Proposed Rule', posted_date='2026-10-07T04:00:00Z',
               docket_id='EPA-R03-OAR-2026-6205', summary='')
    attrs = dict(title=row['title'], agencyId='EPA', documentType='Proposed Rule',
                 postedDate=row['posted_date'], docketId=row['docket_id'], docAbstract=None,
                 withdrawn=False, restrictReason=None, restrictReasonType=None,
                 fileFormats=[dict(fileUrl=f'https://downloads.regulations.gov/{DOC}/content.html',
                                   format='html', size=len(HTML)),
                              dict(fileUrl=f'https://downloads.regulations.gov/{DOC}/content.pdf',
                                   format='pdf', size=10000)])
    detail = {'data': {'id': DOC, 'type': 'documents', 'attributes': attrs,
                      'relationships': {'attachments': {'data': []}}},
              'links': {'self': f'https://regulations.gov/v4/documents/{DOC}?include=attachments'}}
    bodies = {DOC: HTML}
    if attachment:
        rid = DOC + '_attachment_1'
        detail['data']['relationships']['attachments']['data'] = [{'id': rid, 'type': 'attachments'}]
        detail['included'] = [{'id': rid, 'type': 'attachments', 'attributes': {
            'title': 'Appendix', 'docOrder': 1, 'restrictReason': None, 'restrictReasonType': None,
            'fileFormats': [{'fileUrl': f'https://downloads.regulations.gov/{rid}/content.txt',
                             'format': 'txt', 'size': 24}]}}]
        bodies[rid] = b'Complete appendix text.\n'
        detail['included'][0]['attributes']['fileFormats'][0]['size'] = len(bodies[rid])
    return row, detail, bodies


def module():
    return importlib.import_module('tradingagents.strategies.data_sources.regulation_evidence')


def build(*, attachment=False):
    row, detail, bodies = fixture(attachment=attachment)
    return module().build_regulation_evidence(row, json.dumps(detail).encode(), bodies,
                                               observed_at=STAMP)


def test_empty_summary_does_not_replace_full_body_and_all_text_is_retained():
    value = build()
    assert value['content_status'] == 'complete'
    assert len(value['units'][0]['text']) > 5000
    assert value['units'][0]['text'].endswith('Full text.')
    assert len(value['resources'][0]['formats']) == 2
    assert module().validate_regulation_evidence(value) == value


@pytest.mark.parametrize('field,value', [('id', 'EPA-OTHER-0001'), ('type', 'comments')])
def test_detail_identity_mismatch_cannot_replace_discovered_rule(field, value):
    row, detail, bodies = fixture()
    detail['data'][field] = value
    with pytest.raises(ValueError, match='invalid_regulation_identity'):
        module().build_regulation_evidence(row, json.dumps(detail).encode(), bodies, observed_at=STAMP)


@pytest.mark.parametrize('mutation', ['missing', 'orphan', 'duplicate', 'wrong_type'])
def test_attachment_relationship_is_exact_not_a_partial_inventory(mutation):
    row, detail, bodies = fixture(attachment=True)
    if mutation == 'missing': detail.pop('included')
    elif mutation == 'orphan': detail['data']['relationships']['attachments']['data'] = []
    elif mutation == 'duplicate': detail['included'] *= 2
    else: detail['included'][0]['type'] = 'documents'
    with pytest.raises(ValueError, match='invalid_regulation_attachments'):
        module().build_regulation_evidence(row, json.dumps(detail).encode(), bodies, observed_at=STAMP)


@pytest.mark.parametrize('url', [
    'https://evil.example/content.html',
    f'https://downloads.regulations.gov.evil.example/{DOC}/content.html',
    f'https://user@downloads.regulations.gov/{DOC}/content.html',
    f'https://downloads.regulations.gov/{DOC}/content.html?key=secret',
    f'https://downloads.regulations.gov/{DOC}/../content.html',
    'https://downloads.regulations.gov/OTHER-ID/content.html',
])
def test_unbound_advertised_download_is_rejected(url):
    row, detail, bodies = fixture()
    detail['data']['attributes']['fileFormats'][0]['fileUrl'] = url
    with pytest.raises(ValueError, match='invalid_regulation_download_url'):
        module().build_regulation_evidence(row, json.dumps(detail).encode(), bodies, observed_at=STAMP)


def test_missing_attachment_body_is_not_complete():
    row, detail, bodies = fixture(attachment=True)
    bodies.pop(DOC + '_attachment_1')
    with pytest.raises(ValueError, match='missing_regulation_body'):
        module().build_regulation_evidence(row, json.dumps(detail).encode(), bodies, observed_at=STAMP)


def test_declared_body_size_detects_a_truncated_response():
    row, detail, bodies = fixture()
    bodies[DOC] = HTML[:-5]
    with pytest.raises(ValueError, match='invalid_regulation_body_size'):
        module().build_regulation_evidence(row, json.dumps(detail).encode(), bodies, observed_at=STAMP)


@pytest.mark.parametrize('html', [b'<html><body><img src="chart.png"></body></html>',
                                  b'<html><body>Text<img src="chart.png"></body></html>',
                                  b'<html><body>Text<iframe src="external"></iframe></body></html>'])
def test_unassessed_visual_or_embedded_content_is_not_complete_text(html):
    row, detail, bodies = fixture()
    bodies[DOC] = html
    detail['data']['attributes']['fileFormats'][0]['size'] = len(html)
    with pytest.raises(ValueError, match='unsupported_regulation_embedded_content'):
        module().build_regulation_evidence(row, json.dumps(detail).encode(), bodies, observed_at=STAMP)


def test_envelope_tampering_cannot_make_changed_text_original():
    value = build(attachment=True)
    value['units'][0]['text'] = 'Neutral, no affected company.'
    with pytest.raises(ValueError): module().validate_regulation_evidence(value)


def test_unavailable_pdf_worker_is_not_parent_process_parsing():
    row, detail, bodies = fixture()
    detail['data']['attributes']['fileFormats'] = [detail['data']['attributes']['fileFormats'][1]]
    bodies[DOC] = b'%PDF-1.7\n%%EOF\n'
    detail['data']['attributes']['fileFormats'][0]['size'] = len(bodies[DOC])
    with pytest.raises(ValueError, match='regulation_pdf_parser_unavailable'):
        module().build_regulation_evidence(row, json.dumps(detail).encode(), bodies, observed_at=STAMP)


def test_cancellation_propagates_without_claiming_source_inadequacy():
    row, detail, bodies = fixture()
    def expired(): raise TimeoutError('expired')
    with pytest.raises(TimeoutError):
        module().build_regulation_evidence(row, json.dumps(detail).encode(), bodies,
                                           observed_at=STAMP, check=expired)


def test_real_provider_detail_without_included_has_verified_empty_attachment_set():
    raw = (Path(__file__).parent / 'fixtures/regulation_detail_wv.json').read_bytes()
    attrs = json.loads(raw)['data']['attributes']
    row = {'document_id': DOC, 'title': attrs['title'], 'agency_id': attrs['agencyId'],
           'document_type': attrs['documentType'], 'posted_date': attrs['postedDate'],
           'docket_id': attrs['docketId']}
    plan = module().detail_plan(row, raw, observed_at=STAMP)
    assert len(plan['resources']) == 1
    assert plan['resources'][0]['selected'] == {
        'fileUrl': 'https://downloads.regulations.gov/EPA-R03-OAR-2026-6205-0001/content.html',
        'format': 'html', 'size': 11539}
    assert plan['detail_receipt']['sha256'] == '04c7110fce20d743bbf895e9ef1a5133e39dde967dd131413b680222a0f3f698'


@pytest.mark.parametrize('html', [b'<html><body>Partial text',
    b'<html><body>Text<script>loadMoreText()</script></body></html>',
    b'<html><body><p style="background-image:url(chart.png)">Text</p></body></html>'])
def test_incomplete_or_dynamic_html_cannot_claim_complete_rule_text(html):
    row, detail, bodies = fixture()
    bodies[DOC] = html
    detail['data']['attributes']['fileFormats'][0]['size'] = len(html)
    with pytest.raises(ValueError):
        module().build_regulation_evidence(row, json.dumps(detail).encode(), bodies, observed_at=STAMP)
