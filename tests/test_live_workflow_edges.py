"""Constructed unit controls for the driver; these are not native campaign evidence."""
import importlib
import json
from pathlib import Path

import pytest

from tools.live_workflow import recorded_io as tape


def parent(tmp_path, *, finra=False, model=False, finra_padding=0):
    importlib.reload(tape)
    root = tmp_path / 'parent'
    identity = {'candidate_commit': 'a' * 40, 'reference_session': '2026-10-30', 'profile_sha256': 'b' * 64}
    tape.configure_tape(mode='live_record', tape_dir=root, run_identity=identity, phase='daily')
    if model:
        event = tape.reserve_event(boundary='model_helper', operation='call_analysis_model', public_request={
            'native_provider': 'chatgpt_plan', 'document_inputs': None, 'model': 'gpt-6-luna',
            'system': 'system', 'prompt': 'original actual-shaped prompt', 'max_tokens': 64,
            'temperature': 0, 'effort': 'high', 'max_call_seconds': None,
            'regulation_graphics_model_policy': None, 'role': 'thesis', 'client_profile_identity': 'public'})
        tape.finish_event(event, sdk_value={'received_text': '{"direction":"neutral"}',
            'returned_text': '{"direction":"neutral"}', 'outcome': 'returned', 'response_observed': True,
            'provenance': {'configured_model':'gpt-6-luna', 'reasoning_effort':'high'}})
    if finra:
        body = {'limit': 3, 'compareFilters': [{'fieldName':'settlementDate','fieldValue':'2026-10-15','compareType':'EQUAL'}],
                'domainFilters':[{'fieldName':'symbolCode','values':['A','B']}]}
        event = tape.reserve_event(boundary='http_decoded', operation='requests.send', public_request={
            'method':'POST','url':'https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest',
            'body':body,'headers':{'accept':'application/json','content-type':'application/json'}})
        path = tmp_path / 'body'
        rows = [{'symbolCode':symbol,'settlementDate':'2026-10-15',
            'accountingYearMonthNumber':20261015,'currentShortPositionQuantity':100,
            'averageDailyVolumeQuantity':10,'daysToCoverQuantity':10} for symbol in ['A','B']]
        if finra_padding:
            rows[0]['providerExtra'] = 'x' * finra_padding
        path.write_text(json.dumps(rows))
        tape.finish_event(event, response_file=path, status=200, safe_headers={'content-type':'application/json',
            'record-total':'2','record-offset':'0','record-limit':'3','total-records-on-page':'2'})
    return root


def test_empty_native_tape_exposes_unexercised_cases(tmp_path):
    from tools.live_workflow.edges import plan_edges
    result = plan_edges(parent(tmp_path))
    assert result['whole_workflow_coverage'] is False
    assert result['cases']
    assert all(case['status'] == 'unexercised' for case in result['cases'])


def test_plan_selects_exact_parent_and_excludes_missing_models(tmp_path):
    from tools.live_workflow.edges import plan_edges
    root = parent(tmp_path, finra=True)
    result = plan_edges(root)
    cases = {case['case']:case for case in result['cases']}
    assert cases['finra_duplicate_symbols']['status'] == 'planned'
    assert cases['finra_duplicate_symbols']['level'] == 'native_finra_adapter'
    assert cases['model_timeout']['status'] == 'unexercised'
    assert cases['source_403']['parent_record_sha256']
    assert cases['source_403']['occurrence'] == 0
    assert result['parent_tape_sha256']


