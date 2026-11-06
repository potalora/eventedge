#!/usr/bin/env python3
"""Existing timer entrypoint: one normal daily sample, bounded local recovery.

No install/activation side effects. Status is an ordinary application-owned JSON
heartbeat for the existing Hermes monitor. --worker is an internal pinned-worktree
mode; it never calls providers or models.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradingagents.strategies.orchestration.runtime_recovery import (
    VERSION, MAX_ATTEMPTS, MAX_INCIDENTS, RECOVERY_TIMEOUT_SECONDS,
    DAILY_TIMEOUT_SECONDS, atomic_json, bounded_process, finish_attempt,
    identity_for, incident_key, load_journal, new_incident, recover_owner,
    status_projection, _result,
    PROSPECTIVE_LAUNCH_MODE, validate_prospective_intent,
)
from tradingagents.strategies.orchestration.runtime_lock import (
    RuntimeLockBusy, RuntimeLockInvalid, canonical_runtime_lock_path, runtime_lock,
)
from tradingagents.strategies.orchestration.trading_calendar import (
    exchange_date, is_session, session_close,
)


def manifest_rows(repo, *, all_rows=False):
    path = repo / 'data/generations/manifest.json'
    if path.stat().st_size > 1048576:
        raise ValueError('identity_mismatch')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('identity_mismatch')
            value[key] = item
        return value
    manifest = json.loads(path.read_text(), object_pairs_hook=unique)
    rows = manifest['generations']
    if type(rows) is not list or len(rows) > MAX_INCIDENTS or len({row['gen_id'] for row in rows}) != len(rows):
        raise ValueError('identity_mismatch')
    return rows if all_rows else [row for row in rows if row['status'] == 'active']


def _discover_sessions(row, now):
    """Bounded read-only discovery of persisted, unstaged execution after reboot."""
    sessions = set()
    state = Path(row['state_dir']).resolve()
    oldest = (exchange_date(now) - timedelta(days=10)).isoformat()
    for path in sorted(state.glob('horizon_*/portfolio.db'))[:16]:
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as connection:
            values = connection.execute('SELECT DISTINCT c.session FROM session_execution_contexts c '
                'LEFT JOIN staging_runs s ON s.session=c.session AND s.cohort_id=c.cohort_id '
                'WHERE c.session>=? AND s.session IS NULL LIMIT 10', (oldest,)).fetchall()
            sessions.update(day for (day,) in values)
    return sorted(sessions)


def launch(repo, row, session, action, *, heartbeat=None, expected_roster=None):
    env = os.environ.copy()
    if action == 'daily':
        with runtime_lock(canonical_runtime_lock_path(repo), exclusive=True) as lock:
            current = manifest_rows(repo)
            expected = expected_roster or [row]
            if sorted(identity_for(repo,item,session)['generation_id']+incident_key(identity_for(repo,item,session)) for item in current) != sorted(identity_for(repo,item,session)['generation_id']+incident_key(identity_for(repo,item,session)) for item in expected):
                raise ValueError('identity_mismatch')
            return _daily_process(repo, lock, heartbeat=heartbeat)
    env.update(AUTORESEARCH_STATE_DIR=str(Path(row['state_dir']).resolve()),
        EVENTEDGE_GENERATION_ID=row['gen_id'], EVENTEDGE_GENERATION_COMMIT=row['git_commit'],
        EVENTEDGE_RECOVERY_REPO=str(repo), EVENTEDGE_GENERATION_CONFIG_FILE=row['config_file'],
        EVENTEDGE_GENERATION_CONFIG_SHA256=row['config_sha256'])
    from tradingagents.strategies.orchestration.generation_config import CONFIG_FILE_ENV, CONFIG_SHA_ENV
    env[CONFIG_FILE_ENV], env[CONFIG_SHA_ENV] = row['config_file'], row['config_sha256']
    env['PYTHONPATH'] = str(Path(row['worktree_path']).resolve())
    python = os.environ.get('EVENTEDGE_PYTHON') or str(repo/'.venv/bin/python')
    return bounded_process([python, 'scripts/recover_runtime.py', '--worker', '--date', session,
                            '--repo-root', str(repo)], cwd=Path(row['worktree_path']), env=env,
                            timeout=RECOVERY_TIMEOUT_SECONDS, heartbeat=heartbeat)


def _daily_process(repo, lock, *, heartbeat=None, prospective_args=()):
    env = os.environ.copy()
    env['EVENTEDGE_RUNTIME_LOCK_FD'] = str(lock.fd)
    env['EVENTEDGE_RUNTIME_LOCK_MODE'] = 'exclusive'
    return bounded_process(['bash', str(repo/'scripts/daily_trading.sh'), *prospective_args], cwd=repo,
        env=env, timeout=DAILY_TIMEOUT_SECONDS, heartbeat=heartbeat, pass_fds=(lock.fd,))


def _root_code_identity(repo, expected_commit):
    """The actual controller/wrapper run from the root checkout, not its worktree."""
    head = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
        check=True, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(['git', '-C', str(repo), 'diff', '--quiet', 'HEAD'],
        capture_output=True).returncode
    if head != expected_commit or dirty != 0:
        raise ValueError('prospective_root_code_mismatch')


def _prospective_row(repo, session, now, *, expected_generation, expected_commit,
                     expected_profile, expected_created, fresh):
    from tradingagents.strategies.orchestration.decision_clock import require_reference, policy, PROSPECTIVE
    from tradingagents.strategies.orchestration.generation_config import read_generation_config, FROZEN_CONFIG_NAME
    if (type(session) is not str or date.fromisoformat(session).isoformat() != session
            or now.tzinfo is None or now.utcoffset() is None):
        raise ValueError('prospective_reference_invalid')
    reference = date.fromisoformat(session)
    if not is_session(reference):
        raise ValueError('prospective_reference_invalid')
    if fresh:
        require_reference(reference, now)
    rows = manifest_rows(repo, all_rows=True)
    if any(not re.fullmatch(r'gen_[0-9]{3,}', row['gen_id']) for row in rows):
        raise ValueError('identity_mismatch')
    active = [row for row in rows if row['status'] == 'active']
    if (len(active) != 1 or active[0]['gen_id'] != expected_generation
            or max(int(row['gen_id'][4:]) for row in rows) != int(expected_generation[4:])):
        raise ValueError('identity_mismatch')
    row = active[0]
    if (row['git_commit'] != expected_commit or row.get('config_sha256') != expected_profile
            or row.get('created_at') != expected_created or type(expected_created) is not str
            or len(expected_created) > 64 or 'T' not in expected_created):
        raise ValueError('identity_mismatch')
    datetime.fromisoformat(expected_created)  # Exact identity; native creation may be naive.
    _root_code_identity(repo, expected_commit)
    identity = identity_for(repo, row, session)
    _, config, _ = read_generation_config(row['config_file'], expected_profile)
    ar, execution = config.get('autoresearch', {}), config.get('execution', {})
    if (policy(config) != PROSPECTIVE or execution.get('mode') != 'paper'
            or execution.get('execution_enabled') is not False
            or ((ar.get('bounded_document_decision_policy') is not None or ar.get('bounded_document_registry'))
                and ar.get('bounded_document_decision_policy') != 'verified_scoped_claims_next_open_v2')):
        raise ValueError('prospective_profile_required')
    pending = repo/'data/generations/.pending-start.json'
    if pending.exists() or pending.is_symlink():
        raise ValueError('pending_lifecycle')
    if fresh:
        state = Path(row['state_dir'])
        entries = list(state.iterdir())
        if (row.get('run_history') != [] or len(entries) != 1 or entries[0].name != FROZEN_CONFIG_NAME
                or entries[0].is_symlink() or not entries[0].is_file()):
            raise ValueError('prospective_generation_not_fresh')
    return row, identity


def validate_prospective_launch(repo, session, *, now=None, environ=None, **expected):
    """Pure wrapper gate: no books/providers; require the parent's existing exclusive lease."""
    repo = Path(repo).resolve()
    environ = os.environ if environ is None else environ
    raw_fd = environ.get('EVENTEDGE_RUNTIME_LOCK_FD', '')
    if (environ.get('EVENTEDGE_RUNTIME_LOCK_MODE') != 'exclusive' or type(raw_fd) is not str
            or re.fullmatch(r'[0-9]{1,10}', raw_fd) is None or int(raw_fd) <= 2):
        raise RuntimeLockInvalid('prospective launch requires an inherited exclusive lease')
    path = canonical_runtime_lock_path(repo)
    # runtime_lock validates descriptor/path ownership. First prove an exclusive
    # lock already exists, rather than acquiring an unlocked caller-supplied FD.
    probe = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(probe, fcntl.LOCK_UN)
            raise RuntimeLockInvalid('prospective inherited lease is not already held')
    finally:
        os.close(probe)
    with runtime_lock(path, exclusive=True, inherited_fd=int(raw_fd), inherited_exclusive=True):
        now = datetime.now(timezone.utc) if now is None else now
        row, identity = _prospective_row(repo, session, now, fresh=True, **expected)
        journal = load_journal(repo/'data/runtime_recovery/journal.json')
        records = [record for record in journal['incidents'].values()
                   if record['identity']['generation_id'] == row['gen_id']]
        if len(records) != 1:
            raise ValueError('prospective_launch_intent_missing')
        record = records[0]
        validate_prospective_intent(record)
        if (record['identity'] != identity or record['running'] is not True
                or record['disposition'] != 'retrying' or record['reason_code'] != 'interrupted'
                or record['launch_intent']['generation_created_at'] != expected['expected_created']
                or datetime.fromisoformat(record['launch_intent']['started_at']) > now):
            raise ValueError('prospective_launch_intent_mismatch')
    return identity


