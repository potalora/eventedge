"""Official PDF retention is separate from publisher rows and trading authority."""
from copy import deepcopy
from datetime import timedelta
import importlib
import io
import zipfile

import pytest

from test_congress_disclosure_audit import NOW, REVISION, synthetic_raw
from tradingagents.strategies.data_sources import congress_disclosure_audit as audit
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError

PDF = b'%PDF-1.4\nexplicit synthetic original fixture\n%%EOF\n'


def module():
    return importlib.import_module('tradingagents.strategies.data_sources.congress_house_originals')


def snapshot(ids=('20035687', '20035691')):
    raw = synthetic_raw()
    index = io.BytesIO()
    with zipfile.ZipFile(index, 'w') as archive:
        archive.writestr(zipfile.ZipInfo('2026FD.xml'), '<FinancialDisclosure>' + ''.join(
            f'<Member><DocID>{ident}</DocID><FilingType>P</FilingType>'
            '<FilingDate>10/1/2026</FilingDate></Member>' for ident in ('12345', *ids))
            + '</FinancialDisclosure>')
        archive.writestr(zipfile.ZipInfo('2026FD.txt'), 'synthetic')
    base = f'{audit.HF}/datasets/{audit.DATASET}/resolve/{REVISION}'
    import json
    artifacts = [audit._record('revision_metadata', audit.REVISION_URL,
        json.dumps({'sha': REVISION, 'id': audit.DATASET}).encode()),
        audit._record('manifest', base + '/snapshot.json', raw['snapshot.json'])]
    for table, name in [('political_filings', 'filings-2026.parquet'), ('political_trades', 'trades-2026.parquet')]:
        artifacts.append(audit._record('parquet', base + f'/data/{table}/2026-00000-of-00001.parquet', raw[name]))
    artifacts.append(audit._record('house_index', 'https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2026FD.ZIP', index.getvalue()))
    return audit._assemble(artifacts, revision=REVISION, start='2026-09-09', end='2026-10-09',
        acquired_at=NOW.isoformat())['audit_snapshot']


def capture(value, download=lambda url, limit: PDF, now=lambda: NOW):
    return module().capture_house_originals(value, download=download, now=now)


def test_only_verified_missing_ids_get_fixed_house_originals_and_forward_clock():
    source = snapshot(); original = deepcopy(source); calls = []
    result = capture(source, lambda url, limit: calls.append((url, limit)) or PDF,
                     now=lambda: NOW + timedelta(seconds=1))
    assert calls == [(f'https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/{ident}.pdf',
                      module().MAX_DOCUMENT_BYTES) for ident in ('20035687', '20035691')]
    assert result['status'] == 'originals_captured'
    assert result['publisher_snapshot_sha256'] == source['content_sha256']
    assert result['signals_enabled'] is result['model_context_enabled'] is result['order_authority'] is False
    assert all(row['status'] == 'original_only' and row['acquired_at'] == (NOW + timedelta(seconds=1)).isoformat()
               for row in result['documents'])
    assert result['publisher_diagnostics']['missing_house_filings'] == 2
    module().validate_house_originals(result, source, now=NOW + timedelta(seconds=1))
    assert source == original and source['scope_evidence']['house_reconciliation']['missing_ids'] == ['20035687', '20035691']
    assert len(source['tables']['political_trades']) == 1


def test_publisher_refresh_target_is_observational_not_fake_recovery():
    result = capture(snapshot(), now=lambda: NOW + timedelta(hours=21))
    diagnostics = result['publisher_diagnostics']
    assert diagnostics['refresh_target_seconds'] == 20 * 3600
    assert diagnostics['refresh_overdue'] is True
    assert diagnostics['last_source_check_age_seconds'] == 27 * 3600
    assert diagnostics['publisher_coverage_complete'] is False


@pytest.mark.parametrize('body', [b'<html>not a pdf</html>', b'%PDF-1.4 truncated', PDF + b'x' * (1024 * 1024)],
                         ids=['html', 'truncated', 'oversized'])
def test_invalid_or_overlimit_original_never_becomes_retained_success(body):
    result = capture(snapshot(('20035687',)), lambda url, limit: body)
    row = result['documents'][0]
    assert result['status'] == 'partial_originals'
    assert row['status'] == 'failed' and row['reason_code'] == 'invalid_response'
    assert row['body_base64'] is row['sha256'] is row['acquired_at'] is None
    module().validate_house_originals(result, snapshot(('20035687',)), now=NOW)


def test_document_count_and_aggregate_byte_caps_leave_explicit_omissions(monkeypatch):
    m = module(); monkeypatch.setattr(m, 'MAX_DOCUMENTS', 2)
    monkeypatch.setattr(m, 'MAX_TOTAL_BYTES', len(PDF))
    calls = []
    result = capture(snapshot(('20035687', '20035691', '20035692')),
                     lambda url, limit: calls.append(url) or PDF)
    assert len(calls) == 1 and result['omitted_documents'] == 1
    assert [r['status'] for r in result['documents']] == ['original_only', 'not_attempted']
    assert result['documents'][1]['reason_code'] == 'capture_limit'
    assert result['status'] == 'partial_originals'


