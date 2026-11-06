"""Immutable local attempts, exact report pairs, and receipt-only observation."""
import json

import pytest

pytestmark = pytest.mark.scheduled_acceptance

from tradingagents.strategies.orchestration.operational_report import (
    build_operational_report, write_operational_report,
)
from tradingagents.strategies.orchestration.run_evidence import persist_run_evidence
from tradingagents.strategies.orchestration.source_notifications import DeliveryReceipt

GENERATION = 'gen_020'
COMMIT = 'a' * 40
CONFIG = 'b' * 64
SESSION = '2026-10-06'


@pytest.fixture
def local_repo(tmp_path):
    state = tmp_path / 'data/generations' / GENERATION
    state.mkdir(parents=True)
    (state.parent / 'manifest.json').write_text(json.dumps({'generations': [{
        'gen_id': GENERATION, 'git_commit': COMMIT, 'config_sha256': CONFIG,
        'state_dir': str(state), 'run_history': [], 'status': 'active',
    }]}))
    return tmp_path


def record(repo, *, minute=1, success=False, mode='screen', commit=COMMIT,
           config=CONFIG, native=True, process_status='completed', return_code=None,
           session=SESSION, runtime_failure=None):
    result = {'success': success, 'preflight_mode': mode, 'screen_ok': success,
              'screen_failure_count': 0 if success else 1, 'config_sha256': config,
              'screen_source_failures': []}
    if native:
        result['screen_runtime_failures'] = [] if success else [runtime_failure or
            {'phase': 'acquisition', 'reason_code': 'prospective_reference_invalid'}]
    return persist_run_evidence(repo, {
        'schema_version': 1, 'generation_id': GENERATION, 'generation_commit': commit,
        'requested_session': session, 'action': 'preflight', 'preflight_mode': mode,
        'started_at': f'{session}T20:{minute:02d}:00+00:00',
        'finished_at': f'{session}T20:{minute:02d}:01+00:00',
        'process_return_code': (0 if success else 1) if return_code is None else return_code,
        'process_status': process_status, 'stdout': '',
        'stderr': 'PRIVATE https://secret.invalid/provider HTTP 403', 'result': result,
    })


def publish(repo):
    report = build_operational_report(repo, GENERATION, SESSION, snapshot_guaranteed=True)
    paths = write_operational_report(report, repo / 'reports')
    return report, paths


def consume(repo, events, *, receipt='confirmed', **kwargs):
    from tradingagents.strategies.orchestration.source_notifications import consume_preflight_notifications
    def send(event):
        events.append(event)
        return DeliveryReceipt(receipt, f'local-{len(events)}' if receipt == 'confirmed' else None)
    return consume_preflight_notifications(repo / 'reports', repo / 'receipts/preflight.json',
        expected_generation=GENERATION, expected_commit=COMMIT, expected_session=SESSION,
        expected_configuration=CONFIG, expected_mode='screen', recipient_route='local:acceptance',
        send=send, test=True, **kwargs)


def test_failed_attempt_is_actionable_deduplicated_and_resolves_once(local_repo):
    failure = record(local_repo)
    original = failure.read_bytes()
    report, paths = publish(local_repo)
    observation = report['preflight_runtime_observations'][0]
    assert observation == {
        'attempt_id': failure.name, 'generation_id': GENERATION, 'generation_commit': COMMIT,
        'configuration': CONFIG, 'session': SESSION, 'preflight_mode': 'screen',
        'phase': 'acquisition', 'reason_code': 'prospective_reference_invalid',
        'status': 'open', 'resolved_by': None,
    }
    assert report['preflight_incidents'][0]['failures'] == []
    assert report['preflight_incidents'][0]['runtime_failures'] == [
        {'phase': 'acquisition', 'reason_code': 'prospective_reference_invalid'}]
    assert report['source_incidents']['open_incidents'] == []
    markdown = paths['markdown'].read_text()
    for fact in ('acquisition', 'prospective_reference_invalid', GENERATION, SESSION, 'screen', CONFIG):
        assert fact in markdown
    assert 'PRIVATE' not in markdown
    events = []
    assert len(consume(local_repo, events)['delivered']) == 1
    assert consume(local_repo, events)['delivered'] == []
    assert [event['kind'] for event in events] == ['preflight_failed']
    success = record(local_repo, minute=2, success=True)
    resolved, paths = publish(local_repo)
    assert resolved['preflight_runtime_observations'][0]['status'] == 'resolved'
    assert resolved['preflight_runtime_observations'][0]['resolved_by'] == success.name
    assert failure.name in paths['markdown'].read_text()
    assert len(consume(local_repo, events)['delivered']) == 1
    assert consume(local_repo, events)['delivered'] == []
    assert [event['kind'] for event in events] == ['preflight_failed', 'preflight_recovered']
    assert failure.read_bytes() == original
    assert all('PRIVATE' not in event['text'] and 'provider' not in event for event in events)


