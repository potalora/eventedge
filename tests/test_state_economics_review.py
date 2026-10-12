"""Independent-review regressions, entirely offline and source/model free."""
import base64
from copy import deepcopy
import hashlib

import pytest

from test_fred_observation_records import install_pages, row
from test_state_economics_policy import manifest, universe, NOW, history, config
from tradingagents.strategies.data_sources.fred_source import FREDSource
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources import state_economics_policy as policy


@pytest.mark.parametrize('observation',[
    row('2026-08-01','4.0','2026-10-12','2026-10-12'),
    row('2026-08-01','4.0','2026-09-01','2026-09-01'),
    row('2026-12-01','4.0','2026-10-11','2026-10-11'),
])
def test_snapshot_rejects_values_outside_observation_upper_or_vintage_bounds(monkeypatch,observation):
    install_pages(monkeypatch,[{'count':1,'offset':0,'limit':100000,'observations':[observation]}])
    source=FREDSource('offline')
    with pytest.raises(SourceFetchError,match='invalid_response'):
        source.fetch_series('UNRATE','2026-07-01','2026-10-11',as_of='2026-10-11')
    assert not source._cache


def test_snapshot_realtime_period_may_extend_past_the_selected_vintage(monkeypatch):
    install_pages(monkeypatch,[{'count':1,'offset':0,'limit':100000,
        'observations':[row('2026-08-01','4.0','2026-09-18','9999-12-31')]}])
    assert FREDSource('offline').fetch_series('UNRATE','2026-07-01','2026-10-11').iloc[0]==4.0


@pytest.mark.parametrize('series,period,accepted',[
    ('UNRATE','2025-04-01',True),('UNRATE','2025-03-01',False),
    ('TXUR','2025-04-01',True),('BAMLH0A0HYM2','2025-04-08',False),
])
def test_snapshot_lower_bound_respects_known_period_frequency(monkeypatch,series,period,accepted):
    # The real 2026-10-11 source probe returned Apr 1 for a requested Apr 9
    # lower bound on monthly UNRATE/TXUR, but daily HY began on Apr 9.
    install_pages(monkeypatch,[{'count':1,'offset':0,'limit':100000,
        'observations':[row(period,'4.2','2026-10-09','2026-10-09')]}])
    source=FREDSource('offline')
    if accepted:
        assert source.fetch_series(series,'2025-04-09','2026-10-09').iloc[0]==4.2
    else:
        with pytest.raises(SourceFetchError,match='invalid_response'):
            source.fetch_series(series,'2025-04-09','2026-10-09')


def test_state_history_requests_calendar_month_start_for_every_state():
    calls=[]
    class Source:
        def fetch_observation_history(self,sid,start,end,*,as_of):
            calls.append((sid,start));result=history(sid)
            result['observation_start']=start
            # Original/revised April rows reproduce the ALUR source response
            # that the prior midmonth-start acquisition rejected wholesale.
            result['observations'][:0]=[
                {'observation_date':'2025-04-01','value':3.3,'realtime_start':'2025-05-21','realtime_end':'2026-04-07'},
                {'observation_date':'2025-04-01','value':2.9,'realtime_start':'2026-04-08','realtime_end':as_of}]
            result['coverage']['observation_count']=len(result['observations'])
            return result
    result=policy.acquire_states(Source(),'2025-04-09','2026-10-11')
    assert len(calls)==51 and {start for _,start in calls}=={'2025-04-01'}
    assert policy.validate_snapshot(result)['complete'] is True


def duplicate_registry():
    result=manifest();old=next(iter(result['documents']));new=old[:-1]+('2' if old[-1]!='2' else '3')
    document=deepcopy(result['documents'][old])
    raw=base64.b64decode(document['raw_submission_b64']).replace(old.encode(),new.encode())
    document.update(raw_submission_b64=base64.b64encode(raw).decode(),raw_sha256=hashlib.sha256(raw).hexdigest(),
        source_url=document['source_url'].replace(old.replace('-',''),new.replace('-','')).replace(old,new))
    result['documents'][new]=document
    exposure=deepcopy(result['exposures'][0]);exposure['document_ref']=new;result['exposures'].append(exposure)
    return result


def test_two_originals_cannot_duplicate_one_state_security_event():
    u,companies=universe()
    with pytest.raises(ValueError):
        policy.verified_exposures(duplicate_registry(),universe=u,company_map=companies,cutoff=NOW)


def test_duplicate_exposure_stays_failed_journal_evidence_and_replays(tmp_path):
    from test_state_economics_integration import data,engine
    from tradingagents.strategies.orchestration.state_inputs import validate_state_health
    registry=duplicate_registry();value=data(registry)
    value['equity_universe']={'snapshot':value['_state_economics_universe'].evidence}
    e=engine(tmp_path,registry)
    class NoModel:
        def analyze_state_economics(self,**kwargs):raise AssertionError('must not analyze invalid registry')
    e._analyzer=NoModel()
    signals,_,health=e.screen_and_enrich('2026-10-09',value,epoch_id='epoch',policy_id='policy')
    assert not signals and health[0].status=='data_failure'
    assert len(health[0].evidence['state_economic_events'])==51
    assert validate_state_health(value,e.ar_config,'2026-10-09',health,{'policy'})['exposure_registry_error']


def test_month_alignment_does_not_make_a_reversed_request_valid():
    class NoSource:
        def fetch_observation_history(self,*args,**kwargs):raise AssertionError('no acquisition')
    with pytest.raises(ValueError):policy.acquire_states(NoSource(),'2026-10-12','2026-10-11')
