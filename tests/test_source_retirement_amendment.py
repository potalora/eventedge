"""A Congress amendment selects immutable identities without rewriting history."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sqlite3

import pytest


def amended_row(repo):
    state = repo / 'data/generations/gen_028'
    row = {
        'gen_id': 'gen_028', 'git_commit': 'b' * 40,
        'git_branch': 'codex/congress-retirement',
        'worktree_path': str(repo / '.worktrees/gen_028'),
        'state_dir': str(state), 'created_at': '2026-11-06T12:00:00+00:00',
        'status': 'active', 'description': 'Existing books', 'run_history': [],
        'config_file': str(state / 'generation-config-congress-retired.json'),
        'config_sha256': 'd' * 64,
    }
    row['source_retirement'] = {
        'version': 'congress-audit-retirement-v1',
        'effective_session': '2026-11-11',
        'previous_commit': 'a' * 40,
        'previous_config_file': str(state / 'generation-config.json'),
        'previous_config_sha256': 'c' * 64,
        'previous_worktree_path': str(repo / '.worktrees/gen_028-before-congress-retirement'),
        'commit': row['git_commit'], 'config_file': row['config_file'],
        'config_sha256': row['config_sha256'],
    }
    return row


def test_historical_and_boundary_sessions_select_exact_revisions_without_mutating_row(tmp_path):
    from tradingagents.strategies.orchestration import source_retirement as policy
    row = amended_row(tmp_path)
    before = deepcopy(row)
    previous = policy.generation_for_session(row, '2026-11-10')
    current = policy.generation_for_session(row, '2026-11-11')
    assert previous['git_commit'] == 'a' * 40
    assert previous['worktree_path'] == str(tmp_path / '.worktrees/gen_028-before-congress-retirement')
    assert previous['config_sha256'] == 'c' * 64
    assert policy.config_path_for_generation(previous) == tmp_path / 'data/generations/gen_028/generation-config.json'
    assert current['git_commit'] == 'b' * 40
    assert current['config_sha256'] == 'd' * 64
    assert current['state_dir'] == previous['state_dir'] == row['state_dir']
    assert 'source_retirement' not in current and 'source_retirement' not in previous
    previous['run_history'].append({'date': '2026-11-10'})
    assert row == before


@pytest.mark.parametrize('change', [
    lambda row: row['source_retirement'].update(extra=True),
    lambda row: row['source_retirement'].update(version='unknown'),
    lambda row: row['source_retirement'].update(effective_session='2026-02-30'),
    lambda row: row['source_retirement'].update(effective_session='2026-11-11T00:00:00Z'),
    lambda row: row['source_retirement'].update(commit='e' * 40),
    lambda row: row['source_retirement'].update(previous_commit='b' * 40),
    lambda row: row['source_retirement'].update(previous_config_sha256='d' * 64),
    lambda row: row['source_retirement'].update(previous_worktree_path='/foreign/old'),
    lambda row: row.update(config_file='/foreign/profile.json'),
    lambda row: row.update(state_dir='/foreign/books'),
    lambda row: row.update(state_dir='/x'),
    lambda row: row.update(worktree_path='/foreign/worker'),
    lambda row: row.update(_source_retirement_resolved=True),
])
def test_conflicting_or_forged_amendments_fail_closed(tmp_path, change):
    from tradingagents.strategies.orchestration import source_retirement as policy
    row = amended_row(tmp_path)
    change(row)
    with pytest.raises(ValueError, match='source_retirement'):
        policy.validate_generation_retirement(row, repo=tmp_path)


def test_existing_manifest_without_amendment_preserves_identity(tmp_path):
    from tradingagents.strategies.orchestration import source_retirement as policy
    row = amended_row(tmp_path)
    row.pop('source_retirement')
    row['config_file'] = str(tmp_path / 'data/generations/gen_028/generation-config.json')
    assert policy.validate_generation_retirement(row, repo=tmp_path) is None
    assert policy.generation_for_session(row, '2026-11-10') == row


def test_manager_accepts_only_joined_retirement_manifest(tmp_path):
    from tradingagents.strategies.orchestration.generation_manager import GenerationManager, GenerationManifestError
    row = amended_row(tmp_path)
    GenerationManager._validate_manifest({'generations': [row]})
    row['source_retirement']['config_sha256'] = 'e' * 64
    with pytest.raises(GenerationManifestError):
        GenerationManager._validate_manifest({'generations': [row]})


def test_manager_rejects_amendment_without_frozen_config_identity(tmp_path):
    from tradingagents.strategies.orchestration.generation_manager import GenerationManager, GenerationManifestError
    row = amended_row(tmp_path)
    row.pop('config_file')
    row.pop('config_sha256')
    with pytest.raises(GenerationManifestError):
        GenerationManager._validate_manifest({'generations': [row]})


@pytest.fixture
def deployed_amendment(tmp_path, request):
    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*args):
        return subprocess.run(['git', *args], cwd=repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    git('init')
    git('config', 'user.email', 'offline@example.invalid')
    git('config', 'user.name', 'Offline test')
    options = getattr(request, 'param', {})
    (repo / '.gitignore').write_text(('data/\n' if options.get('ignore_data', True) else '') + '.worktrees/\n')
    (repo / 'tracked.txt').write_text('original\n')
    git('add', '.gitignore', 'tracked.txt')
    if options.get('tracked_data'):
        tracked_data = repo / 'data/tracked-fixture.txt'
        tracked_data.parent.mkdir()
        tracked_data.write_text('original tracked data\n')
        git('add', '-f', 'data/tracked-fixture.txt')
    git('commit', '-m', 'original fixture')
    previous_commit = git('rev-parse', 'HEAD')
    (repo / 'tracked.txt').write_text('Congress retired\n')
    git('commit', '-am', 'retired fixture')
    commit = git('rev-parse', 'HEAD')
    row = amended_row(repo)
    row['git_commit'] = row['source_retirement']['commit'] = commit
    row['source_retirement']['previous_commit'] = previous_commit
    for field, sha in (('worktree_path', commit), ('previous_worktree_path', previous_commit)):
        target = row[field] if field in row else row['source_retirement'][field]
        git('worktree', 'add', '--detach', target, sha)
    state = Path(row['state_dir'])
    state.mkdir(parents=True)
    original = (Path(__file__).parents[1] / 'config/profiles/stabilization-v3.json').read_text()
    old_digest = hashlib.sha256(original.encode()).hexdigest()
    config = json.loads(original)
    config['autoresearch']['disabled_strategies']['congressional_trades'] = 'congress_source_retired_v1'
    config['autoresearch']['retired_sources']['congress'] = {
        'reason': 'congress_source_retired_v1', 'strategies': ['congressional_trades']}
    config['autoresearch']['congress_disclosure_policy'] = None
    config['autoresearch']['congress_audit_continuity_policy'] = None
    config['source_retirement'] = {
        'version': 'congress-audit-retirement-v1', 'effective_session': '2026-11-11',
        'previous_config_sha256': old_digest, 'previous_config_text': original}
    current = json.dumps(config, indent=2) + '\n'
    Path(row['source_retirement']['previous_config_file']).write_text(original)
    Path(row['config_file']).write_text(current)
    row['source_retirement']['previous_config_sha256'] = old_digest
    row['config_sha256'] = row['source_retirement']['config_sha256'] = hashlib.sha256(current.encode()).hexdigest()
    return repo, row


def test_runtime_verifies_selected_clean_historical_and_current_worktrees(deployed_amendment):
    from tradingagents.strategies.orchestration.runtime_recovery import identity_for
    repo, row = deployed_amendment
    previous = identity_for(repo, row, '2026-11-10')
    current = identity_for(repo, row, '2026-11-11')
    assert previous['generation_commit'] == row['source_retirement']['previous_commit']
    assert current['generation_commit'] == row['git_commit']
    assert previous['state_dir'] == current['state_dir'] == row['state_dir']
    assert previous['config_sha256'] == row['source_retirement']['previous_config_sha256']
    assert current['config_sha256'] == row['config_sha256']
    # Custody of the old checkout moves; immutable incident identity does not.
    assert previous['worktree_path'] == row['worktree_path']
    Path(row['source_retirement']['previous_worktree_path'], 'tracked.txt').write_text('tampered\n')
    with pytest.raises(ValueError):
        identity_for(repo, row, '2026-11-10')
    # A dirty historical checkout does not alter the selected current checkout.
    assert identity_for(repo, row, '2026-11-11') == current


def test_runtime_rejects_current_profile_proof_bound_to_another_boundary(deployed_amendment):
    from tradingagents.strategies.orchestration.runtime_recovery import identity_for
    repo, row = deployed_amendment
    path = Path(row['config_file'])
    document = json.loads(path.read_text())
    document['source_retirement']['effective_session'] = '2026-11-12'
    path.write_text(json.dumps(document))
    row['config_sha256'] = row['source_retirement']['config_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        identity_for(repo, row, '2026-11-11')


def test_recovery_receipt_configuration_uses_bound_revision_and_rejects_early_amended_proof(deployed_amendment):
    from tradingagents.strategies.orchestration import runtime_recovery as recovery
    _, row = deployed_amendment
    state = Path(row['state_dir'])
    recovery._read_recovery_configuration(state, row['source_retirement']['previous_config_sha256'], '2026-11-10')
    recovery._read_recovery_configuration(state, row['config_sha256'], '2026-11-11')
    with pytest.raises(ValueError):
        recovery._read_recovery_configuration(state, row['config_sha256'], '2026-11-10')


def test_completed_historical_controller_incident_keeps_original_key_and_bytes(deployed_amendment):
    from scripts import recover_runtime as controller
    from tradingagents.strategies.orchestration import runtime_recovery as recovery
    from datetime import datetime, timezone
    repo, row = deployed_amendment
    row['created_at'] = '2026-11-10T00:00:00+00:00'
    (repo / 'data/generations/manifest.json').write_text(json.dumps({'generations': [row]}))
    identity = dict(generation_id=row['gen_id'], generation_commit=row['source_retirement']['previous_commit'],
        config_sha256=row['source_retirement']['previous_config_sha256'], session='2026-11-10',
        state_dir=row['state_dir'], worktree_path=row['worktree_path'])
    now = datetime(2026, 11, 11, 18, tzinfo=timezone.utc)
    record = recovery.new_incident(identity, now)
    record['daily_started'] = True
    recovery.finish_attempt(record, recovery._result('recovered', 'complete'), now)
    key = recovery.incident_key(identity)
    document = dict(version=recovery.VERSION, schema_version=1, incidents={key: record})
    path = repo / 'data/runtime_recovery/journal.json'
    recovery.atomic_json(path, document)
    before = path.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError('completed historical evidence must not relaunch or regenerate')
    result = controller.tick(repo, now, clock=lambda: now, discover=lambda *args: [],
                             launcher=forbidden, reporter=forbidden)
    assert result['controller_reason_code'] is None
    assert path.read_bytes() == before
    assert list(json.loads(path.read_text())['incidents']) == [key]


@pytest.mark.parametrize('action', ['daily', 'recovery'])
def test_historical_recovery_launch_refuses_before_any_child(deployed_amendment, monkeypatch, action):
    from scripts import recover_runtime as controller
    repo, row = deployed_amendment
    (repo / 'data/generations/manifest.json').write_text(json.dumps({'generations': [row]}))
    def forbidden(*args, **kwargs):
        raise AssertionError('historical recovery cannot run new code labelled with the old commit')
    monkeypatch.setattr(controller, 'bounded_process', forbidden)
    monkeypatch.setattr(controller, '_daily_process', forbidden)
    with pytest.raises(ValueError, match='identity_mismatch'):
        controller.launch(repo, row, '2026-11-10', action)


@pytest.fixture
def unapplied_retirement(deployed_amendment):
    repo, amended = deployed_amendment
    binding = amended['source_retirement']
    def git(*args):
        return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    git('worktree', 'remove', amended['worktree_path'])
    git('worktree', 'move', binding['previous_worktree_path'], amended['worktree_path'])
    git('checkout', '--detach', binding['previous_commit'])
    Path(amended['config_file']).unlink()
    old = deepcopy(amended)
    old.pop('source_retirement')
    old.update(git_commit=binding['previous_commit'], config_file=binding['previous_config_file'],
               config_sha256=binding['previous_config_sha256'])
    manifest = repo / 'data/generations/manifest.json'
    manifest.write_text(json.dumps({'generations': [old]}, indent=2) + '\n')
    from tradingagents.strategies.orchestration.operating_contract import ROSTER
    for name in ROSTER:
        book = Path(old['state_dir']) / name / 'portfolio.db'
        book.parent.mkdir()
        with sqlite3.connect(book) as connection:
            connection.executescript('''
                CREATE TABLE session_execution_contexts(session TEXT, cohort_id TEXT);
                CREATE TABLE staging_runs(session TEXT, cohort_id TEXT);
                CREATE TABLE cash_events(amount TEXT);
                INSERT INTO cash_events VALUES ('1234.56');
                CREATE TABLE pending_intents(intent_id TEXT);
                INSERT INTO pending_intents VALUES ('existing-intent');
            ''')
    lock = repo / 'data/operational/eventedge-runtime.lock'
    lock.parent.mkdir()
    lock.touch()
    args = dict(repo=repo, generation='gen_028', effective_session='2026-11-11',
                old_commit=binding['previous_commit'], old_profile_sha256=binding['previous_config_sha256'],
                commit=binding['commit'], expected_profile_sha256=binding['config_sha256'])
    return repo, old, amended, args


def protected_bytes(repo, old):
    state = Path(old['state_dir'])
    return {str(path.relative_to(repo)): path.read_bytes()
            for path in [Path(old['config_file']), *sorted(state.glob('*/portfolio.db'))]}


def test_cli_dry_run_apply_and_identical_retry_preserve_populated_books(unapplied_retirement):
    from scripts import retire_congress as cli
    repo, old, amended, args = unapplied_retirement
    before = protected_bytes(repo, old)
    manifest = repo / 'data/generations/manifest.json'
    original_manifest = manifest.read_bytes()
    plan = cli.retire_congress(**args)
    assert plan['new_profile_sha256'] == amended['config_sha256']
    assert manifest.read_bytes() == original_manifest
    assert protected_bytes(repo, old) == before
    applied = cli.retire_congress(**args, apply=True, scheduler_quiesced=True)
    assert applied['changed'] is True
    assert applied['already_applied'] is False
    assert json.loads(manifest.read_text())['generations'][0]['source_retirement'] == amended['source_retirement']
    assert protected_bytes(repo, old) == before
    repeated = cli.retire_congress(**args, apply=True, scheduler_quiesced=True)
    assert repeated['changed'] is False and repeated['already_applied'] is True
    assert protected_bytes(repo, old) == before
    assert subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip() == args['commit']


@pytest.mark.parametrize('deployed_amendment', [{'ignore_data': False}], indirect=True)
def test_cli_allows_only_authoritative_untracked_host_data_without_ignoring_it(unapplied_retirement):
    from scripts import retire_congress as cli
    repo, old, _, args = unapplied_retirement
    def git(*parts):
        return subprocess.check_output(['git', '-C', str(repo), *parts], text=True).strip()
    assert git('status', '--porcelain', '--untracked-files=normal') == '?? data/'
    before = protected_bytes(repo, old)
    assert cli.retire_congress(**args)['changed'] is False
    assert cli.retire_congress(**args, apply=True, scheduler_quiesced=True)['changed'] is True
    assert cli.retire_congress(**args, apply=True, scheduler_quiesced=True)['already_applied'] is True
    assert protected_bytes(repo, old) == before
    assert git('status', '--porcelain', '--untracked-files=normal') == '?? data/'


@pytest.mark.parametrize('deployed_amendment', [{'ignore_data': False, 'tracked_data': True}], indirect=True)
@pytest.mark.parametrize('change', ['untracked_source', 'untracked_prefix', 'tracked_source', 'staged_source',
    'tracked_data', 'staged_data', 'staged_reverted_source', 'staged_reverted_data'])
def test_cli_still_rejects_untracked_source_and_all_tracked_changes(unapplied_retirement, change):
    from scripts import retire_congress as cli
    repo, old, _, args = unapplied_retirement
    target = (repo / 'untracked.py' if change == 'untracked_source' else
              repo / 'database/untracked.py' if change == 'untracked_prefix' else
              repo / 'data/tracked-fixture.txt' if change.endswith('data') else repo / 'tracked.txt')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('changed source or tracked data\n')
    if change.startswith('staged_'):
        subprocess.run(['git', '-C', str(repo), 'add', str(target)], check=True, capture_output=True)
        if 'reverted' in change:
            # The index is still dirty even when the working file again matches
            # HEAD; a combined diff against HEAD alone misses this state.
            target.write_text('original tracked data\n' if change.endswith('data') else 'original\n')
    before = protected_bytes(repo, old)
    manifest = (repo / 'data/generations/manifest.json').read_bytes()
    with pytest.raises(ValueError, match='host_checkout_changed'):
        cli.retire_congress(**args, apply=True, scheduler_quiesced=True)
    assert protected_bytes(repo, old) == before
    assert (repo / 'data/generations/manifest.json').read_bytes() == manifest
    assert not (Path(old['state_dir']) / cli.JOURNAL_NAME).exists()


@pytest.mark.parametrize('deployed_amendment', [{'ignore_data': False}], indirect=True)
@pytest.mark.parametrize('location', ['active', 'archive'])
def test_host_data_exception_never_allows_untracked_worktree_data(unapplied_retirement, location):
    from scripts import retire_congress as cli
    repo, old, amended, args = unapplied_retirement
    if location == 'archive':
        cli.retire_congress(**args, apply=True, scheduler_quiesced=True)
    worktree = Path(old['worktree_path'] if location == 'active' else amended['source_retirement']['previous_worktree_path'])
    unexpected = worktree / 'data/untracked-source.py'
    unexpected.parent.mkdir()
    unexpected.write_text('unexpected worktree contents\n')
    before = protected_bytes(repo, old)
    with pytest.raises(ValueError, match='worktree_changed'):
        cli.retire_congress(**args, apply=True, scheduler_quiesced=True)
    assert protected_bytes(repo, old) == before


@pytest.mark.parametrize('stage', ['profile', 'move', 'add', 'checkout', 'manifest'])
def test_cli_crash_retry_finishes_only_its_own_recorded_transaction(unapplied_retirement, monkeypatch, stage):
    from scripts import retire_congress as cli
    repo, old, _, args = unapplied_retirement
    before = protected_bytes(repo, old)
    original_git, original_write = cli._git, cli._write_atomic
    def git(repo, *parts):
        result = original_git(repo, *parts)
        if ((stage == 'move' and parts[:2] == ('worktree', 'move'))
                or (stage == 'add' and parts[:2] == ('worktree', 'add'))
                or (stage == 'checkout' and parts[:2] == ('checkout', '--detach'))):
            raise InterruptedError('simulated process loss after git mutation')
        return result
    def write(path, body, *, exclusive=False):
        result = original_write(path, body, exclusive=exclusive)
        if ((stage == 'profile' and path.name == 'generation-config-congress-retired.json')
                or (stage == 'manifest' and path.name == 'manifest.json')):
            raise InterruptedError('simulated process loss after publication')
        return result
    monkeypatch.setattr(cli, '_git', git)
    monkeypatch.setattr(cli, '_write_atomic', write)
    with pytest.raises(InterruptedError):
        cli.retire_congress(**args, apply=True, scheduler_quiesced=True)
    assert protected_bytes(repo, old) == before
    monkeypatch.setattr(cli, '_git', original_git)
    monkeypatch.setattr(cli, '_write_atomic', original_write)
    cli.retire_congress(**args, apply=True, scheduler_quiesced=True)
    assert protected_bytes(repo, old) == before


def test_cli_refuses_busy_lock_foreign_journal_and_already_accepted_boundary(unapplied_retirement):
    from scripts import retire_congress as cli
    from tradingagents.strategies.orchestration.runtime_lock import runtime_lock, canonical_runtime_lock_path, RuntimeLockBusy
    repo, old, _, args = unapplied_retirement
    with runtime_lock(canonical_runtime_lock_path(repo), exclusive=True):
        with pytest.raises(RuntimeLockBusy):
            cli.retire_congress(**args, apply=True, scheduler_quiesced=True)
    journal = Path(old['state_dir']) / '.pending-congress-retirement.json'
    journal.write_text('{"foreign": true}')
    with pytest.raises(ValueError):
        cli.retire_congress(**args, apply=True, scheduler_quiesced=True)
    journal.unlink()
    book = next(Path(old['state_dir']).glob('*/portfolio.db'))
    with sqlite3.connect(book) as connection:
        connection.execute('INSERT INTO session_execution_contexts VALUES (?,?)', ('2026-11-11', book.parent.name))
    with pytest.raises(ValueError):
        cli.retire_congress(**args, apply=True, scheduler_quiesced=True)
    assert not journal.exists()


def test_cli_understands_real_tagged_accepted_inputs_and_rejects_future_slot(unapplied_retirement):
    from scripts import retire_congress as cli
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    from datetime import datetime, timezone
    repo, old, _, args = unapplied_retirement
    store = SourceInputStore(repo / 'data/source-cache', accepted_dir=Path(old['state_dir']) / 'source_inputs')
    store.freeze({'generation': 'gen_028', 'session': '2026-11-10'}, {'retained': True},
                 acquired_at=datetime(2026, 11, 10, 23, tzinfo=timezone.utc))
    assert cli.retire_congress(**args)['changed'] is False
    store.freeze({'generation': 'gen_028', 'session': '2026-11-11'}, {'retained': True},
                 acquired_at=datetime(2026, 11, 11, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match='accepted_boundary'):
        cli.retire_congress(**args, apply=True, scheduler_quiesced=True)
    assert not (Path(old['state_dir']) / cli.JOURNAL_NAME).exists()


def test_completed_cli_retry_preserves_later_natural_run_history(unapplied_retirement):
    from scripts import retire_congress as cli
    repo, old, _, args = unapplied_retirement
    cli.retire_congress(**args, apply=True, scheduler_quiesced=True)
    manifest = repo / 'data/generations/manifest.json'
    document = json.loads(manifest.read_text())
    document['generations'][0]['run_history'].append({'date': '2026-11-11', 'success': True})
    manifest.write_text(json.dumps(document, indent=2) + '\n')
    before = manifest.read_bytes(), protected_bytes(repo, old)
    assert cli.retire_congress(**args, apply=True, scheduler_quiesced=True)['changed'] is False
    assert (manifest.read_bytes(), protected_bytes(repo, old)) == before


@pytest.mark.parametrize('accepted', [False, True])
def test_cli_rollback_requires_owned_unchanged_pre_boundary_books(unapplied_retirement, accepted):
    from scripts import retire_congress as cli
    repo, old, _, args = unapplied_retirement
    path = repo / 'data/generations/manifest.json'
    before_manifest, before_books = path.read_bytes(), protected_bytes(repo, old)
    cli.retire_congress(**args, apply=True, scheduler_quiesced=True)
    if accepted:
        book = next(Path(old['state_dir']).glob('*/portfolio.db'))
        with sqlite3.connect(book) as connection:
            connection.execute('INSERT INTO session_execution_contexts VALUES (?,?)', ('2026-11-11', book.parent.name))
        before = path.read_bytes(), protected_bytes(repo, old)
        with pytest.raises(ValueError, match='accepted_boundary'):
            cli.retire_congress(**args, apply=True, scheduler_quiesced=True, rollback=True)
        assert (path.read_bytes(), protected_bytes(repo, old)) == before
    else:
        result = cli.retire_congress(**args, apply=True, scheduler_quiesced=True, rollback=True)
        assert result['changed'] is True
        assert path.read_bytes() == before_manifest
        assert protected_bytes(repo, old) == before_books
        assert not (repo / '.worktrees/gen_028-before-congress-retirement').exists()
        assert cli.retire_congress(**args, apply=True, scheduler_quiesced=True, rollback=True)['changed'] is False


def readiness_amendment(tmp_path):
    from test_release_readiness import _fixture, _git
    from tradingagents.strategies.orchestration.source_retirement_profile import make_retired_profile
    repo, state, manifest = _fixture(tmp_path)
    row = manifest['generations'][0]
    previous_commit = row['git_commit']
    original = (Path(__file__).parents[1] / 'config/profiles/stabilization-v3.json').read_text()
    old_config = state / 'generation-config.json'
    old_config.write_text(original)
    digest = hashlib.sha256(original.encode()).hexdigest()
    new_config = state / 'generation-config-congress-retired.json'
    body = json.dumps(make_retired_profile(original, '2026-09-22'), indent=2) + '\n'
    new_config.write_text(body)
    archive = repo / '.worktrees/gen_015-before-congress-retirement'
    _git(repo, 'worktree', 'move', row['worktree_path'], str(archive))
    (repo / 'source.py').write_text('retired = True\n')
    _git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-am', 'retired fixture')
    commit = _git(repo, 'rev-parse', 'HEAD')
    _git(repo, 'worktree', 'add', '--detach', row['worktree_path'], commit)
    row.update(git_commit=commit, config_file=str(new_config), config_sha256=hashlib.sha256(body.encode()).hexdigest())
    row['source_retirement'] = dict(version='congress-audit-retirement-v1', effective_session='2026-09-22',
        previous_commit=previous_commit, previous_config_file=str(old_config), previous_config_sha256=digest,
        previous_worktree_path=str(archive), commit=commit, config_file=str(new_config), config_sha256=row['config_sha256'])
    (state.parent / 'manifest.json').write_text(json.dumps(manifest))
    return repo, row


def test_release_readiness_resolves_old_session_epoch_against_original_commit(tmp_path):
    from tradingagents.strategies.orchestration.release_readiness import assess_generation
    from datetime import date
    repo, row = readiness_amendment(tmp_path)
    result = assess_generation(repo, row['gen_id'], row['git_commit'], date(2026, 9, 21), sessions=1)
    assert result['sessions'][0]['epoch_id'] == 'epoch'
    # The deliberately incomplete supported-profile fixture must remain unready.
    assert result['ready'] is False


def test_release_readiness_fingerprints_current_amended_profile(tmp_path, monkeypatch):
    from tradingagents.strategies.orchestration import release_readiness as readiness
    from datetime import date
    repo, row = readiness_amendment(tmp_path)
    def concurrent_change(*args, **kwargs):
        path = Path(row['config_file'])
        path.write_text(path.read_text() + '\n')
        return {'ready': False}
    monkeypatch.setattr(readiness, '_assess_generation', concurrent_change)
    with pytest.raises(ValueError, match='state changed'):
        readiness.assess_generation(repo, row['gen_id'], row['git_commit'], date(2026, 9, 21), sessions=1)
