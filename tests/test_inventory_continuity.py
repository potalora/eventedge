"""Carried inventory recovery and dividend payment boundaries."""
from dataclasses import replace
from decimal import Decimal as D
from datetime import date
from dataclasses import asdict
import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest
from test_execution_audit_repairs import seed, intent, cfg, bar, at, FRI, MON
from tradingagents.strategies.execution import CorporateAction
from tradingagents.strategies.execution.price_source import AdjustedClose
from tradingagents.strategies.orchestration.session_executor import SessionExecutor, SessionInputBundle
from tradingagents.strategies.orchestration.trading_calendar import previous_session, next_session
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

class Source:
    def __init__(self, now, actions=(), price='50', low=None):
        self.now, self.actions, self.price, self.low = now, actions, price, low
        self.calls=[]
    def get_daily_bars(self, tickers, start, end, adjusted=False):
        return {(t,start):replace(bar(t,start,self.price,self.low),fetched_at=at(self.now)) for t in tickers}
    def get_corporate_actions(self,tickers,session):
        self.calls.append((tuple(tickers),session))
        return [replace(a,fetched_at=at(self.now)) for a in self.actions if a.session==session and a.ticker in tickers]
    def get_total_return_closes(self,tickers,start,end):
        return {(t,s):AdjustedClose(t,s,D(100),'fixture',at(self.now)) for t in tickers for s in (start,end)}

@pytest.mark.parametrize('invalid',[False,True])
@pytest.mark.parametrize('side',['buy','short'])
def test_gap_split_restores_inventory_before_stop_or_cost(tmp_path,invalid,side):
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(10000)); seed(l,side=side)
    stop=intent(l,'stop',side='sell' if side=='buy' else 'cover',stop='92' if side=='buy' else '108')
    if invalid: l.invalidate_session_and_cancel_due(MON,'missing raw',at(MON))
    resume=next_session(MON)
    split=CorporateAction('gap-split','AAPL',MON,'split',D(2),None,'fixture',at(resume),True)
    source=Source(resume,[split])
    result=SessionExecutor(l,cfg()).execute_open_and_mark(resume,'epoch',source,{'AAPL':D('.01')},at(resume))
    assert result.valid, result.invalid_reason
    assert l.connection.execute('SELECT open_qty,entry_price FROM lots').fetchone()[:] == (20,'50')
    assert l.intent(stop.intent_id).stop_price == D('46' if side=='buy' else '54')
    assert l.intent(stop.intent_id).status=='pending'
    assert (('AAPL',),MON) in source.calls
    assert l.connection.execute('SELECT session FROM corporate_actions').fetchone()[0]==MON.isoformat()
    if side=='buy': assert result.snapshot.net_equity==D(10000)
    if invalid: assert l.connection.execute('SELECT valid FROM session_runs WHERE session=?',(MON.isoformat(),)).fetchone()[0]==0
    l.close()


def test_unknown_dividend_does_not_finance_entry(tmp_path):
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(1000));seed(l)
    order=intent(l,'new','MSFT',qty=1)
    action=CorporateAction('div','AAPL',MON,'cash_dividend',None,D(10),'fixture',at(MON),True)
    result=SessionExecutor(l,cfg()).execute_open_and_mark(MON,'epoch',Source(MON,[action],price='90'),{},at(MON))
    assert result.valid, result.invalid_reason
    assert l.intent(order.intent_id).status=='rejected'
    assert result.snapshot.cash==0 and result.snapshot.net_equity==1000
    assert result.snapshot.dividend_receivable==100
    assert l.opening_equity(MON,{"AAPL":D(90),"MSFT":D(90)})==1000
    l.close()