def prospective_once(repo, session, *, clock=None, reporter=None, **expected):
    """Explicit fresh forward-prospective launch; no recovery, backfill or retry."""
    repo = Path(repo).resolve()
    clock = clock or (lambda: datetime.now(timezone.utc))
    reporter = reporter or _report_only
    directory = repo/'data/runtime_recovery'
    journal_path, status_path = directory/'journal.json', directory/'status.json'
    with runtime_lock(directory/'controller.lock', exclusive=True):
        with runtime_lock(canonical_runtime_lock_path(repo), exclusive=True) as lease:
            row, identity = _prospective_row(repo, session, clock(), fresh=True, **expected)
            journal = load_journal(journal_path)
            if (len(journal['incidents']) >= MAX_INCIDENTS or any(record['identity']['generation_id'] == row['gen_id']
                    for record in journal['incidents'].values())):
                raise ValueError('prospective_launch_already_recorded')
            now = clock()
            # A slow identity read cannot admit a start after the next open.
            row, identity = _prospective_row(repo, session, now, fresh=True, **expected)
            record = new_incident(identity, now)
            record.update(daily_started=True, running=True, launch_mode=PROSPECTIVE_LAUNCH_MODE,
                launch_intent={'version': 'prospective-native-launch-v1',
                    'generation_created_at': expected['expected_created'], 'started_at': now.isoformat()})
            validate_prospective_intent(record)
            journal['incidents'][incident_key(identity)] = record
            atomic_json(journal_path, journal)
            atomic_json(status_path, status_projection(journal, now, active_ids=[row['gen_id']]))
            def heartbeat():
                atomic_json(status_path, status_projection(journal, clock(), active_ids=[row['gen_id']]))
            argv = ['--prospective-session', session, '--expected-generation', row['gen_id'],
                '--expected-commit', row['git_commit'], '--expected-profile-sha256', row['config_sha256'],
                '--expected-created-at', expected['expected_created']]
            try:
                code, _ = _daily_process(repo, lease, heartbeat=heartbeat, prospective_args=argv)
            except Exception:
                code = None  # The durable intent remains; outcome cannot authorize another launch.
        try:
            current, actual = _prospective_row(repo, session, clock(), fresh=False, **expected)
            if actual != identity:
                raise ValueError('identity_mismatch')
            result = _terminal_report(reporter(repo, current, session)) if code is not None else None
            result = result or _result('requires_user', 'worker_timeout' if code == 124 else 'daily_failed')
        except Exception:
            result = _result('requires_user', 'checkpoint_invalid')
        finish_attempt(record, result, clock())
        atomic_json(journal_path, journal)
        projection = status_projection(journal, clock(), active_ids=[row['gen_id']])
        projection['controller_reason_code'] = None
        atomic_json(status_path, projection)
        return projection


