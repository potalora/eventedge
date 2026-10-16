"""SEC Archives uses integer CIK folders; submissions uses padded CIKs.

SEC's uri_path definition documents leading-zero removal:
https://www.sec.gov/files/variables-edgar-log-file-data-sets.pdf
The retained native failure proved HTTP 301, not its unrecorded Location.
All responses here are synthetic and make no SEC request.
"""
import time
from xml.etree import ElementTree as ET

import pytest

from test_form4_complete_window import deny_unmocked_network
from test_form4_xml_identity import ownership
from tradingagents.strategies.data_sources import edgar_source as edgar
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget


FILING = {'accession_number': '0000320193-26-000001', 'form': '4',
          'filing_date': '2026-10-09', 'primary_document': 'xslF345X05/primary_doc.xml'}
ARCHIVE = 'https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/primary_doc.xml'


class Response:
    def __init__(self, status, body=b'', headers=None):
        self.status_code = status
        self.body = body
        self.headers = headers or {}
        self.closed = False
    def iter_content(self, chunk_size): yield self.body
    def close(self): self.closed = True


def document():
    root = ownership()
    root.find('issuer/issuerCik').text = '0000320193'
    return ET.tostring(root)


def window(monkeypatch, cik):
    source = edgar.EDGARSource()
    monkeypatch.setattr(source, 'ticker_to_cik', lambda _: cik)
    monkeypatch.setattr(source, 'get_company_submission_history', lambda _: {
        'filings': [dict(FILING)], 'archives': [], 'response_sha256': 'a'*64,
        'coverage': {'complete': True}})
    return source


@pytest.mark.parametrize('cik', ['320193', '0000320193'])
def test_complete_form4_window_requests_canonical_archive_without_redirect(monkeypatch, cik):
    import requests
    source = window(monkeypatch, cik)
    calls, responses = [], []
    deadline = time.monotonic()+10
    def request(url, **kwargs):
        calls.append((url, kwargs))
        # Explicit synthetic routing: only the documented canonical path has
        # the bound ownership body. The old padded path fails with HTTP 301.
        response = Response(200, document()) if url == ARCHIVE else Response(301)
        responses.append(response)
        return response
    monkeypatch.setattr(requests, 'get', request)
    with provider_budget('edgar', deadline, limits=()):
        rows = source.get_recent_form4('AAPL', 14, as_of='2026-10-09')
    assert rows == [FILING]  # Valid zero-transaction original remains retained.
    assert rows.coverage['complete'] is True and rows.coverage['matching_filings'] == 1
    assert len(calls) == 1 and calls[0][0] == ARCHIVE
    assert calls[0][1]['allow_redirects'] is False and calls[0][1]['stream'] is True
    assert 0 < calls[0][1]['timeout'] <= 10
    assert responses[0].closed


@pytest.mark.parametrize('location', [ARCHIVE, 'https://untrusted.invalid/ownership.xml'])
def test_redirect_even_from_canonical_archive_still_fails_without_following(monkeypatch, location):
    import requests
    source = window(monkeypatch, '0000320193')
    response = Response(301, headers={'Location': location})
    calls = []
    monkeypatch.setattr(requests, 'get', lambda url, **kwargs: calls.append((url, kwargs)) or response)
    with provider_budget('edgar', time.monotonic()+10, limits=()):
        with pytest.raises(SourceFetchError) as caught:
            source.get_recent_form4('AAPL', 14, as_of='2026-10-09')
    assert caught.value.reason_code == 'http_error' and caught.value.http_status == 301
    assert caught.value.partial_data['coverage']['complete'] is False
    assert caught.value.partial_data['coverage']['matching_filings'] == 1
    assert len(calls) == 1 and calls[0][0] == ARCHIVE
    assert calls[0][1]['allow_redirects'] is False and response.closed


@pytest.mark.parametrize('cik', ['0', '0000000000', 'not-a-cik', '１２３', '12345678901', None, True])
def test_invalid_issuer_cik_rejects_before_any_form4_request(monkeypatch, cik):
    reached = []
    monkeypatch.setattr(edgar, 'provider_request', lambda *a, **k: reached.append(True))
    with pytest.raises(SourceFetchError) as caught:
        edgar.EDGARSource()._parse_form4_xml(cik, FILING)
    assert caught.value.reason_code == 'invalid_response' and not reached


@pytest.mark.parametrize('mutation', ['wrong_issuer', 'missing_issuer', 'malformed_xml'])
def test_canonical_archive_success_does_not_weaken_ownership_validation(monkeypatch, mutation):
    import requests
    source = window(monkeypatch, '0000320193')
    root = ET.fromstring(document())
    if mutation == 'wrong_issuer': root.find('issuer/issuerCik').text = '1'
    if mutation == 'missing_issuer': root.remove(root.find('issuer'))
    body = b'<invalid' if mutation == 'malformed_xml' else ET.tostring(root)
    response = Response(200, body)
    calls = []
    monkeypatch.setattr(requests, 'get', lambda url, **kwargs: calls.append(url) or response)
    with provider_budget('edgar', time.monotonic()+10, limits=()):
        with pytest.raises(SourceFetchError) as caught:
            source.get_recent_form4('AAPL', 14, as_of='2026-10-09')
    assert caught.value.reason_code == 'invalid_response'
    assert caught.value.partial_data['coverage']['complete'] is False
    assert calls == [ARCHIVE] and response.closed
