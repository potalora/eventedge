from copy import deepcopy
from datetime import date, datetime
import json

import pytest

from test_state_economics_policy import config, history, manifest, snapshot, universe, NOW


def strategy(settings=None):
    from tradingagents.strategies.modules.state_economics import StateEconomicsStrategy
    result=StateEconomicsStrategy(); result.configure(settings or config())
    return result


def data(registry=None):
    u,companies=universe()
    from tradingagents.strategies.data_sources.state_economics_policy import digest
    return {'fred':{'state_unemployment':snapshot(),'state_exposure_registry':registry or config()['state_exposure_manifest'],
        'state_policy_settings':{'policy':'state_unemployment_v1','release_lookback_days':7}},
        'edgar':{'company_tickers':companies,'company_tickers_available_at':NOW,'company_tickers_sha256':digest(companies)},'_state_economics_universe':u}


def test_state_screen_retains_all_events_without_momentum_or_invented_issuers():
    s=strategy(); rows=s.screen(data(),'2026-10-09',s.get_default_params())
    assert len(rows)==51
    assert all(c.ticker=='' and c.journal_only and c.direction=='neutral' for c in rows)
    assert {c.metadata['non_actionable_reason'] for c in rows}=={'no_verified_state_exposure'}
    assert all(not c.metadata['needs_llm_analysis'] for c in rows)


def test_only_exact_reviewed_exposure_enters_required_state_analysis():
    registry=manifest(); s=strategy(config(registry)); rows=s.screen(data(registry),'2026-10-09',s.get_default_params())
    target=[c for c in rows if c.ticker]
    assert len(target)==1 and target[0].ticker=='CFR'
    assert target[0].metadata['needs_llm_analysis'] is True
    assert target[0].metadata['analysis_type']=='state_economics'
    assert target[0].direction=='neutral' and target[0].score==0
    assert not target[0].journal_only


def test_incomplete_state_prefix_retained_without_models_or_trades():
    registry=manifest(); s=strategy(config(registry)); value=data(registry)
    state=value['fred']['state_unemployment']; state['states'].pop('AL')
    state['coverage']={'expected_states':51,'accepted_states':50,'failed_states':{'AL':'timeout'},'complete':False}
    rows=s.screen(value,'2026-10-09',s.get_default_params())
    assert len(rows)==50 and all(c.journal_only for c in rows)
    assert all(not c.metadata['needs_llm_analysis'] for c in rows)


def test_new_state_identity_uses_native_vintage_and_forward_availability():
    from tradingagents.strategies.orchestration.event_identity import canonical_event_key,canonical_observation_time
    registry=manifest(); s=strategy(config(registry)); c=next(c for c in s.screen(data(registry),'2026-10-09',{}) if c.ticker)
    first=canonical_event_key(s.name,c.ticker,c.metadata,date(2026,10,9))
    c.metadata['available_at']='2026-10-11T17:00:00+00:00'
    assert canonical_event_key(s.name,c.ticker,c.metadata,date(2026,10,12))==first
    assert canonical_observation_time(s.name,c.metadata)==datetime.fromisoformat(c.metadata['available_at'])
    changed=deepcopy(c.metadata); changed['state_event']['vintage_date']='2026-10-10'
    changed['state_event']['source_observation_id']=changed['state_event']['source_observation_id'].replace('2026-10-09','2026-10-10')
    assert canonical_event_key(s.name,c.ticker,changed,date(2026,10,9))!=first


def engine(tmp_path,registry=None):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies.modules.state_economics import StateEconomicsStrategy
    settings=config(registry);settings['state_dir']=str(tmp_path)
    return MultiStrategyEngine({'autoresearch':settings},strategies=[StateEconomicsStrategy()])


