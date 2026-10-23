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


# Native whole-window identity/date/amount projections from commit 0df capture.
# These retain all 38/37 awards, including the shared-window omission.
# Manifest SHA256: fadae3ee53b881df28857e5da91c4a2976676b6d4bf363897d880f3772b88747
REAL_WINDOW_PROJECTIONS = {
    # Payload SHA256: 657fcb6906ab823a655a84a0d547cb150849109851e754b5ad43d062a21c0c7e
    '2026-10-21': [
        ('CONT_AWD_36C10G26K0300_3600_36C10G19D0038_3600', '2026-09-23', 1200153022.86),
        ('CONT_AWD_36C10G26K0298_3600_36C79119D0006_3600', '2026-09-30', 1085804852.71),
        ('CONT_AWD_36C10G27K0004_3600_36C10G19D0038_3600', '2026-10-07', 900485186.84),
        ('CONT_AWD_36C10G26K0297_3600_36C79119D0005_3600', '2026-09-30', 770881595.07),
        ('CONT_AWD_36C10G26K0296_3600_36C79119D0004_3600', '2026-09-30', 729817620.99),
        ('CONT_AWD_70B01C26F00000874_7014_70B01C26D00000021_7014', '2026-09-26', 332000000.0),
        ('CONT_AWD_693JF726F00059N_6938_693JF720G000004_6938', '2026-09-25', 262783059.0),
        ('CONT_AWD_75N99026F00002_7529_75N99019D00013_7529', '2026-09-22', 237769777.0),
        ('CONT_AWD_15JPSS26C00000450_1501_-NONE-_-NONE-', '2026-09-23', 166340854.36),
        ('CONT_AWD_70B01C26F00001125_7014_70B01C26D00000022_7014', '2026-09-28', 158000000.0),
        ('CONT_AWD_70B01C26F00001021_7014_70B01C26D00000024_7014', '2026-09-25', 150000000.0),
        ('CONT_AWD_70B01C26F00001093_7014_70B01C26D00000025_7014', '2026-09-30', 144000000.0),
        ('CONT_AWD_70B01C26F00001119_7014_70B01C26D00000023_7014', '2026-09-29', 137000000.0),
        ('CONT_AWD_28321326FDS030164_2800_28321326D00060004_2800', '2026-09-28', 130434205.69),
        ('CONT_AWD_70B01C26F00001092_7014_70B01C26D00000025_7014', '2026-09-30', 129000000.0),
        ('CONT_AWD_70B01C26F00000952_7014_70B01C26D00000021_7014', '2026-09-28', 123585356.0),
        ('CONT_AWD_70B01C26F00001079_7014_70B01C26D00000021_7014', '2026-09-29', 117000000.0),
        ('CONT_AWD_70B01C26F00000878_7014_70B01C26D00000019_7014', '2026-09-29', 109000000.0),
        ('CONT_AWD_70B01C26F00000951_7014_70B01C26D00000025_7014', '2026-09-25', 107066852.0),
        ('CONT_AWD_70B01C26F00000871_7014_70B01C26D00000023_7014', '2026-09-29', 103000000.0),
        ('CONT_AWD_12363N27C4000_12C2_-NONE-_-NONE-', '2026-10-06', 99575262.0),
        ('CONT_AWD_75A50126C00010_7505_-NONE-_-NONE-', '2026-09-28', 96058494.0),
        ('CONT_AWD_70B01C26F00000950_7014_70B01C26D00000025_7014', '2026-09-25', 89139948.0),
        ('CONT_AWD_70B06C26F00001127_7014_70B06C24A00000037_7014', '2026-09-25', 86100000.0),
        ('CONT_AWD_91003126F0083_9100_HHSN316201200002W_7529', '2026-09-30', 84066254.47),
        ('CONT_AWD_36C10X26K0559_3600_36C10X23D0032_3600', '2026-09-22', 82387714.43),
        ('CONT_AWD_70B01C26F00000947_7014_70B01C26D00000025_7014', '2026-09-28', 75810908.0),
        ('CONT_AWD_75N91026F00034_7529_75N91019D00024_7529', '2026-09-29', 72167416.0),
        ('CONT_AWD_7571MN26F80170_7571_GS00F045DA_4732', '2026-09-30', 69900000.0),
        ('CONT_AWD_75D30126F21224_7523_47QRAA23D0052_4732', '2026-09-29', 64990000.0),
        ('CONT_AWD_70T04026F7672N007_7013_70T04025D7672N004_7013', '2026-09-28', 63606870.0),
        ('CONT_AWD_70B01C26F00000949_7014_70B01C26D00000020_7014', '2026-09-28', 63358052.0),
        ('CONT_AWD_36C24827C0008_3600_-NONE-_-NONE-', '2026-10-21', 63117507.88),
        ('CONT_AWD_140P2026C0052_1443_-NONE-_-NONE-', '2026-09-25', 62416000.0),
        ('CONT_AWD_28321326FDX030166_2800_47QSWA18D008F_4732', '2026-09-28', 60490406.19),
        ('CONT_AWD_1333BJ27C00280001_1344_-NONE-_-NONE-', '2026-10-01', 60488251.95),
        ('CONT_AWD_70T05026F5900N032_7013_70B01C20D00000004_7014', '2026-09-23', 55999961.5),
        ('CONT_AWD_47PB5326C0009_4740_-NONE-_-NONE-', '2026-09-22', 54253859.37),
    ],
    # Payload SHA256: b43d58b44fedbd80ff748cdce3442c70d6cbc19594ce85e7dad28a3c192f397c
    '2026-10-22': [
        ('CONT_AWD_36C10G26K0300_3600_36C10G19D0038_3600', '2026-09-23', 1200153022.86),
        ('CONT_AWD_36C10G26K0298_3600_36C79119D0006_3600', '2026-09-30', 1085804852.71),
        ('CONT_AWD_36C10G27K0004_3600_36C10G19D0038_3600', '2026-10-07', 900485186.84),
        ('CONT_AWD_36C10G26K0297_3600_36C79119D0005_3600', '2026-09-30', 770881595.07),
        ('CONT_AWD_36C10G26K0296_3600_36C79119D0004_3600', '2026-09-30', 729817620.99),
        ('CONT_AWD_70B01C26F00000874_7014_70B01C26D00000021_7014', '2026-09-26', 332000000.0),
        ('CONT_AWD_693JF726F00059N_6938_693JF720G000004_6938', '2026-09-25', 262783059.0),
        ('CONT_AWD_75N99026F00002_7529_75N99019D00013_7529', '2026-09-22', 237769777.0),
        ('CONT_AWD_15JPSS26C00000450_1501_-NONE-_-NONE-', '2026-09-23', 166340854.36),
        ('CONT_AWD_70B01C26F00001125_7014_70B01C26D00000022_7014', '2026-09-28', 158000000.0),
        ('CONT_AWD_70B01C26F00001021_7014_70B01C26D00000024_7014', '2026-09-25', 150000000.0),
        ('CONT_AWD_70B01C26F00001093_7014_70B01C26D00000025_7014', '2026-09-30', 144000000.0),
        ('CONT_AWD_70B01C26F00001119_7014_70B01C26D00000023_7014', '2026-09-29', 137000000.0),
        ('CONT_AWD_28321326FDS030164_2800_28321326D00060004_2800', '2026-09-28', 130434205.69),
        ('CONT_AWD_70B01C26F00001092_7014_70B01C26D00000025_7014', '2026-09-30', 129000000.0),
        ('CONT_AWD_70B01C26F00000952_7014_70B01C26D00000021_7014', '2026-09-28', 123585356.0),
        ('CONT_AWD_70B01C26F00001079_7014_70B01C26D00000021_7014', '2026-09-29', 117000000.0),
        ('CONT_AWD_70B01C26F00000878_7014_70B01C26D00000019_7014', '2026-09-29', 109000000.0),
        ('CONT_AWD_70B01C26F00000951_7014_70B01C26D00000025_7014', '2026-09-25', 107066852.0),
        ('CONT_AWD_70B01C26F00000871_7014_70B01C26D00000023_7014', '2026-09-29', 103000000.0),
        ('CONT_AWD_12363N27C4000_12C2_-NONE-_-NONE-', '2026-10-06', 99575262.0),
        ('CONT_AWD_75A50126C00010_7505_-NONE-_-NONE-', '2026-09-28', 96058494.0),
        ('CONT_AWD_70B01C26F00000950_7014_70B01C26D00000025_7014', '2026-09-25', 89139948.0),
        ('CONT_AWD_70B06C26F00001127_7014_70B06C24A00000037_7014', '2026-09-25', 86100000.0),
        ('CONT_AWD_91003126F0083_9100_HHSN316201200002W_7529', '2026-09-30', 84066254.47),
        ('CONT_AWD_36C10X26K0559_3600_36C10X23D0032_3600', '2026-09-22', 82387714.43),
        ('CONT_AWD_70B01C26F00000947_7014_70B01C26D00000025_7014', '2026-09-28', 75810908.0),
        ('CONT_AWD_75N91026F00034_7529_75N91019D00024_7529', '2026-09-29', 72167416.0),
        ('CONT_AWD_7571MN26F80170_7571_GS00F045DA_4732', '2026-09-30', 69900000.0),
        ('CONT_AWD_75D30126F21224_7523_47QRAA23D0052_4732', '2026-09-29', 64990000.0),
        ('CONT_AWD_70T04026F7672N007_7013_70T04025D7672N004_7013', '2026-09-28', 63606870.0),
        ('CONT_AWD_70B01C26F00000949_7014_70B01C26D00000020_7014', '2026-09-28', 63358052.0),
        ('CONT_AWD_140P2026C0052_1443_-NONE-_-NONE-', '2026-09-25', 62416000.0),
        ('CONT_AWD_28321326FDX030166_2800_47QSWA18D008F_4732', '2026-09-28', 60490406.19),
        ('CONT_AWD_1333BJ27C00280001_1344_-NONE-_-NONE-', '2026-10-01', 60488251.95),
        ('CONT_AWD_70T05026F5900N032_7013_70B01C20D00000004_7014', '2026-09-23', 55999961.5),
        ('CONT_AWD_47PB5326C0009_4740_-NONE-_-NONE-', '2026-09-22', 54253859.37),
    ],
}


