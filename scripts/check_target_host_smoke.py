#!/usr/bin/env python3
"""Collect bounded read-only EventEdge host facts, or assess a sanitized snapshot.

Example: python scripts/check_target_host_smoke.py --ssh hermes-vps \
  --expected-hostname HOST --candidate-commit SHA --output local-report.json
SSH inspection never runs trading, preflight, models, providers or report writers.
Credential presence is deliberately distinct from successful authentication.
"""
from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path
import re
import subprocess
import sys

PRODUCTION = '/home/hermes/trading_agents'


def collect_snapshot(repo):
    """Allowlisted metadata only; each child command has a five-second bound."""
    import datetime
    import hashlib
    import json
    import os
    from pathlib import Path
    import socket
    import subprocess

    repo = Path(repo).resolve(strict=True)

    def command(args):
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=5,
                                    env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})
            return result.stdout.strip() if result.returncode == 0 else ''
        except (OSError, subprocess.TimeoutExpired):
            return ''

    def head(path):
        return command(['git', '-C', str(path), 'rev-parse', 'HEAD'])

    manifest_path = repo / 'data/generations/manifest.json'
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    active = []
    for row in manifest['generations']:
        if row.get('status') != 'active':
            continue
        worktree = Path(row['worktree_path'])
        # Resolve before reading Git: a lexical child can escape via symlinks.
        boundary = repo / '.worktrees'
        try:
            in_boundary = (boundary.resolve(strict=True) == boundary and
                           worktree.parent == boundary and
                           worktree.resolve(strict=True).parent == boundary and
                           worktree.is_dir())
        except (OSError, RuntimeError):
            in_boundary = False
        active.append({'gen_id': row['gen_id'], 'git_commit': row['git_commit'],
                       'worktree_path': str(worktree),
                       'head': head(worktree) if in_boundary else '',
                       'worktree_in_boundary': in_boundary,
                       'latest_recorded_runs': [{k: entry.get(k) for k in
                           ('date', 'action', 'success', 'elapsed_s')} for entry in
                           row.get('run_history', [])[-3:]]})
    units = {}
    properties = ('LoadState', 'ActiveState', 'SubState', 'UnitFileState', 'User',
                  'WorkingDirectory', 'Result', 'ExecMainStatus',
                  'LastTriggerUSec', 'NextElapseUSecRealtime', 'Triggers')
    for name in ('trade.service', 'trade.timer', 'trade-preflight.service',
                 'trade-preflight.timer', 'trade-rerun.service', 'trade-rerun.path'):
        raw = command(['systemctl', 'show', name, '--no-pager',
                       '--property=' + ','.join(properties)])
        units[name] = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    credentials = {key: False for key in
                   ('OPENAI_API_KEY', 'ALPACA_API_KEY', 'ALPACA_SECRET_KEY', 'FMP_API_KEY')}
    env_path = repo / '.env'
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            key, separator, value = line.strip().removeprefix('export ').partition('=')
            if separator and key in credentials:
                credentials[key] = bool(value.strip().strip('"').strip("'"))
    reports = repo / 'docs/reports'
    latest = []
    if reports.is_dir():
        # Metadata only; no report content or ledger access.
        paths = sorted((p for p in reports.iterdir() if p.is_file() and
                        p.suffix in ('.json', '.md')), key=lambda p: p.stat().st_mtime,
                       reverse=True)[:4]
        latest = [{'name': p.name, 'size_bytes': p.stat().st_size,
                   'mtime_utc': datetime.datetime.fromtimestamp(p.stat().st_mtime,
                       datetime.timezone.utc).isoformat()} for p in paths]
    shadow = Path('/home/hermes/.hermes/private/projects/eventedge')
    return {'collected_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'hostname': socket.gethostname(),
            'production': {'path': str(repo), 'head': head(repo)},
            'shadow': {'path': str(shadow), 'exists': shadow.is_dir(),
                       'head': head(shadow) if shadow.is_dir() else ''},
            'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
            'active_generations': active, 'units': units,
            'credentials_present': credentials, 'latest_report_files': latest,
            'paths': {'manifest_readable': os.access(manifest_path, os.R_OK),
                      'report_directory_writable': os.access(reports, os.W_OK),
                      'operational_report_script_present':
                          (repo / 'scripts/generate_operational_report.py').is_file()}}


