"""Synthetic byte responses prove complete windows without live providers."""
import json

import pytest

from tradingagents.strategies.data_sources import regulations_source as reg
from tradingagents.strategies.data_sources import courtlistener_source as court
from tradingagents.strategies.data_sources import congress_source as congress
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget


class Response:
    status_code = 200
    headers = {}

    def __init__(self, data, url):
        self.body = json.dumps(data).encode()
        self.url = url
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.body

    def json(self):
        return json.loads(self.body)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def deny_unmocked_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("unmocked network forbidden in local pagination tests")
    monkeypatch.setattr("requests.sessions.Session.request", deny)
    monkeypatch.setattr("requests.sessions.Session.send", deny)
    monkeypatch.setattr("urllib.request.urlopen", deny)


def transport(monkeypatch, module, pages):
    calls = []
    def request(*args, **kwargs):
        calls.append((args, kwargs))
        assert len(calls) <= len(pages), 'unbounded/extra acquisition'
        return pages[len(calls)-1]
    monkeypatch.setattr(module, 'provider_request', request)
    return calls


def rule(identity, day):
    return {'id': identity, 'attributes': {'title': 'Rule', 'agencyId': 'EPA',
        'documentType': 'Proposed Rule', 'postedDate': day+'T00:00:00Z'}}


def test_regulations_reads_boundary_day_ties_and_proves_older_window(monkeypatch):
    url = reg.BASE_URL+'/documents'
    pages = [Response({'data': [rule('new','2026-10-09'), rule('tie1','2026-09-25')],
                      'meta': {'totalPages': 10, 'totalElements': 20}}, url),
             Response({'data': [rule('tie2','2026-09-25'), rule('old','2026-09-24')],
                      'meta': {'totalPages': 10, 'totalElements': 20}}, url)]
    calls = transport(monkeypatch, reg, pages)
    rows = reg.RegulationsSource('key').search_documents(agency_id='EPA',
        posted_date_from='2026-09-25', posted_date_to='2026-10-09', page_size=2)
    assert [r['document_id'] for r in rows] == ['new','tie1','tie2']
    assert rows.coverage['complete'] is True and rows.coverage['pages'] == 2
    assert [c[1]['params']['page[number]'] for c in calls] == [1,2]
    assert all('filter[postedDate][ge]' not in c[1]['params'] for c in calls)
    assert all(p.closed for p in pages)


@pytest.mark.parametrize('meta', [
    {'totalPages': 0, 'totalElements': 2},
    {'totalPages': 0, 'totalElements': 0},
    {'totalPages': 1, 'totalElements': 2},
    {'totalPages': 1, 'totalElements': 0},
    {'totalPages': 1, 'totalElements': True},
    {'totalPages': 1, 'totalElements': -1},
])
def test_regulations_rejects_contradictory_terminal_totals(monkeypatch, meta):
    response = Response({'data': [rule('one', '2026-10-09')], 'meta': meta}, 'fixture')
    transport(monkeypatch, reg, [response])
    with pytest.raises(SourceFetchError) as caught:
        reg.RegulationsSource('key').search_documents(posted_date_from='2026-09-25')
    assert caught.value.partial_data['coverage']['complete'] is False
    assert response.closed


@pytest.mark.parametrize('changed', [{'totalPages': 3, 'totalElements': 2},
                                    {'totalPages': 2, 'totalElements': 3}])
def test_regulations_rejects_pagination_metadata_drift(monkeypatch, changed):
    pages = [Response({'data': [rule('one', '2026-10-09')],
                       'meta': {'totalPages': 2, 'totalElements': 2}}, 'fixture'),
             Response({'data': [rule('two', '2026-10-08')], 'meta': changed}, 'fixture')]
    transport(monkeypatch, reg, pages)
    with pytest.raises(SourceFetchError) as caught:
        reg.RegulationsSource('key').search_documents(posted_date_from='2026-09-25')
    assert caught.value.partial_data['coverage']['complete'] is False
    assert all(p.closed for p in pages)


def test_regulations_terminal_total_counts_unfiltered_rows(monkeypatch):
    response = Response({'data': [rule('new', '2026-10-09'), rule('old', '2026-09-24')],
                         'meta': {'totalPages': 1, 'totalElements': 2}}, 'fixture')
    transport(monkeypatch, reg, [response])
    rows = reg.RegulationsSource('key').search_documents(posted_date_from='2026-09-25')
    assert [row['document_id'] for row in rows] == ['new']
    assert rows.coverage['complete'] is True


