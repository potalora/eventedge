"""Focused declared Court scope; all HTTP is synthetic and network is forbidden."""
import copy
import json
from datetime import datetime, timezone, timedelta

import pytest

from tradingagents.strategies.data_sources import courtlistener_source as court
from tradingagents.strategies.data_sources.request_policy import provider_budget


class Clock:
    def __init__(self): self.value = 100.
    def __call__(self): return self.value
    def sleep(self, seconds): self.value += seconds


class Response:
    status_code = 200
    headers = {}
    def __init__(self, value): self.body = json.dumps(value).encode(); self.closed = False
    def iter_content(self, chunk_size): yield self.body
    def close(self): self.closed = True


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs): raise AssertionError('unmocked acquisition forbidden')
    monkeypatch.setattr('requests.sessions.Session.request', deny)
    monkeypatch.setattr('requests.sessions.Session.send', deny)
    monkeypatch.setattr('urllib.request.urlopen', deny)


def scope(names=('Apple Inc.',), cases=()):
    return {'policy':'focused_litigation_v1', 'issuers':[
        {'ticker':f'ISSUER{i}', 'issuer_cik':str(i+1).zfill(10), 'legal_name':name,
         'verification':{'source':'sec_company_map','sha256':'a'*64}, 'roles':['held']}
        for i, name in enumerate(names)], 'case_ids':list(cases)}


def docket(identity=10):
    return {'docket_id':identity, 'caseName':'Investor v. Apple Inc.', 'court':'cand',
        'dateFiled':'2026-10-09', 'suitNature':'Securities', 'cause':'Securities law'}


def install(monkeypatch, pages):
    calls=[]
    def get(url, **kwargs):
        calls.append((url, kwargs)); assert len(calls)<=len(pages), 'extra request'
        value=pages[len(calls)-1]
        if isinstance(value, Exception): raise value
        return value
    monkeypatch.setattr('requests.get', get)
    return calls


def collect(selected, clock, **kwargs):
    return court.CourtListenerSource('synthetic-token').fetch_focused_litigation(selected,
        date_filed_after='2026-09-25', date_filed_before='2026-10-09',
        absolute_deadline=700., **kwargs)


def test_focused_complete_exact_scope_and_metadata_only(monkeypatch):
    pages=[Response({'count':1,'results':[docket()], 'next':None}),
        Response({'id':55,'court_id':'dcd','court':court.BASE_URL+'/courts/dcd/',
            'case_name':'Existing case', 'date_filed':'2021-01-01', 'nature_of_suit':'Patent',
            'cause':'Patent', 'date_modified':'2026-10-09T12:00:00Z'})]
    calls=install(monkeypatch,pages);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):
        result=collect(scope(cases=(55,)),clock)
    assert result['coverage']['complete'] is True and result['coverage']['requests']==2
    assert result['coverage']['content_kind']=='docket_metadata_only'
    assert [r['docket_id'] for r in result['dockets']]==[10,55]
    assert result['dockets'][0]['native_record']==docket()
    assert calls[0][1]['params']['q']=='caseName:"Apple Inc."'
    assert calls[1][0]==court.BASE_URL+'/dockets/55/'
    assert all(p.closed for p in pages)


def test_request_cap_preserves_every_selected_query_as_incomplete(monkeypatch):
    pages=[Response({'count':0,'results':[], 'next':None}) for _ in range(2)]
    calls=install(monkeypatch,pages);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):
        result=collect(scope(('A Inc.','B Inc.','C Inc.')),clock,request_cap=2)
    coverage=result['coverage']
    assert coverage['complete'] is False and coverage['requests']==len(calls)==2
    assert len(coverage['queries'])==3
    assert [q['status'] for q in coverage['queries']]==['complete','complete','not_attempted']
    assert coverage['request_cap_exhausted'] is True and result.get('error')


def test_rate_policy_and_parent_deadline_preserved(monkeypatch):
    pages=[Response({'count':0,'results':[], 'next':None}) for _ in range(6)]
    calls=install(monkeypatch,pages);clock=Clock();diagnostics=[]
    with provider_budget('courtlistener',130.,clock=clock,sleep=clock.sleep,diagnostics=diagnostics):
        result=collect(scope(tuple(f'Issuer {i} Inc.' for i in range(6))),clock)
    assert len(calls)==5 and result['coverage']['complete'] is False
    assert result['coverage']['budget_exhausted'] is True
    assert diagnostics and result['coverage']['absolute_deadline']==130.


