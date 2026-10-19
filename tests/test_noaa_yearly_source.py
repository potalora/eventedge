"""Yearly source contracts; positive payloads are retained official NOAA bytes."""
from importlib.util import find_spec


def test_explicit_yearly_adapter_is_available_without_a_token(monkeypatch):
    # Missing adapter (or accidentally inheriting legacy token policy) must fail.
    assert find_spec('tradingagents.strategies.data_sources.noaa_yearly_source') is not None
    from tradingagents.strategies.data_sources.noaa_yearly_source import NOAAYearlySource
    monkeypatch.delenv('NOAA_CDO_TOKEN', raising=False)
    source = NOAAYearlySource()
    assert source.name == 'noaa'
    assert source.requires_api_key is False
    assert source.is_available() is True

import pytest
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget


@pytest.mark.parametrize('date,lookback,vintage', [
    ('2026-10-18', 30, '2026-10-18'), ('bad', 30, '2026-10-19'),
    ('2026-10-20', 30, '2026-10-19'), ('2026-10-19', 0, None),
    ('2026-10-19', True, None), ('2026-10-19', 61, None),
])
def test_invalid_or_historical_vintage_never_starts_acquisition(monkeypatch, date, lookback, vintage):
    import tradingagents.strategies.data_sources.noaa_yearly_source as module
    monkeypatch.setattr(module, 'current_vintage_date', lambda: '2026-10-19', raising=False)
    with pytest.raises(SourceFetchError) as raised:
        module.NOAAYearlySource().fetch_ag_weather_summary(date, lookback, vintage_as_of=vintage)
    assert raised.value.partial_data['coverage']['complete'] is False
    assert raised.value.partial_data['heat_stress_days'] is None
    assert raised.value.partial_data['_coverage']['reason_code'] in ('invalid_response', 'provider_error')


def test_expired_parent_budget_withholds_metrics_before_network(monkeypatch):
    import tradingagents.strategies.data_sources.noaa_yearly_source as module
    monkeypatch.setattr(module, 'current_vintage_date', lambda: '2026-10-19', raising=False)
    with provider_budget('noaa', 10, clock=lambda: 11, limits=()):
        with pytest.raises(SourceFetchError) as raised:
            module.NOAAYearlySource().fetch_ag_weather_summary('2026-10-19')
    assert raised.value.reason_code == 'timeout'
    assert raised.value.partial_data['avg_daily_prcp'] is None

import gzip
import hashlib
from pathlib import Path

FIXTURES = Path(__file__).parent / 'fixtures' / 'noaa_ghcnd_yearly'


class StreamedResponse:
    """Only HTTP transport is replaced; parsing, budgets and gzip stay real."""
    status_code = 200

    def __init__(self, body, *, after_chunk=None, declared_length=None):
        self.body = body
        self.headers = {'Content-Length': str(len(body) if declared_length is None else declared_length)}
        self.closed = False
        self.consumed = 0
        self.after_chunk = after_chunk

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            chunk = self.body[offset:offset + chunk_size]
            self.consumed += len(chunk)
            yield chunk
            if self.after_chunk:
                self.after_chunk()

    def close(self):
        self.closed = True


@pytest.fixture
def wire(monkeypatch):
    import tradingagents.strategies.data_sources.noaa_yearly_source as module
    monkeypatch.setattr(module, 'current_vintage_date', lambda: '2026-10-19')
    files = {
        'ghcnd-stations.txt': (FIXTURES / 'stations-ten-state-original.txt').read_bytes(),
        'ghcnd-inventory.txt': (FIXTURES / 'inventory-ten-state-core-original.txt').read_bytes(),
        '2026.csv.gz': (FIXTURES / '2026-ten-state-37day-original.csv.gz').read_bytes(),
    }
    responses = []

    def get(url, **kwargs):
        assert kwargs.get('stream') is True
        key = url.rsplit('/', 1)[1]
        assert key in files, f'unexpected acquisition {key}'
        response = files[key]
        if not isinstance(response, StreamedResponse):
            response = StreamedResponse(response)
        responses.append(response)
        return response

    monkeypatch.setattr(module, '_yearly_http_get', get)
    return files, responses