def docket(identity, day='2026-10-09'):
    return {'docket_id': identity, 'caseName': 'Case', 'court': 'Court', 'dateFiled': day}


def test_court_follows_same_query_cursor_to_explicit_terminal(monkeypatch):
    url = court.BASE_URL+'/search/'
    nxt = url+'?q=antitrust&type=r&order_by=dateFiled+desc&filed_after=2026-09-25&filed_before=2026-10-09&page_size=20&cursor=opaque'
    pages = [Response({'count': 2, 'next': nxt, 'results': [docket(1)]}, url),
             Response({'count': 2, 'next': None, 'results': [docket(2)]}, nxt)]
    calls = transport(monkeypatch, court, pages)
    rows = court.CourtListenerSource('key').search_dockets('antitrust',
        date_filed_after='2026-09-25', date_filed_before='2026-10-09')
    assert [r['docket_id'] for r in rows] == [1,2]
    assert rows.coverage['complete'] is True and rows.coverage['pages'] == 2
    assert calls[1][0][2] == nxt
    assert all(p.closed for p in pages)


@pytest.mark.parametrize('count', [0, 2, True, -1, None, '1'])
def test_court_rejects_invalid_or_contradictory_terminal_count(monkeypatch, count):
    response = Response({'count': count, 'next': None, 'results': [docket(1)]}, 'fixture')
    transport(monkeypatch, court, [response])
    with pytest.raises(SourceFetchError) as caught:
        court.CourtListenerSource('key').search_dockets('antitrust')
    assert caught.value.partial_data['coverage']['complete'] is False
    assert response.closed


def test_court_large_complete_population_does_not_treat_estimate_as_exact(monkeypatch):
    # Upstream switches to approximate cardinality at 10,000 parent docket hits.
    # The collected population, not a potentially low estimate, selects the guard.
    response = Response({'count': 9987, 'next': None,
                         'results': [docket(i) for i in range(1, 10001)]}, 'fixture')
    transport(monkeypatch, court, [response])
    rows = court.CourtListenerSource('key').search_dockets('antitrust')
    assert len(rows) == 10000 and rows.coverage['complete'] is True
    assert rows.coverage['count_validation'] == 'approximate_not_compared'


@pytest.mark.parametrize('next_value', ['https://evil.test/search/?cursor=x',
    court.BASE_URL+'/search/?q=changed&cursor=x', 42])
def test_court_rejects_unsafe_or_changed_pagination(monkeypatch, next_value):
    response = Response({'count': 2, 'next': next_value, 'results': [docket(1)]}, court.BASE_URL+'/search/')
    calls = transport(monkeypatch, court, [response])
    with pytest.raises(SourceFetchError) as caught:
        court.CourtListenerSource('key').search_dockets('antitrust')
    assert caught.value.partial_data['coverage']['complete'] is False
    assert len(calls) == 1 and response.closed


def trade(identity, day):
    return {'disclosureId': identity, 'symbol': 'AAPL', 'disclosureDate': day,
        'transactionDate': day, 'office': 'Jane Doe', 'type': 'Purchase',
        'amount': '$1,001 - $15,000'}


def test_congress_full_window_pages_both_chambers_and_keeps_boundary_ties(monkeypatch):
    house = congress.FMP_BASE_URL+'/house-latest'
    senate = congress.FMP_BASE_URL+'/senate-latest'
    pages = [Response([trade('h1','2026-09-09')], house),
             Response([trade('h2','2026-09-09'),trade('old','2026-09-08')], house),
             Response([], senate)]
    calls = transport(monkeypatch, congress, pages)
    rows = congress.CongressSource('key').get_recent_trades(30, '2026-10-09', complete_window=True)
    assert [r['native_disclosure_id'] for r in rows] == ['h1','h2']
    assert rows.coverage['complete'] is True
    assert [c[1]['params']['page'] for c in calls] == [0,1,0]
    assert all(p.closed for p in pages)


def test_congress_repeated_page_fails_and_never_caches_complete_window(monkeypatch):
    url = congress.FMP_BASE_URL+'/house-latest'
    pages = [Response([trade('same','2026-10-09')], url) for _ in range(2)]
    pages.append(Response([], congress.FMP_BASE_URL+'/senate-latest'))
    transport(monkeypatch, congress, pages)
    src = congress.CongressSource('key')
    with pytest.raises(SourceFetchError) as caught:
        src.get_recent_trades(30, '2026-10-09', complete_window=True)
    assert caught.value.partial_data['coverage']['complete'] is False
    assert not src._cache
    assert all(p.closed for p in pages)


