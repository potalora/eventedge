"""Offline compositional regressions for corrected metric evidence contracts."""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from tradingagents.strategies.execution import CorporateAction
from tradingagents.strategies.metrics.models import OutcomeRecord, SignalMetricRecord
from tradingagents.strategies.metrics.outcomes import OutcomeCalculator
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.metrics.service import MetricsService
from tradingagents.strategies.validation.engine import compute_car
from tradingagents.strategies.validation.models import EventSpec
from tradingagents.strategies.validation.price_adapter import yfinance_price_fn
from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
from test_execution_audit_repairs import bar, at

@pytest.mark.parametrize('direction,expected',[('long',D('.02')),('short',D('-.02'))])
def test_outcome_total_shareholder_return_crosses_split_and_distribution(direction,expected):
    entry=date(2026,8,3); exit=date(2026,8,7)
    signal=SignalMetricRecord('event','signal','epoch','policy','strategy','AAPL',direction,at(date(2026,7,31)),date(2026,7,31))
    actions=(CorporateAction('z-split','AAPL',exit,'split',D(2),None,'fixture',at(exit),True),CorporateAction('a-div','AAPL',exit,'cash_dividend',None,D(1),'fixture',at(exit),True))
    result=OutcomeCalculator().build(signal,5,{('AAPL',entry):bar('AAPL',entry),('AAPL',exit):bar('AAPL',exit,p='50')},corporate_actions=actions)
    assert result.status=='valid' and result.signed_return==expected
    assert result.return_basis=='next_open_total_shareholder_return_gross_v2'


def test_reporting_reads_all_mature_scoped_outcomes(tmp_path):
    store=MetricStore(tmp_path/'metrics.sqlite3')
    records=[]
    for i in range(1001):
        val=D('.1') if i<1000 else D('-.1')
        record=OutcomeRecord(f'o-{i:04}',f's-{i:04}',f'e-{i:04}','epoch','strategy','policy','AAPL','long',5,date(2026,8,3),date(2026,8,7),D(100),D(110),val,val,'valid','')
        store.upsert_outcome(record);records.append(record)
    read=store.read_outcomes('epoch')
    assert len(read)==1001
    signals=tuple(SignalMetricRecord(r.event_key, r.signal_id, r.epoch_id, r.policy_id,
        r.strategy, r.ticker, r.direction, at(date(2026,7,31)), date(2026,7,31)) for r in records)
    assert MetricsService._directional_accuracy_5d(signals,read)==1000/1001
    assert MetricsService._directional_accuracy_5d((signals[-1],),read)==0


def sessions():
    from tradingagents.strategies.metrics.calendar import XNYSCalendar
    cal=XNYSCalendar(); current=date(2024,1,2); out=[]
    for _ in range(400):
        out.append(current.isoformat()); current=cal.next_session(current)
    return out


def test_event_study_uses_total_return_adjustment():
    dates=sessions(); ix=300
    raw=np.full(400,100.); raw[ix:]=90.
    def download(symbols,**kwargs):
        symbol=symbols[0]
        return pd.DataFrame({('Close',symbol):np.full(400,100.) if symbol=='SPY' else raw,('Adj Close',symbol):np.full(400,100. if symbol=='SPY' else 90.)},index=pd.to_datetime(dates))
    with patch('yfinance.download',download):
        result=compute_car([EventSpec('TEST',dates[ix],'demo')],yfinance_price_fn(YFinanceSource()),windows=[(0,5)],n_bootstrap=10)
    assert result.events[0].cars['[0,+5]']==pytest.approx(0)
    assert result.events[0].metadata['return_basis']=='total_return_adjusted'


def test_negative_event_window_includes_prior_session_and_withholds_iid_claims():
    dates=sessions(); stock=np.full(400,100.); stock[299:]*=1.1
    def prices(symbol,start,end): return dict(zip(dates,np.full(400,100.) if symbol=='SPY' else stock))
    result=compute_car([EventSpec('TEST',dates[300],'demo')]*2,prices,windows=[(-1,1)],n_bootstrap=10)
    assert result.events[0].cars['[-1,+1]']==pytest.approx(.1)
    stats=result.aggregates[0].windows[0]
    assert stats.p_value is None and stats.ci is None
    assert 'depend' in stats.inference_unavailable_reason


def test_event_study_rejects_missing_exchange_session_in_window():
    dates=sessions()
    def prices(symbol,start,end):
        return {d:100. for i,d in enumerate(dates) if symbol=='SPY' or i!=301}
    result=compute_car([EventSpec('TEST',dates[300],'demo')],prices,windows=[(0,5)],n_bootstrap=10)
    assert not result.events or result.events[0].cars['[0,+5]'] is None

