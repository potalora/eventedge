"""Prospective SIP universe is bound to complete asset evidence, never bar failures."""
from datetime import datetime, timezone
import pytest
from tradingagents.strategies.data_sources.equity_universe import (
    EquityUniverse, normalize_assets, POLICY,
)
NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)

def row(symbol='AAPL', exchange='NASDAQ', status='active', tradable=True):
    return {'symbol':symbol, 'exchange':exchange, 'status':status,
            'tradable':tradable, 'class':'us_equity'}

def snapshot(rows):
    return normalize_assets(rows, observed_at=NOW, response_sha256='a'*64)

def test_exact_active_listed_and_no_broker_tradability_filter():
    universe = EquityUniverse(snapshot([row(), row('ABC', tradable=False), row('OTC', 'OTC')]))
    assert universe.decision('AAPL') == 'eligible'
    assert universe.decision('ABC') == 'eligible'
    assert universe.decision('OTC') == 'outside_sip_exchange_universe'
    assert universe.decision('UNKNOWN') == 'absent_from_asset_master'
    assert universe.decision('') == 'unresolved_issuer'

def test_active_record_resolves_inactive_history_but_multiple_active_is_ambiguous():
    universe = EquityUniverse(snapshot([row('OLD', status='inactive'),row('OLD'),
        row('AMB'), row('AMB',exchange='NYSE'), row('ONLY',status='inactive')]))
    assert universe.decision('OLD') == 'eligible'
    assert universe.decision('AMB') == 'ambiguous_active_asset'
    assert universe.decision('ONLY') == 'inactive_asset'

def test_symbols_are_exact_no_dash_dot_or_case_guess():
    universe = EquityUniverse(snapshot([row('BRK.B','NYSE')]))
    assert universe.decision('BRK.B') == 'eligible'
    assert universe.decision('BRK-B') == 'unresolved_symbol_alias'
    assert universe.decision('brk.b') == 'invalid_symbol'

@pytest.mark.parametrize('mutation',[
    lambda r:r.pop('tradable'), lambda r:r.update(tradable=1),
    lambda r:r.update(status='unknown'),lambda r:r.update(symbol=''),
    lambda r:r.update(exchange=None),lambda r:r.update(**{'class':'crypto'}),
])
def test_incomplete_or_invalid_master_fails(mutation):
    value=row();mutation(value)
    with pytest.raises(ValueError):snapshot([value])

@pytest.mark.parametrize('rows',[[],None,{},'bad'])
def test_master_requires_complete_nonempty_array(rows):
    with pytest.raises(ValueError):snapshot(rows)

def test_canonical_evidence_retains_all_duplicate_observations():
    original=[row(),row('OLD',status='inactive'),row('OLD')]
    a=snapshot(original);b=snapshot(list(reversed(original)))
    assert a==b and a['policy']==POLICY and len(a['assets'])==3
    assert a['observed_at']==NOW.isoformat() and a['response_sha256']=='a'*64
    a['assets'][0][0]='CHANGED'
    with pytest.raises(ValueError):EquityUniverse(a)

def test_complete_company_map_binds_cik_without_display_name_guess():
    universe=EquityUniverse(snapshot([row('LISTED'),row('PINK','OTC')]))
    company_map={'0':{'cik_str':1,'ticker':'LISTED','title':'Any'},
                 '1':{'cik_str':2,'ticker':'PINK','title':'Any'}}
    assert universe.filing_decision(['0000000001'],company_map)['status']=='eligible'
    assert universe.filing_decision(['0000000002'],company_map)['status']=='excluded'
    # Search ciks carry multiple roles: any eligible/unknown role keeps acquisition required.
    assert universe.filing_decision(['0000000002','0000000001'],company_map)['status']=='eligible'
    assert universe.filing_decision(['0000000002','0000000003'],company_map)['status']=='unresolved'
    assert universe.filing_decision([],company_map)['status']=='unresolved'

def test_cik_multiple_share_classes_any_eligible_preserves_filing():
    universe=EquityUniverse(snapshot([row('X.A'),row('X.B','OTC')]))
    company_map={'0':{'cik_str':1,'ticker':'X.A'},'1':{'cik_str':1,'ticker':'X.B'}}
    assert universe.filing_decision(['1'],company_map)['status']=='eligible'

