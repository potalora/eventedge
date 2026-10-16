"""Current official SEC originals: crosswalk binds identity, never omitted assets."""
import json
from pathlib import Path

import pytest

from tradingagents.strategies.data_sources.regulation_evidence import original_receipt, validate_regulation_source
from tradingagents.strategies.data_sources.regulation_government_copy import extract_government_copy

pytestmark = pytest.mark.scheduled_acceptance
FIXTURES = Path(__file__).parent / 'fixtures'
V3 = 'verified_government_copy_v3'


def originals(label='sec_cross_trading'):
    detail = (FIXTURES / ('regulation_detail_' + label + '.json')).read_bytes()
    body = (FIXTURES / ('regulation_govinfo_' + label + '.htm')).read_bytes()
    metadata = (FIXTURES / ('regulation_fr_crosswalk_' + label + '.json')).read_bytes()
    fr = json.loads(detail)['data']['attributes']['frDocNum']
    receipt = original_receipt(metadata, observed_at='2026-10-15T23:00:00+00:00',
        url='https://www.federalregister.gov/api/v1/documents/' + fr + '.json')
    return detail, body, receipt


def test_actual_complete_sec_notice_replays_missing_catalogue_metadata():
    detail, body, receipt = originals()
    proof = extract_government_copy(detail, body, retrieval_policy=V3, crosswalk_receipt=receipt)
    assert proof['identity']['document_id'] == 'SEC-2026-6404-0001'
    assert proof['identity']['first_page'] == 65622 and proof['identity']['last_page'] == 65677
    assert proof['crosswalk_receipt'] == receipt
    assert proof['text'].endswith('BILLING CODE 8011-01-P\n\n\n')
    assert 'Investment Company Cross Trading' in proof['text']
    assert '\x00' not in proof['text']


@pytest.mark.parametrize('label', ['sec_interval', 'sec_custody', 'sec_performance'])
def test_actual_sec_omitted_graphics_cannot_become_complete_text(label):
    detail, body, receipt = originals(label)
    assert b'[TIFF OMITTED]' in body
    with pytest.raises(ValueError, match='missing_regulation_government_graphics'):
        extract_government_copy(detail, body, retrieval_policy=V3, crosswalk_receipt=receipt)


def acquire(monkeypatch, *, metadata_raw=None, metadata_status=200, duplicate=False):
    from tradingagents.strategies.data_sources import regulations_source as source
    from test_regulation_source_evidence import Response
    detail, body, receipt = originals()
    item = json.loads(detail)['data']
    attrs, docid = item['attributes'], item['id']
    primary = next(row['fileUrl'] for row in attrs['fileFormats'] if row['format'] == 'html')
    copy_url = 'https://www.govinfo.gov/content/pkg/FR-2026-10-15/html/2026-21037.htm'
    metadata = (FIXTURES / 'regulation_fr_crosswalk_sec_cross_trading.json').read_bytes()
    transport = {source.BASE_URL + '/documents/' + docid: (detail, 200),
        primary: (b'Access denied', 403), copy_url: (body, 200),
        receipt['url']: (metadata if metadata_raw is None else metadata_raw, metadata_status)}
    calls = []
    def request(provider, method, url, **kwargs):
        assert kwargs['allow_redirects'] is False
        if url == source.BASE_URL + '/documents':
            changed = {**attrs, 'title': 'Conflicting title'} if duplicate and calls else attrs
            raw, status = json.dumps({'data': [{'id': docid, 'attributes': changed}],
                'meta': {'totalPages': 1, 'totalElements': 1}}).encode(), 200
        else:
            raw, status = transport[url]
        calls.append(url)
        return Response(raw, status=status)
    monkeypatch.setattr(source, 'provider_request', request)
    rules = source.RegulationsSource(api_key='fixture-key').get_recent_proposed_rules(
        agencies=['SEC'] * (2 if duplicate else 1), days_back=14, as_of='2026-10-15',
        evidence_policy='complete_document_text_v1', retrieval_policy=V3)
    return {'proposed_rules': list(rules), 'coverage': rules.coverage,
        'regulation_evidence': rules.regulation_evidence}, calls


def test_sec_source_retains_original_403_and_crosswalk_before_complete_copy(monkeypatch):
    payload, calls = acquire(monkeypatch)
    assert validate_regulation_source(payload)['content_complete'] is True
    unit = payload['regulation_evidence']['corpus']['SEC-2026-6404-0001']['units'][0]
    assert unit['primary_failure']['http_status'] == 403
    assert calls.index(unit['government_copy']['crosswalk_receipt']['url']) < calls.index(unit['receipt']['url'])


@pytest.mark.parametrize('raw,status', [(b'Access denied', 403), (b'{}', 200), (b'{bad-json', 200)])
def test_sec_unavailable_or_unbound_crosswalk_keeps_failed_coverage(monkeypatch, raw, status):
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    with pytest.raises(SourceFetchError) as error:
        acquire(monkeypatch, metadata_raw=raw, metadata_status=status)
    payload = error.value.partial_data
    assert validate_regulation_source(payload)['content_complete'] is False


