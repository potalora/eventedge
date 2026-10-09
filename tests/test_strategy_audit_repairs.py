"""Regression cases at audited native strategy/model boundaries."""
from types import SimpleNamespace
import pandas as pd
import pytest
from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
from tradingagents.strategies.modules.insider_activity import InsiderActivityStrategy
from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.data_sources.registry import DataSourceRegistry
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee

def engine(tmp_path):
    return MultiStrategyEngine(config={'autoresearch': {'state_dir': str(tmp_path)}}, registry=DataSourceRegistry())

def test_filings_preserve_distinct_events_and_require_text():
    s=FilingAnalysisStrategy()
    filings=[{'ticker':'HIG','entity_name':'HARTFORD FINANCIAL SERVICES GROUP INC','form_type':f,'accession_number':f,'file_date':'2026-10-09'} for f in ('10-Q','8-K')]
    candidates=s.screen({'edgar':{'filings':filings}},'2026-10-09',s.get_default_params())
    assert len(candidates)==2
    assert {c.ticker for c in candidates}=={'HIG'}
    assert all(c.journal_only and c.metadata['non_actionable_reason']=='missing_source_text' for c in candidates)
    activist=dict(filings[0],form_type='SC 13D')
    assert s.screen({'edgar':{'activist_13d':[activist]}},'2026-10-09',s.get_default_params())

@pytest.mark.parametrize('result',[{'direction':None,'conviction':.8},{'direction':'long','conviction':.8,'affected_tickers':'AAPL'},{'direction':'short','score':.8,'defendant_ticker':['AAPL']}])
def test_complete_schema_and_batch_isolation(tmp_path,result):
    e=engine(tmp_path);e._analyzer=SimpleNamespace(analyze_supply_chain=lambda *a,**k:result)
    bad=Candidate('AAPL','2026-10-09','long',.6,metadata={'analysis_type':'supply_chain','needs_llm_analysis':True,'headline':'Factory closes'})
    other=Candidate('MSFT','2026-10-09')
    assert e._enrich_with_llm([bad,other],'supply_chain')==[bad,other]
    assert bad.journal_only and bad.direction=='long' and 'llm_analysis' not in bad.metadata

@pytest.mark.parametrize('kind',['material_event','activist_stake','passive_stake','quantum_readiness'])
def test_required_dispatch(tmp_path,kind):
    e=engine(tmp_path);calls=[]
    e._analyzer=SimpleNamespace(analyze_filing_change=lambda *a,**k:calls.append(a) or {'direction':'short','conviction':.8,'rationale':'Evidence'},analyze_quantum_readiness=lambda *a,**k:calls.append(a) or {'direction':'short','conviction':.8,'rationale':'Evidence'})
    c=Candidate('AAPL','2026-10-09',metadata={'analysis_type':kind,'needs_llm_analysis':True,'current_text':'Material losses','analysis_text':'Quantum threat'})
    e._enrich_with_llm([c],'filing_analysis')
    assert calls and c.direction=='short' and not c.journal_only

def test_one_insider_rows_do_not_make_cluster():
    s=InsiderActivityStrategy();row={'owner_name':'CEO','accession_number':'one','transaction_type':'buy','transaction_code':'P','acquired_disposed':'A','open_market':True,'shares':10,'price_per_share':100}
    assert s.screen({'edgar':{'form4':{'AAPL':[row]*4}}},'2026-10-09',s.get_default_params())==[]