def test_native_inactive_identifiers_are_preserved_without_aliasing():
    value=snapshot([row(),row('67058h102','NYSE','inactive'),row('ahpaw','NASDAQ','inactive')])
    assert len(value['assets'])==3
    universe=EquityUniverse(value)
    assert universe.decision('AAPL')=='eligible'
    assert universe.decision('AHP AW')=='invalid_symbol'
    assert universe.decision('AHPAW')=='absent_from_asset_master'

def test_possible_provider_symbol_alias_keeps_filing_unresolved():
    universe=EquityUniverse(snapshot([row('BRK.B','NYSE')]))
    company={'0':{'cik_str':1067983,'ticker':'BRK-B'}}
    result=universe.filing_decision(['1067983'],company)
    assert result['status']=='unresolved'
    assert result['symbols']=={'BRK-B':'unresolved_symbol_alias'}

@pytest.fixture
def asset_request(monkeypatch):
    import io,json,requests
    from tradingagents.strategies.data_sources import equity_universe as module
    monkeypatch.setenv('ALPACA_API_KEY','fixture-key');monkeypatch.setenv('ALPACA_SECRET_KEY','fixture-secret')
    calls=[]
    def reply(body=None, status=200):
        response=requests.Response(); response.status_code=status
        response.raw=io.BytesIO(json.dumps([row()] if body is None else body).encode())
        response.headers['Content-Type']='application/json'
        from unittest.mock import Mock
        response.close=Mock(wraps=response.close)
        return response
    response=reply()
    def get(url,**kwargs):
        calls.append((url,kwargs));return response
    monkeypatch.setattr(module.requests,'get',get)
    return module,calls,response,reply

def test_fetch_uses_readonly_all_status_asset_endpoint_and_closes(asset_request):
    module,calls,response,_=asset_request
    result=module.fetch_equity_universe()
    assert EquityUniverse(result['snapshot']).decision('AAPL')=='eligible'
    assert len(calls)==1
    url,options=calls[0]
    assert url==module.ENDPOINT and options['params']=={'asset_class':'us_equity'}
    assert options['stream'] is True and options['allow_redirects'] is False
    assert response.close.call_count==1 and result['coverage']['complete'] is True

def test_fetch_invalid_rows_never_publishes_partial_master(asset_request,monkeypatch):
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    module,_,_,reply=asset_request
    response=reply([row(),{'symbol':'BAD'}])
    monkeypatch.setattr(module.requests,'get',lambda *a,**k:response)
    with pytest.raises(SourceFetchError) as raised:module.fetch_equity_universe()
    assert raised.value.reason_code=='invalid_response' and response.close.call_count==1

def test_fetch_missing_credentials_no_request(asset_request,monkeypatch):
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    module,calls,_,_=asset_request;monkeypatch.delenv('ALPACA_SECRET_KEY')
    with pytest.raises(SourceFetchError):module.fetch_equity_universe()
    assert not calls

def test_universe_admission_runs_before_budget_and_retains_exclusion_identity():
    from tradingagents.strategies.modules.admission import admit_candidates, candidate_universe
    from tradingagents.strategies.modules.base import Candidate
    universe=EquityUniverse(snapshot([row('PINK','OTC'),row('LISTED')]))
    candidates=[Candidate(ticker='PINK',date='2026-10-09',direction='long',score=.9),
                Candidate(ticker='LISTED',date='2026-10-09',direction='long',score=.5)]
    with candidate_universe(universe):result=admit_candidates('supply_chain',candidates,1)
    assert [c.ticker for c in result]==['LISTED']
    manifest=result.admission_manifest
    assert len(manifest['discovered'])==2 and len(manifest['excluded'])==1
    excluded=manifest['excluded'][0]
    assert excluded['reason']=='equity_universe:outside_sip_exchange_universe'
    assert excluded in manifest['discovered']
    assert excluded['universe']['assets_sha256']==universe.evidence['assets_sha256']
    assert manifest['universe_policy']==POLICY
    # Scope never leaks into unrelated screens or legacy configurations.
    assert admit_candidates('supply_chain',candidates,1)[0].ticker=='PINK'

