"""Cold workflow admission and outcome tests; no credentials or external IO."""
from copy import deepcopy

import pytest

from tools.live_workflow import runner


def test_replay_usage_accepts_bounded_lookup_identity_without_request_contents(tmp_path):
    from tools.live_workflow.cold_replay import replay_usage
    lookup={'boundary':'source_http','operation':'requests.send',
        'request_sha256':'a'*64,'occurrence':1,'event_id':'b'*64}
    value={'phase':'daily','reason':'exact_lookup_failed','lookup':lookup}
    runner.private_json(tmp_path/'failure-test.json',value)
    assert replay_usage(tmp_path,[])==(set(),[value])


@pytest.mark.parametrize('change',[
    {'request_sha256':'raw-private-url'}, {'occurrence':True}, {'occurrence':-1},
    {'event_id':'invalid'}, {'raw_prompt':'private prompt'}, {'operation':'private\ntext'}])
def test_replay_usage_rejects_unbounded_or_invalid_lookup_receipts(tmp_path,change):
    from tools.live_workflow.cold_replay import replay_usage
    lookup={'boundary':'model_helper','operation':'call_analysis_model',
        'request_sha256':'a'*64,'occurrence':0,'event_id':'b'*64,**change}
    runner.private_json(tmp_path/'failure-test.json',{'phase':'daily','reason':'exact_lookup_failed','lookup':lookup})
    with pytest.raises(runner.AcceptanceError,match='failure_record_invalid'):
        replay_usage(tmp_path,[])


def test_cold_replay_cannot_pass_when_application_swallows_missing_request():
    from tools.live_workflow.cold_replay import evaluate_outcome
    checks = {name: True for name in ('sixteen_books', 'accounting', 'staging_complete',
        'staging_valid', 'evidence_complete', 'source_completeness')}
    checks['outcome'] = 'clean'
    result = evaluate_outcome(case='baseline', process_success=True, checks=checks,
        reproduction={'canonical_json_identical': True, 'canonical_markdown_identical': True},
        replay_failures=[{'phase': 'daily', 'reason': 'exact_lookup_failed'}],
        consumed={'a'}, expected={'a'}, target_event=None)
    assert result['status'] == 'blocked'
    assert result['reason'] == 'missing_or_incompatible_exact_capture'
    assert result['whole_workflow_coverage'] is False


def test_cold_baseline_requires_all_recordings_and_native_checks():
    from tools.live_workflow.cold_replay import evaluate_outcome
    kwargs = dict(case='baseline', process_success=True,
        checks={**{name: True for name in ('sixteen_books', 'accounting', 'staging_complete',
            'staging_valid', 'evidence_complete', 'source_completeness')}, 'outcome': 'clean'},
        reproduction={'canonical_json_identical': True, 'canonical_markdown_identical': True},
        replay_failures=[], consumed={'a', 'b'}, expected={'a', 'b'}, target_event=None)
    assert evaluate_outcome(**kwargs)['status'] == 'pass'
    for change in ({'consumed': {'a'}}, {'process_success': False},
                   {'checks': {**kwargs['checks'], 'accounting': False}}):
        assert evaluate_outcome(**(kwargs | change))['status'] != 'pass'


def test_mutation_never_passes_without_reaching_selected_occurrence():
    from tools.live_workflow.cold_replay import evaluate_outcome
    result = evaluate_outcome(case='model_malformed', process_success=True, checks={},
        reproduction={}, replay_failures=[], consumed={'a'}, expected={'a', 'b'}, target_event='b')
    assert result['status'] == 'blocked'
    assert result['reason'] == 'selected_mutation_not_reached'


