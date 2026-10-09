"""Provider-shaped transport regressions for the 2026-10-09 source audit."""
from types import SimpleNamespace as NS
import sys

import pandas as pd
import pytest
import requests

from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr('tradingagents.strategies.data_sources.request_policy.PROVIDER_LIMITS', {})
    monkeypatch.setattr('time.sleep', lambda _: None)
    monkeypatch.setattr(requests.sessions.Session, 'request', lambda *a, **k: pytest.fail('unexpected network'))


def response(payload=None, text=''):
    return NS(status_code=200, headers={}, json=lambda: payload, text=text)


def test_edgar_exhaustive_window_and_primary_document_not_directory(monkeypatch):
    from tradingagents.strategies.data_sources.edgar_source import EDGARSource
    calls = []
    def transport(provider, method, url, **kw):
        calls.append((url, kw.get('params')))
        if 'search-index' in url:
            offset = kw['params'].get('from', 0)
            hit = {'_id': f'0001-26-00000{offset + 1}:exhibit.htm', '_source': {'form': '10-K', 'file_date': '2026-10-01', 'adsh': f'0001-26-00000{offset + 1}', 'ciks': ['0001'], 'display_names': ['Example (EXM)']}}
            return response({'hits': {'hits': [hit], 'total': {'value': 2, 'relation': 'eq'}}})
        if url.endswith('-index.htm'):
            return response(text='<table class="tableFile"><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th></tr><tr><td>1</td><td>Annual report</td><td><a href="/Archives/edgar/data/1/000126000001/report.htm">report.htm</a></td><td>10-K</td></tr><tr><td>2</td><td>Exhibit</td><td><a href="exhibit.htm">exhibit.htm</a></td><td>EX-99</td></tr></table>')
        assert url.endswith('/report.htm')
        return response(text='<html>Actual annual report</html>')
    monkeypatch.setattr('tradingagents.strategies.data_sources.edgar_source.provider_request', transport)
    source = EDGARSource()
    filings = source.search_filings('10-K', '2026-10-01', '2026-10-09')
    assert len(filings) == 2
    assert 'Actual annual report' in source.get_filing_text(filings[0]['file_url'])
    assert any(url.endswith('report.htm') for url, _ in calls)


@pytest.mark.parametrize('code,ad,direction,open_market', [('J','D','sell',False), ('J','A','buy',False), ('P','A','buy',True), ('A','A','buy',False), ('M','A','buy',False), ('P','D','other',False)])
def test_form4_preserves_direction_and_economic_transaction(code, ad, direction, open_market, monkeypatch):
    from tradingagents.strategies.data_sources.edgar_source import EDGARSource
    xml = f'<ownershipDocument><reportingOwner><reportingOwnerId><rptOwnerCik>123</rptOwnerCik><rptOwnerName>Officer</rptOwnerName></reportingOwnerId></reportingOwner><nonDerivativeTransaction><transactionCoding><transactionCode>{code}</transactionCode></transactionCoding><transactionAmounts><transactionShares><value>100</value></transactionShares><transactionPricePerShare><value>20</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction></ownershipDocument>'
    monkeypatch.setattr('tradingagents.strategies.data_sources.edgar_source.provider_request', lambda *a, **k: response(text=xml))
    row = EDGARSource()._parse_form4_xml('1', {'accession_number':'0001-26-000001','primary_document':'form4.xml'})[0]
    assert row['transaction_type'] == direction
    assert row['acquired_disposed'] == ad and row['transaction_code'] == code
    assert row['open_market'] is open_market and row['owner_cik'] == '123'
    assert row['transaction_id']


def test_drought_requests_disjoint_categories_and_bounds_score(monkeypatch):
    import tradingagents.strategies.data_sources.drought_monitor_source as mod
    monkeypatch.setattr(mod, 'current_session_date', lambda: '2026-06-04', raising=False)
    def transport(*a, **kw):
        categorical = kw['params']['statisticsType'] == 2
        return response([{'MapDate':'20260602','StateAbbreviation':'IA','StatisticFormatID':2 if categorical else 1,'None':0,'D0':0 if categorical else 100,'D1':0 if categorical else 100,'D2':0 if categorical else 100,'D3':0 if categorical else 100,'D4':100}])
    monkeypatch.setattr(mod, 'provider_request', transport)
    assert mod.DroughtMonitorSource().fetch_composite_score(['IA'], '2026-06-04') == 4


