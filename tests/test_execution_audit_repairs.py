"""Native boundary regressions for the accounting audit and causal paper clock."""
from datetime import date, timedelta
from decimal import Decimal as D
from dataclasses import replace
from unittest.mock import patch
import pandas as pd
import pytest
from tradingagents.strategies.execution import SignalRecord, OrderIntent, Fill, MarketBar, CorporateAction
from tradingagents.strategies.execution.price_source import AdjustedClose, YFinancePriceSource, CorporateActionValidationError
from tradingagents.strategies.orchestration.session_executor import SessionExecutor, SessionInputBundle
from tradingagents.strategies.orchestration.trading_calendar import session_close, session_open, previous_session
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
FRI=date(2026,7,31); MON=date(2026,8,3); THU=date(2026,7,30)
def at(s): return session_close(s)+timedelta(hours=2)
def bar(t,s=MON,p='100',low=None):
    p=D(p)
    return MarketBar(t,s,p,p+1,D(low) if low else p-1,p,'alpaca-sip-1d-raw',at(s),False)
def cfg(**risk):
    return {'execution':{'mode':'paper'},'autoresearch':{'total_capital':10000,'risk_gate':{'long_only':False,'min_position_value':1,'max_position_pct':1,'max_positions':8,'per_strategy_max':8,**risk},'short_selling':{'borrow_cost_reject_above':'0.05'},'paper_ledger':{'pricing_version':'raw-alpaca-sip-v1','slippage_bps':'10','benchmark_symbols':['SPY','BIL']}}}
def intent(l,key,t='AAPL',side='buy',qty=10,ref=FRI,due=MON,stop=None):
    stamp=session_close(ref)
    sig=SignalRecord('s-'+key,'epoch','policy','e-'+key,'strategy',t,'short' if side in ('short','cover') else 'long',stamp,stamp,ref,D('100'),stamp,key)
    l.record_signal(sig)
    o=OrderIntent('i-'+key,(sig.signal_id,),l.cohort_id,side,qty,stamp,due,'resting_stop' if stop else 'next_session_open','pending',D(stop) if stop else None,None)
    l.stage_intent(o); return o

def seed(l,t='AAPL',qty=10,side='buy',mark=True):
    o=intent(l,'seed-'+t,t,side,qty,THU,FRI)
    f=Fill('f-'+t,o.intent_id,side,FRI,session_open(FRI),at(FRI),D('100'),D('100'),qty,D(0),D(0),D(0))
    l.apply_fill(o,f,borrow_rate=D('.01') if side=='short' else None)
    if mark: l.mark(FRI,{t:bar(t,FRI)},'epoch',at(FRI))
    return o

def bundle(bars,actions=()):
    bm={(s,MON):AdjustedClose(s,MON,D(100),'fixture-adjusted',at(MON),previous_session(MON),D(100)) for s in ('SPY','BIL')}
    return SessionInputBundle(MON,tuple(sorted(bars)),{(t,MON):b for t,b in bars.items()},tuple(actions),bm)
@pytest.fixture
def ledger(tmp_path):
    l=PortfolioLedger(tmp_path/'portfolio.db','cohort',D(10000))
    yield l
    l.close()

def test_net_daily_loss_is_durable_at_admission(ledger):
    seed(ledger); intent(ledger,'exit',side='sell'); new=intent(ledger,'new','MSFT',qty=1)
    b=bundle({'AAPL':bar('AAPL',p='60'),'MSFT':bar('MSFT')})
    def crash(phase):
        if phase=='execute_exits': raise RuntimeError('crash')
    with pytest.raises(RuntimeError): SessionExecutor(ledger,cfg(),after_phase_commit=crash).execute_open_and_mark(MON,'epoch',b,{},at(MON))
    assert SessionExecutor(ledger,cfg()).execute_open_and_mark(MON,'epoch',b,{},at(MON)+timedelta(days=2)).valid
    assert ledger.intent(new.intent_id).status=='rejected'

def test_split_open_mark_and_costed_cap(ledger):
    seed(ledger); new=intent(ledger,'new','MSFT',qty=11)
    a=CorporateAction('split','AAPL',MON,'split',D(2),None,'fixture',at(MON),True)
    r=SessionExecutor(ledger,cfg(max_position_pct=.1)).execute_open_and_mark(MON,'epoch',bundle({'AAPL':bar('AAPL',p='50'),'MSFT':bar('MSFT')},[a]),{},at(MON))
    assert r.valid and ledger.intent(new.intent_id).status=='rejected'

