"""Current weekly USDM observations survive the Tuesday/Thursday release lag.

The API fixture preserves the native camel-case response and timestamp from
2026-10-09's retained ten-state response. USDM's Tuesday map is published on
Thursday: https://droughtmonitor.unl.edu/About/WhatistheUSDM.aspx
Only external HTTP I/O and the acquisition clock are replaced.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tradingagents.strategies.data_sources import drought_monitor_source as source_module
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine

pytestmark = pytest.mark.scheduled_acceptance


NATIVE_ROW = {
    'mapDate': '2026-10-06T00:00:00', 'stateAbbreviation': 'IA',
    'none': 98.64, 'd0': 1.36, 'd1': 0.0, 'd2': 0.0, 'd3': 0.0, 'd4': 0.0,
    'validStart': '2026-10-06T00:00:00', 'validEnd': '2026-10-12T23:59:59',
    'statisticFormatID': 2,
}


@pytest.fixture
def weekly_source(monkeypatch):
    monkeypatch.setattr(source_module, 'current_session_date', lambda: '2026-10-14')
    monkeypatch.setattr(source_module, 'acquisition_time', lambda: '2026-10-14T22:03:00+00:00')
    # All requested states are present in the native response. The parser must
    # select the last published weekly period, without treating missing as zero.
    rows = [dict(NATIVE_ROW, stateAbbreviation=state) for state in source_module.DEFAULT_AG_STATES]
    monkeypatch.setattr(source_module, 'provider_request',
        lambda *args, **kwargs: SimpleNamespace(json=lambda: deepcopy(rows)))
    return source_module.DroughtMonitorSource()


@pytest.mark.parametrize('method', ['severity', 'composite', 'engine'])
def test_last_published_tuesday_is_current_on_wednesday(weekly_source, method):
    if method == 'severity':
        result = weekly_source.fetch_drought_severity(end='2026-10-14')
        assert set(result) == set(source_module.DEFAULT_AG_STATES)
        assert {row['observation_date'] for row in result.values()} == {'2026-10-06'}
        assert {row['available_at'] for row in result.values()} == {'2026-10-14T22:03:00+00:00'}
    elif method == 'composite':
        assert weekly_source.fetch_composite_score(date='2026-10-14') == 0.0
    else:
        owner = object.__new__(MultiStrategyEngine)
        owner.registry = SimpleNamespace(get=lambda _: weekly_source)
        result = owner._fetch_drought_data('2026-10-14', vintage_as_of='2026-10-14')
        assert 'error' not in result, result
        assert result['composite_score'] == 0.0
        assert set(result['states']) == set(source_module.DEFAULT_AG_STATES)


@pytest.mark.parametrize('reason,status,partial', [
    ('invalid_response', None, {'states': {}, 'coverage': {'complete': False, 'missing_states': ['IA']}}),
    ('http_error', 503, {}),
])
def test_engine_preserves_typed_drought_failure_for_incident_reporting(reason, status, partial):
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    def unavailable(*args, **kwargs):
        raise SourceFetchError('Drought Monitor acquisition incomplete', reason_code=reason,
                               http_status=status, partial_data=partial, attempts=3)
    owner = object.__new__(MultiStrategyEngine)
    owner.registry = SimpleNamespace(get=lambda _: SimpleNamespace(fetch_drought_severity=unavailable))
    result = owner._fetch_drought_data('2026-10-14')
    assert result['_coverage'] == {
        'status': 'partial' if partial else 'failed',
        'reason_code': reason, 'http_status': status, 'attempts': 3,
    }
    assert result.get('states', {}) == partial.get('states', {})
    assert 'error' in result


@pytest.mark.parametrize('session,clock,observation', [
    ('2026-10-13', '2026-10-13T22:00:00+00:00', '2026-10-06'),
    ('2026-10-14', '2026-10-14T22:00:00+00:00', '2026-10-06'),
    ('2026-10-15', '2026-10-15T12:29:59+00:00', '2026-10-06'),
    ('2026-10-15', '2026-10-15T12:30:00+00:00', '2026-10-13'),
    ('2026-10-16', '2026-10-16T22:00:00+00:00', '2026-10-13'),
    ('2026-10-19', '2026-10-19T22:00:00+00:00', '2026-10-13'),
    ('2026-11-05', '2026-11-05T13:29:59+00:00', '2026-10-27'),
    ('2026-11-05', '2026-11-05T13:30:00+00:00', '2026-11-03'),
])
def test_default_window_follows_published_week_with_eastern_release_boundary(monkeypatch, session, clock, observation):
    from datetime import date
    calls = []
    monkeypatch.setattr(source_module, 'current_session_date', lambda: session)
    monkeypatch.setattr(source_module, 'acquisition_time', lambda: clock)
    row = dict(NATIVE_ROW, mapDate=observation + 'T00:00:00')
    def transport(*args, **kwargs):
        calls.append(kwargs['params'])
        return SimpleNamespace(json=lambda: [deepcopy(row)])
    monkeypatch.setattr(source_module, 'provider_request', transport)
    result = source_module.DroughtMonitorSource().fetch_drought_severity(['IA'], end=session)
    assert result['IA']['observation_date'] == observation
    assert result['IA']['available_at'] == clock
    start = date.fromisoformat(observation)
    assert calls[0]['startdate'] == f'{start.month}/{start.day}/{start.year}'
    assert calls[0]['statisticsType'] == 2


def test_last_weeks_map_is_unavailable_once_new_map_should_be_published(weekly_source, monkeypatch):
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    monkeypatch.setattr(source_module, 'current_session_date', lambda: '2026-10-15')
    monkeypatch.setattr(source_module, 'acquisition_time', lambda: '2026-10-15T12:30:00+00:00')
    with pytest.raises(SourceFetchError, match='requested states unavailable') as failure:
        weekly_source.fetch_drought_severity(end='2026-10-15')
    assert failure.value.reason_code == 'invalid_response'
    assert failure.value.partial_data['coverage']['complete'] is False
    assert failure.value.partial_data['coverage']['missing_states'] == sorted(source_module.DEFAULT_AG_STATES)


def test_explicit_window_is_not_widened_to_hide_missing_observations(weekly_source):
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    with pytest.raises(SourceFetchError, match='requested states unavailable'):
        weekly_source.fetch_drought_severity(start='2026-10-07', end='2026-10-14')


def test_current_vintage_cannot_authorize_observations_after_reference_session(weekly_source, monkeypatch):
    # Friday reference acquired on Saturday remains bound to Friday's period.
    monkeypatch.setattr(source_module, 'current_session_date', lambda: '2026-10-17')
    monkeypatch.setattr(source_module, 'acquisition_time', lambda: '2026-10-17T22:00:00+00:00')
    future = dict(NATIVE_ROW, mapDate='2026-10-20T00:00:00')
    monkeypatch.setattr(source_module, 'provider_request',
        lambda *args, **kwargs: SimpleNamespace(json=lambda: [future]))
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    with pytest.raises(SourceFetchError, match='requested states unavailable'):
        weekly_source.fetch_drought_severity(['IA'], end='2026-10-16', vintage_as_of='2026-10-17')
    with pytest.raises(SourceFetchError, match='historical_vintage_unavailable'):
        weekly_source.fetch_drought_severity(['IA'], end='2026-10-16')