@pytest.mark.parametrize('side',['buy','short'])
@pytest.mark.parametrize('crash_phase',['apply_corporate_actions','execute_exits'])
def test_dividend_payment_and_replay_once(tmp_path,side,crash_phase):
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(10000));seed(l,side=side)
    pay=next_session(MON)
    action=CorporateAction('div','AAPL',MON,'cash_dividend',None,D(10),'fixture',at(MON),True,payment_date=pay)
    ex=SessionExecutor(l,cfg())
    result=ex.execute_open_and_mark(MON,'epoch',Source(MON,[action],price='90'),{'AAPL':D('.01')},at(MON))
    assert result.valid, result.invalid_reason
    signed=D(100) if side=='buy' else D(-100)
    assert result.snapshot.dividend_receivable==signed and result.snapshot.dividend_cash==0
    if side=='short': assert l.account_state().buying_power <= result.snapshot.cash-result.snapshot.margin_used-100
    cash=result.snapshot.cash
    def crash(phase):
        if phase==crash_phase: raise RuntimeError('power loss')
    with pytest.raises(RuntimeError,match='power loss'):
        SessionExecutor(l,cfg(),after_phase_commit=crash).execute_open_and_mark(pay,'epoch',Source(pay,price='90'),{'AAPL':D('.01')},at(pay))
    recovered=ex.execute_open_and_mark(pay,'epoch',Source(pay,price='777'),{'AAPL':D('.01')},at(pay))
    assert recovered.valid, recovered.invalid_reason
    assert recovered.snapshot.dividend_receivable==0 and recovered.snapshot.dividend_cash==signed
    assert recovered.snapshot.cash==cash+signed-(recovered.snapshot.borrow_cost-result.snapshot.borrow_cost)
    assert ex.execute_open_and_mark(pay,'epoch',Source(pay,price='999'),{'AAPL':D('.01')},at(pay)).snapshot==recovered.snapshot
    assert l.connection.execute("SELECT COUNT(*) FROM cash_events WHERE event_type='dividend'").fetchone()[0]==1
    l.close()

@pytest.mark.parametrize('side,opening,low,high',[('buy','100','80','101'),('buy','90','89','91'),('short','100','99','120'),('short','110','109','111')])
def test_persistent_stop_survives_gap_but_missed_entry_expires(tmp_path,side,opening,low,high):
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(10000));seed(l,side=side)
    stop=intent(l,'stop',side='sell' if side=='buy' else 'cover',stop='92' if side=='buy' else '108')
    entry=intent(l,'entry','MSFT',qty=1)
    l.invalidate_session_and_cancel_due(MON,'market unavailable',at(MON))
    assert l.intent(entry.intent_id).status=='cancelled'
    assert l.intent(stop.intent_id).status=='pending'
    resume=next_session(MON); source=Source(resume,price=opening,low=low)
    orig=source.get_daily_bars
    source.get_daily_bars=lambda *args,**kwargs:{key:replace(b,high=D(high)) for key,b in orig(*args,**kwargs).items()}
    ex=SessionExecutor(l,cfg());r=ex.execute_open_and_mark(resume,'epoch',source,{'AAPL':D('.01')},at(resume))
    assert r.valid, r.invalid_reason
    assert l.intent(stop.intent_id).status=='filled'
    assert len(l.read_fills(resume,resume))==1
    assert ex.execute_open_and_mark(resume,'epoch',source,{'AAPL':D('.01')},at(resume)).snapshot==r.snapshot
    assert len(l.read_fills(resume,resume))==1
    l.close()


def test_missing_action_continuity_fails_closed(tmp_path):
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(10000));seed(l)
    resume=next_session(MON); source=Source(resume)
    current_only=SessionExecutor.fetch_input_bundle(resume,('AAPL',),source)
    result=SessionExecutor(l,cfg()).execute_open_and_mark(resume,'epoch',current_only,{},at(resume))
    assert not result.valid and 'missing inventory action continuity' in result.invalid_reason
    assert l.connection.execute('SELECT open_qty FROM lots').fetchone()[0]==10
    assert not l.read_snapshots(resume,resume)
    l.close()


def test_weekend_needs_no_non_session_coverage(tmp_path):
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(10000));seed(l)
    source=Source(MON,price='100')
    r=SessionExecutor(l,cfg()).execute_open_and_mark(MON,'epoch',source,{},at(MON))
    assert r.valid and source.calls==[(('AAPL',),MON)]
    l.close()


def test_caught_up_split_then_dividend_replay_immutable(tmp_path):
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(10000));seed(l)
    resume=next_session(MON)
    actions=[CorporateAction('z-split','AAPL',MON,'split',D(2),None,'fixture',at(resume),True),CorporateAction('a-div','AAPL',MON,'cash_dividend',None,D(1),'fixture',at(resume),True)]
    source=Source(resume,actions,price='49')
    def crash(phase):
        if phase=='apply_corporate_actions':raise RuntimeError('power loss')
    with pytest.raises(RuntimeError):SessionExecutor(l,cfg(),after_phase_commit=crash).execute_open_and_mark(resume,'epoch',source,{},at(resume))
    source.actions=[]
    r=SessionExecutor(l,cfg()).execute_open_and_mark(resume,'epoch',source,{},at(resume))
    assert r.valid, r.invalid_reason
    assert r.snapshot.net_equity==10000 and r.snapshot.dividend_receivable==20
    assert l.connection.execute('SELECT open_qty FROM lots').fetchone()[0]==20
    assert l.connection.execute('SELECT COUNT(*) FROM dividend_events').fetchone()[0]==1
    context=l.session_execution_context(resume)
    assert MON.isoformat() in context['economic_inputs_json']
    l.close()


