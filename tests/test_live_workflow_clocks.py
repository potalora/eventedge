"""Scoped observed clocks preserve native objects and exact replay identity."""
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

from tools.live_workflow import recorded_clocks as clocks, recorded_io as tape

_REAL_LOGICAL_ACTOR=clocks._logical_actor


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in clocks.CANDIDATE_MODULES:
        monkeypatch.delitem(sys.modules,name,raising=False)
    monkeypatch.setattr(tape,'_CONFIG',None)
    monkeypatch.setattr(tape,'_REPLAY_FAILURES',[])
    monkeypatch.setattr(tape,'_REPLAY_COUNTS',{})
    monkeypatch.setattr(tape,'_CONSUMED',set())
    monkeypatch.delenv('EVENTEDGE_LIVE_REPLAY_STATE',raising=False)
    monkeypatch.setattr(clocks,'_logical_actor',lambda:{'entrypoint':'unit-native-fixture.py','source_sha256':'b'*64})
    yield
    clocks.uninstall_clock_io()


def setup(tmp_path,monkeypatch,source=None):
    root=tmp_path/'repo';root.mkdir();site=Path(importlib.import_module('pandas').__file__).parents[1]
    name='tradingagents.strategies.data_sources.evidence'
    path=root/'tradingagents/strategies/data_sources/evidence.py';path.parent.mkdir(parents=True)
    path.write_text(source or 'from datetime import datetime,timezone\ndef sample(ticker):\n return datetime.now(timezone.utc)\n')
    module=ModuleType(name);module.__file__=str(path)
    exec(compile(path.read_bytes(),str(path),'exec'),module.__dict__)
    monkeypatch.setitem(sys.modules,name,module)
    cfg={'mode':'live_record','candidate_repo':str(root),'dependency_site':str(site),
        'tape_dir':str(tmp_path/'tape'),'run_identity':{'candidate_commit':'a'*40}}
    tape.configure_tape(mode=cfg['mode'],tape_dir=cfg['tape_dir'],run_identity=cfg['run_identity'],phase='daily')
    return cfg,module


def replay(cfg,tmp_path,monkeypatch):
    clocks.uninstall_clock_io();tape._CONFIG=None
    cfg={**cfg,'mode':'offline_replay'}
    monkeypatch.setenv('EVENTEDGE_LIVE_REPLAY_STATE',str(tmp_path/'cursor'))
    tape.configure_tape(mode=cfg['mode'],tape_dir=cfg['tape_dir'],run_identity=cfg['run_identity'],phase='daily')
    clocks.install_clock_io(cfg,'daily')
    return cfg


def test_live_returns_identical_native_datetime_and_replay_exact_value(tmp_path,monkeypatch):
    cfg,module=setup(tmp_path,monkeypatch);expected=datetime(2026,11,1,12,3,4,123456,tzinfo=timezone.utc)
    monkeypatch.setattr(clocks,'_call_native',lambda original,method,args,kwargs:expected)
    original=datetime
    clocks.install_clock_io(cfg,'daily')
    observed=module.sample('SPY')
    assert observed is expected and type(observed) is original
    assert isinstance(observed,module.datetime)
    assert type(module.datetime.fromisoformat(observed.isoformat())) is original
    records=tape.verify_tape(cfg['tape_dir']);assert len(records)==1 and records[0]['boundary']=='observed_clock'
    from tools.live_workflow.runner import clock_capture_coverage
    coverage=clock_capture_coverage(cfg['tape_dir'],records)
    assert coverage['status']=='complete' and coverage['clock_events']==1
    assert coverage['capture_failures']=={}
    replay(cfg,tmp_path,monkeypatch)
    monkeypatch.setattr(clocks,'_call_native',lambda *args:pytest.fail('offline called native clock'))
    assert module.sample('SPY')==expected
    tape.assert_replay_complete()
    assert datetime is original


