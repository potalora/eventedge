"""Offline provider-operation contracts: a failed source is never healthy empty."""
from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest
import requests

from tradingagents.strategies.data_sources.edgar_source import EDGARSource
from tradingagents.strategies.data_sources.congress_source import CongressSource
from tradingagents.strategies.data_sources.noaa_source import NOAASource
from tradingagents.strategies.data_sources.usda_source import USDASource
from tradingagents.strategies.data_sources.drought_monitor_source import DroughtMonitorSource
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget

SECRET='fixture-secret-token'


def response(payload,status=200):
    return SimpleNamespace(status_code=status, headers={}, json=lambda:payload, text='')


@pytest.fixture(autouse=True)
def no_real_waits(monkeypatch):
    monkeypatch.setattr('time.sleep',lambda _:None)


def test_edgar_search_failure_and_invalid_success_are_explicit(monkeypatch):
    for status,payload,reason in [(500,{},'http_error'),(200,{},'invalid_response'),(200,{'hits':{'hits':[{}]}},'invalid_response')]:
        monkeypatch.setattr(requests,'get',lambda *a,**kw:response(payload,status))
        with provider_budget('edgar',100,clock=lambda:0,sleep=lambda _:None,limits=(),max_attempts=1):
            with pytest.raises(SourceFetchError) as exc:
                EDGARSource().search_filings('10-K')
        assert exc.value.reason_code==reason


def test_edgar_search_uses_forms_filter_and_keyword_query(monkeypatch):
    captured=[]
    monkeypatch.setattr(requests,'get',lambda *a,**kw:captured.append(kw) or response({'hits':{'hits':[]}}))
    with provider_budget('edgar',100,clock=lambda:0,sleep=lambda _:None,limits=()):
        assert EDGARSource().search_filings('10-K',keyword='post-quantum')==[]
    assert captured[0]['params']['forms']=='10-K'
    assert captured[0]['params']['q']=='post-quantum'


def test_congress_missing_key_never_scrapes(monkeypatch):
    monkeypatch.delenv('FMP_API_KEY',raising=False)
    request=Mock(side_effect=AssertionError('unapproved scraper'))
    monkeypatch.setattr(requests,'get',request)
    with pytest.raises(SourceFetchError):
        CongressSource().get_recent_trades()
    assert request.call_count==0


def test_congress_valid_empty_never_falls_back(monkeypatch):
    calls=[]
    monkeypatch.setattr(requests,'get',lambda url,**kw:calls.append(url) or response([]))
    assert CongressSource(fmp_api_key=SECRET).fetch_all_trades()==[]
    assert len(calls)==2
    assert all('/stable/' in url for url in calls)


def test_congress_partial_preserves_success_and_does_not_cache(monkeypatch):
    monkeypatch.setattr(requests,'get',lambda url,**kw:response([{'symbol':'AAPL','transactionDate':'2026-10-01','disclosureDate':'2026-10-02','office':'Jane Doe','type':'Purchase','amount':'$1,001 - $15,000'}]) if 'house-latest' in url else response({},403))
    source=CongressSource(fmp_api_key=SECRET)
    with pytest.raises(SourceFetchError) as exc:
        source.fetch_all_trades()
    assert exc.value.partial_data['recent_trades'][0]['ticker']=='AAPL'
    assert source._cache=={}
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize('provider,operation',[
    ('noaa','state'),('usda','crop'),('drought_monitor','severity'),
])
@pytest.mark.parametrize('failure', ['http','invalid','timeout'])
def test_weather_required_operation_failure_never_empty(provider,operation,failure,monkeypatch):
    payload={'unexpected': True} if failure=='invalid' else []
    def request(*a,**kw):
        if failure=='timeout':raise requests.Timeout(SECRET)
        return response(payload,503 if failure=='http' else 200)
    monkeypatch.setattr(requests,'get',request)
    if provider=='noaa':
        source=NOAASource(token=SECRET)
        source._session=SimpleNamespace(get=request)
        call=lambda:source.fetch_state_daily('FIPS:19','2026-10-01','2026-10-06')
    elif provider=='usda':
        source=USDASource(api_key=SECRET)
        call=lambda:source.fetch_crop_progress('CORN',2026)
    else:
        source=DroughtMonitorSource()
        call=lambda:source.fetch_drought_severity(['IA'],'2026-10-01','2026-10-06')
    with provider_budget(provider,100,clock=lambda:0,sleep=lambda _:None,limits=(),max_attempts=1):
        with pytest.raises(SourceFetchError) as exc:call()
    assert SECRET not in str(exc.value)
    assert source._cache=={}

