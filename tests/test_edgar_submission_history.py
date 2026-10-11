"""Full recent/archived metadata preserves exact prior-selection evidence."""
import copy
import hashlib
import json

import pytest

from tradingagents.strategies.data_sources.edgar_source import EDGARSource
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget


def arrays():
    return {'accessionNumber': ['0000000001-26-000002', '0000000001-25-000001'],
            'form': ['10-Q', '10-K'], 'filingDate': ['2026-09-30', '2025-09-30'],
            'primaryDocument': ['quarter.htm', 'annual.htm'],
            'isXBRL': [1, 1]}


def descriptor():
    return {'name': 'CIK0000000001-submissions-001.json', 'filingCount': 2,
            'filingFrom': '2025-09-30', 'filingTo': '2026-09-30'}


class Response:
    status_code = 200
    headers = {}

    def __init__(self, data, url='https://data.sec.gov/submissions/CIK0000000001.json'):
        self.body = json.dumps(data).encode()
        self.url, self.closed = url, False

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        self.closed = True


def transport(monkeypatch, response):
    calls = []
    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return response
    monkeypatch.setattr('tradingagents.strategies.data_sources.edgar_source.provider_request', request)
    return calls


def test_recent_history_keeps_all_rows_archives_and_response_provenance(monkeypatch):
    response = Response({'cik': '1', 'filings': {'recent': arrays(), 'files': [descriptor()]}})
    calls = transport(monkeypatch, response)
    with provider_budget('edgar', 100, clock=lambda: 1, limits=()):
        result = EDGARSource().get_company_submission_history('1')
    assert [r['form'] for r in result['filings']] == ['10-Q', '10-K']
    assert result['archives'] == [descriptor()]
    assert result['cik'] == '0000000001'
    assert result['response_sha256'] == hashlib.sha256(response.body).hexdigest()
    assert result['coverage']['complete'] is True
    assert calls[0][1]['stream'] is True and calls[0][1]['allow_redirects'] is False
    assert response.closed


@pytest.mark.parametrize('change', [
    lambda x: x.update(cik='2'),
    lambda x: x['filings']['recent']['isXBRL'].pop(),
    lambda x: x['filings']['recent']['filingDate'].__setitem__(0, '2026-02-30'),
    lambda x: x['filings']['recent']['filingDate'].__setitem__(0, '2026-09-30T12:00:00'),
    lambda x: x['filings']['recent']['accessionNumber'].__setitem__(0, '../secret'),
    lambda x: x['filings'].pop('files'),
    lambda x: x['filings']['files'][0].update(name='../secret'),
    lambda x: x['filings']['files'][0].update(name='CIK0000000002-submissions-001.json'),
    lambda x: x['filings']['files'][0].update(filingFrom='2026-10-01'),
    lambda x: x['filings']['files'][0].update(filingFrom='2025-09-30T12:00:00'),
    lambda x: x['filings']['files'][0].update(filingCount=True),
])
def test_invalid_recent_or_archive_contract_is_closed(monkeypatch, change):
    data = {'cik': '1', 'filings': {'recent': arrays(), 'files': [descriptor()]}}
    change(data)
    response = Response(data)
    transport(monkeypatch, response)
    with pytest.raises(SourceFetchError) as caught:
        EDGARSource().get_company_submission_history('1')
    assert caught.value.reason_code == 'invalid_response'
    assert 'secret' not in str(caught.value)
    assert response.closed


def test_empty_recent_and_no_archives_is_explicit_complete_metadata(monkeypatch):
    empty = {key: [] for key in arrays()}
    response = Response({'cik': 1, 'filings': {'recent': empty, 'files': []}})
    transport(monkeypatch, response)
    result = EDGARSource().get_company_submission_history('0000000001')
    assert result['filings'] == [] and result['archives'] == []
    assert result['coverage']['complete'] is True


@pytest.mark.parametrize('kind', ['success', 'count_mismatch', 'outside_dates', 'redirect', 'late'])
def test_archived_arrays_are_bound_to_cik_descriptor_range_and_deadline(monkeypatch, kind):
    data = copy.deepcopy(arrays())
    url = 'https://data.sec.gov/submissions/' + descriptor()['name']
    response = Response(data, url)
    desc = descriptor()
    if kind == 'count_mismatch':
        desc['filingCount'] = 3
    if kind == 'outside_dates':
        desc['filingFrom'] = '2026-01-01'
    if kind == 'redirect':
        response.url += '?secret'
    clock = [1]
    def request(*args, **kwargs):
        if kind == 'late':
            clock[0] = 101
        return response
    monkeypatch.setattr('tradingagents.strategies.data_sources.edgar_source.provider_request', request)
    with provider_budget('edgar', 100, clock=lambda: clock[0], limits=()):
        if kind == 'success':
            result = EDGARSource().get_company_submission_archive('1', desc)
            assert len(result['filings']) == 2
            assert result['descriptor'] == desc and result['source_url'] == url
        else:
            with pytest.raises(SourceFetchError):
                EDGARSource().get_company_submission_archive('1', desc)
    assert response.closed


def test_invalid_cik_or_archive_never_reaches_transport(monkeypatch):
    calls = transport(monkeypatch, Response({}))
    for cik in ['0', '12345678901', '1/secret']:
        with pytest.raises(SourceFetchError):
            EDGARSource().get_company_submission_history(cik)
    with pytest.raises(SourceFetchError):
        EDGARSource().get_company_submission_archive('1', {**descriptor(), 'name': '../secret'})
    assert calls == []


def test_duplicate_json_fields_are_not_silently_overwritten(monkeypatch):
    response = Response({})
    response.body = b'{"cik":1,"cik":2,"filings":{"recent":{},"files":[]}}'
    transport(monkeypatch, response)
    with pytest.raises(SourceFetchError):
        EDGARSource().get_company_submission_history('1')
    assert response.closed


def test_safe_native_stylesheet_primary_document_path_is_preserved(monkeypatch):
    data = {'cik': 1, 'filings': {'recent': arrays(), 'files': []}}
    data['filings']['recent']['primaryDocument'][0] = 'xslF345X05/doc4.xml'
    response = Response(data)
    transport(monkeypatch, response)
    result = EDGARSource().get_company_submission_history('1')
    assert result['filings'][0]['primary_document'] == 'xslF345X05/doc4.xml'


@pytest.mark.parametrize('path', ['../secret', '/secret', 'xsl/../secret', 'xsl/%2e%2e/secret', 'xsl\\secret'])
def test_history_primary_document_cannot_smuggle_unsafe_path(monkeypatch, path):
    data = {'cik': 1, 'filings': {'recent': arrays(), 'files': []}}
    data['filings']['recent']['primaryDocument'][0] = path
    response = Response(data)
    transport(monkeypatch, response)
    with pytest.raises(SourceFetchError):
        EDGARSource().get_company_submission_history('1')
    assert response.closed