def test_engine_fred_acquires_51_once_and_does_not_poison_macro_with_state_error(tmp_path):
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    import pandas as pd
    calls=[]
    class Source:
        name='fred'
        def is_available(self): return True
        def fetch_credit_spreads(self,*a): return {'BAMLH0A0HYM2':pd.Series([3.0])}
        def fetch_economic_indicators(self,*a): return {'UNRATE':pd.Series([4.0])}
        def fetch_observation_history(self,sid,start,end,*,as_of):
            calls.append(sid)
            if sid=='ALUR': raise TimeoutError()
            return history(sid)
    e=engine(tmp_path);e.registry.register(Source())
    result=e._fetch_fred_data('2026-07-01','2026-10-09',state_vintage='2026-10-11')
    assert len(calls)==51 and result['UNRATE'].iloc[0]==4.0 and not result.get('error')
    assert result['state_unemployment']['coverage']['complete'] is False
    assert result['state_unemployment']['coverage']['accepted_states']==50


def test_engine_no_match_events_are_durable_health_without_analysis_failure(tmp_path):
    e=engine(tmp_path); value=data(); value['equity_universe']={'snapshot':value['_state_economics_universe'].evidence}
    signals,regime,health=e.screen_and_enrich('2026-10-09',value,epoch_id='epoch',policy_id='policy')
    assert signals==[] and health[0].status=='signals'
    assert len(health[0].evidence['state_economic_events'])==51
    assert health[0].evidence['state_source_coverage']['complete'] is True
    assert 'analysis' not in health[0].evidence.get('provider_errors',{})


def test_required_state_analysis_neutral_validates_and_malformed_or_unavailable_fails(tmp_path):
    registry=manifest();e=engine(tmp_path,registry); s=strategy(config(registry))
    candidates=s.screen(data(registry),'2026-10-09',{})
    target=next(c for c in candidates if c.ticker)
    class Analyzer:
        last_call_provenance={}
        def analyze_state_economics(self,**kwargs):
            return {'contract_version':'state-assessment-v1','direction':'neutral','conviction':.2,
                'rationale':'State release alone supplies no directional thesis.',
                'evidence_claim':'The registry reports Texas lending exposure.',
                'event_id':kwargs['state_event']['source_observation_id'],'target_ticker':'CFR',
                'exposure_quote':kwargs['state_exposure']['geography_quote']}
    e._analyzer=Analyzer()
    result=e._enrich_with_llm([target],'state_economics')[0]
    assert result.metadata['analysis_status']=='validated' and result.direction=='neutral'
    broken=deepcopy(target)
    class Wrong(Analyzer):
        def analyze_state_economics(self,**kwargs):
            response=super().analyze_state_economics(**kwargs);response['target_ticker']='CFR-PB';return response
    e._analyzer=Wrong()
    assert e._enrich_with_llm([broken],'state_economics')[0].journal_only


def test_state_analyzer_sends_only_retained_event_and_exposure(monkeypatch):
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    a=LLMAnalyzer(); seen=[]
    monkeypatch.setattr(a,'_call_llm',lambda system,user,**kwargs: seen.append((system,json.loads(user))) or '{}')
    event={'source_observation_id':'fred:TXUR:2026-09-01:2026-10-09'}
    exposure={'geography_quote':'Most lending activity is in Texas.'}
    a.analyze_state_economics(ticker='CFR',state_event=event,state_exposure=exposure)
    assert seen[0][1]['state_event']==event and seen[0][1]['state_exposure']==exposure
    assert 'surprise' in seen[0][0].lower()


def test_state_empty_event_window_still_rejects_broken_registry(tmp_path):
    registry=manifest(); e=engine(tmp_path,registry); value=data(registry)
    for h in value['fred']['state_unemployment']['states'].values():
        h['observations'][-1]['realtime_start']='2026-09-18'
    value['fred']['state_exposure_registry']['exposures'][0]['geography_start']+=1
    value['equity_universe']={'snapshot':value['_state_economics_universe'].evidence}
    _,_,health=e.screen_and_enrich('2026-10-09',value,epoch_id='epoch',policy_id='policy')
    assert health[0].status=='data_failure'