@pytest.mark.parametrize('provider', ['drought', 'noaa', 'usda'])
def test_missing_requested_environment_is_failure(provider, monkeypatch):
    from tradingagents.strategies.data_sources.drought_monitor_source import DroughtMonitorSource
    from tradingagents.strategies.data_sources.noaa_source import NOAASource
    from tradingagents.strategies.data_sources.usda_source import USDASource
    today = '2026-09-14'
    for name in ('drought_monitor', 'noaa', 'usda'):
        monkeypatch.setattr(f'tradingagents.strategies.data_sources.{name}_source.current_session_date', lambda: today)
    for name in ('drought_monitor','noaa','usda'):
        monkeypatch.setattr(f'tradingagents.strategies.data_sources.{name}_source.provider_request', lambda *a, name=name, **kw: response({'data': [], 'results': [], 'metadata':{'resultset':{'count':0}}} if name != 'drought_monitor' else []))
    call = {'drought':lambda: DroughtMonitorSource().fetch_composite_score(['IA'], today), 'noaa':lambda: NOAASource(token='x').fetch_ag_weather_summary(today, 1), 'usda':lambda: USDASource(api_key='x').fetch_crop_progress('CORN', int(today[:4]), 'IA')}[provider]
    with pytest.raises(SourceFetchError):
        call()


@pytest.mark.parametrize('day,expected', [('2026-04-14',0),('2026-04-15',0),('2026-04-16',1),('2026-05-01',1),('2026-05-14',1),('2026-05-15',1)])
def test_noaa_aggregates_regional_days_and_full_frost_dates(day, expected, monkeypatch):
    import tradingagents.strategies.data_sources.noaa_source as mod
    monkeypatch.setattr(mod, 'AG_STATES', {'IA':'FIPS:19','IL':'FIPS:17'})
    monkeypatch.setattr(mod, 'current_session_date', lambda: day, raising=False)
    rows = [{'date':day+'T00:00:00','datatype':dtype,'station':str(i),'value':value,'attributes':',,,'} for i in range(100) for dtype,value in [('TMAX',100),('TMIN',20),('PRCP',0.1)]]
    monkeypatch.setattr(mod.NOAASource, 'fetch_region_daily', lambda self, start, end: {state:rows for state in mod.AG_STATES})
    out = mod.NOAASource(token='x').fetch_ag_weather_summary(day, 1)
    assert out['heat_stress_days'] == 1
    assert out['frost_events'] == expected
    assert out['day_unit'] == 'regional_days' and out['coverage']['complete'] is True


def test_noaa_session_does_not_globally_replace_socket_connector(monkeypatch):
    import urllib3.util.connection as connection
    from tradingagents.strategies.data_sources.noaa_source import _build_session
    from requests.adapters import HTTPAdapter
    original = connection.create_connection
    def send(*a, **kw):
        assert connection.create_connection is original
        return response()
    monkeypatch.setattr(HTTPAdapter, 'send', send)
    session = _build_session()
    session.get_adapter('https://www.ncei.noaa.gov').send(requests.Request('GET','https://www.ncei.noaa.gov/cdo-web/api/v2/data').prepare())


def test_cftc_rolling_lookback_and_acquisition_availability(monkeypatch):
    import tradingagents.strategies.data_sources.cftc_source as mod
    monkeypatch.setattr(mod, 'current_session_date', lambda: '2026-01-31', raising=False)
    years = []
    def annual(year, **kw):
        years.append(year)
        dates = pd.date_range('2025-02-04', periods=48, freq='7D') if year == 2025 else pd.date_range('2026-01-06', periods=4, freq='7D')
        return pd.DataFrame({mod.COL_MARKET:[mod.COMMODITY_CODES['gold']]*len(dates),mod.COL_DATE:dates.strftime('%Y-%m-%d'),mod.COL_MM_LONG:[200]*48 if year==2025 else [10,20,30,100],mod.COL_MM_SHORT:[0]*len(dates)})
    monkeypatch.setitem(sys.modules, 'cot_reports', NS(cot_year=annual))
    row = mod.CFTCSource().fetch({'commodities':['gold'],'lookback_weeks':52,'as_of':'2026-01-31'})['gold']
    assert set(years) >= {2025,2026}
    assert row['percentile'] == round(3/52,4) and row['lookback_observations'] == 52
    assert row['observation_date'] == '2026-01-27'
    assert row['available_at'] == row['acquired_at'] and row['available_at'] > row['observation_date']