def test_real_ten_state_summary_preserves_regional_days_and_actual_vintage(wire):
    from tradingagents.strategies.data_sources.noaa_yearly_source import NOAAYearlySource
    files, responses = wire
    result = NOAAYearlySource().fetch_ag_weather_summary('2026-10-18', vintage_as_of='2026-10-19')
    assert result['coverage']['complete'] is True
    assert result['states_reporting'] == 10
    assert result['observations'] == 900
    assert result['observation_lag_days'] == 2
    assert (result['start_date'], result['end_date']) == ('2026-09-17', '2026-10-16')
    assert result['heat_stress_days'] == 3
    assert result['frost_events'] == 0
    assert result['avg_tmax'] == 74.3
    assert result['avg_temp_anomaly_f'] == 29.3
    assert result['avg_daily_prcp'] == .131
    assert result['precip_deficit_pct'] == 64.1
    assert result['vintage_as_of'] == '2026-10-19'
    assert result['delivery_policy'] == 'ghcnd_yearly_v1'
    assert result['numeric_policy'] == 'ghcnd_metric_exact_to_standard_v1'
    assert result['coverage']['catalog_station_count'] == 10
    assert result['coverage']['catalog_stations_by_state'] == dict.fromkeys(('IL','IA','KS','NE','MN','IN','OH','SD','ND','MO'), 1)
    assert len(result['coverage']['files']) == 3
    annual = next(item for item in result['coverage']['files'] if item['kind'] == 'yearly_observations')
    assert annual['sha256'] == hashlib.sha256(files['2026.csv.gz']).hexdigest()
    assert annual['bytes'] == len(files['2026.csv.gz'])
    assert annual['integrity_complete'] is True
    assert all(response.closed and response.consumed == len(response.body) for response in responses)


@pytest.mark.parametrize('fault', ['duplicate', 'malformed_value', 'missing', 'quality', 'truncated', 'trailing_garbage'])
def test_real_parent_corruption_never_certifies_partial_weather(wire, fault):
    from tradingagents.strategies.data_sources.noaa_yearly_source import NOAAYearlySource
    files, responses = wire
    parent = files['2026.csv.gz']
    lines = gzip.decompress(parent).splitlines(keepends=True)
    target = next(i for i, row in enumerate(lines) if b',20260917,' in row)
    if fault == 'duplicate':
        lines.append(lines[target])
    elif fault == 'malformed_value':
        fields = lines[target].split(b','); fields[3] = b'NaN'; lines[target] = b','.join(fields)
    elif fault == 'missing':
        lines = [row for row in lines if b',TMIN,' not in row]
    elif fault == 'quality':
        for index, row in enumerate(lines):
            if b',TMIN,' in row:
                fields = row.split(b','); fields[5] = b'S'; lines[index] = b','.join(fields)
    files['2026.csv.gz'] = parent[:-8] if fault == 'truncated' else parent + b'garbage' if fault == 'trailing_garbage' else gzip.compress(b''.join(lines), mtime=0)
    with pytest.raises(SourceFetchError) as raised:
        NOAAYearlySource().fetch_ag_weather_summary('2026-10-18', vintage_as_of='2026-10-19')
    partial = raised.value.partial_data
    assert partial['coverage']['complete'] is False
    for field in ('heat_stress_days', 'frost_events', 'precip_deficit_pct', 'avg_temp_anomaly_f', 'avg_tmax', 'avg_daily_prcp'):
        assert partial[field] is None
    assert all(response.closed for response in responses)


def test_default_vintage_is_actual_current_date_not_reference_date(wire):
    from tradingagents.strategies.data_sources.noaa_yearly_source import NOAAYearlySource
    result = NOAAYearlySource().fetch_ag_weather_summary('2026-10-18')
    assert result['vintage_as_of'] == '2026-10-19'
    assert result['as_of'] == '2026-10-18'


