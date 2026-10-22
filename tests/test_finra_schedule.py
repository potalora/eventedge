"""Official publication cycles govern usability; arbitrary age does not."""
from datetime import date, datetime

import pytest


def at(value='2026-10-22T12:00:00+00:00'):
    return datetime.fromisoformat(value)


def test_september_thirtieth_is_current_before_october_publication():
    from tradingagents.strategies.data_sources.finra_schedule import validate_current_settlement
    result = validate_current_settlement('2026-09-30', acquired_at=at())
    assert result['status'] == 'current'
    assert result['required_settlement_date'] == '2026-09-30'
    assert result['publication_date'] == '2026-10-09'


@pytest.mark.parametrize('acquired', ['2026-10-26T04:01:00+00:00', '2026-10-27T03:59:00+00:00'])
def test_date_only_publication_evidence_cannot_certify_midnight_or_intraday_release(acquired):
    from tradingagents.strategies.data_sources.finra_schedule import eligible_settlement_dates, validate_current_settlement
    result = validate_current_settlement('2026-09-30', acquired_at=at(acquired))
    assert result['status'] == 'current' and result['publication_day_ambiguous'] is True
    assert '2026-10-15' not in eligible_settlement_dates(acquired_at=at(acquired))


def test_next_new_york_day_requires_new_cycle_and_rejects_old_symbol():
    from tradingagents.strategies.data_sources.finra_schedule import validate_current_settlement
    acquired = at('2026-10-27T04:00:00+00:00')
    assert validate_current_settlement('2026-10-15', acquired_at=acquired)['status'] == 'current'
    result = validate_current_settlement('2026-09-30', acquired_at=acquired)
    assert result['status'] == 'stale' and result['required_settlement_date'] == '2026-10-15'


def test_reference_session_and_actual_acquisition_both_cap_publication_visibility():
    from tradingagents.strategies.data_sources.finra_schedule import validate_current_settlement
    after = at('2026-10-28T12:00:00+00:00')
    result = validate_current_settlement('2026-09-30', acquired_at=after, as_of=date(2026, 10, 22))
    assert result['status'] == 'current' and result['acquired_at'] == after.isoformat()
    result = validate_current_settlement('2026-09-30', acquired_at=at(), as_of=after)
    assert result['status'] == 'current'


def test_reviewed_holiday_corrections_replace_sdk_phantom_archives():
    from tradingagents.strategies.data_sources.finra_schedule import correct_archive_dates
    assert correct_archive_dates(['20240115', '20240329', '20260930'], acquired_at=at()) == [
        '2024-01-12', '2024-03-28', '2026-09-30',
    ]


def test_midmonth_cycle_is_added_after_publication_even_when_sdk_omits_current_month():
    from tradingagents.strategies.data_sources.finra_schedule import correct_archive_dates
    assert correct_archive_dates(['20260930'], acquired_at=at('2026-10-27T12:00:00+00:00')) == [
        '2026-09-30', '2026-10-15',
    ]


def test_priormonth_settlement_is_not_available_just_because_sdk_requests_it():
    from tradingagents.strategies.data_sources.finra_schedule import correct_archive_dates
    assert correct_archive_dates(['20260915', '20260930'], acquired_at=at('2026-10-01T12:00:00+00:00')) == [
        '2026-09-15',
    ]


def test_current_archive_success_does_not_certify_complete_history():
    from tradingagents.strategies.data_sources.finra_schedule import assess_archive_inventory
    value = assess_archive_inventory(['2026-09-15', '2026-09-30'], ['2026-09-30'], acquired_at=at())
    assert value['current_complete'] is True and value['historical_complete'] is False
    assert value['history_scope'] == 'sdk_requested_archives_with_reviewed_holiday_corrections'
    assert value['missing_historical_archive_dates'] == ['2026-09-15']


def test_missing_current_archive_and_missing_symbol_have_distinct_evidence():
    from tradingagents.strategies.data_sources.finra_schedule import assess_archive_inventory, validate_current_settlement
    inventory = assess_archive_inventory(['2026-09-15', '2026-09-30'], ['2026-09-15'], acquired_at=at())
    assert inventory['current_complete'] is False
    assert inventory['missing_historical_archive_dates'] == []
    assert validate_current_settlement(None, acquired_at=at())['status'] == 'missing'


@pytest.mark.parametrize('settlement', ['2026-10-01', 'bad'])
def test_unreviewed_or_invalid_settlement_cannot_be_current(settlement):
    from tradingagents.strategies.data_sources.finra_schedule import validate_current_settlement
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    with pytest.raises(SourceFetchError):
        validate_current_settlement(settlement, acquired_at=at())


@pytest.mark.parametrize('acquired', [datetime(2026, 10, 22), at('2027-02-01T12:00:00+00:00')])
def test_naive_clock_or_expired_reviewed_calendar_cannot_certify_freshness(acquired):
    from tradingagents.strategies.data_sources.finra_schedule import validate_current_settlement
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    with pytest.raises(SourceFetchError):
        validate_current_settlement('2026-09-30', acquired_at=acquired)


def test_old_reference_date_cannot_reactivate_expired_calendar_for_new_acquisition():
    from tradingagents.strategies.data_sources.finra_schedule import validate_current_settlement
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    with pytest.raises(SourceFetchError):
        validate_current_settlement('2026-12-15', acquired_at=at('2027-01-02T12:00:00+00:00'),
                                    as_of=date(2026, 12, 31))
