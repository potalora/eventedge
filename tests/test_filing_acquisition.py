"""One exact, bounded SEC submission request with full evidence and original clock."""
from datetime import datetime

import pytest

from test_filing_evidence import ACCESSION, submission
from tradingagents.strategies.data_sources.edgar_source import EDGARSource
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget

DIRECTORY = f"https://www.sec.gov/Archives/edgar/data/2065397/{ACCESSION.replace('-', '')}"
INDEX = f"{DIRECTORY}/{ACCESSION}-index.htm"
COMPLETE = f"{DIRECTORY}/{ACCESSION}.txt"


class Response:
    status_code = 200
    headers = {}
    url = COMPLETE

    def __init__(self, body=None):
        self.body = body if body is not None else submission()
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        self.closed = True


def call(source, **kwargs):
    return source.get_complete_submission(
        kwargs.pop('url', INDEX), accession=ACCESSION,
        form_type=kwargs.pop('form_type', '8-K'), filing_date='2026-09-30', **kwargs)


def mock_transport(monkeypatch, response):
    from tradingagents.strategies.data_sources import filing_acquisition
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return response

    monkeypatch.setattr(filing_acquisition, 'provider_request', request)
    return calls


def test_one_complete_get_uses_observed_directory_not_accession_prefix(monkeypatch):
    response = Response(submission([('8-K', 'main.htm', '<p>' + 'A ' * 6000 + 'Last paragraph.</p>')]))
    calls = mock_transport(monkeypatch, response)
    evidence = call(EDGARSource(user_agent='research offline@example.com'))
    assert len(calls) == 1
    args, options = calls[0]
    assert args == ('edgar', 'GET', COMPLETE)
    assert options['stream'] is True and options['allow_redirects'] is False
    assert options['headers'] == {'User-Agent': 'research offline@example.com'}
    assert evidence['source_url'] == COMPLETE
    assert evidence['units'][0]['text'].endswith('Last paragraph.')
    assert evidence['analysis_adequacy'] == 'not_assessed'
    assert evidence['structural_status'] == 'complete'
    assert datetime.fromisoformat(evidence['observed_at']).utcoffset() is not None
    assert response.closed


@pytest.mark.parametrize('url', [
    INDEX.replace('https:', 'http:'), INDEX.replace('www.sec.gov', 'private.example'),
    INDEX.replace('www.sec.gov', 'secret@www.sec.gov'), INDEX + '?token=secret', INDEX + '#secret',
    INDEX.replace('/2065397/', '/0/'), INDEX.replace(ACCESSION + '-index', '0000000000-26-000001-index'),
    INDEX.replace('/' + ACCESSION.replace('-', '') + '/', '/123/'),
    INDEX.replace('/2065397/', '/2065397/../2065397/'), INDEX.replace('-index.htm', '%2Dindex.htm'),
])
def test_invalid_source_url_never_requests(monkeypatch, url):
    calls = mock_transport(monkeypatch, Response())
    with pytest.raises(SourceFetchError) as caught:
        call(EDGARSource(), url=url)
    assert caught.value.reason_code == 'invalid_response'
    assert not calls and 'secret' not in str(caught.value)


@pytest.mark.parametrize('kind', ['bad_body', 'wrong_redirect_url', 'wrong_status', 'body_limit'])
def test_failed_body_is_closed_with_no_evidence(monkeypatch, kind):
    response = Response()
    options = {}
    if kind == 'bad_body':
        response.body = b'invalid provider body'
    elif kind == 'wrong_redirect_url':
        response.url = 'https://private.example/secret'
    elif kind == 'wrong_status':
        response.status_code = 302
    else:
        options['max_submission_bytes'] = 10
    mock_transport(monkeypatch, response)
    with pytest.raises(SourceFetchError) as caught:
        call(EDGARSource(), **options)
    assert response.closed
    assert 'private.example' not in str(caught.value)


def test_late_parser_result_cannot_escape_original_budget(monkeypatch):
    from tradingagents.strategies.data_sources import filing_acquisition
    response = Response()
    mock_transport(monkeypatch, response)
    now = [10.0]
    original = filing_acquisition.build_evidence

    def late(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = 15.0
        return result

    monkeypatch.setattr(filing_acquisition, 'build_evidence', late)
    with provider_budget('edgar', 15.0, clock=lambda: now[0], limits=()):
        with pytest.raises(SourceFetchError) as caught:
            call(EDGARSource())
    assert caught.value.reason_code == 'timeout' and response.closed


@pytest.mark.parametrize('form', ['8-K/A', '10-K/A', '10-Q/A'])
def test_exact_ordinary_amendment_keeps_pqc_source_evidence(monkeypatch, form):
    response = Response(submission([(form, 'main.htm', '<p>Amended disclosure.</p>')], form=form))
    mock_transport(monkeypatch, response)
    result = call(EDGARSource(), form_type=form)
    assert result['form'] == form and result['structural_status'] == 'complete'