def assess_snapshot(snapshot, candidate, hostname):
    """Report evidence dimensions without equating presence with authentication."""
    failures = []
    if snapshot.get('hostname') != hostname:
        failures.append('target_identity_mismatch')
    production = snapshot.get('production', {})
    if production.get('path') != PRODUCTION:
        failures.append('production_boundary_mismatch')
    active = snapshot.get('active_generations', [])
    if not active or len({row['gen_id'] for row in active}) != len(active):
        failures.append('active_generation_scope_missing')
    if any(row.get('worktree_in_boundary') is not True for row in active):
        failures.append('active_worktree_boundary_invalid')
    if any(not isinstance(row.get(key), str) or
           not re.fullmatch(r'[0-9a-f]{40}', row[key])
           for row in active for key in ('head', 'git_commit')):
        failures.append('active_generation_identity_unreadable')
    credentials = snapshot.get('credentials_present', {})
    if not all(type(value) is bool for value in credentials.values()):
        failures.append('credential_presence_invalid')
    paths = snapshot.get('paths', {})
    for key in ('manifest_readable', 'report_directory_writable'):
        if paths.get(key) is not True:
            failures.append(key + '_failed')
    units = snapshot.get('units', {})
    service = units.get('trade.service', {})
    if service.get('User') != 'hermes' or service.get('WorkingDirectory') != PRODUCTION:
        failures.append('scheduler_production_boundary_mismatch')
    if units.get('trade.timer', {}).get('ActiveState') != 'active':
        failures.append('daily_timer_not_active')
    deployed = bool(active) and production.get('head') == candidate and all(
        row.get('worktree_in_boundary') is True and row.get('head') == candidate and
        row.get('git_commit') == candidate for row in active)
    boundaries = ['authentication_not_proven', 'scheduled_candidate_execution_not_proven',
                  'candidate_report_publication_not_proven']
    if not deployed:
        boundaries.insert(0, 'candidate_not_deployed')
    # Publish a strict selection; never forward arbitrary snapshot fields.
    return {'schema_version': 1, 'inspection_passed': not failures,
            'candidate_commit': candidate, 'candidate_deployed': deployed,
            'authentication_proven': False, 'scheduled_candidate_execution_proven': False,
            'candidate_report_publication_proven': False,
            'inspection_failures': failures, 'rollout_boundaries': boundaries,
            'hostname': snapshot.get('hostname'),
            'production': {key: production.get(key) for key in ('path', 'head')},
            'active_generations': [{key: row.get(key) for key in
                ('gen_id', 'git_commit', 'head', 'worktree_path', 'worktree_in_boundary',
                 'latest_recorded_runs')} for row in active],
            'credentials_present': {key: value for key, value in credentials.items()
                                    if type(value) is bool},
            'units': {name: {key: row[key] for key in
                ('LoadState', 'ActiveState', 'SubState', 'UnitFileState', 'User',
                 'WorkingDirectory', 'Result', 'ExecMainStatus', 'LastTriggerUSec',
                 'NextElapseUSecRealtime', 'Triggers') if key in row}
                for name, row in units.items() if name in
                ('trade.service', 'trade.timer', 'trade-preflight.service',
                 'trade-preflight.timer', 'trade-rerun.service', 'trade-rerun.path')},
            'paths': {key: paths.get(key) is True for key in
                ('manifest_readable', 'report_directory_writable',
                 'operational_report_script_present')},
            'latest_report_files': [{key: row.get(key) for key in
                ('name', 'size_bytes', 'mtime_utc')}
                for row in snapshot.get('latest_report_files', [])[:4]],
            'manifest_sha256': snapshot.get('manifest_sha256'),
            'collected_at_utc': snapshot.get('collected_at_utc')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--snapshot', type=Path)
    source.add_argument('--ssh', help='Configured SSH host alias; read-only collector runs as hermes.')
    parser.add_argument('--candidate-commit', required=True)
    parser.add_argument('--expected-hostname', required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--snapshot-output', type=Path,
                        help='Optional local file for the sanitized collected snapshot.')
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r'[0-9a-f]{40}', args.candidate_commit):
            raise ValueError('candidate identity invalid')
        if args.snapshot:
            snapshot = json.loads(args.snapshot.read_text())
        else:
            if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.ssh) or args.ssh.startswith('-'):
                raise ValueError('SSH alias invalid')
            code = inspect.getsource(collect_snapshot) + '\nimport json\nprint(json.dumps(collect_snapshot(' + repr(PRODUCTION) + ')))\n'
            result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o',
                'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10', args.ssh,
                'sudo', '-n', '-u', 'hermes', 'python3', '-'], input=code,
                capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError('collection unavailable')
            snapshot = json.loads(result.stdout)
        report = assess_snapshot(snapshot, args.candidate_commit, args.expected_hostname)
        encoded = json.dumps(report, indent=2, sort_keys=True) + '\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded)
        if args.snapshot_output and args.ssh:
            args.snapshot_output.parent.mkdir(parents=True, exist_ok=True)
            args.snapshot_output.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + '\n')
        print(encoded, end='')
        return 0 if report['inspection_passed'] else 1
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError,
            subprocess.TimeoutExpired):
        print('Target-host inspection unavailable: invalid input, access, or bounded collection failure.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