@pytest.mark.parametrize('provider', ['cftc','noaa','drought_monitor','usda'])
def test_unsupported_fresh_historical_vintage_is_refused(provider, monkeypatch):
    from tradingagents.strategies.data_sources.cftc_source import CFTCSource
    from tradingagents.strategies.data_sources.noaa_source import NOAASource
    from tradingagents.strategies.data_sources.drought_monitor_source import DroughtMonitorSource
    from tradingagents.strategies.data_sources.usda_source import USDASource
    sources = {'cftc':(CFTCSource(), {'as_of':'2026-01-01'}), 'noaa':(NOAASource(token='x'), {'date':'2026-01-01'}), 'drought_monitor':(DroughtMonitorSource(), {'end':'2026-01-01'}), 'usda':(USDASource(api_key='x'), {'commodity':'CORN','year':2026,'as_of':'2026-01-01'})}
    source, params = sources[provider]
    out = source.fetch(params)
    assert 'historical_vintage_unavailable' in out.get('error','')


def test_fred_binds_vintage_and_distinguishes_cache(monkeypatch):
    from tradingagents.strategies.data_sources.fred_source import FREDSource
    calls=[]
    def get(*a, **kw):
        calls.append(kw)
        value=300 if kw.get('realtime_end')=='2026-01-31' else 310
        return pd.Series([value], index=pd.to_datetime(['2026-01-01']))
    monkeypatch.setattr('fredapi.Fred.get_series', get)
    source=FREDSource(api_key='x')
    assert source.fetch_series('CPIAUCSL','2026-01-01','2026-01-31').iloc[0] == 300
    assert source.fetch_series('CPIAUCSL','2026-01-01','2026-01-31',as_of='2026-02-28').iloc[0] == 310
    assert calls[0]['realtime_start']==calls[0]['realtime_end']=='2026-01-31'


def test_usaspending_exhaustive_pagination(monkeypatch):
    from tradingagents.strategies.data_sources.usaspending_source import USASpendingSource
    def transport(*a, **kw):
        page=kw['json']['page']
        return response({'results':[{'Award ID':str(page),'generated_internal_id':f'CONT_{page}','Recipient Name':'Example','Award Amount':100,'Start Date':'2026-10-01','Base Obligation Date':'2026-10-01'}], 'page_metadata':{'page':page,'hasNext':page==1}})
    monkeypatch.setattr('tradingagents.strategies.data_sources.usaspending_source.provider_request',transport)
    assert [r['award_id'] for r in USASpendingSource().search_contracts(date_from='2026-10-01',date_to='2026-10-09')] == ['1','2']


@pytest.mark.parametrize('provider', ['courtlistener','regulations','congress'])
def test_bounded_sample_metadata_is_explicit_and_serializable(provider,monkeypatch):
    from tradingagents.strategies.data_sources.courtlistener_source import CourtListenerSource
    from tradingagents.strategies.data_sources.regulations_source import RegulationsSource
    from tradingagents.strategies.data_sources.congress_source import CongressSource
    from tradingagents.strategies.orchestration.source_inputs import _transform
    payloads={'courtlistener':{'results':[],'count':200,'next':'https://example/next'},'regulations':{'data':[],'meta':{'totalPages':2,'totalElements':200}},'congress':[]}
    monkeypatch.setattr(f'tradingagents.strategies.data_sources.{provider}_source.provider_request',lambda *a, **kw:response(payloads[provider]))
    source={'courtlistener':CourtListenerSource(token='x'),'regulations':RegulationsSource(api_key='x'),'congress':CongressSource(fmp_api_key='x')}[provider]
    out=source.fetch({'method':{'courtlistener':'search_dockets','regulations':'search_documents','congress':'recent_trades'}[provider]})
    assert out['coverage']['mode']=='bounded_sample'
    assert out['coverage']['complete'] is False
    assert _transform(_transform(out, decoding=False), decoding=True)['coverage']==out['coverage']