def _append_catalog(files, key, fixture):
    # Only concatenates exact observed catalog lines, preserving bytes.
    rows = {row[:35] if key.endswith('inventory.txt') else row[:11]: row
            for row in files[key].splitlines(keepends=True)}
    for row in (FIXTURES / fixture).read_bytes().splitlines(keepends=True):
        rows[row[:35] if key.endswith('inventory.txt') else row[:11]] = row
    files[key] = b''.join(rows.values())


def test_real_trace_quality_and_signed_values_preserve_original_flags(wire):
    from tradingagents.strategies.data_sources.noaa_yearly_source import NOAAYearlySource
    files, _ = wire
    _append_catalog(files, 'ghcnd-stations.txt', 'observed-edge-case-stations-original.txt')
    _append_catalog(files, 'ghcnd-inventory.txt', 'observed-edge-case-inventory-original.txt')
    files['2026.csv.gz'] = gzip.compress(gzip.decompress(files['2026.csv.gz']) +
        (FIXTURES / 'observed-edge-cases-original.csv').read_bytes(), mtime=0)
    source = NOAAYearlySource()
    regional = source.fetch_region_daily('2026-09-10', '2026-10-16')
    observations = {row['station']: row for rows in regional.values() for row in rows
                    if row['station'] in ('USS0003E03S','USW00014852','USW00003929','USW00014918','USW00094931')}
    assert observations['USS0003E03S']['value'] == 73.4
    assert observations['USW00014852']['value'] == 0
    assert observations['USW00014852']['measurement_flag'] == 'T'
    assert observations['USW00014852']['observation_time'] == '2400'
    assert observations['USW00014918']['value'] == 32
    assert observations['USW00094931']['value'] == 28.94
    assert 'USW00003929' not in observations
    assert source.observation_exclusions['quality_flag'] == 1
    assert source.regional_coverage['trace_retained'] == 9