@pytest.mark.parametrize('stop_price,opening,low,expected',[('95','90','89','rejected'),('95','100','90','rejected')])
def test_stop_cooldown_and_intraday_cash_causality(tmp_path,stop_price,opening,low,expected):
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(1000)); seed(l)
    intent(l,'stop',side='sell',stop=stop_price)
    new=intent(l,'new',qty=1)
    c=cfg(); c['autoresearch']['risk_discipline']={'reentry_cooldown_days':7}
    r=SessionExecutor(l,c).execute_open_and_mark(MON,'epoch',bundle({'AAPL':bar('AAPL',p=opening,low=low)}),{},at(MON))
    assert r.valid and l.intent(new.intent_id).status==expected
    f=l.read_fills(MON,MON)[0]
    assert f.effective_at == (session_open(MON) if opening=='90' else session_close(MON))
    l.close()

def test_cost_reserve(tmp_path):
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(1000)); new=intent(l,'new','MSFT',qty=9)
    r=SessionExecutor(l,cfg(cash_reserve_pct=.1)).execute_open_and_mark(MON,'epoch',bundle({'MSFT':bar('MSFT')}),{},at(MON))
    assert r.valid and l.intent(new.intent_id).status=='rejected'; l.close()

@pytest.mark.parametrize('value',[None,float('nan')])
def test_missing_action_evidence_is_not_zero(value):
    frame=pd.DataFrame({'Stock Splits':[value],'Dividends':[value]},index=pd.to_datetime([MON]))
    y=YFinancePriceSource(now=lambda:at(MON))
    with patch.object(y,'_raw_frame',return_value=(frame,at(MON))), pytest.raises(CorporateActionValidationError): y.get_corporate_actions(['AAPL'],MON)

def test_split_before_dividend_independent_of_ids(ledger):
    seed(ledger)
    actions=[CorporateAction('z-split','AAPL',MON,'split',D(2),None,'fixture',at(MON),True),CorporateAction('a-dividend','AAPL',MON,'cash_dividend',None,D(1),'fixture',at(MON),True)]
    ledger.apply_corporate_actions(MON,actions,at(MON))
    assert ledger.account_state().cash==D(9000)
    assert ledger.account_state().dividend_receivable==D(20)

def test_weekend_short_cost_survives_close_and_late_resume(ledger):
    seed(ledger,side='short'); intent(ledger,'cover',side='cover')
    b=bundle({'AAPL':bar('AAPL')})
    def crash(phase):
        if phase=='execute_entries': raise RuntimeError('crash')
    with pytest.raises(RuntimeError): SessionExecutor(ledger,cfg(),after_phase_commit=crash).execute_open_and_mark(MON,'epoch',b,{'AAPL':D('.01')},at(MON))
    r=SessionExecutor(ledger,cfg()).execute_open_and_mark(MON,'epoch',b,{'AAPL':D('.01')},at(MON)+timedelta(days=2))
    assert r.valid and r.snapshot.borrow_cost==D('0.0822')

def test_winning_exits_offset_losing_exits_in_daily_gate(ledger):
    seed(ledger,mark=False); seed(ledger,t='WIN',mark=False)
    ledger.mark(FRI,{t:bar(t,FRI) for t in ('AAPL','WIN')},'epoch',at(FRI))
    intent(ledger,'loss',side='sell')
    intent(ledger,'win','WIN','sell'); new=intent(ledger,'new','MSFT',qty=1)
    b=bundle({'AAPL':bar('AAPL',p='60'),'WIN':bar('WIN',p='140'),'MSFT':bar('MSFT')})
    r=SessionExecutor(ledger,cfg()).execute_open_and_mark(MON,'epoch',b,{},at(MON))
    assert r.valid and ledger.intent(new.intent_id).status=='filled'
    assert ledger.session_realized_net(MON)==D('-2')

@pytest.mark.parametrize('phase',['accrue_borrow','execute_entries'])
@pytest.mark.parametrize('carried',[True,False])
def test_short_delayed_resume_uses_bound_validation_and_exactly_once_cost(ledger,phase,carried):
    if carried: seed(ledger,side='short')
    else: intent(ledger,'new',side='short')
    b=bundle({'AAPL':bar('AAPL')})
    def crash(done):
        if done==phase: raise RuntimeError('crash')
    with pytest.raises(RuntimeError): SessionExecutor(ledger,cfg(),after_phase_commit=crash).execute_open_and_mark(MON,'epoch',b,{'AAPL':D('.01')},at(MON))
    r=SessionExecutor(ledger,cfg()).execute_open_and_mark(MON,'epoch',b,{'AAPL':D('.01')},at(MON)+timedelta(days=7))
    assert r.valid and r.snapshot.borrow_cost==(D('.0822') if carried else D(0))
    assert SessionExecutor(ledger,cfg()).execute_open_and_mark(MON,'epoch',b,{'AAPL':D('.01')},at(MON)+timedelta(days=8)).snapshot == r.snapshot

