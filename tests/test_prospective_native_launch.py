"""One-shot native controller contract; local clocks/processes, no providers."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts import recover_runtime as controller
from tradingagents.strategies.orchestration import runtime_recovery as recovery
from tradingagents.strategies.orchestration.runtime_lock import RuntimeLockBusy, RuntimeLockInvalid, runtime_lock
from test_source_reliability_pipeline import pipeline, SESSION as PIPELINE_SESSION, InterruptedWorker
from test_runtime_recovery import prospective

NOW = datetime(2026, 10, 16, 5, tzinfo=timezone.utc)
SESSION = '2026-10-15'
ROOT_CODE_IDENTITY = getattr(controller, '_root_code_identity', None)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    repo = tmp_path
    state = repo/'data/generations/gen_023'
    state.mkdir(parents=True)
    config = {'autoresearch': {'decision_clock_policy': 'prospective_next_open_v1',
        'bounded_document_decision_policy': 'verified_scoped_claims_next_open_v2'},
        'execution': {'mode': 'paper', 'execution_enabled': False}}
    profile = state/'generation-config.json'
    profile.write_text(json.dumps(config))
    row = {'gen_id': 'gen_023', 'git_commit': 'a'*40, 'status': 'active',
        'config_sha256': hashlib.sha256(profile.read_bytes()).hexdigest(), 'config_file': str(profile),
        'state_dir': str(state), 'worktree_path': str(repo/'.worktrees/gen_023'),
        'created_at': '2026-10-15T10:00:00', 'run_history': []}
    manifest = repo/'data/generations/manifest.json'
    manifest.write_text(json.dumps({'generations': [row]}))
    monkeypatch.setattr(controller, 'identity_for', lambda repo, r, session: {
        'generation_id': r['gen_id'], 'generation_commit': r['git_commit'],
        'config_sha256': r['config_sha256'], 'session': session,
        'state_dir': r['state_dir'], 'worktree_path': r['worktree_path']})
    lock = repo/'data/operational/eventedge-runtime.lock'
    monkeypatch.setattr(controller, 'canonical_runtime_lock_path', lambda repo: lock)
    monkeypatch.setattr(controller, '_root_code_identity', lambda *a: None, raising=False)
    args = dict(expected_generation=row['gen_id'], expected_commit=row['git_commit'],
        expected_profile=row['config_sha256'], expected_created=row['created_at'])
    return repo, row, config, args, lock


def accepted_report(*unused):
    return {'accounting_valid': True, 'staging_complete': True, 'evidence_complete': True,
            'sources': {}, 'source_health_failures': [], 'candidate_input_issues': []}


@pytest.mark.scheduled_acceptance
def test_retained_native_failed_staging_is_not_accepted_source_degradation():
    fixture=json.loads((Path(__file__).parent/'fixtures/native_failed_controller_report.json').read_text())
    report=fixture['report']
    assert fixture['original_sha256']=='fa6d5e982fe2739457c342ddf30500c7e4eac618b8268dc8efa0e04969b1e078'
    assert len(report['cohorts'])==16 and sum(row['staging_complete'] for row in report['cohorts'].values())==4
    assert controller._terminal_report(report)==recovery._result('requires_user','checkpoint_invalid')


def test_failed_outcome_precedes_even_complete_staging_and_source_denial():
    report=accepted_report()|{'outcome':'failed','sources':{'configuration':'c'*64,
        'unresolved':[{'source':'edgar','reason_code':'disabled_v1'}]}}
    assert controller._terminal_report(report)==recovery._result('requires_user','checkpoint_invalid')


def test_completed_sole_edgar_degradation_remains_native_terminal():
    report=accepted_report()|{'outcome':'degraded','staging_valid':True,
        'sources':{'configuration':'c'*64,'unresolved':[{'source':'edgar','reason_code':'disabled_v1'}]}}
    assert controller._terminal_report(report)==recovery._result('wait_next_session','accepted_outcome')


def test_partial_checkpoint_is_not_accepted_and_keeps_local_recovery_semantics():
    report={'accounting_valid':True,'staging_complete':False,
        'sources':{'configuration':'c'*64,'unresolved':[{'source':'edgar','http_status':403}]}}
    assert controller._terminal_report(report) is None


@pytest.mark.scheduled_acceptance
def test_failed_native_controller_outcome_blocks_next_tick_and_exit_is_nonzero(fresh,monkeypatch):
    report=json.loads((Path(__file__).parent/'fixtures/native_failed_controller_report.json').read_text())['report']
    now=datetime(2026,10,15,22,tzinfo=timezone.utc);calls=[]
    launcher=lambda *a,**kw:(calls.append(a[3]) or (1,''))
    result=controller.tick(fresh[0],now,clock=lambda:now,launcher=launcher,reporter=lambda *a:report,discover=lambda *a:[])
    assert result['status']=='operator_required' and calls==['daily']
    controller.tick(fresh[0],now,clock=lambda:now,launcher=launcher,reporter=lambda *a:report,discover=lambda *a:[])
    assert calls==['daily']
    monkeypatch.setattr(controller,'tick',lambda *a,**kw:result)
    monkeypatch.setattr('sys.argv',['recover_runtime.py','--repo-root',str(fresh[0])])
    with pytest.raises(SystemExit) as stopped:controller.main()
    assert stopped.value.code==1


@pytest.mark.scheduled_acceptance
def test_configured_profile_recovery_uses_ledger_policy_not_health_policy(pipeline,monkeypatch):
    fixture,owner,config,repo=prospective(pipeline,monkeypatch)
    ledger_policy='generation-config-'+'e'*64
    for target in (config,owner._base_config,*[c['engine'].config for c in owner.cohorts]):
        target['autoresearch'].setdefault('paper_ledger',{})['policy_id']=ledger_policy
    ledger=owner.cohorts[-1]['ledger'];complete=ledger.complete_staging
    monkeypatch.setattr(ledger,'complete_staging',lambda *a,**kw:(_ for _ in ()).throw(InterruptedWorker()))
    with pytest.raises(InterruptedWorker):owner.run_daily(PIPELINE_SESSION.isoformat())
    monkeypatch.setattr(ledger,'complete_staging',complete)
    before_calls,before_models=fixture.calls.copy(),fixture.model_calls.copy();fixture.block_all=True
    assert all(c['ledger'].committee_decision(PIPELINE_SESSION,owner._epoch_id,ledger_policy) is not None for c in owner.cohorts)
    assert all(owner._policy_id_for_horizon(c['config'].horizon)!=ledger_policy for c in owner.cohorts)
    result=recovery.recover_owner(owner,PIPELINE_SESSION,now=fixture.now)
    assert result==recovery._result('recovered','complete')
    assert all(c['ledger'].staging_completed(PIPELINE_SESSION,owner._epoch_id,ledger_policy) for c in owner.cohorts)
    assert fixture.calls==before_calls and fixture.model_calls==before_models and not fixture.blocked_calls


def once(fresh, monkeypatch, *, now=NOW, process=None, reporter=accepted_report, session=SESSION, **overrides):
    repo, row, config, args, lock = fresh
    if process is None:
        process = lambda *a, **kw: (0, '')
    monkeypatch.setattr(controller, 'bounded_process', process)
    return controller.prospective_once(repo, session, clock=lambda: now, reporter=reporter, **(args|overrides))


def test_preopen_selected_reference_owns_lease_and_durable_intent(fresh, monkeypatch):
    repo, row, config, args, lock = fresh
    def process(argv, **kwargs):
        journal = recovery.load_journal(repo/'data/runtime_recovery/journal.json')
        record = next(iter(journal['incidents'].values()))
        assert record['running'] and record['daily_started']
        assert record['launch_mode'] == 'prospective_once_v1'
        assert record['launch_intent']['generation_created_at'] == row['created_at']
        assert argv[2:] == ['--prospective-session', SESSION, '--expected-generation', row['gen_id'],
            '--expected-commit', row['git_commit'], '--expected-profile-sha256', row['config_sha256'],
            '--expected-created-at', row['created_at']]
        with pytest.raises(RuntimeLockBusy):
            with runtime_lock(lock, exclusive=True): pass
        env = kwargs['env']
        controller.validate_prospective_launch(repo, SESSION, now=NOW, environ=env, **args)
        return 0, ''
    result = once(fresh, monkeypatch, process=process)
    assert result['status'] == 'healthy'
    record = next(iter(recovery.load_journal(repo/'data/runtime_recovery/journal.json')['incidents'].values()))
    assert (record['disposition'], record['reason_code'], record['running'], record['next_attempt_at']) == ('recovered', 'complete', False, None)


@pytest.mark.parametrize('now,session', [
    (datetime(2026,10,16,13,30,tzinfo=timezone.utc), SESSION),
    (datetime(2026,10,16,14,tzinfo=timezone.utc), SESSION),
    (NOW, '2026-10-16'), (NOW, '2026-10-14'), (NOW, '2026-10-10'),
    (datetime(2026,12,26,5,tzinfo=timezone.utc), '2026-12-25'),
])
def test_wrong_or_expired_reference_never_dispatches(fresh, monkeypatch, now, session):
    calls=[]
    with pytest.raises(ValueError):
        once(fresh, monkeypatch, now=now, session=session, process=lambda *a,**kw:calls.append(a))
    assert calls == []
    assert not (fresh[0]/'data/runtime_recovery/journal.json').exists()


def test_valid_friday_reference_on_actual_weekend(fresh, monkeypatch):
    assert once(fresh, monkeypatch, now=datetime(2026,10,17,5,tzinfo=timezone.utc), session='2026-10-16')['status']=='healthy'


@pytest.mark.parametrize('fault', ['generation','commit','profile','created','malformed_created',
    'history','partial','pending','newer','multiple','historical_clock','legacy_document','live'])
def test_fresh_identity_guards_refuse_without_launch(fresh, monkeypatch, fault):
    repo,row,config,args,lock=fresh
    override={}
    if fault in {'generation','commit','profile','created','malformed_created'}:
        override['expected_'+fault.replace('malformed_','')]= {'generation':'gen_022','commit':'b'*40,
            'profile':'b'*64,'created':'not-a-date' if fault=='malformed_created' else '2026-10-15T11:00:00'}[fault.replace('malformed_','')]
    elif fault=='history': row['run_history']=[{'date':SESSION}]
    elif fault=='partial': (Path(row['state_dir'])/'run.log').write_text('partial')
    elif fault=='pending': (repo/'data/generations/.pending-start.json').write_text('{}')
    elif fault in {'newer','multiple'}:
        newer=row|{'gen_id':'gen_024','status':'paused' if fault=='newer' else 'active'}
        (repo/'data/generations/manifest.json').write_text(json.dumps({'generations':[row,newer]}))
    elif fault in {'historical_clock','legacy_document','live'}:
        if fault=='historical_clock': config['autoresearch']['decision_clock_policy']='session_close_v1'
        elif fault=='legacy_document': config['autoresearch']['bounded_document_decision_policy']='verified_scoped_claims_v1'
        else: config['execution']['execution_enabled']=True
        Path(row['config_file']).write_text(json.dumps(config));row['config_sha256']=hashlib.sha256(Path(row['config_file']).read_bytes()).hexdigest();args['expected_profile']=row['config_sha256']
    if fault not in {'newer','multiple'}: (repo/'data/generations/manifest.json').write_text(json.dumps({'generations':[row]}))
    calls=[]
    with pytest.raises(ValueError): once(fresh,monkeypatch,process=lambda *a,**kw:calls.append(a),**override)
    assert calls==[]


def test_interrupted_launch_is_retained_and_never_retried(fresh,monkeypatch):
    repo,row,config,args,lock=fresh
    def death(*a,**kw): raise SystemExit('uncertain')
    with pytest.raises(SystemExit): once(fresh,monkeypatch,process=death)
    before=(repo/'data/runtime_recovery/journal.json').read_bytes()
    with pytest.raises(ValueError): once(fresh,monkeypatch)
    assert (repo/'data/runtime_recovery/journal.json').read_bytes()==before
    calls=[]
    result=controller.tick(repo,NOW,clock=lambda:NOW,launcher=lambda *a,**kw:calls.append(a),reporter=accepted_report,discover=lambda *a:[])
    assert calls==[] and result['status']=='operator_required'


@pytest.mark.parametrize('failure',[124,1,'exception','report'])
def test_caught_failure_retains_intent_and_requires_operator(fresh,monkeypatch,failure):
    def process(*a,**kw):
        if failure=='exception': raise OSError('start uncertain')
        return (failure if isinstance(failure,int) else 0),' '
    reporter=(lambda *a: {}) if failure in {124,1,'exception'} else (lambda *a: (_ for _ in ()).throw(ValueError('bad report')))
    result=once(fresh,monkeypatch,process=process,reporter=reporter)
    assert result['status']=='operator_required'
    with pytest.raises(ValueError): once(fresh,monkeypatch)


def test_wrapper_validation_requires_inherited_exclusive_lease(fresh):
    repo,row,config,args,lock=fresh
    with pytest.raises(RuntimeLockInvalid): controller.validate_prospective_launch(repo,SESSION,now=NOW,environ={},**args)


def test_unlocked_descriptor_cannot_impersonate_parent_lease(fresh):
    repo,row,config,args,lock=fresh
    lock.parent.mkdir(parents=True)
    with lock.open('w+') as handle:
        with pytest.raises(RuntimeLockInvalid):
            controller.validate_prospective_launch(repo,SESSION,now=NOW,environ={
                'EVENTEDGE_RUNTIME_LOCK_FD':str(handle.fileno()),'EVENTEDGE_RUNTIME_LOCK_MODE':'exclusive'},**args)


def test_busy_runtime_has_no_launch_intent(fresh,monkeypatch):
    with runtime_lock(fresh[4],exclusive=True):
        with pytest.raises(RuntimeLockBusy): once(fresh,monkeypatch)
    assert not (fresh[0]/'data/runtime_recovery/journal.json').exists()


def test_calendar_rechecked_under_lease_before_intent(fresh):
    moments=iter([NOW,datetime(2026,10,16,13,30,tzinfo=timezone.utc)])
    with pytest.raises(ValueError):controller.prospective_once(fresh[0],SESSION,clock=lambda:next(moments),**fresh[3])
    assert not (fresh[0]/'data/runtime_recovery/journal.json').exists()


def test_accepted_completion_after_open_keeps_original_valid_publication(fresh,monkeypatch):
    moments=iter([NOW,NOW]+[datetime(2026,10,16,14,tzinfo=timezone.utc)]*5)
    monkeypatch.setattr(controller,'bounded_process',lambda *a,**kw:(0,''))
    result=controller.prospective_once(fresh[0],SESSION,clock=lambda:next(moments),reporter=accepted_report,**fresh[3])
    assert result['status']=='healthy'


@pytest.mark.parametrize('session',[SESSION,'2026-10-14'])
def test_any_prior_generation_incident_refuses_and_preserves_history(fresh,monkeypatch,session):
    repo,row,config,args,lock=fresh
    identity=controller.identity_for(repo,row,session);record=recovery.new_incident(identity,NOW)
    record.update(daily_started=True)
    path=repo/'data/runtime_recovery/journal.json'
    recovery.atomic_json(path,{'version':recovery.VERSION,'schema_version':1,'incidents':{recovery.incident_key(identity):record}})
    before=path.read_bytes()
    with pytest.raises(ValueError):once(fresh,monkeypatch)
    assert path.read_bytes()==before


@pytest.mark.scheduled_acceptance
def test_registered_git_worktree_profile_and_lease_end_to_end(fresh,monkeypatch):
    repo,row,config,args,lock=fresh
    def git(*argv):return subprocess.check_output(['git','-C',str(repo),*argv],text=True,stderr=subprocess.DEVNULL).strip()
    git('init');git('config','user.email','offline@example.invalid');git('config','user.name','Offline test')
    git('commit','--allow-empty','-m','fixture');commit=git('rev-parse','HEAD')
    worktree=Path(row['worktree_path']);worktree.parent.mkdir();git('worktree','add','--detach',str(worktree),commit)
    row['git_commit']=commit;args['expected_commit']=commit
    (repo/'data/generations/manifest.json').write_text(json.dumps({'generations':[row]}))
    monkeypatch.setattr(controller,'identity_for',recovery.identity_for)
    monkeypatch.setattr(controller,'_root_code_identity',ROOT_CODE_IDENTITY)
    def process(argv,**kw):
        controller.validate_prospective_launch(repo,SESSION,now=NOW,environ=kw['env'],**args)
        return 0,''
    assert once(fresh,monkeypatch,process=process)['status']=='healthy'
    (worktree/'untracked.txt').write_text('drift')
    with pytest.raises(ValueError):once(fresh,monkeypatch)


def test_wrong_root_head_or_dirty_tracked_script_is_rejected(tmp_path):
    def git(*argv):return subprocess.check_output(['git','-C',str(tmp_path),*argv],text=True,stderr=subprocess.DEVNULL).strip()
    git('init');git('config','user.email','offline@example.invalid');git('config','user.name','Offline test')
    script=tmp_path/'daily_trading.sh';script.write_text('original')
    git('add',script.name);git('commit','-m','fixture');commit=git('rev-parse','HEAD')
    ROOT_CODE_IDENTITY(tmp_path,commit)
    with pytest.raises(ValueError):ROOT_CODE_IDENTITY(tmp_path,'b'*40)
    script.write_text('drift')
    with pytest.raises(ValueError):ROOT_CODE_IDENTITY(tmp_path,commit)


@pytest.mark.parametrize('fault',['mode','intent_extra','intent_missing','created','stamp','row_extra',
    'identity_extra','retry','pending','started_false','attempt_bool'])
def test_strict_one_shot_marker_does_not_change_legacy_journal(fresh,monkeypatch,fault):
    repo=fresh[0]
    once(fresh,monkeypatch)
    path=repo/'data/runtime_recovery/journal.json';journal=json.loads(path.read_text());r=next(iter(journal['incidents'].values()))
    if fault=='mode': r['launch_mode']='anything'
    elif fault=='intent_extra': r['launch_intent']['extra']=1
    elif fault=='intent_missing': del r['launch_intent']
    elif fault=='created': r['launch_intent']['generation_created_at']='nonsense'
    elif fault=='stamp': r['launch_intent']['started_at']='2026-10-16T05:00:00'
    elif fault=='row_extra': r['untrusted']=True
    elif fault=='identity_extra': r['identity']['extra']=1
    elif fault=='retry': r['disposition']='retrying'
    elif fault=='pending': r['next_attempt_at']=NOW.isoformat()
    elif fault=='started_false':r['daily_started']=False
    else:r['attempt_count']=False
    path.write_text(json.dumps(journal))
    with pytest.raises(ValueError): recovery.load_journal(path)


def test_legacy_journal_bytes_and_other_generation_history_preserved(fresh,monkeypatch):
    repo,row,config,args,lock=fresh
    legacy=recovery.new_incident(controller.identity_for(repo,row|{'gen_id':'gen_022'},SESSION),NOW)
    recovery.finish_attempt(legacy,recovery._result('wait_next_session','missed_session'),NOW)
    path=repo/'data/runtime_recovery/journal.json';key=recovery.incident_key(legacy['identity'])
    recovery.atomic_json(path,{'version':recovery.VERSION,'schema_version':1,'incidents':{key:legacy}})
    assert recovery.load_journal(path)['incidents'][key]==legacy
    once(fresh,monkeypatch)
    assert recovery.load_journal(path)['incidents'][key]==legacy


@pytest.mark.parametrize('override',['EVENTEDGE_RUNTIME_LOCK_FD','EVENTEDGE_RUNTIME_LOCK_MODE',
    'EVENTEDGE_PYTHON','EVENTEDGE_GENERATION_CONFIG_SHA256'])
def test_dotenv_cannot_change_guarded_prospective_environment(tmp_path,override):
    repo=Path(__file__).resolve().parents[1];scripts=tmp_path/'scripts';scripts.mkdir()
    (scripts/'daily_trading.sh').write_bytes((repo/'scripts/daily_trading.sh').read_bytes())
    (tmp_path/'.env').write_text(override+'=different\n')
    calls=tmp_path/'calls';stub=tmp_path/'python'
    stub.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS"\nexit 0\n');stub.chmod(0o755)
    env=os.environ|{'EVENTEDGE_PYTHON':str(stub),'EVENTEDGE_LOG_DIR':str(tmp_path/'logs'),'CALLS':str(calls)}
    args=['--prospective-session',SESSION,'--expected-generation','gen_023','--expected-commit','a'*40,
        '--expected-profile-sha256','b'*64,'--expected-created-at','2026-10-15T10:00:00']
    result=subprocess.run(['bash',str(scripts/'daily_trading.sh'),*args],env=env,capture_output=True,text=True)
    assert result.returncode==2
    assert len(calls.read_text().splitlines())==1 and '--validate-prospective-launch' in calls.read_text()


@pytest.mark.scheduled_acceptance
def test_native_shell_propagates_selected_reference_and_reports_failure(tmp_path):
    repo=Path(__file__).resolve().parents[1]
    stub=tmp_path/'python';calls=tmp_path/'calls'
    stub.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS"\ncase "$*" in\n*--validate-prospective-launch*) exit "${VALIDATE_RC:-0}";;\n*--preflight-mode*) exit 0;;\n*run-daily*) exit 1;;\n*generate_operational_report.py*) exit 0;;\nesac\nexit 90\n');stub.chmod(0o755)
    args=['--prospective-session',SESSION,'--expected-generation','gen_023','--expected-commit','a'*40,
        '--expected-profile-sha256','b'*64,'--expected-created-at','2026-10-15T10:00:00']
    env=os.environ|{'EVENTEDGE_PYTHON':str(stub),'EVENTEDGE_LOG_DIR':str(tmp_path/'logs'),'CALLS':str(calls)}
    result=subprocess.run(['bash',str(repo/'scripts/daily_trading.sh'),*args],env=env,capture_output=True,text=True)
    assert result.returncode==1
    lines=calls.read_text().splitlines();assert len(lines)==6
    assert all('--validate-prospective-launch' in line for line in lines[:2])
    assert all('--date '+SESSION in line for line in lines[2:])
    calls.unlink();env['VALIDATE_RC']='2'
    result=subprocess.run(['bash',str(repo/'scripts/daily_trading.sh'),*args],env=env,capture_output=True,text=True)
    assert result.returncode==2 and len(calls.read_text().splitlines())==1