def test_exact_prior_reuse_keeps_original_clock_and_stale_is_visible(monkeypatch):
    pages=[Response({'count':0,'results':[], 'next':None}) for _ in range(2)]
    calls=install(monkeypatch,pages);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):
        original=collect(scope(),clock)
        reused=collect(scope(),clock,prior_evidence=original,max_evidence_age_seconds=3600)
    assert len(calls)==1 and reused['coverage']['acquired_at']==original['coverage']['acquired_at']
    assert reused['reuse']['status']=='exact_prior_evidence' and reused['coverage']==original['coverage']
    old=copy.deepcopy(original)
    from tradingagents.strategies.data_sources.courtlistener_scope import evidence_digest
    old['coverage']['acquired_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    old['coverage']['acquisition_started_at']=old['coverage']['acquired_at']
    old['coverage']['evidence_sha256']=evidence_digest(old)
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):
        fresh=collect(scope(),clock,prior_evidence=old,max_evidence_age_seconds=3600)
    assert len(calls)==2 and fresh['coverage']['prior_reuse_status']=='stale'


@pytest.mark.parametrize('mutation',[
    lambda s:s['issuers'][0].update(legal_name=''),
    lambda s:s['issuers'][0].update(verification={'source':'guess','sha256':'bad'}),
    lambda s:s['issuers'][0].update(roles=['unverified']),
    lambda s:s['case_ids'].append(True),
])
def test_unverified_or_malformed_scope_never_requests(monkeypatch,mutation):
    calls=install(monkeypatch,[]);selected=scope();mutation(selected);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):
        with pytest.raises(ValueError):collect(selected,clock)
    assert not calls


def test_subbudget_has_no_phantom_request_and_restores_parent_after_exception(monkeypatch):
    from tradingagents.strategies.data_sources import request_policy as policy
    clock=Clock();diagnostics=[]
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep,diagnostics=diagnostics):
        with pytest.raises(RuntimeError):
            with policy.provider_subbudget('courtlistener',maximum_seconds=120,absolute_deadline=150):
                assert policy.current_provider_deadline('courtlistener')==150
                raise RuntimeError('synthetic')
        assert policy.current_provider_deadline('courtlistener')==700.
    assert diagnostics==[]


@pytest.mark.parametrize('known_case', [False, True])
def test_physical_retry_cap_rejects_before_next_rate_slot(monkeypatch, known_case):
    from tradingagents.strategies.data_sources import request_policy as policy
    pages=[Response({}),Response({})]
    for page in pages: page.status_code=503
    calls=install(monkeypatch,pages);clock=Clock();diagnostics=[]
    selected=scope(names=() if known_case else ('Apple Inc.',),cases=(55,) if known_case else ())
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep,
                         limits=((50,60),),random_fn=lambda:0,diagnostics=diagnostics):
        result=collect(selected,clock,request_cap=2)
    assert len(calls)==result['coverage']['requests']==2
    assert len(policy._HISTORY[('courtlistener',clock,50,60)])==2
    assert diagnostics[-1]['attempts']==2
    assert result['coverage']['complete'] is False
    assert result['coverage']['request_cap_exhausted'] is True
    assert all(page.closed for page in pages)


def test_public_validator_rejects_rehashed_wrong_native_identity(monkeypatch):
    pages=[Response({'count':1,'results':[docket()], 'next':None})]
    install(monkeypatch,pages);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):
        result=collect(scope(),clock)
    from tradingagents.strategies.data_sources.courtlistener_scope import validate_focused_litigation, evidence_digest
    assert validate_focused_litigation(result,expected_scope=scope(),date_filed_after='2026-09-25',date_filed_before='2026-10-09')==result
    bad=copy.deepcopy(result);bad['dockets'][0]['native_record']['docket_id']=99
    bad['coverage']['evidence_sha256']=evidence_digest(bad)
    with pytest.raises(ValueError):validate_focused_litigation(bad,expected_scope=scope(),date_filed_after='2026-09-25',date_filed_before='2026-10-09')