@pytest.mark.parametrize('tag', ['nonDerivativeTransaction', 'derivativeTransaction'])
@pytest.mark.parametrize('code,ad,direction', [('P', 'A', 'long'), ('S', 'D', 'short')])
def test_insider_native_xml_requires_open_market(tag, code, ad, direction, monkeypatch):
    from tradingagents.strategies.data_sources.edgar_source import EDGARSource

    rows = []
    for owner in ('123', '456'):
        xml = f'''<ownershipDocument><reportingOwner><reportingOwnerId>
            <rptOwnerCik>{owner}</rptOwnerCik><rptOwnerName>Owner {owner}</rptOwnerName>
            </reportingOwnerId></reportingOwner><{tag}><transactionCoding>
            <transactionCode>{code}</transactionCode></transactionCoding><transactionAmounts>
            <transactionShares><value>100</value></transactionShares>
            <transactionPricePerShare><value>20</value></transactionPricePerShare>
            <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
            </transactionAmounts></{tag}></ownershipDocument>'''
        monkeypatch.setattr(
            'tradingagents.strategies.data_sources.edgar_source.provider_request',
            lambda *a, text=xml, **k: SimpleNamespace(text=text),
        )
        filing = {'accession_number': f'0001-26-{owner}', 'primary_document': 'form4.xml', 'filing_date': '2026-10-08'}
        rows.extend({**filing, **row} for row in EDGARSource()._parse_form4_xml('1', filing))

    strategy = InsiderActivityStrategy()
    def screen(records):
        return strategy.screen({'edgar': {'form4': {'AAPL': records}}}, '2026-10-09', strategy.get_default_params())

    assert all(row['open_market'] is (tag == 'nonDerivativeTransaction') for row in rows)
    candidates = screen(rows)
    if tag == 'derivativeTransaction':
        assert candidates == []
    else:
        assert len(candidates) == 1 and candidates[0].direction == direction
        assert candidates[0].metadata['deterministic_evidence_complete'] and not candidates[0].journal_only
        for flag in (None, False, 1, 'true'):
            assert screen([{**row, 'open_market': flag} for row in rows]) == []
        assert screen([{key: value for key, value in row.items() if key != 'open_market'} for row in rows]) == []

def test_unavailable_cot_does_not_exit():
    s=CommodityMacroStrategy()
    for source in ({'error':'unavailable'},{'silver':{'percentile':.99}},{'gold':{'percentile':.5}}):
        assert s.check_exit('GLD',100,100,1,s.get_default_params('3m'),{'cftc':source})==(False,'')

def test_macro_shapes_and_native_catalyst():
    s=CommodityMacroStrategy();fred={'CPIAUCSL':{'2025-01-01':100,'2025-04-01':103,'2026-01-01':110,'2026-04-01':111},'FEDFUNDS':{'2026-01-01':3,'2026-04-01':4}}
    assert s._macro_vetoes('gold','long',fred)
    assert s._macro_vetoes('gold','long',{k:pd.Series(v) for k,v in fred.items()})
    assert s._scan_catalysts('gold',{'regulations':{'proposed_rules':[{'title':'Gold mining tariff'}]}})
    assert not s._scan_catalysts('gold',{'regulations':{'proposed_rules':[{'title':'OPEC oil energy regulation'}]}})

@pytest.mark.parametrize('commodity', ['gold', 'silver', 'crude_oil', 'nat_gas'])
def test_macro_requires_exact_three_month_observation(commodity):
    strategy = CommodityMacroStrategy()
    fred = {'CPIAUCSL': {'2024-12-01': 100, '2025-04-01': 100,
                        '2025-12-01': 101, '2026-04-01': 100},
            'FEDFUNDS': {'2025-12-01': 2, '2026-04-01': 3}}
    assert not strategy._macro_inputs_available(commodity, 'long', fred)
    assert not strategy._macro_vetoes(commodity, 'long', fred)
    fred['CPIAUCSL'].update({'2025-01-01': 101, '2026-01-01': 101})
    fred['FEDFUNDS']['2026-01-01'] = 2
    assert strategy._macro_inputs_available(commodity, 'long', fred)
    assert strategy._macro_vetoes(commodity, 'long', fred)

def test_missing_regime_is_unknown(tmp_path):
    r=engine(tmp_path)._build_regime_model({})
    assert r['overall_regime']=='unknown' and r['vix_level'] is None and r['yield_curve_slope'] is None

def test_opposing_strategies_are_not_convergence():
    c=PortfolioCommittee({'autoresearch':{'paper_trade':{'portfolio_committee_enabled':False}}})
    signals=[{'ticker':'AAPL','strategy':'first','direction':'short','score':.8},{'ticker':'AAPL','strategy':'second','direction':'long','score':.1}]
    assert c.synthesize(signals)==[]

