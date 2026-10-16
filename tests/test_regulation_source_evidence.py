"""Provider-shaped source tests: complete discovery is not complete content."""
from copy import deepcopy
import json
import time

import pytest

from tradingagents.strategies.data_sources import regulations_source as source
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget
from test_regulation_evidence import DOC, HTML, fixture


class Response:
    def __init__(self, body, status=200, chunks=None):
        self.body, self.status_code, self.closed = body, status, False
        self.headers = {'Content-Length': str(len(body))}
        self.chunks = chunks

    def iter_content(self, chunk_size):
        yield from self.chunks() if self.chunks else [self.body]

    def close(self):
        self.closed = True


def transport(monkeypatch, *, attachment=False, status=200, mutate_detail=None, late=False):
    row, detail, bodies = fixture(attachment=attachment)
    if mutate_detail: mutate_detail(detail)
    page = {'data': [{'id': DOC, 'attributes': detail['data']['attributes']}],
            'meta': {'totalPages': 1, 'totalElements': 1}}
    calls, replies = [], []
    def request(provider, method, url, **kwargs):
        assert provider == 'regulations' and method == 'GET'
        assert kwargs['stream'] is True and kwargs['allow_redirects'] is False
        calls.append((url, kwargs))
        if url == source.BASE_URL + '/documents':
            response = Response(json.dumps(page).encode())
        elif url == source.BASE_URL + '/documents/' + DOC:
            assert kwargs['params'] == {'include': 'attachments'}
            response = Response(json.dumps(detail).encode())
        else:
            assert kwargs.get('headers', {}) == {}
            rid = url.split('/')[-2]
            response = Response(bodies[rid], status=status)
        replies.append(response)
        return response
    monkeypatch.setattr(source, 'provider_request', request)
    return calls, replies


def run(src):
    return src.get_recent_proposed_rules(agencies=['EPA'], days_back=14,
                                         as_of='2026-10-09', evidence_policy='complete_document_text_v1')


def test_source_acquires_advertised_body_and_retains_exact_evidence(monkeypatch):
    calls, replies = transport(monkeypatch, attachment=True)
    rules = run(source.RegulationsSource(api_key='fixture-key'))
    assert len(rules) == 1 and rules[0]['summary'] == ''
    assert rules[0]['regulation_evidence_status'] == 'complete'
    graph = rules.regulation_evidence
    assert graph['coverage']['discovery_complete'] is True
    assert graph['coverage']['content_complete'] is True
    assert graph['coverage']['required_rows'] == 1
    assert len(graph['corpus'][DOC]['units']) == 2
    assert all(response.closed for response in replies)
    assert all(call[1]['headers'] == {'X-Api-Key': 'fixture-key'} for call in calls[:2])


def test_body_http403_retains_rule_and_failure_without_title_fallback(monkeypatch):
    calls, replies = transport(monkeypatch, status=403)
    with pytest.raises(SourceFetchError) as error:
        run(source.RegulationsSource(api_key='fixture-key'))
    payload = error.value.partial_data
    assert payload['coverage']['complete'] is True
    assert payload['proposed_rules'][0]['document_id'] == DOC
    assert payload['proposed_rules'][0]['regulation_evidence_status'] == 'failed'
    graph = payload['regulation_evidence']
    assert graph['coverage']['content_complete'] is False
    assert graph['coverage']['failed_rows'] == 1
    assert graph['acquisition_failures'][DOC]['failure']['http_status'] == 403
    assert graph['acquisition_failures'][DOC]['detail_receipt']['bytes'] > 0
    assert not graph['corpus']
    assert all(response.closed for response in replies)


def test_inherited_deadline_is_not_restarted(monkeypatch):
    calls, _ = transport(monkeypatch)
    with provider_budget('regulations', 1, clock=lambda: 2, limits=()):
        with pytest.raises(SourceFetchError) as error:
            run(source.RegulationsSource(api_key='fixture-key'))
    assert error.value.partial_data['regulation_evidence']['coverage']['content_complete'] is False
    assert calls == []