@pytest.mark.parametrize('provider', ['finnhub','cftc','regulations','fred','courtlistener','usaspending'])
def test_other_required_operation_errors_are_explicit(provider,monkeypatch):
    import sys
    import pandas as pd
    from tradingagents.strategies.data_sources.finnhub_source import FinnhubSource
    from tradingagents.strategies.data_sources.cftc_source import CFTCSource
    from tradingagents.strategies.data_sources.regulations_source import RegulationsSource
    from tradingagents.strategies.data_sources.fred_source import FREDSource
    from tradingagents.strategies.data_sources.courtlistener_source import CourtListenerSource
    from tradingagents.strategies.data_sources.usaspending_source import USASpendingSource
    def fail(*a,**kw):raise requests.Timeout(SECRET)
    monkeypatch.setattr(requests,'get',fail)
    monkeypatch.setattr(requests,'post',fail)
    if provider=='finnhub':
        source=FinnhubSource(api_key=SECRET,sleep_fn=lambda _:None,monotonic_fn=lambda:0)
        source._http=SimpleNamespace(get=fail)
        call=lambda:source.fetch_recent_earnings('2026-10-01','2026-10-06')
    elif provider=='cftc':
        monkeypatch.setitem(sys.modules,'cot_reports',SimpleNamespace(cot_year=fail))
        source=CFTCSource();call=lambda:source._fetch_raw_report()
    elif provider=='regulations':
        source=RegulationsSource(api_key=SECRET);call=lambda:source.search_documents()
    elif provider=='fred':
        monkeypatch.setattr('fredapi.Fred.get_series',fail)
        source=FREDSource(api_key=SECRET);call=lambda:source.fetch_series('UNRATE','2026-10-01','2026-10-06')
    elif provider=='courtlistener':
        source=CourtListenerSource(token=SECRET);call=lambda:source.search_dockets('test')
    else:
        source=USASpendingSource();call=lambda:source.search_contracts()
    with provider_budget(provider,100,clock=lambda:0,sleep=lambda _:None,limits=(),max_attempts=1):
        with pytest.raises(SourceFetchError) as exc:call()
    assert SECRET not in str(exc.value)
    assert source._cache=={}


def test_usda_valid_fallback_remains_partial_not_healthy(monkeypatch):
    source=USDASource(api_key=SECRET)
    monkeypatch.setattr(requests,'get',lambda *a,**kw:response({},503))
    rows=[{'week_ending':'2026-10-04','state':'IA','commodity':'CORN','good_pct':50,'excellent_pct':10}]
    monkeypatch.setattr(source,'_esmis_fallback',lambda *a:rows)
    with provider_budget('usda',100,clock=lambda:0,sleep=lambda _:None,limits=(),max_attempts=1):
        with pytest.raises(SourceFetchError) as exc:source.fetch_crop_progress('CORN',2026)
    assert exc.value.partial_data['crop_progress']['CORN']==rows
    assert source._cache=={}


def test_noaa_missing_collection_is_invalid_but_valid_empty_collection_succeeds(monkeypatch):
    source=NOAASource(token=SECRET)
    for payload,valid in [({},False),({'results':[],'metadata':{'resultset':{'count':0}}},True)]:
        source._session=SimpleNamespace(get=lambda *a,**kw:response(payload))
        with provider_budget('noaa',100,clock=lambda:0,sleep=lambda _:None,limits=()):
            if valid:assert source.fetch_state_daily('FIPS:19','2026-10-01','2026-10-06')==[]
            else:
                with pytest.raises(SourceFetchError):source.fetch_state_daily('FIPS:19','2026-10-01','2026-10-06')