def test_captured_38_and_37_award_windows_expose_shared_date_omission():
    from scripts.check_usaspending_source import compare_windows
    sessions = ['2026-10-21', '2026-10-22']
    payloads = [{'data': {'contracts': [dict(zip(
        ('award_key', 'base_obligation_date', 'amount'), row))
        for row in REAL_WINDOW_PROJECTIONS[session]]}} for session in sessions]
    result = compare_windows(payloads, sessions)
    assert not result['consistent']
    assert result['only_in_first'] == ['CONT_AWD_36C24827C0008_3600_-NONE-_-NONE-']
    assert result['only_in_second'] == []


@pytest.mark.parametrize('legacy', [False, True])
def test_capture_retains_both_valid_payloads_when_overlap_contradicts(tmp_path, monkeypatch, legacy):
    from scripts.check_usaspending_source import capture, replay
    native_transport(monkeypatch)
    original = requests.Session.send
    def send(session, request, **kwargs):
        response = original(session, request, **kwargs)
        body = json.loads(request.body) if request.body else {}
        if request.url.endswith('spending_by_award/') and body['filters']['time_period'][0]['end_date'] == '2026-10-23':
            response._content = b'{"results":[],"page_metadata":{"page":1,"hasNext":false}}'
        return response
    monkeypatch.setattr(requests.Session, 'send', send)
    directory = tmp_path / 'inconsistent'
    result = capture(['2026-10-22', '2026-10-23'], directory, now=NOW, offline_limits=True)
    assert not result['ok'] and not result['population_consistent']
    assert result['acquisition_ok'] and result['offline_replay_ok']
    assert (directory / 'payload-2026-10-22.json').exists()
    assert (directory / 'payload-2026-10-23.json').exists()
    if legacy:
        manifest_path = directory / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['version'] = 'usaspending-manual-capture-v1'
        for field in ('acquisition_ok', 'population_consistent', 'population_consistency'):
            del manifest[field]
        manifest_path.write_text(json.dumps(manifest))
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    reassessment = replay(directory)
    assert not reassessment['ok'] and reassessment['offline_replay_ok']
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


