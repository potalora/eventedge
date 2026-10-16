"""Engine integration preserves required analysis and whole-sample failure."""
from threading import Barrier
from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.data_sources.registry import DataSourceRegistry


def make_engine(tmp_path, analyzer, workers=2):
    engine=MultiStrategyEngine(config={'autoresearch':{'state_dir':str(tmp_path),'candidate_analysis_workers':workers}},
                               registry=DataSourceRegistry())
    engine._analyzer=analyzer
    return engine


def candidates():
    return [Candidate(ticker=symbol,date='2026-10-09',direction='long',score=.5,
        metadata={'needs_llm_analysis':True,'analysis_type':'supply_chain','headline':symbol})
        for symbol in ('AAPL','MSFT')]


def test_parallel_engine_uses_distinct_analyzers_preserves_order_and_provenance(tmp_path):
    barrier=Barrier(2,timeout=1)
    forks=[]
    class Analyzer:
        last_call_provenance={}
        last_call_failure=''
        def fork_for_parallel(self):
            worker=Analyzer();forks.append(worker);return worker
        def analyze_supply_chain(self,headline,*args,**kwargs):
            self.last_call_provenance={'symbol':headline}
            barrier.wait()
            return {'direction':'short','conviction':.8,'rationale':'validated source'}
    rows=candidates()
    result=make_engine(tmp_path,Analyzer())._enrich_with_llm(rows,'supply_chain')
    assert len(forks)==2
    assert result==rows and [c.ticker for c in result]==['AAPL','MSFT']
    assert [c.metadata['model_provenance']['symbol'] for c in result]==['AAPL','MSFT']
    assert all(c.metadata['analysis_status']=='validated' for c in result)


def test_parallel_refusal_accounts_for_every_candidate_without_fallback(tmp_path):
    class Analyzer:
        def fork_for_parallel(self):raise ValueError('parallel_client_unsupported')
        def analyze_supply_chain(self,*a,**k):raise AssertionError('must not fallback')
    rows=candidates()
    result=make_engine(tmp_path,Analyzer())._enrich_with_llm(rows,'supply_chain')
    assert result==rows
    assert all(c.journal_only and c.metadata['analysis_status']=='failed' for c in rows)
    assert all(c.metadata['analysis_failure_reason']=='analysis_unavailable' for c in rows)


def test_parallel_timeout_invalidates_completed_and_deterministic_candidates(tmp_path):
    from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded
    class Analyzer:
        last_call_provenance={}
        last_call_failure=''
        def fork_for_parallel(self):return Analyzer()
        def analyze_supply_chain(self,headline,*a,**k):
            if headline=='MSFT':raise ModelDeadlineExceeded('model_deadline_exhausted')
            return {'direction':'long','conviction':.8,'rationale':'complete'}
    rows=candidates()+[Candidate(ticker='BA',date='2026-10-09',direction='long',score=.5)]
    result=make_engine(tmp_path,Analyzer())._enrich_with_llm(rows,'supply_chain')
    assert result==rows and all(c.journal_only for c in rows)
    assert all(c.metadata['analysis_failure_reason']=='model_deadline_exhausted' for c in rows)
    assert all(c.metadata['non_actionable_reason']=='model_sample_incomplete' for c in rows)