@pytest.mark.parametrize('strategy,down,up',[('earnings_call',94,106),('insider_activity',89,111),('quantum_readiness',92,108)])
def test_native_engine_short_strategy_exits(tmp_path,strategy,down,up):
    from datetime import date,datetime,timezone
    from decimal import Decimal
    from tradingagents.strategies.modules.earnings_call import EarningsCallStrategy
    from tradingagents.strategies.modules.quantum_readiness import QuantumReadinessStrategy
    module={'earnings_call':EarningsCallStrategy,'insider_activity':InsiderActivityStrategy,'quantum_readiness':QuantumReadinessStrategy}[strategy]()
    e=engine(tmp_path);e.paper_trade_strategies=[module]
    position={'ticker':'AAPL','lot_id':'lot','strategies':(strategy,), 'entry_price':Decimal('100'),'opened_session':date(2026,10,8),'direction':'short','quantity':1,'signal_ids':('sig',)}
    e.ledger=SimpleNamespace(cohort_id='offline',open_exit_positions=lambda:[position],pending_exit_intents=lambda *a:[])
    session=date(2026,10,9);cutoff=datetime(2026,10,9,20,tzinfo=timezone.utc)
    for price,rule in [(down,'resting_stop'),(up,'next_session_open')]:
        bar=SimpleNamespace(session=session,adjusted=False,close=Decimal(price))
        specs,_=e._build_exit_specs(session,cutoff,date(2026,10,12),{'AAPL':bar},{},'30d')
        assert specs[0][0].side=='cover' and specs[0][0].price_rule==rule

@pytest.mark.parametrize("first_text,second_text", [("2026-07-31", "2026-08-03"), ("2026-07-02", "2026-07-06"), ("2026-11-27", "2026-11-30")])
def test_late_event_released_once_and_direction_flip_suppressed(tmp_path, first_text, second_text):
    from datetime import datetime,timezone,date
    from decimal import Decimal
    from unittest.mock import patch
    from test_session_executor import _config,FRIDAY,MONDAY,FakePriceSource,_bar
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    from tradingagents.strategies.state.state import StateManager
    from tradingagents.strategies.orchestration.session_executor import SessionExecutor
    from tradingagents.strategies.orchestration.trading_calendar import next_session
    FRIDAY,MONDAY=date.fromisoformat(first_text),date.fromisoformat(second_text)
    cfg=_config();cfg['autoresearch'].update(state_dir=str(tmp_path),horizon='30d')
    ledger=PortfolioLedger(tmp_path/'portfolio.db','cohort',Decimal('1000'))
    try:
        native=MultiStrategyEngine(config=cfg,strategies=[FilingAnalysisStrategy()],registry=DataSourceRegistry(),state_manager=StateManager(str(tmp_path)),ledger=ledger)
        signal={'ticker':'AAPL','direction':'long','score':.8,'strategy':'filing_analysis','metadata':{'form_type':'8-K','accession_number':'late','file_date':FRIDAY.isoformat(),'current_text':'Strong sales'}}
        counts=[]
        for index,session in enumerate((FRIDAY,MONDAY,next_session(MONDAY))):
            lifecycle=SessionExecutor(ledger,cfg).execute_open_and_mark(session,'epoch',FakePriceSource(bars={('AAPL',session):_bar('AAPL',session)},adjusted={('SPY',session):Decimal('650'),('BIL',session):Decimal('91')}),{},datetime.combine(session,datetime.min.time(),tzinfo=timezone.utc).replace(hour=22))
            if index==2:signal=dict(signal,direction='short')
            with patch('tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize',return_value=[]) as committee:
                native.screen_and_stage(session.isoformat(),{'_execution_reference_bars':{'AAPL':_bar('AAPL',session)}},[signal],{},{},None,lifecycle.snapshot)
                counts.append(len(committee.call_args.kwargs['signals']))
            with patch('tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize',return_value=[]) as replay_committee:
                replay=native.screen_and_stage(session.isoformat(),{'_execution_reference_bars':{'AAPL':_bar('AAPL',session)}},[signal],{},{},None,lifecycle.snapshot)
                assert replay['replayed'] is True and not replay_committee.called
        assert counts==[0,1,0]
        assert len(ledger.read_signals())==2
    finally:
        ledger.close()