def test_unrecorded_ticker_or_timezone_never_reuses_another_clock(tmp_path,monkeypatch):
    cfg,module=setup(tmp_path,monkeypatch);clocks.install_clock_io(cfg,'daily');module.sample('SPY')
    replay(cfg,tmp_path,monkeypatch)
    with pytest.raises(tape.TapeError):module.sample('VT')
    with pytest.raises(tape.TapeError,match='recorded replay failures'):tape.assert_replay_complete()


def test_unreviewed_phase_installs_nothing(tmp_path,monkeypatch):
    cfg,module=setup(tmp_path,monkeypatch);original=module.datetime
    result=clocks.install_clock_io(cfg,'controller')
    assert result['installed'] is False and module.datetime is original
    assert not tape.verify_tape(cfg['tape_dir'])


def test_imported_acquisition_alias_uses_scoped_evidence_clock(tmp_path,monkeypatch):
    cfg,module=setup(tmp_path,monkeypatch,'from datetime import datetime,timezone\ndef acquisition_time():\n return datetime.now(timezone.utc).isoformat()\n')
    alias=module.acquisition_time;clocks.install_clock_io(cfg,'daily')
    observed=alias();replay(cfg,tmp_path,monkeypatch)
    assert alias()==observed


def test_module_local_pandas_proxy_preserves_global_extension_and_nanoseconds(tmp_path,monkeypatch):
    import pandas as pd
    cfg,module=setup(tmp_path,monkeypatch,'import pandas as pd\ndef sample(ticker):\n return pd.Timestamp.now("UTC")\n')
    original=pd.Timestamp;expected=pd.Timestamp('2026-11-01T12:03:04.123456789Z')
    monkeypatch.setattr(clocks,'_call_native',lambda *args:expected)
    clocks.install_clock_io(cfg,'daily')
    assert module.sample('SPY') is expected and pd.Timestamp is original
    assert isinstance(expected,module.pd.Timestamp)
    assert type(module.pd.Timestamp('2026-11-01')) is original
    replay(cfg,tmp_path,monkeypatch)
    value=module.sample('SPY');assert value.value==expected.value and type(value) is original


def test_same_semantic_key_overlap_is_unqualified_without_changing_live_value(tmp_path,monkeypatch):
    cfg,module=setup(tmp_path,monkeypatch);expected=datetime(2026,11,1,tzinfo=timezone.utc)
    monkeypatch.setattr(clocks,'_call_native',lambda *args:expected)
    monkeypatch.setattr(clocks,'_acquire_semantic_lock',lambda key:None)
    clocks.install_clock_io(cfg,'daily')
    assert module.sample('SPY') is expected
    record=tape.verify_tape(cfg['tape_dir'])[0]
    assert record['failure']['reason']=='clock_context_overlap_unqualified'
    replay(cfg,tmp_path,monkeypatch)
    with pytest.raises(tape.TapeError):module.sample('SPY')


def test_module_origin_escape_is_rejected_before_patch(tmp_path,monkeypatch):
    cfg,module=setup(tmp_path,monkeypatch);module.__file__=str(tmp_path/'elsewhere.py')
    with pytest.raises(tape.TapeError,match='clock_module_origin'):clocks.install_clock_io(cfg,'daily')


def test_local_datetime_import_is_declared_unsupported(tmp_path,monkeypatch):
    cfg,module=setup(tmp_path,monkeypatch,'def sample(ticker):\n from datetime import datetime,timezone\n return datetime.now(timezone.utc)\n')
    result=clocks.install_clock_io(cfg,'daily')
    assert any(row['module']==module.__name__ for row in result['unsupported_local_imports'])


