"""Publication-calendar admission, using constructed IO rather than live data."""
from datetime import date, timedelta
import hashlib
from types import SimpleNamespace

import pandas as pd
import pytest

from tradingagents.strategies.data_sources import cftc_source as mod
from tradingagents.strategies.orchestration.source_inputs import successful_source


def calendar(year=2026):
    months = []
    for month in range(1, 13):
        day = date(year, month, 1)
        days = []
        while day.month == month:
            if day.weekday() == 4:
                # Official 2026 exceptions include June 19 -> June 22.
                release = day + timedelta(days=3) if day == date(2026, 6, 19) else day
                days.append(release.day)
            day += timedelta(days=1)
        months.append('<tr><td>' + date(year, month, 1).strftime('%B') + '</td>' +
                      ''.join(f'<td>{number}*</td>' for number in days) + '</tr>')
    return '<h3>' + str(year) + ' Release Schedule</h3><table><tr><th>Month</th><th>Dates</th></tr>' + ''.join(months) + '</table>'


def install(monkeypatch, latest='2026-10-13', acquired='2026-10-21T22:00:00+00:00', body=None):
    monkeypatch.setattr(mod, 'current_session_date', lambda: acquired[:10])
    monkeypatch.setattr(mod, 'acquisition_time', lambda: acquired)
    rows = pd.date_range(end=latest, periods=53, freq='7D')
    frame = pd.DataFrame({mod.COL_MARKET: [mod.COMMODITY_CODES['gold']] * 53,
        mod.COL_DATE: rows.date.astype(str), mod.COL_MM_LONG: range(53), mod.COL_MM_SHORT: [0] * 53})
    source = mod.CFTCSource()
    monkeypatch.setattr(source, '_fetch_raw_report', lambda *a, **k: frame)
    payload = (calendar() if body is None else body).encode()
    monkeypatch.setattr(mod, 'provider_request', lambda *a, **k: SimpleNamespace(content=payload, close=lambda: None))
    return source


def fetch(source, as_of='2026-10-21'):
    return source.fetch({'method': 'cot_positioning', 'commodities': ['gold'],
                         'lookback_weeks': 52, 'as_of': as_of})


def test_stale_complete_archive_is_unavailable(monkeypatch):
    result = fetch(install(monkeypatch, latest='2025-12-30'))
    assert result.get('error') and not successful_source(result)
    assert result['coverage']['expected_observation_date'] == '2026-10-13'
    assert result['coverage']['complete'] is False


@pytest.mark.parametrize('acquired,latest,expected', [
    ('2026-10-23T19:29:59+00:00', '2026-10-13', '2026-10-13'),
    ('2026-10-23T19:30:00+00:00', '2026-10-20', '2026-10-20'),
    ('2026-06-19T22:00:00+00:00', '2026-06-09', '2026-06-09'),
    ('2026-06-22T19:29:59+00:00', '2026-06-09', '2026-06-09'),
    ('2026-06-22T19:30:00+00:00', '2026-06-16', '2026-06-16'),
])
def test_release_boundary_and_official_holiday_delay(monkeypatch, acquired, latest, expected):
    result = fetch(install(monkeypatch, latest=latest, acquired=acquired), acquired[:10])
    assert 'error' not in result
    assert result['gold']['observation_date'] == expected
    assert result['coverage']['release_schedule_sha256'] == hashlib.sha256(calendar().encode()).hexdigest()


def test_unpublished_future_row_is_not_used(monkeypatch):
    result = fetch(install(monkeypatch, latest='2026-10-20', acquired='2026-10-21T22:00:00+00:00'))
    assert 'error' not in result
    assert result['gold']['observation_date'] == '2026-10-13'
    assert result['gold']['lookback_observations'] == 52


@pytest.mark.parametrize('body', ['<h3>2025 Release Schedule</h3><table></table>',
                                '<h3>2026 Release Schedule</h3><table><tr><td>October</td><td>16</td></tr></table>'])
def test_unverifiable_schedule_fails_closed(monkeypatch, body):
    assert fetch(install(monkeypatch, body=body)).get('error')


def test_calendar_does_not_assume_previous_year_release(monkeypatch):
    result = fetch(install(monkeypatch, latest='2025-12-30', acquired='2026-01-01T22:00:00+00:00'), '2026-01-01')
    assert result.get('error')


def test_calendar_missing_week_with_all_months_is_not_publication_proof(monkeypatch):
    body=calendar().replace('<td>9*</td>','',1)
    install(monkeypatch,body=body)
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    with pytest.raises(SourceFetchError):
        mod._publication_scope('2026-10-21','2026-10-21T22:00:00+00:00')


def test_cached_pre_release_archive_cannot_hide_new_required_report(monkeypatch):
    source = install(monkeypatch, acquired='2026-10-23T19:31:00+00:00')
    frame = source._fetch_raw_report()
    frame.attrs['acquired_at'] = '2026-10-23T19:29:00+00:00'
    del source._fetch_raw_report
    source._cache['disaggregated_futures|52|2026-10-23|2026-10-23'] = frame
    result = fetch(source, '2026-10-23')
    assert result.get('error') and not successful_source(result)
    assert result['coverage']['expected_observation_date'] == '2026-10-20'
    assert result['coverage']['archive_acquired_at'] == frame.attrs['acquired_at']


def test_cached_unpublished_row_keeps_original_clock_and_later_availability(monkeypatch):
    source = install(monkeypatch, latest='2026-10-20', acquired='2026-10-23T19:31:00+00:00')
    frame = source._fetch_raw_report()
    frame.attrs['acquired_at'] = '2026-10-23T19:29:00+00:00'
    del source._fetch_raw_report
    source._cache['disaggregated_futures|52|2026-10-23|2026-10-23'] = frame
    result = fetch(source, '2026-10-23')
    assert 'error' not in result
    assert result['gold']['acquired_at'] == frame.attrs['acquired_at']
    assert result['gold']['available_at'] == '2026-10-23T19:31:00+00:00'
    assert result['gold']['observation_date'] == '2026-10-20'


@pytest.mark.parametrize('phase', ['calendar_parse', 'positioning'])
def test_local_processing_cannot_publish_after_shared_deadline(monkeypatch, phase):
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    import bs4
    source = install(monkeypatch)
    now = [0.0]
    if phase == 'calendar_parse':
        original = bs4.BeautifulSoup
        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            now[0] = 10.0
            return result
        monkeypatch.setattr(bs4, 'BeautifulSoup', delayed)
    else:
        original = pd.to_datetime
        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            now[0] = 10.0
            return result
        monkeypatch.setattr(pd, 'to_datetime', delayed)
    with provider_budget('cftc', 10, clock=lambda:now[0], limits=()):
        result = fetch(source)
    assert result.get('error') and '[timeout' in result['error']
    assert not successful_source(result)