def test_retired_openbb_curve_never_infers_maturities_from_dates():
    from tradingagents.strategies.data_sources.openbb_source import OpenBBSource
    source=OpenBBSource()
    source._obb=NS(derivatives=NS(futures=NS(historical=lambda **kw:pytest.fail('retired historical-as-curve acquisition'))))
    out=source.fetch({'method':'commodity_futures_curve','symbol':'GC'})
    assert 'error' in out and 'unsupported' in out['error']


def test_etf_cache_includes_labels_and_assets_and_reports_price_return(monkeypatch):
    from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
    def download(tickers, **kw):
        return pd.DataFrame({('Close',tickers[0]):[100,110]})
    monkeypatch.setattr('yfinance.download',download)
    source=YFinanceSource()
    assert source.fetch_etf_returns({'market':'SPY'},'2026-01-01','2026-02-01')=={'market':pytest.approx(.1)}
    assert source.fetch_etf_returns({'bond':'TLT'},'2026-01-01','2026-02-01')=={'bond':pytest.approx(.1)}


def test_edgar_inline_viewer_primary_and_accession_scope(monkeypatch):
    from tradingagents.strategies.data_sources.edgar_source import EDGARSource
    html='<table class="tableFile"><tr><td>1</td><td>Annual</td><td><a href="/ix?doc=/Archives/edgar/data/1/000126000001/report.htm">report.htm</a></td><td>10-K</td></tr></table>'
    monkeypatch.setattr('tradingagents.strategies.data_sources.edgar_source.provider_request',lambda *a, **kw:response(text=html))
    assert EDGARSource().get_primary_document_url('https://www.sec.gov/Archives/edgar/data/1/000126000001/0001-26-000001-index.htm','10-K') == 'https://www.sec.gov/Archives/edgar/data/1/000126000001/report.htm'


@pytest.mark.parametrize('provider',['edgar','usaspending','cftc'])
def test_multi_request_acquisition_shares_one_absolute_budget(provider, monkeypatch):
    from tradingagents.strategies.data_sources.edgar_source import EDGARSource
    from tradingagents.strategies.data_sources.usaspending_source import USASpendingSource
    import tradingagents.strategies.data_sources.cftc_source as cot_mod
    clock=[0.0]
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    calls=[]
    def transport(*a, **kw):
        calls.append(kw)
        clock[0]+=61
        if provider=='edgar':
            hit={'_id':'0001-26-000001:report.htm','_source':{'form':'10-K','file_date':'2026-10-01','adsh':'0001-26-000001','ciks':['1'],'display_names':['Example']}}
            return response({'hits':{'hits':[hit],'total':{'value':2,'relation':'eq'}}})
        return response({'results':[{'Award ID':str(len(calls)),'generated_internal_id':f'CONT_{len(calls)}','Recipient Name':'Example','Award Amount':1,'Start Date':'2026-10-01','Base Obligation Date':'2026-10-01'}], 'page_metadata':{'page':len(calls),'hasNext':len(calls)==1}})
    monkeypatch.setattr(requests,'get',transport)
    monkeypatch.setattr(requests,'post',transport)
    if provider=='cftc':
        def annual(*a,**kw):
            calls.append(kw);clock[0]+=61
            return pd.DataFrame({cot_mod.COL_MARKET:[cot_mod.COMMODITY_CODES['gold']],cot_mod.COL_DATE:['2025-12-30'],cot_mod.COL_MM_LONG:[1],cot_mod.COL_MM_SHORT:[0]})
        monkeypatch.setitem(sys.modules,'cot_reports',NS(cot_year=annual))
    call={'edgar':lambda:EDGARSource().search_filings('10-K'), 'usaspending':lambda:USASpendingSource().search_contracts(date_from='2026-10-01',date_to='2026-10-09'),'cftc':lambda:cot_mod.CFTCSource()._fetch_raw_report()}[provider]
    with pytest.raises(SourceFetchError) as exc:
        call()
    assert exc.value.reason_code=='timeout'
    assert len(calls)==1