def test_clef_reasoning_provider_scope_and_claim_variants():
    from tradingagents.strategies.orchestration import decision_shadow as shadow
    settings=shadow._settings({})
    signals=[{'ticker':'MOO','strategy':'weather_ag','event_key':'same','direction':'long','metadata':{'article_id':1,'llm_analysis':{'reasoning':'Weather threatens crops.'}}}, {'ticker':'MOO','strategy':'weather_ag','event_key':'same','direction':'short','metadata':{'article_id':1,'llm_analysis':{'reasoning':'Rain helps crops.'}}}]
    entries,_=shadow._entries(signals,{'finnhub':{'news':[{'article_id':1,'headline':'Weather threatens crops.'}]},'edgar':{'filings':[{'article_id':1,'current_text':'Unrelated issuer'}]}},settings)
    assert len(entries)==2
    assert all(e['input']['claim'] for e in entries)
    assert all(e['coverage_reason']=='unsupported_strategy_scope' for e in entries)

@pytest.mark.parametrize('changed_field', [None, 'macro_evidence', 'cot_percentile'])
def test_reacquired_cot_releases_frozen_candidate_once(tmp_path, changed_field):
    from datetime import datetime, timezone, date
    from decimal import Decimal
    from unittest.mock import patch
    from test_session_executor import _config, FakePriceSource, _bar
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    from tradingagents.strategies.state.state import StateManager
    from tradingagents.strategies.orchestration.session_executor import SessionExecutor
    from tradingagents.strategies.orchestration.event_identity import canonical_event_key

    cfg = _config()
    cfg['autoresearch'].update(state_dir=str(tmp_path), horizon='30d')
    ledger = PortfolioLedger(tmp_path / 'portfolio.db', 'cohort', Decimal('1000'))
    sessions = [date(2026, 10, day) for day in (9, 12, 13)]
    counts = []
    try:
        for index, session in enumerate(sessions):
            # A fresh engine/process reacquires the same weekly report after close.
            native = MultiStrategyEngine(config=cfg, strategies=[CommodityMacroStrategy()],
                registry=DataSourceRegistry(), state_manager=StateManager(str(tmp_path)), ledger=ledger)
            acquisition = f'{session.isoformat()}T21:00:00+00:00'
            metadata = {'commodity': 'gold', 'report_id': 'CFTC:GOLD:2026-10-06',
                'window_end': '2026-10-06', 'available_at': acquisition,
                'cot_evidence': {'percentile': .01, 'available_at': acquisition, 'acquired_at': acquisition},
                'macro_evidence': {'FEDFUNDS': {'2026-09-01': 4.0}}, 'cot_percentile': .01}
            if index and changed_field == 'macro_evidence':
                metadata['macro_evidence']['FEDFUNDS']['2026-09-01'] = 5.0
            if index and changed_field == 'cot_percentile':
                metadata['cot_evidence']['percentile'] = metadata['cot_percentile'] = .02
            signal = {'ticker': 'GLD', 'direction': 'long', 'score': .8,
                      'strategy': 'commodity_macro', 'metadata': metadata}
            if index and changed_field:
                signal.update(direction='short', score=.4)
            pending = native.pending_late_signals(session, 'epoch')
            if pending:
                assert pending[0]['metadata']['available_at'] == '2026-10-09T21:00:00+00:00'
                assert pending[0]['metadata']['macro_evidence']['FEDFUNDS']['2026-09-01'] == 4.0
                assert pending[0]['metadata']['cot_percentile'] == .01
                assert pending[0]['metadata']['retained_from_signal_id']
                assert pending[0]['direction'] == 'long' and pending[0]['score'] == .8
            queued_keys = {canonical_event_key(row['strategy'], row['ticker'], row['metadata'], session) for row in pending}
            signals = pending + ([] if canonical_event_key(signal['strategy'], signal['ticker'], metadata, session) in queued_keys else [signal])
            lifecycle = SessionExecutor(ledger, cfg).execute_open_and_mark(
                session, 'epoch', FakePriceSource(bars={('GLD', session): _bar('GLD', session)},
                adjusted={('SPY', session): Decimal('650'), ('BIL', session): Decimal('91')}),
                {}, datetime.combine(session, datetime.min.time(), tzinfo=timezone.utc).replace(hour=22))
            with patch('tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize', return_value=[]) as committee:
                native.screen_and_stage(session.isoformat(),
                    {'_execution_reference_bars': {'GLD': _bar('GLD', session)}},
                    signals, {}, {}, None, lifecycle.snapshot)
                counts.append(len(committee.call_args.kwargs['signals']))
            # Same-session queue reconstruction is stable after the timely row.
            assert native.pending_late_signals(session, 'epoch') == pending
        assert counts == [0, 1, 0]
        records = ledger.read_signals()
        assert records[0].observed_at.isoformat() == '2026-10-09T21:00:00+00:00'
        assert len(records) == 2 and records[-1].observed_at == records[0].observed_at
    finally:
        ledger.close()

