#!/usr/bin/env python3
"""Bounded local paper continuation using the ordinary generation lifecycle.

Prepare only in a separate local clone. Private paths and receipts stay in its
data/local-continuity directory. This command never deploys or trades live.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

WATCHDOG_SHA = '0359ae4e014751095effd524f48f20cff1b7f55354b7b3899659607374c40e68'
LIMITS = dict(wall_seconds=3500, rss_bytes=8 * 1024**3, processes=96,
              console_bytes=1024**2, poll_seconds=.05, cleanup_seconds=3.)
BOOKS = sorted(f'horizon_{h}_size_{s}' for h in ('30d', '3m', '6m', '1y')
               for s in ('5k', '10k', '50k', '100k'))
RUNTIME_ENV = ('PATH', 'HOME', 'TMPDIR', 'USER', 'LOGNAME', 'LANG', 'LC_ALL',
               'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE',
               'HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY')


def require(value, reason):
    if not value:
        raise ValueError(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024**2), b''):
            result.update(block)
    return result.hexdigest()


def save(path, value, *, replace=False):
    path = Path(path)
    output = path.with_suffix(path.suffix + '.tmp') if replace else path
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, sort_keys=True, indent=2, allow_nan=False)
        handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
    if replace:
        os.replace(output, path)


def save_status(folder, value):
    save(folder / 'status.json', value, replace=(folder / 'status.json').exists())
    latest = value.get('latest_session', {})
    lines = ['# Local paper operational acceptance', '',
             f"Consecutive qualifying sessions: {value.get('consecutive_qualifying_sessions', 0)}/5.",
             f"Local operational acceptance: {value.get('local_operational_acceptance', False)}.",
             f"Latest session: {latest.get('session', 'not yet attempted')}; status: {latest.get('status', value.get('status', 'pending'))}.",
             f"Automatic recovery observed: {value.get('automatic_recovery_observed', False)}.",
             f"Fill lifecycle observed: {value.get('fill_lifecycle_observed', False)}; carried positions observed: {value.get('carried_positions_observed', False)}.",
             '', 'Scope: isolated local PAPER, approved EDGAR research exclusion; no production deployment.',
             'Run receipts, accepted evidence and unchanged books remain under this clone. One attempt per actual eligible session; no same-day retry.',
             'See status.json for exact identities, individual checks and reasons.', '']
    path = folder / 'STATUS.md'; temporary = folder / 'STATUS.md.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write('\n'.join(lines))
    os.replace(temporary, path)


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], timeout=15, text=True).strip()


def host_identity():
    return dict(system=platform.system(), release=platform.release(), machine=platform.machine(),
                uid=os.getuid(), python=platform.python_version(), executable=str(Path(sys.executable).resolve()),
                dependencies=digest(sorted((d.metadata['Name'], d.version) for d in importlib.metadata.distributions())))


def research_window(now):
    from tradingagents.strategies.orchestration.decision_clock import latest_completed_session, require_reference
    from tradingagents.strategies.orchestration.trading_calendar import next_session, session_open, session_close
    session = latest_completed_session(now)
    result = dict(session=session.isoformat(), allowed=True,
                  next_open=session_open(next_session(session)).isoformat())
    try:
        require_reference(session, now)
    except ValueError:
        result.update(allowed=False, next_window_opens_at=session_close(next_session(session)).isoformat())
    return result


def claim_session(parent, session):
    path = Path(parent) / session
    path.mkdir(mode=0o700)  # O_EXCL equivalent: a failed attempt remains claimed.
    return path


def logical_database(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'sqlite_integrity')
        schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        tables = {}
        for kind, name, _, _ in schema:
            if kind != 'table':
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            rows = sorted(digest([(type(v).__name__, v.hex() if isinstance(v, bytes) else v) for v in row])
                          for row in db.execute('SELECT * FROM ' + quoted))
            tables[name] = dict(rows=len(rows), sha256=digest(rows))
        return dict(schema=digest(schema), tables=tables,
                    user_version=db.execute('PRAGMA user_version').fetchone()[0],
                    application_id=db.execute('PRAGMA application_id').fetchone()[0])


def is_database(path):
    with path.open('rb') as handle:
        return handle.read(16) == b'SQLite format 3\0'


def state_inventory(state):
    ledgers, sources, files = {}, {}, {}
    for path in sorted(Path(state).rglob('*')):
        require(not path.is_symlink(), 'state_symlink')
        if not path.is_file() or path.name.endswith(('-wal', '-shm', '-journal')):
            continue
        name = str(path.relative_to(state))
        if is_database(path):
            ledgers[name] = logical_database(path)
        else:
            files[name] = sha(path)
            if 'source_inputs' in path.relative_to(state).parts:
                sources[name] = files[name]
    return dict(ledger=digest(ledgers), source=digest(sources), files=digest(files), databases=ledgers)


def copy_state(source, target):
    """Copy a locked state, using SQLite backup so committed WAL rows survive."""
    target.mkdir(mode=0o700)
    for path in sorted(source.rglob('*')):
        require(not path.is_symlink(), 'state_symlink')
        destination = target / path.relative_to(source)
        if path.is_dir():
            destination.mkdir(exist_ok=True)
        elif not path.name.endswith(('-wal', '-shm', '-journal')):
            if is_database(path):
                with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as src:
                    with sqlite3.connect(destination) as dst:
                        src.backup(dst)
            else:
                shutil.copyfile(path, destination)


def native_inventory(repo):
    files = [repo / 'data/generations/manifest.json']
    files += sorted((repo / 'data/logs/run_attempts').glob('*.json'))
    for folder in ('cache', 'results'):
        files += sorted(path for path in (repo / folder).rglob('*') if path.is_file())
    require(not any(path.is_symlink() for path in files), 'native_artifact_symlink')
    return digest({str(path.relative_to(repo)): sha(path) for path in files if path.is_file()})


def load_contract(repo):
    folder = repo / 'data/local-continuity'
    contract = json.loads((folder / 'contract.json').read_text())
    require(contract['repo'] == str(repo) and contract['limits'] == LIMITS, 'local_contract_identity')
    require(contract['host'] == host_identity(), 'runtime_identity_changed')
    require(contract['environment_sha256'] == digest({key: os.environ[key] for key in RUNTIME_ENV if key in os.environ}),
            'runtime_environment_changed')
    require(git(repo, 'rev-parse', 'HEAD') == contract['commit'], 'candidate_head_changed')
    require(not git(repo, 'diff', 'HEAD', '--', 'tradingagents', 'scripts', 'configs'), 'candidate_bytes_changed')
    extras = git(repo, 'ls-files', '--others', '--exclude-standard', '--', 'tradingagents', 'scripts', 'configs')
    require(not extras, 'untracked_application_code')
    require(all(sha(Path(name)) == expected for name, expected in contract['inputs'].items()), 'frozen_input_changed')
    return folder, contract


def generation(repo, contract):
    manifest = json.loads((repo / 'data/generations/manifest.json').read_text())
    require(len(manifest['generations']) == 1, 'one_local_generation_required')
    row = manifest['generations'][0]
    require(row['gen_id'] == contract['generation_id'] and row['git_commit'] == contract['commit']
            and row['config_sha256'] == contract['profile_sha256'] and row['status'] == 'active', 'generation_identity')
    require(Path(row['state_dir']).resolve() == repo / 'data/generations' / row['gen_id'], 'isolated_state_required')
    require(git(Path(row['worktree_path']), 'rev-parse', 'HEAD') == contract['commit']
            and not git(Path(row['worktree_path']), 'diff', 'HEAD'), 'generation_code_changed')
    require(not git(Path(row['worktree_path']), 'ls-files', '--others', '--exclude-standard', '--',
                    'tradingagents', 'scripts', 'configs'), 'untracked_generation_code')
    return row


def prepare(repo, profile, watchdog, dotenv):
    from tradingagents.strategies.orchestration.generation_config import read_generation_config
    from tradingagents.strategies.orchestration.generation_manager import GenerationManager
    require(git(repo, 'rev-parse', '--show-toplevel') == str(repo), 'standalone_clone_required')
    require((repo / '.git').is_dir() and Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
            == repo / '.git', 'standalone_clone_required')
    require(not (repo / 'data/generations/manifest.json').exists(), 'fresh_local_manifest_required')
    require(sha(watchdog) == WATCHDOG_SHA and dotenv.is_file(), 'existing_reviewed_inputs_required')
    _, overlay, profile_sha = read_generation_config(profile)
    ar = overlay.get('autoresearch', {})
    require(ar.get('decision_clock_policy') == 'prospective_next_open_v1'
            and ar.get('filing_research_policy') == 'disabled_v1'
            and ar.get('research_resilience_policy') == 'dependency_isolation_v1'
            and ar.get('llm_billing_mode') == 'chatgpt_plan'
            and ar.get('autoresearch_model') == 'gpt-6-luna' and ar.get('llm_effort') == 'high'
            and ar.get('thesis_model') == 'gpt-6.1-sol' and ar.get('thesis_effort') == 'high'
            and ar.get('paper_trade', {}).get('portfolio_committee_model') == 'gpt-6.1-sol'
            and ar.get('paper_trade', {}).get('portfolio_committee_effort') == 'high', 'approved_profile_required')
    require(overlay.get('execution') == {'mode': 'paper', 'execution_enabled': False}, 'paper_only')
    folder = repo / 'data/local-continuity'; folder.mkdir(mode=0o700, parents=True)
    (folder / 'sessions').mkdir(mode=0o700)
    shutil.copyfile(watchdog, folder / 'mac_supervisor.py')
    info = GenerationManager(str(repo)).start_generation('Persistent isolated local operational acceptance', config_file=profile)
    inputs = {str(path): sha(path) for path in (dotenv, Path(info.config_file), folder / 'mac_supervisor.py',
                                               repo / 'scripts/run_local_continuity.py')}
    contract = dict(version='local-continuity-runtime-v1', repo=str(repo), commit=info.git_commit,
                    prepared_at=datetime.now(timezone.utc).isoformat(),
                    generation_id=info.gen_id, profile_sha256=profile_sha, host=host_identity(),
                    environment_sha256=digest({key: os.environ[key] for key in RUNTIME_ENV if key in os.environ}),
                    dotenv=str(dotenv), limits=LIMITS, inputs=inputs,
                    storage=dict(private_capture_bytes=8*1024**3, minimum_free_disk_bytes=24*1024**3))
    save(folder / 'contract.json', contract)
    save_status(folder, dict(status='waiting_for_first_eligible_session', consecutive_qualifying_sessions=0,
                                    local_operational_acceptance=False, deployed=False, **research_window(datetime.now(timezone.utc))))
    return contract


def worker_environment(repo, contract):
    # Credentials come only from the frozen private dotenv and native auth file.
    # Ambient trading/model/blocked-ticker overrides cannot alter this candidate.
    env = {key: os.environ[key] for key in RUNTIME_ENV if key in os.environ}
    env.update(PYTHONPATH=str(repo), PYTHONDONTWRITEBYTECODE='1', XDG_CACHE_HOME=str(repo / 'cache'),
               EVENTEDGE_SOURCE_CACHE_DIR=str(repo / 'cache/sources'), AUTORESEARCH_FETCH_TIMEOUT_S='900')
    return env


def supervise(repo, contract, session_dir, operation):
    folder = repo / 'data/local-continuity'
    spec = importlib.util.spec_from_file_location('continuity_watchdog', folder / 'mac_supervisor.py')
    watchdog = importlib.util.module_from_spec(spec); spec.loader.exec_module(watchdog)
    limits = dict(LIMITS, wall_seconds=120) if operation == '_replay' else dict(LIMITS)
    return watchdog.supervise([str(repo / '.venv/bin/python'), '-B', str(repo / 'scripts/run_local_continuity.py'),
                               operation, '--repo', str(repo), '--session', session_dir.name],
                              cwd=repo, env=worker_environment(repo, contract), limits=limits,
                              stdout_path=session_dir / (operation + '-stdout.txt'),
                              stderr_path=session_dir / (operation + '-stderr.txt'))


class ForbiddenCall(BaseException):
    pass


def replay_guards(attempts):
    def forbidden(name):
        def stop(*args, **kwargs):
            attempts[name] += 1
            raise ForbiddenCall(name)
        return stop
    for name in ('getaddrinfo', 'gethostbyname', 'create_connection'):
        setattr(socket, name, forbidden('network.' + name))
    for name in ('connect', 'connect_ex'):
        setattr(socket.socket, name, forbidden('network.socket.' + name))
    import requests, httpx, urllib.request
    requests.Session.request = forbidden('requests')
    httpx.Client.send = forbidden('httpx')
    httpx.AsyncClient.send = forbidden('httpx.async')
    urllib.request.urlopen = forbidden('urllib')
    try:
        import curl_cffi
        curl_cffi.Curl.perform = forbidden('curl')
    except ImportError:
        pass
    from tradingagents.strategies import llm_utils, runtime_deadline
    llm_utils.call_analysis_model = forbidden('model.analysis')
    runtime_deadline.bounded_transport = forbidden('model.transport')
    return forbidden


def native_worker(repo, contract, session):
    from dotenv import load_dotenv
    load_dotenv(contract['dotenv'], override=True)
    os.environ.pop('BLOCKED_TICKERS', None)
    os.environ['AUTORESEARCH_FETCH_TIMEOUT_S'] = '900'
    from tradingagents.strategies.orchestration.generation_manager import GenerationManager
    from tradingagents.strategies.orchestration.multi_strategy_engine import _fetch_timeout_s
    from tradingagents.strategies.runtime_deadline import DEFAULT_MODEL_BUDGET_S, MODEL_CALL_CAP_S
    require((DEFAULT_MODEL_BUDGET_S, MODEL_CALL_CAP_S, _fetch_timeout_s()) == (2400, 120, 900), 'budget_changed')
    window = research_window(datetime.now(timezone.utc))
    require(window['allowed'] and window['session'] == session, 'real_window_required')
    result = GenerationManager(str(repo)).run_daily(session)
    save(repo / 'data/local-continuity/sessions' / session / 'manager-result.json', result)


def replay_worker(repo, contract, session):
    from dotenv import load_dotenv
    load_dotenv(contract['dotenv'], override=True)
    os.environ.pop('BLOCKED_TICKERS', None)
    os.environ['AUTORESEARCH_FETCH_TIMEOUT_S'] = '900'
    row = generation(repo, contract)
    sys.path.insert(0, row['worktree_path'])
    from scripts.run_cohorts import _build_config, _build_parser
    from tradingagents.strategies.orchestration.generation_config import CONFIG_FILE_ENV, CONFIG_SHA_ENV
    from tradingagents.strategies.orchestration.cohort_orchestrator import CohortOrchestrator, build_default_cohorts
    from tradingagents.strategies.runtime_deadline import model_budget
    os.environ.update({CONFIG_FILE_ENV: row['config_file'], CONFIG_SHA_ENV: row['config_sha256'],
                       'AUTORESEARCH_STATE_DIR': str(repo / 'data/local-continuity/sessions' / session / 'replay-state')})
    config = _build_config(_build_parser().parse_args([]))
    attempts = Counter(); forbidden = replay_guards(attempts)
    owner = None
    try:
        owner = CohortOrchestrator(build_default_cohorts(config), config,
                                  generation_id=row['gen_id'], generation_commit=row['git_commit'])
        for cohort in owner.cohorts:
            for source in cohort['engine'].registry._sources.values():
                for name in dir(type(source)):
                    if name == 'fetch' or name.startswith(('fetch_', 'get_', 'search_')):
                        if callable(getattr(source, name, None)):
                            setattr(source, name, forbidden('provider.' + source.name + '.' + name))
        for name in dir(type(owner._price_source)):
            if name.startswith(('fetch', 'get_', 'resolve_')) and callable(getattr(owner._price_source, name, None)):
                setattr(owner._price_source, name, forbidden('price.' + name))
        subprocess.Popen = forbidden('process')
        with model_budget(time.monotonic() + 110):
            result = owner.run_daily(session)
        require(sorted(result) == BOOKS and all(value.get('replayed') is True for value in result.values()), 'completed_replay_required')
        require(not attempts, 'replay_call_attempted')
        save(repo / 'data/local-continuity/sessions' / session / 'replay-result.json',
             dict(zero_calls=True, attempted_calls=dict(attempts), replayed_cohorts=sorted(result)))
    finally:
        if owner is not None:
            for cohort in owner.cohorts:
                cohort['ledger'].close()


def continue_session(repo):
    from tradingagents.strategies.orchestration.runtime_lock import canonical_runtime_lock_path, runtime_lock
    from tradingagents.strategies.orchestration.operational_report import build_operational_report
    from tradingagents.strategies.orchestration.local_continuity import classify_session, summarize_continuity
    from tradingagents.strategies.orchestration.local_continuity_evidence import collect_session
    folder, contract = load_contract(repo); row = generation(repo, contract)
    window = research_window(datetime.now(timezone.utc))
    if not window['allowed']:
        return dict(status='waiting_for_eligible_window', **window)
    session = window['session']
    if (folder / 'sessions' / session).exists():
        return dict(status='session_already_claimed_no_retry', session=session)
    require(shutil.disk_usage(repo).free >= contract['storage']['minimum_free_disk_bytes'], 'insufficient_free_disk')
    used = sum(path.stat().st_size for folder in ('data', 'cache', 'results')
               for path in (repo / folder).rglob('*') if path.is_file() and not path.is_symlink())
    require(used < contract['storage']['private_capture_bytes'], 'private_storage_cap')
    # Separate nonblocking coordinator lock covers the whole live/copy/replay/report lifecycle.
    with runtime_lock(folder / 'continuation.lock', exclusive=True):
        session_dir = claim_session(folder / 'sessions', session)
        save(session_dir / 'started.json', dict(started_at=datetime.now(timezone.utc).isoformat(), **window))
        dotenv_before = sha(Path(contract['dotenv']))
        native = supervise(repo, contract, session_dir, '_daily')
        save(session_dir / 'native-supervision.json', native)
        report = build_operational_report(repo, row['gen_id'], session)
        save(session_dir / 'operational-report.json', report)
        identity = dict(session=session, generation_id=row['gen_id'], generation_commit=row['git_commit'],
                        profile_sha256=row['config_sha256'], epoch_id=report.get('epoch_id'))
        replay_receipt = None; replay = None
        if native['status'] == 'completed' and not native['remaining_pids'] and report.get('accounting_valid'):
            with runtime_lock(canonical_runtime_lock_path(repo), exclusive=False):
                before = state_inventory(Path(row['state_dir'])); native_before = native_inventory(repo)
                copy_state(Path(row['state_dir']), session_dir / 'replay-state')
                copy_before = state_inventory(session_dir / 'replay-state')
                require(before == copy_before, 'replay_copy_mismatch')
            replay = supervise(repo, contract, session_dir, '_replay')
            save(session_dir / 'replay-supervision.json', replay)
            with runtime_lock(canonical_runtime_lock_path(repo), exclusive=False):
                after = state_inventory(Path(row['state_dir'])); native_after = native_inventory(repo)
                copy_after = state_inventory(session_dir / 'replay-state')
            if replay['status'] == 'completed' and not replay['remaining_pids']:
                actual = json.loads((session_dir / 'replay-result.json').read_text())
                require(before == after and copy_before == copy_after, 'immutable_replay_changed_state')
                replay_receipt = dict(version='local-continuity-replay-v1', **identity,
                    zero_calls=actual['zero_calls'], replayed_cohorts=actual['replayed_cohorts'],
                    native_before=native_before, native_after=native_after,
                    source_before=before['source'], source_after=after['source'],
                    ledger_before=before['ledger'], ledger_after=copy_after['ledger'])
                save(session_dir / 'replay-anchor.json', replay_receipt)
                replay_receipt['anchor_sha256'] = sha(session_dir / 'replay-anchor.json')
                save(session_dir / 'replay-receipt.json', replay_receipt)
        # Supported native auth refresh is permitted; dotenv and candidate/profile bytes must stay fixed.
        load_contract(repo)
        cleanup = dict(version='local-continuity-cleanup-v1', **identity,
                       owned_workers_reaped=not native['remaining_pids'] and (replay is None or not replay['remaining_pids']),
                       inputs_unchanged=sha(Path(contract['dotenv'])) == dotenv_before)
        save(session_dir / 'cleanup-receipt.json', cleanup)
        observation = collect_session(repo, row['gen_id'], session, expected_commit=contract['commit'],
                                      expected_profile=contract['profile_sha256'], replay_receipt=replay_receipt,
                                      cleanup_receipt=cleanup)
        classified = classify_session(observation, expected_commit=contract['commit'], expected_profile=contract['profile_sha256'])
        save(session_dir / 'observation.json', classified)
        records = [json.loads(path.read_text()) for path in sorted((folder / 'sessions').glob('*/observation.json'))]
        from datetime import date
        status = summarize_continuity(records, through=date.fromisoformat(session))
        status.update(latest_session=classified, generation_id=row['gen_id'], generation_commit=contract['commit'],
                      profile_sha256=contract['profile_sha256'], deployed=False, full_research_coverage=False)
        save_status(folder, status)
        return status


def health(repo, *, now=None):
    """Read-only scheduler/freshness projection; never retries a claimed day."""
    from datetime import timedelta
    from datetime import date
    now = now or datetime.now(timezone.utc)
    folder, contract = load_contract(repo)
    generation(repo, contract)
    status = json.loads((folder / 'status.json').read_text())
    window = research_window(now)
    issues = []
    from zoneinfo import ZoneInfo
    first_date = datetime.fromisoformat(contract['prepared_at']).astimezone(ZoneInfo('America/New_York')).date()
    if date.fromisoformat(window['session']) >= first_date:
        current = folder / 'sessions' / window['session']
        # Schedule is 16:05 Eastern; early-close days still use that planned time.
        planned = datetime.combine(date.fromisoformat(window['session']), datetime.min.time(), ZoneInfo('America/New_York')).replace(hour=16, minute=5)
        if not current.exists() and now >= planned + timedelta(minutes=10):
            issues.append('scheduled_session_missing')
        elif current.exists() and not (current / 'observation.json').is_file():
            started_path = current / 'started.json'
            started = json.loads(started_path.read_text()) if started_path.is_file() else None
            if started is None:
                issues.append('session_claim_incomplete')
            elif now >= datetime.fromisoformat(started['started_at']) + timedelta(seconds=3500+120+60):
                issues.append('session_incomplete_after_bounds')
        elif current.exists():
            observation = json.loads((current / 'observation.json').read_text())
            if observation.get('qualifies') is not True:
                issues.append('session_did_not_qualify')
    if status.get('status') == 'continuation_failed_no_retry':
        issues.append('continuation_failed_no_retry')
    return dict(scope='local_only', observed_at=now.isoformat(), window=window,
                issues=issues, healthy=not issues, status=status, deployed=False)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'continue', 'status', 'health', '_daily', '_replay'))
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--profile', type=Path); parser.add_argument('--watchdog', type=Path)
    parser.add_argument('--dotenv', type=Path); parser.add_argument('--session')
    args = parser.parse_args(); repo = args.repo.resolve(); sys.path.insert(0, str(repo))
    if args.operation == 'prepare':
        result = prepare(repo, args.profile.resolve(), args.watchdog.resolve(), args.dotenv.resolve())
    elif args.operation in ('_daily', '_replay'):
        _, contract = load_contract(repo)
        require((repo / 'data/local-continuity/sessions' / args.session / 'started.json').is_file(), 'claimed_session_required')
        (native_worker if args.operation == '_daily' else replay_worker)(repo, contract, args.session)
        return
    elif args.operation == 'continue':
        try:
            result = continue_session(repo)
        except Exception as error:
            # A partial/terminated attempt remains claimed; never repair or retry automatically.
            result = dict(status='continuation_failed_no_retry', error_type=type(error).__name__,
                          observed_at=datetime.now(timezone.utc).isoformat(), local_operational_acceptance=False,
                          deployed=False)
            folder = repo / 'data/local-continuity'
            if (folder / 'contract.json').is_file():
                save_status(folder, result)
            print(json.dumps(result, sort_keys=True))
            raise SystemExit(1) from None
    elif args.operation == 'health':
        result = health(repo)
    else:
        load_contract(repo)
        result = json.loads((repo / 'data/local-continuity/status.json').read_text())
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
