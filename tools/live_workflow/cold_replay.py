"""Run a native cold workflow on a private pristine copy and an immutable tape.

The external supervisor restores and mounts the baseline at its original path.
This module never clears accepted state to fabricate a fresh campaign, reads
model authentication, or relaxes recorded request identity.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import parse_qs, urlsplit

from . import runner

CASES = ('baseline', 'source_http_401', 'source_http_429', 'source_http_503',
         'source_malformed_json', 'model_malformed')


def mutation_changes(case, event):
    """Mutate a retained response, never its request, role or provenance."""
    if case == 'model_malformed':
        runner.require(event.get('boundary') == 'model_helper'
            and event.get('operation') == 'call_analysis_model', 'mutation_requires_model_event')
        value = deepcopy(event.get('sdk_value'))
        runner.require(isinstance(value, dict) and value.get('outcome') == 'returned'
            and value.get('response_observed') is True and event.get('failure') is None,
            'mutation_requires_completed_model_response')
        value.update(received_text='{', returned_text='{')
        return {'sdk_value': value}
    runner.require(case in CASES and case.startswith('source_'), 'unsupported_workflow_case')
    runner.require(event.get('boundary') in ('http_decoded', 'source_http')
        and event.get('failure') is None and event.get('response_file') is not None
        and event.get('status') == 200, 'mutation_requires_completed_http_response')
    if case == 'source_malformed_json':
        return {'response_body': b'{'}
    return {'status': int(case.rsplit('_', 1)[1])}


def evaluate_outcome(*, case, process_success, checks, reproduction,
                     replay_failures, consumed, expected, target_event,
                     effects=None, baseline_qualified=False, governed_blocked=False,
                     process_failure=None, report_verified=False, source_fault_observed=False):
    result = {'status': 'fail', 'reason': 'native_acceptance_incomplete',
              'whole_workflow_coverage': False}
    if replay_failures:
        return result | {'status': 'blocked', 'reason': 'missing_or_incompatible_exact_capture'}
    if process_failure is not None:
        return result | {'reason': 'native_process_infrastructure_failure'}
    if target_event is not None and target_event not in consumed:
        return result | {'status': 'blocked', 'reason': 'selected_mutation_not_reached'}
    if case != 'baseline' and not baseline_qualified:
        return result | {'status': 'blocked', 'reason': 'successful_cold_baseline_required'}
    checks_pass = runner.native_acceptance_passed({'checks': checks,
        'canonical_report_reproduction': reproduction,
        'recorded_boundary_coverage': {'unfinished_reservations': 0}})
    if case in ('baseline', 'model_malformed'):
        if consumed != expected:
            return result | {'status': 'blocked', 'reason': 'unconsumed_recordings'}
        if process_success and checks_pass:
            return result | {'status': 'pass', 'reason': 'native_workflow_accepted',
                             'whole_workflow_coverage': True}
    elif case.startswith('source_'):
        # A source fault qualifies a full native fail-closed path only when the
        # governed gate rejects it and no economic effect was committed.
        if (not process_success and governed_blocked and report_verified and source_fault_observed
                and checks.get('source_completeness') is False
                and effects is not None and effects.get('verified') is True
                and effects.get('economic_rows') == 0):
            return result | {'status': 'pass', 'reason': 'native_source_failure_held',
                             'whole_workflow_coverage': True}
    return result


def economic_effects(state):
    """Inspect all private ledger databases; unknown/corrupt state never means zero."""
    state = runner.no_symlinks(state)
    runner.require(state.is_dir(), 'cold_state_directory_required')
    counts = {}
    for path in sorted(state.rglob('*')):
        runner.require(not path.is_symlink(), 'cold_state_symlink')
        if not path.is_file():
            continue
        is_database = runner._is_database(path)
        runner.require(is_database or path.suffix not in ('.db', '.sqlite', '.sqlite3'),
                       'cold_state_database_header_invalid')
        if not is_database:
            continue
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            runner.require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)],
                           'cold_state_integrity')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('order_intents', 'fills', 'lots', 'lot_closures',
                          'external_orders', 'cash_events', 'fee_events', 'dividend_events'):
                if table in tables:
                    counts[str(path.relative_to(state)) + ':' + table] = db.execute(
                        'SELECT COUNT(*) FROM "' + table + '"').fetchone()[0]
    return {'verified': True, 'economic_rows': sum(counts.values()), 'table_rows': counts}


def replay_usage(cursor, records):
    cursor = runner.no_symlinks(cursor)
    by_id = {row['event_id']: row for row in records}
    consumed = set()
    for path in cursor.glob('*.consumed'):
        runner.no_symlinks(path)
        value = json.loads(path.read_bytes())
        runner.require(path.stem in by_id and value == {'phase': by_id[path.stem]['phase']},
                       'cold_replay_consumption_identity')
        consumed.add(path.stem)
    failures = []
    for path in sorted(cursor.glob('failure-*.json')):
        runner.no_symlinks(path)
        value = json.loads(path.read_bytes())
        runner.require(type(value) is dict and set(value) in
                       ({'phase','reason'},{'phase','reason','lookup'})
                       and all(type(value.get(key)) is str for key in ('phase','reason')),
                       'cold_replay_failure_record_invalid')
        if 'lookup' in value:
            lookup=value['lookup']
            runner.require(type(lookup) is dict and set(lookup)==
                           {'boundary','operation','request_sha256','occurrence','event_id'},
                           'cold_replay_failure_record_invalid')
            runner.require(all(type(lookup[key]) is str and
                           re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',lookup[key]) is not None
                           for key in ('boundary','operation'))
                           and all(type(lookup[key]) is str and
                           re.fullmatch(r'[a-f0-9]{64}',lookup[key]) is not None
                           for key in ('request_sha256','event_id'))
                           and type(lookup['occurrence']) is int and 0<=lookup['occurrence']<1000000,
                           'cold_replay_failure_record_invalid')
        failures.append(value)
    return consumed, failures


def _baseline_success(path, expected_sha256, identity):
    if path is None:
        return False
    path = runner.no_symlinks(path)
    runner.require(expected_sha256 is not None and runner.sha(path) == expected_sha256,
                   'cold_baseline_success_hash')
    runner.require(path.stat().st_size <= 1024**2, 'cold_baseline_result_bound')
    value = json.loads(path.read_bytes())
    return (value.get('case') == 'baseline' and value.get('status') == 'pass'
        and value.get('whole_workflow_coverage') is True
        and all(value.get(key) == identity[key] for key in
                ('candidate_commit', 'session', 'contract_sha256', 'baseline_tree_sha256',
                 'parent_tape_sha256', 'tool_sha256')))


def source_fault_observed(report, event, session, *, repo, generation, commit, profile_sha256):
    """Recognize only native governed-bar rejection tied to the mutated SIP request."""
    if not event or event.get('phase') != 'preflight_governed':
        return False
    request = event.get('public_request', {})
    url = urlsplit(request.get('url', ''))
    if url.scheme != 'https' or url.hostname != 'data.alpaca.markets' or url.path != '/v2/stocks/bars':
        return False
    symbols = set(','.join(parse_qs(url.query).get('symbols', [])).split(',')) - {''}
    folder = runner.no_symlinks(Path(repo) / 'data/logs/run_attempts')
    for attempt in report.get('attempts', []):
        if attempt.get('action') != 'preflight' or attempt.get('preflight_mode') != 'governed':
            continue
        path = runner.no_symlinks(attempt.get('evidence_path', ''))
        runner.require(path.parent == folder and path.is_file() and path.stat().st_size <= 8 * 1024**2,
                       'cold_governed_attempt_path')
        artifact = json.loads(path.read_bytes())
        result = artifact.get('result', {})
        # The operational report intentionally flattens failure diagnostics.
        # Read its exact retained native artifact for ticker-level attribution.
        if not (artifact.get('schema_version') == 1 and artifact.get('action') == 'preflight'
                and artifact.get('preflight_mode') == 'governed'
                and artifact.get('generation_id') == generation and artifact.get('generation_commit') == commit
                and artifact.get('requested_session') == session and artifact.get('process_status') == 'completed'
                and type(artifact.get('process_return_code')) is int and artifact['process_return_code'] != 0
                and result.get('config_sha256') == profile_sha256 and result.get('preflight_mode') == 'governed'
                and result.get('success') is False and result.get('governed_ok') is False
                and result.get('governed_probe_status') == 'failed' and result.get('state_status') == 'uninitialized'):
            continue
        for ticker, reason in result.get('governed_failure_map', {}).items():
            if ticker in symbols and reason == 'invalid ' + ticker + '/' + session:
                return True
    return False


def assert_process_quiescent(timeout=5):
    """In the supervisor's private PID namespace only this worker and ancestors remain."""
    allowed = set()
    pid = os.getpid()
    while pid > 0 and pid not in allowed:
        allowed.add(pid)
        status = (Path('/proc') / str(pid) / 'status').read_text()
        pid = int(next(line.split()[1] for line in status.splitlines() if line.startswith('PPid:')))
    deadline = time.monotonic() + timeout
    while True:
        extra = {int(p.name) for p in Path('/proc').iterdir() if p.name.isdigit()} - allowed
        if not extra:
            return {'private_pid_namespace_quiescent': True, 'remaining_processes': len(allowed)}
        runner.require(time.monotonic() < deadline, 'cold_descendants_not_quiescent')
        time.sleep(.05)


