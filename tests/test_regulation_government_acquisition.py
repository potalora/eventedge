"""Government-copy acquisition keeps original failures and bounded byte ownership."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tradingagents.strategies.data_sources import regulations_source as source
from tradingagents.strategies.data_sources.regulation_evidence import digest, validate_regulation_source, receipt_bytes, MAX_BODY_BYTES
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from test_regulation_evidence import DOC
from test_regulation_source_evidence import Response

POLICY = 'verified_government_copy_v1'
GOV = 'https://www.govinfo.gov/content/pkg/FR-2026-10-07/html/2026-20611.htm'
PRIMARY = f'https://downloads.regulations.gov/{DOC}/content.html'


def setup_transport(monkeypatch, *, status=403, failed_body=b'Access denied', government_status=200,
                    government_body=None, attachment=False, chunks=None, original_body=None,
                    mutate_detail=None, attachment_status=403):
    detail_raw = (Path(__file__).parent / 'fixtures/regulation_detail_wv.json').read_bytes()
    detail = json.loads(detail_raw)
    if attachment:
        rid = DOC + '_attachment_1'
        detail['data']['relationships']['attachments']['data'] = [{'id': rid, 'type': 'attachments'}]
        detail['included'] = [{'id': rid, 'type': 'attachments', 'attributes': {'fileFormats': [
            {'fileUrl': f'https://downloads.regulations.gov/{rid}/content.txt', 'format': 'txt', 'size': 8}]}}]
        detail_raw = json.dumps(detail).encode()
    if mutate_detail:
        mutate_detail(detail)
        detail_raw = json.dumps(detail).encode()
    attrs = detail['data']['attributes']
    page = json.dumps({'data': [{'id': DOC, 'attributes': attrs}], 'meta': {'totalPages': 1, 'totalElements': 1}}).encode()
    calls, replies = [], []
    def request(provider, method, url, **kwargs):
        assert provider == 'regulations' and kwargs['allow_redirects'] is False
        calls.append((url, kwargs))
        if url == source.BASE_URL + '/documents': body, code = page, 200
        elif url == source.BASE_URL + '/documents/' + DOC: body, code = detail_raw, 200
        elif url == GOV:
            body = government_body
            if body is None:
                body = (Path(__file__).parent / 'fixtures/regulation_govinfo_wv_native.htm').read_bytes()
            code = government_status
        elif url == PRIMARY: body, code = (failed_body if original_body is None else original_body), status
        else: body, code = b'Appendix', attachment_status
        response = Response(body, status=code, chunks=chunks if url == GOV else None)
        replies.append(response)
        return response
    monkeypatch.setattr(source, 'provider_request', request)
    return calls, replies


def acquire():
    return source.RegulationsSource(api_key='fixture-key').get_recent_proposed_rules(
        agencies=['EPA'], days_back=14, as_of='2026-10-09',
        evidence_policy='complete_document_text_v1', retrieval_policy=POLICY)


def payload(rules):
    return {'proposed_rules': list(rules), 'coverage': rules.coverage,
            'regulation_evidence': rules.regulation_evidence}


def reseal(value):
    value['manifest_sha256'] = digest({key: item for key, item in value.items() if key != 'manifest_sha256'})


@pytest.mark.parametrize('status', [403, 404, 410])
def test_verified_copy_retains_exact_primary_failure_and_full_original_inventory(monkeypatch, status):
    calls, replies = setup_transport(monkeypatch, status=status)
    result = payload(acquire())
    graph = result['regulation_evidence']
    evidence = graph['corpus'][DOC]
    unit = evidence['units'][0]
    assert graph['retrieval_policy'] == evidence['retrieval_policy'] == POLICY
    assert evidence['resources'][0]['selected']['size'] == 11539
    assert len(evidence['resources'][0]['formats']) == 2
    assert unit['representation'] == 'verified_government_copy_text'
    assert unit['receipt']['url'] == GOV
    assert unit['primary_failure']['http_status'] == status
    assert receipt_bytes(unit['primary_failure']['receipt'], MAX_BODY_BYTES) == b'Access denied'
    assert unit['government_copy']['text'] == unit['text']
    assert validate_regulation_source(result)['content_complete'] is True
    assert next(kwargs for url, kwargs in calls if url == GOV)['headers'] == {}
    assert all(reply.closed for reply in replies)


def test_empty_http_error_original_is_valid_evidence(monkeypatch):
    setup_transport(monkeypatch, failed_body=b'')
    result = payload(acquire())
    assert result['regulation_evidence']['corpus'][DOC]['units'][0]['primary_failure']['receipt']['bytes'] == 0
    assert validate_regulation_source(result)['content_complete'] is True


@pytest.mark.parametrize('retrieval,evidence', [(POLICY, None), ('other', 'complete_document_text_v1')])
def test_invalid_or_orphan_retrieval_policy_is_rejected(retrieval, evidence):
    with pytest.raises(ValueError, match='invalid_regulation_retrieval_policy'):
        source.RegulationsSource().get_recent_proposed_rules(retrieval_policy=retrieval, evidence_policy=evidence)


@pytest.mark.parametrize('status', [302, 401, 429, 500])
def test_other_primary_http_status_never_falls_back(monkeypatch, status):
    calls, _ = setup_transport(monkeypatch, status=status)
    with pytest.raises(SourceFetchError) as error: acquire()
    assert not any(url == GOV for url, _ in calls)
    assert validate_regulation_source(error.value.partial_data)['content_complete'] is False


@pytest.mark.parametrize('government_status,government_body', [(404, b'Government missing'), (200, b'Invalid government document')])
def test_failed_fallback_keeps_both_original_attempts(monkeypatch, government_status, government_body):
    setup_transport(monkeypatch, government_status=government_status, government_body=government_body)
    with pytest.raises(SourceFetchError) as error: acquire()
    result = error.value.partial_data
    attempts = result['regulation_evidence']['acquisition_failures'][DOC]['retrieval_attempts']
    assert [attempt['receipt']['url'] for attempt in attempts] == [PRIMARY, GOV]
    assert [attempt['receipt']['bytes'] for attempt in attempts] == [len(b'Access denied'), len(government_body)]
    assert validate_regulation_source(result)['content_complete'] is False


def test_required_attachment_failure_remains_incomplete_after_resolved_primary(monkeypatch):
    setup_transport(monkeypatch, attachment=True)
    with pytest.raises(SourceFetchError) as error: acquire()
    assert validate_regulation_source(error.value.partial_data)['content_complete'] is False
    assert not error.value.partial_data['regulation_evidence']['corpus']


@pytest.mark.parametrize('mutation', ['lost_failure', 'url', 'proof', 'policy', 'consumed'])
def test_resealed_tampering_does_not_authorize_government_copy(monkeypatch, mutation):
    setup_transport(monkeypatch)
    result = payload(acquire())
    graph = result['regulation_evidence']
    evidence = graph['corpus'][DOC]
    unit = evidence['units'][0]
    if mutation == 'lost_failure': unit.pop('primary_failure')
    elif mutation == 'url': unit['receipt']['url'] = GOV.replace('20611', '20612')
    elif mutation == 'proof': unit['government_copy']['identity']['fr_document_number'] = '2026-20612'
    elif mutation == 'policy': graph.pop('retrieval_policy')
    else: graph['coverage']['consumed_bytes'] = evidence['detail_receipt']['bytes'] + unit['receipt']['bytes']
    reseal(evidence)
    reseal(graph)
    with pytest.raises(ValueError): validate_regulation_source(result)


def test_enabled_policy_is_bound_even_without_fallback(monkeypatch):
    body = b'<html><body>' + b'X' * (11539 - len(b'<html><body></body></html>')) + b'</body></html>'
    calls, _ = setup_transport(monkeypatch, status=200, original_body=body)
    result = payload(acquire())
    assert result['regulation_evidence']['retrieval_policy'] == POLICY
    assert result['regulation_evidence']['corpus'][DOC]['retrieval_policy'] == POLICY
    assert 'government_copy' not in result['regulation_evidence']['corpus'][DOC]['units'][0]
    assert not any(url == GOV for url, _ in calls)
    assert validate_regulation_source(result)['retrieval_policy'] == POLICY


def test_enabled_policy_is_carried_by_empty_discovery(monkeypatch):
    def request(*args, **kwargs):
        return Response(json.dumps({'data': [], 'meta': {'totalPages': 0, 'totalElements': 0}}).encode())
    monkeypatch.setattr(source, 'provider_request', request)
    result = payload(acquire())
    assert result['regulation_evidence']['retrieval_policy'] == POLICY
    assert validate_regulation_source(result)['content_complete'] is True


def test_fetch_dispatch_passes_opt_in_policy(monkeypatch):
    body = b'<html><body>' + b'X' * (11539 - len(b'<html><body></body></html>')) + b'</body></html>'
    setup_transport(monkeypatch, status=200, original_body=body)
    result = source.RegulationsSource(api_key='fixture-key').fetch({'method': 'recent_proposed_rules',
        'agencies': ['EPA'], 'as_of': '2026-10-09', 'evidence_policy': 'complete_document_text_v1',
        'retrieval_policy': POLICY})
    assert result['regulation_evidence']['retrieval_policy'] == POLICY
    assert validate_regulation_source({**result, 'proposed_rules': list(result['data'])})['content_complete'] is True


def test_primary_parse_failure_never_falls_back(monkeypatch):
    malformed = b'X' * 11539
    calls, _ = setup_transport(monkeypatch, status=200, original_body=malformed)
    with pytest.raises(SourceFetchError) as error: acquire()
    assert not any(url == GOV for url, _ in calls)
    assert validate_regulation_source(error.value.partial_data)['content_complete'] is False


def test_partial_primary_http_failure_never_falls_back(monkeypatch):
    calls, replies = setup_transport(monkeypatch)
    original = source.provider_request
    def broken(provider, method, url, **kwargs):
        response = original(provider, method, url, **kwargs)
        if url == PRIMARY:
            def chunks():
                yield b'Access'
                raise TimeoutError('body timeout')
            response.chunks = chunks
        return response
    monkeypatch.setattr(source, 'provider_request', broken)
    with pytest.raises(SourceFetchError) as error: acquire()
    result = error.value.partial_data
    assert not any(url == GOV for url, _ in calls)
    attempt = result['regulation_evidence']['acquisition_failures'][DOC]['retrieval_attempts'][0]
    assert attempt['complete'] is False and attempt['receipt']['bytes'] == 6
    assert all(reply.closed for reply in replies)
    assert validate_regulation_source(result)['content_complete'] is False
    coverage = result['regulation_evidence']['coverage']
    assert coverage['consumed_bytes'] + coverage['reserved_bytes'] == coverage['byte_limit']


def test_primary_transport_failure_never_falls_back(monkeypatch):
    calls, _ = setup_transport(monkeypatch)
    original = source.provider_request
    def broken(provider, method, url, **kwargs):
        if url == PRIMARY:
            raise ConnectionError('transport failed')
        return original(provider, method, url, **kwargs)
    monkeypatch.setattr(source, 'provider_request', broken)
    with pytest.raises(SourceFetchError) as error: acquire()
    assert not any(url == GOV for url, _ in calls)
    assert error.value.partial_data['regulation_evidence']['acquisition_failures'][DOC]['retrieval_attempts'] == []
    assert validate_regulation_source(error.value.partial_data)['content_complete'] is False


def test_partial_fallback_stream_is_retained_and_budget_is_not_recycled(monkeypatch):
    def chunks():
        yield b'Partial government copy'
        raise TimeoutError('fallback timeout')
    setup_transport(monkeypatch, government_body=b'X' * 200, chunks=chunks)
    with pytest.raises(SourceFetchError) as error: acquire()
    result = error.value.partial_data
    attempts = result['regulation_evidence']['acquisition_failures'][DOC]['retrieval_attempts']
    assert len(attempts) == 2 and attempts[1]['complete'] is False
    assert receipt_bytes(attempts[1]['receipt'], MAX_BODY_BYTES) == b'Partial government copy'
    coverage = result['regulation_evidence']['coverage']
    assert coverage['consumed_bytes'] + coverage['reserved_bytes'] == coverage['byte_limit']
    assert validate_regulation_source(result)['content_complete'] is False


def test_error_body_cap_failure_prevents_fallback_and_preserves_consumption(monkeypatch):
    calls, _ = setup_transport(monkeypatch, failed_body=b'Access denied')
    detail = (Path(__file__).parent / 'fixtures/regulation_detail_wv.json').read_bytes()
    page = json.dumps({'data': [{'id': DOC, 'attributes': json.loads(detail)['data']['attributes']}],
                       'meta': {'totalPages': 1, 'totalElements': 1}}).encode()
    limit = len(detail) + len(page) + len(b'Access denied') - 1
    monkeypatch.setattr(source, 'MAX_REGULATION_SOURCE_BYTES', limit)
    with pytest.raises(SourceFetchError) as error: acquire()
    result = error.value.partial_data
    assert not any(url == GOV for url, _ in calls)
    assert result['regulation_evidence']['coverage']['consumed_bytes'] <= limit
    assert validate_regulation_source(result)['content_complete'] is False


def test_fallback_does_not_restart_inherited_deadline(monkeypatch):
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    clock = [0]
    calls, _ = setup_transport(monkeypatch)
    original = source.provider_request
    def expires(provider, method, url, **kwargs):
        response = original(provider, method, url, **kwargs)
        if url == PRIMARY:
            def chunks():
                yield b'Access denied'
                clock[0] = 11
            response.chunks = chunks
        return response
    monkeypatch.setattr(source, 'provider_request', expires)
    with provider_budget('regulations', 10, clock=lambda: clock[0], limits=()):
        with pytest.raises(SourceFetchError) as error: acquire()
    assert not any(url == GOV for url, _ in calls)
    assert validate_regulation_source(error.value.partial_data)['content_complete'] is False


def test_conflicting_duplicate_identity_preserves_resolved_original_and_failure(monkeypatch):
    setup_transport(monkeypatch)
    original = source.provider_request
    pages = [0]
    def duplicate(provider, method, url, **kwargs):
        response = original(provider, method, url, **kwargs)
        if url == source.BASE_URL + '/documents':
            pages[0] += 1
            if pages[0] == 2:
                data = json.loads(response.body)
                data['data'][0]['attributes']['title'] = 'Conflicting discovered title'
                response = Response(json.dumps(data).encode())
        return response
    monkeypatch.setattr(source, 'provider_request', duplicate)
    with pytest.raises(SourceFetchError) as error:
        source.RegulationsSource(api_key='fixture-key').get_recent_proposed_rules(
            agencies=['EPA', 'EPA'], days_back=14, as_of='2026-10-09',
            evidence_policy='complete_document_text_v1', retrieval_policy=POLICY)
    result = error.value.partial_data
    failed = result['regulation_evidence']['acquisition_failures'][DOC]
    assert failed['failure']['code'] == 'invalid_regulation_identity'
    assert [attempt['receipt']['url'] for attempt in failed['retrieval_attempts']] == [PRIMARY, GOV]
    assert validate_regulation_source(result)['content_complete'] is False


def test_selected_pdf_failure_never_bypasses_unavailable_pdf_support(monkeypatch):
    def pdf_only(detail):
        detail['data']['attributes']['fileFormats'] = [item for item in
            detail['data']['attributes']['fileFormats'] if item['format'] == 'pdf']
    calls, _ = setup_transport(monkeypatch, mutate_detail=pdf_only)
    with pytest.raises(SourceFetchError) as error: acquire()
    assert not any(url == GOV for url, _ in calls)
    assert validate_regulation_source(error.value.partial_data)['content_complete'] is False


def test_resolved_primary_still_requires_exact_attachment_inventory(monkeypatch):
    setup_transport(monkeypatch, attachment=True, attachment_status=200)
    result = payload(acquire())
    assert len(result['regulation_evidence']['corpus'][DOC]['units']) == 2
    assert validate_regulation_source(result)['content_complete'] is True
    evidence = result['regulation_evidence']['corpus'][DOC]
    evidence['units'].pop()
    reseal(evidence)
    reseal(result['regulation_evidence'])
    with pytest.raises(ValueError): validate_regulation_source(result)


@pytest.mark.parametrize('mutation', ['url', 'primary_missing', 'duplicate_read', 'wrong_resource'])
def test_failed_fallback_receipts_cannot_be_resealed_to_hide_or_redirect_originals(monkeypatch, mutation):
    setup_transport(monkeypatch, government_status=404, government_body=b'Absent')
    with pytest.raises(SourceFetchError) as error: acquire()
    result = error.value.partial_data
    graph = result['regulation_evidence']
    failed = graph['acquisition_failures'][DOC]
    attempts = failed['retrieval_attempts']
    if mutation == 'url': attempts[1]['receipt']['url'] = GOV.replace('20611', '20612')
    elif mutation == 'primary_missing': attempts.pop(0)
    elif mutation == 'duplicate_read': attempts.append(deepcopy(attempts[1]))
    else: attempts[1]['resource_id'] = 'OTHER-ID'
    reseal(graph)
    with pytest.raises(ValueError): validate_regulation_source(result)


def test_failed_parser_original_bytes_cannot_be_refunded(monkeypatch):
    setup_transport(monkeypatch, government_body=b'Invalid government document')
    with pytest.raises(SourceFetchError) as error: acquire()
    result = error.value.partial_data
    graph = result['regulation_evidence']
    failed = graph['acquisition_failures'][DOC]
    graph['coverage']['consumed_bytes'] = failed['detail_receipt']['bytes']
    reseal(graph)
    with pytest.raises(ValueError): validate_regulation_source(result)