def test_source_store_daily_and_volatility_identities_are_recordable(tmp_path,monkeypatch):
    source='from datetime import datetime,timezone\ndef sample(identity):\n return datetime.now(timezone.utc)\n'
    cfg,module=setup(tmp_path,monkeypatch,source)
    monkeypatch.delitem(sys.modules,module.__name__)
    name='tradingagents.strategies.orchestration.source_inputs';module.__name__=name
    monkeypatch.setitem(sys.modules,name,module)
    # Function globals hold the same renamed namespace, binding the actual store identity contract.
    clocks.install_clock_io(cfg,'daily')
    for identity in ({'generation':'gen_001','session':'2026-10-30','commit':'a'*40,'configuration':'b'*64},
            {'generation':'gen_001','session':'2026-10-30','commit':'a'*40,'configuration':'b'*64,'purpose':'staging-volatility-v1'}):
        module.sample(identity)
    assert all(row['failure'] is None for row in tape.verify_tape(cfg['tape_dir']))


def test_clock_context_does_not_evaluate_owner_properties(tmp_path,monkeypatch):
    source='from datetime import datetime,timezone\nclass Owner:\n @property\n def ticker(self):\n  raise AssertionError("observer evaluated a property")\n def sample(self):\n  return datetime.now(timezone.utc)\n'
    cfg,module=setup(tmp_path,monkeypatch,source);clocks.install_clock_io(cfg,'daily')
    module.Owner().sample()
    assert tape.verify_tape(cfg['tape_dir'])[0]['failure'] is None


def test_pandas_native_microsecond_unit_is_preserved_offline(tmp_path,monkeypatch):
    import pandas as pd
    cfg,module=setup(tmp_path,monkeypatch,'import pandas as pd\ndef sample(ticker):\n return pd.Timestamp.now("UTC")\n')
    expected=pd.Timestamp('2026-11-01T12:03:04.123456Z').as_unit('us')
    monkeypatch.setattr(clocks,'_call_native',lambda *args:expected)
    clocks.install_clock_io(cfg,'daily');assert module.sample('SPY') is expected
    replay(cfg,tmp_path,monkeypatch)
    observed=module.sample('SPY');assert observed.unit==expected.unit and observed==expected


def test_clock_budget_exhaustion_is_durably_unqualified_without_changing_live(tmp_path,monkeypatch):
    cfg,module=setup(tmp_path,monkeypatch);clocks.install_clock_io(cfg,'daily')
    monkeypatch.setattr(clocks,'MAX_CLOCK_EVENTS',0)
    observed=module.sample('SPY');assert type(observed) is datetime
    receipt=json.loads(next(Path(cfg['tape_dir']).glob('.clock-runtime-*.json')).read_bytes())
    assert receipt['capture_failures']['clock_event_limit']==1


@pytest.mark.parametrize('value',[float('nan'),float('inf'),float('-inf')])
def test_nonfinite_time_values_are_not_replayable(value):
    with pytest.raises(tape.TapeError):clocks._encode(value,'time')
    with pytest.raises(tape.TapeError):clocks._decode({'kind':'time','hex':value.hex()},'time')


def test_uninstall_resets_real_logical_actor_cache(tmp_path,monkeypatch):
    cfg,module=setup(tmp_path,monkeypatch)
    actor=_REAL_LOGICAL_ACTOR;main=sys.modules['__main__']
    first=Path(cfg['candidate_repo'])/'first.py';first.write_text('# first\n')
    second=Path(cfg['candidate_repo'])/'second.py';second.write_text('# second\n')
    monkeypatch.setattr(clocks,'_CONFIG',{'candidate_repo':Path(cfg['candidate_repo'])})
    monkeypatch.setattr(main,'__file__',str(first),raising=False)
    assert actor()['entrypoint']=='first.py'
    clocks.uninstall_clock_io()
    assert clocks._ACTOR is None
    monkeypatch.setattr(clocks,'_CONFIG',{'candidate_repo':Path(cfg['candidate_repo'])})
    monkeypatch.setattr(main,'__file__',str(second))
    assert actor()['entrypoint']=='second.py'


