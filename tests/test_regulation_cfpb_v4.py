"""Actual CFPB RFI originals plus explicitly mutated rejection controls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from tradingagents.strategies.data_sources.regulation_government_copy import (
    extract_government_copy, government_copy_plan,
)

pytestmark = pytest.mark.scheduled_acceptance
FIXTURES = Path(__file__).parent / 'fixtures'
V4 = 'verified_government_copy_v4'


def originals():
    return ((FIXTURES / 'regulation_detail_cfpb_0018.json').read_bytes(),
            (FIXTURES / 'regulation_govinfo_cfpb_0018.htm').read_bytes())


def test_actual_cfpb_originals_keep_exact_capture_identity():
    manifest = json.loads((FIXTURES / 'regulation_cfpb_20261022_manifest.json').read_text())
    for name, receipt in manifest['files'].items():
        raw = (FIXTURES / name).read_bytes()
        assert len(raw) == receipt['bytes']
        assert hashlib.sha256(raw).hexdigest() == receipt['sha256']


def test_actual_cfpb_rfi_preserves_complete_notice_and_action():
    detail, body = originals()
    proof = extract_government_copy(detail, body, retrieval_policy=V4)
    assert proof['identity']['document_id'] == 'CFPB-2026-0018-0001'
    assert proof['identity']['cfr_part'] == '12 CFR Parts 1024 and 1026'
    assert proof['notice_metadata']['action'] == 'Notice; request for information.'
    assert proof['extraction_version'] == 'govinfo-fr-pre-v4'
    assert proof['text'].endswith('BILLING CODE 4810-AM-P\n\n\n')
    assert 'Comments must be received on or before August 10, 2026.' in proof['text']
    assert all(name + ':' in proof['text'] for name in (
        'SUMMARY', 'DATES', 'ADDRESSES', 'FOR FURTHER INFORMATION CONTACT',
        'SUPPLEMENTARY INFORMATION'))
    assert [row['kind'] for row in proof['transformations']].count('cloudflare_email_xor_v1') == 2
    assert any(row['kind'] == 'govinfo_bullet_notation_v1' for row in proof['transformations'])


@pytest.mark.parametrize('policy', ['verified_government_copy_v1',
                                    'verified_government_copy_v2',
                                    'verified_government_copy_v3'])
def test_earlier_policies_keep_cfpb_outside_their_admitted_subset(policy):
    with pytest.raises(ValueError, match='unsupported_regulation_government_notice'):
        extract_government_copy(*originals(), retrieval_policy=policy)


@pytest.mark.parametrize('field,value', [
    ('cfrPart', '17 CFR Parts 1024 and 1026'),
    ('cfrPart', '12 CFR Part 1024'),
    ('cfrPart', '12 CFR Parts 1024 and 1024'),
    ('cfrPart', '12 CFR Parts 1024-1026'),
    ('cfrPart', '12 CFR Parts 1024 and 1027'),
    ('agencyId', 'DOL'), ('documentType', 'Rule'),
    ('postedDate', '2025-07-09T04:00:00Z'),
    ('title', 'Unrelated request for information'),
])
def test_mutated_cfpb_catalogue_cannot_change_authority_or_scope(field, value):
    detail, body = originals()
    altered = json.loads(detail)
    altered['data']['attributes'][field] = value
    with pytest.raises(ValueError, match='(?:invalid|unsupported)_regulation_'):
        extract_government_copy(json.dumps(altered).encode(), body, retrieval_policy=V4)


@pytest.mark.parametrize('old,new', [
    (b'12 CFR Parts 1024 and 1026', b'12 CFR Parts 1024 and 1027'),
    (b'Docket No. CFPB-2026-0018', b'Docket No. CFPB-2026-0019'),
    (b'AGENCY: Consumer Financial Protection Bureau.', b'AGENCY: Securities and Exchange Commission.'),
    (b'ACTION: Notice; request for information.', b'ACTION: Final rule.'),
    (b'Request for Information Regarding Promoting Access', b'Request for Information Regarding Preventing Access'),
    (b'[[Page 42383]]', b'[[Page 42385]]'),
    (b'SUPPLEMENTARY INFORMATION:', b'ADDITIONAL INFORMATION:'),
    (b'BILLING CODE 4810-AM-P', b'BILLING CODE 4810-AM-P\n[GRAPHIC] [TIFF OMITTED] TP09JY26.000'),
    (b'<bullet>', b'<bullet hidden="yes">'),
])
def test_mutated_official_body_cannot_drop_identity_material_or_boundary(old, new):
    detail, body = originals()
    assert old in body
    with pytest.raises(ValueError, match='(?:invalid|unsupported)_regulation_'):
        extract_government_copy(detail, body.replace(old, new), retrieval_policy=V4)


def acquire(monkeypatch):
    from tradingagents.strategies.data_sources import regulations_source as mod, request_policy
    from test_regulation_source_evidence import Response
    detail, body = originals()
    item = json.loads(detail)['data']; attrs = item['attributes']; docid = item['id']
    native = next(row['fileUrl'] for row in attrs['fileFormats'] if row['format'] == 'html')
    government = 'https://www.govinfo.gov/content/pkg/FR-2026-07-09/html/2026-13834.htm'
    replies = {
        mod.BASE_URL + '/documents': (json.dumps({'data': [item], 'meta': {
            'totalPages': 1, 'totalElements': 1}}).encode(), 200),
        mod.BASE_URL + '/documents/' + docid: (detail, 200),
        native: (b'Access denied', 403), government: (body, 200),
    }
    calls = []
    def request(provider, method, url, **kwargs):
        calls.append(url)
        assert provider == 'regulations' and kwargs['allow_redirects'] is False
        if url == government:
            assert kwargs.get('headers', {}) == {}
        raw, status = replies[url]
        return Response(raw, status=status)
    monkeypatch.setattr(mod, 'provider_request', request)
    monkeypatch.setattr(request_policy, 'PROVIDER_LIMITS', {})
    rows = mod.RegulationsSource(api_key='synthetic').get_recent_proposed_rules(
        agencies=['CFPB'], days_back=14, as_of='2026-07-10',
        evidence_policy='complete_document_text_v1', retrieval_policy=V4)
    assert calls == list(replies)
    return rows


def test_actual_cfpb_acquisition_freeze_and_complete_consumer_request(monkeypatch):
    from tradingagents.strategies.data_sources.regulation_evidence import validate_regulation_source
    from tradingagents.strategies.data_sources.regulation_assessment import prepare_request
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    rows = acquire(monkeypatch)
    payload = {'proposed_rules': list(rows), 'coverage': rows.coverage,
               'regulation_evidence': rows.regulation_evidence}
    assert validate_regulation_source(payload)['content_complete'] is True
    frozen = SourceInputStore.decode(SourceInputStore.encode(payload))
    assert validate_regulation_source(frozen)['content_complete'] is True
    doc = next(iter(frozen['regulation_evidence']['corpus'].values()))
    request = prepare_request(doc)
    assert json.loads(request.user)['units'][0]['text'] == doc['units'][0]['text']
    assert 'ACTION: Notice; request for information.' in request.user
    assert doc['units'][0]['primary_failure']['http_status'] == 403
    altered = deepcopy(frozen)
    next(iter(altered['regulation_evidence']['corpus'].values()))['units'][0]['text'] += ' extra'
    with pytest.raises(ValueError, match='invalid_regulation_'):
        validate_regulation_source(altered)


def test_v4_preserves_complete_sec_graphics_and_missing_asset_failure_replay(monkeypatch):
    from tradingagents.strategies.data_sources.regulation_evidence import validate_regulation_source
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    from tradingagents.strategies.data_sources import request_policy
    import test_regulation_government_graphics_v3 as fixtures
    monkeypatch.setattr(fixtures, 'V3', V4)
    monkeypatch.setattr(request_policy, 'PROVIDER_LIMITS', {})
    payload, calls = fixtures.acquire(monkeypatch, 'sec_interval')
    assert validate_regulation_source(payload)['content_complete'] is True
    unit = next(iter(payload['regulation_evidence']['corpus'].values()))['units'][0]
    assert len(unit['government_copy']['graphics']) == 34
    assert sum(url.startswith('https://img.federalregister.gov/') for url in calls) == 34
    with pytest.raises(SourceFetchError) as failed:
        fixtures.acquire(monkeypatch, 'sec_interval', asset_status=404)
    assert validate_regulation_source(failed.value.partial_data)['content_complete'] is False


def test_v4_does_not_accept_a_correction_as_a_standalone_notice():
    detail = (FIXTURES / 'regulation_detail_sec_correction_6502.json').read_bytes()
    with pytest.raises(ValueError, match='invalid_regulation_government_identity'):
        government_copy_plan(detail, retrieval_policy=V4)
