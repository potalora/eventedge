#!/usr/bin/env python3
"""Existing timer entrypoint: one normal daily sample, bounded local recovery.

No install/activation side effects. Status is an ordinary application-owned JSON
heartbeat for the existing Hermes monitor. --worker is an internal pinned-worktree
mode; it never calls providers or models.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradingagents.strategies.orchestration.runtime_recovery import (
    VERSION, MAX_ATTEMPTS, MAX_INCIDENTS, RECOVERY_TIMEOUT_SECONDS,
    DAILY_TIMEOUT_SECONDS, atomic_json, bounded_process, finish_attempt,
    identity_for, incident_key, load_journal, new_incident, recover_owner,
    status_projection, _result,
)
from tradingagents.strategies.orchestration.runtime_lock import (
    RuntimeLockBusy, RuntimeLockInvalid, canonical_runtime_lock_path, runtime_lock,
)
from tradingagents.strategies.orchestration.trading_calendar import (
    exchange_date, is_session, session_close,
)


def manifest_rows(repo):
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
    return [row for row in rows if row['status'] == 'active']


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
            env['EVENTEDGE_RUNTIME_LOCK_FD'] = str(lock.fd)
            env['EVENTEDGE_RUNTIME_LOCK_MODE'] = 'exclusive'
            return bounded_process(['bash', str(repo/'scripts/daily_trading.sh')], cwd=repo,
                env=env, timeout=DAILY_TIMEOUT_SECONDS, heartbeat=heartbeat, pass_fds=(lock.fd,))
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
    if any(row.get('status') == 'strategy_defect' for row in report.get('source_health_failures', [])):
        return _result('requires_user', 'checkpoint_invalid')
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
    if (report.get('accounting_valid') and report.get('sources', {}).get('configuration') and report.get('sources', {}).get('unresolved')
            and not any(row.get('code') == 'accepted_sources_invalid' for row in report.get('diagnostics', []))):
        return _result('wait_next_session', 'accepted_outcome')
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
    args = parser.parse_args()
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
