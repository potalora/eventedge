"""Deterministic notification acceptance against local reports and a receipt sink."""
import json

import pytest


def publish(folder, session='2026-10-06', *, previous=None, healthy=False):
    from tradingagents.strategies.orchestration.source_incidents import assess_source_incidents
    from tradingagents.strategies.orchestration.operational_report import render_operational_report
    report = {'generation_id': 'gen_020', 'generation_commit': 'a' * 40, 'session': session,
              'evidence_complete': True, 'outcome': 'degraded', 'accounting_valid': True,
              'input_coverage_valid': False, 'staging_complete': False, 'staging_valid': False,
              'attempt_counts': {'daily': 1, 'preflight': 0}, 'cohorts': {}, 'attempts': [],
              'performance_claims_withheld': True, 'candidate_input_issues': [],
              'source_health_failures': [], 'preflight_incidents': [], 'diagnostics': [],
              'sources': {'configuration': 'cfg123', 'acquired_at': session + 'T22:00:00+00:00',
                          'observations': {'fred': 'success' if healthy else 'failed'},
                          'unresolved': [] if healthy else [{'provider': 'fred', 'reason_code': 'timeout'}],
                          'recovered': []}}
    report['source_incidents'] = assess_source_incidents(report, previous)
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f'{session}-gen_020-operational-report'
    stem.with_suffix('.json').write_text(json.dumps(report))
    stem.with_suffix('.md').write_text(render_operational_report(report))
    return report


def consume(folder, state, sink, session='2026-10-06', **kwargs):
    from tradingagents.strategies.orchestration.source_notifications import consume_source_notifications
    return consume_source_notifications(folder, state, expected_generation='gen_020',
        expected_commit='a' * 40, expected_session=session,
        recipient_route=kwargs.pop('recipient_route', 'discord:test-recipient'), send=sink,
        test=True, **kwargs)


def confirmed_sink(events):
    def send(event):
        from tradingagents.strategies.orchestration.source_notifications import DeliveryReceipt
        events.append(event)
        return DeliveryReceipt('confirmed', f'receipt-{len(events)}')
    return send


def test_three_sessions_escalate_and_recovery_deliver_once(tmp_path):
    folder, state, events = tmp_path / 'reports', tmp_path / 'state.json', []
    previous = None
    for session, healthy in [('2026-10-06', False), ('2026-10-07', False),
                             ('2026-10-08', False), ('2026-10-09', True)]:
        previous = publish(folder, session, previous=previous, healthy=healthy)
        first = consume(folder, state, confirmed_sink(events), session)
        second = consume(folder, state, confirmed_sink(events), session)
        assert first['report_status'] == second['report_status'] == 'available'
        assert second['delivered'] == []
    assert [event['kind'] for event in events] == ['opened', 'escalated', 'recovered']
    assert len({event['event_id'] for event in events}) == 3
    assert all(event['text'].startswith('[TEST]') for event in events)
    assert all(event['provider'] == 'fred' for event in events)
    assert all(event['event_id'] == event['alert_id'] for event in events)


@pytest.mark.parametrize('damage', ['missing', 'malformed', 'oversized', 'stale',
    'wrong_commit', 'wrong_policy', 'unavailable', 'markdown_missing', 'markdown_mismatch',
    'invalid_provider', 'invalid_alert_id', 'duplicate_alert'])
def test_invalid_report_alert_once_and_valid_return_recovers(tmp_path, damage):
    from tradingagents.strategies.orchestration.operational_report import render_operational_report
    folder, state, events = tmp_path / 'reports', tmp_path / 'state.json', []
    report = publish(folder)
    json_path = folder / '2026-10-06-gen_020-operational-report.json'
    markdown = json_path.with_suffix('.md')
    if damage == 'missing': json_path.unlink()
    elif damage == 'malformed': json_path.write_text('{PRIVATE exception response')
    elif damage == 'oversized': json_path.write_text('x' * (2 * 1024 * 1024 + 1))
    elif damage == 'markdown_missing': markdown.unlink()
    elif damage == 'markdown_mismatch': markdown.write_text('PRIVATE response')
    else:
        if damage == 'stale': report['session'] = '2026-10-05'
        elif damage == 'wrong_commit': report['generation_commit'] = 'b' * 40
        elif damage == 'wrong_policy': report['source_incidents']['policy'] = 'other'
        elif damage == 'unavailable': report['source_incidents']['assessment_status'] = 'unavailable'
        elif damage == 'invalid_provider': report['source_incidents']['alerts'][0]['provider'] = 'PRIVATE'
        elif damage == 'invalid_alert_id': report['source_incidents']['alerts'][0]['alert_id'] = 'PRIVATE'
        elif damage == 'duplicate_alert': report['source_incidents']['alerts'] *= 2
        json_path.write_text(json.dumps(report))
        markdown.write_text(render_operational_report(report))
    first = consume(folder, state, confirmed_sink(events))
    consume(folder, state, confirmed_sink(events))
    assert first['report_status'] != 'available'
    assert [event['kind'] for event in events] == ['report_unavailable']
    assert 'PRIVATE' not in json.dumps(events)
    publish(folder)
    consume(folder, state, confirmed_sink(events))
    consume(folder, state, confirmed_sink(events))
    assert [event['kind'] for event in events] == ['report_unavailable', 'report_recovered', 'opened']