@pytest.mark.parametrize('cap,value', [
    ('MAX_COMPRESSED_BYTES', 4000), ('MAX_DECOMPRESSED_BYTES', 42000),
    ('MAX_YEARLY_ROWS', 1109), ('MAX_SELECTED_RECORDS', 100),
    ('MAX_REGIONAL_STATIONS', 9), ('MAX_CATALOG_BYTES', 100),
])
def test_real_parent_resource_caps_close_response_and_temporary_files(wire, monkeypatch, cap, value):
    import tradingagents.strategies.data_sources.noaa_yearly_source as module
    _, responses = wire
    opened = []
    original = module.tempfile.TemporaryFile

    def tracked_file(*args, **kwargs):
        handle = original(*args, **kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr(module.tempfile, 'TemporaryFile', tracked_file)
    monkeypatch.setattr(module, cap, value)
    with pytest.raises(SourceFetchError) as raised:
        module.NOAAYearlySource().fetch_ag_weather_summary('2026-10-18')
    assert raised.value.reason_code == 'invalid_response'
    assert all(handle.closed for handle in opened)
    assert all(response.closed for response in responses)


def test_real_parent_compression_bomb_row_is_bounded_and_closed(wire):
    from tradingagents.strategies.data_sources.noaa_yearly_source import NOAAYearlySource
    files, responses = wire
    rows = gzip.decompress(files['2026.csv.gz']).splitlines(keepends=True)
    # Fault control: inflate a real parent observation's value field; no fabricated positive baseline.
    fields = rows[0].split(b','); fields[3] = b'0' * 1_000_000; rows[0] = b','.join(fields)
    files['2026.csv.gz'] = gzip.compress(b''.join(rows), mtime=0)
    with pytest.raises(SourceFetchError) as raised:
        NOAAYearlySource().fetch_ag_weather_summary('2026-10-18')
    assert raised.value.reason_code == 'invalid_response'
    assert all(response.closed for response in responses)


def test_shorter_parent_deadline_survives_body_read_and_retains_diagnostics(wire):
    import time
    from tradingagents.strategies.data_sources.noaa_yearly_source import NOAAYearlySource
    files, responses = wire
    clock = [time.monotonic()]
    files['2026.csv.gz'] = StreamedResponse(files['2026.csv.gz'], after_chunk=lambda: clock.__setitem__(0, clock[0] + 2))
    with provider_budget('noaa', clock[0] + 1, clock=lambda: clock[0], limits=()) as diagnostics:
        with pytest.raises(SourceFetchError) as raised:
            NOAAYearlySource().fetch_ag_weather_summary('2026-10-18')
    assert raised.value.reason_code == 'timeout'
    assert len(diagnostics) == 3
    assert raised.value.partial_data['heat_stress_days'] is None
    assert all(response.closed for response in responses)


def test_crossyear_uses_both_true_archives_and_preserves_observed_missingness(wire):
    from tradingagents.strategies.data_sources.noaa_yearly_source import NOAAYearlySource
    files, responses = wire
    files['ghcnd-stations.txt'] = (FIXTURES / 'stations-crossyear-original.txt').read_bytes()
    files['ghcnd-inventory.txt'] = (FIXTURES / 'inventory-crossyear-core-original.txt').read_bytes()
    files['2025.csv.gz'] = (FIXTURES / '2025-crossyear-20251231-original.csv.gz').read_bytes()
    files['2026.csv.gz'] = (FIXTURES / '2026-crossyear-20260101-original.csv.gz').read_bytes()
    source = NOAAYearlySource()
    with pytest.raises(SourceFetchError) as raised:
        source.fetch_ag_weather_summary('2026-01-01', 2)
    assert raised.value.reason_code == 'batch_failure'
    assert '2026-01-01:TMIN' in raised.value.partial_data['coverage']['missing']['MN']
    annual = [file for file in source.regional_coverage['files'] if file['kind'] == 'yearly_observations']
    assert [file['year'] for file in annual] == [2025, 2026]
    assert [file['rows'] for file in annual] == [33, 32]
    assert all(response.closed for response in responses)


def test_file_provenance_binds_complete_bytes_to_actual_acquisition_times(wire):
    from tradingagents.strategies.data_sources.noaa_yearly_source import NOAAYearlySource
    result = NOAAYearlySource().fetch_ag_weather_summary('2026-10-18')
    for item in result['coverage']['files']:
        assert item['official_url'].startswith('https://www.ncei.noaa.gov/pub/data/ghcn/daily/')
        assert item['acquisition_started_at'] <= item['acquisition_completed_at'] <= result['acquired_at']
    assert len(result['coverage']['selected_raw_sha256']) == 64


def test_quality_exclusion_scope_ignores_real_flag_outside_window(wire):
    from tradingagents.strategies.data_sources.noaa_yearly_source import NOAAYearlySource
    files, _ = wire
    _append_catalog(files, 'ghcnd-stations.txt', 'observed-edge-case-stations-original.txt')
    _append_catalog(files, 'ghcnd-inventory.txt', 'observed-edge-case-inventory-original.txt')
    files['2026.csv.gz'] = gzip.compress(gzip.decompress(files['2026.csv.gz']) +
        (FIXTURES / 'observed-edge-cases-original.csv').read_bytes(), mtime=0)
    source = NOAAYearlySource()
    source.fetch_region_daily('2026-09-14', '2026-10-16')
    assert source.observation_exclusions['quality_flag'] == 0
    assert source.regional_coverage['exclusion_scope']['start_date'] == '2026-09-14'


def test_original_mixed_case_and_hyphen_catalog_ids_keep_precipitation_only_station(wire):
    from tradingagents.strategies.data_sources.noaa_yearly_source import NOAAYearlySource
    files, _ = wire
    _append_catalog(files, 'ghcnd-stations.txt', 'stations-id-and-partial-original.txt')
    _append_catalog(files, 'ghcnd-inventory.txt', 'inventory-id-and-partial-core-original.txt')
    files['2026.csv.gz'] = gzip.compress(gzip.decompress(files['2026.csv.gz']) +
        (FIXTURES / 'rainfall-only-us10adam008-37day-original.csv').read_bytes(), mtime=0)
    source = NOAAYearlySource()
    regional = source.fetch_region_daily('2026-09-10', '2026-10-16')
    assert source.regional_coverage['catalog_station_count'] == 11
    assert 'BR00A6-0010' not in source.regional_coverage['catalog_station_ids']
    assert 'US10adam008' in source.regional_coverage['catalog_station_ids']
    rain = [row for row in regional['NE'] if row['station'] == 'US10adam008']
    assert len(rain) == 37
    assert {row['datatype'] for row in rain} == {'PRCP'}


def test_real_second_station_uses_equal_state_day_means_and_matches_independent_dly(wire):
    from fractions import Fraction
    from datetime import date
    from tradingagents.strategies.data_sources.noaa_yearly_source import NOAAYearlySource
    files, _ = wire
    _append_catalog(files, 'ghcnd-stations.txt', 'stations-independent-oracle-original.txt')
    _append_catalog(files, 'ghcnd-inventory.txt', 'inventory-independent-oracle-core-original.txt')
    files['2026.csv.gz'] = gzip.compress(gzip.decompress(files['2026.csv.gz']) +
        (FIXTURES / 'usw00014933-20260910-20261015-annual-original.csv').read_bytes(), mtime=0)
    source = NOAAYearlySource()
    regional = source.fetch_region_daily('2026-09-10', '2026-10-16')
    actual = {(row['date'], row['datatype']): row for row in regional['IA'] if row['station'] == 'USW00014933'}
    assert len(actual) == 108
    assert ('2026-10-16', 'TMAX') not in actual
    checked = 0
    for line in (FIXTURES / 'usw00014933-202609-202610-core-original.dly').read_text().splitlines():
        year, month, element = int(line[11:15]), int(line[15:17]), line[17:21]
        for day in range(1, 32):
            field = line[21 + (day - 1) * 8:21 + day * 8]
            raw = int(field[:5])
            if raw == -9999:
                continue
            key = (date(year, month, day).isoformat(), element)
            if key not in actual:
                continue
            expected = Fraction(raw, 254) if element == 'PRCP' else Fraction(raw * 9, 50) + 32
            assert actual[key]['value'] == float(expected)
            assert actual[key]['measurement_flag'] == field[5].strip()
            assert actual[key]['quality_flag'] == field[6].strip()
            assert actual[key]['source_flag'] == field[7].strip()
            checked += 1
    assert checked == 108
    summary = source.fetch_ag_weather_summary('2026-10-18')
    assert summary['observations'] == 987
    assert summary['avg_tmax'] == 74.4
    assert summary['avg_daily_prcp'] == .126
    assert summary['precip_deficit_pct'] == 57.5


@pytest.mark.parametrize('fault', ['duplicate_catalog', 'bad_inventory_year', 'empty_value', 'missing_sentinel', 'invalid_day', 'negative_rain'])
def test_selected_real_parent_validation_rejects_inconsistent_or_missing_records(wire, fault):
    from tradingagents.strategies.data_sources.noaa_yearly_source import NOAAYearlySource
    files, _ = wire
    if fault == 'duplicate_catalog':
        files['ghcnd-stations.txt'] += files['ghcnd-stations.txt'].splitlines(keepends=True)[0]
    elif fault == 'bad_inventory_year':
        line = files['ghcnd-inventory.txt'].splitlines(keepends=True)[0]
        files['ghcnd-inventory.txt'] = files['ghcnd-inventory.txt'].replace(line, line[:36] + b'nope' + line[40:], 1)
    else:
        rows = gzip.decompress(files['2026.csv.gz']).splitlines(keepends=True)
        for index, row in enumerate(rows):
            fields = row.split(b',')
            if fault == 'missing_sentinel' and fields[2] == b'TMIN':
                fields[3] = b'-9999'
            elif fault != 'missing_sentinel' and index == 300:
                if fault == 'empty_value': fields[3] = b''
                elif fault == 'invalid_day': fields[1] = b'20260931'
                elif fault == 'negative_rain': fields[2], fields[3] = b'PRCP', b'-1'
            rows[index] = b','.join(fields)
        files['2026.csv.gz'] = gzip.compress(b''.join(rows), mtime=0)
    with pytest.raises(SourceFetchError) as raised:
        NOAAYearlySource().fetch_ag_weather_summary('2026-10-18')
    assert raised.value.partial_data['coverage']['complete'] is False
    assert raised.value.partial_data['avg_daily_prcp'] is None


def test_http_failure_dispatch_retains_safe_typed_status_without_payload_or_url(wire):
    import time
    from tradingagents.strategies.data_sources.noaa_yearly_source import NOAAYearlySource
    files, responses = wire
    failed = StreamedResponse(b'credential=secret https://private.invalid/details')
    failed.status_code = 503
    files['2026.csv.gz'] = failed
    with provider_budget('noaa', time.monotonic() + 10, max_attempts=1, limits=()):
        result = NOAAYearlySource().fetch({'date': '2026-10-18'})
    assert result['_coverage']['reason_code'] == 'http_error'
    assert result['_coverage']['http_status'] == 503
    assert result['heat_stress_days'] is None
    assert 'secret' not in str(result)
    assert 'private.invalid' not in str(result)
    assert all(response.closed for response in responses)


def test_hard_transport_deadline_interrupts_trickled_real_fixture_and_cleans_storage(monkeypatch):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import tradingagents.strategies.data_sources.noaa_yearly_source as module
    from tradingagents.strategies.data_sources.request_policy import provider_request
    assert callable(getattr(module, '_yearly_http_get', None))
    body = (FIXTURES / '2026-ten-state-37day-original.csv.gz').read_bytes()
    import subprocess
    directories, processes = [], []
    original_directory, original_process = module.tempfile.TemporaryDirectory, subprocess.Popen

    def temporary_directory(*args, **kwargs):
        result = original_directory(*args, **kwargs); directories.append(Path(result.name)); return result

    def spawn(*args, **kwargs):
        result = original_process(*args, **kwargs); processes.append(result); return result

    monkeypatch.setattr(module.tempfile, 'TemporaryDirectory', temporary_directory)
    monkeypatch.setattr(subprocess, 'Popen', spawn)

    began_body = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            began_body.set()
            self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
            try:
                for byte in body:
                    self.wfile.write(bytes([byte])); self.wfile.flush(); time.sleep(.03)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        with provider_budget('noaa', started + .6, max_attempts=1, limits=()) as diagnostics:
            with pytest.raises(SourceFetchError) as raised:
                provider_request('noaa', 'GET', f'http://127.0.0.1:{server.server_port}/retained.csv.gz',
                    transport=module._yearly_http_get, operation='yearly_observations',
                    stream=True, allow_redirects=False, headers={'Accept-Encoding': 'identity'},
                    timeout=(.1, .15), max_bytes=10000)
        elapsed = time.monotonic() - started
        assert raised.value.reason_code == 'timeout'
        assert elapsed < 1.6
        assert began_body.is_set()
        assert diagnostics[-1]['reason_code'] == 'timeout'
        assert directories and all(not directory.exists() for directory in directories)
        assert processes and all(process.poll() is not None for process in processes)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=1)


