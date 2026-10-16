"""Synthetic complete recent/archive inventories, without SEC requests."""
import pytest

from tradingagents.strategies.data_sources.edgar_source import EDGARSource
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


@pytest.fixture(autouse=True)
def deny_unmocked_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("unmocked network forbidden in local pagination tests")
    monkeypatch.setattr("requests.sessions.Session.request", deny)
    monkeypatch.setattr("requests.sessions.Session.send", deny)
    monkeypatch.setattr("urllib.request.urlopen", deny)


def filing(number, day='2026-10-09', form='4'):
    return {'accession_number': f'0000000001-26-{number:06}', 'form': form,
        'filing_date': day, 'primary_document': f'doc{number}.xml'}


def test_form4_full_window_keeps_more_than40_and_overlapping_archive(monkeypatch):
    source = EDGARSource()
    monkeypatch.setattr(source, 'get_company_filings', lambda *a, **k: (_ for _ in ()).throw(AssertionError('legacy latest40 path still used')))
    monkeypatch.setattr(source, 'ticker_to_cik', lambda _: '1')
    archive = {'name': 'CIK0000000001-submissions-001.json', 'filingFrom': '2026-09-01',
        'filingTo': '2026-10-09', 'filingCount': 2}
    monkeypatch.setattr(source, 'get_company_submission_history', lambda _: {
        'filings': [filing(i) for i in range(1,46)], 'archives': [archive],
        'response_sha256': 'a'*64, 'coverage': {'complete': True}})
    monkeypatch.setattr(source, 'get_company_submission_archive', lambda *args: {
        'filings': [filing(46,'2026-09-25','4/A'),filing(47,'2026-09-24')],
        'coverage': {'complete': True}})
    monkeypatch.setattr(source, '_parse_form4_xml', lambda *args: [{'shares': 1},{'shares': 1}])
    rows = source.get_recent_form4('TEST',14,as_of='2026-10-09')
    assert len(rows) == 92  # Identical transaction rows remain separate observations.
    assert rows.coverage['complete'] is True
    assert rows.coverage['matching_filings'] == 46
    assert rows.coverage['archive_files_consulted'] == [archive['name']]


def test_form4_conflicting_accession_metadata_is_failure(monkeypatch):
    source = EDGARSource()
    monkeypatch.setattr(source, 'get_company_filings', lambda *a, **k: (_ for _ in ()).throw(AssertionError('legacy latest40 path still used')))
    monkeypatch.setattr(source, 'ticker_to_cik', lambda _: '1')
    monkeypatch.setattr(source, 'get_company_submission_history', lambda _: {
        'filings': [filing(1),filing(1,form='4/A')], 'archives': [],
        'response_sha256': 'a'*64, 'coverage': {'complete': True}})
    monkeypatch.setattr(source, '_parse_form4_xml', lambda *args: [])
    with pytest.raises(SourceFetchError):
        source.get_recent_form4('TEST',14,as_of='2026-10-09')


def test_unresolved_form4_identity_is_not_a_complete_empty_window(monkeypatch):
    source=EDGARSource()
    monkeypatch.setattr(source,'ticker_to_cik',lambda _:None)
    with pytest.raises(SourceFetchError) as caught:
        source.get_recent_form4('UNKNOWN',14,as_of='2026-10-09')
    assert caught.value.partial_data['coverage']['complete'] is False


def test_form4_failure_preserves_prior_transactions_and_full_population_count(monkeypatch):
    source=EDGARSource()
    monkeypatch.setattr(source,'ticker_to_cik',lambda _:'1')
    monkeypatch.setattr(source,'get_company_submission_history',lambda _:{
        'filings':[filing(1),filing(2)],'archives':[], 'response_sha256':'a'*64,'coverage':{'complete':True}})
    def parse(cik,row):
        if row['accession_number']==filing(2)['accession_number']:
            raise SourceFetchError('synthetic XML failure',reason_code='invalid_response')
        return [{'shares':1}]
    monkeypatch.setattr(source,'_parse_form4_xml',parse)
    with pytest.raises(SourceFetchError) as caught:
        source.get_recent_form4('TEST',14,as_of='2026-10-09')
    assert len(caught.value.partial_data['form4_filings'])==1
    assert caught.value.partial_data['coverage']['matching_filings']==2
    assert caught.value.partial_data['coverage']['complete'] is False

@pytest.mark.parametrize('failure',['late','overlong','malformed'])
def test_form4_xml_stream_is_bounded_closed_and_uses_original_deadline(monkeypatch,failure):
    from tradingagents.strategies.data_sources import edgar_source as mod
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    clock=[1.0]
    body=b'<ownershipDocument />' if failure!='malformed' else b'<bad>'
    class XMLResponse:
        status_code=200
        headers={'Content-Length':str(16*1024*1024+1)} if failure=='overlong' else {}
        text=body.decode()
        closed=False
        def iter_content(self,chunk_size):
            yield body
            if failure=='late': clock[0]=11.0
        def close(self): self.closed=True
    response=XMLResponse();calls=[]
    monkeypatch.setattr(mod,'provider_request',lambda *a,**kw:calls.append(kw) or response)
    with provider_budget('edgar',10,clock=lambda:clock[0],limits=()):
        with pytest.raises(SourceFetchError):
            EDGARSource()._parse_form4_xml('1',filing(1))
    assert response.closed
    assert calls[0]['stream'] is True and calls[0]['allow_redirects'] is False

@pytest.mark.parametrize('kind',['metadata','transactions'])
def test_form4_full_window_finite_population_caps_fail_explicitly(monkeypatch,kind):
    source=EDGARSource()
    monkeypatch.setattr(source,'ticker_to_cik',lambda _:'1')
    rows=[filing(1)]*(100001 if kind=='metadata' else 1)
    monkeypatch.setattr(source,'get_company_submission_history',lambda _:{'filings':rows,
        'archives':[],'coverage':{'complete':True},'response_sha256':'a'*64})
    monkeypatch.setattr(source,'_parse_form4_xml',lambda *args:[{'shares':1}]*100001)
    with pytest.raises(SourceFetchError) as caught:
        source.get_recent_form4('TEST',14,as_of='2026-10-09')
    assert caught.value.reason_code=='invalid_response'
    assert caught.value.partial_data['coverage']['complete'] is False
