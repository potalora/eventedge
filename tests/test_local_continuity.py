"""Local continuity must distinguish approved scope from useful live completion."""
import copy
from datetime import date

import pytest


COMMIT = 'a' * 40
PROFILE = 'b' * 64
SESSIONS = ('2026-10-12', '2026-10-13', '2026-10-14', '2026-10-15', '2026-10-16')


def observation(session=SESSIONS[0]):
    from tradingagents.strategies.orchestration.local_continuity import REQUIRED_CHECKS
    return {'session': session, 'generation_id': 'gen_001', 'generation_commit': COMMIT,
        'profile_sha256': PROFILE, 'epoch_id': 'epoch', 'attempts': 1,
        'checks': {name: True for name in REQUIRED_CHECKS},
        'full_source_coverage': False, 'approved_exclusions': ['filing_analysis'],
        'unexpected_failures': [], 'research_holds': [],
        'native_models': ['gpt-6-luna', 'gpt-6.1-sol'],
        'source_observations': {'edgar': 'failed', 'fred': 'success'},
        'fill_lifecycle_observed': False, 'carried_positions_observed': False}


def classify(value):
    from tradingagents.strategies.orchestration.local_continuity import classify_session
    return classify_session(value, expected_commit=COMMIT, expected_profile=PROFILE)


def test_approved_reduced_scope_can_qualify_without_claiming_full_coverage():
    value = classify(observation())
    assert value['qualifies'] is True
    assert value['status'] == 'qualifying_reduced_scope'
    assert value['full_source_coverage'] is False


@pytest.mark.parametrize('check', ['accounting', 'staging', 'sources', 'screening',
    'model_outcomes', 'committee', 'timeliness', 'report', 'replay', 'cleanup'])
def test_missing_integrated_evidence_blocks_qualification(check):
    value = observation(); value['checks'][check] = False
    assert classify(value)['qualifies'] is False


def test_unexpected_outage_or_whole_research_hold_is_degraded_even_with_accounting():
    for field in ('unexpected_failures', 'research_holds'):
        value = observation(); value[field] = ['cftc_unavailable']
        assert classify(value)['status'] == 'degraded'
        assert classify(value)['qualifies'] is False


def test_duplicate_live_attempt_and_candidate_or_profile_change_cannot_qualify():
    for field, replacement in [('attempts', 2), ('generation_commit', 'c' * 40),
                               ('profile_sha256', 'd' * 64)]:
        value = observation(); value[field] = replacement
        assert classify(value)['qualifies'] is False


def test_healthy_no_event_session_does_not_require_manufactured_model_calls():
    value = observation(); value['native_models'] = []
    value['checks']['no_model_work_required'] = True
    assert classify(value)['qualifies'] is True
    value['checks']['no_model_work_required'] = False
    assert classify(value)['qualifies'] is False


def test_five_sessions_alone_do_not_prove_recovery_or_unobserved_models():
    from tradingagents.strategies.orchestration.local_continuity import summarize_continuity
    rows = [classify(observation(session)) for session in SESSIONS]
    result = summarize_continuity(rows, through=date(2026, 10, 16))
    assert result['consecutive_qualifying_sessions'] == 5
    assert result['five_session_continuity_accepted'] is True
    assert result['automatic_recovery_observed'] is False
    assert result['local_operational_acceptance'] is False
    assert result['fill_lifecycle_observed'] is False
    assert result['carried_positions_observed'] is False
    for row in rows: row['native_models'] = []
    assert summarize_continuity(rows, through=date(2026, 10, 16))['native_models_observed'] == []


def test_normal_later_recovery_and_same_identity_qualifying_window_are_bound():
    from tradingagents.strategies.orchestration.local_continuity import summarize_continuity
    earlier = observation('2026-10-09')
    earlier['unexpected_failures'] = ['fred']
    earlier['source_observations']['fred'] = 'failed'
    rows = [classify(earlier), *[classify(observation(session)) for session in SESSIONS]]
    result = summarize_continuity(rows, through=date(2026, 10, 16))
    assert result['automatic_recovery_observed'] is True
    assert result['local_operational_acceptance'] is True
    changed = copy.deepcopy(rows); changed[-1]['epoch_id'] = 'replacement_epoch'
    assert summarize_continuity(changed, through=date(2026, 10, 16))['local_operational_acceptance'] is False


def test_same_day_duplicates_and_calendar_gaps_never_count_as_five_sessions():
    from tradingagents.strategies.orchestration.local_continuity import summarize_continuity
    rows = [classify(observation(session)) for session in SESSIONS]
    assert summarize_continuity([*rows, rows[-1]], through=date(2026, 10, 16))['consecutive_qualifying_sessions'] == 0
    assert summarize_continuity([*rows[:2], *rows[3:]], through=date(2026, 10, 16))['consecutive_qualifying_sessions'] == 2


def test_recovery_under_a_different_candidate_or_profile_is_not_automatic_continuity():
    from tradingagents.strategies.orchestration.local_continuity import summarize_continuity
    old = classify(observation('2026-10-09'))
    old['source_observations']['fred'] = 'failed'; old['qualifies'] = False
    old['profile_sha256'] = 'e' * 64
    rows = [old, *[classify(observation(session)) for session in SESSIONS]]
    assert summarize_continuity(rows, through=date(2026, 10, 16))['automatic_recovery_observed'] is False