def test_usda_current_asof_filters_future_weeks_and_keeps_acquisition_clock(monkeypatch):
    import tradingagents.strategies.data_sources.usda_source as mod
    monkeypatch.setattr(mod,'current_session_date',lambda:'2026-09-14')
    rows=[{'state_alpha':'IA','week_ending':week,'unit_desc':unit,'Value':value} for week in ('2026-09-13','2026-09-20') for unit,value in [('PCT GOOD','50'),('PCT EXCELLENT','10')]]
    monkeypatch.setattr(mod,'provider_request',lambda *a,**kw:response({'data':rows}))
    out=mod.USDASource(api_key='x').fetch_crop_progress('CORN',2026,'IA',as_of='2026-09-14')
    assert len(out)==1 and out[0]['week_ending']=='2026-09-13'
    assert out[0]['available_at']==out[0]['acquired_at']


def test_court_requested_asof_cutoff_reaches_transport_and_scope(monkeypatch):
    from tradingagents.strategies.data_sources.courtlistener_source import CourtListenerSource
    captured=[]
    monkeypatch.setattr('tradingagents.strategies.data_sources.courtlistener_source.provider_request',lambda *a,**kw: captured.append(kw['params']) or response({'results':[],'count':0,'next':None}))
    out=CourtListenerSource(token='x').search_dockets('securities',date_filed_after='2026-09-01',date_filed_before='2026-10-06')
    assert captured[0]['filed_before']=='2026-10-06'
    assert out.coverage['date_filed_before']=='2026-10-06'


@pytest.mark.parametrize('provider',['edgar','usaspending'])
def test_exhaustive_window_cannot_infer_completeness_without_provider_metadata(provider,monkeypatch):
    from tradingagents.strategies.data_sources.edgar_source import EDGARSource
    from tradingagents.strategies.data_sources.usaspending_source import USASpendingSource
    monkeypatch.setattr(f'tradingagents.strategies.data_sources.{provider}_source.provider_request',lambda *a,**kw:response({'hits':{'hits':[]},'results':[]}))
    with pytest.raises(SourceFetchError):
        EDGARSource().search_filings('10-K') if provider=='edgar' else USASpendingSource().search_contracts()


def test_drought_keeps_observation_period_separate_from_acquisition(monkeypatch):
    import tradingagents.strategies.data_sources.drought_monitor_source as mod
    monkeypatch.setattr(mod,'current_session_date',lambda:'2026-06-04')
    monkeypatch.setattr(mod,'provider_request',lambda *a,**kw:response([{'MapDate':'20260602','StateAbbreviation':'IA','StatisticFormatID':2,'None':100,'D0':0,'D1':0,'D2':0,'D3':0,'D4':0}]))
    row=mod.DroughtMonitorSource().fetch_drought_severity(['IA'],'2026-06-01','2026-06-04')['IA']
    assert row['observation_date']=='2026-06-02'
    assert row['available_at']==row['acquired_at'] and row['available_at'] > row['observation_date']


def test_cftc_invalid_date_is_not_silently_filtered_out_of_annual_report(monkeypatch):
    import tradingagents.strategies.data_sources.cftc_source as mod
    frame=pd.DataFrame({mod.COL_MARKET:[mod.COMMODITY_CODES['gold']],mod.COL_DATE:['not-a-date'],mod.COL_MM_LONG:[10],mod.COL_MM_SHORT:[0]})
    monkeypatch.setitem(sys.modules,'cot_reports',NS(cot_year=lambda *a,**kw:frame))
    source=mod.CFTCSource()
    with pytest.raises(SourceFetchError):
        source._fetch_raw_report()
    assert not source._cache


@pytest.mark.parametrize('provider',['courtlistener','regulations','congress'])
def test_native_sample_scope_survives_freeze_and_reaches_strategy_health(provider,monkeypatch,tmp_path):
    from tradingagents.strategies.data_sources.courtlistener_source import CourtListenerSource
    from tradingagents.strategies.data_sources.regulations_source import RegulationsSource
    from tradingagents.strategies.data_sources.congress_source import CongressSource
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    from tradingagents.strategies.modules.litigation import LitigationStrategy
    from tradingagents.strategies.modules.regulatory_pipeline import RegulatoryPipelineStrategy
    from tradingagents.strategies.modules.congressional_trades import CongressionalTradesStrategy
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    payloads={'courtlistener':{'results':[],'count':200,'next':'https://example/next'},'regulations':{'data':[],'meta':{'totalPages':2,'totalElements':200}},'congress':[]}
    monkeypatch.setattr(f'tradingagents.strategies.data_sources.{provider}_source.provider_request',lambda *a, **kw:response(payloads[provider]))
    registry=DataSourceRegistry()
    registry.register({'courtlistener':CourtListenerSource(token='x'),'regulations':RegulationsSource(api_key='x'),'congress':CongressSource(fmp_api_key='x')}[provider])
    strategy={'courtlistener':LitigationStrategy(),'regulations':RegulatoryPipelineStrategy(),'congress':CongressionalTradesStrategy()}[provider]
    engine=MultiStrategyEngine({'autoresearch':{'state_dir':str(tmp_path)}},registry=registry,strategies=[strategy],use_llm=False)
    acquired=engine._fetch_all_data('2026-09-01','2026-10-06')
    frozen=SourceInputStore.decode(SourceInputStore.encode(acquired))
    _,_,health=engine.screen_and_enrich('2026-10-06',frozen,epoch_id='audit',policy_id='30d')
    assert health[0].evidence['source_coverage'][provider] == acquired[provider]['coverage']
    assert health[0].evidence['source_coverage'][provider]


