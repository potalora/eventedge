"""Provider-shape regressions for bounded environmental acquisition."""
from datetime import date, timedelta
from unittest.mock import Mock

import pytest

from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


def response(body=None, text=''):
    return Mock(status_code=200, json=Mock(return_value=body), text=text, headers={})


def test_usdm_real_camelcase_and_fips_preserve_categorical_score(monkeypatch):
    import tradingagents.strategies.data_sources.drought_monitor_source as mod
    monkeypatch.setattr(mod, 'current_session_date', lambda: '2026-10-09')
    def transport(*args, **kwargs):
        assert kwargs['params']['aoi'] == '19,17'
        assert kwargs['params']['statisticsType'] == 2
        return response([{'stateAbbreviation': state, 'mapDate': '2026-10-06T00:00:00',
                          'statisticFormatID': 2, 'none': 40, 'd0': 20, 'd1': 10,
                          'd2': 10, 'd3': 10, 'd4': 10} for state in ('IA', 'IL')])
    monkeypatch.setattr(mod, 'provider_request', transport)
    source = mod.DroughtMonitorSource()
    assert source.fetch_composite_score(['IA','IL'], '2026-10-09') == 1.0
    assert source.fetch_drought_severity(['IA','IL'], end='2026-10-09')['IA']['observation_date'] == '2026-10-06'


def test_cdo_quality_quarantine_preserves_raw_offset_and_later_page(monkeypatch):
    import tradingagents.strategies.data_sources.noaa_source as mod
    monkeypatch.setattr(mod, 'current_session_date', lambda: '2026-10-09')
    def transport(*args, **kwargs):
        offset = kwargs['params']['offset']
        if offset == 1:
            rows = [{'date':'2026-10-04', 'station':'A', 'datatype':'TMIN', 'value':-99, 'attributes':',S,H,0600'},
                    {'date':'2026-10-04', 'station':'B', 'datatype':'TMIN', 'value':40, 'attributes':',,H,0600'}]
        else:
            assert offset == 3
            rows = [{'date':'2026-10-04', 'station':'C', 'datatype':'TMIN', 'value':50, 'attributes':',,H,0600'}]
        return response({'results':rows,'metadata':{'resultset':{'count':3,'offset':offset,'limit':1000}}})
    monkeypatch.setattr(mod, 'provider_request', transport)
    source = mod.NOAASource(token='x')
    rows = source.fetch_state_daily('FIPS:19','2026-10-04','2026-10-09')
    assert [r['value'] for r in rows] == [40,50]
    assert source.observation_exclusions['quality_flag'] == 1


def station_line(station, state):
    return f'{station:11} {42:8.4f} {-93:9.4f} {100:6.1f} {state} TEST'


def inventory_line(station, datatype):
    return f'{station:11} {42:8.4f} {-93:9.4f} {datatype} 2000 2026'


@pytest.mark.parametrize('missing,flagged,success', [(False,False,True),(True,False,False),(False,True,False)])
def test_bulk_catalog_filters_state_and_requires_every_usable_day_type(monkeypatch, missing, flagged, success):
    import tradingagents.strategies.data_sources.noaa_source as mod
    monkeypatch.setattr(mod,'current_session_date',lambda:'2026-10-09')
    monkeypatch.setattr(mod,'AG_STATES',{'IA':'FIPS:19','IL':'FIPS:17'})
    stations = [('USW00014933','IA'),('USW00094846','IL'),('USW00099999','TX')]
    def transport(*args,**kwargs):
        url=args[2]
        if url.endswith('ghcnd-stations.txt'):
            return response(text='\n'.join(station_line(*x) for x in stations))
        if url.endswith('ghcnd-inventory.txt'):
            return response(text='\n'.join(inventory_line(s,d) for s,_ in stations for d in ('TMAX','TMIN','PRCP')))
        assert kwargs['params']['stations'] == 'USW00014933,USW00094846'
        assert kwargs['params']['units'] == 'standard'
        rows=[]
        for station,_ in stations[:2]:
            for day in ('2026-10-03','2026-10-04'):
                r={'DATE':day,'STATION':station,'TMAX':'100','TMIN':'50','PRCP':'0.10',
                   'TMAX_ATTRIBUTES':',,1','TMIN_ATTRIBUTES':',,1','PRCP_ATTRIBUTES':',,1,2400'}
                if station=='USW00094846' and day=='2026-10-04':
                    if missing:r.pop('PRCP')
                    if flagged:r['PRCP_ATTRIBUTES']=',S,1,2400'
                rows.append(r)
        return response(rows)
    monkeypatch.setattr(mod,'provider_request',transport)
    source=mod.NOAASource(token='x')
    if success:
        out=source.fetch_ag_weather_summary('2026-10-09',2)
        assert out['observation_date']=='2026-10-04' and out['heat_stress_days']==2
        assert out['coverage']['catalog_station_count']==2
        assert out['coverage']['acquisition_path']=='ncei_daily_summaries_bulk'
    else:
        with pytest.raises(SourceFetchError) as exc:source.fetch_ag_weather_summary('2026-10-09',2)
        assert exc.value.partial_data['coverage']['complete'] is False
        assert exc.value.partial_data['heat_stress_days'] is None
        if flagged:assert exc.value.partial_data['coverage']['exclusions']['quality_flag']==1