def test_unresolved_filing_issuer_keeps_analysis_despite_display_ticker():
    from tradingagents.strategies.modules.admission import admit_candidates, candidate_universe
    from tradingagents.strategies.modules.base import Candidate
    universe=EquityUniverse(snapshot([row('PINK','OTC')]),company_map={'0':{'cik_str':1,'ticker':'PINK'}})
    candidates=[Candidate(ticker='PINK',date='2026-10-09',direction='long',score=.5,
        metadata={'accession_number':'0000000001-26-000001','source_ciks':['2']})]
    with candidate_universe(universe):result=admit_candidates('filing_analysis',candidates,None)
    assert len(result)==1 and not result.admission_manifest['excluded']
    assert result[0].metadata['equity_universe']['decision']=='unresolved_issuer'

def test_only_bound_outside_filing_is_excluded_before_analysis():
    from tradingagents.strategies.modules.admission import admit_candidates, candidate_universe
    from tradingagents.strategies.modules.base import Candidate
    universe=EquityUniverse(snapshot([row('PINK','OTC')]),company_map={'0':{'cik_str':1,'ticker':'PINK'}})
    candidate=Candidate(ticker='',date='2026-10-09',direction='long',score=.5,
        metadata={'accession_number':'0000000001-26-000001','source_ciks':['1']})
    with candidate_universe(universe):result=admit_candidates('filing_analysis',[candidate],None)
    assert not result and len(result.admission_manifest['excluded'])==1

def test_unknown_and_ambiguous_symbols_do_not_receive_favorable_scope_evidence():
    from tradingagents.strategies.modules.admission import admit_candidates, candidate_universe
    from tradingagents.strategies.modules.base import Candidate
    universe=EquityUniverse(snapshot([row('AMB'),row('AMB','NYSE')]))
    candidates=[Candidate(ticker=t,date='2026-10-09',direction='long',score=.5) for t in ('','AMB')]
    with candidate_universe(universe):result=admit_candidates('litigation',candidates,None)
    assert len(result)==2 and not result.admission_manifest['excluded']
    assert {c.metadata['equity_universe']['decision'] for c in result}=={'unresolved_issuer','ambiguous_active_asset'}

def engine_for(candidates):
    from types import SimpleNamespace
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies.modules.admission import admit_candidates
    engine=MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.ar_config={'equity_universe_policy':POLICY}
    engine.paper_trade_strategies=[SimpleNamespace(name='supply_chain',data_sources=['finnhub'],
        get_default_params=lambda **kwargs:{},
        screen=lambda *args:admit_candidates('supply_chain',candidates,1))]
    engine._emit=lambda *a,**k:None
    engine._build_regime_model=lambda *a:{}
    engine._enrich_with_llm=lambda value,*a,**k:value
    return engine

def test_engine_uses_declared_universe_before_enrichment():
    from tradingagents.strategies.modules.base import Candidate
    candidates=[Candidate(ticker=t,date='2026-10-09',direction='long',score=s) for t,s in [('PINK',.9),('LISTED',.5)]]
    engine=engine_for(candidates)
    calls=[]
    engine._enrich_with_llm=lambda value,*a,**k:calls.append([c.ticker for c in value]) or value
    data={'finnhub':{},'equity_universe':{'snapshot':snapshot([row('PINK','OTC'),row('LISTED')])}}
    signals,_,health=engine.screen_and_enrich('2026-10-09',data,epoch_id='epoch',policy_id='policy')
    assert [s['ticker'] for s in signals]==['LISTED'] and calls==[['LISTED']]
    assert health[0].status=='signals'
    assert health[0].evidence['admission_manifest']['excluded'][0]['ticker']=='PINK'

def test_engine_missing_declared_universe_is_coverage_failure_even_at_zero_candidates():
    engine=engine_for([])
    _,_,health=engine.screen_and_enrich('2026-10-09',{'finnhub':{}},epoch_id='epoch',policy_id='policy')
    assert health[0].status=='data_failure'
    assert health[0].evidence['provider_errors']['equity_universe']=='universe_evidence_unavailable'

def test_engine_retains_post_analysis_outside_target_in_health_but_not_priced_signals():
    from tradingagents.strategies.modules.base import Candidate
    candidate=Candidate(ticker='',date='2026-10-09',direction='long',score=.5)
    engine=engine_for([candidate])
    def analyze(values,*a,**k):
        values[0].ticker='PINK';values[0].metadata['analysis_status']='validated'
        return values
    engine._enrich_with_llm=analyze
    data={'finnhub':{},'equity_universe':{'snapshot':snapshot([row('PINK','OTC')])}}
    signals,_,health=engine.screen_and_enrich('2026-10-09',data,epoch_id='epoch',policy_id='policy')
    assert not signals and health[0].status=='signals'
    assert health[0].evidence['universe_assessments'][0]['decision']=='outside_sip_exchange_universe'
    assert len(health[0].evidence['admission_manifest']['admitted'])==1