@pytest.mark.parametrize('lag,usable',[(2,True),(7,True),(8,False)])
def test_noaa_latest_complete_contiguous_window_has_bounded_freshness(lag,usable,monkeypatch):
    import tradingagents.strategies.data_sources.noaa_source as mod
    monkeypatch.setattr(mod,'current_session_date',lambda:'2026-06-12')
    monkeypatch.setattr(mod,'AG_STATES',{'IA':'FIPS:19','IL':'FIPS:17'})
    ending=pd.Timestamp('2026-06-12')-pd.Timedelta(days=lag)
    days=pd.date_range(ending-pd.Timedelta(days=2),ending)
    rows=[{'date':str(day.date())+'T00:00:00','datatype':dtype,'station':'S1','value':value,'attributes':',,,'} for day in days for dtype,value in [('TMAX',100),('TMIN',55),('PRCP',.1)]]
    monkeypatch.setattr(mod.NOAASource,'fetch_region_daily',lambda self,start,end:{state:rows for state in mod.AG_STATES})
    source=mod.NOAASource(token='x')
    if not usable:
        with pytest.raises(SourceFetchError) as exc:
            source.fetch_ag_weather_summary('2026-06-12',3)
        assert exc.value.partial_data['heat_stress_days'] is None
        assert exc.value.partial_data['coverage']['complete'] is False
        return
    out=source.fetch_ag_weather_summary('2026-06-12',3)
    assert out['heat_stress_days']==3
    assert out['observation_date']==str(ending.date())
    assert out['start_date']==str(days[0].date()) and out['end_date']==str(ending.date())
    assert out['as_of']=='2026-06-12' and out['observation_lag_days']==lag
    assert out['coverage']['complete'] is True


def test_environment_dispatch_defaults_use_new_york_asof(monkeypatch):
    import tradingagents.strategies.data_sources.noaa_source as mod
    monkeypatch.setattr(mod,'current_session_date',lambda:'2026-06-12')
    source=mod.NOAASource(token='x')
    monkeypatch.setattr(source,'fetch_ag_weather_summary',lambda date,lookback:{'as_of':date})
    assert source.fetch({})['as_of']=='2026-06-12'