def test_frozen_state_replay_checks_health_without_source_or_model_calls(tmp_path):
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    from tradingagents.strategies.orchestration.state_inputs import validate_state_health
    e=engine(tmp_path); value=data();value.pop('_state_economics_universe')
    value['equity_universe']={'snapshot':universe()[0].evidence}
    identity={'generation':'state_fixture','session':'2026-10-09','commit':'abc','configuration':'cfg'}
    store=SourceInputStore(tmp_path/'cache',accepted_dir=tmp_path/'accepted')
    store.freeze(identity,value,acquired_at=datetime.fromisoformat(NOW))
    frozen=store.load_frozen(identity)
    _,_,health=e.screen_and_enrich('2026-10-09',frozen,epoch_id='epoch',policy_id='policy')
    validate_state_health(frozen,e.ar_config,'2026-10-09',health,{'policy'})
    changed=deepcopy(health);changed[0].evidence['state_economic_events'].pop()
    with pytest.raises(ValueError):validate_state_health(frozen,e.ar_config,'2026-10-09',changed,{'policy'})


def test_fresh_historical_reference_cannot_create_state_candidate():
    with pytest.raises(ValueError,match='latest completed'):
        strategy().screen(data(),'2026-10-08',{})


@pytest.mark.parametrize('mutation',['expired','raw_hash'])
def test_no_recent_events_cannot_hide_expired_or_forged_registry(tmp_path,mutation):
    registry=manifest()
    if mutation=='expired': registry['exposures'][0]['valid_until']='2026-10-10T00:00:00+00:00'
    else: next(iter(registry['documents'].values()))['raw_sha256']='0'*64
    e=engine(tmp_path,registry); value=data(registry)
    for h in value['fred']['state_unemployment']['states'].values():h['observations'][-1]['realtime_start']='2026-09-18'
    value['equity_universe']={'snapshot':value['_state_economics_universe'].evidence}
    _,_,health=e.screen_and_enrich('2026-10-09',value,epoch_id='epoch',policy_id='policy')
    assert health[0].status=='data_failure' and health[0].evidence['state_economic_events']==[]
    assert health[0].evidence['state_source_scope']['exposure_registry_error']=='invalid_state_exposure_registry'


def test_state_source_codec_failure_retains_macro_and_explicit_failed_coverage(tmp_path,monkeypatch):
    import pandas as pd
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore,SourceInputError
    class Source:
        name='fred'
        def is_available(self): return True
        def fetch_credit_spreads(self,*args):return {}
        def fetch_economic_indicators(self,*args):return {'UNRATE':pd.Series([4.0])}
        def fetch_observation_history(self,sid,*args,**kwargs):return history(sid)
    e=engine(tmp_path);e.registry.register(Source())
    encode=SourceInputStore.encode
    def bounded(payload,**kwargs):
        if len(payload.get('state_unemployment',{}).get('states',{}))==51:raise SourceInputError('source document exceeds byte limit')
        return encode(payload,**kwargs)
    monkeypatch.setattr(SourceInputStore,'encode',bounded)
    result=e._fetch_fred_data('2026-07-01','2026-10-09',state_vintage='2026-10-11')
    assert result['UNRATE'].iloc[0]==4.0 and not result.get('error')
    assert result['state_evidence_error']=='state_source_codec_limit'
    assert result['state_unemployment']['coverage']['complete'] is False
    assert len(result['state_unemployment']['coverage']['failed_states'])==51


def test_exposure_expiry_is_rechecked_at_later_full_decision_cutoff():
    from tradingagents.strategies.orchestration.state_inputs import state_policy_scope
    registry=manifest();registry['exposures'][0]['valid_until']='2026-10-11T17:00:00+00:00'
    value=data(registry);value['equity_universe']={'snapshot':value['_state_economics_universe'].evidence}
    assert state_policy_scope(value,config(registry),'2026-10-09')['exposure_registry_error'] is None
    value['_decision_context']={'cutoff':'2026-10-11T17:01:00+00:00'}
    assert state_policy_scope(value,config(registry),'2026-10-09')['exposure_registry_error']=='invalid_state_exposure_registry'
    value['_decision_context']['cutoff']='2026-10-11T15:59:00+00:00'
    with pytest.raises(ValueError,match='after_decision_cutoff'):state_policy_scope(value,config(registry),'2026-10-09')