def test_usda_default_wheat_uses_native_class_universes_and_inactive_season(monkeypatch):
    import tradingagents.strategies.data_sources.usda_source as mod
    monkeypatch.setattr(mod,'current_session_date',lambda:'2026-10-09')
    classes={'WINTER':['IL','IN','KS','MO','ND','NE','OH','SD'],
             'SPRING, (EXCL DURUM)':['MN','ND','SD'], 'SPRING, DURUM':['ND']}
    def transport(*args,**kwargs):
        assert 'IA' not in kwargs['params']['state_alpha']
        return response({'data':[{'week_ending':day,'state_alpha':state,'class_desc':cl,'year':'2026',
              'unit_desc':unit,'Value':value} for cl,states in classes.items() for state in states
              for day in ('2026-06-14','2026-06-21') for unit,value in [('PCT GOOD','50'),('PCT EXCELLENT','20')]]})
    monkeypatch.setattr(mod,'provider_request',transport)
    out=mod.USDASource(api_key='x').fetch_crop_progress('WHEAT',2026)
    assert out.coverage['complete'] is True
    assert out.coverage['active_classes']==[]
    assert all(row['survey_active'] is False for row in out)
    from tradingagents.strategies.modules.weather_ag import WeatherAgStrategy
    out[-1]['good_pct']=0
    assert WeatherAgStrategy._check_crop_decline({'crop_progress':{'WHEAT':out}})==0


def test_usda_active_class_requires_latest_state_and_class_coverage(monkeypatch):
    import tradingagents.strategies.data_sources.usda_source as mod
    monkeypatch.setattr(mod,'current_session_date',lambda:'2026-06-24')
    records=[{'week_ending':day,'state_alpha':state,'class_desc':'ALL CLASSES','year':'2026',
              'unit_desc':unit,'Value':value} for state in ('IA','IL')
             for day in (('2026-06-21','2026-06-14') if state=='IA' else ('2026-06-07',))
             for unit,value in [('PCT GOOD','50'),('PCT EXCELLENT','20')]]
    monkeypatch.setattr(mod,'provider_request',lambda *a,**kw:response({'data':records}))
    with pytest.raises(SourceFetchError) as exc:mod.USDASource(api_key='x').fetch_crop_progress('CORN',2026,'IA,IL')
    assert exc.value.partial_data['coverage']['classes']['ALL CLASSES']['stale_states']==['IL']


def test_fall_wheat_requires_only_active_winter_harvest_year(monkeypatch):
    import tradingagents.strategies.data_sources.usda_source as mod
    monkeypatch.setattr(mod,'current_session_date',lambda:'2026-11-20')
    scope=mod.condition_scope('WHEAT','2026-11-20')
    assert scope['reporting_year']==2027
    assert scope['active_classes']==['WINTER']
    rows=[{'week_ending':'2026-11-15','state_alpha':state,'class_desc':'WINTER','year':'2027',
           'unit_desc':unit,'Value':value} for state in ('IL','IN','KS','MO','ND','NE','OH','SD')
          for unit,value in [('PCT GOOD','50'),('PCT EXCELLENT','20')]]
    monkeypatch.setattr(mod,'provider_request',lambda *a,**kw:response({'data':rows}))
    result=mod.USDASource(api_key='x').fetch_crop_progress('WHEAT',scope['reporting_year'])
    assert result.coverage['complete'] is True
    assert all(row['survey_active'] for row in result)
    assert result.coverage['classes']['SPRING, DURUM']['active'] is False


def test_weather_admission_retains_momentum_priority_and_all_discoveries():
    import pandas as pd
    from tradingagents.strategies.modules.weather_ag import WeatherAgStrategy
    index=pd.date_range('2026-09-01',periods=25)
    prices={ticker:pd.DataFrame({'Close':[100+i*growth for i in range(25)]},index=index)
            for ticker,growth in [('DBA',1),('CORN',2),('SOYB',3),('WEAT',4)]}
    result=WeatherAgStrategy().screen({'yfinance':{'prices':prices},'drought_monitor':{'composite_score':2}},
                                    '2026-09-25',{'lookback_days':21})
    assert [row.ticker for row in result]==['WEAT','SOYB','CORN']
    assert len(result.admission_manifest['discovered'])==4
    assert [row['ticker'] for row in result.admission_manifest['excluded']]==['DBA']