def test_overlap_allows_only_natural_window_entry_exit_and_ignores_derived_fields():
    from scripts.check_usaspending_source import compare_windows
    common = {'award_key': 'common', 'base_obligation_date': '2026-10-21', 'amount': 63117507.88}
    first = {'data': {'contracts': [dict(common, observed_at='old', issuer_attribution={'status': 'unresolved'}),
                                  {'award_key': 'exits', 'base_obligation_date': '2026-09-22'}]}}
    second = {'data': {'contracts': [dict(common, observed_at='new', issuer_attribution={'status': 'verified_listed_target'}),
                                   {'award_key': 'enters', 'base_obligation_date': '2026-10-23'}]}}
    result = compare_windows([first, second], ['2026-10-22', '2026-10-23'])
    assert result['consistent'] and result['common_window_counts'] == [1, 1]


@pytest.mark.parametrize('field', ['award_id', 'generated_internal_id', 'internal_id', 'recipient_uei',
                                  'recipient_id', 'recipient_name', 'amount', 'agency', 'description',
                                  'start_date', 'last_modified_date'])
def test_overlap_rejects_changed_native_identity_or_economics(field):
    from scripts.check_usaspending_source import compare_windows
    common = {'award_key': 'common', 'base_obligation_date': '2026-10-21', field: 'original'}
    changed = dict(common, **{field: 'changed'})
    result = compare_windows([{'data': {'contracts': [common]}}, {'data': {'contracts': [changed]}}],
                             ['2026-10-22', '2026-10-23'])
    assert not result['consistent']
    assert result['changed_awards'] == [{'award_key': 'common', 'fields': [field],
                                        'first': {field: 'original'}, 'second': {field: 'changed'}}]