def test_actual_clock_exception_survives_wire_attempt_report_and_receipt(local_repo):
    from types import SimpleNamespace
    from tradingagents.strategies.orchestration.preflight import run_preflight
    from tradingagents.strategies.orchestration.generation_manager import (
        normalize_preflight_report, _preflight_subprocess_result,
    )

    def fail_acquisition(*args, **kwargs):
        raise ValueError('prospective reference is not the latest completed session')

    probe = run_preflight({}, SESSION, mode='screen', engine=SimpleNamespace(
        _fetch_all_data=fail_acquisition, config={}, ar_config={}))
    wire = normalize_preflight_report(probe, mode='screen', trading_date=SESSION)
    result = _preflight_subprocess_result(stdout=json.dumps(wire, indent=2), stderr='',
        returncode=1, elapsed=1, mode='screen', trading_date=SESSION)
    result['config_sha256'] = CONFIG
    failure = persist_run_evidence(local_repo, {
        'schema_version': 1, 'generation_id': GENERATION, 'generation_commit': COMMIT,
        'requested_session': SESSION, 'action': 'preflight', 'preflight_mode': 'screen',
        'started_at': f'{SESSION}T15:45:00+00:00', 'finished_at': f'{SESSION}T15:45:01+00:00',
        'process_return_code': 1, 'process_status': 'completed',
        'stdout': json.dumps(wire, indent=2), 'stderr': '', 'result': result,
    })
    original = failure.read_bytes()
    report, paths = publish(local_repo)
    observation = report['preflight_runtime_observations'][0]
    assert observation['reason_code'] == 'prospective_reference_invalid'
    assert observation['phase'] == 'acquisition'
    assert report['source_incidents']['open_incidents'] == []
    assert 'prospective_reference_invalid' in paths['markdown'].read_text()
    assert 'malformed preflight report' not in paths['markdown'].read_text()
    events = []
    assert len(consume(local_repo, events)['delivered']) == 1
    assert consume(local_repo, events)['delivered'] == []
    assert events[0]['observation']['attempt_id'] == failure.name
    assert events[0]['observation']['reason_code'] == 'prospective_reference_invalid'
    assert failure.read_bytes() == original


@pytest.mark.parametrize('failure', [
    {'phase': 'acquisition', 'reason_code': 'acquisition_failed'},
    {'phase': 'screen', 'reason_code': 'screen_runtime_error'},
])
def test_other_fixed_runtime_causes_remain_separate_from_provider_incidents(local_repo, failure):
    record(local_repo, runtime_failure=failure)
    report, paths = publish(local_repo)
    assert report['preflight_runtime_observations'][0]['phase'] == failure['phase']
    assert report['preflight_runtime_observations'][0]['reason_code'] == failure['reason_code']
    assert report['source_incidents']['open_incidents'] == []
    events = []
    consume(local_repo, events)
    assert len(events) == 1
    assert failure['reason_code'] in events[0]['text']
    assert 'PRIVATE' not in paths['markdown'].read_text()


@pytest.mark.parametrize('change', [
    {'mode': 'governed'}, {'mode': 'all'}, {'commit': 'c' * 40}, {'config': 'd' * 64},
    {'native': False}, {'config': None}, {'process_status': 'timeout'}, {'return_code': 1},
    {'minute': 0}, {'session': '2026-10-07'},
])
def test_unrelated_or_unproven_success_cannot_resolve(local_repo, change):
    record(local_repo)
    record(local_repo, **dict({'minute': 2, 'success': True}, **change))
    report, _ = publish(local_repo)
    assert report['preflight_runtime_observations'][0]['status'] == 'open'
    events = []
    consume(local_repo, events)
    assert [event['kind'] for event in events] == ['preflight_failed']


def test_missing_report_cannot_clear_failed_receipt_and_unknown_never_resends(local_repo):
    record(local_repo)
    _, paths = publish(local_repo)
    events = []
    first = consume(local_repo, events, receipt='unknown')
    assert len(first['unknown']) == 1
    assert consume(local_repo, events)['delivered'] == []
    assert len(events) == 1
    paths['json'].unlink()
    missing = consume(local_repo, events)
    assert missing['report_status'] == 'missing'
    assert all(event['kind'] != 'preflight_recovered' for event in events)
    record(local_repo, minute=2, success=True)
    publish(local_repo)
    consume(local_repo, events)
    consume(local_repo, events)
    assert [event['kind'] for event in events].count('preflight_failed') == 1
    assert [event['kind'] for event in events].count('preflight_recovered') == 1