def test_shared_historical_actions_do_not_readjust_up_to_date_book(tmp_path):
    old=PortfolioLedger(tmp_path/'old.db','old',D(10000));seed(old)
    fresh=PortfolioLedger(tmp_path/'fresh.db','fresh',D(10000));seed(fresh)
    split=CorporateAction('split','AAPL',MON,'split',D(2),None,'fixture',at(MON),True)
    assert SessionExecutor(fresh,cfg()).execute_open_and_mark(MON,'epoch',Source(MON,[split]),{},at(MON)).valid
    resume=next_session(MON)
    source=Source(resume,[split])
    shared=SessionExecutor.fetch_input_bundle(resume,('AAPL',),source,continuity_requirements=old.inventory_action_requirements(resume))
    for ledger in (old,fresh):
        result=SessionExecutor(ledger,cfg()).execute_open_and_mark(resume,'epoch',shared,{},at(resume))
        assert result.valid,result.invalid_reason
        assert result.snapshot.net_equity==10000
        assert ledger.connection.execute('SELECT open_qty FROM lots').fetchone()[0]==20
        ledger.close()


def test_silent_skip_expires_next_open_entry_without_backdating_fill(tmp_path):
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(10000));seed(l)
    order=intent(l,'missed','MSFT',qty=1)
    resume=next_session(MON)
    result=SessionExecutor(l,cfg()).execute_open_and_mark(resume,'epoch',Source(resume,price='100'),{},at(resume))
    assert result.valid,result.invalid_reason
    assert l.intent(order.intent_id).status=='cancelled'
    assert not l.read_fills(MON,resume)
    assert not l.read_snapshots(MON,MON)
    l.close()

@pytest.mark.parametrize('kind',['split','stop'])
@pytest.mark.parametrize('gap',['skipped','invalid'])
def test_native_orchestrator_recovers_gap_inventory_and_protection(kind,gap):
    result=_native_gap(gap,kind)
    assert result['execution_valid']
    if kind=='split':
        assert result['quantity']==20 and result['net_equity']==100000
        assert result['actions']==[{'action_id':'real-gap-split','session':'2026-03-31'}]
    else:
        assert result['quantity']==0 and result['net_equity']==D('99919.08')
        assert len(result['final_fills'])==1


def _native_gap(skip, action_kind='split'):
    from test_30day_simulation import _authoritative_orchestrator, AuthoritativePriceSource, CohortConfig
    from tradingagents.strategies.execution import SignalRecord, OrderIntent, Fill
    from tradingagents.strategies.orchestration.trading_calendar import session_close,session_open
    from datetime import datetime, timezone
    with tempfile.TemporaryDirectory(prefix='blindspot-native-gap-') as temp:
        p=Path(temp); source=AuthoritativePriceSource()
        c=CohortConfig(name='horizon_3m_size_100k',state_dir=str(p/'book'),horizon='3m',size_profile='100k',use_llm=False)
        orch,_=_authoritative_orchestrator(p,cohort_configs=[c],strategy_modules=[],source=source)
        days=[date(2026,3,30),date(2026,3,31),date(2026,4,1)]
        orig=source.get_daily_bars
        def prices(tickers,start,end,adjusted=False):
            rows=orig(tickers,start,end,adjusted)
            price=D(100) if start==days[0] or action_kind=='stop' else D(50)
            low=D(80) if action_kind=='stop' and start==days[2] else price-D(1)
            return {k:replace(v,open=price,close=price,high=price+D(1),low=low) for k,v in rows.items()}
        source.get_daily_bars=prices
        orig_actions=source.get_corporate_actions
        def actions(tickers,s):
            orig_actions(tickers,s)
            return [CorporateAction('real-gap-split','AAPL',s,'split',D(2),None,'fixture',datetime.now(timezone.utc),True)] if action_kind=='split' and s==days[1] and 'AAPL' in tickers else []
        source.get_corporate_actions=actions
        with patch('tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize',return_value=[]):
            l=orch.cohorts[0]['ledger']
            # Seed a legitimate pre-first-session position; actual orchestration owns every subsequent phase.
            ref=previous_session(days[0]); stamp=session_close(ref)
            sig=SignalRecord('seed','epoch','policy','seed-event','filing_analysis','AAPL','long',stamp,stamp,ref,D(100),stamp,'seed')
            l.record_signal(sig)
            order=OrderIntent('seed-intent',('seed',),l.cohort_id,'buy',10,stamp,days[0],'next_session_open','pending',None,None)
            l.stage_intent(order)
            l.apply_fill(order,Fill('seed-fill',order.intent_id,'buy',days[0],session_open(days[0]),datetime.now(timezone.utc),D(100),D(100),10,D(0),D(0),D(0)))
            first=orch.run_daily(days[0].isoformat()); assert first[c.name]['execution_valid'],first
            if skip=='control':
                middle=orch.run_daily(days[1].isoformat());assert middle[c.name]['execution_valid'],middle
            elif skip=='invalid':
                resolve=source.resolve_governed_daily_bars
                def unavailable(tickers,s,*,processed_at):
                    result=resolve(tickers,s,processed_at=processed_at)
                    if s==days[1]:return replace(result,bars={},failure_map={'AAPL':'injected gap'})
                    return result
                source.resolve_governed_daily_bars=unavailable
                middle=orch.run_daily(days[1].isoformat()); assert not middle[c.name]['execution_valid'],middle
            final=orch.run_daily(days[2].isoformat())
            snap=l.read_snapshots(days[2],days[2])[0]
            out={'case':skip,'action_kind':action_kind,'execution_valid':final[c.name]['execution_valid'],'net_equity':snap.net_equity,'quantity':l.connection.execute('select open_qty from lots').fetchone()[0],'actions':[dict(x) for x in l.connection.execute('select action_id,session from corporate_actions')],'action_calls':source.action_calls,'epoch':orch._epoch_id,'intents':[dict(x) for x in l.connection.execute('select intent_id,price_rule,stop_price,eligible_session,status from order_intents')],'final_fills':[asdict(f) for f in l.read_fills(days[2],days[2])]}
            l.close(); return out