def test_incomplete_state_source_never_enters_success_cache(tmp_path):
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore,cache_identity
    value=data()['fred'];state=value['state_unemployment'];state['states'].pop('AL')
    state['coverage']={'expected_states':51,'accepted_states':50,'failed_states':{'AL':'timeout'},'complete':False}
    value['coverage']={'state_unemployment':state['coverage']}
    store=SourceInputStore(tmp_path/'cache')
    assert store.save_cached(cache_identity('fred','2026-07-01','2026-10-09','cfg'),value) is False


def test_fred_official_pacing_consumes_shared_deadline_before_121st_call():
    from tradingagents.strategies.data_sources import request_policy as p
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    from types import SimpleNamespace
    class Clock:
        now=0
        def __call__(self):return self.now
        def sleep(self,seconds):self.now+=seconds
    clock=Clock();calls=[]
    with p._LOCK:
        for key in list(p._HISTORY):
            if key[0]=='fred':p._HISTORY.pop(key)
    try:
        with p.provider_budget('fred',59,clock=clock,sleep=clock.sleep):
            for i in range(120):
                p.provider_request('fred','GET','https://offline.test',transport=lambda *a,**kw:calls.append(1) or SimpleNamespace(status_code=200))
            with pytest.raises(SourceFetchError):p.provider_request('fred','GET','https://offline.test',transport=lambda *a,**kw:calls.append(1))
        assert len(calls)==120 and p.PROVIDER_LIMITS['fred']==((120,60),)
    finally:
        with p._LOCK:
            for key in list(p._HISTORY):
                if key[0]=='fred':p._HISTORY.pop(key)


def test_completed_state_health_cannot_relabel_events_as_no_event(tmp_path):
    from tradingagents.strategies.orchestration.state_inputs import validate_state_health
    e=engine(tmp_path);value=data();value['equity_universe']={'snapshot':value['_state_economics_universe'].evidence}
    _,_,health=e.screen_and_enrich('2026-10-09',value,epoch_id='epoch',policy_id='policy')
    from dataclasses import replace
    health[0]=replace(health[0],status='legitimate_no_event')
    with pytest.raises(ValueError):validate_state_health(value,e.ar_config,'2026-10-09',health,{'policy'})


def test_exact_security_map_availability_is_required_and_delays_state_event():
    from tradingagents.strategies.orchestration.state_inputs import state_policy_scope
    registry=manifest();value=data(registry);value['equity_universe']={'snapshot':value['_state_economics_universe'].evidence}
    value['edgar'].pop('company_tickers_available_at')
    assert state_policy_scope(value,config(registry),'2026-10-09')['exposure_registry_error']=='invalid_state_exposure_registry'
    value=data(registry);value['edgar']['company_tickers_available_at']='2026-10-11T17:00:00+00:00'
    candidate=next(c for c in strategy(config(registry)).screen(value,'2026-10-09',{}) if c.ticker)
    assert candidate.metadata['available_at']=='2026-10-11T17:00:00+00:00'


def test_global_model_deadline_holds_state_journal_evidence_and_replays(tmp_path):
    from tradingagents.strategies.orchestration.multi_strategy_engine import hold_incomplete_model_sample
    from tradingagents.strategies.orchestration.state_inputs import validate_state_health
    e=engine(tmp_path);value=data();value['equity_universe']={'snapshot':value['_state_economics_universe'].evidence}
    signals,_,health=e.screen_and_enrich('2026-10-09',value,epoch_id='epoch',policy_id='policy')
    _,health=hold_incomplete_model_sample(signals,health,incomplete=True)
    assert all(row['analysis_failure_reason']=='model_deadline_exhausted' and row['journal_only']
        for row in health[0].evidence['state_economic_events'])
    validate_state_health(value,e.ar_config,'2026-10-09',health,{'policy'})
