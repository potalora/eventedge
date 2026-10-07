"""Consumed records and bounded collection completeness are source contracts."""
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget


@pytest.fixture(autouse=True)
def offline_policy(monkeypatch):
    monkeypatch.setattr('tradingagents.strategies.data_sources.request_policy.PROVIDER_LIMITS', {})
    monkeypatch.setattr('time.sleep', lambda _: None)


def response(payload, status=200):
    return SimpleNamespace(status_code=status, headers={}, json=lambda: payload)


def trade(symbol='AAPL', transaction='2026-10-01', disclosure='2026-10-02'):
    return {'symbol': symbol, 'transactionDate': transaction, 'disclosureDate': disclosure,
            'office': 'Jane Doe', 'type': 'Purchase', 'amount': '$1,001 - $15,000'}


def test_required_iso_date_rejects_trailing_garbage():
    from tradingagents.strategies.data_sources.fetch_errors import source_date
    assert not source_date('2026-10-01garbage')
    assert source_date('2026-10-01T00:00:00Z')


def test_congress_valid_iso_datetime_is_filtered_by_calendar_day(monkeypatch):
    from tradingagents.strategies.data_sources.congress_source import CongressSource
    monkeypatch.setattr(requests, 'get', lambda url, **kw: response([trade(transaction='2026-10-01T00:00:00Z')]) if 'house-latest' in url else response([]))
    assert len(CongressSource(fmp_api_key='offline').get_recent_trades(30, '2026-10-06')) == 1


@pytest.mark.parametrize('provider', ['congress', 'noaa', 'drought_monitor', 'usda', 'regulations'])
@pytest.mark.parametrize('malformed', [True, False])
def test_consumed_records_reach_engine_as_failure_but_empty_is_valid(provider, malformed, tmp_path, monkeypatch):
    from tradingagents.strategies.data_sources.congress_source import CongressSource
    from tradingagents.strategies.data_sources.noaa_source import NOAASource
    from tradingagents.strategies.data_sources.drought_monitor_source import DroughtMonitorSource
    from tradingagents.strategies.data_sources.usda_source import USDASource
    from tradingagents.strategies.data_sources.regulations_source import RegulationsSource
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    rows = [{}] if malformed else []
    payloads = {'congress': rows, 'noaa': {'results': rows, 'metadata': {'resultset': {'count': len(rows)}}},
                'drought_monitor': rows, 'usda': {'data': rows},
                'regulations': {'data': [{'attributes': {}}] if malformed else []}}
    request = lambda *a, **kw: response(payloads[provider])
    monkeypatch.setattr(requests, 'get', request)
    sources = {'congress': CongressSource(fmp_api_key='offline'), 'noaa': NOAASource(token='offline'),
               'drought_monitor': DroughtMonitorSource(), 'usda': USDASource(api_key='offline'),
               'regulations': RegulationsSource(api_key='offline')}
    sources['noaa']._session = SimpleNamespace(get=request)
    registry = DataSourceRegistry(); registry.register(sources[provider])
    engine = MultiStrategyEngine({'autoresearch': {'state_dir': str(tmp_path)}}, registry=registry,
                                 strategies=[], use_llm=False)
    data = engine._fetch_all_data('2026-09-01', '2026-10-06')[provider]
    assert bool(data.get('error')) is malformed
    from tradingagents.strategies.orchestration.source_inputs import successful_source
    assert successful_source(data) is (not malformed)
    if malformed:
        assert not sources[provider]._cache


@pytest.mark.parametrize('partial', [False, True])
def test_congress_cutoff_is_identical_for_success_and_partial(partial, monkeypatch):
    from tradingagents.strategies.data_sources.congress_source import CongressSource
    rows = [trade('OLD', '2026-01-01'), trade('FUTURE', disclosure='2026-10-07'), trade('VALID')]
    monkeypatch.setattr(requests, 'get', lambda url, **kw: response(rows) if 'house-latest' in url else response([], 403 if partial else 200))
    source = CongressSource(fmp_api_key='offline')
    if partial:
        with pytest.raises(SourceFetchError) as exc:
            source.get_recent_trades(30, '2026-10-06')
        recent = exc.value.partial_data['recent_trades']
    else:
        recent = source.get_recent_trades(30, '2026-10-06')
    assert [row['ticker'] for row in recent] == ['VALID']


@pytest.mark.parametrize('operation', ['prices', 'vix'])
def test_all_nan_market_history_is_failure_and_never_cached(operation, monkeypatch):
    from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
    monkeypatch.setattr('yfinance.download', lambda *a, **kw: pd.DataFrame({'Close': [float('nan')]}))
    source = YFinanceSource()
    with pytest.raises(SourceFetchError):
        if operation == 'prices':
            source.fetch_prices(['AAPL'], '2026-10-01', '2026-10-06')
        else:
            source.fetch_vix('2026-10-01', '2026-10-06')
    assert not source._cache


