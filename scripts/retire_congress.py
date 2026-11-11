#!/usr/bin/env python3
"""Explicit, journaled Congress-only amendment of existing generation books.

The operator must quiesce schedules and retain complete recovery material first.
Default mode only validates and prints the exact proposed identities. No command
in this tool launches trading, providers, models, notification or service work.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.strategies.orchestration.generation_config import read_generation_config
from tradingagents.strategies.orchestration.generation_manager import GenerationManager
from tradingagents.strategies.orchestration.operating_contract import ROSTER
from tradingagents.strategies.orchestration.runtime_lock import canonical_runtime_lock_path, runtime_lock
from tradingagents.strategies.orchestration.runtime_recovery import load_journal
from tradingagents.strategies.orchestration.source_retirement import CONFIG_NAME, VERSION, validate_generation_retirement
from tradingagents.strategies.orchestration.source_retirement_profile import make_retired_profile
from tradingagents.strategies.orchestration.trading_calendar import is_session

JOURNAL_NAME = '.pending-congress-retirement.json'
MAX_JOURNAL = 8 * 1024 * 1024


def _invalid(reason):
    raise ValueError('congress_retirement_' + reason)


def _pairs(entries):
    result = {}
    for key, value in entries:
        if key in result:
            _invalid('duplicate_field')
        result[key] = value
    return result


def _json(body):
    return json.loads(body, object_pairs_hook=_pairs)


def _read(path, limit):
    if path.resolve() != path or any(part.is_symlink() for part in (path, *path.parents)):
        _invalid('unsafe_path')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 <= info.st_size <= limit:
            _invalid('file_bounds')
        body = handle.read(limit + 1)
        final = os.fstat(handle.fileno())
        fingerprint = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if len(body) > limit or fingerprint(final) != fingerprint(info):
            _invalid('file_changed')
    return body


def _sync(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomic(path, body, *, exclusive=False):
    if path.parent.resolve() != path.parent or path.is_symlink():
        _invalid('unsafe_publication')
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.congress-retirement-')
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            os.link(temporary, path, follow_symlinks=False)
        else:
            os.replace(temporary, path)
        _sync(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _git(repo, *parts):
    return subprocess.run(['git', '-C', str(repo), *parts], check=True,
                          capture_output=True, text=True, timeout=60).stdout.strip()


def _verify_worktree(repo, path, commit):
    if path.resolve() != path or any(part.is_symlink() for part in (path, *path.parents)):
        _invalid('unsafe_worktree')
    if _git(path, 'rev-parse', 'HEAD') != commit or _git(path, 'status', '--porcelain', '--untracked-files=normal'):
        _invalid('worktree_changed')
    common = Path(_git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    admin = Path(_git(path, 'rev-parse', '--absolute-git-dir'))
    if (common != repo / '.git' or admin.parent != repo / '.git/worktrees'
            or _read(admin / 'gitdir', 4096).decode().strip() != str(path / '.git')
            or (path / '.git').is_symlink() or not (path / '.git').is_file()):
        _invalid('worktree_identity')


def _protected(row):
    paths = [Path(row['config_file'])]
    state = Path(row['state_dir'])
    for name in ROSTER:
        database = state / name / 'portfolio.db'
        if not database.is_file():
            _invalid('incomplete_book_roster')
        paths.append(database)
        for suffix in ('-wal', '-shm'):
            adjacent = Path(str(database) + suffix)
            if adjacent.exists() or adjacent.is_symlink():
                paths.append(adjacent)
    return {str(path): hashlib.sha256(_read(path, 256 * 1024 * 1024)).hexdigest() for path in paths}


def _assert_idle(repo, row, effective):
    """Inspect actual accepted state and book phases, independently of CLI flags."""
    from tradingagents.strategies.orchestration.source_inputs import (
        SourceInputStore, FULL_FILING_MAX_BYTES, FULL_FILING_MAX_NODES, CONTRACT_VERSION,
    )
    for entry in row.get('run_history', []):
        session = entry.get('date', entry.get('session'))
        if not isinstance(session, str) or date.fromisoformat(session).isoformat() != session:
            _invalid('invalid_history')
        if session >= effective:
            _invalid('accepted_boundary')
    controller = repo / 'data/runtime_recovery/journal.json'
    if controller.exists() or controller.is_symlink():
        _read(controller, 262144)
        records = load_journal(controller)['incidents'].values()
        if any(record['running'] or (record['identity']['generation_id'] == row['gen_id']
                and record['disposition'] not in {'recovered', 'wait_next_session'}) for record in records):
            _invalid('unfinished_runtime')
    state = Path(row['state_dir'])
    # Inspect private copies, never open authoritative SQLite databases directly.
    for name in ROSTER:
        source = state / name / 'portfolio.db'
        with tempfile.TemporaryDirectory(prefix='eventedge-retirement-check-') as scratch:
            target = Path(scratch) / 'portfolio.db'
            target.write_bytes(_read(source, 256 * 1024 * 1024))
            for suffix in ('-wal', '-shm'):
                adjacent = Path(str(source) + suffix)
                if adjacent.exists() or adjacent.is_symlink():
                    Path(str(target) + suffix).write_bytes(_read(adjacent, 256 * 1024 * 1024))
            with sqlite3.connect(target) as connection:
                if connection.execute('SELECT 1 FROM session_execution_contexts WHERE session>=? LIMIT 1', (effective,)).fetchone():
                    _invalid('accepted_boundary')
                if connection.execute('SELECT 1 FROM session_execution_contexts c LEFT JOIN staging_runs s '
                        'ON c.session=s.session AND c.cohort_id=s.cohort_id WHERE s.session IS NULL LIMIT 1').fetchone():
                    _invalid('unfinished_book')
    accepted = state / 'source_inputs'
    total, count = 0, 0
    if accepted.exists() or accepted.is_symlink():
        pending = [accepted]
        while pending:
            directory = pending.pop()
            if directory.is_symlink() or directory.resolve() != directory:
                _invalid('unsafe_accepted_inputs')
            for path in directory.iterdir():
                count += 1
                if count > 4096 or path.is_symlink():
                    _invalid('accepted_input_bounds')
                if path.is_dir():
                    pending.append(path)
                elif path.suffix == '.json':
                    body = _read(path, FULL_FILING_MAX_BYTES)
                    total += len(body)
                    if total > 1024 * 1024 * 1024:
                        _invalid('accepted_input_bounds')
                    document = SourceInputStore.decode(body.decode('utf-8'),
                        max_bytes=FULL_FILING_MAX_BYTES, max_nodes=FULL_FILING_MAX_NODES)
                    if (not isinstance(document, dict)
                            or set(document) != {'version', 'identity', 'acquired_at', 'payload', 'digest'}
                            or document['version'] != CONTRACT_VERSION
                            or not isinstance(document['identity'], dict)
                            or document['identity'].get('generation') != row['gen_id']):
                        _invalid('invalid_accepted_identity')
                    session = document.get('identity', {}).get('session')
                    if not isinstance(session, str) or date.fromisoformat(session).isoformat() != session:
                        _invalid('invalid_accepted_identity')
                    if session >= effective:
                        _invalid('accepted_boundary')


def _proposal(repo, manifest_text, parameters):
    manifest = _json(manifest_text)
    GenerationManager._validate_manifest(manifest)
    matches = [row for row in manifest['generations'] if row['gen_id'] == parameters['generation']]
    if len(matches) != 1:
        _invalid('generation_identity')
    old = matches[0]
    if (old.get('source_retirement') is not None or old['status'] != 'active'
            or old['git_commit'] != parameters['old_commit']
            or old['config_sha256'] != parameters['old_profile_sha256']
            or old['state_dir'] != str(repo / 'data/generations' / old['gen_id'])
            or old['worktree_path'] != str(repo / '.worktrees' / old['gen_id'])):
        _invalid('old_identity')
    original, _, _ = read_generation_config(old['config_file'], old['config_sha256'])
    profile = make_retired_profile(original.decode('utf-8'), parameters['effective_session'])
    profile_text = json.dumps(profile, indent=2) + '\n'
    digest = hashlib.sha256(profile_text.encode()).hexdigest()
    if parameters['expected_profile_sha256'] is not None and parameters['expected_profile_sha256'] != digest:
        _invalid('new_profile_pin')
    new = deepcopy(old)
    new.update(git_commit=parameters['commit'], config_file=str(Path(old['state_dir']) / CONFIG_NAME), config_sha256=digest)
    new['source_retirement'] = {
        'version': VERSION, 'effective_session': parameters['effective_session'],
        'previous_commit': old['git_commit'], 'previous_config_file': old['config_file'],
        'previous_config_sha256': old['config_sha256'],
        'previous_worktree_path': str(repo / '.worktrees' / (old['gen_id'] + '-before-congress-retirement')),
        'commit': new['git_commit'], 'config_file': new['config_file'], 'config_sha256': digest,
    }
    validate_generation_retirement(new, repo=repo)
    manifest['generations'][manifest['generations'].index(old)] = new
    return old, new, profile_text, json.dumps(manifest, indent=2) + '\n'


def retire_congress(*, repo, generation, effective_session, old_commit, old_profile_sha256,
                    commit, expected_profile_sha256=None, apply=False, scheduler_quiesced=False, rollback=False):
    repo = Path(repo).absolute()
    if repo.resolve() != repo or _git(repo, 'rev-parse', '--show-toplevel') != str(repo) or not (repo / '.git').is_dir():
        _invalid('canonical_repository')
    if not re.fullmatch(r'gen_[0-9]{3,}', generation):
        _invalid('generation_identity')
    for value, width in ((old_commit, 40), (commit, 40), (old_profile_sha256, 64)):
        if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{%d}' % width, value):
            _invalid('invalid_pin')
    if (date.fromisoformat(effective_session).isoformat() != effective_session
            or not is_session(date.fromisoformat(effective_session)) or commit == old_commit):
        _invalid('invalid_boundary')
    if apply and (not scheduler_quiesced or expected_profile_sha256 is None):
        _invalid('approval_pins_required')
    parameters = dict(generation=generation, effective_session=effective_session, old_commit=old_commit,
                      old_profile_sha256=old_profile_sha256, commit=commit,
                      expected_profile_sha256=expected_profile_sha256)
    lock_path = canonical_runtime_lock_path(repo)
    if not lock_path.is_file():
        _invalid('existing_runtime_lock_required')
    with runtime_lock(lock_path, exclusive=True):
        manifest_path = repo / 'data/generations/manifest.json'
        manifest_body = _read(manifest_path, 2 * 1024 * 1024)
        journal_path = repo / 'data/generations' / generation / JOURNAL_NAME
        journal = None
        if journal_path.exists() or journal_path.is_symlink():
            journal = _json(_read(journal_path, MAX_JOURNAL))
            journal_stat = journal_path.stat()
            if journal_stat.st_uid != os.geteuid() or stat.S_IMODE(journal_stat.st_mode) != 0o600 or journal_stat.st_nlink != 1:
                _invalid('foreign_journal')
            if (type(journal) is not dict or set(journal) != {'version', 'parameters', 'manifest_before', 'manifest_after', 'profile_after', 'protected'}
                    or journal['version'] != VERSION or journal['parameters'] != parameters):
                _invalid('foreign_journal')
            original_manifest = journal['manifest_before']
        else:
            original_manifest = manifest_body.decode('utf-8')
        old, new, profile_text, manifest_text = _proposal(repo, original_manifest, parameters)
        if journal is not None and (journal['manifest_after'] != manifest_text or journal['profile_after'] != profile_text):
            _invalid('foreign_journal')
        current_document = _json(manifest_body)
        GenerationManager._validate_manifest(current_document)
        proposed_document = _json(manifest_text)
        completed = deepcopy(current_document)
        # Natural future samples may append run history after a completed apply;
        # identical reapplication must leave that evidence untouched.
        for row in completed['generations']:
            if row['gen_id'] == generation:
                row['run_history'] = deepcopy(new['run_history'])
        is_completed = completed == proposed_document
        if manifest_body != original_manifest.encode() and not is_completed:
            _invalid('manifest_changed')
        active, archive = Path(old['worktree_path']), Path(new['source_retirement']['previous_worktree_path'])
        profile_path = Path(new['config_file'])
        _git(repo, 'cat-file', '-e', commit + '^{commit}')
        _git(repo, 'merge-base', '--is-ancestor', old_commit, commit)
        if _git(repo, 'status', '--porcelain', '--untracked-files=normal'):
            _invalid('host_checkout_changed')
        if _git(repo, 'rev-parse', 'HEAD') not in (old_commit, commit):
            _invalid('host_commit_changed')
        if rollback:
            if journal is None or manifest_body not in (original_manifest.encode(), manifest_text.encode()):
                _invalid('rollback_not_owned')
            _assert_idle(repo, old, effective_session)
            if _protected(old) != journal['protected']:
                _invalid('protected_state_changed')
            if profile_path.exists() and _read(profile_path, 1024 * 1024) != profile_text.encode():
                _invalid('profile_changed')
            if archive.exists():
                _verify_worktree(repo, archive, old_commit)
                if active.exists():
                    _verify_worktree(repo, active, commit)
            else:
                _verify_worktree(repo, active, old_commit)
            changed = archive.exists() or _git(repo, 'rev-parse', 'HEAD') != old_commit or manifest_body != original_manifest.encode()
            result = {'changed': False, 'already_applied': False, 'rollback': True,
                      'generation': generation, 'commit': old_commit,
                      'new_profile_sha256': new['config_sha256']}
            if not apply or not changed:
                return result
            if archive.exists():
                if active.exists():
                    _git(repo, 'worktree', 'remove', str(active))
                _git(repo, 'worktree', 'move', str(archive), str(active))
                _sync(active.parent)
            if _git(repo, 'rev-parse', 'HEAD') != old_commit:
                _git(repo, 'checkout', '--detach', old_commit)
            _verify_worktree(repo, active, old_commit)
            if _protected(old) != journal['protected']:
                _invalid('protected_state_changed')
            _write_atomic(manifest_path, original_manifest.encode())
            # Retain the unused amended profile and owned transaction journal as
            # recovery evidence. They never replace the restored frozen profile.
            return {**result, 'changed': True}
        if is_completed:
            if journal is None or _read(profile_path, 1024 * 1024) != profile_text.encode():
                _invalid('unowned_completed_transaction')
            _verify_worktree(repo, archive, old_commit)
            _verify_worktree(repo, active, commit)
            if _git(repo, 'rev-parse', 'HEAD') != commit:
                _invalid('host_commit_changed')
            return {'changed': False, 'already_applied': True, 'generation': generation, 'new_profile_sha256': new['config_sha256'], 'commit': commit}
        before = _protected(old)
        _assert_idle(repo, old, effective_session)
        if _protected(old) != before or journal is not None and journal['protected'] != before:
            _invalid('protected_state_changed')
        if journal is None:
            if archive.exists() or archive.is_symlink() or profile_path.exists() or profile_path.is_symlink():
                _invalid('foreign_transaction_state')
            _verify_worktree(repo, active, old_commit)
        else:
            if archive.exists():
                _verify_worktree(repo, archive, old_commit)
                if active.exists():
                    _verify_worktree(repo, active, commit)
            else:
                _verify_worktree(repo, active, old_commit)
            if profile_path.exists() and _read(profile_path, 1024 * 1024) != profile_text.encode():
                _invalid('profile_changed')
        result = {'changed': False, 'already_applied': False, 'generation': generation, 'new_profile_sha256': new['config_sha256'], 'commit': commit}
        if not apply:
            return result
        if journal is None:
            journal = {'version': VERSION, 'parameters': parameters, 'manifest_before': original_manifest,
                       'manifest_after': manifest_text, 'profile_after': profile_text, 'protected': before}
            body = (json.dumps(journal, indent=2) + '\n').encode()
            if len(body) > MAX_JOURNAL:
                _invalid('journal_bounds')
            _write_atomic(journal_path, body, exclusive=True)
        if not profile_path.exists():
            _write_atomic(profile_path, profile_text.encode(), exclusive=True)
        if not archive.exists():
            _git(repo, 'worktree', 'move', str(active), str(archive))
            _sync(archive.parent)
        if not active.exists():
            _git(repo, 'worktree', 'add', '--detach', str(active), commit)
            _sync(active.parent)
        if _git(repo, 'rev-parse', 'HEAD') != commit:
            _git(repo, 'checkout', '--detach', commit)
        _verify_worktree(repo, archive, old_commit)
        _verify_worktree(repo, active, commit)
        if _protected(old) != before or _read(manifest_path, 2 * 1024 * 1024) != original_manifest.encode():
            _invalid('protected_state_changed')
        _write_atomic(manifest_path, manifest_text.encode())
        return {**result, 'changed': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--generation', required=True)
    parser.add_argument('--effective-session', required=True)
    parser.add_argument('--expected-old-commit', dest='old_commit', required=True)
    parser.add_argument('--expected-old-profile-sha256', dest='old_profile_sha256', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--expected-new-profile-sha256', dest='expected_profile_sha256')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--scheduler-quiesced', action='store_true')
    parser.add_argument('--rollback', action='store_true', help='Restore only this owned transaction before any amended session is accepted')
    args = parser.parse_args()
    try:
        result = retire_congress(**vars(args))
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, sqlite3.Error) as error:
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