def test_acquisition_fetches_asset_snapshot_before_edgar_with_same_deadline(monkeypatch):
    from types import SimpleNamespace
    from tradingagents.strategies.data_sources import equity_universe as module
    from tradingagents.strategies.data_sources.request_policy import current_provider_deadline
    engine=engine_for([]);engine.config={'autoresearch':engine.ar_config}
    engine.paper_trade_strategies=[SimpleNamespace(data_sources=['edgar'])]
    engine.registry=SimpleNamespace(available_sources=lambda:['edgar'],get=lambda name:None)
    events=[];master={'snapshot':snapshot([row()]),'coverage':{'complete':True}}
    def assets():
        events.append(('assets',current_provider_deadline('alpaca')));return master
    def edgar(session, universe_data=None):
        events.append(('edgar',current_provider_deadline('edgar')))
        assert universe_data is not None and universe_data['snapshot']==master['snapshot']
        return {'filings':[]}
    monkeypatch.setattr(module,'fetch_equity_universe',assets)
    engine._fetch_edgar_events=edgar
    data=engine._fetch_all_data('2026-09-25','2026-10-09')
    assert data['equity_universe']['snapshot']==master['snapshot']
    assert [x[0] for x in events]==['assets','edgar'] and events[0][1]==events[1][1]

def test_monitor_excludes_only_bound_outside_filings_from_text_requests():
    from types import SimpleNamespace
    from tradingagents.strategies.learning.event_monitor import EventMonitor
    from tradingagents.strategies.data_sources.evidence import CoverageRecords
    rows=[{'adsh':str(i),'ciks':[str(i)],'form_type':'8-K','file_url':'https://fixture/'+str(i)} for i in (1,2,3)]
    calls=[]
    source=SimpleNamespace(is_available=lambda:True,
        search_filings=lambda **kwargs:CoverageRecords(rows,coverage={'complete':True}),
        get_filing_text=lambda url:calls.append(url) or 'Substantive filing text')
    monitor=EventMonitor(SimpleNamespace(get=lambda name:source));monitor.as_of='2026-10-09'
    monitor.equity_universe=EquityUniverse(snapshot([row('LISTED'),row('PINK','OTC')]),
        company_map={'0':{'cik_str':1,'ticker':'PINK'},'1':{'cik_str':2,'ticker':'LISTED'}})
    result=monitor.poll_edgar_filings(['8-K'])
    assert calls==['https://fixture/2','https://fixture/3'] and len(result)==3
    assert result[0]['text_status']=='outside_declared_equity_universe'
    assert result[0]['universe_membership']['status']=='excluded'


def test_possible_alias_remains_required_in_ordinary_admission_and_postanalysis():
    from tradingagents.strategies.modules.base import Candidate
    candidate=Candidate(ticker='BRK-B',date='2026-10-09',direction='long',score=.5)
    engine=engine_for([candidate])
    data={'finnhub':{},'equity_universe':{'snapshot':snapshot([row('BRK.B','NYSE')])}}
    _,_,health=engine.screen_and_enrich('2026-10-09',data,epoch_id='epoch',policy_id='policy')
    assert len(health[0].evidence['admission_manifest']['admitted'])==1
    assert health[0].evidence['universe_assessments'][0]['decision']=='unresolved_symbol_alias'
    assert health[0].status=='data_failure'
    assert candidate.journal_only


def test_universe_fingerprint_binds_actual_environment_even_with_unused_config(monkeypatch):
    from tradingagents.strategies.orchestration.source_inputs import source_configuration_fingerprint
    config={'autoresearch':{'equity_universe_policy':POLICY,'alpaca_api_key':'unused-key',
                            'alpaca_secret_key':'unused-secret'}}
    monkeypatch.setenv('ALPACA_API_KEY','first-key');monkeypatch.setenv('ALPACA_SECRET_KEY','first-secret')
    first=source_configuration_fingerprint(config)
    monkeypatch.setenv('ALPACA_API_KEY','second-key');monkeypatch.setenv('ALPACA_SECRET_KEY','second-secret')
    assert source_configuration_fingerprint(config)!=first
