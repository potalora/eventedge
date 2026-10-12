"""Safety boundaries of the local runner; no provider/model calls."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import json
import shutil
import subprocess

import pytest


@pytest.fixture
def runner():
    path = Path(__file__).parents[1] / 'scripts/run_local_continuity.py'
    spec = importlib.util.spec_from_file_location('local_runner', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_window_uses_actual_close_and_next_open(runner):
    assert runner.research_window(datetime(2026, 10, 12, 19, 59, tzinfo=timezone.utc))['allowed'] is False
    assert runner.research_window(datetime(2026, 10, 12, 20, 5, tzinfo=timezone.utc)) == {
        'session': '2026-10-12', 'allowed': True, 'next_open': '2026-10-13T13:30:00+00:00'}


def test_duplicate_claim_cannot_launch_again(runner, tmp_path):
    runner.claim_session(tmp_path, '2026-10-12')
    with pytest.raises(FileExistsError):
        runner.claim_session(tmp_path, '2026-10-12')


def test_every_sqlite_table_and_blob_is_compared(runner, tmp_path):
    path = tmp_path / 'portfolio.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE pending_intents (id INTEGER, value BLOB)')
        db.execute('INSERT INTO pending_intents VALUES (?,?)', (1, b'private'))
    before = runner.state_inventory(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE pending_intents SET value=?', (b'changed',))
    assert before['ledger'] != runner.state_inventory(tmp_path)['ledger']


def test_accepted_evidence_changes_are_detected(runner, tmp_path):
    source = tmp_path / 'source_inputs'
    source.mkdir()
    path = source / 'accepted.json'
    path.write_text('{"digest":"a"}')
    before = runner.state_inventory(tmp_path)
    path.write_text('{"digest":"b"}')
    assert before['source'] != runner.state_inventory(tmp_path)['source']


def test_symlink_in_state_is_rejected(runner, tmp_path):
    (tmp_path / 'bad').symlink_to('/etc/hosts')
    with pytest.raises(ValueError, match='state_symlink'):
        runner.state_inventory(tmp_path)


def test_state_copy_uses_sqlite_backup_including_wal(runner, tmp_path):
    state = tmp_path / 'state'; state.mkdir()
    path = state / 'portfolio.db'
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE fills (id INTEGER)')
    db.execute('INSERT INTO fills VALUES (1)'); db.commit()
    copy = tmp_path / 'copy'
    runner.copy_state(state, copy)
    assert runner.state_inventory(state)['ledger'] == runner.state_inventory(copy)['ledger']
    db.close()


@pytest.fixture
def prepared(runner, tmp_path, monkeypatch):
    repo = tmp_path / 'clone'; repo.mkdir()
    (repo / 'scripts').mkdir()
    shutil.copyfile(Path(runner.__file__), repo / 'scripts/run_local_continuity.py')
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', 'scripts'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Local test', '-c',
                    'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    watchdog = tmp_path / 'watchdog.py'; watchdog.write_text('# fixture; never launched\n')
    monkeypatch.setattr(runner, 'WATCHDOG_SHA', runner.sha(watchdog))
    dotenv = tmp_path / 'dotenv'; dotenv.write_text('# no credentials\n')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'execution': {'mode': 'paper', 'execution_enabled': False},
        'autoresearch': {'decision_clock_policy': 'prospective_next_open_v1',
        'filing_research_policy': 'disabled_v1', 'research_resilience_policy': 'dependency_isolation_v1',
        'llm_billing_mode': 'chatgpt_plan', 'autoresearch_model': 'gpt-6-luna', 'llm_effort': 'high',
        'thesis_model': 'gpt-6.1-sol', 'thesis_effort': 'high',
        'paper_trade': {'portfolio_committee_model': 'gpt-6.1-sol', 'portfolio_committee_effort': 'high'}}}))
    contract = runner.prepare(repo, profile, watchdog, dotenv)
    contract['prepared_at'] = '2026-10-12T17:00:00+00:00'
    runner.save(repo / 'data/local-continuity/contract.json', contract, replace=True)
    return runner, repo, contract


def test_prepare_uses_ordinary_frozen_generation_without_daily_attempt(prepared):
    runner, repo, contract = prepared
    row = runner.generation(repo, contract)
    assert row['run_history'] == []
    assert runner.sha(Path(row['config_file'])) == contract['profile_sha256']
    assert Path(row['worktree_path']).is_dir()
    assert not (Path(row['state_dir']) / 'cohort_metrics.db').exists()


def test_changed_profile_and_added_code_refuse_launch(prepared):
    runner, repo, contract = prepared
    injected = repo / 'scripts/injected.py'; injected.write_text('raise RuntimeError()')
    with pytest.raises(ValueError, match='untracked_application_code'):
        runner.load_contract(repo)
    injected.unlink()
    Path(runner.generation(repo, contract)['config_file']).write_text('{}')
    with pytest.raises(ValueError, match='frozen_input_changed'):
        runner.load_contract(repo)


def test_dotenv_cannot_change_between_sessions(prepared):
    runner, repo, contract = prepared
    Path(contract['dotenv']).write_text('# changed settings\n')
    with pytest.raises(ValueError, match='frozen_input_changed'):
        runner.load_contract(repo)


def test_ambient_behavior_overrides_are_not_forwarded(runner, tmp_path, monkeypatch):
    monkeypatch.setenv('BLOCKED_TICKERS', 'SPY')
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-value')
    monkeypatch.setenv('EVENTEDGE_GENERATION_CONFIG_FILE', '/wrong')
    env = runner.worker_environment(tmp_path, {})
    assert not {'BLOCKED_TICKERS', 'OPENAI_API_KEY', 'EVENTEDGE_GENERATION_CONFIG_FILE'} & env.keys()
    assert env['AUTORESEARCH_FETCH_TIMEOUT_S'] == '900'


def test_linked_worktree_cannot_be_prepared(prepared, tmp_path):
    runner, repo, _ = prepared
    linked = tmp_path / 'linked'
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--detach', str(linked)],
                   check=True, capture_output=True)
    with pytest.raises(ValueError, match='standalone_clone_required'):
        runner.prepare(linked, tmp_path / 'unused-profile', tmp_path / 'unused-watchdog', tmp_path / 'unused-dotenv')


def test_health_reports_missing_and_incomplete_runs_without_retry(prepared):
    runner, repo, _ = prepared
    assert runner.health(repo, now=datetime(2026, 10, 12, 20, 5, tzinfo=timezone.utc))['issues'] == []
    assert runner.health(repo, now=datetime(2026, 10, 12, 20, 16, tzinfo=timezone.utc))['issues'] == ['scheduled_session_missing']
    assert runner.health(repo, now=datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc))['issues'] == ['scheduled_session_missing']
    current = runner.claim_session(repo / 'data/local-continuity/sessions', '2026-10-12')
    runner.save(current / 'started.json', {'started_at': '2026-10-12T20:05:00+00:00'})
    assert runner.health(repo, now=datetime(2026, 10, 12, 21, 10, tzinfo=timezone.utc))['issues'] == ['session_incomplete_after_bounds']
    assert not (current / 'manager-result.json').exists()