def test_distinct_source_callers_without_public_args_keep_clock_identity(tmp_path,monkeypatch):
    cfg,module=setup(tmp_path,monkeypatch,'from datetime import datetime,timezone\ndef acquisition_time():\n return datetime.now(timezone.utc)\n')
    caller_name='tradingagents.strategies.data_sources.openbb_source'
    path=Path(cfg['candidate_repo'])/'tradingagents/strategies/data_sources/openbb_source.py'
    path.write_text('def first():\n return acquisition_time()\ndef second():\n return acquisition_time()\n')
    caller=ModuleType(caller_name);caller.__file__=str(path);caller.acquisition_time=module.acquisition_time
    exec(compile(path.read_bytes(),str(path),'exec'),caller.__dict__);monkeypatch.setitem(sys.modules,caller_name,caller)
    clocks.install_clock_io(cfg,'daily');first=caller.first();second=caller.second()
    records=tape.verify_tape(cfg['tape_dir']);assert len({row['request_sha256'] for row in records})==2
    replay(cfg,tmp_path,monkeypatch)
    # Reverse order is still unambiguous by source caller metadata.
    assert caller.second()==second and caller.first()==first
    tape.assert_replay_complete()


def test_actual_yfinance_complementary_request_is_identical_in_subprocess(tmp_path):
    import subprocess
    import pandas as pd
    tool_root=Path(clocks.__file__).parents[2];site=Path(pd.__file__).parents[1]
    candidate=tmp_path/'candidate';candidate.mkdir();script=candidate/'clock_smoke.py'
    script.write_text('''import sys,os,json,socket\nfrom pathlib import Path\nfrom types import SimpleNamespace\nsys.path[:0]=[sys.argv[1],sys.argv[2]]\nfrom tools.live_workflow import recorded_clocks as clocks,recorded_io as tape\nimport curl_cffi\ndef denied(*args,**kwargs):raise AssertionError("real network forbidden")\nsocket.socket.connect=denied;curl_cffi.Curl.perform=denied;curl_cffi.AsyncCurl.add_handle=denied\nimport pandas as pd\noriginal=pd.Timestamp\nroot=Path(__file__).parent\ncfg={'mode':'live_record','candidate_repo':str(root),'dependency_site':sys.argv[2],'tape_dir':str(root/'tape'),'run_identity':{'candidate_commit':'a'*40}}\ntape.configure_tape(mode=cfg['mode'],tape_dir=cfg['tape_dir'],run_identity=cfg['run_identity'],phase='daily')\nclocks.install_clock_io(cfg,'daily')\nfrom yfinance.scrapers.quote import Quote\nurls=[]\nclass Data:\n def cache_get(self,*,url):\n  urls.append(url);return SimpleNamespace(text='{"timeseries":{"result":[]}}')\ndef run():\n q=Quote(Data(),'SPY');q._info={};q._fetch_info=lambda:None;q._fetch_complementary()\nrun();first=urls[-1]\nassert pd.Timestamp is original\nclocks.uninstall_clock_io();tape._CONFIG=None\nos.environ['EVENTEDGE_LIVE_REPLAY_STATE']=str(root/'cursor');cfg['mode']='offline_replay'\ntape.configure_tape(mode=cfg['mode'],tape_dir=cfg['tape_dir'],run_identity=cfg['run_identity'],phase='daily')\nclocks.install_clock_io(cfg,'daily')\ndef native_clock_forbidden(*args):raise AssertionError('offline native clock called')\nclocks._call_native=native_clock_forbidden\nrun();assert urls[-1]==first;tape.assert_replay_complete()\ncoverage=clocks._coverage();assert 'yfinance.scrapers.quote' in coverage['modules']\nassert coverage['capture_failures']=={}\nprint(json.dumps({'request_identical':True,'clock_events':len(tape.verify_tape(cfg['tape_dir']))}))\n''')
    result=subprocess.run([sys.executable,'-I','-S',str(script),str(tool_root),str(site)],
        env={'PATH':'/usr/bin:/bin','HOME':str(tmp_path),'PYTHONDONTWRITEBYTECODE':'1','TZ':'UTC'},
        capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'request_identical':True,'clock_events':2}