@pytest.mark.parametrize('latest_invalid', [None, 'journal_only', 'required_analysis'])
def test_weather_moving_window_releases_prior_frozen_candidate(tmp_path, latest_invalid):
    from datetime import datetime, timezone, date
    from decimal import Decimal
    from unittest.mock import patch
    from test_session_executor import _config, FakePriceSource, _bar
    from tradingagents.strategies.modules.weather_ag import WeatherAgStrategy
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    from tradingagents.strategies.state.state import StateManager
    from tradingagents.strategies.orchestration.session_executor import SessionExecutor

    cfg = _config()
    cfg['autoresearch'].update(state_dir=str(tmp_path), horizon='30d')
    ledger = PortfolioLedger(tmp_path / 'portfolio.db', 'cohort', Decimal('1000'))
    counts = []
    try:
        for index, day in enumerate((9, 12, 13)):
            session = date(2026, 10, day)
            native = MultiStrategyEngine(config=cfg, strategies=[WeatherAgStrategy()],
                registry=DataSourceRegistry(), state_manager=StateManager(str(tmp_path)), ledger=ledger)
            metadata = {'commodity': 'corn', 'source_observation_ids': [f'NOAA:{session}'],
                'window_end': session.isoformat(), 'available_at': f'{session}T21:00:00+00:00',
                'needs_llm_analysis': True, 'analysis_status': 'validated',
                'llm_analysis': {'direction': 'long', 'conviction': .8, 'reasoning': f'Frozen evidence {session}'}}
            current = {'ticker': 'CORN', 'direction': 'long', 'score': .8,
                       'strategy': 'weather_ag', 'metadata': metadata}
            if index == 2 and latest_invalid == 'journal_only':
                current['journal_only'] = True
            if index == 2 and latest_invalid == 'required_analysis':
                metadata['analysis_status'] = 'failed'
            pending = native.pending_late_signals(session, 'epoch')
            assert len(pending) == bool(index)
            if pending:
                prior_day = '2026-10-09' if index == 1 else '2026-10-12'
                assert pending[0]['metadata']['llm_analysis']['reasoning'] == f'Frozen evidence {prior_day}'
                assert pending[0]['metadata']['available_at'] == f'{prior_day}T21:00:00+00:00'
            lifecycle = SessionExecutor(ledger, cfg).execute_open_and_mark(
                session, 'epoch', FakePriceSource(bars={('CORN', session): _bar('CORN', session)},
                adjusted={('SPY', session): Decimal('650'), ('BIL', session): Decimal('91')}),
                {}, datetime.combine(session, datetime.min.time(), tzinfo=timezone.utc).replace(hour=22))
            with patch('tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize', return_value=[]) as committee:
                native.screen_and_stage(session.isoformat(),
                    {'_execution_reference_bars': {'CORN': _bar('CORN', session)}},
                    pending + [current], {}, {}, None, lifecycle.snapshot)
                counts.append(len(committee.call_args.kwargs['signals']))
            assert native.pending_late_signals(session, 'epoch') == pending
        assert counts == [0, 1, 1]
        assert len(native.pending_late_signals(date(2026, 10, 14), 'epoch')) == (0 if latest_invalid else 1)
        native.ar_config['disabled_strategies'] = {'weather_ag': 'test_policy'}
        assert native.pending_late_signals(date(2026, 10, 14), 'epoch') == []
    finally:
        ledger.close()