@pytest.mark.parametrize('fault', ['unknown_id', 'body', 'timestamp', 'authority', 'snapshot_digest'])
def test_rehashing_cannot_admit_unbound_or_altered_evidence(fault):
    m = module(); source = snapshot(); result = capture(source)
    if fault == 'unknown_id': result['documents'][0]['doc_id'] = '999999'
    elif fault == 'body': result['documents'][0]['body_base64'] = 'YWx0ZXJlZA=='
    elif fault == 'timestamp': result['documents'][0]['acquired_at'] = (NOW - timedelta(days=1)).isoformat()
    elif fault == 'authority': result['order_authority'] = True
    else: result['publisher_snapshot_sha256'] = '0' * 64
    result['content_sha256'] = m._digest(result)
    with pytest.raises(ValueError): m.validate_house_originals(result, source, now=NOW)


def test_download_failure_is_sanitized_and_cannot_replace_original_gap():
    def fail(url, limit):
        raise SourceFetchError('fixed failure', reason_code='http_error', http_status=404)
    source = snapshot(); result = capture(source, fail)
    assert all(r['reason_code'] == 'http_error' and r['http_status'] == 404 for r in result['documents'])
    assert result['publisher_diagnostics']['missing_house_filings'] == 2
    assert result['publisher_diagnostics']['publisher_coverage_complete'] is False
    module().validate_house_originals(result, source, now=NOW)


def test_tampered_publisher_snapshot_cannot_select_outbound_documents():
    source = snapshot(); source['scope_evidence']['house_reconciliation']['missing_ids'].append('999999')
    calls = []
    with pytest.raises(ValueError): capture(source, lambda url, limit: calls.append(url) or PDF)
    assert calls == []


def install_snapshot_transport(monkeypatch, source, *, fault=None):
    import requests
    from test_congress_disclosure_audit import Response
    import base64
    bodies = {a['source_url']: base64.b64decode(a['body_base64']) for a in source['raw_artifacts']}
    calls, replies = [], []
    def get(url, **options):
        calls.append(url)
        assert options['stream'] is True and options['allow_redirects'] is False
        if '/ptr-pdfs/' in url:
            reply = fault(url) if fault else Response(PDF)
        else:
            reply = Response(bodies[url])
        replies.append(reply)
        return reply
    monkeypatch.setattr(requests, 'get', get)
    monkeypatch.setattr(audit, '_utcnow', lambda: NOW)
    return calls, replies


def native_fetch(clock=lambda: 1):
    from tradingagents.strategies.data_sources import request_policy
    diagnostics = []
    with request_policy.provider_budget('congress', 100, clock=clock, limits=(), diagnostics=diagnostics):
        with pytest.raises(SourceFetchError) as caught:
            audit.fetch_audit_snapshot(date_filed_after='2026-09-09', date_filed_before='2026-10-09',
                                      absolute_deadline=100)
    return caught.value, diagnostics


def test_native_gap_backfill_preserves_exact_publisher_snapshot_and_incomplete_coverage(monkeypatch):
    original = snapshot(); calls, replies = install_snapshot_transport(monkeypatch, original)
    failure, diagnostics = native_fetch()
    assert failure.reason_code == 'invalid_response'
    assert failure.partial_data['audit_snapshot'] == original
    assert failure.partial_data['coverage']['complete'] is False
    evidence = failure.partial_data['house_originals']
    assert evidence['status'] == 'originals_captured'
    assert len(calls) == 7 and len(diagnostics) == 7 and all(r.closed for r in replies)
    assert [d['operation'] for d in diagnostics[-2:]] == ['house_original', 'house_original']
    audit.validate_audit_snapshot(failure.partial_data, date_filed_after='2026-09-09',
                                 date_filed_before='2026-10-09', now=NOW)


@pytest.mark.parametrize('fault', ['redirect', 'timeout', 'html'])
def test_native_optional_original_failure_preserves_verified_audit_gap(monkeypatch, fault):
    import requests
    from test_congress_disclosure_audit import Response
    def failure(url):
        if fault == 'redirect': return Response(b'', status=302, headers={'Location': 'https://evil.invalid/file.pdf'})
        if fault == 'timeout': raise requests.Timeout('SECRET')
        return Response(b'<html>no original</html>')
    calls, replies = install_snapshot_transport(monkeypatch, snapshot(), fault=failure)
    error, diagnostics = native_fetch()
    assert error.reason_code == 'invalid_response'
    assert error.partial_data['coverage']['integrity_valid'] is True
    evidence = error.partial_data['house_originals']
    assert evidence['status'] == 'partial_originals'
    assert len(calls) == 7 and all(r.closed for r in replies)
    assert all(row['status'] == 'failed' for row in evidence['documents'])
    assert 'SECRET' not in str(evidence) and not any('evil.invalid' in url for url in calls)


def test_optional_originals_cannot_exceed_shared_request_cap(monkeypatch):
    calls, _ = install_snapshot_transport(monkeypatch, snapshot())
    monkeypatch.setattr(audit, 'MAX_REQUESTS', 5)
    error, diagnostics = native_fetch()
    assert len(calls) == 5
    assert error.partial_data['house_originals']['status'] == 'partial_originals'
    assert error.partial_data['coverage']['complete'] is False


