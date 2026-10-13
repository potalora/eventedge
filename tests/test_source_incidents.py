from copy import deepcopy
import json

import pytest


def report(session='2026-10-06', observations=None, unresolved=None):
    return {'generation_id': 'gen_020', 'generation_commit': 'abc123', 'session': session,
            'evidence_complete': True, 'source_health_failures': [],
            'sources': {'configuration': 'cfg123', 'acquired_at': session + 'T22:00:00+00:00',
                        'observations': observations or {}, 'unresolved': unresolved or []}}


def failure(session='2026-10-06', reason='timeout', status=None):
    return report(session, {'fred': 'failed'}, [{'provider': 'fred', 'reason_code': reason,
                                                'http_status': status}])


def assess(value, previous=None):
    from tradingagents.strategies.orchestration.source_incidents import assess_source_incidents
    value['source_incidents'] = assess_source_incidents(value, previous)
    return value['source_incidents']


@pytest.mark.parametrize('reason,status', [('invalid_response', None), ('http_error', 400),
    ('http_error', 401), ('http_error', 403), ('http_error', 404), ('http_error', 410)])
def test_terminal_failure_requires_repair_immediately(reason, status):
    result = assess(failure(reason=reason, status=status))
    assert result['policy'] == 'source_incidents_v1'
    assert result['open_incidents'][0]['status'] == 'repair_required'
    assert result['alerts'][0]['kind'] == 'opened'


def test_three_distinct_sessions_escalate_and_repeat_is_identical():
    first = failure(); assess(first)
    second = failure('2026-10-07'); assess(second, first)
    third = failure('2026-10-08'); result = assess(third, second)
    assert result['open_incidents'][0]['consecutive_failed_sessions'] == 3
    assert result['open_incidents'][0]['first_failed_session'] == first['session']
    assert result['open_incidents'][0]['status'] == 'repair_required'
    assert [row['kind'] for row in result['alerts']] == ['escalated']
    assert assess(third, second) == result
    fourth = failure('2026-10-09'); assess(fourth, third)
    assert fourth['source_incidents']['alerts'] == []


def test_same_day_and_history_gap_cannot_manufacture_streak():
    first = failure(); assess(first)
    repeated = failure(); result = assess(repeated, first)
    assert result['history_status'] == 'invalid'
    assert result['open_incidents'][0]['consecutive_failed_sessions'] == 1
    later = failure('2026-10-08'); result = assess(later, first)
    assert result['history_status'] == 'invalid'
    assert result['open_incidents'][0]['consecutive_failed_sessions'] == 1


def test_unknown_observation_preserves_repair_without_streak_and_dedupes():
    first = failure(reason='invalid_response'); assess(first)
    unknown = report('2026-10-07'); result = assess(unknown, first)
    row = result['open_incidents'][0]
    assert (row['status'], row['observation'], row['consecutive_failed_sessions']) == ('repair_required', 'unknown', 0)
    assert result['alerts'][0]['kind'] == 'observation_lost'
    next_unknown = report('2026-10-08'); assert assess(next_unknown, unknown)['alerts'] == []


def test_failure_after_unobserved_session_starts_new_streak():
    first = failure(); assess(first)
    unknown = report('2026-10-07'); assess(unknown, first)
    current = failure('2026-10-08')
    row = assess(current, unknown)['open_incidents'][0]
    assert (row['status'], row['consecutive_failed_sessions']) == ('degraded', 1)
    assert row['first_failed_session'] == '2026-10-06'


def test_fresh_recovery_uses_new_york_day_and_weekend_previous_session(tmp_path):
    from tradingagents.strategies.orchestration.source_incidents import load_source_incidents
    prior = failure('2026-10-09'); assess(prior)
    path = tmp_path / '2026-10-09-gen_020-operational-report.json'
    path.write_text(json.dumps(prior))
    current = report('2026-10-12', {'fred': 'success'})
    current['sources']['acquired_at'] = '2026-10-13T01:00:00+00:00'
    result = load_source_incidents(current, tmp_path)
    assert result['history_status'] == 'available'
    assert result['open_incidents'] == []
    assert result['alerts'][0]['kind'] == 'recovered'


@pytest.mark.parametrize('damage', ['incomplete', 'health_failure', 'unresolved', 'stale', 'disabled'])
def test_recovery_requires_explicit_fresh_complete_healthy_evidence(damage):
    first = failure(reason='invalid_response'); assess(first)
    current = report('2026-10-07', {'fred': 'success_empty'})
    if damage == 'incomplete': current['evidence_complete'] = False
    if damage == 'health_failure': current['source_health_failures'] = [{'sources': ['fred']}]
    if damage == 'unresolved': current['sources']['unresolved'] = [{'provider': 'fred', 'reason_code': 'timeout'}]
    if damage == 'stale': current['sources']['acquired_at'] = '2026-10-06T22:00:00+00:00'
    if damage == 'disabled': current['sources']['observations']['fred'] = 'disabled'
    assert assess(current, first)['open_incidents'][0]['status'] == 'repair_required'