def test_legacy_ledger_remains_readable_without_schema_writes(tmp_path):
    import sqlite3
    path=tmp_path/'p.db'
    ledger=PortfolioLedger(path,'cohort',D(10000));seed(ledger);ledger.close()
    connection=sqlite3.connect(path)
    connection.execute('DROP TABLE dividend_receivables')
    connection.execute('DROP TABLE dividend_payment_terms')
    connection.execute('ALTER TABLE account_snapshots DROP COLUMN dividend_receivable')
    connection.commit();connection.close()
    before=path.read_bytes()
    ledger=PortfolioLedger.open_existing(path)
    assert ledger.read_snapshots(FRI,FRI)[0].dividend_receivable==0
    assert ledger.account_state().dividend_receivable==0
    assert ledger.execution_starting_state(MON)['dividend_receivables']==[]
    ledger.close()
    assert path.read_bytes()==before


def test_ambiguous_gap_dividends_invalidate_before_cost_or_inventory_changes(tmp_path):
    ledger=PortfolioLedger(tmp_path/'p.db','cohort',D(10000));seed(ledger,side='short')
    resume=next_session(MON)
    actions=[CorporateAction(key,'AAPL',MON,'cash_dividend',None,D(1),'fixture',at(resume),True) for key in ('a','b')]
    result=SessionExecutor(ledger,cfg()).execute_open_and_mark(resume,'epoch',Source(resume,actions,price='100'),{'AAPL':D('.01')},at(resume))
    assert not result.valid and 'ambiguous corporate action terms' in result.invalid_reason
    assert ledger.connection.execute('SELECT COUNT(*) FROM borrow_accruals').fetchone()[0]==0
    assert ledger.dividend_receivable()==0
    ledger.close()


@pytest.mark.parametrize("side", ["buy", "short"])
def test_already_applied_gap_action_reconciles_fresh_provenance_once(tmp_path,side):
    ledger=PortfolioLedger(tmp_path/'p.db','cohort',D(10000));seed(ledger,side=side)
    action=CorporateAction('split','AAPL',MON,'split',D(2),None,'fixture',at(MON),True)
    ledger.apply_corporate_actions(MON,[action],at(MON))
    ledger.invalidate_session_and_cancel_due(MON,'later accounting unavailable',at(MON))
    resume=next_session(MON)
    result=SessionExecutor(ledger,cfg()).execute_open_and_mark(resume,'epoch',Source(resume,[action]),{'AAPL':D('.01')},at(resume))
    assert result.valid,result.invalid_reason
    assert result.snapshot.net_equity==D(10000)-result.snapshot.borrow_cost
    assert ledger.opening_equity(resume,{'AAPL':D(50)})==10000
    if side=='short': assert result.snapshot.borrow_cost==D('.1096')
    assert ledger.connection.execute('SELECT open_qty FROM lots').fetchone()[0]==20
    assert ledger.connection.execute('SELECT COUNT(*) FROM lot_action_applications').fetchone()[0]==1
    assert ledger.connection.execute('SELECT fetched_at FROM corporate_actions').fetchone()[0]==at(MON).isoformat()
    ledger.close()