def test_optional_originals_never_start_late_requests_or_accept_late_bodies(monkeypatch):
    import requests
    calls, replies = install_snapshot_transport(monkeypatch, snapshot())
    original_get = requests.get; clock = [1]
    def late_original(url, **options):
        result = original_get(url, **options)
        if '/ptr-pdfs/' in url: clock[0] = 100
        return result
    monkeypatch.setattr(requests, 'get', late_original)
    error, diagnostics = native_fetch(clock=lambda: clock[0])
    assert len(calls) == 6 and all(r.closed for r in replies)
    assert error.reason_code == 'invalid_response' and error.partial_data['coverage']['integrity_valid'] is True
    assert all(row['status'] == 'failed' and row['reason_code'] == 'timeout'
               for row in error.partial_data['house_originals']['documents'])
    assert [d['attempts'] for d in diagnostics[-2:]] == [1, 0]


@pytest.mark.parametrize('failed_original', [False, True])
def test_separately_validated_originals_preserve_visible_continuity_warning(monkeypatch, failed_original):
    from test_congress_audit_continuity import CONFIG
    from test_congress_disclosure_audit import Response
    from tradingagents.strategies.orchestration.congress_audit_continuity import warning_for_source
    install_snapshot_transport(monkeypatch, snapshot(), fault=(lambda url: Response(b'bad')) if failed_original else None)
    error, diagnostics = native_fetch()
    payload = {**error.partial_data, 'error': str(error), '_coverage': {'status': 'partial',
        'reason_code': error.reason_code, 'http_status': None, 'attempts': 0}, '_request_diagnostics': diagnostics}
    warning = warning_for_source(payload, CONFIG, '2026-10-09', NOW)
    assert warning['house_missing_filings'] == 2
    assert warning['research_coverage_complete'] is warning['order_authority'] is False
    assert payload['coverage']['complete'] is False
    bad = deepcopy(payload); bad['house_originals']['documents'][0]['doc_id'] = '999999'
    bad['house_originals']['content_sha256'] = module()._digest(bad['house_originals'])
    with pytest.raises(ValueError): warning_for_source(bad, CONFIG, '2026-10-09', NOW)


def test_maximum_pdf_total_survives_native_codec_warning_and_source_report(monkeypatch, tmp_path):
    from test_congress_audit_continuity import CONFIG
    from test_congress_disclosure_audit import Response
    from tradingagents.strategies.orchestration.congress_audit_continuity import warning_for_source
    from tradingagents.strategies.orchestration.congress_policy import declaration
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore, source_codec_limits
    from tradingagents.strategies.orchestration.operational_report import _sources
    m = module(); prefix, suffix = b'%PDF-1.4\n', b'\n%%EOF\n'
    maximum_pdf = prefix + b'x' * (m.MAX_DOCUMENT_BYTES - len(prefix) - len(suffix)) + suffix
    original = snapshot(tuple(str(20035680 + i) for i in range(m.MAX_DOCUMENTS)))
    install_snapshot_transport(monkeypatch, original, fault=lambda url: Response(maximum_pdf))
    error, diagnostics = native_fetch()
    payload = {**error.partial_data, 'error': str(error), '_coverage': {'status': 'partial',
        'reason_code': error.reason_code, 'http_status': None, 'attempts': 0}, '_request_diagnostics': diagnostics}
    assert sum(r['bytes'] for r in payload['house_originals']['documents']) == m.MAX_TOTAL_BYTES
    limits = source_codec_limits({'autoresearch': CONFIG}, source='congress')
    encoded = SourceInputStore.encode(payload, **limits)
    decoded = SourceInputStore.decode(encoded, **limits)
    assert decoded == payload and decoded['audit_snapshot'] == original
    assert warning_for_source(decoded, CONFIG, '2026-10-09', NOW)['house_missing_filings'] == m.MAX_DOCUMENTS
    store = SourceInputStore(tmp_path/'cache', accepted_dir=tmp_path/'source_inputs', **limits)
    identity = {'generation': 'gen_999', 'commit': 'a'*40, 'session': '2026-10-09', 'configuration': 'cfg'}
    store.freeze(identity, {**declaration(CONFIG), 'congress': decoded}, acquired_at=NOW)
    diagnostics = []
    projected = _sources(tmp_path, 'gen_999', '2026-10-09', 'a'*40, lambda *row: diagnostics.append(row))
    assert ('accepted_sources_invalid',) not in diagnostics
    assert projected['observations']['congress'] == 'partial'
    assert any(row['provider'] == 'congress' and row['reason_code'] == 'invalid_response'
               for row in projected['unresolved'])
    summary = projected['congress_audit_scope']['house_originals']
    assert summary['captured_bytes'] == m.MAX_TOTAL_BYTES
    assert summary['counts'] == {'original_only': 4, 'failed': 0, 'not_attempted': 4, 'omitted': 0}
    assert 'body_base64' not in str(summary) and summary['order_authority'] is False
