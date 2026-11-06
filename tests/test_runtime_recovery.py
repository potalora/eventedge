"""Local replay and accelerated unattended recovery; no live transports."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tradingagents.strategies.orchestration import runtime_recovery as recovery
from scripts import recover_runtime as controller
from test_source_reliability_pipeline import pipeline, SESSION, InterruptedWorker
from tradingagents.strategies.orchestration import decision_clock


def tick(repo, now, **kwargs):
    return controller.tick(repo, now, clock=lambda: now, **kwargs)


def prospective(pipeline, monkeypatch):
    fixture, owner, config, repo = pipeline
    for target in (config, owner._base_config):
        target['autoresearch']['decision_clock_policy'] = decision_clock.PROSPECTIVE
        target['autoresearch']['research_resilience_policy'] = 'dependency_isolation_v1'
    for cohort in owner.cohorts:
        cohort['engine'].config['autoresearch']['decision_clock_policy'] = decision_clock.PROSPECTIVE
        cohort['engine'].config['autoresearch']['research_resilience_policy'] = 'dependency_isolation_v1'
    fixture.now = datetime(2026, 10, 3, 18, tzinfo=timezone.utc)
    monkeypatch.setattr(decision_clock, 'utc_now', lambda: fixture.now)
    return fixture, owner, config, repo


def test_partial_p0_resumes_real_committed_phases_without_market_or_model(pipeline, monkeypatch):
    fixture, owner, config, repo = prospective(pipeline, monkeypatch)
    executor = owner.cohorts[-1]['executor']
    original = executor._phase
    def interrupt(session, phase, *args, **kwargs):
        if phase == 'snapshot_account':
            raise InterruptedWorker('process stopped before final P0 transaction')
        return original(session, phase, *args, **kwargs)
    monkeypatch.setattr(executor, '_phase', interrupt)
    with pytest.raises(InterruptedWorker):
        owner.run_daily(SESSION.isoformat())
    monkeypatch.setattr(executor, '_phase', original)
    before_calls, before_models = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    result = recovery.recover_owner(owner, SESSION, now=fixture.now)
    assert result['disposition'] == 'wait_next_session'
    assert result['reason_code'] == 'checkpoint_missing'
    from tradingagents.strategies.orchestration.session_executor import PHASES
    assert all(all(c['ledger'].phase_completed(SESSION, phase) for phase in PHASES) for c in owner.cohorts)
    before_counts = [tuple(c['ledger'].connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
        for table in ('fills', 'cash_events', 'account_snapshots')) for c in owner.cohorts]
    repeated = recovery.recover_owner(owner, SESSION, now=fixture.now)
    assert repeated == result
    after_counts = [tuple(c['ledger'].connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
        for table in ('fills', 'cash_events', 'account_snapshots')) for c in owner.cohorts]
    assert before_counts == after_counts
    assert fixture.calls == before_calls and fixture.model_calls == before_models
    assert not fixture.blocked_calls


def test_frozen_committee_resumes_staging_without_provider_model_or_shadow(pipeline, monkeypatch):
    fixture, owner, config, repo = prospective(pipeline, monkeypatch)
    ledger = owner.cohorts[-1]['ledger']
    original = ledger.complete_staging
    def interrupt(*args, **kwargs):
        raise InterruptedWorker('process stopped after accepted committee before staging')
    monkeypatch.setattr(ledger, 'complete_staging', interrupt)
    with pytest.raises(InterruptedWorker):
        owner.run_daily(SESSION.isoformat())
    monkeypatch.setattr(ledger, 'complete_staging', original)
    before_calls, before_models = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    result = recovery.recover_owner(owner, SESSION, now=fixture.now)
    assert result['disposition'] == 'recovered', result
    assert all(c['ledger'].staging_completed(SESSION, owner._epoch_id,
        owner._policy_id_for_horizon(c['config'].horizon)) for c in owner.cohorts)
    assert fixture.calls == before_calls and fixture.model_calls == before_models
    assert not fixture.blocked_calls
    fixture.now = datetime(2026, 10, 6, 20, tzinfo=timezone.utc)
    assert recovery.recover_owner(owner, SESSION, now=fixture.now)['disposition'] == 'recovered'
    assert fixture.calls == before_calls and fixture.model_calls == before_models


def test_expired_partial_staging_is_missed_without_fresh_historical_synthesis(pipeline, monkeypatch):
    fixture, owner, config, repo = prospective(pipeline, monkeypatch)
    ledger = owner.cohorts[-1]['ledger']
    original = ledger.complete_staging
    monkeypatch.setattr(ledger, 'complete_staging', lambda *a, **kw: (_ for _ in ()).throw(InterruptedWorker()))
    with pytest.raises(InterruptedWorker):
        owner.run_daily(SESSION.isoformat())
    monkeypatch.setattr(ledger, 'complete_staging', original)
    before_calls, before_models = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    result = recovery.recover_owner(owner, SESSION, now=datetime(2026, 10, 5, 13, 30, tzinfo=timezone.utc))
    assert result['disposition'] == 'wait_next_session' and result['reason_code'] == 'missed_staging'
    assert not ledger.staging_completed(SESSION, owner._epoch_id, owner._policy_id_for_horizon(owner.cohorts[-1]['config'].horizon))
    assert fixture.calls == before_calls and fixture.model_calls == before_models


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    repo = tmp_path
    path = repo/'data/generations/manifest.json'
    path.parent.mkdir(parents=True)
    row = {'gen_id':'gen_100', 'git_commit':'a'*40, 'config_sha256':'b'*64,
           'state_dir':str(repo/'state'), 'worktree_path':str(repo/'worktree'),
           'config_file':str(repo/'profile.json'), 'status':'active'}
    path.write_text(json.dumps({'generations':[row]}))
    monkeypatch.setattr(controller, 'identity_for', lambda repo, row, session: {
        'generation_id':row['gen_id'], 'generation_commit':row['git_commit'],
        'config_sha256':row['config_sha256'], 'session':session,
        'state_dir':row['state_dir'], 'worktree_path':row['worktree_path']})
    monkeypatch.setattr(controller, 'canonical_runtime_lock_path', lambda repo: repo/'runtime.lock')
    return repo, row


def test_accelerated_days_reboot_retry_backoff_exhaustion_and_next_session(scheduler):
    repo, row = scheduler
    now = datetime(2026, 10, 14, 22, 1, tzinfo=timezone.utc)
    calls = []
    def launch(repo, row, session, action, **kwargs):
        calls.append((session, action))
        if action == 'daily':
            raise SystemExit('simulated controller process death after durable launch')
        return 124, ''
    reporter = lambda *a: {}
    discover = lambda *a: []
    with pytest.raises(SystemExit):
        tick(repo, now, launcher=launch, reporter=reporter, discover=discover)
    journal = recovery.load_journal(repo/'data/runtime_recovery/journal.json')
    record = next(iter(journal['incidents'].values()))
    assert record['running'] and record['daily_started']
    # New process observes durable launch rather than starting a second sample.
    result = tick(repo, now+timedelta(minutes=1), launcher=launch, reporter=reporter, discover=discover)
    assert calls == [('2026-10-14','daily')]
    for minutes in (6, 12, 43):
        result = tick(repo, now+timedelta(minutes=minutes), launcher=launch, reporter=reporter, discover=discover)
    assert result['status'] == 'operator_required'
    assert result['incidents'][0]['attempt_count'] == 3
    assert result['incidents'][0]['reason_code'] == 'attempts_exhausted'
    # Exhausted accounting is sticky and blocks a new ordinary sample.
    def next_launch(repo, row, session, action, **kwargs):
        calls.append((session, action))
        return 0, 'RECOVERY_RESULT='+json.dumps(recovery._result('recovered','complete'))
    success = {'staging_complete':True,'accounting_valid':True,'evidence_complete':True,'sources':{}}
    tomorrow = now+timedelta(days=1)
    tick(repo, tomorrow, launcher=next_launch, reporter=lambda *a:success, discover=discover)
    tick(repo, tomorrow+timedelta(minutes=5), launcher=next_launch, reporter=lambda *a:success, discover=discover)
    assert calls.count(('2026-10-15','daily')) == 0


def test_frozen_failed_source_waits_and_heartbeat_recovers_next_day(scheduler):
    repo, row = scheduler
    now = datetime(2026, 10, 14, 22, 1, tzinfo=timezone.utc)
    calls = []
    def launcher(repo, row, session, action, **kwargs):
        calls.append(action)
        kwargs['heartbeat']()
        return 0, ''
    failed = {'accounting_valid':True, 'staging_complete':True, 'outcome':'degraded',
        'sources':{'configuration':'c'*64,'unresolved':[{'http_status':503}]}}
    first = tick(repo, now, launcher=launcher, reporter=lambda *a:failed, discover=lambda *a:[])
    assert first['status'] == 'waiting_next_session' and calls == ['daily']
    second = tick(repo, now+timedelta(days=1), launcher=launcher,
        reporter=lambda *a:{'staging_complete':True,'accounting_valid':True,'evidence_complete':True,'sources':{}}, discover=lambda *a:[])
    assert second['status'] == 'healthy' and calls == ['daily','daily']
    assert any(item['disposition']=='wait_next_session' for item in second['incidents'])
    assert second['controller_reason_code'] is None


def test_corrupt_journal_preserved_and_status_raises_operator_action(scheduler):
    repo, row = scheduler
    path = repo/'data/runtime_recovery/journal.json'
    path.parent.mkdir(parents=True)
    path.write_text('{broken')
    result = tick(repo, datetime(2026, 10, 14, 20, tzinfo=timezone.utc))
    assert result['status'] == 'operator_required' and result['controller_reason_code']=='state_corrupt'
    assert path.read_text() == '{broken'


def test_timeout_reaps_real_descendant_process_group(tmp_path):
    child_pid = tmp_path/'child.pid'
    command = [sys.executable, '-c',
        'import subprocess,time,pathlib; p=subprocess.Popen(["sleep","60"]); '
        'pathlib.Path('+repr(str(child_pid))+').write_text(str(p.pid)); time.sleep(60)']
    started = time.monotonic()
    code, output = recovery.bounded_process(command, cwd=tmp_path, env=dict(__import__('os').environ), timeout=1)
    assert code == 124 and time.monotonic()-started < 15
    pid = int(child_pid.read_text())
    # A killed child can remain a zombie briefly until the system reaper runs;
    # it must never remain a live worker holding a runtime lock.
    probe = subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True)
    assert probe.returncode != 0 or probe.stdout.strip().startswith('Z')


def test_abrupt_process_exit_then_new_process_reopens_all_16_books(pipeline, monkeypatch, tmp_path):
    fixture, owner, config, repo = prospective(pipeline, monkeypatch)
    executor = owner.cohorts[-1]['executor']
    original = executor._phase
    def stop_before_inventory(session, phase, *args, **kwargs):
        if phase == 'apply_corporate_actions':
            raise InterruptedWorker('prepare persisted partial boundary for actual crash')
        return original(session, phase, *args, **kwargs)
    monkeypatch.setattr(executor, '_phase', stop_before_inventory)
    with pytest.raises(InterruptedWorker):
        owner.run_daily(SESSION.isoformat())
    monkeypatch.setattr(executor, '_phase', original)
    profile = tmp_path/'child-config.json'
    profile.write_text(json.dumps(config))
    harness = tmp_path/'runtime-child.py'
    harness.write_text('''import json, os, socket, sys
from datetime import date, datetime, timezone
sys.modules['openbb'] = None
def forbidden(*args, **kwargs):
    raise AssertionError('recovery attempted live IO/model work')
socket.socket.connect = forbidden
socket.create_connection = forbidden
from tradingagents.strategies.orchestration.cohort_orchestrator import CohortOrchestrator, build_default_cohorts
from tradingagents.strategies.orchestration.runtime_recovery import recover_owner
config = json.load(open(sys.argv[1]))
owner = CohortOrchestrator(build_default_cohorts(config), config,
    generation_id='gen_099', generation_commit='a'*40)
owner._screen_for_horizon = forbidden
owner._fetch_openbb_enrichment = forbidden
for cohort in owner.cohorts:
    cohort['engine']._fetch_all_data = forbidden
    cohort['engine']._fetch_missing_prices = forbidden
last = owner.cohorts[-1]['executor']
if sys.argv[2] == 'crash':
    original = last._phase
    def crash_after_commit(session, phase, *args, **kwargs):
        value = original(session, phase, *args, **kwargs)
        if phase == 'apply_corporate_actions':
            os._exit(73)
        return value
    last._phase = crash_after_commit
result = recover_owner(owner, date(2026,10,2), now=datetime(2026,10,3,18,tzinfo=timezone.utc))
print(json.dumps(result))
''')
    # Fixture identity is taken from its actual epoch, not assumed by child.
    epoch = owner._metric_store.current_epoch()
    harness.write_text(harness.read_text().replace("generation_id='gen_099', generation_commit='a'*40",
        'generation_id='+repr(epoch.generation_id)+', generation_commit='+repr(epoch.generation_commit)))
    import os
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    def child(mode):
        process = subprocess.Popen([sys.executable, str(harness), str(profile), mode],
            env=env, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = process.communicate(timeout=30)
        return process.returncode, stdout, stderr
    code, stdout, stderr = child('crash')
    assert code == 73, (stdout, stderr)
    from tradingagents.strategies.orchestration.session_executor import PHASES
    assert owner.cohorts[-1]['ledger'].phase_completed(SESSION, 'apply_corporate_actions')
    assert not owner.cohorts[-1]['ledger'].phase_completed(SESSION, 'snapshot_account')
    code, stdout, stderr = child('resume')
    assert code == 0, stderr
    result = json.loads(stdout.splitlines()[-1])
    assert result['disposition'] == 'wait_next_session'
    assert result['reason_code'] == 'checkpoint_missing'
    assert all(all(c['ledger'].phase_completed(SESSION, p) for p in PHASES) for c in owner.cohorts)
    counts = [tuple(c['ledger'].connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
        for table in ('fills','cash_events','account_snapshots')) for c in owner.cohorts]
    code, stdout, stderr = child('resume')
    assert code == 0, stderr
    assert counts == [tuple(c['ledger'].connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
        for table in ('fills','cash_events','account_snapshots')) for c in owner.cohorts]


def test_mixed_bound_and_unbound_books_require_user_without_new_effects(pipeline, monkeypatch):
    fixture, owner, config, repo = prospective(pipeline, monkeypatch)
    first = owner.cohorts[0]['executor']
    original = first._phase
    def stop(session, phase, *args, **kwargs):
        if phase == 'snapshot_account':
            raise InterruptedWorker('one book bound while remaining books have no context')
        return original(session, phase, *args, **kwargs)
    monkeypatch.setattr(first, '_phase', stop)
    with pytest.raises(InterruptedWorker):
        owner.run_daily(SESSION.isoformat())
    monkeypatch.setattr(first, '_phase', original)
    calls, models = fixture.calls.copy(), fixture.model_calls.copy()
    result = recovery.recover_owner(owner, SESSION, now=fixture.now)
    assert result['disposition']=='requires_user' and result['reason_code']=='checkpoint_invalid'
    assert not first.ledger.phase_completed(SESSION, 'snapshot_account')
    assert fixture.calls == calls and fixture.model_calls == models


def test_older_recovery_precedes_new_daily_and_failed_accounting_blocks(scheduler):
    repo, row = scheduler
    yesterday = datetime(2026,10,14,22,1,tzinfo=timezone.utc)
    identity = controller.identity_for(repo,row,'2026-10-14')
    journal = {'version':recovery.VERSION,'schema_version':1,'incidents':{}}
    record = recovery.new_incident(identity,yesterday)
    record['daily_started'] = True
    journal['incidents'][recovery.incident_key(identity)] = record
    recovery.atomic_json(repo/'data/runtime_recovery/journal.json',journal)
    actions = []
    def launcher(repo,row,session,action,**kwargs):
        actions.append((session,action))
        return 0,'RECOVERY_RESULT='+json.dumps(recovery._result('wait_next_session','checkpoint_missing'))
    today = yesterday+timedelta(days=1)
    tick(repo,today,launcher=launcher,reporter=lambda *a:{},discover=lambda *a:[])
    assert actions[0] == ('2026-10-14','recovery')
    assert actions[1] == ('2026-10-15','daily')


def test_exact_checkout_identity_rejects_dirty_untracked_and_foreign_paths(tmp_path):
    repo = tmp_path.resolve()/'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True,text=True).stdout.strip()
    git('init')
    git('config','user.email','offline@example.invalid')
    git('config','user.name','Offline acceptance')
    (repo/'.gitignore').write_text('data/\n.worktrees/\n')
    (repo/'tracked.py').write_text('value=1\n')
    git('add','.gitignore','tracked.py')
    git('commit','-m','offline fixture')
    commit = git('rev-parse','HEAD')
    worktree = repo/'.worktrees/gen_100'
    worktree.parent.mkdir()
    git('worktree','add','--detach',str(worktree),commit)
    state = repo/'data/generations/gen_100'
    state.mkdir(parents=True)
    profile = state/'generation-config.json'
    profile.write_text('{}')
    import hashlib
    row = {'gen_id':'gen_100','git_commit':commit,'worktree_path':str(worktree),
           'state_dir':str(state),'config_file':str(profile),
           'config_sha256':hashlib.sha256(profile.read_bytes()).hexdigest()}
    assert recovery.identity_for(repo,row,'2026-10-14')['generation_commit']==commit
    sibling = repo/'.worktrees/gen_101'
    git('worktree','add','--detach',str(sibling),commit)
    pointer = (worktree/'.git').read_text()
    (worktree/'.git').write_text((sibling/'.git').read_text())
    with pytest.raises(ValueError):
        recovery.identity_for(repo,row,'2026-10-14')
    (worktree/'.git').write_text(pointer)
    (worktree/'shadow.py').write_text('untracked=1')
    with pytest.raises(ValueError):
        recovery.identity_for(repo,row,'2026-10-14')
    (worktree/'shadow.py').unlink()
    (worktree/'tracked.py').write_text('value=2\n')
    with pytest.raises(ValueError):
        recovery.identity_for(repo,row,'2026-10-14')
    (worktree/'tracked.py').write_text('value=1\n')
    foreign = dict(row,state_dir=str(tmp_path/'foreign'))
    with pytest.raises(ValueError):
        recovery.identity_for(repo,foreign,'2026-10-14')
    profile.write_text('{"altered":true}')
    with pytest.raises(ValueError):
        recovery.identity_for(repo,row,'2026-10-14')


def test_missing_daily_report_is_rebuilt_without_a_new_sample(scheduler):
    repo,row = scheduler
    now = datetime(2026,10,14,22,1,tzinfo=timezone.utc)
    identity = controller.identity_for(repo,row,'2026-10-14')
    record = recovery.new_incident(identity,now)
    record['daily_started']=True
    recovery.atomic_json(repo/'data/runtime_recovery/journal.json',
        {'version':recovery.VERSION,'schema_version':1,'incidents':{recovery.incident_key(identity):record}})
    rendered = []
    def reporter(repo,row,session):
        rendered.append(session)
        return {'staging_complete':True,'accounting_valid':True,'evidence_complete':True,'sources':{}}
    result = tick(repo,now,reporter=reporter,launcher=lambda *a,**kw:pytest.fail('report repair launched worker'),discover=lambda *a:[])
    assert result['status']=='healthy' and rendered==['2026-10-14']


def test_wholly_missed_previous_session_is_reported_without_historical_run(scheduler):
    repo,row = scheduler
    row['created_at']='2026-10-14T20:00:00+00:00'
    (repo/'data/generations/manifest.json').write_text(json.dumps({'generations':[row]}))
    now = datetime(2026,10,15,15,tzinfo=timezone.utc)
    result = tick(repo,now,reporter=lambda *a:{},launcher=lambda *a,**kw:pytest.fail('missed session fetched future evidence'),discover=lambda *a:[])
    assert len(result['incidents'])==1
    assert result['incidents'][0]['reason_code']=='missed_session'
    assert result['incidents'][0]['disposition']=='wait_next_session'


def test_four_accelerated_sessions_recover_missed_run_then_preserve_failed_sample(scheduler):
    repo,row = scheduler
    first = datetime(2026,10,14,22,1,tzinfo=timezone.utc)
    reports, calls = {}, []
    healthy = {'staging_complete':True,'accounting_valid':True,'evidence_complete':True,'sources':{}}
    failed_source = {'accounting_valid':True,'staging_complete':True,'outcome':'degraded',
        'sources':{'configuration':'c'*64,'unresolved':[{'http_status':503}]}}
    def launcher(repo,row,session,action,**kwargs):
        calls.append((session,action))
        if session=='2026-10-14' and action=='daily':
            raise SystemExit('unattended reboot before any accounting context')
        if action=='recovery':
            return 0,'RECOVERY_RESULT='+json.dumps(recovery._result('wait_next_session','missed_session'))
        reports[session] = failed_source if session=='2026-10-15' else healthy
        return 0,''
    reporter = lambda repo,row,session:reports.get(session,{})
    with pytest.raises(SystemExit):
        tick(repo,first,launcher=launcher,reporter=reporter,discover=lambda *a:[])
    second = first+timedelta(days=1)
    # First boot records the abandoned launch/backoff; later wake safely closes
    # the wholly missed sample before starting the next ordinary session.
    tick(repo,second,launcher=launcher,reporter=reporter,discover=lambda *a:[])
    result = tick(repo,second+timedelta(minutes=6),launcher=launcher,reporter=reporter,discover=lambda *a:[])
    assert result['status']=='waiting_next_session'
    for delta in (2,5):
        result = tick(repo,first+timedelta(days=delta),launcher=launcher,reporter=reporter,discover=lambda *a:[])
        assert result['status']=='healthy'
    assert [session for session,action in calls if action=='daily']==[
        '2026-10-14','2026-10-15','2026-10-16','2026-10-19']
    assert [session for session,action in calls if action=='recovery']==['2026-10-14']
    assert len(result['incidents'])==4
    assert {item['session']:item['reason_code'] for item in result['incidents']}['2026-10-15']=='accepted_outcome'


def test_new_clean_accepted_evidence_resolves_historical_preflight_source_access_incident():
    report = {'accounting_valid':True,'staging_complete':True,'evidence_complete':True,
        'sources':{'observations':{'edgar':'success'},'unresolved':[]},
        'source_health_failures':[],'candidate_input_issues':[],
        'preflight_incidents':[{'failures':[{'source':'edgar','http_status':401}]}]}
    assert controller._terminal_report(report)==recovery._result('recovered','complete')
    report['sources']['unresolved']=[{'provider':'edgar','http_status':401}]
    assert controller._terminal_report(report)==recovery._result('wait_next_session','accepted_outcome')


@pytest.mark.parametrize('status', [401, 403])
def test_source_http_access_failure_is_accepted_degraded_sample_not_model_auth(status):
    """Retained gen_021 shape: source denial plus incomplete provider coverage."""
    from copy import deepcopy

    report = {'accounting_valid':True, 'staging_complete':True, 'evidence_complete':True,
        'sources':{'configuration':'c'*64, 'unresolved':[
            {'provider':'regulations', 'source':'regulations', 'operation':'get',
             'http_status':status, 'reason_code':'http_error', 'recovered':False},
            {'provider':'noaa', 'http_status':None, 'reason_code':'provider_error'},
            {'provider':'regulations', 'http_status':None, 'reason_code':'batch_failure'}]},
        'source_health_failures':[{'strategy':'commodity_macro', 'status':'data_failure'}],
        'candidate_input_issues':[], 'diagnostics':[],
        'preflight_incidents':[{'failures':[
            {'source':'regulations', 'http_status':status, 'reason_code':'batch_failure'}],
            'runtime_failures':[]}]}
    before = deepcopy(report)
    assert controller._terminal_report(report) == recovery._result('wait_next_session', 'accepted_outcome')
    assert report == before  # Historical and current source facts remain intact.


@pytest.mark.parametrize('incomplete', ['staging_complete', 'evidence_complete', 'accounting_valid'])
def test_historical_source_denial_cannot_mask_unrelated_incomplete_recovery_evidence(incomplete):
    from copy import deepcopy

    report = {'accounting_valid':True, 'staging_complete':True, 'evidence_complete':True,
        'sources':{'configuration':'c'*64, 'observations':{'regulations':'success'},
                   'unresolved':[]},
        'source_health_failures':[], 'candidate_input_issues':[],
        'preflight_incidents':[{'failures':[
            {'source':'regulations', 'http_status':403, 'reason_code':'http_error'}]}]}
    assert controller._terminal_report(report) == recovery._result('recovered', 'complete')
    report[incomplete] = False
    before = deepcopy(report)
    assert controller._terminal_report(report) is None
    assert report == before


def test_strategy_defect_remains_operator_required_despite_source_access_failure():
    report = {'accounting_valid':True, 'staging_complete':True, 'evidence_complete':True,
        'sources':{'configuration':'c'*64, 'unresolved':[
            {'provider':'regulations', 'http_status':403, 'reason_code':'http_error'}]},
        'source_health_failures':[{'status':'strategy_defect'}]}
    assert controller._terminal_report(report) == recovery._result('requires_user', 'checkpoint_invalid')


def test_explicit_validated_worker_auth_result_remains_operator_required():
    value = recovery._result('requires_user', 'auth_required')
    assert controller._worker_result(0, 'RECOVERY_RESULT='+json.dumps(value)) == value