def test_exact_candidate_regime_and_source_store_codec_in_subprocess(tmp_path):
    import os
    import subprocess
    import pandas as pd
    candidate=Path(os.environ.get('EVENTEDGE_CLOCK_CANDIDATE_ROOT',
        '/home/pedro/.codex/worktrees/finra-query-api/eventedge'))
    if not (candidate/'tradingagents/strategies/orchestration/source_inputs.py').is_file():
        pytest.skip('exact candidate source is supplied separately from the acceptance tool')
    site=Path(pd.__file__).parents[1];tool_root=Path(clocks.__file__).parents[2]
    script=tmp_path/'candidate_clock_smoke.py'
    script.write_text('''import sys,os,json,socket\nfrom pathlib import Path\nfrom datetime import datetime,date,timezone\nsys.path[:0]=[sys.argv[1],sys.argv[2]]\nfrom tools.live_workflow import recorded_clocks as clocks,recorded_io as tape\nsys.path.insert(0,sys.argv[3])\nimport curl_cffi\ndef denied(*args,**kwargs):raise AssertionError('real network forbidden')\nsocket.socket.connect=denied;curl_cffi.Curl.perform=denied;curl_cffi.AsyncCurl.add_handle=denied\nimport pandas as pd\nfrom tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine\nfrom tradingagents.strategies.orchestration.source_inputs import SourceInputStore\nfrom tradingagents.strategies.orchestration.shared_execution import _store as shared_execution_store\nfrom types import SimpleNamespace\nroot=Path(__file__).parent\ncfg={'mode':'live_record','candidate_repo':sys.argv[3],'dependency_site':sys.argv[2],'tape_dir':str(root/'tape'),'run_identity':{'candidate_commit':'a'*40}}\n# This is a component fixture, not a claim that its driver is a native CLI.\nclocks._logical_actor=lambda:{'entrypoint':'unit-native-method-fixture','source_sha256':'b'*64}\ntape.configure_tape(mode=cfg['mode'],tape_dir=cfg['tape_dir'],run_identity=cfg['run_identity'],phase='daily')\nclocks.install_clock_io(cfg,'daily')\ndef stage(name):\n engine=object.__new__(MultiStrategyEngine);engine.ar_config={'horizon':'30d'}\n data={'yfinance':{'vix':pd.DataFrame({'Close':[20.0]})},'fred':{'BAMLH0A0HYM2':pd.Series([4.0]),'T10Y2Y':pd.Series([0.5])}}\n regime=engine._build_regime_model(data)\n value={'datetime':datetime(2026,11,1,tzinfo=timezone.utc),'date':date(2026,11,1),'timestamp':pd.Timestamp('2026-11-01T00:00:00.123456789Z')}\n restored=SourceInputStore.decode(SourceInputStore.encode(value))\n assert type(restored['datetime']) is datetime and type(restored['date']) is date and type(restored['timestamp']) is pd.Timestamp\n store=SourceInputStore(root/name/'cache',accepted_dir=root/name/'accepted')\n identity={'generation':'gen_001','session':'2026-10-30','commit':'a'*40,'configuration':'c'*64}\n assert store.freeze(identity,{'native_codec':True})=={'native_codec':True}\n owner=SimpleNamespace(_base_config={'autoresearch':{'state_dir':str(root/name/'shared-state'),'source_cache_dir':str(root/name/'shared-cache')}},_metric_epoch_context=SimpleNamespace(generation_id='gen_001',generation_commit='a'*40))\n shared,shared_identity=shared_execution_store(owner,date(2026,10,30))\n assert shared_identity['purpose']=='shared-execution-v1'\n assert shared.freeze(shared_identity,{'real_shared_store':True},acquired_at=value['datetime'])=={'real_shared_store':True}\n assert shared.load_frozen(shared_identity)=={'real_shared_store':True}\n return regime\nfirst=stage('first');clocks.uninstall_clock_io();tape._CONFIG=None\nos.environ['EVENTEDGE_LIVE_REPLAY_STATE']=str(root/'cursor');cfg['mode']='offline_replay'\ntape.configure_tape(mode=cfg['mode'],tape_dir=cfg['tape_dir'],run_identity=cfg['run_identity'],phase='daily')\nclocks.install_clock_io(cfg,'daily')\ndef native_clock_forbidden(*args):raise AssertionError('offline native clock called')\nclocks._call_native=native_clock_forbidden\nsecond=stage('second');assert first==second;tape.assert_replay_complete()\nassert clocks._coverage()['capture_failures']=={}\nprint(json.dumps({'regime_identical':True,'native_codec_types':True,'clock_events':len(tape.verify_tape(cfg['tape_dir']))}))\n''')
    result=subprocess.run([sys.executable,'-I','-S',str(script),str(tool_root),str(site),str(candidate)],
        env={'PATH':'/usr/bin:/bin','HOME':str(tmp_path),'PYTHONDONTWRITEBYTECODE':'1','TZ':'UTC'},
        capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout)
    assert value['regime_identical'] is True and value['native_codec_types'] is True and value['clock_events']>=3


