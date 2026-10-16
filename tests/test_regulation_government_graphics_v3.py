"""V3 retains the complete 98-image current official corpus, without OCR claims."""
import hashlib
import json
import struct
import zlib

import pytest

from test_regulation_government_sec import originals, FIXTURES, V3
from tradingagents.strategies.data_sources.regulation_evidence import original_receipt
from tradingagents.strategies.data_sources.regulation_government_copy import extract_government_copy

pytestmark = pytest.mark.scheduled_acceptance
CASES = [('sec_interval', 34), ('sec_custody', 50), ('sec_performance', 14)]


def graphic_receipts(label):
    _, _, metadata_receipt = originals(label)
    metadata = json.loads((FIXTURES / ('regulation_fr_crosswalk_' + label + '.json')).read_bytes())
    return {key: original_receipt((FIXTURES / 'regulation_graphics' / (key + '.png')).read_bytes(),
        observed_at=metadata_receipt['observed_at'], url=value['original_size'])
        for key, value in metadata['images'].items()}


@pytest.mark.parametrize('label,count', CASES)
def test_actual_complete_graphic_inventory_binds_every_original_size_asset(label, count):
    from tradingagents.strategies.data_sources.regulation_government_copy import government_graphics_plan
    detail, body, metadata = originals(label)
    plan = government_graphics_plan(detail, body, crosswalk_receipt=metadata, retrieval_policy=V3)
    receipts = graphic_receipts(label)
    assert len(plan) == count and {row['graphic_id'] for row in plan} == set(receipts)
    proof = extract_government_copy(detail, body, crosswalk_receipt=metadata,
        retrieval_policy=V3, graphics_receipts=receipts)
    assert proof['representation'] == 'preserves_preformatted_layout_with_retained_graphics'
    assert len(proof['graphics']) == count
    assert proof['extraction_version'] == 'govinfo-fr-pre-v3'
    for row in proof['graphics']:
        assert proof['text'][row['marker_codepoint_start']:row['marker_codepoint_end']].strip().endswith(row['marker_id'])
        raw = (FIXTURES / 'regulation_graphics' / (row['graphic_id'] + '.png')).read_bytes()
        assert row['png_sha256'] == hashlib.sha256(raw).hexdigest() == row['receipt']['sha256']
        assert row['publisher_md5'] == hashlib.md5(raw).hexdigest()


@pytest.mark.parametrize('label,count', CASES)
def test_one_missing_required_graphic_never_becomes_complete_text(label, count):
    detail, body, metadata = originals(label)
    receipts = graphic_receipts(label)
    receipts.pop(next(iter(receipts)))
    with pytest.raises(ValueError, match='missing_regulation_government_graphics'):
        extract_government_copy(detail, body, crosswalk_receipt=metadata,
            retrieval_policy=V3, graphics_receipts=receipts)


@pytest.mark.parametrize('mutation', ['truncated', 'crc', 'wrong_asset', 'wrong_url', 'oversized', 'zero_dimension'])
def test_invalid_graphic_receipt_or_png_is_not_complete(mutation):
    detail, body, metadata = originals('sec_performance')
    receipts = graphic_receipts('sec_performance')
    key = sorted(receipts)[0]
    raw = (FIXTURES / 'regulation_graphics' / (key + '.png')).read_bytes()
    url = receipts[key]['url']
    if mutation == 'truncated': raw = raw[:-1]
    elif mutation == 'crc': raw = raw[:40] + bytes([raw[40] ^ 1]) + raw[41:]
    elif mutation == 'wrong_asset': raw = (FIXTURES / 'regulation_graphics' / (sorted(receipts)[1] + '.png')).read_bytes()
    elif mutation == 'wrong_url': url = url.replace('_original_size', '_large')
    elif mutation == 'oversized': raw = b'x' * (2 * 1024**2 + 1)
    elif mutation == 'zero_dimension': raw = raw[:16] + b'\0\0\0\0' + raw[20:]
    receipts[key] = original_receipt(raw, observed_at=receipts[key]['observed_at'], url=url)
    with pytest.raises(ValueError, match='(?:invalid|unsupported)_regulation_'):
        extract_government_copy(detail, body, crosswalk_receipt=metadata,
            retrieval_policy=V3, graphics_receipts=receipts)


def test_unadvertised_or_duplicate_body_marker_cannot_hide_in_inventory():
    from tradingagents.strategies.data_sources.regulation_government_copy import government_graphics_plan
    detail, body, metadata = originals('sec_interval')
    marker = b'[GRAPHIC] [TIFF OMITTED] TP05OC26.001'
    with pytest.raises(ValueError, match='invalid_regulation_government_graphics'):
        government_graphics_plan(detail, body.replace(marker, marker + b'\n' + marker),
            crosswalk_receipt=metadata, retrieval_policy=V3)