def test_edgar_real_adapter_health_and_preflight_fail_closed(tmp_path,monkeypatch):
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies.orchestration.preflight import run_preflight
    class Observer:
        name='source_observer';track='paper_trade';data_sources=['edgar']
        def get_default_params(self,horizon='30d'):return {}
        def screen(self,data,trading_date,params):return []
    source=EDGARSource();registry=DataSourceRegistry();registry.register(source)
    config={'autoresearch':{'state_dir':str(tmp_path)}}
    engine=MultiStrategyEngine(config,registry=registry,strategies=[Observer()],use_llm=False)
    monkeypatch.setattr(requests,'get',lambda *a,**kw:response({},500))
    monkeypatch.setattr('tradingagents.strategies.data_sources.request_policy.PROVIDER_LIMITS',{})
    data=engine._fetch_all_data('2026-07-01','2026-10-06')
    assert data['edgar'].get('error')
    _,_,health=engine.screen_and_enrich('2026-10-06',data,epoch_id='test',policy_id='30d')
    assert health[0].status=='data_failure'
    assert run_preflight(config,'2026-10-06',engine=engine)['ok'] is False


def test_noaa_partial_page_and_states_are_preserved(monkeypatch):
    source=NOAASource(token=SECRET)
    rows=[{'date':'2026-10-01','datatype':'TMAX','station':'TEST','value':100}]
    calls=[0]
    def pages(*a,**kw):
        calls[0]+=1
        if calls[0]==1:return response({'results':rows,'metadata':{'resultset':{'count':3}}})
        return response({},403)
    source._session=SimpleNamespace(get=pages)
    with provider_budget('noaa',100,clock=lambda:0,sleep=lambda _:None,limits=()):
        with pytest.raises(SourceFetchError) as exc:source.fetch_state_daily('FIPS:19','2026-10-01','2026-10-06')
    assert exc.value.partial_data['observations']==rows
    assert source._cache=={}


def test_event_monitor_preserves_valid_filing_before_bad_form(monkeypatch):
    from tradingagents.strategies.learning.event_monitor import EventMonitor
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    source=EDGARSource();registry=DataSourceRegistry();registry.register(source)
    valid={'_source':{'form':'10-K','file_date':'2026-10-01','display_names':['Company (ABC)'],'adsh':'2026-001','ciks':['0001']}}
    def request(*a,**kw):
        return response({'hits':{'hits':[valid]}}) if kw['params']['forms']=='10-K' else response({},500)
    monkeypatch.setattr(requests,'get',request)
    monitor=EventMonitor(registry);monitor.as_of='2026-10-06'
    with provider_budget('edgar',100,clock=lambda:0,sleep=lambda _:None,limits=(),max_attempts=1):
        with pytest.raises(SourceFetchError) as exc:monitor.poll_edgar_filings(['10-K','10-Q'],fetch_text=False)
    assert exc.value.partial_data['filings'][0]['ticker']=='ABC'
    assert exc.value.failed_http_statuses=={'form_1':500}


def test_finnhub_shared_budget_makes_one_retry_layer(monkeypatch):
    from tradingagents.strategies.data_sources.finnhub_source import FinnhubSource
    clock=[0.0];calls=[]
    def request(*a,**kw):
        calls.append(kw)
        raise requests.Timeout(SECRET)
    source=FinnhubSource(api_key=SECRET,sleep_fn=lambda _:None,monotonic_fn=lambda:clock[0])
    source._http=SimpleNamespace(get=request)
    with provider_budget('finnhub',100,clock=lambda:clock[0],sleep=lambda delay:clock.__setitem__(0,clock[0]+delay),limits=(),max_attempts=3):
        with pytest.raises(SourceFetchError):source.fetch_recent_earnings('2026-10-01','2026-10-06')
    assert len(calls)==3


@pytest.mark.parametrize('operation', ['prices','vix','earnings'])
def test_yfinance_required_transport_failure_is_explicit(operation,monkeypatch):
    import yfinance
    from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
    def fail(*a,**kw):raise requests.Timeout(SECRET)
    monkeypatch.setattr(yfinance,'download',fail)
    monkeypatch.setattr(yfinance,'Ticker',lambda *a:SimpleNamespace(get_earnings_dates=fail))
    source=YFinanceSource()
    calls={'prices':lambda:source.fetch_prices(['ABC'],'2026-10-01','2026-10-06'),
           'vix':lambda:source.fetch_vix('2026-10-01','2026-10-06'),
           'earnings':lambda:source.fetch_earnings_dates(['ABC'])}
    with provider_budget('yfinance',100,clock=lambda:0,sleep=lambda _:None,limits=(),max_attempts=1):
        with pytest.raises(SourceFetchError) as exc:calls[operation]()
    assert SECRET not in str(exc.value)
    assert source._cache=={}