def run(args):
    from .baseline import verify_restored_baseline
    from .edges import _snapshot, require_network_denied
    from .recorded_io import create_mutation, verify_tape

    root = runner.no_symlinks(args.root)
    output = runner.no_symlinks(args.output_dir)
    tape = runner.no_symlinks(args.tape_dir)
    runner.require(output.is_dir() and not any(output.iterdir()) and output.stat().st_uid == os.getuid()
        and output.stat().st_mode & 0o077 == 0, 'cold_empty_private_output_required')
    runner.require(not output.is_relative_to(root) and not root.is_relative_to(output)
        and not tape.is_relative_to(root) and not tape.is_relative_to(output), 'cold_paths_must_be_separate')
    boundary = runner.require_boundary(root, args.boundary_receipt, offline=True)
    net = require_network_denied()
    receipt_path = runner.no_symlinks(args.baseline_receipt)
    runner.require(runner.sha(receipt_path) == args.baseline_sha256, 'cold_baseline_receipt_hash')
    receipt = json.loads(receipt_path.read_bytes())
    verify_restored_baseline(root, receipt)
    runner.require(runner.sha(root / 'contract.json') == args.contract_sha256
        == receipt['contract_sha256'], 'cold_contract_hash')
    contract, row = runner.verify_contract(root)
    runner.require(args.session == contract['reference_session'], 'reference_session_changed')
    runner.require(not (root / 'native-attempt.json').exists()
        and not list((root / 'tape/events').glob('*.json')), 'cold_pristine_acquisition_required')
    runner.require([p.name for p in Path(row['state_dir']).iterdir()] == [Path(row['config_file']).name],
                   'cold_pristine_generation_required')
    records, run_identity, parent_digest = _snapshot(tape)
    runner.require(records and not list((tape / 'reservations').glob('*.pending')), 'cold_completed_tape_required')
    ids = {event['event_id'] for event in records}
    runner.require({p.stem for p in (tape / 'reservations').glob('*.json')} == ids,
                   'cold_unfinished_parent_reservations')
    original_config = json.loads((root / 'hook-config.json').read_bytes())
    runner.require(run_identity == original_config['run_identity'], 'cold_tape_contract_identity')
    runner.require(all(event.get('lane') == 'native_observation' and not event.get('parent')
        for event in records), 'cold_native_parent_required')
    recorded_clocks=any(event['boundary']=='observed_clock' for event in records)
    clock_coverage=runner.clock_capture_coverage(tape,records)
    runner.require(clock_coverage['status'] not in ('invalid','incomplete'),
        'cold_observed_clock_capture_incomplete')
    identity = {'schema_version': 1, 'lane': 'cold_recorded_workflow', 'case': args.case,
        'candidate_commit': contract['candidate_commit'], 'session': args.session,
        'contract_sha256': args.contract_sha256, 'baseline_receipt_sha256': args.baseline_sha256,
        'baseline_tree_sha256': runner.digest(receipt['snapshot_inventory']),
        'parent_tape_sha256': parent_digest, 'boundary_receipt_sha256': boundary,
        'kernel_network_namespace': net, 'live_provider_calls': 0, 'live_model_calls': 0,
        'clock_policy': 'recorded_scoped_wall_clock_inputs' if recorded_clocks else 'real_wall_clock',
        'recorded_clock_capture':clock_coverage,
        'admission_and_deadline_clocks':'real_wall_clock_and_monotonic',
        'authentication_lifecycle_replayed': False,
        'offline_auth_placeholders': ['yahoo_anonymous_crumb', 'recorded_plan_profile_identity'],
        'started_at': datetime.now(timezone.utc).isoformat()}
    tool_root = Path(__file__).resolve().parents[2]
    identity['tool_sha256'] = {str(p.relative_to(tool_root)): runner.sha(p)
        for p in sorted((tool_root / 'tools/live_workflow').glob('*.py'))}
    baseline_qualified = _baseline_success(getattr(args, 'baseline_success_receipt', None),
        getattr(args, 'baseline_success_sha256', None), identity)
    if args.case != 'baseline':
        runner.require(baseline_qualified, 'successful_cold_baseline_required')
    selected = None
    if args.case == 'baseline':
        runner.require(args.event_id is None, 'baseline_has_no_mutation')
        replay_tape = tape
    else:
        selected = next((event for event in records if event['event_id'] == args.event_id), None)
        runner.require(selected is not None, 'cold_mutation_event_missing')
        if args.case.startswith('source_'):
            runner.require(selected['phase'] == 'preflight_governed', 'source_mutation_requires_governed_phase')
        replay_tape = output / 'derivative-tape'
        create_mutation(parent_tape=tape, destination=replay_tape, event_id=args.event_id,
            label='cold_workflow_' + args.case, changes=mutation_changes(args.case, selected))
        identity['mutation'] = {key: selected[key] for key in
            ('event_id', 'record_sha256', 'request_sha256', 'phase', 'occurrence', 'boundary', 'operation')}
        records = verify_tape(replay_tape)
    credentials = runner.selected_credential_environment(args.credential_env)
    identity['credential_env_names'] = sorted(credentials)
    runner.private_json(output / 'cold-attempt.json', identity)
    runner.private_json(root / 'native-attempt.json', {'lane': 'cold_recorded_workflow',
        'baseline_receipt_sha256': args.baseline_sha256, 'case': args.case})
    cursor = output / 'cursor'
    cursor.mkdir(mode=0o700)
    config = {**original_config, 'mode': 'offline_replay', 'tape_dir': str(replay_tape),
        'dependency_site': contract['dependency_site'], 'dependency_sha256': contract['dependency_sha256'],
        'replay_state_dir': str(cursor),
        'replay_observed_clocks':recorded_clocks,
        'offline_auth_placeholders': ['yahoo_anonymous_crumb', 'recorded_plan_profile_identity'],
        'recorded_plan_auth_path': contract['auth_path']}
    config_path = output / 'replay-config.json'
    runner.private_json(config_path, config)
    site = root / 'repo/.venv/lib' / ('python' + contract['python_version']) / 'site-packages'
    startup = site / 'live_workflow.pth'
    original_startup_hash = runner.sha(startup)
    runner._write_owned_startup(site, contract['dependency_site'], tool_root, config_path)
    runner.private_json(output / 'tool-overlay.json', {'scope': 'private_restored_runtime_only',
        'original_startup_sha256': original_startup_hash, 'replay_startup_sha256': runner.sha(startup),
        'replay_config_sha256': runner.sha(config_path), 'candidate_code_changed': False})
    env = runner.owned_environment(root, credentials)
    env['EVENTEDGE_LIVE_CONFIG'] = str(config_path)
    env['EVENTEDGE_LIVE_REPLAY_STATE'] = str(cursor)
    process_success = False
    report_success = False
    try:
        runner.run_process(runner.prospective_argv(root / 'repo', row, args.session), root,
            env=env, label='cold-native', timeout=1400)
        process_success = True
    except runner.AcceptanceError:
        pass  # The actual exit and bounded redacted console remain in output.
    try:
        runner.run_process([str(root / 'repo/.venv/bin/python'), '-m',
            'tools.live_workflow.bootstrap', 'report', '--root', str(root), '--session', args.session],
            root, env=env, label='cold-report', timeout=120)
        report_success = True
    except runner.AcceptanceError:
        pass
    quiescence = assert_process_quiescent()
    runner.validate_candidate(root / 'repo', contract['candidate_commit'])
    runner.validate_candidate(row['worktree_path'], contract['candidate_commit'])
    runner.require(runner.sha(root / 'contract.json') == args.contract_sha256
        and runner.sha(row['config_file']) == contract['profile_sha256'], 'cold_pinned_identity_changed')
    runner.require(_snapshot(tape)[2] == parent_digest, 'cold_parent_tape_changed')
    consumed, failures = replay_usage(cursor, records)
    report = json.loads((root / 'native-report.json').read_bytes()) if report_success else {}
    reproduction = json.loads((root / 'report-reproduction.json').read_bytes()) if report_success else {}
    checks = runner.report_checks(report)
    report_verified = (report_success and report.get('generation_commit') == contract['candidate_commit']
        and report.get('profile_sha256') == contract['profile_sha256']
        and report.get('session') == args.session and report.get('generation_id') == row['gen_id']
        and all(reproduction.get(key) is True for key in
                ('canonical_json_identical', 'canonical_markdown_identical')))
    fault_observed = source_fault_observed(report, selected, args.session, repo=root / 'repo',
        generation=row['gen_id'], commit=contract['candidate_commit'], profile_sha256=contract['profile_sha256'])
    effects = economic_effects(row['state_dir'])
    process_receipt = json.loads((root / 'cold-native-process.json').read_bytes())
    daily_log = root / 'repo/data/logs' / ('daily_' + args.session + '.log')
    governed_blocked = (daily_log.exists() and
        ('GOVERNED PREFLIGHT FAILED for ' + args.session + '; daily run blocked') in daily_log.read_text())
    outcome = evaluate_outcome(case=args.case, process_success=process_success, checks=checks,
        reproduction=reproduction, replay_failures=failures, consumed=consumed,
        expected=ids, target_event=args.event_id, effects=effects, baseline_qualified=baseline_qualified,
        governed_blocked=governed_blocked, process_failure=process_receipt.get('failure'),
        report_verified=report_verified, source_fault_observed=fault_observed)
    result = {**identity, **outcome, 'process_success': process_success, 'checks': checks,
        'report_reproduction': reproduction, 'economic_effects': effects,
        'governed_gate_blocked_daily': governed_blocked,
        'independent_report_verified': report_verified, 'selected_source_fault_observed': fault_observed,
        'process_quiescence': quiescence,
        'native_process_receipt_sha256': runner.sha(root / 'cold-native-process.json'),
        'recorded_events': len(ids), 'consumed_events': len(consumed),
        'unconsumed_events': sorted(ids - consumed), 'replay_failures': failures,
        'parent_tape_preserved': True, 'original_baseline_preservation': 'external_supervisor_postcheck_required',
        'finished_at': datetime.now(timezone.utc).isoformat()}
    runner.private_json(output / 'cold-workflow.json', result)
    return result
