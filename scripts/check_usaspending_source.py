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

VERSION = 'usaspending-manual-capture-v1'
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
                     headers={k: response.headers[k] for k in ('Date', 'Content-Type') if k in response.headers})
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
                'response_byte_semantics': 'consumed decompressed entity bytes; not compressed wire bytes'}
    captures, diagnostics = [], []
    deadline = time.monotonic() + budget_seconds
    try:
        with provider_budget('usaspending', deadline, diagnostics=diagnostics,
                             **({'limits': ()} if offline_limits else {})), capture_transport(captures):
            for session in sessions:
                first = len(captures)
                payload = acquire(session)
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
        manifest['capture_ok'] = True
    except Exception as exc:
        manifest['failure_class'] = type(exc).__name__
    finally:
        for index, entry in enumerate(captures):
            data = bytes(entry.pop('_body'))
            name = f'response-{index:04d}.bin'
            manifest['files'].append(write_new(output, name, data))
            entry.update(body_file=name, body_sha256=digest(data), body_bytes=len(data))
            manifest['captures'].append(entry)
        manifest.update(finished_at=utc_now().isoformat(), request_diagnostics=diagnostics)
        write_new(output, 'manifest.json', encoded(manifest))
    if not manifest['capture_ok']:
        return {**manifest, 'ok': False}
    result = replay(output)
    write_new(output, 'replay-result.json', encoded(result))
    return {**manifest, 'ok': result['ok']}


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
        require(manifest['version'] == VERSION and manifest['capture_ok'] is True and manifest['policy'] == POLICY)
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
        replay_diagnostics = []
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
        require(index == len(captures), 'unused captured requests')
        require(replay_diagnostics == manifest['request_diagnostics'], 'request diagnostics mismatch')
        return {'version': VERSION, 'scope': 'offline_http_adapter_replay', 'ok': True,
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
        summary = {k: result[k] for k in ('scope', 'ok', 'incident_recovery_authority')}
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
