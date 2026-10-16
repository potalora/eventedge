"""Offline full-roster NOAA acquisition under a deterministic wall clock."""
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from tradingagents.strategies.data_sources import noaa_source as noaa
from tradingagents.strategies.data_sources import request_policy
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


@pytest.fixture
def regional_transport(monkeypatch):
    # Twenty-four seconds per healthy batch needs ~240s with eight workers for the
    # retained native 7,877-station roster. Charge the amortized wall time at
    # the actual configured concurrency, exercising the real executor, adapter,
    # request policy and rate limits without a several-minute test. This is a
    # constructed latency stress case, not measured native response timings.
    lock = Lock()
    now = [0.0]
    calls = []
    failures = set()
    decode_delays = {}
    all_started = Event()
    workers = [None]
    batch_seconds = [24]
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
            if len(calls) == 79:
                all_started.set()
            now[0] += batch_seconds[0] / workers[0]
        if batch[0] in failures:
            return SimpleNamespace(status_code=503, headers={}, close=lambda: None)
        # Full declared station roster is requested. Like the real contract,
        # completeness means state/day/type samples, not a station census.
        rows = [{'STATION': station, 'DATE': day, 'TMAX': '70', 'TMIN': '50', 'PRCP': '.1',
                 'TMAX_ATTRIBUTES': ',,H,', 'TMIN_ATTRIBUTES': ',,H,', 'PRCP_ATTRIBUTES': ',,H,'}
                for station in batch[:10] for day in days]
        def decode():
            delay = decode_delays.get(batch[0], 0)
            if delay:
                assert all_started.wait(3), 'late-body case requires all batches started'
                advance(delay)
            return rows
        return SimpleNamespace(status_code=200, headers={}, json=decode)

    def executor(max_workers):
        workers[0] = max_workers
        return ThreadPoolExecutor(max_workers=max_workers)

    monkeypatch.setattr(noaa, 'ThreadPoolExecutor', executor)
    monkeypatch.setattr(noaa.time, 'monotonic', clock)
    monkeypatch.setattr(noaa.time, 'sleep', advance)
    monkeypatch.setattr(noaa, 'current_session_date', lambda: '2026-10-09')
    monkeypatch.setattr(request_policy.requests, 'get', get)
    return SimpleNamespace(clock=clock, calls=calls, failures=failures, stations=stations, decode_delays=decode_delays, batch_seconds=batch_seconds, advance=advance)


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


def test_sixteen_inflight_batches_keep_shared_five_per_second_limit(monkeypatch):
    """Real threads overlap slow calls while the physical request starts pace."""
    lock, release = Lock(), Event()
    started, active, peak = [], [0], [0]
    stations = [f'US{i:09d}' for i in range(1600)]
    states = list(noaa.AG_STATES)
    catalog = '\n'.join(station + ' ' * 27 + states[i % 10]
                        for i, station in enumerate(stations))
    inventory = '\n'.join(station + ' ' * 20 + 'TMAX 2020 2026'
                          for station in stations)

    def get(url, **options):
        if url.endswith('ghcnd-stations.txt'):
            return SimpleNamespace(status_code=200, text=catalog, headers={})
        if url.endswith('ghcnd-inventory.txt'):
            return SimpleNamespace(status_code=200, text=inventory, headers={})
        assert url == noaa.BULK_URL
        with lock:
            started.append(noaa.time.monotonic())
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            if len(started) == 16:
                release.set()
        assert release.wait(6), 'all sixteen slow batches must overlap'
        with lock:
            active[0] -= 1
        return SimpleNamespace(status_code=200, headers={}, json=lambda: [])

    monkeypatch.setattr(request_policy.requests, 'get', get)
    noaa.NOAASource().fetch_region_daily('2026-09-09', '2026-10-15')
    assert len(started) == peak[0] == 16
    assert request_policy.PROVIDER_LIMITS['noaa'] == ((5, 1), (10000, 86400))
    assert all(sum(start <= other < start + 1 for other in started) <= 5
               for start in started)


def test_final_complete_bulk_body_after_deadline_is_not_accepted(regional_transport):
    fixture = regional_transport
    # All 79 requests can begin in time, but an otherwise valid last decoded
    # body arrives late. Socket inactivity timeouts do not bound total body time.
    fixture.batch_seconds[0] = 12
    fixture.decode_delays[fixture.stations[7800]] = 180
    with pytest.raises(SourceFetchError) as caught:
        noaa.NOAASource().fetch_ag_weather_summary('2026-10-09', lookback_days=30)
    assert len(fixture.calls) == 79
    assert caught.value.reason_code == 'batch_failure'
    assert set(caught.value.failed_operations.values()) == {'timeout'}
    assert caught.value.partial_data['coverage']['complete'] is False
    assert caught.value.partial_data['heat_stress_days'] is None


def test_summary_completed_after_absolute_budget_is_not_accepted(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(noaa.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(noaa, 'current_session_date', lambda: '2026-10-15')

    def region(self, start, end):
        now[0] = 179
        return {state:[{'date':'2026-10-15', 'station':state, 'datatype':kind, 'value':50}
                       for kind in ('TMAX','TMIN','PRCP')] for state in noaa.AG_STATES}

    def finished():
        now[0] = 181
        return '2026-10-15T22:00:00+00:00'

    monkeypatch.setattr(noaa.NOAASource, 'fetch_region_daily', region)
    monkeypatch.setattr(noaa, 'acquisition_time', finished)
    with pytest.raises(SourceFetchError) as caught:
        noaa.NOAASource().fetch_ag_weather_summary('2026-10-15', lookback_days=1)
    assert caught.value.reason_code == 'batch_failure'
    assert caught.value.partial_data['coverage']['complete'] is False
    assert set(caught.value.failed_operations.values()) == {'timeout'}
    assert caught.value.partial_data['heat_stress_days'] is None


def test_region_collection_after_deadline_is_not_accepted(regional_transport, monkeypatch):
    fixture = regional_transport
    fixture.batch_seconds[0] = 12
    executor_factory = noaa.ThreadPoolExecutor

    class DelayedCollection:
        def __init__(self, max_workers):
            self.executor = executor_factory(max_workers=max_workers)
            self.count = 0

        def __enter__(self):
            self.executor.__enter__()
            return self

        def __exit__(self, *args):
            return self.executor.__exit__(*args)

        def submit(self, *args):
            future = self.executor.submit(*args)
            self.count += 1
            if self.count != 79:
                return future

            def collect():
                rows = future.result()
                fixture.advance(180)
                return rows
            return SimpleNamespace(result=collect)

    monkeypatch.setattr(noaa, 'ThreadPoolExecutor', DelayedCollection)
    with pytest.raises(SourceFetchError) as caught:
        noaa.NOAASource().fetch_region_daily('2026-09-03', '2026-10-09')
    assert len(fixture.calls) == 79
    assert caught.value.reason_code == 'timeout'
    assert set(caught.value.partial_data['states']) == set(noaa.AG_STATES)
