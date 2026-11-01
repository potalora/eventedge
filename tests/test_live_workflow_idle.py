"""Idle guard regression tests isolate children; they are not real timer evidence."""
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tools.live_workflow import recorded_io, runner


def implementation():
    assert importlib.util.find_spec('tools.live_workflow.idle') is not None, 'idle helper missing'
    return importlib.import_module('tools.live_workflow.idle')


@pytest.fixture
def campaign(tmp_path, monkeypatch):
    root=tmp_path/'campaign';root.mkdir(mode=0o700)
    for name in ('logs','repo','home','tmp','cache','repo/data/generations/gen_001'):
        (root/name).mkdir(mode=0o700,parents=True,exist_ok=True)
    state=root/'repo/data/generations/gen_001';profile=state/'generation-config.json'
    runner.private_json(profile,{'configured':'only'})
    contract={'candidate_commit':'a'*40,'profile_sha256':runner.sha(profile)}
    row={'gen_id':'gen_001','state_dir':str(state),'config_file':str(profile)}
    runner.private_json(root/'contract.json',contract)
    identity={'candidate_commit':contract['candidate_commit']}
    runner.private_json(root/'hook-config.json',{'run_identity':identity})
    boundary=root/'boundary.json';runner.private_json(boundary,{'live':'reviewed'})
    calls=[];checks=[];controls={'mutation':None,'window_before':True,'window_after':True,'stale':False,'native_code':0,
        'projection':'valid'}
    monkeypatch.setattr(runner,'verify_contract',lambda path:(checks.append('contract') or (contract,row)))
    monkeypatch.setattr(runner,'require_boundary',lambda path,receipt:(checks.append('boundary') or runner.sha(receipt)))
    monkeypatch.setattr(recorded_io,'_CONFIG',None)
    recorded_io.configure_tape(mode='live_record',tape_dir=root/'tape',run_identity=identity,phase='controller')
    def process(argv, source, *, env, label, timeout):
        calls.append({'argv':argv,'env':env,'label':label,'timeout':timeout})
        started=datetime.now(timezone.utc).isoformat()
        code=0;console=b'controlled regression child output\n'
        if 'idle-window' in argv:
            output=Path(argv[argv.index('--output-dir')+1]);role=output.name
            now=datetime.now(timezone.utc)
            observed=now-timedelta(seconds=121) if controls['stale'] else now
            runner.private_json(output/'idle-window.json',{'observed_at':observed.isoformat(),
                'exchange_date':observed.astimezone(ZoneInfo('America/New_York')).date().isoformat(),
                'ordinary_idle_expected':controls['window_'+role]})
        else:
            directory=root/'repo/data/runtime_recovery';directory.mkdir(mode=0o700,parents=True,exist_ok=True)
            recorded_io._private_write(directory/'journal.json',b'{"incidents":{}}',replace=True)
            mode=controls['projection'];status=directory/'status.json'
            projection={'updated_at':datetime.now(timezone.utc).isoformat(),'status':'healthy',
                'controller_reason_code':None,'incidents':[]}
            if mode=='stale':projection['updated_at']=(datetime.fromisoformat(started)-timedelta(seconds=1)).isoformat()
            if mode=='future':projection['updated_at']=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()
            if mode=='naive':projection['updated_at']=datetime.now().isoformat()
            if mode=='unhealthy':projection['status']='operator_required'
            if mode=='reason':projection['controller_reason_code']='state_corrupt'
            if mode=='incident':projection['incidents']=[{'unexpected':True}]
            if mode=='missing':status.unlink(missing_ok=True)
            elif mode=='malformed':recorded_io._private_write(status,b'{invalid',replace=True)
            elif mode!='lock_busy':recorded_io._private_write(status,json.dumps(projection).encode(),replace=True)
            if mode!='lock_busy':console+=json.dumps(projection).encode()+b'\n\n'
            if mode=='console_mismatch':console=b'{"different":"projection"}\n'
            if mode=='console_missing':console=b'controller returned without a projection\n'
            if controls['mutation']:controls['mutation']()
            code=controls['native_code']
        log=root/'logs'/(label+'.log');log.write_bytes(console);log.chmod(0o600)
        receipt={'argv':argv,'returncode':code,'failure':None,'started_at':started,
            'finished_at':datetime.now(timezone.utc).isoformat(),'console_sha256':runner.sha(log)}
        runner.private_json(root/(label+'-process.json'),receipt)
        if code:raise runner.AcceptanceError('controlled_child_failed')
        return receipt
    monkeypatch.setattr(runner,'run_process',process)
    args=SimpleNamespace(root=str(root),boundary_receipt=str(boundary),attempt_id='timer-001')
    return root,args,contract,row,controls,calls,checks