def test_marked_short_collateral_blocks_extra_exposure(tmp_path):
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(1000)); seed(l,side='short')
    new=intent(l,'new','MSFT',qty=1)
    b=bundle({'AAPL':bar('AAPL',p='140'),'MSFT':bar('MSFT')})
    r=SessionExecutor(l,cfg(max_drawdown_pct=1)).execute_open_and_mark(MON,'epoch',b,{'AAPL':D('.01')},at(MON))
    assert r.valid and l.intent(new.intent_id).status=='rejected'
    assert r.snapshot.margin_used==D(2100)
    assert l.account_state().buying_power < 0
    l.close()

@pytest.mark.parametrize('field',['Stock Splits','Dividends'])
def test_missing_action_column_rejects_coverage(field):
    frame=pd.DataFrame({field:[0.]},index=pd.to_datetime([MON]))
    y=YFinancePriceSource(now=lambda:at(MON))
    with patch.object(y,'_raw_frame',return_value=(frame,at(MON))), pytest.raises(CorporateActionValidationError):
        y.get_corporate_actions(['AAPL'],MON)


def test_intraday_stop_cannot_fund_unrelated_open_entry(tmp_path):
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(1000)); seed(l)
    intent(l,'stop',side='sell',stop='95'); new=intent(l,'new','MSFT',qty=1)
    b=bundle({'AAPL':bar('AAPL',low='90'),'MSFT':bar('MSFT')})
    r=SessionExecutor(l,cfg()).execute_open_and_mark(MON,'epoch',b,{},at(MON))
    assert r.valid and l.intent(new.intent_id).status=='rejected'
    assert l.read_fills(MON,MON)[0].effective_at==session_close(MON)
    l.close()


def test_cooldown_counts_exchange_sessions_and_uses_persisted_stops(ledger):
    from tradingagents.strategies.orchestration.trading_calendar import next_session
    seed(ledger); intent(ledger,'stop',side='sell',stop='95')
    SessionExecutor(ledger,cfg()).execute_open_and_mark(MON,'epoch',bundle({'AAPL':bar('AAPL',p='90')}),{},at(MON))
    cursor=MON
    for _ in range(7):
        assert ledger.cooling_tickers(cursor,7)=={'AAPL'}
        cursor=next_session(cursor)
    assert ledger.cooling_tickers(cursor,7)==set()

@pytest.mark.parametrize('split',[False,True])
def test_default_shared_policy_uses_net_loss_and_coherent_split_open(tmp_path,split):
    import test_portfolio_policy_execution_invariants as fixture
    from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
    l=fixture._ledger(tmp_path)
    profile=SIZE_PROFILES['5k']; config=fixture._config(tmp_path,profile)
    ex=SessionExecutor(l,config,size_profile=profile)
    held=fixture._stage_entry(l,ex,'AAPL',FRI,5,suffix='held',reference_session=THU)
    l.apply_fill(held,Fill('seed',held.intent_id,'buy',FRI,session_open(FRI),at(FRI),D(100),D(100),5,D(0),D(0),D(0)))
    l.mark(FRI,{'AAPL':bar('AAPL',FRI)},'epoch',at(FRI))
    if split:
        original=fixture._signal
        def lower_reference(ticker,*args,**kwargs):
            return replace(original(ticker,*args,**kwargs),reference_close=D(95) if ticker=='MSFT' else D(100))
        with patch.object(fixture,'_signal',lower_reference):
            new=fixture._stage_entry(l,ex,'MSFT',MON,13,suffix='new')
        actions=(CorporateAction('split','AAPL',MON,'split',D(2),None,'fixture',at(MON),True),); price='50'
    else:
        new=fixture._stage_entry(l,ex,'MSFT',MON,2,suffix='new')
        l.stage_intent(OrderIntent('exit',held.signal_ids,l.cohort_id,'sell',5,session_close(FRI),MON,'next_session_open','pending',None,None))
        actions=();price='60'
    r=ex.execute_open_and_mark(MON,'epoch',bundle({'AAPL':bar('AAPL',p=price),'MSFT':bar('MSFT')},actions),{},at(MON))
    assert ex.policy_enabled and r.valid and l.intent(new.intent_id).status=='rejected'
    l.close()