@pytest.mark.parametrize('fault',['incomplete','query_missing','scope_rebound','extra_row','query_ids','clock_future','clock_order','native_case','missing_native'])
def test_public_validator_enforces_complete_scope_rows_and_chronology(monkeypatch,fault):
    install(monkeypatch,[Response({'count':1,'results':[docket()], 'next':None})]);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):result=collect(scope(),clock)
    from tradingagents.strategies.data_sources.courtlistener_scope import validate_focused_litigation, evidence_digest
    if fault=='incomplete':result['coverage']['complete']=False
    if fault=='query_missing':result['coverage']['queries']=[]
    if fault=='scope_rebound':result['coverage']['scope']['issuers'][0]['legal_name']='Other Corp.'
    if fault=='extra_row':result['dockets'].append({**result['dockets'][0],'docket_id':88});result['coverage']['returned']=2
    if fault=='query_ids':result['coverage']['queries'][0]['docket_ids']=[]
    if fault=='clock_future':result['coverage']['acquired_at']=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    if fault=='clock_order':result['coverage']['acquisition_started_at']=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    if fault=='native_case':result['dockets'][0]['case_name']='Substituted case'
    if fault=='missing_native':result['dockets'][0].pop('native_record')
    result['coverage']['evidence_sha256']=evidence_digest(result)
    with pytest.raises(ValueError):validate_focused_litigation(result,expected_scope=scope(),date_filed_after='2026-09-25',date_filed_before='2026-10-09')


def test_focused_pages_until_terminal_and_failure_keeps_all_queries(monkeypatch):
    from urllib.parse import urlencode
    params={'q':'caseName:"Apple Inc."','type':'d','page_size':20,'order_by':'dateFiled desc',
        'filed_after':'2026-09-25','filed_before':'2026-10-09','cursor':'next'}
    pages=[Response({'count':2,'results':[docket(10)],'next':court.BASE_URL+'/search/?'+urlencode(params)}),
        Response({'count':2,'results':[docket(11)],'next':None})]
    calls=install(monkeypatch,pages);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):result=collect(scope(),clock)
    assert result['coverage']['complete'] is True and result['coverage']['queries'][0]['docket_ids']==[10,11]
    assert result['coverage']['requests']==2 and calls[1][1]['params'] is None and all(p.closed for p in pages)


def test_deadline_after_stream_never_accepts_and_closes(monkeypatch):
    clock=Clock()
    class Late(Response):
        def iter_content(self,chunk_size):
            clock.value=221.
            yield self.body
    response=Late({'count':0,'results':[], 'next':None});install(monkeypatch,[response])
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):result=collect(scope(),clock)
    assert result['coverage']['complete'] is False and result['coverage']['budget_exhausted'] is True
    assert response.closed


def test_known_case_without_token_cannot_attempt_transport(monkeypatch):
    monkeypatch.delenv('COURTLISTENER_TOKEN',raising=False)
    calls=install(monkeypatch,[]);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):
        result=court.CourtListenerSource().fetch_focused_litigation(scope(names=(),cases=(55,)),
            date_filed_after='2026-09-25',date_filed_before='2026-10-09',absolute_deadline=700.)
    assert not calls and result['coverage']['requests']==0 and not result['coverage']['complete']


def test_completed_replay_validates_original_evidence_without_current_age(monkeypatch):
    install(monkeypatch,[Response({'count':0,'results':[], 'next':None})]);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):result=collect(scope(),clock)
    from tradingagents.strategies.data_sources.courtlistener_scope import validate_focused_litigation
    later=datetime.now(timezone.utc)+timedelta(days=5)
    result['courtlistener_scope_policy']='focused_litigation_v1';result['_request_diagnostics']=[]
    assert validate_focused_litigation(result,expected_scope=scope(),date_filed_after='2026-09-25',
        date_filed_before='2026-10-09',now=later,max_evidence_age_seconds=None)==result
    with pytest.raises(ValueError):validate_focused_litigation(result,expected_scope=scope(),date_filed_after='2026-09-25',date_filed_before='2026-10-09',now=later)