def test_conflicting_success_source_diagnostics_cannot_resolve_runtime_failure(local_repo):
    record(local_repo)
    success = record(local_repo, minute=2, success=True)
    artifact = json.loads(success.read_text())
    artifact['result']['screen_source_failures'] = [
        {'source': 'edgar', 'reason_code': 'invalid_response', 'http_status': None,
         'attempts': 1, 'operation_count': 1}]
    success.write_text(json.dumps(artifact))
    report, _ = publish(local_repo)
    assert report['preflight_runtime_observations'][0]['status'] == 'open'


def test_noncanonical_offset_cannot_forge_later_recovery(local_repo):
    record(local_repo)
    success = record(local_repo, minute=2, success=True)
    report, paths = publish(local_repo)
    attempt = next(row for row in report['attempts'] if row['attempt_id'] == success.name)
    # This is lexically later but chronologically before the failed attempt.
    attempt.update(started_at=f'{SESSION}T21:00:00+02:00',
                   finished_at=f'{SESSION}T21:01:00+02:00')
    from tradingagents.strategies.orchestration.operational_report import render_operational_report
    paths['json'].write_text(json.dumps(report))
    paths['markdown'].write_text(render_operational_report(report))
    events = []
    result = consume(local_repo, events)
    assert result['report_status'] == 'invalid'
    assert all(event['kind'] != 'preflight_recovered' for event in events)


@pytest.mark.parametrize('changed', [
    {'expected_configuration': 'd' * 64}, {'expected_mode': 'all'},
    {'recipient_route': 'local:other'}, {'test': False},
])
def test_changed_receipt_scope_cannot_reuse_state(local_repo, changed):
    from tradingagents.strategies.orchestration.source_notifications import consume_preflight_notifications
    record(local_repo)
    publish(local_repo)
    events = []
    consume(local_repo, events)
    args = dict(expected_generation=GENERATION, expected_commit=COMMIT, expected_session=SESSION,
        expected_configuration=CONFIG, expected_mode='screen', recipient_route='local:acceptance',
        send=lambda event: pytest.fail('incompatible receipt scope delivered'), test=True)
    args.update(changed)
    result = consume_preflight_notifications(local_repo / 'reports', local_repo / 'receipts/preflight.json', **args)
    assert result['state_status'] == 'identity_mismatch'


def test_transport_cannot_mutate_retained_observation_receipt(local_repo):
    from tradingagents.strategies.orchestration.source_notifications import consume_preflight_notifications
    record(local_repo)
    publish(local_repo)
    def send(event):
        event['observation']['reason_code'] = 'PRIVATE'
        return DeliveryReceipt('confirmed', 'local-receipt')
    args = dict(expected_generation=GENERATION, expected_commit=COMMIT, expected_session=SESSION,
        expected_configuration=CONFIG, expected_mode='screen', recipient_route='local:acceptance',
        send=send, test=True)
    first = consume_preflight_notifications(local_repo / 'reports', local_repo / 'receipts/preflight.json', **args)
    second = consume_preflight_notifications(local_repo / 'reports', local_repo / 'receipts/preflight.json', **args)
    assert len(first['delivered']) == 1
    assert second['state_status'] == 'available'
    assert second['delivered'] == []


@pytest.mark.parametrize('damage', ['commit', 'config', 'mode', 'markdown', 'runtime_reason'])
def test_preflight_receipt_consumer_rejects_incompatible_or_invalid_reports(local_repo, damage):
    record(local_repo)
    report, paths = publish(local_repo)
    if damage == 'commit': report['generation_commit'] = 'c' * 40
    elif damage == 'config': report['preflight_runtime_observations'][0]['configuration'] = 'd' * 64
    elif damage == 'mode': report['preflight_runtime_observations'][0]['preflight_mode'] = 'governed'
    elif damage == 'runtime_reason': report['preflight_runtime_observations'][0]['reason_code'] = 'PRIVATE'
    if damage != 'markdown':
        from tradingagents.strategies.orchestration.operational_report import render_operational_report
        paths['json'].write_text(json.dumps(report))
        paths['markdown'].write_text(render_operational_report(report))
    else:
        paths['markdown'].write_text('PRIVATE')
    events = []
    result = consume(local_repo, events)
    assert result['report_status'] != 'available'
    assert all(event['kind'] not in {'preflight_failed', 'preflight_recovered'} for event in events)
    assert 'PRIVATE' not in json.dumps(events)