def test_calendar_financing_uses_prior_debit_and_replays(ledger):
    # A pre-existing debit is carried across the weekend even if today's exits repay it.
    seed(ledger,qty=110)
    b=bundle({'AAPL':bar('AAPL')}); intent(ledger,'exit',side='sell',qty=110)
    c=cfg(); c['autoresearch']['paper_ledger']['margin_financing_rate']='0.365'
    r=SessionExecutor(ledger,c).execute_open_and_mark(MON,'epoch',b,{},at(MON))
    assert r.valid and r.snapshot.financing_cost==D(3)
    assert SessionExecutor(ledger,c).execute_open_and_mark(MON,'epoch',b,{},at(MON)+timedelta(days=7)).snapshot==r.snapshot


def test_contract_and_cooldown_changes_rotate_epoch_identity(tmp_path):
    from test_metric_epoch_runtime import _context, _policy
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(10000))
    c=cfg(); baseline=SessionExecutor(l,c).semantic_policy_document()
    c['autoresearch']['risk_discipline']={'reentry_cooldown_days':7}
    changed=SessionExecutor(l,c).semantic_policy_document()
    old={**baseline,'execution_clock_contract':'exact-next-xnys-open-v1','cost_model_contract':'adverse-equity-fill-v1'}
    contexts=[_context(cohort_policies=(_policy(execution_policy=p),)) for p in (baseline,changed,old)]
    assert len({c.config_hash for c in contexts})==3
    assert old['execution_clock_contract']=='exact-next-xnys-open-v1'
    l.close()

@pytest.mark.parametrize('gate,value',[('earnings_blackout_days',3),('max_borrow_cost_pct',.05)])
def test_enabled_optional_short_gate_fails_closed_without_evidence(ledger,gate,value):
    new=intent(ledger,'new',side='short')
    r=SessionExecutor(ledger,cfg(**{gate:value})).execute_open_and_mark(MON,'epoch',bundle({'AAPL':bar('AAPL')}),{'AAPL':D('.01')},at(MON))
    assert r.valid and ledger.intent(new.intent_id).status=='rejected'

def test_duplicate_identical_split_does_not_inflate_daily_loss_denominator(ledger):
    seed(ledger); intent(ledger,'exit',side='sell'); new=intent(ledger,'new','MSFT',qty=1)
    split=CorporateAction('split','AAPL',MON,'split',D(2),None,'fixture',at(MON),True)
    b=bundle({'AAPL':bar('AAPL',p='34.5'),'MSFT':bar('MSFT')},[split,split])
    r=SessionExecutor(ledger,cfg()).execute_open_and_mark(MON,'epoch',b,{},at(MON))
    assert r.valid and ledger.intent(new.intent_id).status=='rejected'
    assert ledger.opening_equity(MON,{'AAPL':D('34.5'),'MSFT':D(100)})==D(9690)

@pytest.mark.parametrize('held_pending',[False,True])
def test_costed_policy_rebases_existing_and_pending_exposure(tmp_path,held_pending):
    import test_portfolio_policy_execution_invariants as fixture
    from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
    l=fixture._ledger(tmp_path); profile=SIZE_PROFILES['5k']
    ex=SessionExecutor(l,fixture._config(tmp_path,profile),size_profile=profile)
    for ticker,qty in [('AAPL',10),('HELD',5)]:
        if ticker=='HELD' and held_pending:
            fixture._stage_entry(l,ex,ticker,date(2026,8,4),qty,suffix=ticker)
        else:
            staged=fixture._stage_entry(l,ex,ticker,FRI,qty,suffix=ticker,reference_session=THU)
            l.apply_fill(staged,Fill('seed-'+ticker,staged.intent_id,'buy',FRI,session_open(FRI),at(FRI),D(100),D(100),qty,D(0),D(0),D(0)))
    marks={t:bar(t,FRI) for t in ('AAPL',) if held_pending}
    if not held_pending: marks={t:bar(t,FRI) for t in ('AAPL','HELD')}
    l.mark(FRI,marks,'epoch',at(FRI))
    new=fixture._stage_entry(l,ex,'MSFT',MON,10,suffix='new')
    r=ex.execute_open_and_mark(MON,'epoch',bundle({'AAPL':bar('AAPL'),'HELD':bar('HELD'),'MSFT':bar('MSFT',p='99.96')}),{},at(MON))
    assert r.valid and l.intent(new.intent_id).status=='rejected'
    assert l.account_state().net_equity==D(5000)
    l.close()