def test_missing_market_symbol_preserves_other_usable_history(monkeypatch):
    from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
    frame = pd.DataFrame({('Close', 'AAPL'): [100, 101], ('Close', 'MISSING'): [float('nan'), float('nan')]})
    monkeypatch.setattr('yfinance.download', lambda *a, **kw: frame)
    source = YFinanceSource()
    with pytest.raises(SourceFetchError) as exc:
        source.fetch_prices(['AAPL', 'MISSING'], '2026-10-01', '2026-10-06')
    assert ('Close', 'AAPL') in exc.value.partial_data['prices'].columns
    assert ('Close', 'MISSING') not in exc.value.partial_data['prices'].columns
    assert exc.value.failed_operations == {'MISSING': 'invalid_response'}
    assert not source._cache


@pytest.mark.parametrize('total', [6000, 3])
def test_noaa_pagination_cap_or_inconsistent_total_is_explicit(total):
    from tradingagents.strategies.data_sources.noaa_source import NOAASource
    rows = [{'date': '2026-10-01T00:00:00', 'datatype': 'TMAX', 'station': 'TEST', 'value': 100}] * 1000
    source = NOAASource(token='offline')
    source._session = SimpleNamespace(get=lambda *a, **kw: response({'results': rows, 'metadata': {'resultset': {'count': total}}}))
    with provider_budget('noaa', 100, clock=lambda: 0, sleep=lambda _: None, limits=()):
        with pytest.raises(SourceFetchError) as exc:
            source.fetch_state_daily('FIPS:19', '2026-10-01', '2026-10-06')
    assert exc.value.reason_code == 'invalid_response'
    assert exc.value.partial_data.get('observations')
    assert not source._cache


@pytest.mark.parametrize('provider', ['finnhub_earnings', 'finnhub_news', 'courtlistener', 'usaspending', 'edgar', 'fred'])
def test_other_consumed_required_records_reject_empty_records(provider, monkeypatch):
    from tradingagents.strategies.data_sources.finnhub_source import FinnhubSource
    from tradingagents.strategies.data_sources.courtlistener_source import CourtListenerSource
    from tradingagents.strategies.data_sources.usaspending_source import USASpendingSource
    from tradingagents.strategies.data_sources.edgar_source import EDGARSource
    from tradingagents.strategies.data_sources.fred_source import FREDSource
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: response({'results': [{}], 'hits': {'hits': [{'_source': {'form': '', 'file_date': ''}}]}}))
    monkeypatch.setattr(requests, 'post', lambda *a, **kw: response({'results': [{}]}))
    if provider.startswith('finnhub'):
        source = FinnhubSource(api_key='offline', sleep_fn=lambda _: None, monotonic_fn=lambda: 0)
        source._http = SimpleNamespace(get=lambda *a, **kw: {'earningsCalendar': [{}]} if provider.endswith('earnings') else [{}])
        call = (lambda: source.fetch_recent_earnings('2026-10-01', '2026-10-06')) if provider.endswith('earnings') else (lambda: source.fetch_company_news('AAPL', '2026-10-01', '2026-10-06'))
        budget_provider = 'finnhub'
    elif provider == 'courtlistener':
        source = CourtListenerSource(token='offline'); call = lambda: source.search_dockets('test'); budget_provider = provider
    elif provider == 'usaspending':
        source = USASpendingSource(); call = lambda: source.search_contracts(); budget_provider = provider
    elif provider == 'edgar':
        source = EDGARSource(); call = lambda: source.search_filings('10-K'); budget_provider = provider
    else:
        monkeypatch.setattr('fredapi.Fred.get_series', lambda *a, **kw: pd.Series([float('nan')], index=['2026-10-01']))
        source = FREDSource(api_key='offline'); call = lambda: source.fetch_series('UNRATE', '2026-10-01', '2026-10-06'); budget_provider = provider
    with provider_budget(budget_provider, 100, clock=lambda: 0, sleep=lambda _: None, limits=()):
        with pytest.raises(SourceFetchError):
            call()
    assert not source._cache if hasattr(source, '_cache') else not source._session_cache


def test_yahoo_engine_exposes_missing_symbols_and_retains_valid_history(monkeypatch, tmp_path):
    from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    frame = pd.DataFrame({('Close', 'SPY'): [100.0, 101.0]})
    monkeypatch.setattr('yfinance.download', lambda symbol, **kw: pd.DataFrame({'Close': [20.0]}) if symbol == '^VIX' else frame)
    registry = DataSourceRegistry(); registry.register(YFinanceSource())
    engine = MultiStrategyEngine({'autoresearch': {'state_dir': str(tmp_path)}}, registry=registry, strategies=[], use_llm=False)
    payload = engine._fetch_yfinance_data('2026-10-01', '2026-10-06')
    assert payload.get('error')
    assert list(payload['prices']) == ['SPY']