@pytest.mark.parametrize('mode', ['complete', 'cap', 'truncated_http', 'retry_http'])
def test_native_worker_bounds_bytes_retries_status_and_owns_success_file(monkeypatch, mode):
    import subprocess
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import tradingagents.strategies.data_sources.noaa_yearly_source as module
    from tradingagents.strategies.data_sources.request_policy import provider_request
    parent = (FIXTURES / '2026-ten-state-37day-original.csv.gz').read_bytes()
    requests_seen, spawned, directories = [], [], []
    original_process, original_directory = subprocess.Popen, module.tempfile.TemporaryDirectory

    def spawn(command, **kwargs):
        assert set(kwargs['env']) == {'LANG', 'LC_ALL', 'PYTHONIOENCODING'}
        assert kwargs['stderr'] == subprocess.DEVNULL
        child = original_process(command, **kwargs); spawned.append(child); return child

    def temporary_directory(*args, **kwargs):
        directory = original_directory(*args, **kwargs); directories.append(Path(directory.name)); return directory

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    monkeypatch.setattr(module.tempfile, 'TemporaryDirectory', temporary_directory)
    monkeypatch.setenv('NOAA_CDO_TOKEN', 'test-secret-must-not-enter-child')
    monkeypatch.setenv('HTTPS_PROXY', 'http://credential-must-not-enter-child.invalid')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests_seen.append(self.path)
            status = 503 if mode == 'retry_http' and len(requests_seen) == 1 else 200
            self.send_response(status)
            self.send_header('Content-Length', str(len(parent)))
            self.end_headers()
            if status == 200:
                self.wfile.write(parent[:-8] if mode == 'truncated_http' else parent)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with provider_budget('noaa', time.monotonic() + 10,
                max_attempts=2 if mode == 'retry_http' else 1, limits=()) as diagnostics:
            options = dict(transport=module._yearly_http_get, operation='yearly_observations',
                stream=True, allow_redirects=False, headers={'Accept-Encoding': 'identity'},
                timeout=(1, 1), max_bytes=len(parent) - 1 if mode == 'cap' else len(parent))
            if mode in ('cap', 'truncated_http'):
                with pytest.raises(SourceFetchError) as raised:
                    provider_request('noaa', 'GET', f'http://127.0.0.1:{server.server_port}/original.csv.gz', **options)
                assert raised.value.reason_code == ('invalid_response' if mode == 'cap' else 'transport_error')
                assert diagnostics[-1]['reason_code'] == raised.value.reason_code
            else:
                response = provider_request('noaa', 'GET', f'http://127.0.0.1:{server.server_port}/original.csv.gz', **options)
                assert b''.join(response.iter_content(chunk_size=1024)) == parent
                assert response.body is not None and not response.body.closed
                assert response.directory.name and Path(response.directory.name).exists()
                response.close()
                assert response.body.closed
                assert diagnostics[-1]['reason_code'] is None
                assert diagnostics[-1]['attempts'] == (2 if mode == 'retry_http' else 1)
        assert spawned and all(child.poll() is not None for child in spawned)
        assert directories and all(not directory.exists() for directory in directories)
        assert len(requests_seen) == (2 if mode == 'retry_http' else 1)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=1)


