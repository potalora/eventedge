"""Bounded controlled replays derived from one immutable native campaign tape.

A case exercises an exact recorded boundary or the candidate FINRA adapter, not
an entire cold workflow. Parent captures, request identity and original duplicate
occurrence are retained. Execution requires the supervisor's offline receipt and
a current network namespace with no non-loopback interfaces.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from . import recorded_io as tape

MAX_EDGE_BODY = 1024 * 1024
FINRA_URL = 'https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest'
FINRA_HEADERS = {'record-total', 'record-offset', 'record-limit'}
KEY_FIELDS = ('schema_version','run_identity','phase','boundary','operation','public_request','request_sha256')
CASES = (
    'finra_parent_control',
    'source_403','source_429','source_timeout','source_malformed_json','source_partial_json',
    'finra_duplicate_symbols','finra_missing_symbol','finra_invalid_pagination',
    'finra_stale_settlement','finra_future_settlement',
    'model_timeout','model_refusal','model_malformed_output',
    'wrong_request_identity','wrong_run_identity','interrupted_reservation',
)


class EdgeError(RuntimeError):
    pass


def _require(value, reason):
    if not value:
        raise EdgeError(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _path(path):
    path = Path(path).absolute()
    _require(not any(part.is_symlink() for part in (path, *path.parents)), 'symlink_path')
    return path


def _write(path, value, *, binary=False):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(value if binary else _canonical(value))
        output.flush()
        os.fsync(output.fileno())


def _snapshot(parent):
    parent = _path(parent)
    records = tape.verify_tape(parent)
    identity = json.loads((parent / 'identity.json').read_bytes())
    known_bodies = {r['response_file']:r['response_sha256'] for r in records if r.get('response_file')}
    inventory = {}
    for path in sorted(parent.rglob('*')):
        _require(not path.is_symlink() and (path.is_dir() or path.is_file()), 'unsupported_parent_path')
        if path.is_file():
            relative = str(path.relative_to(parent))
            inventory[relative] = known_bodies.get(relative) or _sha(path)
    _require(all(r['run_identity'] == identity for r in records), 'parent_event_identity')
    return records, identity, _digest(inventory)


def _body(parent, record):
    if not record.get('response_file') or record.get('response_bytes', MAX_EDGE_BODY + 1) > MAX_EDGE_BODY:
        return None
    return (Path(parent) / record['response_file']).read_bytes()


def _json_body(parent, record):
    if record.get('failure') is not None or record.get('status') != 200:
        return None
    body = _body(parent, record)
    try:
        return json.loads(body) if body else None
    except (ValueError, UnicodeError, RecursionError):
        return None


def _reconstructable_http(record):
    request = record.get('public_request', {})
    body = request.get('body')
    return (record.get('operation') == 'requests.send' and isinstance(request.get('headers'), dict)
            and isinstance(request.get('url'), str) and request.get('method') in {'GET','POST','HEAD'}
            and (body is None or isinstance(body, (dict, list)))
            and not (isinstance(body, dict) and 'opaque_sha256' in body)
            and record.get('response_bytes', 0) <= MAX_EDGE_BODY)


def _finra_context(record, rows):
    request = record.get('public_request', {})
    body = request.get('body')
    try:
        symbols = body['domainFilters'][0]['values']
        settlement = body['compareFilters'][0]['fieldValue']
        headers = record['safe_headers']
        valid = (request['url'] == FINRA_URL and request['method'] == 'POST' and isinstance(rows, list)
                 and bool(rows) and all(isinstance(row, dict) for row in rows)
                 and isinstance(symbols, list) and 1 <= len(symbols) <= 100
                 and symbols == sorted(set(symbols)) and all(isinstance(s, str) for s in symbols)
                 and isinstance(settlement, str) and body['limit'] == len(symbols) + 1
                 and FINRA_HEADERS <= headers.keys() and headers['record-offset'] == '0'
                 and int(headers['record-limit']) == len(symbols) + 1 and int(headers['record-total']) == len(rows)
                 and all(row.get('symbolCode') in symbols and row.get('settlementDate') == settlement for row in rows)
                 and len({row['symbolCode'] for row in rows}) == len(rows))
        if valid:
            date.fromisoformat(settlement)
            return {'symbols': symbols, 'settlement': settlement}
    except (KeyError, TypeError, ValueError, IndexError):
        pass
    return None


def plan_edges(parent_tape):
    """Read-only selection. Planned means eligible, never passed or exercised."""
    parent = _path(parent_tape)
    records, identity, parent_digest = _snapshot(parent)
    native = [r for r in records if r.get('lane') == 'native_observation' and not r.get('parent')]
    native.sort(key=lambda r: (r['occurrence'], r['observed_at'], r['event_id']))
    http = next((r for r in native if _reconstructable_http(r)), None)
    json_http = next((r for r in native if _reconstructable_http(r) and _json_body(parent, r) is not None), None)
    finra = next((r for r in native if _reconstructable_http(r) and r['public_request']['url'] == FINRA_URL
                  and _finra_context(r, _json_body(parent, r)) is not None), None)
    model = next((r for r in native if r.get('boundary') == 'model_helper'
                  and r.get('public_request', {}).get('native_provider') == 'chatgpt_plan'
                  and r['public_request'].get('document_inputs') is None
                  and isinstance(r.get('sdk_value'), dict)), None)
    any_record = finra or http or model or next((r for r in native if r.get('response_bytes',0) <= MAX_EDGE_BODY), None)
    completed = {r['event_id'] for r in records}
    interrupted = None
    for reservation_path in sorted((parent/'reservations').glob('*.json')):
        reservation = json.loads(reservation_path.read_bytes())
        if reservation.get('event_id') not in completed and reservation.get('run_identity') == identity and reservation.get('lane') == 'native_observation':
            interrupted = {**reservation, 'record_sha256':_sha(reservation_path)}
            break
    cases = []
    for name in CASES:
        level = 'tape_binding'
        if name.startswith('source_'):
            record = finra or (json_http if 'json' in name else http)
            level = 'native_finra_adapter' if record is finra and finra is not None else ('http_json_decoder' if 'json' in name else 'http_transport')
        elif name.startswith('finra_'):
            record, level = finra, 'native_finra_adapter'
        elif name.startswith('model_'):
            record, level = model, 'native_model_helper_replay'
            if name == 'model_malformed_output' and record and not isinstance(record['sdk_value'].get('received_text'), str):
                record = None
        else:
            record = interrupted if name == 'interrupted_reservation' and interrupted else any_record
        row = {'case':name, 'level':level, 'status':'planned' if record else 'unexercised',
               'whole_workflow_coverage':False}
        if record:
            row['parent_kind'] = 'reservation' if record is interrupted else 'finished_event'
            row.update(parent_event_id=record['event_id'], parent_record_sha256=record['record_sha256'],
                       phase=record['phase'], occurrence=record['occurrence'], request_sha256=record['request_sha256'])
        else:
            row['reason'] = ('missing_complete_native_finra_capture_with_public_headers' if name.startswith('finra_')
                             else 'missing_eligible_native_parent_capture')
        cases.append(row)
    return {'schema_version':1, 'lane':'controlled_mutation', 'whole_workflow_coverage':False,
            'parent_tape_sha256':parent_digest, 'run_identity':identity, 'cases':cases,
            'limitations':['single_recorded_occurrence_per_case','no_cold_workflow_replay',
                           'no_changed_prompt_response_reuse','native_failures_remain_native_failures']}


def materialize_case(parent_tape, destination, plan, case):
    """Copy at most one bounded body; never duplicate the complete source tape."""
    parent, destination = _path(parent_tape), _path(destination)
    _require(case['status'] == 'planned' and case['case'] in CASES, 'case_not_planned')
    _require(not destination.exists() and destination != parent and parent not in destination.parents, 'new_external_case_directory_required')
    if case.get('parent_kind') == 'reservation':
        _require(case['case'] == 'interrupted_reservation', 'reservation_case_mismatch')
        original_path = parent/'reservations'/(case['parent_event_id']+'.json')
        record = json.loads(original_path.read_bytes())
        record.update(record_sha256=_sha(original_path), response_file=None, response_sha256=None, response_bytes=0, status=None, failure=None, sdk_value=None, safe_headers={})
    else:
        record = tape._validate_record(parent, parent / 'events' / (case['parent_event_id'] + '.json'))
    _require(record['record_sha256'] == case['parent_record_sha256'] and record['run_identity'] == plan['run_identity'], 'selected_parent_changed')
    _require(record.get('lane') == 'native_observation' and record.get('response_bytes', 0) <= MAX_EDGE_BODY, 'eligible_native_parent_required')
    body = _body(parent, record)
    original = deepcopy(record)
    name = case['case']
    details = {}
    if name in ('source_403','source_429'):
        record.update(status=int(name.rsplit('_', 1)[1]), failure=None)
        if name == 'source_429':
            record['safe_headers']['retry-after'] = '1'
    elif name == 'source_timeout':
        record.update(status=None, failure={'type':'ReadTimeout','reason':'controlled_source_timeout'})
    elif name in ('source_malformed_json','source_partial_json'):
        _require(body is not None, 'json_body_missing')
        body = b'{' if name == 'source_malformed_json' else body.rstrip()[:-1]
    elif name.startswith('finra_') and name != 'finra_parent_control':
        rows = json.loads(body)
        context = _finra_context(record, rows)
        _require(context is not None, 'finra_parent_context_missing')
        if name == 'finra_duplicate_symbols':
            rows.append(deepcopy(rows[0]))
        elif name == 'finra_missing_symbol':
            details['expected_missing_symbol'] = rows.pop(0)['symbolCode']
        elif name == 'finra_invalid_pagination':
            record['safe_headers']['record-offset'] = '1'
        else:
            delta = -31 if name == 'finra_stale_settlement' else 31
            changed = (date.fromisoformat(context['settlement']) + timedelta(days=delta)).isoformat()
            rows[0]['settlementDate'] = changed
            rows[0]['accountingYearMonthNumber'] = int(changed.replace('-', ''))
        if name in ('finra_duplicate_symbols','finra_missing_symbol'):
            record['safe_headers']['record-total'] = str(len(rows))
            if 'total-records-on-page' in record['safe_headers']:
                record['safe_headers']['total-records-on-page'] = str(len(rows))
        body = _canonical(rows)
    elif name.startswith('model_'):
        value = record['sdk_value']
        if name == 'model_malformed_output':
            value.update(outcome='returned', returned_text='{', received_text='{', response_observed=True)
            record['failure'] = None
        else:
            value.update(outcome='raised', returned_text=None, received_text=None, response_observed=False)
            record['failure'] = ({'type':'RuntimeError','reason':'model_response_refused'} if name == 'model_refusal'
                                 else {'type':'ModelDeadlineExceeded','reason':'model_deadline_exhausted'})
    # A semantic case must reach its intended validator, not the earlier byte
    # limit. Keep the parent immutable and do not materialize an oversized case.
    if body is not None and len(body) > MAX_EDGE_BODY:
        return {**case, 'status':'unexercised', 'reason':'mutation_exceeds_body_bound',
                'mutated_bytes':len(body), 'maximum_bytes':MAX_EDGE_BODY,
                'parent_tape_sha256':plan['parent_tape_sha256']}
    record.update(lane='controlled_mutation', representation='controlled_mutation',
                  parent={'event_id':original['event_id'],'event_sha256':original['record_sha256'],
                          'tape_sha256':plan['parent_tape_sha256'],'label':name})
    destination.mkdir(mode=0o700)
    selected = destination / 'tape'
    selected.mkdir(mode=0o700)
    for directory in ('events','bodies','reservations'):
        (selected/directory).mkdir(mode=0o700)
    _write(selected/'identity.json', plan['run_identity'])
    reservation = {key:original[key] for key in (*KEY_FIELDS,'occurrence','event_id','observed_at','representation','lane')}
    _write(selected/'reservations'/(record['event_id']+'.json'), reservation)
    if body is not None:
        relative = 'bodies/' + record['event_id'] + '.bin'
        _write(selected/relative, body, binary=True)
        record.update(response_file=relative, response_sha256=hashlib.sha256(body).hexdigest(), response_bytes=len(body))
    else:
        record.update(response_file=None, response_sha256=None, response_bytes=0)
    record.pop('record_sha256')
    record['record_sha256'] = _digest(record)
    if name != 'interrupted_reservation':
        _write(selected/'events'/(record['event_id']+'.json'), record)
    spec = {**case, **details, 'lane':'controlled_mutation', 'tape_dir':str(selected),
            'parent_tape_sha256':plan['parent_tape_sha256'], 'run_identity':plan['run_identity'],
            'public_request':original['public_request'], 'boundary':original['boundary'],
            'operation':original['operation'], 'observed_at':original['observed_at'],
            'selected_prefix_occurrences_excluded':original['occurrence']}
    _write(destination/'case.json', spec)
    return spec


def require_network_denied():
    # A receipt alone cannot establish this process is in the admitted namespace.
    _require(all(name == 'lo' for _, name in socket.if_nameindex()), 'kernel_network_namespace_required')
    _require(Path('/proc/self/ns/net').exists(), 'kernel_network_namespace_unavailable')
    return os.readlink('/proc/self/ns/net')


def _seed_cursor(spec):
    cursor = Path(spec['tape_dir']).parent/'cursor'
    cursor.mkdir(mode=0o700)
    key = {name:spec[name] for name in ('run_identity','phase','boundary','operation','public_request','request_sha256')}
    key['schema_version'] = 1
    # This is an explicitly selected component occurrence, not an assertion that
    # earlier native calls were replayed. Preserve its original number and key.
    _write(cursor/(_digest(key)+'.counter'), str(spec['occurrence']).encode(), binary=True)
    os.environ['EVENTEDGE_LIVE_REPLAY_STATE'] = str(cursor)


def _forbid_native_network():
    attempts = []
    def forbidden(*args, **kwargs):
        attempts.append('unexpected_native_io')
        raise EdgeError('unexpected_native_io')
    for name in ('create_connection','getaddrinfo','gethostbyname'):
        setattr(socket, name, forbidden)
    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    subprocess.Popen = forbidden
    return attempts


def _source_request(spec):
    import requests
    request = spec['public_request']
    arguments = {'headers':request['headers'], 'timeout':5, 'stream':True, 'allow_redirects':False}
    body = request['body']
    if isinstance(body, dict) or (isinstance(body, list) and request['headers'].get('content-type','').split(';',1)[0] == 'application/json'):
        arguments['json'] = body
    elif isinstance(body, list):
        arguments['data'] = [(key, value) for key, value in body]
    session = requests.Session()
    session.trust_env = False
    try:
        with session.request(request['method'], request['url'], **arguments) as response:
            if spec['case'] in ('source_403','source_429'):
                response.raise_for_status()
            elif 'json' in spec['case']:
                response.json()
            else:
                response.content
    finally:
        session.close()


def _finra_request(spec):
    from tradingagents.strategies.data_sources import finra_query
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    from tradingagents.strategies.data_sources.finra_schedule import validate_current_settlement
    request = spec['public_request']['body']
    symbols = request['domainFilters'][0]['values']
    settlement = request['compareFilters'][0]['fieldValue']
    acquired = datetime.fromisoformat(spec['observed_at'])
    reference = date.fromisoformat(spec['run_identity']['reference_session'])
    expected = validate_current_settlement(None, acquired_at=acquired, as_of=reference)
    if expected['required_settlement_date'] != settlement:
        return {'status':'unexercised','reason':'captured_clock_context_cannot_be_reconstructed'}
    attempt = finra_query.new_attempt()
    try:
        with provider_budget('openbb', time.monotonic()+30, max_attempts=1):
            values, errors = finra_query.acquire(symbols, attempt, acquired_at=acquired, as_of=reference)
    except Exception as error:
        return {'exception_type':type(error).__name__, 'reason_code':getattr(error,'reason_code',None),
                'http_status':getattr(error,'http_status',None), 'current_complete':attempt.get('query_coverage',{}).get('current_complete')}
    return {'returned':True, 'accepted_symbols':sorted(values), 'missing_symbols':attempt['query_coverage']['missing_symbols'],
            'current_complete':attempt['query_coverage']['current_complete'], 'error_symbols':sorted(errors)}


def _model_request(spec):
    from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient
    from tradingagents.strategies import llm_utils, runtime_deadline
    from . import recorded_models as models
    request = spec['public_request']
    client = ChatGPTPlanClient(auth_path='/nonexistent/edge-replay-no-auth', profile_identity=request['client_profile_identity'])
    arguments = {key:request[key] for key in ('model','system','prompt','max_tokens','temperature','effort',
        'max_call_seconds','regulation_graphics_model_policy')}
    arguments.update(provenance={}, document_inputs=None)
    received = []
    arguments['response_observer'] = received.append
    token = models._ROLE.set(request['role'])
    try:
        with runtime_deadline.model_budget(time.monotonic()+30):
            result = llm_utils.call_analysis_model(client, **arguments)
        from tradingagents.strategies.learning.llm_analyzer import _parse_json_response
        return {'returned':True, 'native_parser_empty':_parse_json_response(result) == {}, 'observer_calls':len(received),
                'parser':'tradingagents.strategies.learning.llm_analyzer._parse_json_response',
                'role_specific_parser_coverage':False}
    except Exception as error:
        return {'exception_type':type(error).__name__, 'observer_calls':len(received)}
    finally:
        models._ROLE.reset(token)


def _execute_case(spec):
    """Called in a fresh admitted child. No application state is initialized."""
    _seed_cursor(spec)
    arguments = dict(mode='offline_replay', tape_dir=spec['tape_dir'], run_identity=spec['run_identity'], phase=spec['phase'])
    name = spec['case']
    if name == 'wrong_run_identity':
        arguments['run_identity'] = {**arguments['run_identity'], 'candidate_commit':'0'*40}
        try:
            tape.configure_tape(**arguments)
        except tape.TapeError:
            return {'status':'pass','observed':'wrong_run_identity_rejected'}
        return {'status':'fail','reason':'wrong_run_identity_accepted'}
    tape.configure_tape(**arguments)
    if name in ('wrong_request_identity','interrupted_reservation'):
        request = deepcopy(spec['public_request'])
        if name == 'wrong_request_identity':
            if spec['boundary'] == 'model_helper':
                request['prompt'] += '\ncontrolled different request'
            else:
                request['url'] += ('&' if '?' in request['url'] else '?') + 'controlled_different_request=1'
        try:
            tape.replay_event(boundary=spec['boundary'], operation=spec['operation'], public_request=request)
        except tape.TapeError:
            try:
                tape.assert_replay_complete()
            except tape.TapeError:
                return {'status':'pass','observed':'exact_replay_rejected_and_audited'}
        return {'status':'fail','reason':'invalid_replay_accepted'}
    tape.install_source_io(**arguments)
    if name.startswith('model_'):
        from .recorded_models import install_model_io
        install_model_io(**arguments)
    attempts = _forbid_native_network()
    try:
        if spec['level'] == 'native_finra_adapter':
            observed = _finra_request(spec)
        elif name.startswith('model_'):
            observed = _model_request(spec)
        else:
            try:
                _source_request(spec)
                observed = {'returned':True}
            except Exception as error:
                observed = {'exception_type':type(error).__name__,
                            'http_status':getattr(getattr(error,'response',None),'status_code',None)}
        if observed.get('status') == 'unexercised':
            return observed
        tape.assert_replay_complete()
        _require(not attempts, 'unexpected_native_io')
    except tape.TapeError:
        return {'status':'fail','reason':'exact_replay_not_complete'}
    if name == 'finra_parent_control':
        if observed.get('returned') is not True or observed.get('current_complete') is not True:
            return {'status':'unexercised','reason':'parent_finra_not_accepted','observed':observed}
        passed = True
    elif name in ('source_403','source_429'):
        passed = observed.get('http_status') == int(name.rsplit('_',1)[1]) and not observed.get('returned')
    elif name == 'source_timeout':
        passed = observed.get('reason_code') == 'timeout' if spec['level'] == 'native_finra_adapter' else observed.get('exception_type') == 'ReadTimeout'
    elif name == 'finra_missing_symbol':
        passed = observed.get('returned') is True and observed.get('current_complete') is False and spec['expected_missing_symbol'] in observed.get('missing_symbols', [])
    elif name.startswith('finra_') or (name.startswith('source_') and spec['level'] == 'native_finra_adapter'):
        passed = observed.get('reason_code') == 'invalid_response' and observed.get('current_complete') is False
    elif name.startswith('source_'):
        passed = observed.get('exception_type') == 'JSONDecodeError'
    elif name == 'model_timeout':
        passed = observed.get('exception_type') == 'ModelDeadlineExceeded' and observed.get('observer_calls') == 0
    elif name == 'model_refusal':
        passed = observed.get('exception_type') == 'RuntimeError' and observed.get('observer_calls') == 0
    else:
        passed = observed.get('returned') is True and observed.get('native_parser_empty') is True and observed.get('observer_calls') == 1
    return {'status':'pass' if passed else 'fail', 'observed':observed}


def _worker(spec_path):
    spec_path = _path(spec_path)
    spec = json.loads(spec_path.read_bytes())
    result_path = spec_path.parent/'result.json'
    from .runner import require_boundary
    require_boundary(Path(spec['campaign_root']),spec['boundary_receipt'],offline=True)
    namespace = require_network_denied()
    try:
        outcome = _execute_case(spec)
        from .bootstrap import _imports
        _imports(Path(spec['candidate_repo']))
    except Exception as error:
        outcome = {'status':'fail','reason':'case_worker_exception','exception_type':type(error).__name__}
    _write(result_path, {**outcome,'case':spec['case'],'level':spec['level'],
        'parent_record_sha256':spec['parent_record_sha256'],'kernel_network_namespace':namespace,
        'whole_workflow_coverage':False})


def run_edges(*, root, boundary_receipt, destination=None):
    """Run after native completion, inside the separately admitted offline sandbox."""
    from .runner import require_boundary, verify_contract
    root = _path(root)
    boundary_receipt = _path(boundary_receipt)
    boundary_digest = require_boundary(root,boundary_receipt,offline=True)
    namespace = require_network_denied()
    contract, _ = verify_contract(root)
    tooling = {name:_sha(Path(__file__).with_name(name)) for name in ('edges.py','recorded_io.py','recorded_models.py','transport_safety.py','runner.py','bootstrap.py')}
    _require((root/'native-report.json').is_file() and (root/'native-attempt.json').is_file(), 'native_attempt_must_finish_first')
    destination = _path(destination or root/'edge-cases')
    _require(destination.parent == root and not destination.exists(), 'new_campaign_child_destination_required')
    plan = plan_edges(root/'tape')
    expected_identity = json.loads((root/'hook-config.json').read_bytes())['run_identity']
    _require(plan['run_identity'] == expected_identity, 'parent_tape_contract_identity')
    destination.mkdir(mode=0o700)
    _write(destination/'plan.json', plan)
    tool_root = Path(__file__).resolve().parents[2]
    python = root/'repo/.venv/bin/python'
    results = []
    finra_control = None
    for case in plan['cases']:
        if case['status'] == 'unexercised':
            results.append(case)
            continue
        semantic = ((case['case'].startswith('finra_') and case['case'] != 'finra_parent_control')
                    or case['case'] in ('source_malformed_json','source_partial_json'))
        if semantic and case['level'] == 'native_finra_adapter' and finra_control != 'pass':
            results.append({**case,'status':'unexercised','reason':'parent_finra_control_not_passed'})
            continue
        directory = destination/case['case']
        spec = materialize_case(root/'tape',directory,plan,case)
        if spec['status'] == 'unexercised':
            results.append(spec)
            continue
        spec.update(campaign_root=str(root),candidate_repo=str(root/'repo'),boundary_receipt=str(boundary_receipt))
        # The immutable selection description and executable context are separate.
        _write(directory/'worker.json', spec)
        launch = 'import sys;sys.path[:0]='+repr([str(root/'repo'),str(tool_root),contract['dependency_site']])+';from tools.live_workflow.edges import _worker;_worker(sys.argv[1])'
        environment = {'PATH':'/usr/bin:/bin','HOME':str(directory),'TMPDIR':str(directory),
                       'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1'}
        try:
            child = subprocess.run([str(python),'-I','-S','-B','-c',launch,str(directory/'worker.json')],
                                   env=environment,cwd=root/'repo',capture_output=True,timeout=60)
            result = json.loads((directory/'result.json').read_bytes()) if child.returncode == 0 and (directory/'result.json').is_file() else {'status':'fail','reason':'worker_process_failed','returncode':child.returncode}
        except subprocess.TimeoutExpired:
            result = {'status':'fail','reason':'worker_deadline_exceeded'}
        results.append({**case,**result})
        if case['case'] == 'finra_parent_control':
            finra_control = result['status']
    _, _, after = _snapshot(root/'tape')
    _require(after == plan['parent_tape_sha256'], 'immutable_parent_changed')
    verify_contract(root)
    _require(tooling == {name:_sha(Path(__file__).with_name(name)) for name in tooling}, 'edge_tooling_changed')
    matrix = {**plan,'cases':results,'boundary_receipt_sha256':boundary_digest,
              'kernel_network_namespace':namespace,'immutable_parent_preserved':True,'edge_tooling_sha256':tooling,
              'counts':{status:sum(c['status']==status for c in results) for status in ('pass','fail','unexercised')}}
    _write(destination/'matrix.json',matrix)
    return matrix


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True)
    parser.add_argument('--boundary-receipt',required=True)
    parser.add_argument('--destination')
    arguments = parser.parse_args(argv)
    result = run_edges(root=arguments.root,boundary_receipt=arguments.boundary_receipt,destination=arguments.destination)
    print(json.dumps({'counts':result['counts'],'whole_workflow_coverage':False,'immutable_parent_preserved':result['immutable_parent_preserved']}))
    return int(result['counts']['fail'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