def test_idle_uses_real_controller_shape_without_credentials_or_session_override(campaign,monkeypatch):
    root,args,contract,row,controls,calls,checks=campaign
    monkeypatch.setenv('FRED_API_KEY','must-not-inherit');monkeypatch.setenv('OPENAI_API_KEY','must-not-inherit')
    result=implementation().run_idle(args)
    assert result['status']=='pass' and result['reason']=='ordinary_non_session_idle_observed'
    assert result['attempt_id']==args.attempt_id and result['root']==str(root)
    assert result['source_model_event_delta']=={'source_events':0,'model_events':0}
    assert result['generation_state_unchanged'] and result['journal_incidents_empty'] and result['identity_verified']
    assert result['native_idle_projection_verified'] is True
    assert result['unfinished_reservations']==0 and result['credential_env_names']==[]
    assert len(calls)==3 and all(call['timeout']==120 for call in calls)
    assert calls[1]['argv']==[str(root/'repo/.venv/bin/python'),str(root/'repo/scripts/recover_runtime.py'),'--repo-root',str(root/'repo')]
    assert all('--session' not in c['argv'] and '--prospective-session' not in c['argv'] for c in calls)
    assert all(not (set(c['env']) & (runner.CREDENTIAL_NAMES|{'OPENAI_API_KEY','ANTHROPIC_API_KEY','FAKETIME'})) for c in calls)
    assert checks.count('contract')>=2 and checks.count('boundary')>=2
    output=root/'idle-ticks'/args.attempt_id
    for relative,digest in result['artifact_sha256'].items():assert runner.sha(root/relative)==digest
    assert result['result_sha256']==runner.digest({k:v for k,v in result.items() if k!='result_sha256'})
    assert (output/'result.json').is_file()
    assert (output/'native-status.json').read_bytes()==(root/'repo/data/runtime_recovery/status.json').read_bytes()
    assert 'repo/data/runtime_recovery/status.json' not in result['artifact_sha256']


def test_lock_busy_zero_exit_cannot_borrow_previous_idle_status(campaign):
    root,args,_,_,controls,calls,_=campaign;idle=implementation()
    first=idle.run_idle(args);assert first['status']=='pass'
    controls['projection']='lock_busy';args.attempt_id='timer-002'
    result=idle.run_idle(args)
    assert result['status']=='fail' and result['native_idle_projection_verified'] is False
    assert (root/'idle-ticks/timer-002/result.json').is_file()
    assert len(calls)==5  # No after-calendar launch after rejected native evidence.


@pytest.mark.parametrize('projection',['missing','malformed','stale','future','naive','unhealthy','reason',
    'incident','console_mismatch','console_missing'])
def test_native_exit_zero_requires_fresh_healthy_status_and_matching_final_projection(campaign,projection):
    _,args,_,_,controls,calls,_=campaign;controls['projection']=projection
    result=implementation().run_idle(args)
    assert result['status']=='fail' and result['native_idle_projection_verified'] is False
    assert len(calls)==2


def test_distinct_ticks_are_allowed_but_duplicate_identity_is_exclusive(campaign):
    root,args,_,_,_,calls,_=campaign;idle=implementation()
    first=idle.run_idle(args);first_bytes=(root/'idle-ticks/timer-001/result.json').read_bytes()
    with pytest.raises(runner.AcceptanceError):idle.run_idle(args)
    assert len(calls)==3
    args.attempt_id='timer-002';second=idle.run_idle(args)
    assert first['status']==second['status']=='pass' and len(calls)==6
    assert (root/'idle-ticks/timer-001/result.json').read_bytes()==first_bytes


@pytest.mark.parametrize('location',['before','after'])
def test_actual_candidate_calendar_must_observe_non_session_on_both_sides(campaign,location):
    root,args,_,_,controls,calls,_=campaign;controls['window_'+location]=False
    result=implementation().run_idle(args)
    assert result['status']=='fail'
    assert 'idle_non_session_required' in result['reason']
    assert len(calls)==(1 if location=='before' else 3)


def test_stale_calendar_observation_cannot_launch_controller(campaign):
    _,args,_,_,controls,calls,_=campaign;controls['stale']=True
    result=implementation().run_idle(args)
    assert result['status']=='fail' and len(calls)==1