def _worker_result(code, output):
    if code == 124:
        return _result('retrying', 'worker_timeout')
    lines = [line[len('RECOVERY_RESULT='):] for line in output.splitlines() if line.startswith('RECOVERY_RESULT=')]
    if code != 0 or len(lines) != 1:
        return _result('requires_user', 'worker_failed')
    result = json.loads(lines[0])
    if set(result) != {'version', 'disposition', 'reason_code'} or result['version'] != VERSION:
        return _result('requires_user', 'worker_failed')
    return _result(result['disposition'], result['reason_code'])


def _report(repo, generation, session):
    from tradingagents.strategies.orchestration.operational_report import build_operational_report
    return build_operational_report(repo, generation, session)


def _report_only(repo, row, session):
    """Render truthful existing evidence; never invent a daily wire/receipt."""
    from tradingagents.strategies.orchestration.operational_report import write_operational_report
    report = _report(repo, row['gen_id'], session)
    write_operational_report(report, repo/'docs/reports')
    return report


def _terminal_report(report):
    # An explicit failed daily attempt cannot become an accepted sample merely
    # because source denials coexist with valid accounting. Missing model or
    # staging work must never be hidden behind accepted_outcome.
    if report.get('outcome') == 'failed':
        return _result('requires_user', 'checkpoint_invalid')
    if any(row.get('status') == 'strategy_defect' for row in report.get('source_health_failures', [])):
        return _result('requires_user', 'checkpoint_invalid')
    if 'completion' in report:
        completion = report['completion']
        if (not isinstance(completion, dict)
                or completion.get('version') != 'supported_sixteen_books_v1'):
            return _result('requires_user', 'checkpoint_invalid')
        if (completion.get('workflow_complete') is True and report.get('evidence_complete') is True
                and report.get('accounting_valid') is True and report.get('staging_valid') is True):
            return _result('recovered', 'complete')
        if report.get('staging_complete') and report.get('accounting_valid'):
            expected_holds = {'research_held', 'screening_model_incomplete', 'committee_incomplete',
                'required_source_failed', 'supported_health_invalid'}
            reasons = completion.get('reason_codes')
            if (report.get('evidence_complete') is True and report.get('staging_valid') is True
                    and isinstance(reasons, list) and reasons and all(type(reason) is str for reason in reasons)
                    and set(reasons) <= expected_holds):
                # The accepted evidence proves a held/missed sample. Retain it
                # without claiming workflow completion or freezing future days.
                return _result('wait_next_session', 'accepted_outcome')
            return _result('requires_user', 'checkpoint_invalid')
        # An interrupted economic/staging phase still reaches the existing
        # durable recovery path. Completion never authorizes missing model work.
        return None
    # New complete accepted evidence can prove recovery after an earlier
    # preflight source-access incident; keep that incident in the report, not as a sticky
    # blocker that contradicts the newer verified sample.
    if (report.get('accounting_valid') and report.get('staging_complete')
            and report.get('evidence_complete')
            and not report.get('sources', {}).get('unresolved')
            and not report.get('source_health_failures')
            and not report.get('candidate_input_issues')):
        return _result('recovered', 'complete')
    # Source HTTP denials, including historical preflights, cannot establish
    # model credential loss. Explicit worker auth results retain their contract;
    # accepted degraded samples follow the ordinary source-coverage policy.
    if report.get('staging_complete') and report.get('accounting_valid'):
        if report.get('sources', {}).get('unresolved') or report.get('source_health_failures') or report.get('candidate_input_issues'):
            return _result('wait_next_session', 'accepted_outcome')
        if report.get('evidence_complete'):
            return _result('recovered', 'complete')
    return None


