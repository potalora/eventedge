"""Capture/replay the actual adapter at the prepared HTTP transport boundary."""
from datetime import datetime, timezone
import io
import json
from pathlib import Path

import pytest
import requests

FIXTURE = Path(__file__).parent / 'fixtures/usaspending-recipient-fallback-2026-10-23.json'
NOW = datetime(2026, 10, 23, 6, tzinfo=timezone.utc)


def native_transport(monkeypatch):
    monkeypatch.setattr('scripts.check_usaspending_source.utc_now', lambda: NOW)
    fixture = json.loads(FIXTURE.read_text())['responses']
    raw = next(r for r in fixture['search']['body']['results'] if r['internal_id'] == 363616285)
    replies = {
        fixture['search']['capture']['url']: (200, {'results': [raw], 'page_metadata': {'page': 1, 'hasNext': False}}),
        **{fixture[k]['capture']['url']: (fixture[k]['capture']['status'], fixture[k]['body'])
           for k in ('detail', 'transactions', 'recipient')},
    }
    calls = []
    def send(self, request, **kwargs):
        calls.append((request, kwargs))
        status, body = replies[request.url]
        response = requests.Response()
        response.status_code = status
        response.request = request
        response.url = request.url
        response.headers.update({'Content-Type': 'application/json', 'Date': 'Fri, 23 Oct 2026 06:00:25 GMT'})
        data = json.dumps(body).encode()
        response.raw = io.BytesIO(data)
        if not kwargs.get('stream'):
            response._content = data
            response._content_consumed = True
        return response
    monkeypatch.setattr(requests.Session, 'send', send)
    return calls


def test_public_404_capture_replays_real_adapter_without_network_or_overwrite(tmp_path, monkeypatch):
    from scripts.check_usaspending_source import capture, replay
    calls = native_transport(monkeypatch)
    directory = tmp_path / 'capture'
    result = capture(['2026-10-22', '2026-10-23'], directory, budget_seconds=20, now=NOW, offline_limits=True)
    assert result['ok'] and len(calls) == 8
    assert result['sessions'][0]['kind'] == 'completed_reference'
    assert result['sessions'][1]['kind'] == 'next_session_diagnostic'
    assert all(r['counts']['unresolved'] == 1 for r in result['sessions'])
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    monkeypatch.setattr(requests.Session, 'send', lambda *a, **k: pytest.fail('offline replay used real transport'))
    assert replay(directory)['ok']
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(FileExistsError):
        capture(['2026-10-22', '2026-10-23'], directory, now=NOW)


@pytest.mark.parametrize('fault', ['missing', 'body', 'incomplete', 'request', 'population'])
def test_replay_rejects_missing_tampered_incomplete_or_mismatched_evidence(tmp_path, monkeypatch, fault):
    from scripts.check_usaspending_source import capture, replay
    native_transport(monkeypatch)
    directory = tmp_path / 'capture'
    capture(['2026-10-22', '2026-10-23'], directory, budget_seconds=20, now=NOW, offline_limits=True)
    manifest_path = directory / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    entry = manifest['captures'][0]
    if fault == 'missing':
        (directory / entry['body_file']).unlink()
    elif fault == 'body':
        (directory / entry['body_file']).write_bytes(b'{}')
    elif fault == 'incomplete':
        entry['complete'] = False
    elif fault == 'request':
        entry['request']['method'] = 'GET'
    else:
        manifest['sessions'][0]['population_count'] += 1
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        replay(directory)


@pytest.mark.parametrize('sessions', [['2026-10-23'], ['2026-10-22', '2026-10-26'], ['2026-10-23', '2026-10-22'], ['2026-10-24', '2026-10-26']])
def test_invalid_session_pair_never_creates_output(tmp_path, sessions):
    from scripts.check_usaspending_source import capture
    with pytest.raises(ValueError):
        capture(sessions, tmp_path / 'bad', now=NOW)
    assert not (tmp_path / 'bad').exists()