def test_sec_conflicting_duplicate_preserves_acquisition_order_for_failed_replay(monkeypatch):
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    with pytest.raises(SourceFetchError) as error:
        acquire(monkeypatch, duplicate=True)
    payload = error.value.partial_data
    assert validate_regulation_source(payload)['content_complete'] is False
    attempts = payload['regulation_evidence']['acquisition_failures']['SEC-2026-6404-0001']['retrieval_attempts']
    assert [row['receipt']['url'] for row in attempts] == [
        'https://downloads.regulations.gov/SEC-2026-6404-0001/content.html',
        'https://www.federalregister.gov/api/v1/documents/2026-21037.json',
        'https://www.govinfo.gov/content/pkg/FR-2026-10-15/html/2026-21037.htm']


def test_declared_image_inventory_cannot_be_ignored_by_text_only_copy():
    detail, body, receipt = originals()
    metadata = json.loads((FIXTURES / 'regulation_fr_crosswalk_sec_cross_trading.json').read_bytes())
    metadata['images'] = {'EP15OC26.001': {'original_size': 'https://img.federalregister.gov/EP15OC26.001/image.png'}}
    mutated = original_receipt(json.dumps(metadata).encode(), observed_at=receipt['observed_at'], url=receipt['url'])
    with pytest.raises(ValueError, match='invalid_regulation_government_graphics'):
        extract_government_copy(detail, body, retrieval_policy=V3, crosswalk_receipt=mutated)


@pytest.mark.parametrize('old,new', [
    (b'[[Page 65650]]', b'[[Page 65651]]'),
    (b'[FR Doc No: 2026-21037]', b'[FR Doc No: 2026-21038]'),
    (b'[FR Doc. 2026-21037 Filed', b'[FR Doc. 2026-21038 Filed'),
    (b'RIN 3235-AN45', b'RIN 3235-AN46'),
    (b'File No. S7-2026-36', b'File No. S7-2026-37'),
    (b'17 CFR Parts 270 and 274', b'17 CFR Parts 270 and 275'),
    (b'S&P Global', b'S&unknown Global'),
    (b'\x00\x00Federal Register', b'\x00Federal Register'),
    (b'SUMMARY:', b'\x00SUMMARY:'),
])
def test_complete_sec_notice_does_not_admit_changed_authority_pages_or_unaccounted_controls(old, new):
    detail, body, receipt = originals()
    assert old in body
    with pytest.raises(ValueError, match='(?:invalid|unsupported)_regulation_government_'):
        extract_government_copy(detail, body.replace(old, new), retrieval_policy=V3, crosswalk_receipt=receipt)


@pytest.mark.parametrize('field,value', [('document_id', 'SEC-2026-6405-0001'), ('agency_id', 'FTC')])
def test_official_crosswalk_requires_exact_regulations_original_document_id(field, value):
    detail, body, receipt = originals()
    metadata = json.loads((FIXTURES / 'regulation_fr_crosswalk_sec_cross_trading.json').read_bytes())
    metadata['regulations_dot_gov_info'][field] = value
    mutated = original_receipt(json.dumps(metadata).encode(), observed_at=receipt['observed_at'], url=receipt['url'])
    with pytest.raises(ValueError, match='invalid_regulation_government_crosswalk'):
        extract_government_copy(detail, body, retrieval_policy=V3, crosswalk_receipt=mutated)


def test_sec_crosswalk_rejects_nonfinite_json_even_in_unrecognized_fields():
    detail, body, receipt = originals()
    metadata = json.loads((FIXTURES / 'regulation_fr_crosswalk_sec_cross_trading.json').read_bytes())
    metadata['unrecognized'] = float('nan')
    mutated = original_receipt(json.dumps(metadata).encode(), observed_at=receipt['observed_at'], url=receipt['url'])
    with pytest.raises(ValueError, match='invalid_regulation_government_crosswalk'):
        extract_government_copy(detail, body, retrieval_policy=V3, crosswalk_receipt=mutated)


@pytest.mark.parametrize('field,value', [('document_number', '2026-21038'),
    ('publication_date', '2026-10-14'), ('title', 'Another Notice'), ('start_page', 65623),
    ('docket_ids', ['Release No. IC-36359', 'File No. S7-2026-36']),
    ('regulation_id_numbers', ['3235-AN46']), ('cfr_references', [{'title': 17, 'part': '275'}])])
def test_sec_crosswalk_or_body_mismatch_cannot_bind_other_notice(field, value):
    detail, body, receipt = originals()
    metadata = json.loads((FIXTURES / 'regulation_fr_crosswalk_sec_cross_trading.json').read_bytes())
    metadata[field] = value
    mutated = original_receipt(json.dumps(metadata).encode(), observed_at=receipt['observed_at'], url=receipt['url'])
    with pytest.raises(ValueError, match='(?:invalid|unsupported)_regulation_government_'):
        extract_government_copy(detail, body, retrieval_policy=V3, crosswalk_receipt=mutated)