@pytest.mark.parametrize('mutation',['state','journal','native_marker','ordinary','contract','boundary'])
def test_exit_zero_cannot_hide_state_incident_or_identity_changes(campaign,mutation):
    root,args,_,row,controls,_,_=campaign
    def alter():
        if mutation=='state':(Path(row['state_dir'])/'portfolio.db').write_bytes(b'created economic state')
        elif mutation=='journal':(root/'repo/data/runtime_recovery/journal.json').write_text('{"incidents":{"unexpected":{}}}')
        elif mutation=='native_marker':runner.private_json(root/'native-attempt.json',{'unexpected':True})
        elif mutation=='ordinary':(root/'ordinary-sessions/2026-11-02').mkdir(parents=True)
        elif mutation=='contract':(root/'contract.json').write_text('{"changed":true}')
        else:Path(args.boundary_receipt).write_text('{"changed":true}')
    controls['mutation']=alter
    assert implementation().run_idle(args)['status']=='fail'


@pytest.mark.parametrize('boundary',['source_http','model_helper','unfinished'])
def test_unexpected_source_model_or_unfinished_record_cannot_pass(campaign,boundary):
    _,args,_,_,controls,_,_=campaign
    def alter():
        event=recorded_io.reserve_event(boundary='source_http' if boundary=='unfinished' else boundary,
            operation='unexpected_idle_operation',public_request={'unwanted':True})
        if boundary!='unfinished':recorded_io.finish_event(event,status=200)
    controls['mutation']=alter
    result=implementation().run_idle(args)
    assert result['status']=='fail'


def test_failed_tick_is_retained_and_blocks_new_idle_attempt(campaign):
    root,args,_,_,controls,calls,_=campaign;controls['native_code']=9
    idle=implementation();result=idle.run_idle(args)
    assert result['status']=='fail' and (root/'idle-ticks/timer-001/result.json').exists()
    controls['native_code']=0;args.attempt_id='timer-002';before=len(calls)
    with pytest.raises(runner.AcceptanceError):idle.run_idle(args)
    assert len(calls)==before


def test_previous_log_tampering_blocks_new_attempt(campaign):
    root,args,_,_,_,calls,_=campaign;idle=implementation();result=idle.run_idle(args)
    relative=next(p for p in result['artifact_sha256'] if p.endswith('.log'))
    (root/relative).write_bytes(b'tampered')
    args.attempt_id='timer-002'
    with pytest.raises(runner.AcceptanceError):idle.run_idle(args)
    assert len(calls)==3


@pytest.mark.parametrize('marker',['state','native','ordinary','pending','attempt'])
def test_campaign_must_be_fresh_before_any_child(campaign,marker):
    root,args,_,row,_,calls,_=campaign
    if marker=='state':(Path(row['state_dir'])/'portfolio.db').write_bytes(b'existing')
    elif marker=='native':runner.private_json(root/'native-attempt.json',{})
    elif marker=='ordinary':(root/'ordinary-sessions/2026-11-02').mkdir(parents=True)
    elif marker=='attempt':
        directory=root/'repo/data/logs/run_attempts';directory.mkdir(parents=True)
        runner.private_json(directory/'old-attempt.json',{})
    else:recorded_io.reserve_event(boundary='source_http',operation='pending',public_request={})
    result=implementation().run_idle(args)
    assert result['status']=='fail' and calls==[]


@pytest.mark.parametrize('attempt',['../escape','','bad/name','x'*65])
def test_attempt_identifier_cannot_escape_private_receipt_directory(campaign,attempt):
    _,args,_,_,_,calls,_=campaign;args.attempt_id=attempt
    with pytest.raises(runner.AcceptanceError):implementation().run_idle(args)
    assert calls==[]


def test_incomplete_prior_tick_refuses_before_child(campaign):
    root,args,_,_,_,calls,_=campaign;idle=implementation()
    (root/'idle-ticks').mkdir(mode=0o700)
    (root/'idle-ticks/prior-incomplete').mkdir(mode=0o700)
    with pytest.raises(runner.AcceptanceError,match='idle_previous_tick_incomplete'):idle.run_idle(args)
    assert calls==[]


def test_history_artifact_paths_cannot_read_outside_campaign(campaign,monkeypatch):
    root,args,_,_,_,calls,_=campaign;idle=implementation();result=idle.run_idle(args)
    result['artifact_sha256']['../private-grant-do-not-read']='0'*64
    result['result_sha256']=runner.digest({k:v for k,v in result.items() if k!='result_sha256'})
    (root/'idle-ticks/timer-001/result.json').write_text(json.dumps(result))
    original=runner.sha
    def bounded_hash(path):
        assert '..' not in Path(path).parts,'outside campaign read attempted'
        return original(path)
    monkeypatch.setattr(runner,'sha',bounded_hash);args.attempt_id='timer-002'
    with pytest.raises(runner.AcceptanceError,match='idle_unsafe_artifact_path'):idle.run_idle(args)
    assert len(calls)==3
