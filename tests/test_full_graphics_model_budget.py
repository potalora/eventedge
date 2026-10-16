"""Finite prospective timing policy; no native provider requests."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tradingagents.strategies import runtime_deadline as budget
from tradingagents.strategies import llm_utils


def config():
    return {'execution': {'mode': 'paper', 'execution_enabled': False}, 'autoresearch': {
        'model_runtime_policy': 'full_regulation_graphics_v1',
        'regulation_retrieval_policy': 'verified_government_copy_v3',
        'llm_billing_mode': 'chatgpt_plan', 'autoresearch_model': 'gpt-6-luna',
        'llm_effort': 'high', 'thesis_model': 'gpt-6.1-sol', 'thesis_effort': 'high',
        'llm_temperature': 0.0, 'chatgpt_plan_auth_path':'/not/read'}}


def test_finite_policy_and_legacy_defaults():
    assert budget.model_phase_budget_seconds({}) == 2400
    assert budget.graphics_call_seconds({}) == 120
    assert budget.model_phase_budget_seconds(config()) == 9000
    assert budget.graphics_call_seconds(config()) == 240
    assert (budget.DEFAULT_MODEL_BUDGET_S, budget.MODEL_CALL_CAP_S) == (2400, 120)


@pytest.mark.parametrize('field,value', [
    ('model_runtime_policy', None), ('model_runtime_policy', True),
    ('model_runtime_policy', 'unknown'), ('regulation_retrieval_policy', 'verified_government_copy_v2'),
    ('llm_billing_mode', 'api_key'), ('autoresearch_model', 'gpt-6-astra'),
    ('llm_effort', 'low'), ('thesis_model', 'gpt-6-astra'), ('thesis_effort', 'low'),
    ('llm_temperature', True), ('llm_temperature', 0.2),
])
def test_invalid_policy_fails_closed(field, value):
    candidate = config(); candidate['autoresearch'][field] = value
    with pytest.raises(ValueError, match='invalid_model_runtime_policy'):
        budget.model_phase_budget_seconds(candidate)


@pytest.mark.parametrize('execution', [{}, {'mode':'live','execution_enabled':False},
                                      {'mode':'paper','execution_enabled':True},
                                      {'mode':'paper','execution_enabled':0}])
def test_execution_guard(execution):
    candidate = config(); candidate['execution'] = execution
    with pytest.raises(ValueError): budget.graphics_call_seconds(candidate)


def test_standalone_and_nested_phase_preserve_absolute_budget(monkeypatch):
    monkeypatch.setattr(budget.time, 'monotonic', lambda: 100.)
    class Owner:
        def __init__(self, candidate): self.config = candidate
        @budget.bounded_model_phase
        def phase(self): return budget.current_model_deadline()
    owner = Owner(config())
    assert owner.phase() == 9100
    assert Owner({}).phase() == 2500
    with budget.model_budget(7000): assert owner.phase() == 7000
    with budget.model_budget(150): assert owner.phase() == 150
    assert budget.current_model_deadline() is None


def plan_call(monkeypatch, *, maximum=None, remaining=None):
    from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient
    from test_regulation_graphics import assets
    from tradingagents.strategies.data_sources.regulation_graphics import prepare_graphics_recognition
    request = prepare_graphics_recognition(assets())
    client = ChatGPTPlanClient(auth_path='/not/read', profile_identity='fixture')
    seen = []
    def transport(payload, timeout):
        seen.append((payload, timeout)); return {'text':'{}', 'provenance':{}}
    monkeypatch.setattr(llm_utils, 'bounded_transport', transport)
    monkeypatch.setattr(budget.time, 'monotonic', lambda: 100.)
    options = {} if maximum is None else {'max_call_seconds':maximum}
    if remaining is None:
        llm_utils.call_analysis_model(client,model='gpt-6-luna',system=request.system,prompt=request.user,
            max_tokens=4096,temperature=0.,effort='high',document_inputs=request.document_inputs,**options)
    else:
        with budget.model_budget(100+remaining):
            llm_utils.call_analysis_model(client,model='gpt-6-luna',system=request.system,prompt=request.user,
                max_tokens=4096,temperature=0.,effort='high',document_inputs=request.document_inputs,**options)
    return seen


@pytest.mark.parametrize('maximum,remaining,expected', [(None,None,120),(240,None,240),(240,35,35)])
def test_physical_native_plan_timeout(monkeypatch,maximum,remaining,expected):
    seen=plan_call(monkeypatch,maximum=maximum,remaining=remaining)
    assert len(seen)==1
    assert seen[0][0]['timeout']==seen[0][1]==expected
    assert 'max_call_seconds' not in seen[0][0]['request']


@pytest.mark.parametrize('maximum', [True,0,-1,float('inf'),float('nan'),121,241,'240'])
def test_unbounded_or_unknown_call_caps_rejected(monkeypatch,maximum):
    with pytest.raises(ValueError,match='invalid_model_call_cap'):
        plan_call(monkeypatch,maximum=maximum)


def test_expired_extended_call_never_starts(monkeypatch):
    with pytest.raises(budget.ModelDeadlineExceeded): plan_call(monkeypatch,maximum=240,remaining=-1)


def test_extended_cap_requires_images_native_plan_and_luna_high():
    with pytest.raises(ValueError):
        llm_utils.call_analysis_model(SimpleNamespace(),model='gpt-6-luna',system='s',prompt='p',
            max_tokens=4096,temperature=0.,effort='high',max_call_seconds=240)


def test_analyzer_only_forwards_extended_cap_with_strict_policy(monkeypatch):
    from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
    from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient
    from tradingagents.strategies.data_sources.regulation_graphics import prepare_graphics_recognition
    from test_regulation_graphics import assets
    monkeypatch.setattr('tradingagents.strategies.chatgpt_plan_client._profile_identity',lambda path:'fixture')
    request=prepare_graphics_recognition(assets()); analyzer=LLMAnalyzer(config())
    client=ChatGPTPlanClient(auth_path='/not/read',profile_identity='fixture')
    monkeypatch.setattr(analyzer,'_get_client',lambda:client)
    seen=[]
    def call(client,**kwargs): seen.append(kwargs); return '{}'
    monkeypatch.setattr(llm_utils,'call_analysis_model',call)
    assert analyzer._call_llm('s','p',role='bounded',document_inputs=request.document_inputs,max_call_seconds=240)=='{}'
    assert seen[-1]['max_call_seconds']==240
    assert analyzer._call_llm('s','p',role='thesis')=='{}'
    assert 'max_call_seconds' not in seen[-1]
    before=len(seen)
    assert analyzer._call_llm('s','p',role='thesis',max_call_seconds=240)==''
    assert len(seen)==before and analyzer.last_call_failure=='analysis_unavailable'
    analyzer.config['autoresearch'].pop('model_runtime_policy')
    assert analyzer._call_llm('s','p',role='bounded',document_inputs=request.document_inputs,max_call_seconds=240)==''
    assert len(seen)==before


@pytest.mark.parametrize('remaining', [None,125])
def test_standalone_enrichment_uses_policy_without_shortening_outer_budget(tmp_path,monkeypatch,remaining):
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies import candidate_parallel
    candidate=config();candidate['autoresearch']['state_dir']=str(tmp_path)
    engine=MultiStrategyEngine(config=candidate,registry=DataSourceRegistry())
    monkeypatch.setattr(budget.time,'monotonic',lambda:100.)
    seen=[]
    def analyze(*args,**kwargs): seen.append(budget.current_model_deadline()); return []
    monkeypatch.setattr(candidate_parallel,'analyze_candidates',analyze)
    if remaining is None: engine._enrich_with_llm([], 'supply_chain')
    else:
        with budget.model_budget(100+remaining): engine._enrich_with_llm([], 'supply_chain')
    assert seen==[9100 if remaining is None else 100+remaining]


@pytest.mark.parametrize('remaining', [None,125])
def test_standalone_committee_keeps_regular_call_cap_and_policy_phase(monkeypatch,remaining):
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient
    monkeypatch.setattr('tradingagents.strategies.chatgpt_plan_client._profile_identity',lambda path:'fixture')
    committee=PortfolioCommittee(config())
    client=ChatGPTPlanClient(auth_path='/not/read',profile_identity='fixture')
    monkeypatch.setattr(committee,'_get_client',lambda:client)
    monkeypatch.setattr(budget.time,'monotonic',lambda:100.)
    seen=[]
    def call(client,**kwargs): seen.append((budget.current_model_deadline(),kwargs)); return '{}'
    monkeypatch.setattr(llm_utils,'call_analysis_model',call)
    if remaining is None: committee._call_llm(system='s',prompt='p')
    else:
        with budget.model_budget(100+remaining): committee._call_llm(system='s',prompt='p')
    assert seen[0][0]==(9100 if remaining is None else 100+remaining)
    assert 'max_call_seconds' not in seen[0][1]


def test_extended_plan_transport_shared_deadline_kills_real_stalled_child(monkeypatch,tmp_path):
    import subprocess,sys,time
    from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient
    from tradingagents.strategies.data_sources.regulation_graphics import prepare_graphics_recognition
    from test_regulation_graphics import assets
    request=prepare_graphics_recognition(assets()); children=[]
    started=tmp_path/'started'
    actual_popen=subprocess.Popen
    def stalled_worker(*args,**kwargs):
        child=actual_popen([sys.executable,'-c',
            'import pathlib,sys,time; sys.stdin.buffer.read(); pathlib.Path(sys.argv[1]).write_text("started"); time.sleep(30)',str(started)],**kwargs)
        children.append(child);return child
    monkeypatch.setattr(budget.subprocess,'Popen',stalled_worker)
    client=ChatGPTPlanClient(auth_path='/not/read',profile_identity='fixture')
    now=time.monotonic()
    with budget.model_budget(now+1),pytest.raises(budget.ModelDeadlineExceeded):
        llm_utils.call_analysis_model(client,model='gpt-6-luna',system=request.system,prompt=request.user,
            max_tokens=4096,temperature=0.,effort='high',document_inputs=request.document_inputs,max_call_seconds=240)
    assert started.exists(), 'real child must consume startup/request then stall'
    assert len(children)==1 and children[0].poll() is not None and children[0].returncode<0
    assert time.monotonic()-now<2


def test_late_decoded_response_is_retained_but_unusable(monkeypatch):
    from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient
    from tradingagents.strategies.data_sources.regulation_graphics import prepare_graphics_recognition
    from test_regulation_graphics import assets
    request=prepare_graphics_recognition(assets()); now=[100.]; observed=[]
    monkeypatch.setattr(budget.time,'monotonic',lambda:now[0])
    def transport(payload,timeout): now[0]=136.;return {'text':'{}','provenance':{}}
    monkeypatch.setattr(llm_utils,'bounded_transport',transport)
    client=ChatGPTPlanClient(auth_path='/not/read',profile_identity='fixture')
    with budget.model_budget(135),pytest.raises(budget.ModelDeadlineExceeded):
        llm_utils.call_analysis_model(client,model='gpt-6-luna',system=request.system,prompt=request.user,
            max_tokens=4096,temperature=0.,effort='high',document_inputs=request.document_inputs,
            max_call_seconds=240,response_observer=observed.append)
    assert observed==['{}']


def test_explicit_policy_retains_documented_zero_temperature_default():
    candidate=config();candidate['autoresearch'].pop('llm_temperature')
    assert budget.model_phase_budget_seconds(candidate)==9000
    assert budget.graphics_call_seconds(candidate)==240


@pytest.mark.parametrize("outer", [None,150.])
def test_daily_four_horizons_and_later_staging_share_one_policy_deadline(tmp_path,monkeypatch,outer):
    from datetime import date,datetime,timezone
    from tradingagents.strategies.orchestration import daily_pipeline,source_inputs,decision_clock
    now=[100.];seen=[]
    monkeypatch.setattr(budget.time,'monotonic',lambda:now[0])
    engine=SimpleNamespace(_price_cache={},pending_late_signals=lambda *args:[],
                           state=SimpleNamespace(save_regime_snapshot=lambda *args,**kwargs:None))
    cohorts=[{'config':SimpleNamespace(horizon=h,name=h),'engine':engine,
              'executor':SimpleNamespace(validated_execution_reference_bars=lambda *args:{})}
             for h in ('14d','30d','3m','6m')]
    def screen(*args):
        seen.append(budget.current_model_deadline());now[0]+=10;return [],{},[]
    owner=SimpleNamespace(cohorts=cohorts,_base_config=config(),_screen_for_horizon=screen)
    state=daily_pipeline.DailyRunState(owner,'2026-10-15',date(2026,10,15),datetime.now(timezone.utc),
                                     epoch_id='epoch',valid=cohorts)
    monkeypatch.setattr(source_inputs,'daily_source_store',lambda *args:(SimpleNamespace(load_frozen=lambda *args:{}),'identity'))
    monkeypatch.setattr(daily_pipeline,'_persist_screen_health',lambda state:None)
    expected=9100 if outer is None else outer
    if outer is None:
        assert daily_pipeline.run_horizon_screening(state) is None
    else:
        with budget.model_budget(outer):
            assert daily_pipeline.run_horizon_screening(state) is None
    assert seen==[expected]*4 and state.model_deadline==expected
    state.valid=[]
    class StopAfterDeadline(BaseException):pass
    def stop(*args,**kwargs):
        assert kwargs['deadline']==expected
        raise StopAfterDeadline
    monkeypatch.setattr(decision_clock,'prepare_decision_inputs',stop)
    with pytest.raises(StopAfterDeadline): daily_pipeline.stage_daily_results(state)
    assert state.model_deadline==expected


@pytest.mark.parametrize('existing,outer,expected', [(None,None,9100),(200,150,150),(125,150,125),(200,90,90)])
def test_staging_only_entry_starts_policy_deadline(monkeypatch,existing,outer,expected):
    from datetime import date
    from tradingagents.strategies.orchestration import daily_pipeline,decision_clock
    monkeypatch.setattr(budget.time,'monotonic',lambda:100.)
    state=SimpleNamespace(horizon_signals={},candidate_bar_quarantines=[],volatility_quarantines=[],
        governed_reference_bars={},candidate_reference_bars={},owner=SimpleNamespace(_base_config=config()),
        model_deadline=existing,session=date(2026,10,15),shared_data={},source_digest='digest',new_entry_eligibility=None)
    monkeypatch.setattr(daily_pipeline,'_all_signals',lambda state:[])
    class StopAfterDeadline(BaseException):pass
    def stop(*args,**kwargs):
        assert kwargs['deadline']==expected
        raise StopAfterDeadline
    monkeypatch.setattr(decision_clock,'prepare_decision_inputs',stop)
    with pytest.raises(StopAfterDeadline):
        if outer is None: daily_pipeline.stage_daily_results(state)
        else:
            with budget.model_budget(outer): daily_pipeline.stage_daily_results(state)
    assert state.model_deadline==expected