def test_legacy_overrides_and_signed_failures(tmp_path):
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    from tradingagents.strategies.learning.signal_journal import SignalJournal,JournalEntry
    analyzer=LLMAnalyzer();analyzer.set_prompt_override('earnings_call','OVERRIDE');seen=[]
    analyzer._call_llm=lambda system,user,**k:seen.append(system) or '{}'
    analyzer.analyze_earnings_call('Coverage','AAPL');assert seen==['OVERRIDE']
    journal=SignalJournal(str(tmp_path));journal.log_signal(JournalEntry('2026-10-01','insider_activity','AAPL','short',.9,llm_conviction=.9,entry_price=100.,return_5d=.1))
    assert not journal.get_high_conviction_failures('insider_activity')

def test_complete_deterministic_rule_survives_model_failure(tmp_path):
    e=engine(tmp_path);e._analyzer=SimpleNamespace(analyze_insider_context=lambda *a,**k:{})
    s=InsiderActivityStrategy()
    rows=[{'owner_name':owner,'accession_number':owner,'transaction_type':'buy','transaction_code':'P','acquired_disposed':'A','open_market':True,'shares':10,'price_per_share':100,'filing_date':'2026-10-08'} for owner in ('one','two')]
    candidate=s.screen({'edgar':{'form4':{'AAPL':rows}}},'2026-10-09',s.get_default_params())[0]
    e._enrich_with_llm([candidate],s.name)
    assert not candidate.journal_only and candidate.direction=='long'
    assert candidate.metadata['analysis_status']=='failed'

def test_quantum_duplicate_articles_cannot_multiply_evidence():
    from tradingagents.strategies.modules.quantum_readiness import QuantumReadinessStrategy
    s=QuantumReadinessStrategy();article={'article_id':'one','symbol':'CRWD','headline':'Quantum milestone and PQC deadline','summary':'quantum-safe migration','published_at':'2026-10-09T12:00:00+00:00'}
    def screen(news):
        return s.screen({'finnhub':{'pqc_news':news}},'2026-10-09',s.get_default_params())
    assert screen([article]*8)==screen([article])

def test_event_monitor_text_failure_isolated_and_attempt_budgeted():
    from tradingagents.strategies.learning.event_monitor import EventMonitor
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    calls=[]
    def text(url):
        calls.append(url)
        if url=='bad':raise SourceFetchError('unavailable',reason_code='provider_error')
        return 'Actual source event text'
    source=SimpleNamespace(is_available=lambda:True,search_filings=lambda **k:[{'form_type':'8-K','file_url':'bad'},{'form_type':'8-K','file_url':'good'}],get_filing_text=text)
    monitor=EventMonitor(SimpleNamespace(get=lambda name:source));monitor.as_of='2026-10-09'
    with pytest.raises(SourceFetchError) as failure:
        monitor.poll_edgar_filings(['8-K'],max_text_fetches=2)
    filings=failure.value.partial_data['filings']
    assert calls==['bad','good'] and filings[1]['current_text']=='Actual source event text'

def test_clef_atomic_claim_never_matches_other_provider():
    from tradingagents.strategies.orchestration import decision_shadow as shadow
    signal={'ticker':'AAPL','strategy':'filing_analysis','event_key':'x','direction':'short','metadata':{'accession_number':'same','llm_analysis':{'evidence_claim':'AAPL reports losses'}}}
    entries,_=shadow._entries([signal],{'finnhub':{'news':[{'accession_number':'same','headline':'Other provider record'}]}},shadow._settings({}))
    assert entries[0]['coverage_reason']=='missing_original_evidence'
    assert entries[0]['input']['original_source_evidence']==[]

@pytest.mark.parametrize('kind,result,reason',[
    ('unknown_required',{'direction':'long','score':.8,'rationale':'Claim'},'unsupported_required_analysis:'),
    ('supply_chain',{'direction':'short','score':.8},'missing_analysis_explanation'),
    ('supply_chain',{'direction':'short','score':.8,'rationale':'Claim','severity':'unbounded'},'invalid_severity'),
])
def test_unknown_dispatch_and_incomplete_semantic_schema_fail_closed(tmp_path,kind,result,reason):
    e=engine(tmp_path);e._analyzer=SimpleNamespace(analyze_supply_chain=lambda *a,**k:result)
    candidate=Candidate('AAPL','2026-10-09',metadata={'analysis_type':kind,'needs_llm_analysis':True,'headline':'Factory closes'})
    e._enrich_with_llm([candidate],'supply_chain')
    assert candidate.journal_only
    assert candidate.metadata['analysis_failure_reason'].startswith(reason)
