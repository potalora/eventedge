"""Explicit official yearly GHCNd acquisition with bounded full-body validation.

The supported strategy window is 1..60 days, plus up to seven days of lag.
Metric integers use exact mathematical conversion, not Access JSON rounding.
Archives are mutable: a reference observation date never claims a past vintage.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date as calendar_date, datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import re
import tempfile
import time
from typing import Any
from zoneinfo import ZoneInfo


from .fetch_errors import SourceFetchError, source_fetch_error
from .noaa_source import AG_STATES, GROWING_SEASON, HEAT_STRESS_F, FROST_THRESHOLD_F
from .request_policy import provider_subbudget, provider_request, provider_timeout

CATALOG_URL = 'https://www.ncei.noaa.gov/pub/data/ghcn/daily'
YEARLY_URL = CATALOG_URL + '/by_year'
DELIVERY_POLICY = 'ghcnd_yearly_v1'
NUMERIC_POLICY = 'ghcnd_metric_exact_to_standard_v1'
REGIONAL_BUDGET_SECONDS = 180
MAX_OBSERVATION_LAG_DAYS = 7
MAX_LOOKBACK_DAYS = 60
MAX_YEARS = 2
MAX_REGIONAL_STATIONS = 12_000
MAX_CATALOG_STATIONS = 200_000
MAX_CATALOG_ROWS = 1_000_000
MAX_CATALOG_BYTES = 64 * 1024 * 1024
MAX_COMPRESSED_BYTES = 256 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_YEARLY_ROWS = 40_000_000
MAX_SELECTED_RECORDS = 2_000_000
MAX_ROW_BYTES = 1024
CHUNK_BYTES = 64 * 1024
ELEMENTS = ('TMAX', 'TMIN', 'PRCP')
_METRICS = ('heat_stress_days', 'frost_events', 'precip_deficit_pct',
            'avg_temp_anomaly_f', 'avg_tmax', 'avg_daily_prcp')
_STATION = re.compile(r'[A-Za-z0-9_-]{11}\Z')
_INTEGER = re.compile(rb'-?\d{1,6}\Z')


def current_vintage_date() -> str:
    """Actual New York calendar date; independent from requested observations."""
    return datetime.now(ZoneInfo('America/New_York')).date().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _invalid(message='NOAA yearly response invalid'):
    return SourceFetchError(message, reason_code='invalid_response')


class _LocalHTTPResponse:
    """A completed body with explicit ownership of its private directory."""

    def __init__(self, status_code, headers, directory, body=None):
        self.status_code, self.headers = status_code, headers
        self.directory, self.body = directory, body

    def iter_content(self, chunk_size):
        if self.body is not None:
            while chunk := self.body.read(chunk_size):
                yield chunk

    def close(self):
        try:
            if self.body is not None:
                self.body.close()
        finally:
            self.directory.cleanup()


def _yearly_http_get(url, *, stream, allow_redirects, timeout, headers, max_bytes):
    """Acquire in a killable worker; the original budget includes DNS/body.

    Kill/reap and filesystem cleanup can require a small scheduling allowance
    after the deadline. No network child or resolver survives a timed-out call.
    The synchronous provider limiter owns retries and physical request starts.
    """
    provider_timeout('noaa')
    directory = tempfile.TemporaryDirectory(prefix='eventedge-noaa-http-')
    child, owned = None, True
    try:
        output = Path(directory.name) / 'body'
        command = [sys.executable, str(Path(__file__).with_name('noaa_yearly_transport.py')),
            '--url', url, '--output', str(output), '--max-bytes', str(max_bytes),
            '--connect-timeout', str(timeout[0]), '--read-timeout', str(timeout[1])]
        # Credentials, proxy variables, HOME, PYTHONPATH and client config never
        # enter the network worker. Its own client also disables trust_env.
        child_env = {'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PYTHONIOENCODING': 'utf-8'}
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=child_env, close_fds=True)
        try:
            metadata, _ = child.communicate(timeout=provider_timeout('noaa', maximum=REGIONAL_BUDGET_SECONDS))
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            raise SourceFetchError('NOAA network acquisition deadline exhausted', reason_code='timeout') from None
        provider_timeout('noaa')
        if child.returncode or not metadata or len(metadata) > 4096:
            raise _invalid('NOAA network worker metadata invalid')
        try:
            result = json.loads(metadata)
            if not isinstance(result, dict):
                raise ValueError
            reason = result.get('reason_code')
            if reason is not None:
                if reason not in ('timeout', 'transport_error', 'invalid_response', 'provider_error'):
                    raise ValueError
                raise SourceFetchError('NOAA network acquisition failed', reason_code=reason)
            status, safe_headers = result['status_code'], result['headers']
            if type(status) is not int or not 100 <= status <= 599 or not isinstance(safe_headers, dict):
                raise ValueError
            if set(safe_headers) - {'Content-Length', 'Content-Encoding', 'Retry-After'} or any(
                    not isinstance(value, str) or len(value) > 64 for value in safe_headers.values()):
                raise ValueError
            body = None
            if 200 <= status < 300:
                count, digest = result['bytes'], result['sha256']
                if type(count) is not int or not 0 < count <= max_bytes or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                    raise ValueError
                if output.stat().st_size != count:
                    raise ValueError
                body = output.open('rb')
            response = _LocalHTTPResponse(status, safe_headers, directory, body)
            owned = False
            return response
        except (ValueError, TypeError, KeyError, OSError):
            raise _invalid('NOAA network worker metadata invalid') from None
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate()
        if owned:
            directory.cleanup()


class _DeadlineCompressedInput:
    """Check the shared budget inside gzip header, body and padding reads.

    Gzip may consume optional filename/comment headers one byte at a time
    before producing any row. Row-level checks alone cannot bound that work.
    The acquisition context retains ownership of the underlying temporary file.
    """

    def __init__(self, body):
        self.body = body

    def read(self, size=-1):
        provider_timeout('noaa')
        data = self.body.read(size)
        provider_timeout('noaa')
        return data

    def seek(self, offset, whence=0):
        provider_timeout('noaa')
        result = self.body.seek(offset, whence)
        provider_timeout('noaa')
        return result

    def tell(self):
        provider_timeout('noaa')
        return self.body.tell()


class NOAAYearlySource:
    name = 'noaa'
    requires_api_key = False

    def __init__(self):
        self.regional_coverage: dict[str, Any] = {}
        self.observation_exclusions: dict[str, int] = {}

    def is_available(self) -> bool:
        return True

    def clear_cache(self) -> None:
        # Mutable annual files are deliberately never reused across acquisitions.
        self.regional_coverage = {}
        self.observation_exclusions = {}

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if params.get('method', 'ag_weather_summary') != 'ag_weather_summary':
                raise _invalid('NOAA yearly method unsupported')
            return self.fetch_ag_weather_summary(params.get('date', current_vintage_date()),
                params.get('lookback_days', 30), vintage_as_of=params.get('vintage_as_of'))
        except SourceFetchError as exc:
            return {**self._failure(exc).partial_data, 'error': str(exc)}

    def _failure(self, exc: SourceFetchError) -> SourceFetchError:
        partial = dict(exc.partial_data)
        coverage = {**self.regional_coverage, **partial.get('coverage', {}), 'complete': False}
        partial.update({field: None for field in _METRICS})
        partial.update({'coverage': coverage, 'delivery_policy': DELIVERY_POLICY, 'numeric_policy': NUMERIC_POLICY,
            '_coverage': {'status': 'failed', 'reason_code': exc.reason_code, 'http_status': exc.http_status,
                'attempts': exc.attempts, 'failed_operations': exc.failed_operations,
                'failed_http_statuses': exc.failed_http_statuses}})
        exc.partial_data = partial
        return exc

    @contextmanager
    def _budget(self):
        provider_timeout('noaa')
        with provider_subbudget('noaa', maximum_seconds=REGIONAL_BUDGET_SECONDS,
                absolute_deadline=time.monotonic() + REGIONAL_BUDGET_SECONDS):
            provider_timeout('noaa')
            yield
            provider_timeout('noaa')

    @contextmanager
    def _download(self, filename: str, kind: str, limit: int, *, year=None):
        """Hash bounded bytes to anonymous disk storage and close on every path."""
        provider_timeout('noaa')
        url = f'{YEARLY_URL}/{filename}' if year is not None else f'{CATALOG_URL}/{filename}'
        started = _now()
        response = provider_request('noaa', 'GET', url, operation=kind,
            transport=_yearly_http_get, stream=True, allow_redirects=False, timeout=(5, 15),
            headers={'Accept-Encoding': 'identity'}, max_bytes=limit)
        try:
            headers = getattr(response, 'headers', {})
            length = headers.get('Content-Length')
            if length is not None:
                if not isinstance(length, str) or not length.isdigit() or len(length) > 12:
                    raise _invalid('NOAA response length invalid')
                length = int(length)
                if length > limit:
                    raise _invalid('NOAA response exceeds byte limit')
            if headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                raise _invalid('NOAA response encoding unsupported')
            with tempfile.TemporaryFile(mode='w+b') as body:
                digest, received = hashlib.sha256(), 0
                for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                    provider_timeout('noaa')
                    if not isinstance(chunk, bytes):
                        raise _invalid('NOAA response bytes invalid')
                    received += len(chunk)
                    if received > limit:
                        raise _invalid('NOAA response exceeds byte limit')
                    digest.update(chunk)
                    body.write(chunk)
                provider_timeout('noaa')
                if not received or length is not None and received != length:
                    raise _invalid('NOAA response incomplete')
                evidence = {'kind': kind, 'bytes': received, 'sha256': digest.hexdigest(),
                    'acquired_at': _now(), 'integrity_complete': False, 'official_url': url,
                    'acquisition_started_at': started, 'acquisition_completed_at': _now()}
                if year is not None:
                    evidence['year'] = year
                self.regional_coverage.setdefault('files', []).append(evidence)
                body.seek(0)
                yield body, evidence
        except SourceFetchError:
            raise
        except Exception as exc:
            raise source_fetch_error('NOAA yearly acquisition failed', exc) from None
        finally:
            response.close()

    def _catalog_lines(self, body, *, ascii_prefix=40):
        count = 0
        while True:
            line = body.readline(MAX_ROW_BYTES + 1)
            if not line:
                break
            count += 1
            if len(line) > MAX_ROW_BYTES or count > MAX_CATALOG_ROWS:
                raise _invalid('NOAA catalog row limit exceeded')
            if count % 1024 == 0:
                provider_timeout('noaa')
            try:
                yield (line[:ascii_prefix].decode('ascii') + line[ascii_prefix:].decode('utf-8')).rstrip('\r\n')
            except UnicodeError:
                raise _invalid('NOAA catalog text invalid') from None
        provider_timeout('noaa')

    def _station_catalog(self, start: str, end: str):
        stations = {}
        with self._download('ghcnd-stations.txt', 'station_catalog', MAX_CATALOG_BYTES) as (body, evidence):
            for line in self._catalog_lines(body):
                station = line[:11]
                if len(line) < 40 or not _STATION.fullmatch(station) or station in stations:
                    raise _invalid('NOAA station catalog inconsistent')
                stations[station] = line[38:40]
                if len(stations) > MAX_CATALOG_STATIONS:
                    raise _invalid('NOAA station catalog exceeds limit')
            evidence['integrity_complete'] = True
        selected, inventory_seen = set(), set()
        with self._download('ghcnd-inventory.txt', 'element_inventory', MAX_CATALOG_BYTES) as (body, evidence):
            for line in self._catalog_lines(body, ascii_prefix=45):
                station, element = line[:11], line[31:35]
                if element not in ELEMENTS:
                    continue
                if len(line) < 45 or station not in stations:
                    raise _invalid('NOAA element inventory inconsistent')
                if stations[station] not in AG_STATES:
                    continue
                identity = (station, element)
                if identity in inventory_seen:
                    raise _invalid('NOAA element inventory repeated')
                inventory_seen.add(identity)
                try:
                    first, last = int(line[36:40]), int(line[41:45])
                except ValueError:
                    raise _invalid('NOAA element inventory years invalid') from None
                if not 1700 <= first <= last <= 9999:
                    raise _invalid('NOAA element inventory years invalid')
                if first <= int(end[:4]) and last >= int(start[:4]):
                    selected.add(station)
                    if len(selected) > MAX_REGIONAL_STATIONS:
                        raise _invalid('NOAA regional station limit exceeded')
            evidence['integrity_complete'] = True
        if not selected or set(AG_STATES) - {stations[s] for s in selected}:
            raise _invalid('NOAA regional station catalog incomplete')
        identities = sorted(selected)
        self.regional_coverage.update({'catalog_station_count': len(selected), 'catalog_station_ids': identities,
            'station_selection_sha256': hashlib.sha256(('\n'.join(identities) + '\n').encode('ascii')).hexdigest(),
            'catalog_stations_by_state': {state: sum(stations[s] == state for s in selected) for state in AG_STATES},
            'catalog_selection': 'all_requested_state_stations_with_element_inventory_overlapping_observation_years'})
        return {station.encode('ascii'): stations[station] for station in selected}

    def fetch_region_daily(self, start: str, end: str) -> dict[str, list[dict]]:
        """Acquire complete gzip bodies; select all eligible station identities."""
        self.regional_coverage = {'acquisition_path': 'ncei_ghcnd_yearly', 'files': [],
            'acquisition_budget_seconds': REGIONAL_BUDGET_SECONDS, 'delivery_policy': DELIVERY_POLICY,
            'numeric_policy': NUMERIC_POLICY, 'access_json_rounding_parity_proven': False}
        self.observation_exclusions = {'quality_flag': 0, 'missing_value': 0}
        try:
            first, last = calendar_date.fromisoformat(start), calendar_date.fromisoformat(end)
            if first > last or (last-first).days >= MAX_LOOKBACK_DAYS + MAX_OBSERVATION_LAG_DAYS:
                raise _invalid('NOAA regional window invalid')
            years = list(range(first.year, last.year + 1))
            if len(years) > MAX_YEARS:
                raise _invalid('NOAA yearly file count exceeds limit')
            with self._budget():
                stations = self._station_catalog(start, end)
                regional = {state: [] for state in AG_STATES}
                seen = set()
                raw_digest = hashlib.sha256()
                flag_counts = {'measurement': {}, 'source': {}}
                trace_retained = 0
                raw_start, raw_end = start.replace('-', '').encode('ascii'), end.replace('-', '').encode('ascii')
                for year in years:
                    with self._download(f'{year}.csv.gz', 'yearly_observations', MAX_COMPRESSED_BYTES, year=year) as (body, evidence):
                        row_count, decompressed = 0, 0
                        try:
                            with gzip.GzipFile(fileobj=_DeadlineCompressedInput(body), mode='rb') as decoded:
                                while True:
                                    line = decoded.readline(MAX_ROW_BYTES + 1)
                                    if not line:
                                        break  # gzip validates CRC, size and concatenated-member EOF here.
                                    row_count += 1
                                    decompressed += len(line)
                                    if row_count % 4096 == 0:
                                        provider_timeout('noaa')
                                    if len(line) > MAX_ROW_BYTES or row_count > MAX_YEARLY_ROWS or decompressed > MAX_DECOMPRESSED_BYTES:
                                        raise _invalid('NOAA yearly decoded resource limit exceeded')
                                    # Fast identity filtering keeps full annual traversal bounded in CPU.
                                    identity = line.split(b',', 3)
                                    if identity[0] not in stations:
                                        continue
                                    if len(identity) < 4 or len(identity[1]) != 8 or not identity[1].isdigit():
                                        raise _invalid('NOAA selected observation date invalid')
                                    if not raw_start <= identity[1] <= raw_end or identity[2] not in (b'TMAX', b'TMIN', b'PRCP'):
                                        continue
                                    fields = line.rstrip(b'\r\n').split(b',')
                                    if len(fields) != 8 or not _INTEGER.fullmatch(fields[3]) or any(len(flag) > 1 for flag in fields[4:7]):
                                        raise _invalid('NOAA selected observation invalid')
                                    if fields[7] and (len(fields[7]) != 4 or not fields[7].isdigit()):
                                        raise _invalid('NOAA observation time invalid')
                                    try:
                                        day = datetime.strptime(fields[1].decode('ascii'), '%Y%m%d').date()
                                        if day.year != year:
                                            raise ValueError
                                    except ValueError:
                                        raise _invalid('NOAA selected observation date invalid') from None
                                    key = (fields[0], fields[1], fields[2])
                                    if key in seen:
                                        raise _invalid('NOAA selected observation repeated')
                                    seen.add(key)
                                    raw_digest.update(line)
                                    for group, flag in (('measurement', fields[4]), ('source', fields[6])):
                                        label = flag.decode('ascii')
                                        flag_counts[group][label] = flag_counts[group].get(label, 0) + 1
                                    if len(seen) > MAX_SELECTED_RECORDS:
                                        raise _invalid('NOAA selected record limit exceeded')
                                    if fields[5].strip():
                                        self.observation_exclusions['quality_flag'] += 1
                                        continue
                                    value = int(fields[3])
                                    if value == -9999:
                                        self.observation_exclusions['missing_value'] += 1
                                        continue
                                    if fields[2] == b'PRCP' and value < 0:
                                        raise _invalid('NOAA precipitation invalid')
                                    if fields[4] == b'T':
                                        trace_retained += 1
                                    value = value / 254 if fields[2] == b'PRCP' else (value * 9 + 1600) / 50
                                    regional[stations[fields[0]]].append({'date': day.isoformat(),
                                        'station': fields[0].decode('ascii'), 'datatype': fields[2].decode('ascii'), 'value': value,
                                        'measurement_flag': fields[4].decode('ascii'), 'quality_flag': fields[5].decode('ascii'),
                                        'source_flag': fields[6].decode('ascii'), 'observation_time': fields[7].decode('ascii')})
                        except (gzip.BadGzipFile, EOFError, OSError):
                            raise _invalid('NOAA yearly gzip integrity invalid') from None
                        provider_timeout('noaa')
                        evidence.update({'integrity_complete': True, 'rows': row_count, 'decompressed_bytes': decompressed})
                self.regional_coverage.update({'exclusions': dict(self.observation_exclusions),
                    'exclusion_scope': {'start_date': start, 'end_date': end, 'selected_stations_only': True,
                        'datatypes': list(ELEMENTS)}, 'selected_records': len(seen), 'selected_raw_sha256': raw_digest.hexdigest(),
                    'flag_counts': flag_counts, 'trace_retained': trace_retained})
                return regional
        except (TypeError, ValueError):
            raise self._failure(_invalid('NOAA regional window invalid')) from None
        except SourceFetchError as exc:
            raise self._failure(exc) from None

    def fetch_ag_weather_summary(self, date: str, lookback_days: int = 30, *, vintage_as_of: str | None = None) -> dict[str, Any]:
        self.clear_cache()
        try:
            end = calendar_date.fromisoformat(date)
            vintage = current_vintage_date() if vintage_as_of is None else vintage_as_of
            version = calendar_date.fromisoformat(vintage)
            if type(lookback_days) is not int or not 1 <= lookback_days <= MAX_LOOKBACK_DAYS or end > version:
                raise _invalid('NOAA observation window invalid')
            if vintage != current_vintage_date():
                raise SourceFetchError('NOAA historical vintage unavailable', reason_code='provider_error')
            with self._budget():
                fetch_start = end - timedelta(days=lookback_days - 1 + MAX_OBSERVATION_LAG_DAYS)
                regional = self.fetch_region_daily(fetch_start.isoformat(), date)
                return self._summary(regional, end, lookback_days, vintage)
        except (TypeError, ValueError):
            raise self._failure(_invalid('NOAA observation window invalid')) from None
        except SourceFetchError as exc:
            raise self._failure(exc) from None

    def _summary(self, regional, reference, lookback, vintage):
        groups = {}
        for state in AG_STATES:
            grouped = {}
            for index, row in enumerate(regional[state]):
                if index % 4096 == 0:
                    provider_timeout('noaa')
                grouped.setdefault((row['date'], row['datatype']), []).append(row['value'])
            groups[state] = grouped
        selected_lag, selected_end = None, reference
        for lag in range(MAX_OBSERVATION_LAG_DAYS + 1):
            candidate_end = reference - timedelta(days=lag)
            candidate_start = candidate_end - timedelta(days=lookback - 1)
            expected = {((candidate_start + timedelta(days=i)).isoformat(), dtype)
                for i in range(lookback) for dtype in ELEMENTS}
            if all(expected <= grouped.keys() for grouped in groups.values()):
                selected_lag, selected_end = lag, candidate_end
                break
        if selected_lag is None:
            expected = {((reference - timedelta(days=i)).isoformat(), dtype) for i in range(lookback) for dtype in ELEMENTS}
        start = selected_end - timedelta(days=lookback - 1)
        in_season = GROWING_SEASON[0] <= selected_end.month <= GROWING_SEASON[1]
        heat_dates, frost_dates, affected, tmax, prcp, missing, reporting = set(), set(), {}, [], [], {}, []
        observations = 0
        for state, grouped in groups.items():
            provider_timeout('noaa')
            absent = expected - grouped.keys()
            if absent:
                missing[state] = sorted(f'{day}:{dtype}' for day, dtype in absent)
            selected = {key: values for key, values in grouped.items() if key in expected}
            if selected:
                reporting.append(state)
            for (day, dtype), values in selected.items():
                value = sum(values) / len(values)
                observations += len(values)
                if dtype == 'TMAX':
                    tmax.append(value)
                    if value > HEAT_STRESS_F:
                        heat_dates.add(day)
                        affected.setdefault(day, {}).setdefault('heat', []).append(state)
                elif dtype == 'TMIN':
                    observed = calendar_date.fromisoformat(day)
                    if value < FROST_THRESHOLD_F and GROWING_SEASON[0] <= observed.month <= GROWING_SEASON[1] and observed > observed.replace(month=4, day=15):
                        frost_dates.add(day)
                        affected.setdefault(day, {}).setdefault('frost', []).append(state)
                else:
                    prcp.append(value)
        avg_tmax = sum(tmax) / len(tmax) if tmax else None
        avg_prcp = sum(prcp) / len(prcp) if prcp else None
        normal_tmax, normal_prcp = (85., .12) if in_season else (45., .08)
        acquired = _now()
        summary = {'heat_stress_days': len(heat_dates), 'frost_events': len(frost_dates),
            'precip_deficit_pct': round((avg_prcp - normal_prcp) / normal_prcp * 100, 1) if avg_prcp is not None else None,
            'avg_temp_anomaly_f': round(avg_tmax - normal_tmax, 1) if avg_tmax is not None else None,
            'avg_tmax': round(avg_tmax, 1) if avg_tmax is not None else None,
            'avg_daily_prcp': round(avg_prcp, 3) if avg_prcp is not None else None,
            'states_reporting': len(reporting), 'in_growing_season': in_season, 'lookback_days': lookback,
            'observations': observations, 'day_unit': 'regional_days', 'affected_states_by_date': affected,
            'acquired_at': acquired, 'available_at': acquired, 'as_of': reference.isoformat(), 'vintage_as_of': vintage,
            'observation_lag_days': selected_lag, 'observation_date': selected_end.isoformat(),
            'start_date': start.isoformat(), 'end_date': selected_end.isoformat(),
            'delivery_policy': DELIVERY_POLICY, 'numeric_policy': NUMERIC_POLICY,
            'coverage': {**self.regional_coverage, 'mode': 'state_date_datatype_sample', 'complete': not missing,
                'requested_states': list(AG_STATES), 'states_reporting': reporting,
                'window_start': start.isoformat(), 'window_end': selected_end.isoformat(), 'as_of': reference.isoformat(),
                'observation_lag_days': selected_lag, 'max_observation_lag_days': MAX_OBSERVATION_LAG_DAYS,
                'missing': missing, 'station_aggregation': 'arithmetic_mean_per_state_date_datatype'}}
        if missing:
            raise SourceFetchError('NOAA state coverage incomplete', reason_code='batch_failure',
                failed_operations={state: 'invalid_response' for state in missing}, partial_data=summary)
        provider_timeout('noaa')
        return summary
