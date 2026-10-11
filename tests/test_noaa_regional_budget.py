"""Offline full-roster NOAA acquisition under a deterministic wall clock."""
from datetime import date, timedelta
from threading import Lock
from types import SimpleNamespace

import pytest

from tradingagents.strategies.data_sources import noaa_source as noaa
from tradingagents.strategies.data_sources import request_policy
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


@pytest.fixture
def regional_transport(monkeypatch):
    # Six seconds per batch at four workers consumes about 120 seconds for
    # 79 batches. Charge that amortized wall time deterministically, while
    # exercising the real executor, adapter, request policy and rate limits.
    lock = Lock()
    now = [0.0]
    calls = []
    failures = set()
    stations = [f'US{i:09d}' for i in range(7877)]
    states = list(noaa.AG_STATES)
    catalog = '\n'.join(station + ' ' * 27 + states[i % 10]
                        for i, station in enumerate(stations))
    inventory = '\n'.join(station + ' ' * 20 + 'TMAX 2020 2026'
                          for station in stations)
    days = [(date(2026, 10, 9) - timedelta(days=i)).isoformat() for i in range(30)]

    def clock():
        with lock:
            return now[0]

    def advance(seconds):
        with lock:
            now[0] += seconds

    def get(url, **options):
        if url.endswith('ghcnd-stations.txt'):
            advance(3.5)
            return SimpleNamespace(status_code=200, text=catalog, headers={})
        if url.endswith('ghcnd-inventory.txt'):
            advance(.62)
            return SimpleNamespace(status_code=200, text=inventory, headers={})
        assert url == noaa.BULK_URL
        params = options['params']
        batch = tuple(params['stations'].split(','))
        with lock:
            calls.append(batch)
            now[0] += 1.5
        if batch[0] in failures:
            return SimpleNamespace(status_code=503, headers={}, close=lambda: None)
        # Full declared station roster is requested. Like the real contract,
        # completeness means state/day/type samples, not a station census.
        rows = [{'STATION': station, 'DATE': day, 'TMAX': '70', 'TMIN': '50', 'PRCP': '.1',
                 'TMAX_ATTRIBUTES': ',,H,', 'TMIN_ATTRIBUTES': ',,H,', 'PRCP_ATTRIBUTES': ',,H,'}
                for station in batch[:10] for day in days]
        return SimpleNamespace(status_code=200, headers={}, json=lambda: rows)

    monkeypatch.setattr(noaa.time, 'monotonic', clock)
    monkeypatch.setattr(noaa.time, 'sleep', advance)
    monkeypatch.setattr(noaa, 'current_session_date', lambda: '2026-10-09')
    monkeypatch.setattr(request_policy.requests, 'get', get)
    return SimpleNamespace(clock=clock, calls=calls, failures=failures, stations=stations)


def test_slow_healthy_full_roster_completes_beyond_ninety_seconds(regional_transport):
    fixture = regional_transport
    with request_policy.provider_budget('noaa', 900):
        result = noaa.NOAASource().fetch_ag_weather_summary('2026-10-09', lookback_days=30)
        assert request_policy.current_provider_deadline('noaa') == 900
    assert 90 < fixture.clock() < 180
    assert len(fixture.calls) == 79
    assert sorted(station for batch in fixture.calls for station in batch) == fixture.stations
    assert sorted(map(len, fixture.calls)) == [77] + [100] * 78
    coverage = result['coverage']
    assert coverage['complete'] is True and coverage['missing'] == {}
    assert coverage['catalog_station_ids'] == fixture.stations
    assert coverage['catalog_station_count'] == 7877
    assert set(coverage['states_reporting']) == set(noaa.AG_STATES)
    assert coverage['window_start'] == '2026-09-10' and coverage['window_end'] == '2026-10-09'
    assert result['heat_stress_days'] == result['frost_events'] == 0
    assert result['observations'] > 0


@pytest.mark.parametrize('parent_deadline', [75, 110])
def test_shorter_parent_deadline_still_rejects_partial_regional_data(regional_transport, parent_deadline):
    fixture = regional_transport
    with request_policy.provider_budget('noaa', parent_deadline):
        with pytest.raises(SourceFetchError) as caught:
            noaa.NOAASource().fetch_ag_weather_summary('2026-10-09', lookback_days=30)
        assert request_policy.current_provider_deadline('noaa') == parent_deadline
    assert len(fixture.calls) < 79
    assert caught.value.reason_code == 'batch_failure'
    assert set(caught.value.failed_operations.values()) == {'timeout'}
    assert caught.value.partial_data['coverage']['complete'] is False
    for key in ('heat_stress_days', 'frost_events', 'precip_deficit_pct', 'avg_temp_anomaly_f'):
        assert caught.value.partial_data[key] is None


def test_terminal_bulk_failure_keeps_retries_and_incomplete_summary(regional_transport):
    fixture = regional_transport
    fixture.failures.add(fixture.stations[0])
    with request_policy.provider_budget('noaa', 900):
        with pytest.raises(SourceFetchError) as caught:
            noaa.NOAASource().fetch_ag_weather_summary('2026-10-09', lookback_days=30)
    assert sum(batch[0] == fixture.stations[0] for batch in fixture.calls) == 3
    assert caught.value.reason_code == 'batch_failure'
    assert set(caught.value.failed_operations.values()) == {'http_error'}
    assert caught.value.partial_data['coverage']['complete'] is False
    assert caught.value.partial_data['heat_stress_days'] is None