def tick(repo, now, *, launcher=launch, reporter=_report_only, discover=_discover_sessions, clock=None):
    """One bounded scheduler pass; fake clocks/transports support fault acceptance."""
    repo = Path(repo).resolve()
    now = now.astimezone(timezone.utc)
    clock = clock or (lambda: datetime.now(timezone.utc))
    directory = repo/'data/runtime_recovery'
    journal_path, status_path = directory/'journal.json', directory/'status.json'
    directory.mkdir(parents=True, exist_ok=True)
    # Serializes scheduler instances separately from worker accounting. The
    # worker owns the canonical runtime lock; ordinary daily retains its locks.
    with runtime_lock(directory/'controller.lock', exclusive=True):
        try:
            journal = load_journal(journal_path)
            rows = manifest_rows(repo)
            by_id = {row['gen_id']: row for row in rows}
            today = exchange_date(now)
            for row in rows:
                identity_for(repo, row, today.isoformat())
            # 18:00 New York is the retained daily schedule (DST aware).
            from zoneinfo import ZoneInfo
            due = is_session(today) and now >= session_close(today) and now.astimezone(ZoneInfo('America/New_York')).hour >= 18
            def heartbeat():
                fresh = clock()
                atomic_json(status_path, status_projection(journal, fresh, active_ids=by_id))
            daily_records = []
            for row in rows:
                persisted_sessions = set(discover(row, now))
                sessions = list(persisted_sessions)
                # Record completely missed eligible days without creating a
                # historical sample. Discovery is bounded to ten calendar days
                # and to this generation's documented creation date.
                created = row.get('created_at')
                if created:
                    first = max(date.fromisoformat(created[:10]), today - timedelta(days=10))
                    for offset in range((today - first).days):
                        day = first + timedelta(days=offset)
                        if is_session(day):
                            sessions.append(day.isoformat())
                if due:
                    sessions.append(today.isoformat())
                for session in sorted(set(sessions)):
                    identity = identity_for(repo, row, session)
                    key = incident_key(identity)
                    if key not in journal['incidents']:
                        # Discard old resolved history only. Pending/operator
                        # incidents never disappear merely because time passes.
                        if len(journal['incidents']) >= MAX_INCIDENTS:
                            resolved = sorted((k for k, v in journal['incidents'].items()
                                if v['disposition'] in {'recovered', 'wait_next_session'}),
                                key=lambda k: journal['incidents'][k]['updated_at'])
                            if not resolved:
                                raise ValueError('state_corrupt')
                            del journal['incidents'][resolved[0]]
                        record = new_incident(identity, now)
                        if session in persisted_sessions:
                            record['daily_started'] = True
                        elif session < today.isoformat():
                            record['daily_started'] = True
                            report = reporter(repo, row, session)
                            finish_attempt(record, _terminal_report(report) or _result('wait_next_session', 'missed_session'), now)
                        journal['incidents'][key] = record
                    record = journal['incidents'][key]
                    if session == today.isoformat() and due and not record['daily_started']:
                        daily_records.append(record)
            # One recovery worker per invocation bounds the service wall time.
            for key, record in sorted(journal['incidents'].items(), key=lambda item: item[1]['identity']['session']):
                if record['disposition'] != 'retrying':
                    continue
                if not record['daily_started']:
                    continue
                if record.get('launch_mode') == PROSPECTIVE_LAUNCH_MODE:
                    # A one-shot is never resumed or re-inferred by the normal timer.
                    finish_attempt(record, _result('requires_user', 'interrupted'), now)
                    continue
                identity = record['identity']
                row = by_id.get(identity['generation_id'])
                if row is None or identity_for(repo, row, identity['session']) != identity:
                    finish_attempt(record, _result('requires_user', 'identity_mismatch'), now)
                    continue
                if record['running']:
                    # Controller lock is free after the former process/cgroup
                    # exited. An interrupted launch consumes its recorded slot.
                    finish_attempt(record, _result('retrying', 'interrupted'), now)
                if record['attempt_count'] >= MAX_ATTEMPTS:
                    finish_attempt(record, _result('requires_user', 'attempts_exhausted'), now)
                    continue
                if record['next_attempt_at'] and datetime.fromisoformat(record['next_attempt_at']) > now:
                    continue
                report = reporter(repo, row, identity['session'])
                terminal = _terminal_report(report)
                if terminal:
                    finish_attempt(record, terminal, now)
                    continue
                # Busy ordinary work is deferred without consuming a recovery
                # attempt. The worker independently reacquires this same lock.
                with runtime_lock(canonical_runtime_lock_path(repo), exclusive=True):
                    pass
                record.update(attempt_count=record['attempt_count']+1, running=True, updated_at=now.isoformat())
                atomic_json(journal_path, journal)
                atomic_json(status_path, status_projection(journal, now, active_ids=by_id))
                code, output = launcher(repo, row, identity['session'], 'recovery', heartbeat=heartbeat)
                result = _worker_result(code, output)
                post_report = reporter(repo, row, identity['session'])
                if result['disposition'] == 'recovered':
                    # Completion proof must be accepted by the ordinary report
                    # before the heartbeat claims recovery is healthy.
                    result = _terminal_report(post_report) or _result('requires_user', 'checkpoint_invalid')
                finish_attempt(record, result, clock())
                break
            # Start the existing daily wrapper at most once per generation/date.
            # Record all active identities before launch, including interrupted
            # preflight (which has no application accounting context yet).
            if daily_records and len(daily_records) != len(rows):
                # The wrapper launches all active generations. Never replay an
                # already-started generation just to start a newly active one.
                for record in daily_records:
                    record['daily_started'] = True
                    finish_attempt(record, _result('wait_next_session', 'missed_session'), now)
                daily_records = []
            unsafe = any(record['identity']['generation_id'] in by_id and
                (record['disposition'] == 'requires_user' or
                 (record['identity']['session'] < today.isoformat() and record['disposition'] == 'retrying'))
                for record in journal['incidents'].values())
            if unsafe or exchange_date(clock()) != today:
                daily_records = []
            if daily_records:
                for record in daily_records:
                    record.update(daily_started=True, running=True, updated_at=now.isoformat())
                atomic_json(journal_path, journal)
                atomic_json(status_path, status_projection(journal, now, active_ids=by_id))
                code, _ = launcher(repo, rows[0], today.isoformat(), 'daily', heartbeat=heartbeat, expected_roster=rows)
                for record in daily_records:
                    record['running'] = False
                    row = by_id[record['identity']['generation_id']]
                    if identity_for(repo, row, today.isoformat()) != record['identity']:
                        finish_attempt(record, _result('requires_user', 'identity_mismatch'), clock())
                    else:
                        report = reporter(repo, row, today.isoformat())
                        finish_attempt(record, _terminal_report(report) or
                            _result('retrying', 'worker_timeout' if code == 124 else 'daily_failed'), clock())
                atomic_json(journal_path, journal)
            atomic_json(journal_path, journal)
            projection = status_projection(journal, clock(), active_ids=by_id)
            projection['controller_reason_code'] = None
        except (OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError, RuntimeLockInvalid, subprocess.SubprocessError) as error:
            # Preserve corrupt journal and emit a separate observable heartbeat.
            # Never reset attempts or overwrite uncertain durable launch state.
            projection = {'version': VERSION, 'schema_version': 1, 'updated_at': clock().isoformat(),
                'status': 'operator_required', 'controller_reason_code':
                    'identity_mismatch' if str(error) in {'identity_mismatch','generation_config_invalid'} else 'state_corrupt', 'incidents': []}
        atomic_json(status_path, projection)
        return projection