def test_openbb_optional_failure_is_not_cached_as_empty(monkeypatch):
    from tradingagents.strategies.data_sources.openbb_source import OpenBBSource
    def fail(**kw):raise requests.Timeout(SECRET)
    source=OpenBBSource()
    source._obb=SimpleNamespace(equity=SimpleNamespace(screener=SimpleNamespace(screen=fail)))
    with provider_budget('openbb',100,clock=lambda:0,sleep=lambda _:None,limits=(),max_attempts=1):
        result=source.fetch({'method':'sector_tickers','industry':'technology'})
    assert result.get('error')
    assert SECRET not in str(result)
    assert source._cache=={}


@pytest.mark.parametrize('method', ['search','submissions','tickers','text','form4'])
def test_edgar_all_required_operations_reject_invalid_success(method,monkeypatch):
    source=EDGARSource()
    monkeypatch.setattr(requests,'get',lambda *a,**kw:response({}))
    calls={'search':lambda:source.search_filings('10-K'),
           'submissions':lambda:source.get_company_filings('1'),
           'tickers':lambda:source._ensure_company_tickers(),
           'text':lambda:source.get_filing_text('https://sec.invalid/filing'),
           'form4':lambda:source._parse_form4_xml('1',{'accession_number':'2026-01','primary_document':'doc.xml'})}
    with provider_budget('edgar',100,clock=lambda:0,sleep=lambda _:None,limits=()):
        with pytest.raises(SourceFetchError) as exc:calls[method]()
    assert exc.value.reason_code=='invalid_response'


@pytest.mark.parametrize('method', ['earnings','news','peers'])
def test_finnhub_all_required_operations_reject_invalid_success(method,monkeypatch):
    from tradingagents.strategies.data_sources.finnhub_source import FinnhubSource
    source=FinnhubSource(api_key=SECRET,sleep_fn=lambda _:None,monotonic_fn=lambda:0)
    source._http=SimpleNamespace(get=lambda *a,**kw:{'unexpected':True})
    calls={'earnings':lambda:source.fetch_recent_earnings('2026-10-01','2026-10-06'),
           'news':lambda:source.fetch_company_news('ABC','2026-10-01','2026-10-06'),
           'peers':lambda:source.fetch_supply_chain('ABC')}
    with provider_budget('finnhub',100,clock=lambda:0,sleep=lambda _:None,limits=()):
        with pytest.raises(SourceFetchError) as exc:calls[method]()
    assert exc.value.reason_code=='invalid_response'
    assert source._cache=={}


def test_cftc_missing_required_commodity_preserves_existing_positioning(monkeypatch):
    import pandas as pd
    from tradingagents.strategies.data_sources.cftc_source import CFTCSource, COMMODITY_CODES, COL_MARKET, COL_DATE, COL_MM_LONG, COL_MM_SHORT
    source=CFTCSource()
    frame=pd.DataFrame({COL_MARKET:[COMMODITY_CODES['gold']]*4,COL_DATE:['2026-09-01','2026-09-08','2026-09-15','2026-09-22'],COL_MM_LONG:[10,20,30,40],COL_MM_SHORT:[1,2,3,4]})
    monkeypatch.setattr(source,'_fetch_raw_report',lambda *a:frame)
    result=source.fetch({'method':'cot_positioning','commodities':['gold','silver']})
    assert result.get('error')
    assert result['gold']['net_position']==36


@pytest.mark.parametrize('provider',['courtlistener','regulations'])
def test_missing_access_survives_real_monitor_to_engine(provider,tmp_path,monkeypatch):
    from tradingagents.strategies.data_sources.courtlistener_source import CourtListenerSource
    from tradingagents.strategies.data_sources.regulations_source import RegulationsSource
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    monkeypatch.delenv('COURTLISTENER_TOKEN',raising=False)
    monkeypatch.delenv('REGULATIONS_API_KEY',raising=False)
    source=CourtListenerSource() if provider=='courtlistener' else RegulationsSource()
    registry=DataSourceRegistry();registry.register(source)
    engine=MultiStrategyEngine({'autoresearch':{'state_dir':str(tmp_path)}},registry=registry,strategies=[],use_llm=False)
    request=Mock(side_effect=AssertionError('unexpected transport without access'))
    monkeypatch.setattr(requests,'get',request)
    call=engine._fetch_courtlistener_data if provider=='courtlistener' else engine._fetch_regulations_data
    try:result=call()
    except SourceFetchError:result={'error':'explicit access failure'}
    assert result.get('error')
    assert request.call_count==0