def acquire(monkeypatch, label, *, asset_status=200, invalid_asset=False, duplicate=False):
    from tradingagents.strategies.data_sources import regulations_source as source
    from test_regulation_source_evidence import Response
    detail, body, crosswalk = originals(label)
    attrs = json.loads(detail)['data']['attributes']
    docid = json.loads(detail)['data']['id']
    original_url = next(row['fileUrl'] for row in attrs['fileFormats'] if row['format'] == 'html')
    copy_url = 'https://www.govinfo.gov/content/pkg/FR-' + attrs['postedDate'][:10] + '/html/' + attrs['frDocNum'] + '.htm'
    metadata = (FIXTURES / ('regulation_fr_crosswalk_' + label + '.json')).read_bytes()
    transport = {source.BASE_URL + '/documents/' + docid: (detail, 200), original_url: (b'Access denied', 403),
        copy_url: (body, 200), crosswalk['url']: (metadata, 200)}
    receipts = graphic_receipts(label)
    first = sorted(receipts)[0]
    for key, receipt in receipts.items():
        raw = (FIXTURES / 'regulation_graphics' / (key + '.png')).read_bytes()
        if invalid_asset and key == first: raw = raw[:-1]
        transport[receipt['url']] = (raw, asset_status if key == first else 200)
    calls = []
    def request(provider, method, url, **kwargs):
        assert kwargs['allow_redirects'] is False
        if not url.startswith(source.BASE_URL): assert kwargs['headers'] == {}
        if url == source.BASE_URL + '/documents':
            changed = {**attrs, 'title': 'Conflicting title'} if duplicate and calls else attrs
            raw, status = json.dumps({'data': [{'id': docid, 'attributes': changed}],
                'meta': {'totalPages': 1, 'totalElements': 1}}).encode(), 200
        else: raw, status = transport[url]
        calls.append(url)
        return Response(raw, status=status)
    monkeypatch.setattr(source, 'provider_request', request)
    rules = source.RegulationsSource(api_key='fixture-key').get_recent_proposed_rules(
        agencies=['SEC'] * (2 if duplicate else 1), days_back=14, as_of='2026-10-15',
        evidence_policy='complete_document_text_v1', retrieval_policy=V3)
    return {'proposed_rules': list(rules), 'coverage': rules.coverage,
        'regulation_evidence': rules.regulation_evidence}, calls


@pytest.mark.parametrize('label,count', CASES)
def test_source_adapter_acquires_and_replays_every_current_graphic(monkeypatch, label, count):
    from tradingagents.strategies.data_sources.regulation_evidence import validate_regulation_source
    payload, calls = acquire(monkeypatch, label)
    assert validate_regulation_source(payload)['content_complete'] is True
    unit = next(iter(payload['regulation_evidence']['corpus'].values()))['units'][0]
    assert unit['representation'] == 'verified_government_copy_with_graphics'
    assert len(unit['government_copy']['graphics']) == count
    assert len([url for url in calls if url.startswith('https://img.federalregister.gov/')]) == count
    assert unit['primary_failure']['http_status'] == 403
    assert len(unit['graphics_sha256']) == 64


@pytest.mark.parametrize('status,invalid,duplicate', [(404, False, False), (200, True, False), (200, False, True)])
def test_missing_malformed_asset_or_conflicting_duplicate_preserves_failed_material_replay(monkeypatch, status, invalid, duplicate):
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    from tradingagents.strategies.data_sources.regulation_evidence import validate_regulation_source
    with pytest.raises(SourceFetchError) as error:
        acquire(monkeypatch, 'sec_performance', asset_status=status, invalid_asset=invalid, duplicate=duplicate)
    payload = error.value.partial_data
    scope = validate_regulation_source(payload)
    assert scope['content_complete'] is False and scope['complete_rows'] == 0
    attempts = next(iter(payload['regulation_evidence']['acquisition_failures'].values()))['retrieval_attempts']
    assert any(row['receipt']['url'].startswith('https://img.federalregister.gov/') for row in attempts)
    if duplicate: assert len(attempts) == 17


def test_graphic_source_byte_budget_is_not_recycled_after_failure(monkeypatch):
    from tradingagents.strategies.data_sources import regulations_source as source
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    from tradingagents.strategies.data_sources.regulation_evidence import validate_regulation_source
    monkeypatch.setattr(source, 'MAX_REGULATION_SOURCE_BYTES', 650000)
    with pytest.raises(SourceFetchError) as error:
        acquire(monkeypatch, 'sec_performance')
    scope = validate_regulation_source(error.value.partial_data)
    assert scope['content_complete'] is False and scope['byte_budget_exhausted'] is True
    assert scope['consumed_bytes'] + scope['reserved_bytes'] == scope['byte_limit'] == 650000


@pytest.mark.parametrize('policy,seconds', [('verified_government_copy_v1', 60),
    ('verified_government_copy_v2', 60), (V3, 180)])