def worker(repo, session):
    from scripts.run_cohorts import _build_parser, _build_config, _build_orchestrator
    from tradingagents.strategies.orchestration.runtime_recovery import RecoveryUnavailable
    with runtime_lock(canonical_runtime_lock_path(repo), exclusive=True):
        matches = [row for row in manifest_rows(repo) if row['gen_id'] == os.environ.get('EVENTEDGE_GENERATION_ID')]
        if len(matches) != 1 or matches[0]['git_commit'] != os.environ.get('EVENTEDGE_GENERATION_COMMIT'):
            return _result('requires_user', 'identity_mismatch')
        row = matches[0]
        identity_for(repo, row, session)
        if Path(__file__).resolve().parents[1] != Path(row['worktree_path']).resolve():
            return _result('requires_user', 'identity_mismatch')
        args = _build_parser().parse_args(['--date', session])
        owner = _build_orchestrator(args, _build_config(args), row['gen_id'], row['git_commit'])
        try:
            result = recover_owner(owner, date.fromisoformat(session))
            if result['disposition'] == 'recovered':
                from tradingagents.strategies.orchestration.runtime_recovery import write_recovery_receipt
                write_recovery_receipt(owner, date.fromisoformat(session), configuration_sha256=row['config_sha256'])
            return result
        except RecoveryUnavailable:
            return _result('wait_next_session', 'checkpoint_missing')
        except OSError as error:
            import errno
            return _result('retrying', 'transient_io') if error.errno in {errno.EAGAIN, errno.EINTR, errno.ETIMEDOUT} else _result('requires_user', 'state_corrupt')
        except (ValueError, KeyError, TypeError, sqlite3.DatabaseError):
            return _result('requires_user', 'checkpoint_invalid')
        finally:
            for cohort in owner.cohorts:
                cohort['ledger'].close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--date')
    parser.add_argument('--prospective-session')
    parser.add_argument('--validate-prospective-launch', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--expected-generation')
    parser.add_argument('--expected-commit')
    parser.add_argument('--expected-profile-sha256')
    parser.add_argument('--expected-created-at')
    args = parser.parse_args()
    expected = dict(expected_generation=args.expected_generation, expected_commit=args.expected_commit,
                    expected_profile=args.expected_profile_sha256, expected_created=args.expected_created_at)
    if args.prospective_session is not None:
        if args.worker or args.status or args.date or any(value is None for value in expected.values()):
            parser.error('prospective mode requires all exact identity pins and no other mode')
        if args.validate_prospective_launch:
            validate_prospective_launch(args.repo_root, args.prospective_session, **expected)
            return
        result = prospective_once(args.repo_root, args.prospective_session, **expected)
        print(json.dumps(result, sort_keys=True))
        if result['status'] == 'operator_required':
            raise SystemExit(1)
        return
    if args.validate_prospective_launch or any(value is not None for value in expected.values()):
        parser.error('prospective identity pins require --prospective-session')
    if args.worker:
        if not args.date:
            parser.error('--worker requires --date')
        result = worker(args.repo_root.resolve(), args.date)
        print('RECOVERY_RESULT=' + json.dumps(result, sort_keys=True))
        return
    if args.status:
        print((args.repo_root/'data/runtime_recovery/status.json').read_text())
        return
    try:
        result = tick(args.repo_root, datetime.now(timezone.utc))
    except RuntimeLockBusy:
        return
    print(json.dumps(result, sort_keys=True))
    if result['status'] == 'operator_required':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