def test_derivative_copies_one_small_event_preserves_parent_and_binding(tmp_path):
    from tools.live_workflow.edges import plan_edges, materialize_case
    root = parent(tmp_path, finra=True, model=True)
    before = {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    plan = plan_edges(root)
    case = next(c for c in plan['cases'] if c['case'] == 'finra_missing_symbol')
    result = materialize_case(root, tmp_path/'edge', plan, case)
    records = tape.verify_tape(tmp_path/'edge'/'tape')
    assert len(records) == 1
    record = records[0]
    assert record['event_id'] == case['parent_event_id']
    assert record['public_request'] == next(r for r in tape.verify_tape(root) if r['event_id'] == record['event_id'])['public_request']
    assert record['parent']['tape_sha256'] == plan['parent_tape_sha256']
    assert record['lane'] == 'controlled_mutation'
    rows = json.loads((tmp_path/'edge'/'tape'/record['response_file']).read_bytes())
    assert len(rows) == 1 and record['safe_headers']['record-total'] == '1'
    assert result['expected_missing_symbol'] == 'A'
    assert before == {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_incomplete_finra_headers_cannot_manufacture_native_semantic_cases(tmp_path):
    from tools.live_workflow.edges import plan_edges
    root = parent(tmp_path, finra=True)
    path = next((root/'events').glob('*.json'))
    record = json.loads(path.read_bytes())
    record['safe_headers'].pop('record-total')
    record.pop('record_sha256')
    record['record_sha256'] = tape._digest(tape._canonical(record))
    path.write_bytes(tape._canonical(record))
    cases = {c['case']:c for c in plan_edges(root)['cases']}
    assert cases['finra_duplicate_symbols']['status'] == 'unexercised'
    assert cases['source_403']['level'] == 'http_transport'


def test_model_mutations_keep_original_prompt_and_explicit_lane(tmp_path):
    from tools.live_workflow.edges import plan_edges, materialize_case
    root = parent(tmp_path, model=True)
    plan = plan_edges(root)
    case = next(c for c in plan['cases'] if c['case'] == 'model_refusal')
    materialize_case(root, tmp_path/'edge', plan, case)
    record = tape.verify_tape(tmp_path/'edge'/'tape')[0]
    assert record['public_request']['prompt'] == 'original actual-shaped prompt'
    assert record['failure']['reason'] == 'model_response_refused'
    assert record['sdk_value']['outcome'] == 'raised'


def test_network_namespace_admission_rejects_live_interfaces(monkeypatch):
    from tools.live_workflow.edges import require_network_denied, EdgeError
    import socket
    monkeypatch.setattr(socket,'if_nameindex',lambda:[(1,'lo'),(2,'eth0')])
    with pytest.raises(EdgeError, match='network_namespace'):
        require_network_denied()


@pytest.mark.parametrize('name', ['source_403','source_429','source_timeout','source_malformed_json',
                                  'source_partial_json','wrong_request_identity','wrong_run_identity','interrupted_reservation'])
def test_selected_case_executes_exact_offline_transport_or_binding(tmp_path, name):
    import subprocess
    import sys
    from tools.live_workflow.edges import plan_edges, materialize_case
    root = parent(tmp_path, finra=True)
    plan = plan_edges(root)
    case = next(c for c in plan['cases'] if c['case'] == name)
    spec = materialize_case(root,tmp_path/'edge',plan,case)
    # This unit control exercises transport/binding only; native FINRA semantics
    # require the separately supplied exact candidate, never a stand-in adapter.
    if name.startswith('source_'):
        spec['level'] = 'http_json_decoder' if 'json' in name else 'http_transport'
    executable = tmp_path/'unit-case.json'
    executable.write_text(json.dumps(spec))
    code = 'import json,sys;from tools.live_workflow.edges import _execute_case;spec=json.load(open(sys.argv[1]));print(json.dumps(_execute_case(spec)))'
    result = subprocess.run([sys.executable,'-B','-c',code,str(executable)],capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'pass', result.stdout


def test_parent_mutation_rejected_before_derivative(tmp_path):
    from tools.live_workflow.edges import plan_edges, materialize_case, EdgeError
    root = parent(tmp_path, model=True)
    plan = plan_edges(root)
    case = next(c for c in plan['cases'] if c['case']=='model_timeout')
    path = root/'events'/(case['parent_event_id']+'.json')
    record = json.loads(path.read_bytes())
    record['sdk_value']['received_text'] = 'changed'
    record.pop('record_sha256')
    record['record_sha256'] = tape._digest(tape._canonical(record))
    path.write_bytes(tape._canonical(record))
    with pytest.raises(EdgeError,match='selected_parent_changed'):
        materialize_case(root,tmp_path/'edge',plan,case)
    assert not (tmp_path/'edge').exists()


@pytest.mark.parametrize('name', ['finra_parent_control','source_403','source_429','source_timeout','source_malformed_json','source_partial_json',
    'finra_duplicate_symbols','finra_missing_symbol','finra_invalid_pagination','finra_stale_settlement','finra_future_settlement',
    'model_timeout','model_refusal','model_malformed_output'])
def test_exact_candidate_constructed_control(tmp_path, name):
    """Optional real adapter/helper integration; parent inputs here are constructed."""
    import os
    import subprocess
    import sys
    from tools.live_workflow.edges import plan_edges, materialize_case
    candidate = os.environ.get('EVENTEDGE_EDGE_TEST_CANDIDATE')
    if not candidate:
        pytest.skip('set exact candidate path for native adapter/helper integration controls')
    root = parent(tmp_path, finra=True, model=True)
    plan = plan_edges(root)
    case = next(c for c in plan['cases'] if c['case'] == name)
    spec = materialize_case(root,tmp_path/'edge',plan,case)
    executable = tmp_path/'unit-case.json'
    executable.write_text(json.dumps(spec))
    code = 'import json,sys;sys.path.insert(0,sys.argv[2]);from tools.live_workflow.edges import _execute_case;spec=json.load(open(sys.argv[1]));print(json.dumps(_execute_case(spec)))'
    result = subprocess.run([sys.executable,'-B','-c',code,str(executable),candidate],capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome['status'] == 'pass', result.stdout
    if name == 'model_malformed_output':
        assert outcome['observed']['parser'] == 'tradingagents.strategies.learning.llm_analyzer._parse_json_response'
        assert outcome['observed']['role_specific_parser_coverage'] is False


def test_campaign_rejects_tape_from_other_identity_before_execution(tmp_path, monkeypatch):
    from tools.live_workflow import edges, runner
    root = tmp_path/'campaign'
    root.mkdir()
    parent(root).rename(root/'tape')
    for name in ('native-report.json','native-attempt.json'):
        (root/name).write_text('{}')
    (root/'hook-config.json').write_text(json.dumps({'run_identity':{'candidate_commit':'wrong'}}))
    monkeypatch.setattr(runner,'require_boundary',lambda *args,**kwargs:'test-boundary')
    monkeypatch.setattr(runner,'verify_contract',lambda *args:({},{}))
    monkeypatch.setattr(edges,'require_network_denied',lambda:'test-only-no-execution')
    with pytest.raises(edges.EdgeError,match='parent_tape_contract_identity'):
        edges.run_edges(root=root,boundary_receipt=tmp_path/'unused')
    assert not (root/'edge-cases').exists()


def test_actual_unfinished_reservation_can_be_selected_without_finished_events(tmp_path):
    from tools.live_workflow.edges import plan_edges, materialize_case
    root = parent(tmp_path)
    event = tape.reserve_event(boundary='http_decoded', operation='requests.send', public_request={
        'method':'GET','url':'https://example.org/data','body':None,'headers':{}})
    plan = plan_edges(root)
    case = next(c for c in plan['cases'] if c['case']=='interrupted_reservation')
    assert case['status'] == 'planned'
    assert case['parent_kind'] == 'reservation'
    materialize_case(root,tmp_path/'edge',plan,case)
    assert tape.verify_tape(tmp_path/'edge'/'tape') == []
    assert (tmp_path/'edge'/'tape'/'reservations'/(event['event_id']+'.json')).is_file()


def test_selected_nonzero_occurrence_is_preserved_and_exercised(tmp_path):
    import subprocess
    import sys
    from tools.live_workflow.edges import plan_edges, materialize_case
    root = parent(tmp_path, finra=True)
    original = tape.verify_tape(root)[0]
    duplicate = tape.reserve_event(boundary=original['boundary'], operation=original['operation'], public_request=original['public_request'])
    second = tape.finish_event(duplicate,response_file=root/original['response_file'],status=200,safe_headers=original['safe_headers'])
    plan = plan_edges(root)
    case = next(c for c in plan['cases'] if c['case']=='source_403')
    case.update(parent_event_id=second['event_id'],parent_record_sha256=second['record_sha256'],occurrence=1)
    spec = materialize_case(root,tmp_path/'edge',plan,case)
    assert spec['selected_prefix_occurrences_excluded'] == 1
    assert tape.verify_tape(tmp_path/'edge'/'tape')[0]['occurrence'] == 1
    spec['level'] = 'http_transport'
    executable = tmp_path/'unit-case.json'
    executable.write_text(json.dumps(spec))
    code = 'import json,sys;from tools.live_workflow.edges import _execute_case;spec=json.load(open(sys.argv[1]));print(json.dumps(_execute_case(spec)))'
    result = subprocess.run([sys.executable,'-B','-c',code,str(executable)],capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'pass', result.stdout


def test_duplicate_derivative_over_body_bound_is_unexercised_without_writes(tmp_path):
    import hashlib
    from tools.live_workflow.edges import plan_edges, materialize_case, MAX_EDGE_BODY
    root = parent(tmp_path, finra=True)
    record_path = next((root/'events').glob('*.json'))
    record = json.loads(record_path.read_bytes())
    body_path = root/record['response_file']
    rows = json.loads(body_path.read_bytes())
    rows[0]['providerExtra'] = 'x' * (600 * 1024)
    encoded = json.dumps(rows).encode()
    assert len(encoded) < MAX_EDGE_BODY
    body_path.write_bytes(encoded)
    record.update(response_bytes=len(encoded),response_sha256=hashlib.sha256(encoded).hexdigest())
    record.pop('record_sha256')
    record['record_sha256'] = tape._digest(tape._canonical(record))
    record_path.write_bytes(tape._canonical(record))
    before = {str(path.relative_to(root)):path.read_bytes() for path in root.rglob('*') if path.is_file()}
    plan = plan_edges(root)
    case = next(case for case in plan['cases'] if case['case']=='finra_duplicate_symbols')
    assert case['status'] == 'planned'
    outcome = materialize_case(root,tmp_path/'edge',plan,case)
    assert outcome['status'] == 'unexercised'
    assert outcome['reason'] == 'mutation_exceeds_body_bound'
    assert outcome['mutated_bytes'] > outcome['maximum_bytes'] == MAX_EDGE_BODY
    assert not (tmp_path/'edge').exists()
    assert before == {str(path.relative_to(root)):path.read_bytes() for path in root.rglob('*') if path.is_file()}


def test_campaign_does_not_launch_oversized_semantic_mutation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tools.live_workflow import edges, runner
    root = tmp_path/'campaign'
    root.mkdir()
    parent(root,finra=True,finra_padding=600*1024).rename(root/'tape')
    identity = json.loads((root/'tape'/'identity.json').read_bytes())
    for name in ('native-report.json','native-attempt.json'):
        (root/name).write_text('{}')
    (root/'hook-config.json').write_text(json.dumps({'run_identity':identity}))
    monkeypatch.setattr(runner,'require_boundary',lambda *args,**kwargs:'test-boundary')
    monkeypatch.setattr(runner,'verify_contract',lambda *args:({'dependency_site':str(tmp_path)},{}))
    monkeypatch.setattr(edges,'require_network_denied',lambda:'test-only-no-execution')
    original_plan = edges.plan_edges
    def two_cases(path):
        plan = original_plan(path)
        plan['cases'] = [case for case in plan['cases'] if case['case'] in ('finra_parent_control','finra_duplicate_symbols')]
        return plan
    monkeypatch.setattr(edges,'plan_edges',two_cases)
    launches = []
    def controlled_worker(command, **kwargs):
        path = Path(command[-1])
        spec = json.loads(path.read_bytes())
        launches.append(spec['case'])
        assert spec['case'] == 'finra_parent_control'
        (path.parent/'result.json').write_text(json.dumps({'status':'pass'}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(edges.subprocess,'run',controlled_worker)
    matrix = edges.run_edges(root=root,boundary_receipt=tmp_path/'test-receipt')
    assert launches == ['finra_parent_control']
    assert matrix['counts'] == {'pass':1,'fail':0,'unexercised':1}
    duplicate = next(case for case in matrix['cases'] if case['case']=='finra_duplicate_symbols')
    assert duplicate['status'] == 'unexercised'
    assert duplicate['reason'] == 'mutation_exceeds_body_bound'
    assert not (root/'edge-cases'/'finra_duplicate_symbols').exists()
