"""Real scheduled preflight termination and canonical-lock acceptance."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

from scheduled_acceptance.harness import create_harness
from tradingagents.strategies.orchestration.runtime_lock import (
    canonical_runtime_lock_path, runtime_lock,
)

pytestmark = pytest.mark.scheduled_acceptance
NOW = '2026-10-14T15:45:00+00:00'


def protected(harness):
    paths = [harness.repo/'data/generations/manifest.json', *harness.state.rglob('*'),
             *(harness.repo/'data/document-inputs').rglob('*')]
    return {str(path):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file()}


def traced_pids(harness):
    return {row[key] for row in harness.trace() for key in ('pid','ppid')
            if row.get(key) and row[key] != os.getpid()}


def owned_alive(harness):
    """Inspect only PIDs retained by this disposable invocation."""
    alive = {}
    for pid in traced_pids(harness):
        result = subprocess.run(['ps','-p',str(pid),'-o','command='], capture_output=True, text=True)
        if result.returncode == 0 and str(harness.repo.parent) in result.stdout:
            alive[pid] = result.stdout.strip()
    return alive


def cleanup_owned(harness):
    # Always clean test-owned children even when a termination regression fails.
    for pid in owned_alive(harness):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def assert_quiescent(harness, before):
    assert not owned_alive(harness), owned_alive(harness)
    lock_path = canonical_runtime_lock_path(harness.repo)
    with runtime_lock(lock_path, exclusive=True):
        assert protected(harness) == before
    assert not [row for row in harness.trace() if row['kind'] in {'model','forbidden_network','unexpected_httpx'}]
    assert not list(harness.state.rglob('*.db'))
    assert not list(harness.state.rglob('source_inputs'))


def assert_subsequent_screen(harness, before):
    offset = len(harness.trace())
    previous = {json.dumps(row, sort_keys=True) for row in harness.attempts()}
    result = harness.run(NOW, entrypoint='preflight')
    assert result.returncode == 1, result.stdout+result.stderr
    new = [row for row in harness.attempts() if json.dumps(row, sort_keys=True) not in previous]
    assert len(new) == 1
    attempt = new[0]
    assert attempt['process_status'] == 'completed'
    assert attempt['result']['screen_runtime_failures'] == []
    assert {row['source'] for row in attempt['result']['screen_source_failures']} == {'edgar'}
    assert any(row['kind']=='http' and row['url'].endswith('/v2/assets') for row in harness.trace()[offset:])
    assert_quiescent(harness, before)


@pytest.mark.parametrize('fault,limit,nested', [
    pytest.param('stall_transport',3,False,id='direct-worker'),
    pytest.param('stall_transport_worker',8,True,id='nested-source-transport'),
])
def test_actual_configured_screen_timeout_releases_processes_and_lock(tmp_path, fault, limit, nested):
    harness = create_harness(tmp_path)
    before = protected(harness)
    started = time.monotonic()
    try:
        result = harness.run(NOW, entrypoint='preflight', fault=fault,
                             preflight_timeout=limit, timeout=30)
        elapsed = time.monotonic()-started
        assert elapsed < 25
        assert result.returncode == 1, result.stdout+result.stderr
        stalled = [row for row in harness.trace() if row['kind']=='stalled_transport']
        assert stalled, harness.trace()
        if nested:
            assert all(any('transport_worker.py' in arg for arg in row['argv']) for row in stalled)
            parents = {row['pid'] for row in harness.trace()
                       if any('run_cohorts.py' in arg for arg in row['argv'])}
            assert all(row['ppid'] in parents and row['pid'] not in parents for row in stalled)
            assert all('api.stlouisfed.org' in row['url'] or 'cftc.gov' in row['url'] for row in stalled)
        else:
            assert all(any('run_cohorts.py' in arg for arg in row['argv']) for row in stalled)
        attempt = harness.attempts()[-1]
        assert attempt['action']=='preflight' and attempt['preflight_mode']=='screen'
        assert attempt['process_status']=='timeout'
        assert attempt['process_return_code'] is None
        assert attempt['result']['success'] is False
        assert attempt['result']['error']==f'Timed out after {limit}s'
        assert attempt['result']['config_sha256']==harness.audit['fixture_sha256']
        assert_quiescent(harness,before)
        assert_subsequent_screen(harness,before)
    finally:
        cleanup_owned(harness)


def test_actual_preflight_lock_busy_refuses_before_external_io(tmp_path):
    harness = create_harness(tmp_path)
    before = protected(harness)
    lock_path = canonical_runtime_lock_path(harness.repo)
    try:
        with runtime_lock(lock_path, exclusive=True):
            result = harness.run(NOW, entrypoint='preflight')
        assert result.returncode != 0, result.stdout+result.stderr
        assert harness.attempts()==[]
        assert not [row for row in harness.trace() if row['kind'] in {'http','sdk','model','forbidden_network'}]
        assert_quiescent(harness,before)
        assert_subsequent_screen(harness,before)
    finally:
        cleanup_owned(harness)


@pytest.mark.parametrize('fault,nested', [
    pytest.param('stall_transport',False,id='direct-worker'),
    pytest.param('stall_transport_worker',True,id='nested-source-transport'),
])
def test_abrupt_screen_worker_death_releases_inherited_lock(tmp_path, fault, nested):
    harness = create_harness(tmp_path)
    before = protected(harness)
    # Use the same scheduled shell, scenario and child bootstrap as Harness.run;
    # asynchronous launch permits a real SIGKILL at the external transport wait.
    harness.scenario_path.write_text(json.dumps({'now':NOW, 'session':'2026-10-14',
                                                 'fault':fault, 'preflight_timeout':20}))
    process = subprocess.Popen(['/bin/bash',str(harness.repo/'scripts/preflight.sh')],
        cwd=harness.repo, env=harness.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic()+15
        stalled = []
        while time.monotonic()<deadline:
            stalled = [row for row in harness.trace() if row['kind']=='stalled_transport']
            if stalled or process.poll() is not None:
                break
            time.sleep(0.05)
        assert stalled, harness.trace()
        if nested:
            assert any('transport_worker.py' in arg for arg in stalled[-1]['argv'])
            worker = stalled[-1]['ppid']
            assert any(row['pid']==worker and any('run_cohorts.py' in arg for arg in row['argv'])
                       for row in harness.trace())
            assert worker != stalled[-1]['pid']
        else:
            worker = stalled[-1]['pid']
            assert any('run_cohorts.py' in arg for arg in stalled[-1]['argv'])
        os.kill(worker,signal.SIGKILL)
        stdout,stderr = process.communicate(timeout=15)
        assert process.returncode==1, stdout+stderr
        attempt = harness.attempts()[-1]
        assert attempt['process_status']=='completed'
        assert attempt['process_return_code']==-signal.SIGKILL
        assert attempt['result']['success'] is False
        assert attempt['result']['config_sha256']==harness.audit['fixture_sha256']
        assert_quiescent(harness,before)
        assert_subsequent_screen(harness,before)
    finally:
        cleanup_owned(harness)
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_outer_controller_timeout_forwards_to_nested_preflight_group(tmp_path):
    """An inner owned session must still obey the real controller deadline."""
    harness = create_harness(tmp_path)
    before = protected(harness)
    started = time.monotonic()
    try:
        result = harness.run('2026-10-14T22:00:00+00:00', entrypoint='controller',
                             fault='stall_transport_worker', preflight_timeout=60,
                             daily_timeout=10, timeout=40)
        assert time.monotonic()-started < 35
        assert result.returncode==0, result.stdout+result.stderr
        status = json.loads(result.stdout)
        assert status['status']=='recovering', status
        assert any(row['reason_code']=='worker_timeout' for row in status['incidents'])
        stalled = [row for row in harness.trace() if row['kind']=='stalled_transport']
        assert stalled, harness.trace()
        parents = {row['pid'] for row in harness.trace()
                   if any('run_cohorts.py' in arg for arg in row['argv'])}
        assert all(any('transport_worker.py' in arg for arg in row['argv'])
                   and row['ppid'] in parents and row['pid'] not in parents for row in stalled)
        # No daily worker may reach account execution after the outer timeout.
        assert not [row for row in harness.attempts() if row['action']=='daily']
        assert_quiescent(harness,before)
        assert_subsequent_screen(harness,before)
    finally:
        cleanup_owned(harness)


def test_outer_grace_waits_for_delayed_forwarder_after_leader_exits(tmp_path):
    """A fast leader exit must not cut short the existing group TERM grace."""
    import sys
    import textwrap
    from tradingagents.strategies.orchestration.runtime_recovery import bounded_process
    pids_path = tmp_path/'owned-pids.json'
    forwarded_path = tmp_path/'forwarded.json'
    manager_code = textwrap.dedent('''\
        import json,os,pathlib,signal,subprocess,sys,time
        leader = os.getppid()
        child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],start_new_session=True)
        def forward(signum,frame):
            time.sleep(.35)
            try:
                os.kill(leader,0)
                leader_alive = True
            except ProcessLookupError:
                leader_alive = False
            os.killpg(child.pid,signal.SIGKILL)
            child.wait(timeout=2)
            pathlib.Path(FORWARDED).write_text(json.dumps({'leader_alive':leader_alive}))
            raise SystemExit(128+signum)
        signal.signal(signal.SIGTERM,forward)
        pathlib.Path(PIDS).write_text(json.dumps({'manager':os.getpid(),'leader':leader,'child':child.pid}))
        time.sleep(120)
    ''').replace('FORWARDED',repr(str(forwarded_path))).replace('PIDS',repr(str(pids_path)))
    leader_code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"+repr(manager_code)+"]); time.sleep(120)"
    try:
        code,_ = bounded_process([sys.executable,'-c',leader_code],cwd=tmp_path,env={},timeout=1)
        assert code==124
        pids = json.loads(pids_path.read_text())
        assert forwarded_path.exists(), 'manager was killed before its delayed forwarding handler completed'
        assert json.loads(forwarded_path.read_text())=={'leader_alive':False}
        child = subprocess.run(['ps','-p',str(pids['child']),'-o','stat='],capture_output=True,text=True)
        assert child.returncode!=0 or child.stdout.strip().startswith('Z')
    finally:
        if pids_path.exists():
            child_pid = json.loads(pids_path.read_text())['child']
            try:
                os.killpg(child_pid,signal.SIGKILL)
            except ProcessLookupError:
                pass