def test_court_original_budget_rejects_late_final_body(monkeypatch):
    clock = [1.0]
    response = Response({'count': 1, 'next': None, 'results': [docket(1)]}, court.BASE_URL+'/search/')
    def chunks(chunk_size):
        yield response.body
        clock[0] = 11.0
    response.iter_content = chunks
    transport(monkeypatch, court, [response])
    with provider_budget('courtlistener', 10, clock=lambda: clock[0], limits=()):
        with pytest.raises(SourceFetchError):
            court.CourtListenerSource('key').search_dockets('antitrust')
    assert response.closed

@pytest.mark.parametrize('module,source,provider,payload', [
    (reg, lambda: reg.RegulationsSource('key').search_documents(posted_date_from='2026-09-25'), 'regulations', b'{"data":[],"meta":{"totalPages":0},"data":[]}'),
    (court, lambda: court.CourtListenerSource('key').search_dockets('antitrust'), 'courtlistener', b'{"results":[],"next":null,"next":null}'),
    (congress, lambda: congress.CongressSource('key').get_recent_trades(30,'2026-10-09',complete_window=True), 'congress', b'[{"symbol":"AAPL","symbol":"MSFT"}]'),
])
def test_duplicate_json_keys_are_not_a_completion_proof(monkeypatch,module,source,provider,payload):
    pages=[Response({},'fixture') for _ in range(2 if provider=='congress' else 1)]
    for page in pages: page.body=payload
    transport(monkeypatch,module,pages)
    with pytest.raises(SourceFetchError) as caught: source()
    assert caught.value.partial_data['coverage']['complete'] is False
    assert all(p.closed for p in pages)

@pytest.mark.parametrize('payload', [
    {'data':[rule('old','2026-09-24'),rule('new','2026-10-09')],'meta':{'totalPages':2}},
    {'data':[],'meta':{'totalPages':2}},
    {'data':[]},
])
def test_regulations_invalid_order_or_missing_terminal_proof_fails(monkeypatch,payload):
    response=Response(payload,reg.BASE_URL+'/documents')
    transport(monkeypatch,reg,[response])
    with pytest.raises(SourceFetchError) as caught:
        reg.RegulationsSource('key').search_documents(agency_id='EPA',posted_date_from='2026-09-25')
    assert caught.value.partial_data['coverage']['complete'] is False
    assert response.closed


def test_court_missing_next_is_not_an_empty_complete_window(monkeypatch):
    response=Response({'results':[],'count':0},court.BASE_URL+'/search/')
    transport(monkeypatch,court,[response])
    with pytest.raises(SourceFetchError): court.CourtListenerSource('key').search_dockets('x')
    assert response.closed


def test_congress_future_transaction_invalid_even_on_older_boundary(monkeypatch):
    invalid=trade('bad','2026-09-08');invalid['transactionDate']='2026-10-09'
    pages=[Response([invalid],'fixture'),Response([],'fixture')]
    transport(monkeypatch,congress,pages)
    with pytest.raises(SourceFetchError) as caught:
        congress.CongressSource('key').get_recent_trades(30,'2026-10-09',complete_window=True)
    assert caught.value.partial_data['coverage']['complete'] is False
    assert all(p.closed for p in pages)


def test_congress_complete_window_cache_is_separate_and_success_only(monkeypatch):
    pages=[Response([],'fixture'),Response([],'fixture')]
    calls=transport(monkeypatch,congress,pages)
    src=congress.CongressSource('key')
    src._cache['recent|30|2026-10-09']=['old sample']
    src._cache['fmp_latest']=['old sample']
    assert src.get_recent_trades(30,'2026-10-09',complete_window=True)==[]
    assert src.get_recent_trades(30,'2026-10-09',complete_window=True).coverage['complete'] is True
    assert len(calls)==2