def test_native_form4_forty_filing_sample_has_explicit_scope(monkeypatch, tmp_path):
    from tradingagents.strategies.data_sources.edgar_source import EDGARSource
    dates=['2026-10-01']*41
    submissions={'filings':{'recent':{'form':['4']*41,'filingDate':dates,'accessionNumber':[f'0001-26-{i:06}' for i in range(41)],'primaryDocument':['form4.xml']*41},'files':[{'name':'CIK0000000001-submissions-001.json','filingFrom':'2025-01-01','filingTo':'2026-09-30'}]}}
    xml='<ownershipDocument><reportingOwner><reportingOwnerId><rptOwnerCik>123</rptOwnerCik><rptOwnerName>Officer</rptOwnerName></reportingOwnerId></reportingOwner><nonDerivativeTransaction><transactionCoding><transactionCode>P</transactionCode></transactionCoding><transactionAmounts><transactionShares><value>100</value></transactionShares><transactionPricePerShare><value>20</value></transactionPricePerShare><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction></ownershipDocument>'
    def transport(*a,**kw):
        url=a[2]
        if 'search-index' in url:
            return response({'hits':{'hits':[],'total':{'value':0,'relation':'eq'}}})
        if url.endswith('company_tickers.json'):
            return response({'0':{'cik_str':1,'ticker':'AAPL','title':'Example'}})
        if 'submissions' in url:
            return response(submissions)
        return response(text=xml)
    monkeypatch.setattr('tradingagents.strategies.data_sources.edgar_source.provider_request',transport)
    rows=EDGARSource().get_recent_form4('AAPL',days_back=14,as_of='2026-10-06')
    assert len(rows)==40
    assert rows.coverage['mode']=='bounded_sample' and rows.coverage['complete'] is False
    assert rows.coverage['source_total']==41 and rows.coverage['has_more'] is True
    assert rows.coverage['archived_possible'] is True
    assert rows.coverage['limit']==40 and rows.coverage['date_to']=='2026-10-06'

    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    from tradingagents.strategies.modules.insider_activity import InsiderActivityStrategy
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    registry=DataSourceRegistry()
    registry.register(EDGARSource())
    engine=MultiStrategyEngine({'autoresearch':{'state_dir':str(tmp_path)}},registry=registry,strategies=[InsiderActivityStrategy()],use_llm=False)
    acquired={'edgar':engine._fetch_edgar_events('2026-10-06')}
    frozen=SourceInputStore.decode(SourceInputStore.encode(acquired))
    assert len(frozen['edgar']['form4']['AAPL'])==40
    _,_,health=engine.screen_and_enrich('2026-10-06',frozen,epoch_id='audit',policy_id='30d')
    scope=health[0].evidence['source_coverage']['edgar']['form4']
    assert scope['mode']=='bounded_sample' and scope['complete'] is False
    assert scope['issuers']['AAPL']==rows.coverage
    assert scope['issuers']['AAPL']['source_total']==41


@pytest.mark.parametrize('outer,expected', [(None,190),(400,190),(150,150)])
@pytest.mark.parametrize('entry', ['summary', 'bulk'])
def test_noaa_native_regional_budget_caps_outer_deadline(outer, expected, entry, monkeypatch):
    """A pipeline's larger budget cannot expand NOAA's declared 90 seconds."""
    import contextlib
    import tradingagents.strategies.data_sources.noaa_source as mod
    from tradingagents.strategies.data_sources.request_policy import provider_budget, current_provider_deadline
    monkeypatch.setattr(mod.time, 'monotonic', lambda:100)
    monkeypatch.setattr(mod, 'current_session_date', lambda:'2026-06-12')
    seen=[]
    def capture(*args, **kwargs):
        seen.append(current_provider_deadline('noaa'))
        raise SourceFetchError('bounded fixture stop',reason_code='timeout')
    source=mod.NOAASource(token='x')
    if entry == 'summary':
        monkeypatch.setattr(source,'fetch_region_daily',capture)
    else:
        monkeypatch.setattr(mod,'provider_request',capture)
    context=provider_budget('noaa',outer) if outer is not None else contextlib.nullcontext()
    with context, pytest.raises(SourceFetchError):
        if entry == 'summary':
            source.fetch_ag_weather_summary('2026-06-12',1)
        else:
            source.fetch_region_daily('2026-06-12','2026-06-12')
    assert seen == [expected]


def test_noaa_public_summary_available_without_cdo_token(monkeypatch):
    import tradingagents.strategies.data_sources.noaa_source as mod
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    monkeypatch.delenv('NOAA_CDO_TOKEN',raising=False)
    monkeypatch.setattr(mod,'current_session_date',lambda:'2026-06-12')
    source=mod.NOAASource()
    registry=DataSourceRegistry()
    registry.register(source)
    assert 'noaa' in registry.available_sources()
    assert source.requires_api_key is False
    rows=[{'date':'2026-06-12','datatype':dtype,'station':'S1','value':value}
          for dtype,value in [('TMAX',85),('TMIN',55),('PRCP',.12)]]
    monkeypatch.setattr(source,'fetch_region_daily',lambda start,end:{state:rows for state in mod.AG_STATES})
    assert source.fetch_ag_weather_summary('2026-06-12',1)['coverage']['complete'] is True
    with pytest.raises(SourceFetchError,match='access missing'):
        source.fetch_state_daily('FIPS:19','2026-06-12','2026-06-12')
