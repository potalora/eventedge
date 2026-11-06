"""Target-host evidence must not imply deployment or leak raw snapshot fields."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check_target_host_smoke.py'
SHADOW = Path('/home/hermes/.hermes/private/projects/eventedge')


@pytest.fixture
def collector_host(monkeypatch):
    """Keep advisory shadow probes and systemd reads independent of this host."""
    state = {'shadow': False, 'shadow_probes': 0, 'shadow_git_calls': []}
    native_is_dir, native_run = Path.is_dir, subprocess.run

    def is_dir(path):
        if path == SHADOW:
            state['shadow_probes'] += 1
            if isinstance(state['shadow'], Exception):
                raise state['shadow']
            return state['shadow']
        return native_is_dir(path)

    def run(args, *positional, **options):
        if args[0] == 'systemctl':
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
        if args[:3] == ['git', '-C', str(SHADOW)]:
            state['shadow_git_calls'].append(args)
            return subprocess.CompletedProcess(args, 0, stdout='c' * 40 + '\n', stderr='')
        return native_run(args, *positional, **options)

    monkeypatch.setattr(Path, 'is_dir', is_dir)
    monkeypatch.setattr(subprocess, 'run', run)
    return state


def snapshot():
    return {
        'hostname': 'test-vps',
        'production': {'path': '/home/hermes/trading_agents', 'head': 'a' * 40},
        'active_generations': [{'gen_id': 'gen_014', 'git_commit': 'a' * 40,
                                'head': 'a' * 40, 'worktree_in_boundary': True}],
        'units': {'trade.service': {'LoadState': 'loaded', 'ActiveState': 'inactive',
                                  'User': 'hermes', 'WorkingDirectory': '/home/hermes/trading_agents'},
                  'trade.timer': {'LoadState': 'loaded', 'ActiveState': 'active'}},
        'credentials_present': {'OPENAI_API_KEY': True},
        'paths': {'manifest_readable': True, 'report_directory_writable': True,
                  'candidate_report_script_present': False},
    }


def run_snapshot(tmp_path, data, candidate='b' * 40, hostname='test-vps'):
    source = tmp_path / 'snapshot.json'
    source.write_text(json.dumps(data))
    output = tmp_path / 'report.json'
    result = subprocess.run([sys.executable, str(SCRIPT), '--snapshot', str(source),
                             '--candidate-commit', candidate, '--expected-hostname', hostname,
                             '--output', str(output)], capture_output=True, text=True, timeout=10)
    return result, output


def test_cli_reports_candidate_not_deployed_without_running_a_cycle(tmp_path):
    result, output = run_snapshot(tmp_path, snapshot())
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report['candidate_deployed'] is False
    assert report['authentication_proven'] is False
    assert report['scheduled_candidate_execution_proven'] is False
    assert 'candidate_not_deployed' in report['rollout_boundaries']


def test_matching_heads_still_cannot_prove_authentication_or_scheduled_execution(tmp_path):
    result, output = run_snapshot(tmp_path, snapshot(), candidate='a' * 40)
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report['candidate_deployed'] is True
    assert report['authentication_proven'] is False
    assert report['scheduled_candidate_execution_proven'] is False


@pytest.mark.parametrize('mutation,reason', [
    ('hostname', 'target_identity_mismatch'),
    ('production', 'production_boundary_mismatch'),
    ('active_generations', 'active_generation_scope_missing'),
])
def test_identity_or_missing_active_generation_fails_closed(tmp_path, mutation, reason):
    data = snapshot()
    data[mutation] = {'path': '/home/hermes/.hermes/private/projects/eventedge'} if mutation == 'production' else ([] if mutation == 'active_generations' else 'other-vps')
    result, output = run_snapshot(tmp_path, data)
    assert result.returncode == 1
    assert reason in json.loads(output.read_text())['inspection_failures']


def test_unknown_snapshot_fields_and_credential_values_are_not_published(tmp_path):
    data = snapshot()
    data['raw_token'] = 'PRIVATE_SECRET'
    data['credentials_present']['OPENAI_API_KEY'] = 'PRIVATE_SECRET'
    result, output = run_snapshot(tmp_path, data)
    assert result.returncode == 1
    assert 'PRIVATE_SECRET' not in output.read_text() + result.stdout + result.stderr
    assert 'credential_presence_invalid' in json.loads(output.read_text())['inspection_failures']


def test_invalid_json_reports_only_sanitized_failure(tmp_path):
    source = tmp_path / 'invalid.json'
    source.write_text('{PRIVATE_SECRET')
    result = subprocess.run([sys.executable, str(SCRIPT), '--snapshot', str(source),
                             '--candidate-commit', 'b' * 40, '--expected-hostname', 'test-vps'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert 'PRIVATE_SECRET' not in result.stdout + result.stderr
    assert 'Target-host inspection unavailable' in result.stderr


def test_collector_reads_only_selected_env_presence_and_manifest_rows(tmp_path, collector_host):
    import runpy
    module = runpy.run_path(str(SCRIPT))
    repo = tmp_path / 'repo'
    (repo / 'data/generations').mkdir(parents=True)
    (repo / 'docs/reports').mkdir(parents=True)
    (repo / '.env').write_text('OPENAI_API_KEY=PRIVATE_SECRET\nOTHER_SECRET=PRIVATE_SECRET\n')
    (repo / 'data/generations/manifest.json').write_text(json.dumps({'generations': [
        {'gen_id': 'gen_001', 'status': 'retired', 'description': 'PRIVATE_SECRET'},
        {'gen_id': 'gen_014', 'status': 'active', 'git_commit': 'a' * 40,
         'worktree_path': str(repo / '.worktrees/gen_014'), 'description': 'PRIVATE_SECRET'},
    ]}))
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    result = module['collect_snapshot'](repo)
    assert result['credentials_present']['OPENAI_API_KEY'] is True
    assert result['credentials_present']['ALPACA_API_KEY'] is False
    assert [row['gen_id'] for row in result['active_generations']] == ['gen_014']
    assert 'PRIVATE_SECRET' not in json.dumps(result)
    assert 'OTHER_SECRET' not in json.dumps(result)


def test_unknown_nested_snapshot_metadata_is_not_published(tmp_path):
    data = snapshot()
    data['production']['token'] = 'PRIVATE_SECRET'
    data['units']['trade.service']['Environment'] = 'TOKEN=PRIVATE_SECRET'
    data['paths']['raw_error'] = 'PRIVATE_SECRET'
    data['latest_report_files'] = [{'name': 'report.json', 'size_bytes': 12,
                                   'mtime_utc': '2026-10-12T22:15:32+00:00',
                                   'raw_error': 'PRIVATE_SECRET'}]
    result, output = run_snapshot(tmp_path, data)
    assert result.returncode == 0, result.stderr
    assert 'PRIVATE_SECRET' not in output.read_text() + result.stdout + result.stderr


@pytest.mark.parametrize('field,value,reason', [
    ('worktree_in_boundary', False, 'active_worktree_boundary_invalid'),
    ('worktree_in_boundary', None, 'active_worktree_boundary_invalid'),
    ('head', '', 'active_generation_identity_unreadable'),
    ('head', 'HEAD', 'active_generation_identity_unreadable'),
    ('git_commit', '', 'active_generation_identity_unreadable'),
])
def test_active_worktree_boundary_and_identity_fail_closed(tmp_path, field, value, reason):
    data = snapshot()
    if value is None:
        data['active_generations'][0].pop(field)
    else:
        data['active_generations'][0][field] = value
    result, output = run_snapshot(tmp_path, data)
    assert result.returncode == 1
    report = json.loads(output.read_text())
    assert report['inspection_passed'] is False
    assert reason in report['inspection_failures']


@pytest.mark.parametrize('symlink_level', ['worktrees', 'generation'])
def test_collector_rejects_worktree_symlink_outside_repo(tmp_path, symlink_level, collector_host):
    import runpy
    module = runpy.run_path(str(SCRIPT))
    repo = tmp_path / 'repo'
    (repo / 'data/generations').mkdir(parents=True)
    outside = tmp_path / 'outside'
    subprocess.run(['git', 'init', '-q', str(outside)], check=True)
    subprocess.run(['git', '-C', str(outside), '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@example.invalid', 'commit', '-q',
                    '--allow-empty', '-m', 'fixture'], check=True)
    if symlink_level == 'worktrees':
        (repo / '.worktrees').symlink_to(tmp_path, target_is_directory=True)
        worktree = repo / '.worktrees/outside'
    else:
        (repo / '.worktrees').mkdir()
        worktree = repo / '.worktrees/gen_014'
        worktree.symlink_to(outside, target_is_directory=True)
    (repo / 'data/generations/manifest.json').write_text(json.dumps({'generations': [
        {'gen_id': 'gen_014', 'status': 'active', 'git_commit': 'a' * 40,
         'worktree_path': str(worktree)}]}))
    row = module['collect_snapshot'](repo)['active_generations'][0]
    assert row['worktree_in_boundary'] is False
    assert row['head'] == ''


@pytest.mark.parametrize('condition,exists,status', [
    (False, False, 'absent'),
    (True, True, 'present'),
    (PermissionError('PRIVATE_ERROR'), None, 'inaccessible'),
])
def test_collector_distinguishes_shadow_absence_from_inaccessible_metadata(
        tmp_path, collector_host, condition, exists, status):
    import runpy
    module = runpy.run_path(str(SCRIPT))
    repo = tmp_path / 'repo'
    (repo / 'data/generations').mkdir(parents=True)
    (repo / 'data/generations/manifest.json').write_text(json.dumps({'generations': []}))
    collector_host['shadow'] = condition
    result = module['collect_snapshot'](repo)
    assert result['shadow'] == {'path': str(SHADOW), 'exists': exists,
                                'status': status, 'head': 'c' * 40 if exists else ''}
    assert collector_host['shadow_probes'] == 1
    assert len(collector_host['shadow_git_calls']) == int(exists is True)
    assert 'PRIVATE_ERROR' not in json.dumps(result)
    # The development shadow is advisory; it does not redefine the production
    # boundary assessment or its strict allowlisted report schema.
    production = snapshot()
    production['shadow'] = result['shadow']
    report = module['assess_snapshot'](production, 'a' * 40, 'test-vps')
    assert report['inspection_passed'] is True
    assert 'shadow' not in report


def test_collector_inaccessible_shadow_survives_self_contained_ssh_serialization(
        tmp_path, collector_host):
    import inspect
    import runpy
    module = runpy.run_path(str(SCRIPT))
    reconstructed = {}
    exec(inspect.getsource(module['collect_snapshot']), reconstructed)
    repo = tmp_path / 'repo'
    (repo / 'data/generations').mkdir(parents=True)
    (repo / 'data/generations/manifest.json').write_text(json.dumps({'generations': []}))
    collector_host['shadow'] = PermissionError('PRIVATE_ERROR')
    result = reconstructed['collect_snapshot'](repo)
    assert result['shadow']['exists'] is None
    assert result['shadow']['status'] == 'inaccessible'
    assert collector_host['shadow_git_calls'] == []


@pytest.mark.parametrize('error', [OSError('PRIVATE_ERROR'), RuntimeError('PRIVATE_ERROR')])
def test_collector_does_not_swallow_unrelated_shadow_probe_errors(tmp_path, collector_host, error):
    import runpy
    module = runpy.run_path(str(SCRIPT))
    repo = tmp_path / 'repo'
    (repo / 'data/generations').mkdir(parents=True)
    (repo / 'data/generations/manifest.json').write_text(json.dumps({'generations': []}))
    collector_host['shadow'] = error
    with pytest.raises(type(error), match='PRIVATE_ERROR'):
        module['collect_snapshot'](repo)
    assert collector_host['shadow_git_calls'] == []