def test_rehashed_substantive_metadata_substitution_rejected(monkeypatch):
    install(monkeypatch,[Response({'count':1,'results':[docket()], 'next':None})]);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):result=collect(scope(),clock)
    from tradingagents.strategies.data_sources.courtlistener_scope import validate_focused_litigation,evidence_digest
    result['dockets'][0]['cause']='Invented criminal misconduct'
    result['coverage']['evidence_sha256']=evidence_digest(result)
    with pytest.raises(ValueError):validate_focused_litigation(result,expected_scope=scope(),date_filed_after='2026-09-25',date_filed_before='2026-10-09')


def test_retries_count_toward_shared_http_cap_without_restarting_queries(monkeypatch):
    first=Response({});first.status_code=503
    second=Response({});second.status_code=503
    calls=install(monkeypatch,[first,second]);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep,random_fn=lambda:0):
        result=collect(scope(('A Inc.','B Inc.')),clock,request_cap=2)
    assert len(calls)==2 and first.closed and second.closed
    assert result['coverage']['request_cap_exhausted'] is True and not result['coverage']['complete']
    assert [q['status'] for q in result['coverage']['queries']]==['failed','not_attempted']


def test_malformed_native_metadata_cannot_claim_complete(monkeypatch):
    malformed=docket();malformed['cause']={'invented':'container'}
    response=Response({'count':1,'results':[malformed],'next':None});install(monkeypatch,[response]);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):result=collect(scope(),clock)
    assert result['coverage']['complete'] is False and result.get('error') and response.closed


@pytest.mark.parametrize('body',[b'{"count":0,"count":0,"results":[],"next":null}',
    b'{"count":0,"results":[],"next":null} trailing',b'{"count":NaN,"results":[],"next":null}'])
def test_focused_malformed_json_is_incomplete_and_closed(monkeypatch,body):
    response=Response({});response.body=body;install(monkeypatch,[response]);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):result=collect(scope(),clock)
    assert not result['coverage']['complete'] and response.closed


def test_rehashed_prior_with_different_window_must_reacquire(monkeypatch):
    pages=[Response({'count':0,'results':[], 'next':None}) for _ in range(2)]
    calls=install(monkeypatch,pages);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):original=collect(scope(),clock)
    from tradingagents.strategies.data_sources.courtlistener_scope import evidence_digest
    prior=copy.deepcopy(original);prior['coverage']['date_filed_before']='2026-10-08';prior['coverage']['evidence_sha256']=evidence_digest(prior)
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):result=collect(scope(),clock,prior_evidence=prior)
    assert len(calls)==2 and result['coverage']['prior_reuse_status']=='invalid'


@pytest.mark.parametrize('maximum,deadline',[(True,100),(0,100),(float('inf'),100),(1,float('nan'))])
def test_subbudget_rejects_invalid_bounds(maximum,deadline):
    from tradingagents.strategies.data_sources.request_policy import provider_subbudget
    with pytest.raises(ValueError):
        with provider_subbudget('courtlistener',maximum_seconds=maximum,absolute_deadline=deadline):pass


def test_nonfinite_native_json_extra_field_is_not_ignored(monkeypatch):
    response=Response({});response.body=b'{"count":0,"results":[],"next":null,"extra":1e9999}'
    install(monkeypatch,[response]);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):result=collect(scope(),clock)
    assert result['coverage']['complete'] is False and response.closed


def test_empty_declared_scope_has_no_request_and_no_global_claim(monkeypatch):
    calls=install(monkeypatch,[]);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):result=collect(scope(names=()),clock)
    assert not calls and result['coverage']['complete'] is True
    assert result['coverage']['scope']=={'policy':'focused_litigation_v1','issuers':[],'case_ids':[]}
    assert result['coverage']['content_kind']=='docket_metadata_only' and not result['dockets']


def test_focused_stream_aggregate_byte_cap_closes_response(monkeypatch):
    from tradingagents.strategies.data_sources import courtlistener_scope
    monkeypatch.setattr(courtlistener_scope,'MAX_BYTES',64)
    response=Response({'count':0,'results':[], 'next':None,'padding':'x'*100})
    install(monkeypatch,[response]);clock=Clock()
    with provider_budget('courtlistener',700.,clock=clock,sleep=clock.sleep):result=collect(scope(),clock)
    assert not result['coverage']['complete'] and response.closed