def test_real_recovery_does_not_close_unrelated_provider():
    first = failure(reason='invalid_response')
    first['sources']['unresolved'].append({'provider': 'noaa', 'reason_code': 'timeout'})
    assess(first)
    current = report('2026-10-07', {'fred': 'success'})
    result = assess(current, first)
    assert [row['provider'] for row in result['open_incidents']] == ['noaa']
    assert {row['kind'] for row in result['alerts']} == {'recovered', 'observation_lost'}
    assert all(len(row['alert_id']) == 64 for row in result['alerts'])


def test_health_failure_is_authoritative_even_without_request_diagnostic():
    current = report(observations={'fred': 'success'})
    current['source_health_failures'] = [{'sources': ['fred']}]
    assert assess(current)['open_incidents'][0]['observation'] == 'failed'


def test_untrusted_provider_and_reason_never_enter_incident_output():
    current = report(unresolved=[{'provider': 'https://secret?token=PRIVATE', 'reason_code': 'PRIVATE'},
                                 {'provider': 'fred', 'reason_code': 'PRIVATE'}])
    result = assess(current)
    assert 'PRIVATE' not in json.dumps(result)
    assert [row['provider'] for row in result['open_incidents']] == ['fred']
    assert result['open_incidents'][0]['reason_code'] == 'provider_error'


def test_loader_reads_only_previous_xnys_session_and_validates_identity(tmp_path):
    from tradingagents.strategies.orchestration.source_incidents import load_source_incidents
    prior = failure('2026-10-09'); assess(prior)
    current = failure('2026-10-13')  # Monday is an XNYS session, not a federal-holiday closure.
    assert load_source_incidents(current, tmp_path)['history_status'] == 'missing'
    prior = failure('2026-10-12'); assess(prior)
    path = tmp_path / '2026-10-12-gen_020-operational-report.json'
    path.write_text(json.dumps(prior))
    assert load_source_incidents(current, tmp_path)['history_status'] == 'available'
    prior['generation_commit'] = 'changed'; path.write_text(json.dumps(prior))
    assert load_source_incidents(current, tmp_path)['history_status'] == 'invalid'
    path.write_text('x' * (2 * 1024 * 1024 + 1))
    assert load_source_incidents(current, tmp_path)['history_status'] == 'invalid'


@pytest.mark.parametrize('damage', ['configuration', 'policy', 'count', 'duplicate', 'provider'])
def test_invalid_history_cannot_inject_or_escalate_incidents(damage):
    prior = failure(); assess(prior)
    if damage == 'configuration': prior['sources']['configuration'] = 'changed'
    if damage == 'policy': prior['source_incidents']['policy'] = 'unknown'
    row = prior['source_incidents']['open_incidents'][0]
    if damage == 'count': row['consecutive_failed_sessions'] = True
    if damage == 'duplicate': prior['source_incidents']['open_incidents'].append(deepcopy(row))
    if damage == 'provider': row['provider'] = 'PRIVATE'
    result = assess(failure('2026-10-07'), prior)
    assert result['history_status'] == 'invalid'
    assert result['open_incidents'][0]['consecutive_failed_sessions'] == 1


@pytest.mark.parametrize('damage', ['commit', 'non_session', 'bad_session', 'generation'])
def test_unavailable_current_identity_does_not_prevent_report_publication(tmp_path, damage):
    from tradingagents.strategies.orchestration.source_incidents import load_source_incidents
    current = report()
    if damage == 'commit': current['generation_commit'] = None
    if damage == 'non_session': current['session'] = '2026-10-10'
    if damage == 'bad_session': current['session'] = 'invalid'
    if damage == 'generation': current['generation_id'] = '../../other'
    assert load_source_incidents(current, tmp_path) == {
        'policy': 'source_incidents_v1', 'history_status': 'invalid',
        'assessment_status': 'unavailable', 'open_incidents': [], 'alerts': []}


def test_available_assessment_is_explicit():
    assert assess(failure())['assessment_status'] == 'available'