def test_changed_recipient_cannot_reuse_acknowledgment(tmp_path):
    folder, state, events = tmp_path / 'reports', tmp_path / 'state.json', []
    publish(folder)
    consume(folder, state, confirmed_sink(events))
    result = consume(folder, state, confirmed_sink(events), recipient_route='discord:other')
    assert result['state_status'] == 'identity_mismatch'
    assert len(events) == 1
    assert 'discord:test-recipient' not in state.read_text()


def test_confirmed_requires_receipt_and_uncertain_delivery_never_auto_retries(tmp_path):
    from tradingagents.strategies.orchestration.source_notifications import DeliveryReceipt
    folder, state, events = tmp_path / 'reports', tmp_path / 'state.json', []
    publish(folder)
    def uncertain(event):
        events.append(event)
        return DeliveryReceipt('confirmed')
    first = consume(folder, state, uncertain)
    second = consume(folder, state, confirmed_sink(events))
    assert len(first['unknown']) == len(second['unknown']) == 1
    assert first['delivered'] == second['delivered'] == []
    assert len(events) == 1


def test_known_rejection_is_retryable(tmp_path):
    from tradingagents.strategies.orchestration.source_notifications import DeliveryReceipt
    folder, state, events = tmp_path / 'reports', tmp_path / 'state.json', []
    publish(folder)
    first = consume(folder, state, lambda event: DeliveryReceipt('rejected'))
    assert len(first['retryable']) == 1
    second = consume(folder, state, confirmed_sink(events))
    assert len(second['delivered']) == 1


def test_callback_exception_is_safe_and_does_not_resend(tmp_path):
    folder, state, events = tmp_path / 'reports', tmp_path / 'state.json', []
    publish(folder)
    def fails(event):
        events.append(event)
        raise RuntimeError('PRIVATE token provider response')
    first = consume(folder, state, fails)
    second = consume(folder, state, confirmed_sink(events))
    assert len(events) == len(first['unknown']) == len(second['unknown']) == 1
    assert 'PRIVATE' not in json.dumps(first) + state.read_text()


def test_interrupted_delivery_is_durable_unknown(tmp_path):
    folder, state, events = tmp_path / 'reports', tmp_path / 'state.json', []
    publish(folder)
    def interrupted(event):
        events.append(event)
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt): consume(folder, state, interrupted)
    result = consume(folder, state, confirmed_sink(events))
    assert len(events) == len(result['unknown']) == 1


def test_corrupt_state_fails_closed(tmp_path):
    folder, state, events = tmp_path / 'reports', tmp_path / 'state.json', []
    publish(folder)
    state.write_text('{broken')
    result = consume(folder, state, confirmed_sink(events))
    assert result['state_status'] == 'invalid'
    assert events == []


def test_old_sessions_cannot_replay_after_retention(tmp_path):
    folder, state, events = tmp_path / 'reports', tmp_path / 'state.json', []
    publish(folder, '2026-10-06')
    consume(folder, state, confirmed_sink(events), '2026-10-06')
    publish(folder, '2027-04-06')
    consume(folder, state, confirmed_sink(events), '2027-04-06')
    result = consume(folder, state, confirmed_sink(events), '2026-10-06')
    assert result['state_status'] == 'outside_retention'
    assert len(events) == 2


def test_drill_checks_five_confirmations_with_replay_suppression(tmp_path):
    from scripts.run_source_notification_drill import run_drill
    events = []
    result = run_drill(tmp_path / 'drill', send=confirmed_sink(events), recipient_route='local:sink')
    assert result['passed'] is True
    assert result['delivered_count'] == 5
    assert result['kinds'] == ['opened', 'escalated', 'recovered', 'report_unavailable', 'report_recovered']
    assert len(events) == 5
    assert all(step['replay']['delivered'] == [] for step in result['steps'])


def test_drill_is_not_passed_without_delivery_confirmation(tmp_path):
    from scripts.run_source_notification_drill import run_drill
    from tradingagents.strategies.orchestration.source_notifications import DeliveryReceipt
    result = run_drill(tmp_path / 'drill', send=lambda event: DeliveryReceipt('unknown'), recipient_route='local:sink')
    assert result['passed'] is False
    assert result['delivered_count'] == 0


def test_unsafe_persisted_event_fails_closed(tmp_path):
    from tradingagents.strategies.orchestration.source_notifications import DeliveryReceipt
    folder, state, events = tmp_path / 'reports', tmp_path / 'state.json', []
    publish(folder)
    consume(folder, state, lambda event: DeliveryReceipt('rejected'))
    value = json.loads(state.read_text())
    next(iter(value['events'].values()))['event']['text'] = 'PRIVATE raw response'
    state.write_text(json.dumps(value))
    result = consume(folder, state, confirmed_sink(events))
    assert result['state_status'] == 'invalid'
    assert events == []


def test_cli_default_local_sink_works_with_system_temp_directory():
    import subprocess
    import sys
    from pathlib import Path
    run = subprocess.run([sys.executable, 'scripts/run_source_notification_drill.py'],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result['passed'] is True
    assert result['transport'] == 'local_receipt_sink'
    assert result['delivered_count'] == 5


def test_drill_rejects_invalid_receipt_without_leaking_raw_transport_text(tmp_path):
    from scripts.run_source_notification_drill import run_drill
    from tradingagents.strategies.orchestration.source_notifications import DeliveryReceipt
    root = tmp_path / 'drill'
    result = run_drill(root, send=lambda event: DeliveryReceipt('confirmed', 'PRIVATE response body'))
    assert result['passed'] is False
    assert result['delivered_count'] == 0
    assert 'PRIVATE' not in json.dumps(result) + (root / 'drill-result.json').read_text()