def test_two_logical_entrypoints_cannot_cross_consume_clock_records(tmp_path):
    import os
    import subprocess
    import pandas as pd
    site=Path(pd.__file__).parents[1];tool_root=Path(clocks.__file__).parents[2]
    candidate=tmp_path/'candidate';candidate.mkdir()
    source=candidate/'evidence.py';source.write_text('from datetime import datetime,timezone\ndef sample(ticker):\n return datetime.now(timezone.utc)\n')
    code='''import sys,os,json\nfrom pathlib import Path\nfrom types import ModuleType\nsys.path[:0]=[sys.argv[1],sys.argv[2]]\nfrom tools.live_workflow import recorded_clocks as clocks,recorded_io as tape\nroot=Path(__file__).parent;mode=sys.argv[3]\nmodule=ModuleType('tradingagents.strategies.data_sources.evidence');module.__file__=str(root/'evidence.py')\nexec(compile((root/'evidence.py').read_bytes(),module.__file__,'exec'),module.__dict__);sys.modules[module.__name__]=module\ncfg={'mode':mode,'candidate_repo':str(root),'dependency_site':sys.argv[2],'tape_dir':str(root/'tape'),'run_identity':{'candidate_commit':'a'*40}}\nif mode=='offline_replay':os.environ['EVENTEDGE_LIVE_REPLAY_STATE']=str(root/'cursor')\ntape.configure_tape(mode=mode,tape_dir=cfg['tape_dir'],run_identity=cfg['run_identity'],phase='daily');clocks.install_clock_io(cfg,'daily')\nif mode=='live_record':\n module.sample('SPY');print(json.dumps({'recorded':True}))\nelse:\n try:module.sample('SPY')\n except tape.TapeError:print(json.dumps({'different_actor_blocked':True}))\n else:raise AssertionError('cross-actor clock reuse')\n'''
    first=candidate/'first_actor.py';second=candidate/'second_actor.py';first.write_text(code);second.write_text(code)
    env={'PATH':'/usr/bin:/bin','HOME':str(tmp_path),'PYTHONDONTWRITEBYTECODE':'1','TZ':'UTC'}
    live=subprocess.run([sys.executable,'-I','-S',str(first),str(tool_root),str(site),'live_record'],env=env,capture_output=True,text=True,timeout=30)
    assert live.returncode==0,live.stderr
    offline=subprocess.run([sys.executable,'-I','-S',str(second),str(tool_root),str(site),'offline_replay'],env=env,capture_output=True,text=True,timeout=30)
    assert offline.returncode==0,offline.stderr
    assert json.loads(offline.stdout)=={'different_actor_blocked':True}