def test_capture_preserves_safe_cache_provenance_and_replay_without_cookies(tmp_path, monkeypatch):
    from scripts.check_usaspending_source import capture, replay
    native_transport(monkeypatch)
    original = requests.Session.send
    cache_headers = {'Cache-Trace': 'hit-cache', 'Cache-Control': 'max-age=60', 'Age': '12',
                     'ETag': '"public-response-v1"', 'Last-Modified': 'Thu, 22 Oct 2026 20:00:00 GMT',
                     'X-Cache': 'HIT'}
    def send(session, request, **kwargs):
        response = original(session, request, **kwargs)
        response.headers.update(cache_headers)
        response.headers['Set-Cookie'] = 'private-session=excluded'
        response.headers['Cache-Key'] = 'excluded-key'
        return response
    monkeypatch.setattr(requests.Session, 'send', send)
    directory = tmp_path / 'cache-provenance'
    result = capture(['2026-10-22', '2026-10-23'], directory, now=NOW, offline_limits=True)
    assert result['ok']
    for entry in result['captures']:
        assert entry['headers'] == {**cache_headers, 'Content-Type': 'application/json',
                                    'Date': 'Fri, 23 Oct 2026 06:00:25 GMT'}
    manifest = (directory / 'manifest.json').read_bytes()
    assert b'private-session' not in manifest and b'excluded-key' not in manifest
    original_response, replay_responses = requests.Response, []
    class ReplayResponse(original_response):
        def __init__(self):
            super().__init__()
            replay_responses.append(self)
    monkeypatch.setattr(requests, 'Response', ReplayResponse)
    assert replay(directory)['ok']
    assert len(replay_responses) == len(result['captures'])
    assert all({key: response.headers[key] for key in cache_headers} == cache_headers
               for response in replay_responses)