def test_prospective_graphics_default_budget_covers_paced_corpus_without_changing_legacy(monkeypatch, policy, seconds):
    from tradingagents.strategies.data_sources import regulations_source as source
    from tradingagents.strategies.data_sources.request_policy import current_provider_deadline
    monkeypatch.setattr(source.time, 'monotonic', lambda: 100)
    adapter = source.RegulationsSource(api_key='fixture-key')
    def complete(*args, **kwargs):
        assert current_provider_deadline('regulations') == 100 + seconds
        return []
    monkeypatch.setattr(adapter, '_complete_recent_rules', complete)
    assert adapter.get_recent_proposed_rules(agencies=['SEC'], as_of='2026-10-15',
        evidence_policy='complete_document_text_v1', retrieval_policy=policy) == []


@pytest.mark.parametrize('bad_pixels', ['excess_decoded_bytes', 'invalid_row_filter'])
def test_png_decoder_refuses_malformed_pixel_stream_even_with_matching_publisher_hash(bad_pixels):
    detail, body, metadata_receipt = originals('sec_performance')
    metadata = json.loads((FIXTURES / 'regulation_fr_crosswalk_sec_performance.json').read_bytes())
    receipts = graphic_receipts('sec_performance')
    key = sorted(receipts)[0]
    original = (FIXTURES / 'regulation_graphics' / (key + '.png')).read_bytes()
    row = metadata['images_metadata'][key]['original_size']
    row_bytes = 1 + (row['width'] + 7) // 8
    decoded = bytearray(row['height'] * row_bytes + (1 if bad_pixels == 'excess_decoded_bytes' else 0))
    if bad_pixels == 'invalid_row_filter': decoded[0] = 5
    def chunk(kind, raw):
        return struct.pack('>I', len(raw)) + kind + raw + struct.pack('>I', zlib.crc32(kind + raw) & 0xffffffff)
    raw = original[:8] + chunk(b'IHDR', original[16:29]) + chunk(b'tRNS', b'\0\1')
    raw += chunk(b'IDAT', zlib.compress(bytes(decoded))) + chunk(b'IEND', b'')
    row.update(size=len(raw), sha=hashlib.md5(raw).hexdigest())
    metadata_receipt = original_receipt(json.dumps(metadata).encode(), observed_at=metadata_receipt['observed_at'], url=metadata_receipt['url'])
    receipts[key] = original_receipt(raw, observed_at=receipts[key]['observed_at'], url=receipts[key]['url'])
    with pytest.raises(ValueError, match='invalid_regulation_government_png'):
        extract_government_copy(detail, body, crosswalk_receipt=metadata_receipt,
            retrieval_policy=V3, graphics_receipts=receipts)


def test_graphic_content_change_invalidates_unit_identity_even_when_body_text_is_identical(monkeypatch):
    from tradingagents.strategies.data_sources.regulation_evidence import build_regulation_evidence, receipt_bytes, MAX_DETAIL_BYTES
    payload, _ = acquire(monkeypatch, 'sec_performance')
    evidence = next(iter(payload['regulation_evidence']['corpus'].values()))
    unit = evidence['units'][0]
    government = unit['government_copy']
    metadata = json.loads(receipt_bytes(government['crosswalk_receipt'], MAX_DETAIL_BYTES))
    receipts = {row['graphic_id']: row['receipt'] for row in government['graphics']}
    key = sorted(receipts)[0]
    row = metadata['images_metadata'][key]['original_size']
    # Construct a different valid raster with the same dimensions, and amend
    # the publisher metadata as an upstream asset revision would. Raw corpus
    # validity must not let yesterday's semantic recognition authorize it.
    def chunk(kind, raw):
        return struct.pack('>I', len(raw)) + kind + raw + struct.pack('>I', zlib.crc32(kind + raw) & 0xffffffff)
    pixels = bytes(row['height'] * (1 + (row['width'] + 7) // 8))
    raw = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', row['width'], row['height'], 1, 0, 0, 0, 0))
    raw += chunk(b'tRNS', b'\0\1') + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')
    row.update(size=len(raw), sha=hashlib.md5(raw).hexdigest())
    receipts[key] = original_receipt(raw, observed_at=receipts[key]['observed_at'], url=receipts[key]['url'])
    old_metadata = government['crosswalk_receipt']
    revised_metadata = original_receipt(json.dumps(metadata).encode(), observed_at=old_metadata['observed_at'], url=old_metadata['url'])
    detail, body, _ = originals('sec_performance')
    revised = build_regulation_evidence(evidence['identity'], detail, {unit['resource_id']: body},
        observed_at=evidence['detail_receipt']['observed_at'],
        body_observed_at={unit['resource_id']: unit['receipt']['observed_at']}, retrieval_policy=V3,
        government_copies={unit['resource_id']: {'primary_failure': unit['primary_failure'],
            'crosswalk_receipt': revised_metadata, 'graphics_receipts': receipts}})
    assert revised['units'][0]['text_sha256'] == unit['text_sha256']
    assert revised['units'][0]['graphics_sha256'] != unit['graphics_sha256']
    assert revised['units'][0]['unit_id'] != unit['unit_id']
