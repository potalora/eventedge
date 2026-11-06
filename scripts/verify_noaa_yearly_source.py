#!/usr/bin/env python3
"""Replay pinned NOAA bytes through the selected adapter with external IO denied."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import socket
import stat
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / 'tests/fixtures/noaa_ghcnd_yearly'
REFERENCE = '2026-10-18'
REPLAY_VINTAGE = '2026-10-19'


class AcceptanceError(ValueError):
    pass


def sha256(body):
    return hashlib.sha256(body).hexdigest()


def code_identity():
    def git(*args):
        return subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True,
                              timeout=5, check=True).stdout
    paths = [ROOT / 'scripts/verify_noaa_yearly_source.py']
    paths.extend(sorted((ROOT / 'tradingagents').rglob('*.py')))
    sources = {str(path.relative_to(ROOT)): sha256(path.read_bytes()) for path in paths}
    return {'head': git('rev-parse', 'HEAD').decode().strip(),
            'dirty': bool(git('status', '--porcelain=v1')),
            'tracked_diff_sha256': sha256(git('diff', '--binary', 'HEAD')),
            'python_sources_sha256': sha256(json.dumps(sources, sort_keys=True).encode()),
            'python_source_count': len(sources)}


@contextmanager
def offline_guard(counters):
    def deny(kind):
        def blocked(*args, **kwargs):
            counters[kind] += 1
            raise AcceptanceError('offline_io_forbidden')
        return blocked
    with ExitStack() as stack, tempfile.TemporaryDirectory(prefix='noaa-replay-') as scratch:
        # Constructors cannot discover real provider/model credentials.
        stack.enter_context(patch.dict(os.environ, {'PATH': os.defpath, 'HOME': scratch}, clear=True))
        for name in ('getaddrinfo', 'create_connection'):
            stack.enter_context(patch.object(socket, name, deny('network_attempts')))
        for name in ('connect', 'connect_ex'):
            stack.enter_context(patch.object(socket.socket, name, deny('network_attempts')))
        stack.enter_context(patch.object(subprocess, 'Popen', deny('subprocess_attempts')))
        import requests
        import httpx
        stack.enter_context(patch.object(requests.sessions.Session, 'request', deny('network_attempts')))
        stack.enter_context(patch.object(httpx.Client, 'request', deny('network_attempts')))
        stack.enter_context(patch.object(httpx.AsyncClient, 'request', deny('network_attempts')))
        from curl_cffi import Curl, AsyncCurl
        stack.enter_context(patch.object(Curl, 'perform', deny('network_attempts')))
        stack.enter_context(patch.object(AsyncCurl, 'add_handle', deny('network_attempts')))
        yield


def checked_file(path, expected, maximum):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise AcceptanceError('required_original_missing') from None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise AcceptanceError('recorded_file_type_or_size_invalid')
        digest, size = hashlib.sha256(), 0
        while chunk := stream.read(65536):
            size += len(chunk)
            if size > maximum:
                raise AcceptanceError('recorded_file_type_or_size_invalid')
            digest.update(chunk)
        if digest.hexdigest() != expected['sha256'] or size != expected.get('bytes', size):
            raise AcceptanceError('recorded_file_identity_mismatch')
    return {'sha256': digest.hexdigest(), 'bytes': size}


def read_metadata(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise AcceptanceError('fixture_metadata_type_or_size_invalid')
        body = stream.read(1024 * 1024 + 1)
        if len(body) != info.st_size:
            raise AcceptanceError('fixture_metadata_type_or_size_invalid')
        return body


class FileResponse:
    status_code = 200

    def __init__(self, path, identity):
        self.path = path
        self.headers = {'Content-Length': str(identity['bytes'])}
        self.expected = identity
        self.closed, self.consumed = False, 0

    def iter_content(self, chunk_size):
        digest = hashlib.sha256()
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise AcceptanceError('recorded_file_type_or_size_invalid')
            while chunk := stream.read(chunk_size):
                self.consumed += len(chunk)
                if self.consumed > self.expected['bytes']:
                    raise AcceptanceError('recorded_file_identity_mismatch')
                digest.update(chunk)
                yield chunk
        if self.consumed != self.expected['bytes'] or digest.hexdigest() != self.expected['sha256']:
            raise AcceptanceError('recorded_file_identity_mismatch')

    def close(self):
        self.closed = True


def run_acceptance(fixtures=FIXTURES, full_corpus=None):
    start = time.monotonic()
    report = {'schema_version': 1, 'passed': False,
        'scope': 'full_retained_current_year_raw_file_replay' if full_corpus else 'public_subset_raw_file_replay',
        'reference_date': REFERENCE, 'replay_clock_vintage': REPLAY_VINTAGE,
        'current_acquisition': False, 'historical_point_in_time_claim': False,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'io_counters': {'network_attempts': 0, 'subprocess_attempts': 0},
        'transport': {'requests': 0, 'all_closed': True, 'all_bodies_consumed': True},
        'dependencies': {'python': platform.python_version(),
                         **{name: version(name) for name in ('requests', 'urllib3', 'httpx', 'httpcore', 'certifi')}}}
    responses = []
    try:
        report['code'] = code_identity()
        manifest_body = read_metadata(fixtures / 'manifest.json')
        provenance_body = read_metadata(fixtures / 'provenance.json')
        manifest, provenance = json.loads(manifest_body), json.loads(provenance_body)
        report['manifest_sha256'] = sha256(manifest_body)
        report['provenance_sha256'] = sha256(provenance_body)
        if full_corpus:
            names = {'ghcnd-stations.txt': 'stations.txt', 'ghcnd-inventory.txt': 'inventory.txt',
                     '2026.csv.gz': '2026.csv.gz'}
            directory, identities = full_corpus, provenance['original_parents']
        else:
            names = {'ghcnd-stations.txt': 'stations-ten-state-original.txt',
                     'ghcnd-inventory.txt': 'inventory-ten-state-core-original.txt',
                     '2026.csv.gz': '2026-ten-state-37day-original.csv.gz'}
            directory, identities = fixtures, manifest['files']
        checked = {name: checked_file(directory / name, identities[name],
            256 * 1024 * 1024 if name.endswith('.gz') else 64 * 1024 * 1024)
            for name in names.values()}
        report['recorded_files'] = checked
        report['original_provenance'] = {name: value for name, value in provenance['original_parents'].items()
            if name in ('stations.txt', 'inventory.txt', '2026.csv.gz')}
        with offline_guard(report['io_counters']):
            from tradingagents.strategies.data_sources import noaa_yearly_source as module
            from tradingagents.strategies.data_sources.registry import build_default_registry
            urls = {module.CATALOG_URL + '/' + key: value for key, value in names.items() if not key.endswith('.gz')}
            urls[module.YEARLY_URL + '/2026.csv.gz'] = names['2026.csv.gz']

            def recorded_get(url, **kwargs):
                if url not in urls or kwargs.get('stream') is not True or kwargs.get('allow_redirects') is not False:
                    raise AcceptanceError('unexpected_adapter_transport')
                name = urls[url]
                response = FileResponse(directory / name, checked[name])
                responses.append(response)
                report['transport']['requests'] += 1
                return response

            with patch.object(module, '_yearly_http_get', recorded_get), patch.object(module, 'current_vintage_date', lambda: REPLAY_VINTAGE):
                source = build_default_registry({'noaa_delivery_policy': module.DELIVERY_POLICY}).get('noaa')
                report['adapter'] = type(source).__module__ + '.' + type(source).__name__
                summary = source.fetch_ag_weather_summary(REFERENCE, vintage_as_of=REPLAY_VINTAGE)
            summary['coverage'].pop('catalog_station_ids', None)
            report['summary'] = summary
            expected = {'states_reporting': 10, 'observation_lag_days': 2,
                'start_date': '2026-09-17', 'end_date': '2026-10-16',
                'delivery_policy': module.DELIVERY_POLICY, 'numeric_policy': module.NUMERIC_POLICY}
            if full_corpus:
                expected.update({'observations': 201243, 'heat_stress_days': 2,
                                 'avg_tmax': 73.9, 'avg_daily_prcp': .146})
                expected_stations = 7877
            else:
                expected.update({'observations': 900, 'heat_stress_days': 3,
                                 'avg_tmax': 74.3, 'avg_daily_prcp': .131})
                expected_stations = 10
            report['expected'] = expected
            if (any(summary.get(key) != value for key, value in expected.items())
                    or summary['coverage'].get('complete') is not True
                    or summary['coverage'].get('catalog_station_count') != expected_stations):
                raise AcceptanceError('recorded_summary_contract_mismatch')
    except Exception as exc:
        from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
        if isinstance(exc, AcceptanceError):
            report['reason'] = str(exc)
        elif isinstance(exc, SourceFetchError):
            report['reason'] = 'source_' + exc.reason_code
            report['failure'] = exc.partial_data
        elif isinstance(exc, FileNotFoundError):
            report['reason'] = 'required_original_missing'
        else:
            report['reason'] = 'acceptance_execution_failed'
            report['exception_type'] = type(exc).__name__
    report['transport'].update({'all_closed': all(response.closed for response in responses),
        'all_bodies_consumed': all(response.consumed == response.expected['bytes'] for response in responses)})
    report['elapsed_seconds'] = round(time.monotonic() - start, 6)
    report['passed'] = (not report.get('reason') and report['transport']['requests'] == 3
        and report['transport']['all_closed'] and report['transport']['all_bodies_consumed']
        and not any(report['io_counters'].values()))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path, default=FIXTURES)
    parser.add_argument('--full-corpus', type=Path, help='Pinned originals: stations.txt, inventory.txt, 2026.csv.gz')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    report = run_acceptance(args.fixtures, args.full_corpus)
    body = json.dumps(report, indent=2, sort_keys=True) + '\n'
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(body)
    print(body, end='')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