def test_all_agencies_and_body_reads_share_one_aggregate_cap(monkeypatch):
    calls, _ = transport(monkeypatch)
    # Two successful reads consume the shared budget; the selected body must
    # not receive a fresh 32 MiB allowance after discovery/detail acquisition.
    row, detail, _ = fixture()
    page = {'data': [{'id': DOC, 'attributes': detail['data']['attributes']}],
            'meta': {'totalPages': 1, 'totalElements': 1}}
    limit = len(json.dumps(page).encode()) + len(json.dumps(detail).encode()) + len(HTML) - 1
    monkeypatch.setattr(source, 'MAX_REGULATION_SOURCE_BYTES', limit, raising=False)
    with pytest.raises(SourceFetchError) as error:
        run(source.RegulationsSource(api_key='fixture-key'))
    graph = error.value.partial_data['regulation_evidence']
    assert graph['coverage']['content_complete'] is False
    assert graph['coverage']['consumed_bytes'] <= limit
    assert graph['coverage']['byte_limit'] == limit


def test_strategy_carries_compact_refs_without_reslicing_full_text():
    from tradingagents.strategies.modules.regulatory_pipeline import RegulatoryPipelineStrategy
    row, _, _ = fixture()
    row.update(regulation_evidence_ref=DOC, regulation_evidence_status='complete')
    candidates = RegulatoryPipelineStrategy().screen({'regulations': {
        'proposed_rules': [row], 'regulation_evidence': {'policy': 'complete_document_text_v1'}}},
        '2026-10-09', {'analysis_budget': 5})
    assert candidates[0].metadata['regulation_evidence_ref'] == DOC
    assert candidates[0].metadata['regulation_evidence_policy'] == 'complete_document_text_v1'
    assert candidates[0].metadata['analysis_type'] == 'regulation'


def test_frozen_source_validator_rejects_removed_row_and_changed_manifest(monkeypatch):
    from tradingagents.strategies.data_sources.regulation_evidence import validate_regulation_source
    transport(monkeypatch)
    rules = run(source.RegulationsSource(api_key='fixture-key'))
    payload = {'proposed_rules': list(rules), 'coverage': rules.coverage,
               'regulation_evidence': rules.regulation_evidence}
    assert validate_regulation_source(payload)['content_complete'] is True
    stripped = deepcopy(payload)
    stripped['proposed_rules'].clear()
    with pytest.raises(ValueError): validate_regulation_source(stripped)
    payload['regulation_evidence']['corpus'][DOC]['units'][0]['text'] = 'No impact'
    with pytest.raises(ValueError): validate_regulation_source(payload)


def test_failed_body_cannot_be_relabelled_as_complete_source(monkeypatch):
    from tradingagents.strategies.data_sources.regulation_evidence import validate_regulation_source, digest
    transport(monkeypatch, status=403)
    with pytest.raises(SourceFetchError) as error:
        run(source.RegulationsSource(api_key='fixture-key'))
    payload = error.value.partial_data
    assert validate_regulation_source(payload)['content_complete'] is False
    graph = payload['regulation_evidence']
    graph['coverage']['content_complete'] = True
    graph['manifest_sha256'] = digest({key: value for key, value in graph.items() if key != 'manifest_sha256'})
    with pytest.raises(ValueError): validate_regulation_source(payload)


def test_partial_failed_stream_reserves_capacity_without_claiming_bytes_received(monkeypatch):
    clock = [0]
    transport(monkeypatch)
    original = source.provider_request
    def dispatch(provider, method, url, **kwargs):
        response = original(provider, method, url, **kwargs)
        if url.startswith('https://downloads.'):
            def delayed():
                yield HTML[:10]
                clock[0] = 11
                yield HTML[10:]
            response.chunks = delayed
        return response
    monkeypatch.setattr(source, 'provider_request', dispatch)
    with provider_budget('regulations', 10, clock=lambda: clock[0], limits=()):
        with pytest.raises(SourceFetchError) as error:
            run(source.RegulationsSource(api_key='fixture-key'))
    coverage = error.value.partial_data['regulation_evidence']['coverage']
    assert coverage['content_complete'] is False
    assert coverage['reserved_bytes'] > 0
    assert coverage['consumed_bytes'] < coverage['byte_limit']
    assert coverage['consumed_bytes'] + coverage['reserved_bytes'] == coverage['byte_limit']