def test_native_benchmark_pairs_chain_distribution_without_rewriting_history(tmp_path):
    from tradingagents.strategies.execution.price_source import YFinancePriceSource
    from tradingagents.strategies.orchestration.session_executor import SessionExecutor
    from tradingagents.strategies.orchestration.trading_calendar import previous_session
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    from tradingagents.strategies.metrics.portfolio import matched_benchmark_returns
    from test_execution_audit_repairs import cfg
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(10000)); ex=SessionExecutor(l,cfg())
    observed=[]
    for session,level in [(date(2026,8,3),100),(date(2026,8,4),99)]:
        prior=previous_session(session)
        def download(symbols,**kwargs):
            observed.append((kwargs['start'],kwargs['end']))
            dates=pd.to_datetime([prior,session])
            return pd.DataFrame([[level,level],[level,level]],index=dates,columns=pd.MultiIndex.from_tuples([('Close','SPY'),('Close','BIL')]))
        with patch('tradingagents.strategies.execution.price_source.yf.download',download):
            b=ex.fetch_input_bundle(session,(),YFinancePriceSource(now=lambda:at(session)))
        assert b.benchmarks['SPY',session].previous_session==prior
        assert b.benchmarks['SPY',session].previous_close==D(level)
        assert ex.execute_open_and_mark(session,'epoch',b,{},at(session)).valid
    records=l.read_benchmark_observations()
    assert {r.return_basis for r in records}=={'paired_total_return_index_v2'}
    assert {r.close for r in records}=={D(100)}
    assert matched_benchmark_returns(l.read_snapshots(),records)[0].value==pytest.approx(0)
    old=l.persisted_input_bundle if hasattr(l,'persisted_input_bundle') else ex.persisted_input_bundle
    assert old(date(2026,8,3)).benchmarks['SPY',date(2026,8,3)].close==D(100)
    assert old(date(2026,8,4)).benchmarks['SPY',date(2026,8,4)].previous_close==D(99)
    l.close()

@pytest.mark.parametrize('count,reason',[(30,'insufficient_return_count'),(31,'zero_variance')])
def test_ratio_availability_distinguishes_count_from_zero_variance(count,reason):
    from dataclasses import asdict
    from test_portfolio_metrics_v2 import _snapshot, _observation, COHORT, EPOCH
    from tradingagents.strategies.metrics.portfolio import portfolio_metrics
    from tradingagents.dashboard.pages.returns import _rows, SHARPE_LABEL
    dates=[date.fromisoformat(s) for s in sessions()[:count]]
    report=portfolio_metrics(cohort_id=COHORT,epoch_id=EPOCH,snapshots=[_snapshot(s,'100') for s in dates],benchmark_observations=[_observation(s,symbol,'100') for s in dates for symbol in ('SPY','BIL')],signals=[],fills=[])
    assert report.sharpe_unavailable_reason==reason
    assert report.information_ratio_unavailable_reason==reason
    assert report.max_drawdown==0 and report.closed_trades==0
    text=_rows({'book':asdict(report)})[0][SHARPE_LABEL]
    assert ('zero variance' in text) if count==31 else ('29/30 returns' in text)

def _capture_independent_outcome_inputs(executor, source, session):
    from tradingagents.strategies.orchestration.daily_pipeline import DailyRunState
    from tradingagents.strategies.orchestration.outcome_evidence import capture_outcome_inputs

    class SessionClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at(session)

    cohort = {"executor": executor, "ledger": executor.ledger}
    owner = SimpleNamespace(cohorts=[cohort], _metric_store=executor.metric_store, _price_source=source)
    state = DailyRunState(owner, str(session), session, at(session), fresh=[cohort])
    with patch("tradingagents.strategies.orchestration.outcome_evidence.datetime", SessionClock):
        capture_outcome_inputs(state)


@pytest.mark.parametrize('direction,expected',[('long',D('.02')),('short',D('-.02'))])
def test_native_untraded_outcome_has_continuous_action_coverage(tmp_path,direction,expected):
    from test_session_executor import FakePriceSource, _config
    from test_outcome_metrics_v2 import _ledger_signal
    from tradingagents.strategies.orchestration.session_executor import SessionExecutor
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(1000)); ex=SessionExecutor(l,_config())
    signal=replace(_ledger_signal('AAPL',date(2026,8,3)),direction=direction)
    l.record_signal(signal)
    for day in (4,5,6,7,10):
        session=date(2026,8,day); price='50' if day>=6 else '100'; b=replace(bar('AAPL',session,p=price),source='fixture-raw')
        actions=[CorporateAction('z-split','AAPL',session,'split',D(2),None,'fixture',at(session),True),CorporateAction('a-div','AAPL',session,'cash_dividend',None,D(1),'fixture',at(session),True)] if day==6 else []
        source=FakePriceSource(bars={('AAPL',session):b},actions=actions,adjusted={(s,session):D(100) for s in ('SPY','BIL')})
        _capture_independent_outcome_inputs(ex, source, session)
        assert ex.execute_open_and_mark(session,'epoch-1',source,{},at(session)).valid
    assert ex.record_due_outcomes(session,'epoch-1',{('AAPL',session):b})==1
    outcome=ex.metric_store.read_outcomes('epoch-1')[0]
    assert outcome.status=='valid' and outcome.signed_return==expected
    assert not l.read_fills() and l.account_state().cash==D(1000)
    l.close()