def test_missing_source_evidence_preserves_incident_without_extending_streak():
    prior = failure(); assess(prior)
    current = report('2026-10-07', {'fred': 'missing'},
                     [{'provider': 'fred', 'reason_code': 'missing_source_evidence'}])
    current['source_health_failures'] = [{'sources': ['fred']}]
    result = assess(current, prior)
    row = result['open_incidents'][0]
    assert (row['status'], row['observation'], row['consecutive_failed_sessions']) == ('degraded', 'unknown', 0)
    assert row['first_failed_session'] == '2026-10-06'
    assert result['alerts'][0]['kind'] == 'observation_lost'
    next_missing = deepcopy(current); next_missing['session'] = '2026-10-08'
    result = assess(next_missing, current)
    assert result['alerts'] == []
    assert result['open_incidents'][0]['consecutive_failed_sessions'] == 0


def test_new_missing_provider_opens_unknown_without_invented_failure_dates():
    current = report(observations={'fred': 'missing'},
                     unresolved=[{'provider': 'fred', 'reason_code': 'missing_source_evidence'}])
    row = assess(current)['open_incidents'][0]
    assert (row['status'], row['observation'], row['consecutive_failed_sessions']) == ('degraded', 'unknown', 0)
    assert row['reason_code'] == 'missing_source_evidence'
    assert row['first_failed_session'] is None
    assert row['last_failed_session'] is None
    actual = failure('2026-10-07'); row = assess(actual, current)['open_incidents'][0]
    assert row['first_failed_session'] == '2026-10-07'
    assert row['consecutive_failed_sessions'] == 1


def test_typed_failure_wins_over_missing_observation_and_evidence():
    current = report(observations={'fred': 'missing'}, unresolved=[
        {'provider': 'fred', 'reason_code': 'missing_source_evidence'},
        {'provider': 'fred', 'reason_code': 'invalid_response'}])
    row = assess(current)['open_incidents'][0]
    assert (row['status'], row['observation'], row['consecutive_failed_sessions']) == ('repair_required', 'failed', 1)


def test_missing_current_configuration_preserves_known_incident_as_unavailable():
    prior = failure(reason='invalid_response'); assess(prior)
    current = report('2026-10-07', {'fred': 'success'})
    current['sources'].pop('configuration')
    result = assess(current, prior)
    assert (result['history_status'], result['assessment_status']) == ('invalid', 'unavailable')
    row = result['open_incidents'][0]
    assert (row['status'], row['observation'], row['consecutive_failed_sessions']) == ('repair_required', 'unknown', 0)
    assert row['first_failed_session'] == '2026-10-06'
    assert [alert['kind'] for alert in result['alerts']] == ['observation_lost']


def test_asset_master_transport_alias_does_not_create_unrecoverable_duplicate():
    prior = report(observations={'equity_universe': 'failed'}, unresolved=[
        {'provider': 'alpaca', 'source': 'equity_universe', 'reason_code': 'http_error', 'http_status': 403},
        {'provider': 'equity_universe', 'reason_code': 'provider_error'}])
    result = assess(prior)
    assert [row['provider'] for row in result['open_incidents']] == ['equity_universe']
    current = report('2026-10-07', {'equity_universe': 'complete'})
    assert assess(current, prior)['open_incidents'] == []


def test_configuration_survives_unavailable_sessions_and_recovery():
    prior = failure(reason='invalid_response'); assess(prior)
    missing = report('2026-10-07'); missing['sources'].pop('configuration')
    result = assess(missing, prior)
    assert result['configuration'] == 'cfg123'
    missing_again = report('2026-10-08'); missing_again['sources'].pop('configuration')
    result = assess(missing_again, missing)
    assert result['configuration'] == 'cfg123'
    assert result['alerts'] == []
    assert result['open_incidents'][0]['status'] == 'repair_required'
    healthy = report('2026-10-09', {'fred': 'success'})
    result = assess(healthy, missing_again)
    assert result['history_status'] == 'available'
    assert result['open_incidents'] == []
    assert result['alerts'][0]['kind'] == 'recovered'


def test_restored_differing_configuration_cannot_reuse_unavailable_incident():
    prior = failure(reason='invalid_response'); assess(prior)
    missing = report('2026-10-07'); missing['sources'].pop('configuration'); assess(missing, prior)
    healthy = report('2026-10-08', {'fred': 'success'})
    healthy['sources']['configuration'] = 'different'
    result = assess(healthy, missing)
    assert result['history_status'] == 'invalid'
    assert result['alerts'] == []


def test_corrupt_retained_configuration_does_not_reuse_incidents_on_missing_day():
    prior = failure(reason='invalid_response'); assess(prior)
    prior['source_incidents']['configuration'] = ['PRIVATE']
    missing = report('2026-10-07'); missing['sources'].pop('configuration')
    result = assess(missing, prior)
    assert result['history_status'] == 'invalid'
    assert result['open_incidents'] == []
    assert 'PRIVATE' not in json.dumps(result)