@pytest.mark.parametrize('optional_flag', [0x08, 0x10], ids=['filename', 'comment'])
def test_real_parent_optional_gzip_header_cannot_outrun_decode_deadline(wire, monkeypatch, optional_flag):
    """Fault control inserts a legal optional header into the retained gzip.

    Advancing only on compressed one-byte reads distinguishes header parsing
    from transport, decoded row checks, and the later full-body EOF check.
    """
    import time
    import tradingagents.strategies.data_sources.noaa_yearly_source as module
    files, responses = wire
    parent = files['2026.csv.gz']
    assert parent[:4] == b'\x1f\x8b\x08\x00'
    mutated = bytearray(parent[:10]); mutated[3] |= optional_flag
    files['2026.csv.gz'] = bytes(mutated) + b'a' * 16_384 + b'\x00' + parent[10:]
    clock, compressed_byte_reads, opened = [time.monotonic()], [0], []
    original_file = module.tempfile.TemporaryFile

    class ClockedCompressedFile:
        def __init__(self, handle):
            self.handle = handle

        def read(self, size=-1):
            if size == 1:
                compressed_byte_reads[0] += 1
                clock[0] += .001
            return self.handle.read(size)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

    def temporary(*args, **kwargs):
        handle = original_file(*args, **kwargs); opened.append(handle)
        return ClockedCompressedFile(handle)

    monkeypatch.setattr(module.tempfile, 'TemporaryFile', temporary)
    with provider_budget('noaa', clock[0] + .02, clock=lambda: clock[0], limits=()):
        with pytest.raises(SourceFetchError) as raised:
            module.NOAAYearlySource().fetch_ag_weather_summary('2026-10-18')
    assert raised.value.reason_code == 'timeout'
    assert 1 <= compressed_byte_reads[0] <= 25
    assert raised.value.partial_data['heat_stress_days'] is None
    assert all(response.closed for response in responses)
    assert opened and all(handle.closed for handle in opened)