@pytest.mark.parametrize('kind',['missing','wrong_session','mixed_vintage'])
def test_benchmark_pair_evidence_fails_closed(kind):
    from datetime import timedelta
    from tradingagents.strategies.execution.price_source import AdjustedClose, paired_adjusted_closes, BarValidationError
    from tradingagents.strategies.orchestration.trading_calendar import previous_session
    current=date(2026,8,4); prior=previous_session(current)
    row=AdjustedClose('SPY',current,D(99),'fixture',at(current))
    rows={('SPY',current):row,('SPY',prior):replace(row,session=prior)}
    if kind=='missing': rows.pop(('SPY',prior))
    elif kind=='wrong_session': rows['SPY',prior]=replace(row,session=current)
    else: rows['SPY',prior]=replace(rows['SPY',prior],fetched_at=at(current)-timedelta(days=1))
    with pytest.raises(BarValidationError): paired_adjusted_closes(rows,('SPY',),current)


def test_legacy_unpaired_benchmark_report_is_explicitly_unavailable(tmp_path):
    from test_metrics_service import _epoch, _record_window, SESSIONS
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    cohort='horizon_3m_size_100k'; l=PortfolioLedger(tmp_path/cohort/'portfolio.db',cohort,D(100000))
    _record_window(l,'epoch-1',SESSIONS[:2])
    # This database models an existing v1 ledger, not a production migration.
    l.connection.execute("UPDATE benchmark_observations SET return_basis='total_return_adjusted'")
    service=MetricsService(tmp_path,{cohort:l}); service.store.save_epoch(_epoch())
    report=service.generation_report('epoch-1')
    book=report['headline_books'][cohort]
    assert not book['metrics_available'] and book['unavailable_reason']=='legacy_unpaired_benchmark_basis'
    assert report['cohort_series'][cohort]['matched_benchmark_returns']==[]
    assert {r.return_basis for r in l.read_benchmark_observations()}=={'total_return_adjusted'}
    l.close()


def test_event_journal_preserves_distinct_catalysts_direction_and_clock():
    from tradingagents.strategies.validation.journal_source import events_from_journals
    stamp='2026-08-03T21:30:00+00:00'
    entries=[dict(timestamp=stamp,strategy='litigation',ticker='AAPL',direction=d,score=.5,metadata={'event_key':key,'published_at':'2026-08-03T21:00:00+00:00'}) for key,d in [('case1','long'),('case2','long'),('case1','short')]]
    journal=SimpleNamespace(get_entries=lambda **_:entries)
    result=events_from_journals([journal,journal])
    assert len(result)==3
    assert {r.metadata['direction'] for r in result}=={'long','short'}
    assert all(r.metadata['journaled_at']==stamp and r.metadata['publication_at'].endswith('21:00:00+00:00') for r in result)


def test_untraded_outcome_with_missing_intermediate_action_coverage_is_invalid(tmp_path):
    from test_session_executor import FakePriceSource, _config, _bar
    from test_outcome_metrics_v2 import _ledger_signal
    from tradingagents.strategies.orchestration.session_executor import SessionExecutor
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    l=PortfolioLedger(tmp_path/'p.db','cohort',D(1000)); ex=SessionExecutor(l,_config())
    l.record_signal(_ledger_signal('AAPL',date(2026,8,3)))
    for day in (4,5,7,10):  # Aug 6 corporate-action evidence was never accepted.
        session=date(2026,8,day); b=_bar('AAPL',session,'100','100')
        source=FakePriceSource(bars={('AAPL',session):b},adjusted={(s,session):D(100) for s in ('SPY','BIL')})
        _capture_independent_outcome_inputs(ex, source, session)
        assert ex.execute_open_and_mark(session,'epoch-1',source,{},at(session)).valid
    ex.record_due_outcomes(session,'epoch-1',{('AAPL',session):b})
    outcome=ex.metric_store.read_outcomes('epoch-1')[0]
    assert outcome.status=='invalid' and outcome.invalid_reason=='missing_action_coverage:AAPL/2026-08-06'
    assert outcome.raw_return is None
    l.close()


def test_event_study_missing_embargo_session_cannot_shift_estimation_window():
    dates=sessions()
    # This jump is day -251 and must remain outside the [-250,-11] fit.
    stock=np.full(400,100.); stock[49:]*=1.1
    def prices(symbol,start,end):
        values=np.full(400,100.) if symbol=='SPY' else stock
        return {day:value for index,(day,value) in enumerate(zip(dates,values)) if symbol=='SPY' or index!=295}
    result=compute_car([EventSpec('TEST',dates[300],'demo')],prices,windows=[(0,5)],n_bootstrap=10)
    assert not result.events