def test_new_timezone_does_not_reuse_clock_from_different_timezone(tmp_path,monkeypatch):
    from zoneinfo import ZoneInfo
    cfg,module=setup(tmp_path,monkeypatch,'from datetime import datetime\ndef sample(ticker,tz):\n return datetime.now(tz)\n')
    clocks.install_clock_io(cfg,'daily');module.sample('SPY',timezone.utc)
    replay(cfg,tmp_path,monkeypatch)
    with pytest.raises(tape.TapeError):module.sample('SPY',ZoneInfo('America/New_York'))
    assert tape._REPLAY_FAILURES


@pytest.mark.parametrize('extra',[
    {'purpose':'shared-execution-v1'},
    {'purpose':'prospective-decision-inputs-v1'},
    {'purpose':'research-historical-decision-inputs-v1'},
    {'policy':'reference_session_activity_v1'},
    {'purpose':'research-screening-v1','epoch_id':'gen_001-2026-10-30-'+'a'*16,
        'research_resilience_policy':'dependency_isolation_v1','screening_binding_sha256':'b'*64},
    {'purpose':'research-screening-v1','epoch_id':'gen_001-2026-10-29-'+'a'*16,
        'research_resilience_policy':'dependency_isolation_v1','screening_binding_sha256':'b'*64},
])
def test_all_reviewed_frozen_source_identity_contracts_are_exact(extra):
    identity={'generation':'gen_001','session':'2026-10-30','commit':'a'*40,'configuration':'c'*64,**extra}
    assert clocks._source_identity(identity)==tape._digest(tape._canonical(identity))


@pytest.mark.parametrize('extra',[
    {'purpose':'research-screening-v1','epoch_id':'gen_001-2026-11-02-'+'a'*16,
        'research_resilience_policy':'dependency_isolation_v1','screening_binding_sha256':'b'*64},
    {'purpose':'research-screening-v1','epoch_id':'gen_001-2026-02-30-'+'a'*16,
        'research_resilience_policy':'dependency_isolation_v1','screening_binding_sha256':'b'*64},
    {'purpose':'unknown-future-purpose'},
    {'policy':'unknown-policy'},
    {'purpose':'research-screening-v1'},
    {'purpose':'research-screening-v1','epoch_id':'gen_002-2026-10-30-'+'a'*16,
        'research_resilience_policy':'dependency_isolation_v1','screening_binding_sha256':'b'*64},
    {'purpose':'research-screening-v1','epoch_id':'gen_001-2026-10-30-'+'a'*16,
        'research_resilience_policy':'other-policy','screening_binding_sha256':'b'*64},
])
def test_unknown_or_cross_bound_frozen_identity_is_not_relaxed(extra):
    identity={'generation':'gen_001','session':'2026-10-30','commit':'a'*40,'configuration':'c'*64,**extra}
    with pytest.raises(tape.TapeError):clocks._source_identity(identity)


def test_unsupported_identity_retains_safe_code_diagnostic_without_frame_values(tmp_path,monkeypatch):
    source='from datetime import datetime,timezone\ndef sample(identity):\n return datetime.now(timezone.utc)\n'
    cfg,module=setup(tmp_path,monkeypatch,source)
    monkeypatch.delitem(sys.modules,module.__name__)
    name='tradingagents.strategies.orchestration.source_inputs';module.__name__=name
    monkeypatch.setitem(sys.modules,name,module)
    clocks.install_clock_io(cfg,'daily')
    module.sample({'generation':'gen_001','session':'2026-10-30','commit':'a'*40,
        'configuration':'b'*64,'purpose':'private-sentinel-unknown'})
    record=tape.verify_tape(cfg['tape_dir'])[0]
    assert record['failure']['reason']=='observed_clock_capture_unsupported'
    assert record['failure']['diagnostic_reason']=='unsupported_source_clock_identity'
    assert record['public_request']['diagnostic_callsite']['function']=='sample'
    assert record['public_request']['diagnostic_callsite']['line']==3
    assert 'private-sentinel' not in json.dumps(record)