def test_court_existing_rate_policy_cannot_publish_110_page_prefix(monkeypatch):
    import requests
    from urllib.parse import urlencode
    clock=[0.0];responses=[]
    params={'q':'antitrust','type':'r','page_size':20,'order_by':'dateFiled desc',
            'filed_after':'2026-09-25','filed_before':'2026-10-09'}
    def request(url,**kwargs):
        number=len(responses)+1
        nxt=court.BASE_URL+'/search/?'+urlencode({**params,'cursor':str(number)})
        response=Response({'count':2170,'next':nxt if number<110 else None,
                           'results':[docket(number)]},url)
        responses.append(response)
        return response
    monkeypatch.setattr(requests,'get',request)
    def sleep(seconds): clock[0]+=seconds
    with provider_budget('courtlistener',600,clock=lambda:clock[0],sleep=sleep,
                         limits=((5,60),(50,3600),(125,86400)),max_attempts=1):
        with pytest.raises(SourceFetchError) as caught:
            court.CourtListenerSource('key').search_dockets('antitrust',
                date_filed_after='2026-09-25',date_filed_before='2026-10-09')
    assert caught.value.reason_code=='timeout'
    assert len(responses)==50 and all(r.closed for r in responses)
    assert len(caught.value.partial_data['dockets'])==50
    assert caught.value.partial_data['coverage']['complete'] is False
    assert clock[0]<600


@pytest.mark.parametrize('provider',['regulations','congress'])
def test_late_final_body_cannot_publish_or_cache_full_window(monkeypatch,provider):
    clock=[1.0]
    module=reg if provider=='regulations' else congress
    pages=[Response({'data':[],'meta':{'totalPages':0}},'fixture')] if provider=='regulations' else [Response([],'fixture'),Response([],'fixture')]
    def chunks(chunk_size):
        yield pages[0].body
        clock[0]=11.0
    pages[0].iter_content=chunks
    transport(monkeypatch,module,pages)
    src=reg.RegulationsSource('key') if provider=='regulations' else congress.CongressSource('key')
    with provider_budget(provider,10,clock=lambda:clock[0],limits=()):
        with pytest.raises(SourceFetchError) as caught:
            if provider=='regulations':src.search_documents(posted_date_from='2026-09-25')
            else:src.get_recent_trades(30,'2026-10-09',complete_window=True)
    assert caught.value.partial_data['coverage']['complete'] is False
    assert not src._cache and pages[0].closed


@pytest.mark.parametrize('provider',['regulations','congress'])
def test_page_cap_is_explicit_failure_not_complete_prefix(monkeypatch,provider):
    responses=[]
    module=reg if provider=='regulations' else congress
    def request(*args,**kwargs):
        number=kwargs['params'].get('page[number]',kwargs['params'].get('page'))
        if provider=='regulations':payload={'data':[rule(str(number),'2026-10-09')],'meta':{'totalPages':201}}
        else:payload=[] if args[2].endswith('senate-latest') else [trade(str(number),'2026-10-09')]
        response=Response(payload,args[2]);responses.append(response);return response
    monkeypatch.setattr(module,'provider_request',request)
    with pytest.raises(SourceFetchError) as caught:
        if provider=='regulations':reg.RegulationsSource('key').search_documents(posted_date_from='2026-09-25')
        else:congress.CongressSource('key').get_recent_trades(30,'2026-10-09',complete_window=True)
    assert caught.value.partial_data['coverage']['complete'] is False
    assert len(responses)==(200 if provider=='regulations' else 102)
    assert all(r.closed for r in responses)

@pytest.mark.parametrize('provider',['regulations','courtlistener','congress'])
def test_standalone_pagination_has_one_absolute_fallback_budget(monkeypatch,provider):
    from tradingagents.strategies.data_sources.request_policy import current_provider_deadline
    module={'regulations':reg,'courtlistener':court,'congress':congress}[provider]
    seen=[]
    def request(*args,**kwargs):
        seen.append(current_provider_deadline(provider))
        if provider=='regulations':data={'data':[],'meta':{'totalPages':0}}
        elif provider=='courtlistener':data={'results':[],'count':0,'next':None}
        else:data=[]
        return Response(data,args[2])
    monkeypatch.setattr(module,'provider_request',request)
    if provider=='regulations':reg.RegulationsSource('key').search_documents(posted_date_from='2026-09-25')
    elif provider=='courtlistener':court.CourtListenerSource('key').search_dockets('x')
    else:congress.CongressSource('key').get_recent_trades(30,'2026-10-09',complete_window=True)
    assert seen and seen[0] is not None and len(set(seen))==1
    assert current_provider_deadline(provider) is None