@pytest.mark.parametrize('provider', ['congress', 'noaa', 'drought_monitor', 'usda', 'regulations'])
def test_valid_records_survive_invalid_sibling_without_success_cache(provider, monkeypatch):
    from tradingagents.strategies.data_sources.congress_source import CongressSource
    from tradingagents.strategies.data_sources.noaa_source import NOAASource
    from tradingagents.strategies.data_sources.drought_monitor_source import DroughtMonitorSource
    from tradingagents.strategies.data_sources.usda_source import USDASource
    from tradingagents.strategies.data_sources.regulations_source import RegulationsSource
    observation = {'date': '2026-10-01T00:00:00', 'datatype': 'TMAX', 'station': 'TEST', 'value': 100}
    drought = {'MapDate': '20261001', 'StateAbbreviation': 'IA', 'None': 50, 'D0': 25, 'D1': 25, 'D2': 0, 'D3': 0, 'D4': 0}
    crop = [{'week_ending': '2026-10-01', 'state_alpha': 'IA', 'unit_desc': unit, 'Value': value}
            for unit, value in [('PCT GOOD', '50'), ('PCT EXCELLENT', '10')]]
    document = {'id': 'EPA-1', 'attributes': {'title': 'Test rule', 'agencyId': 'EPA', 'documentType': 'Proposed Rule', 'postedDate': '2026-10-01'}}
    payloads = {'congress': [trade(), {}], 'noaa': {'results': [observation, {}], 'metadata': {'resultset': {'count': 2}}},
                'drought_monitor': [drought, {}], 'usda': {'data': crop + [{}]},
                'regulations': {'data': [document, {'attributes': {}}]}}
    request = lambda *a, **kw: response(payloads[provider])
    monkeypatch.setattr(requests, 'get', request)
    sources = {'congress': CongressSource(fmp_api_key='offline'), 'noaa': NOAASource(token='offline'),
               'drought_monitor': DroughtMonitorSource(), 'usda': USDASource(api_key='offline'),
               'regulations': RegulationsSource(api_key='offline')}
    source = sources[provider]
    sources['noaa']._session = SimpleNamespace(get=request)
    calls = {'congress': lambda: source.get_recent_trades(30, '2026-10-06'),
             'noaa': lambda: source.fetch_state_daily('FIPS:19', '2026-10-01', '2026-10-06'),
             'drought_monitor': lambda: source.fetch_drought_severity(['IA'], '2026-10-01', '2026-10-06'),
             'usda': lambda: source.fetch_crop_progress('CORN', 2026, 'IA'),
             'regulations': lambda: source.search_documents(posted_date_from='2026-10-01')}
    with provider_budget(provider, 100, clock=lambda: 0, sleep=lambda _: None, limits=()):
        with pytest.raises(SourceFetchError) as exc:
            calls[provider]()
    assert exc.value.partial_data
    assert not source._cache
    assert '{}' not in str(exc.value)


def test_cftc_invalid_numeric_observation_is_not_cached(monkeypatch):
    import sys
    from tradingagents.strategies.data_sources.cftc_source import CFTCSource, COL_MARKET, COL_DATE, COL_MM_LONG, COL_MM_SHORT
    frame = pd.DataFrame({COL_MARKET: ['GOLD'], COL_DATE: ['2026-10-01'], COL_MM_LONG: [float('inf')], COL_MM_SHORT: [0]})
    monkeypatch.setitem(sys.modules, 'cot_reports', SimpleNamespace(cot_year=lambda *a, **kw: frame))
    source = CFTCSource()
    with provider_budget('cftc', 100, clock=lambda: 0, sleep=lambda _: None, limits=()):
        with pytest.raises(SourceFetchError):
            source._fetch_raw_report()
    assert not source._cache


def test_cftc_valid_positioning_survives_invalid_report_sibling(monkeypatch):
    import sys
    from tradingagents.strategies.data_sources.cftc_source import CFTCSource, COMMODITY_CODES, COL_MARKET, COL_DATE, COL_MM_LONG, COL_MM_SHORT
    frame = pd.DataFrame({COL_MARKET: [COMMODITY_CODES['gold']] * 4 + ['BAD'],
                          COL_DATE: ['2026-09-10', '2026-09-17', '2026-09-24', '2026-10-01', '2026-10-01'],
                          COL_MM_LONG: [10, 20, 30, 40, float('inf')], COL_MM_SHORT: [0] * 5})
    monkeypatch.setitem(sys.modules, 'cot_reports', SimpleNamespace(cot_year=lambda *a, **kw: frame))
    source = CFTCSource()
    with provider_budget('cftc', 100, clock=lambda: 0, sleep=lambda _: None, limits=()):
        with pytest.raises(SourceFetchError) as exc:
            source._dispatch_cot_positioning({'commodities': ['gold']})
    assert exc.value.partial_data['gold']['net_position'] == 40
    assert exc.value.failed_operations == {'cot_report': 'invalid_response'}
    assert not source._cache