def test_fault_plan_preserves_exact_model_request_and_role():
    from tools.live_workflow.cold_replay import mutation_changes
    event = {'boundary': 'model_helper', 'operation': 'call_analysis_model',
        'public_request': {'role': 'committee', 'model': 'native-model', 'effort': 'high'},
        'sdk_value': {'outcome': 'returned', 'received_text': '{"action":"hold"}',
            'returned_text': '{"action":"hold"}', 'response_observed': True,
            'provenance': {'configured_model': 'native-model', 'reasoning_effort': 'high'}},
        'failure': None}
    before = deepcopy(event)
    changes = mutation_changes('model_malformed', event)
    assert changes['sdk_value']['received_text'] == changes['sdk_value']['returned_text'] == '{'
    assert changes['sdk_value']['provenance'] == event['sdk_value']['provenance']
    assert event == before
    with pytest.raises(runner.AcceptanceError):
        mutation_changes('source_http_401', event)


def test_source_mutation_keeps_body_while_changing_only_http_status():
    from tools.live_workflow.cold_replay import mutation_changes
    event = {'boundary': 'source_http', 'operation': 'requests.send', 'status': 200,
        'failure': None, 'response_file': 'bodies/real.bin', 'representation': 'sdk_decoded_http'}
    assert mutation_changes('source_http_401', event) == {'status': 401}
    assert mutation_changes('source_malformed_json', event) == {'response_body': b'{'}
    with pytest.raises(runner.AcceptanceError):
        mutation_changes('invented_case', event)


def test_source_fault_requires_native_governed_gate_and_zero_economic_effects():
    from tools.live_workflow.cold_replay import evaluate_outcome
    args = dict(case='source_http_401', process_success=False,
        checks={'source_completeness': False}, reproduction={}, replay_failures=[],
        consumed={'a'}, expected={'a', 'b'}, target_event='a', baseline_qualified=True,
        effects={'verified': True, 'economic_rows': 0}, governed_blocked=True,
        report_verified=True, source_fault_observed=True)
    assert evaluate_outcome(**args)['status'] == 'pass'
    for change in ({'governed_blocked': False}, {'baseline_qualified': False},
                   {'process_failure': 'outer_timeout'},
                   {'report_verified': False}, {'source_fault_observed': False},
                   {'effects': {'verified': True, 'economic_rows': 1}}):
        assert evaluate_outcome(**(args | change))['status'] != 'pass'


def test_corrupt_ledger_cannot_be_reported_as_zero_effects(tmp_path):
    from tools.live_workflow.cold_replay import economic_effects
    (tmp_path / 'portfolio.db').write_bytes(b'corrupt database')
    with pytest.raises(runner.AcceptanceError, match='database_header'):
        economic_effects(tmp_path)


def test_source_fault_attribution_requires_exact_native_governed_ticker(tmp_path):
    from tools.live_workflow.cold_replay import source_fault_observed
    import json
    folder = tmp_path / 'data/logs/run_attempts'
    folder.mkdir(parents=True)
    artifact = folder / 'attempt.json'
    value = {'schema_version': 1, 'action': 'preflight', 'preflight_mode': 'governed',
        'generation_id': 'gen_001', 'generation_commit': 'a' * 40, 'requested_session': '2026-10-30',
        'process_status': 'completed', 'process_return_code': 1,
        'result': {'config_sha256': 'b' * 64, 'preflight_mode': 'governed', 'success': False,
            'governed_ok': False, 'governed_probe_status': 'failed', 'state_status': 'uninitialized',
            'governed_failure_map': {'SPY': 'invalid SPY/2026-10-30'}}}
    artifact.write_text(json.dumps(value))
    pins = dict(repo=tmp_path, generation='gen_001', commit='a' * 40, profile_sha256='b' * 64)
    event = {'phase': 'preflight_governed', 'public_request': {
        'url': 'https://data.alpaca.markets/v2/stocks/bars?symbols=SPY%2CBIL'}}
    report = {'attempts': [{'action': 'preflight', 'preflight_mode': 'governed', 'evidence_path': str(artifact)}]}
    assert source_fault_observed(report, event, '2026-10-30', **pins)
    assert not source_fault_observed(report, event, '2026-10-29', **pins)
    assert not source_fault_observed({}, event, '2026-10-30', **pins)
    event['public_request']['url'] = 'https://data.alpaca.markets/v2/stocks/bars?symbols=VT'
    assert not source_fault_observed(report, event, '2026-10-30', **pins)