@pytest.mark.parametrize('field', ['kind', 'window', 'response_time', 'future_clock', 'diagnostics'])
def test_replay_rejects_mislabeled_diagnostic_and_missing_completion_time(tmp_path, monkeypatch, field):
    from scripts.check_usaspending_source import capture, replay
    native_transport(monkeypatch)
    directory = tmp_path / 'capture'
    capture(['2026-10-22', '2026-10-23'], directory, budget_seconds=20, now=NOW, offline_limits=True)
    path = directory / 'manifest.json'
    manifest = json.loads(path.read_text())
    if field == 'kind':
        manifest['sessions'][1]['kind'] = 'completed_reference'
    elif field == 'window':
        manifest['sessions'][1]['window']['start'] = '2020-01-01'
    elif field == 'response_time':
        del manifest['captures'][0]['response_completed_at']
    elif field == 'future_clock':
        for key in ('request_started_at', 'response_started_at', 'response_completed_at'):
            manifest['captures'][0][key] = '2030-01-01T00:00:00+00:00'
    else:
        manifest['request_diagnostics'][0]['attempts'] += 1
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        replay(directory)


def test_stream_capture_never_prereads_and_partial_close_stays_incomplete(monkeypatch):
    from scripts.check_usaspending_source import capture_transport
    seen = []
    def send(session, request, **kwargs):
        assert kwargs['stream'] is True
        value = requests.Response()
        value.status_code = 200
        value.raw = io.BytesIO(b'abcdef')
        seen.append(value.raw)
        return value
    monkeypatch.setattr(requests.Session, 'send', send)
    receipts = []
    with capture_transport(receipts):
        response = requests.get('https://api.usaspending.gov/api/v2/test/', stream=True)
        assert seen[0].tell() == 0
        iterator = response.iter_content(chunk_size=2)
        assert next(iterator) == b'ab'
        response.close()
    assert receipts[0]['complete'] is False
    assert bytes(receipts[0]['_body']) == b'ab'
    assert seen[0].closed


def test_untrusted_transport_is_refused_before_dispatch(monkeypatch):
    from scripts.check_usaspending_source import capture_transport
    monkeypatch.setattr(requests.Session, 'send', lambda *a, **k: pytest.fail('dispatched'))
    with capture_transport([]), pytest.raises(ValueError):
        requests.get('https://example.com/api/v2/awards/1/')


def test_retry503_then_real404_preserves_missing_retry_body_and_replays(tmp_path, monkeypatch):
    from scripts.check_usaspending_source import capture, replay
    native_transport(monkeypatch)
    original = requests.Session.send
    sent_retry = []
    def send(session, request, **kwargs):
        if '/awards/' in request.url and not sent_retry:
            sent_retry.append(True)
            response = requests.Response()
            response.status_code = 503
            response.raw = io.BytesIO(b'{"detail":"temporarily unavailable"}')
            return response
        return original(session, request, **kwargs)
    monkeypatch.setattr(requests.Session, 'send', send)
    directory = tmp_path / 'retry'
    result = capture(['2026-10-22', '2026-10-23'], directory, now=NOW, offline_limits=True)
    assert result['ok']
    retry = next(r for r in result['captures'] if r['status'] == 503)
    assert retry['complete'] is False and retry['body_bytes'] == 0
    assert retry['body_state'] == 'intentionally_unconsumed_retry'
    assert replay(directory)['ok']


@pytest.mark.parametrize('status,partial', [(200, False), (404, False), (503, True)])
def test_unread_identity_or_partially_read_retry_is_not_acceptable(status, partial, monkeypatch):
    from scripts.check_usaspending_source import acceptable_capture, capture_transport
    def send(session, request, **kwargs):
        response = requests.Response()
        response.status_code = status
        response.raw = io.BytesIO(b'abcdef')
        return response
    monkeypatch.setattr(requests.Session, 'send', send)
    entries = []
    with capture_transport(entries):
        response = requests.get('https://api.usaspending.gov/api/v2/awards/1/', stream=True)
        if partial:
            next(response.iter_content(chunk_size=1))
        response.close()
    assert not acceptable_capture(entries[0])
