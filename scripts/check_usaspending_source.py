#!/usr/bin/env python3
"""Bounded public source capture and immutable HTTP-layer replay; no trading authority."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradingagents.strategies.data_sources.award_attribution_policy import POLICY, build_attribution_scope, validate_attribution_scope
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.data_sources.usaspending_source import USASpendingSource
from tradingagents.strategies.orchestration.decision_clock import latest_completed_session
from tradingagents.strategies.orchestration.trading_calendar import is_session, next_session
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine

VERSION = 'usaspending-manual-capture-v2'
LEGACY_VERSION = 'usaspending-manual-capture-v1'
STABLE_FIELDS = ('award_key', 'award_id', 'generated_internal_id', 'internal_id',
                 'recipient_uei', 'recipient_id', 'recipient_name', 'base_obligation_date',
                 'amount', 'agency', 'description', 'start_date', 'last_modified_date',
                 'amount_basis', 'award_scope')
RETRY_STATUSES = {429, 500, 502, 503, 504}


def utc_now():
    return datetime.now(timezone.utc)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(value, reason='invalid capture evidence'):
    if not value:
        raise ValueError(reason)


def session_pair(sessions, now):
    require(isinstance(sessions, list) and len(sessions) == 2, 'exactly two adjacent sessions required')
    values = [date.fromisoformat(s) for s in sessions]
    require(all(d.isoformat() == s and is_session(d) for d, s in zip(values, sessions)))
    require(next_session(values[0]) == values[1], 'query sessions must be ordered adjacent XNYS sessions')
    reference = latest_completed_session(now)
    require(values[1] <= next_session(reference), 'query session is beyond the next diagnostic session')
    return reference


def prepared_identity(request):
    parts = urlsplit(request.url)
    require(parts.scheme == 'https' and parts.netloc == 'api.usaspending.gov'
            and parts.path.startswith('/api/v2/') and not parts.fragment, 'nonofficial source request refused')
    require(not any(k.lower() in {'authorization', 'cookie', 'proxy-authorization'} for k in request.headers),
            'authenticated request refused')
    raw = request.body or b''
    if isinstance(raw, str):
        raw = raw.encode()
    require(isinstance(raw, bytes))
    body = json.loads(raw) if raw else None
    return {'method': request.method, 'url': request.url, 'json': body,
            'body_sha256': digest(raw), 'body_bytes': len(raw)}


@contextmanager
def capture_transport(captures):
    """Observe consumed entity bytes; do not pre-read streams or alter send options."""
    original = requests.Session.send
    def send(session, request, **kwargs):
        entry = {'request': prepared_identity(request), 'request_started_at': utc_now().isoformat(),
                 'complete': False, 'body_state': 'incomplete', 'read_started': False, '_body': bytearray()}
        captures.append(entry)
        response = original(session, request, **kwargs)
        entry.update(response_started_at=utc_now().isoformat(), status=response.status_code,
                     headers={k: response.headers[k] for k in
                              ('Date', 'Content-Type', 'Cache-Trace', 'Cache-Control', 'Age',
                               'ETag', 'Last-Modified', 'X-Cache') if k in response.headers})
        require(not response.history, 'redirected capture refused')
        if not kwargs.get('stream', False):
            entry['_body'].extend(response.content)
            entry.update(complete=True, body_state='complete', response_completed_at=utc_now().isoformat())
            return response
        original_iter, original_close = response.iter_content, response.close
        def chunks(*args, **options):
            entry['read_started'] = True
            for chunk in original_iter(*args, **options):
                require(isinstance(chunk, bytes), 'nonbyte response capture refused')
                entry['_body'].extend(chunk)
                yield chunk
            entry.update(complete=True, body_state='complete', response_completed_at=utc_now().isoformat())
        def close():
            if (not entry['complete'] and not entry['read_started'] and not entry['_body']
                    and entry['status'] in RETRY_STATUSES):
                entry['body_state'] = 'intentionally_unconsumed_retry'
            if 'response_completed_at' not in entry:
                entry['response_completed_at'] = utc_now().isoformat()
            return original_close()
        response.iter_content, response.close = chunks, close
        return response
    with patch.object(requests.Session, 'send', send):
        yield


def acceptable_capture(entry):
    return (entry.get('complete') is True or (entry.get('complete') is False
            and entry.get('body_state') == 'intentionally_unconsumed_retry'
            and entry.get('status') in RETRY_STATUSES and entry.get('read_started') is False
            and not entry.get('_body', b'') and entry.get('body_bytes', 0) == 0))


def acquire(session):
    source = USASpendingSource()
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.registry = SimpleNamespace(get=lambda name: source if name == 'usaspending' else None)
    engine.ar_config = {'award_attribution_policy': POLICY}
    payload = engine._fetch_usaspending_data(session)
    validate_attribution_scope(payload, session=session)
    return payload


def population(payload, session):
    scope = validate_attribution_scope(payload, session=session)
    rows = payload['data']['contracts']
    return {'population_count': len(rows), 'population_sha256': digest(encoded(rows)),
            'award_keys': sorted(r['award_key'] for r in rows), 'counts': scope['counts'],
            'scope_sha256': scope['scope_sha256'], 'source_rows_sha256': scope['source_rows_sha256'],
            'coverage': payload['coverage']}


def compare_windows(payloads, sessions):
    """Compare common-date evidence; changes are inconsistencies, not cause diagnoses."""
    require(len(payloads) == len(sessions) == 2)
    start = max(date.fromisoformat(s) - timedelta(days=30) for s in sessions).isoformat()
    end = min(sessions)
    projected = []
    for payload in payloads:
        rows = payload['data']['contracts']
        projected.append({r['award_key']: {field: r.get(field) for field in STABLE_FIELDS}
                          for r in rows if start <= r['base_obligation_date'] <= end})
    first, second = projected
    only_first, only_second = sorted(first.keys() - second.keys()), sorted(second.keys() - first.keys())
    changed = []
    for key in sorted(first.keys() & second.keys()):
        fields = [f for f in STABLE_FIELDS if first[key][f] != second[key][f]]
        if fields:
            changed.append({'award_key': key, 'fields': fields,
                            'first': {f: first[key][f] for f in fields},
                            'second': {f: second[key][f] for f in fields}})
    consistent = not (only_first or only_second or changed)
    return {'consistent': consistent, 'reason_code': None if consistent else 'inconsistent_shared_window_evidence',
            'common_window': {'start': start, 'end': end},
            'common_window_counts': [len(first), len(second)],
            'only_in_first': only_first, 'only_in_second': only_second, 'changed_awards': changed,
            'compared_fields': list(STABLE_FIELDS)}


def git_identity():
    root = Path(__file__).resolve().parents[1]
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
    return {'commit': git('rev-parse', 'HEAD'), 'git_dirty': bool(git('status', '--porcelain'))}


def write_new(directory, name, data):
    with (directory / name).open('xb') as handle:
        handle.write(data)
    return {'file': name, 'sha256': digest(data), 'bytes': len(data)}


def capture(sessions, output, *, budget_seconds=180, now=None, offline_limits=False):
    """Acquire both windows under one shared deadline and replay before returning ok."""
    now = now or utc_now()
    reference = session_pair(sessions, now)
    require(type(budget_seconds) in (int, float) and math.isfinite(budget_seconds)
            and 1 <= budget_seconds <= 300, 'budget must be 1..300 seconds')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    manifest = {'version': VERSION, 'scope': 'manual_source_observation',
                'incident_recovery_authority': False, 'production_mutations': False,
                'started_at': now.isoformat(), 'latest_completed_session': reference.isoformat(),
                'reference_session': reference.isoformat(), 'queried_sessions': sessions,
                'budget_seconds': budget_seconds, 'policy': POLICY,
                'runtime': {'python': sys.version.split()[0], 'requests': requests.__version__},
                'response_started_at_semantics': 'Session.send return; headers for streams, consumed body for nonstreams',
                **git_identity(), 'captures': [], 'sessions': [], 'files': [], 'capture_ok': False,
                'acquisition_ok': False, 'population_consistent': None, 'population_consistency': None,
                'response_byte_semantics': 'consumed decompressed entity bytes; not compressed wire bytes'}
    captures, diagnostics, payloads = [], [], []
    deadline = time.monotonic() + budget_seconds
    try:
        with provider_budget('usaspending', deadline, diagnostics=diagnostics,
                             **({'limits': ()} if offline_limits else {})), capture_transport(captures):
            for session in sessions:
                first = len(captures)
                payload = acquire(session)
                payloads.append(payload)
                require(time.monotonic() < deadline, 'shared source deadline exhausted')
                name = f'payload-{session}.json'
                manifest['files'].append(write_new(output, name, encoded(payload)))
                manifest['sessions'].append({'session': session,
                    'kind': 'next_session_diagnostic' if date.fromisoformat(session) > reference else
                            ('completed_reference' if session == reference.isoformat() else 'earlier_completed_query'),
                    'window': {'start': (date.fromisoformat(session) - timedelta(days=30)).isoformat(),
                               'end': session, 'date_type': 'new_awards_only', 'minimum_amount': 50_000_000},
                    'payload_file': name, 'capture_start': first, 'capture_end': len(captures),
                    **population(payload, session)})
        require(captures and all(acceptable_capture(r) for r in captures), 'incomplete source capture')
        manifest['capture_ok'] = manifest['acquisition_ok'] = True
    except Exception as exc:
        manifest['failure_class'] = type(exc).__name__
    finally:
        if len(payloads) == 2:
            comparison = compare_windows(payloads, sessions)
            manifest.update(population_consistency=comparison, population_consistent=comparison['consistent'])
        for index, entry in enumerate(captures):
            data = bytes(entry.pop('_body'))
            name = f'response-{index:04d}.bin'
            manifest['files'].append(write_new(output, name, data))
            entry.update(body_file=name, body_sha256=digest(data), body_bytes=len(data))
            manifest['captures'].append(entry)
        manifest.update(finished_at=utc_now().isoformat(), request_diagnostics=diagnostics)
        write_new(output, 'manifest.json', encoded(manifest))
    if not manifest['capture_ok']:
        return {**manifest, 'offline_replay_ok': False, 'ok': False}
    try:
        result = replay(output)
    except Exception as exc:
        result = {'acquisition_ok': True, 'offline_replay_ok': False,
                  'population_consistent': manifest['population_consistent'],
                  'population_consistency': manifest['population_consistency'],
                  'ok': False, 'failure_class': type(exc).__name__}
    write_new(output, 'replay-result.json', encoded(result))
    return {**manifest, **{k: result[k] for k in
            ('ok', 'acquisition_ok', 'offline_replay_ok', 'population_consistent', 'population_consistency')}}


def read_verified(directory, name, files):
    require(isinstance(name, str) and Path(name).name == name and name in files)
    path = directory / name
    require(path.is_file() and not path.is_symlink(), 'missing or unsafe artifact')
    data = path.read_bytes()
    require(len(data) == files[name]['bytes'] and digest(data) == files[name]['sha256'], 'artifact hash mismatch')
    return data


def restore_observation_times(actual, expected):
    """Only acquisition-generated observed_at fields vary; all provider bytes stay bound."""
    if isinstance(actual, dict) and isinstance(expected, dict):
        for key in actual.keys() & expected.keys():
            if key == 'observed_at':
                actual[key] = expected[key]
            elif key != 'body':
                restore_observation_times(actual[key], expected[key])
    elif isinstance(actual, list) and isinstance(expected, list):
        for a, e in zip(actual, expected):
            restore_observation_times(a, e)


def replay(directory):
    """Verify artifacts and replay every recorded request in order, without any network."""
    directory = Path(directory)
    require(not (directory / 'manifest.json').is_symlink())
    try:
        manifest = json.loads((directory / 'manifest.json').read_bytes())
        require(manifest['version'] in {VERSION, LEGACY_VERSION}
                and manifest['capture_ok'] is True and manifest['policy'] == POLICY)
        require(manifest['scope'] == 'manual_source_observation'
                and manifest['incident_recovery_authority'] is False and manifest['production_mutations'] is False)
        recorded_now = datetime.fromisoformat(manifest['started_at'])
        finished = datetime.fromisoformat(manifest['finished_at'])
        require(recorded_now.tzinfo is not None and finished.tzinfo is not None and recorded_now <= finished)
        reference = session_pair(manifest['queried_sessions'], recorded_now)
        require(manifest['latest_completed_session'] == manifest['reference_session'] == reference.isoformat())
        require(len(manifest['sessions']) == 2 and len(manifest['captures']) > 0)
        files = {r['file']: r for r in manifest['files']}
        require(len(files) == len(manifest['files']))
        for name in files:
            read_verified(directory, name, files)
        captures = manifest['captures']
        for entry in captures:
            require(acceptable_capture(entry) and type(entry['status']) is int)
            stamps = [datetime.fromisoformat(entry[k]) for k in
                      ('request_started_at', 'response_started_at', 'response_completed_at')]
            require(all(t.tzinfo is not None and t.utcoffset() == timedelta(0) for t in stamps)
                    and stamps == sorted(stamps) and recorded_now <= stamps[0] <= stamps[-1] <= finished,
                    'invalid capture clocks')
            raw = read_verified(directory, entry['body_file'], files)
            require(entry['body_bytes'] == len(raw) and entry['body_sha256'] == digest(raw))
        index = 0
        def send(session, request, **kwargs):
            nonlocal index
            require(index < len(captures), 'unexpected replay request')
            entry = captures[index]
            require(prepared_identity(request) == entry['request'], 'replay request mismatch')
            index += 1
            response = requests.Response()
            response.status_code, response.url, response.request = entry['status'], request.url, request
            response.headers.update(entry['headers'])
            data = read_verified(directory, entry['body_file'], files)
            response.raw = io.BytesIO(data)
            if not kwargs.get('stream', False):
                response._content, response._content_consumed = data, True
            return response
        replay_diagnostics, payloads = [], []
        with patch.object(requests.Session, 'send', send), provider_budget(
                'usaspending', time.monotonic() + 300, limits=(), sleep=lambda seconds: None,
                diagnostics=replay_diagnostics):
            for session, receipt in zip(manifest['queried_sessions'], manifest['sessions']):
                require(receipt['session'] == session and receipt['capture_start'] == index)
                expected_kind = ('next_session_diagnostic' if date.fromisoformat(session) > reference else
                                 ('completed_reference' if session == reference.isoformat() else 'earlier_completed_query'))
                require(receipt['kind'] == expected_kind)
                require(receipt['window'] == {'start': (date.fromisoformat(session) - timedelta(days=30)).isoformat(),
                        'end': session, 'date_type': 'new_awards_only', 'minimum_amount': 50_000_000})
                expected = json.loads(read_verified(directory, receipt['payload_file'], files))
                require(population(expected, session) == {k: receipt[k] for k in population(expected, session)})
                actual = acquire(session)
                require(receipt['capture_end'] == index, 'session request census mismatch')
                restore_observation_times(actual, expected)
                # Recompute time-dependent policy digests after rebinding acquisition-only clocks.
                scope = build_attribution_scope(actual['data']['contracts'], actual['coverage'], session=session)
                actual['coverage']['attribution_scope'] = scope
                actual['data']['coverage'] = actual['coverage']
                validate_attribution_scope(actual, session=session)
                require(actual == expected, 'normalized whole-population replay mismatch')
                payloads.append(expected)
        require(index == len(captures), 'unused captured requests')
        require(replay_diagnostics == manifest['request_diagnostics'], 'request diagnostics mismatch')
        comparison = compare_windows(payloads, manifest['queried_sessions'])
        if manifest['version'] == VERSION:
            require(manifest['acquisition_ok'] is True
                    and manifest['population_consistency'] == comparison
                    and manifest['population_consistent'] == comparison['consistent'],
                    'population comparison evidence mismatch')
        return {'version': VERSION, 'capture_version': manifest['version'],
                'scope': 'offline_http_adapter_replay', 'ok': comparison['consistent'],
                'acquisition_ok': True, 'offline_replay_ok': True,
                'population_consistent': comparison['consistent'], 'population_consistency': comparison,
                'incident_recovery_authority': False, 'queried_sessions': manifest['queried_sessions'],
                'request_count': index, 'manifest_sha256': digest((directory / 'manifest.json').read_bytes())}
    except (KeyError, TypeError, OSError, json.JSONDecodeError, SourceFetchError) as exc:
        raise ValueError('missing or invalid replay evidence') from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', action='append')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--budget-seconds', type=float, default=180)
    parser.add_argument('--replay', type=Path)
    args = parser.parse_args()
    if args.replay and (args.session or args.output):
        parser.error('--replay cannot be combined with --session or --output')
    if not args.replay and (not args.session or not args.output):
        parser.error('capture requires two --session values and --output NEW_DIRECTORY')
    try:
        result = replay(args.replay) if args.replay else capture(args.session, args.output, budget_seconds=args.budget_seconds)
        summary = {k: result[k] for k in ('scope', 'ok', 'incident_recovery_authority', 'acquisition_ok', 'offline_replay_ok', 'population_consistent')}
        if not args.replay:
            summary.update(output=str(args.output), sessions=[{k: r[k] for k in
                ('session', 'kind', 'population_count', 'counts')} for r in result['sessions']])
        print(json.dumps(summary, sort_keys=True))
        return 0 if result['ok'] else 1
    except Exception as exc:
        print(json.dumps({'ok': False, 'failure_class': type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
