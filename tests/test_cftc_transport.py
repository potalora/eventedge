"""CFTC status/retry/deadline acceptance with only the HTTP boundary replaced."""
import base64
import io
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import zipfile

import pytest
import requests

from tradingagents.strategies.data_sources import cftc_source, request_policy
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies import runtime_deadline, transport_worker


class Clock:
    def __init__(self):
        self.now, self.waits = 0.0, []
    def __call__(self):
        return self.now
    def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay


def archive(member='f_year.txt', content=None):
    if content is None:
        content = ('Market_and_Exchange_Names,Report_Date_as_YYYY-MM-DD,'
                   'M_Money_Positions_Long_All,M_Money_Positions_Short_All\n'
                   'GOLD - COMMODITY EXCHANGE INC.,2026-10-06,100,40\n')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, content)
    return output.getvalue()


def response(status=200, body=None, retry_after=''):
    return {'status_code': status, 'headers': {'Retry-After': retry_after},
            'body_base64': base64.b64encode(archive() if body is None else body).decode()}


def corrupt_archive():
    body = bytearray(archive())
    # ZIP local header is 30 bytes, followed by the ten-byte member name.
    # BFINAL=1/BTYPE=3 is an invalid deflate block, with an intact ZIP directory.
    body[40] = 7
    return bytes(body)


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('CFTC transport regression attempted live network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setitem(sys.modules, 'cot_reports', SimpleNamespace(cot_year=forbidden))
    monkeypatch.setattr(cftc_source, 'current_session_date', lambda: '2026-10-12')
    monkeypatch.setattr(cftc_source, 'acquisition_time', lambda: '2026-10-12T22:00:00+00:00')


def install_transport(monkeypatch, replies, clock=None):
    replies, calls = iter(replies), []
    def send(payload, timeout):
        calls.append((payload, timeout))
        item = next(replies)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item()
        return item
    monkeypatch.setattr(runtime_deadline, 'bounded_transport', send)
    return calls


def fetch(source=None):
    return (source or cftc_source.CFTCSource())._fetch_raw_report(
        lookback_weeks=4, as_of='2026-10-12')


@pytest.mark.parametrize('failure', [response(429, retry_after='2'), response(503),
                                   {'error': 'timeout'}, {'error': 'transport_error'},
                                   TimeoutError('transport_deadline_exhausted')],
                         ids=['429', '503', 'read_timeout', 'disconnect', 'hard_deadline'])
def test_transient_cftc_recovers_within_original_budget(monkeypatch, tmp_path, failure):
    monkeypatch.chdir(tmp_path)
    clock, diagnostics = Clock(), []
    calls = install_transport(monkeypatch, [failure, response()])
    with request_policy.provider_budget('cftc', 10, clock=clock, sleep=clock.sleep,
            random_fn=lambda: 0, limits=(), diagnostics=diagnostics):
        frame = fetch()
    assert frame.iloc[0]['M_Money_Positions_Long_All'] == 100
    assert len(calls) == 2 and all(0 < timeout <= 10 for _, timeout in calls)
    assert all(item['url'] == 'https://cftc.gov/files/dea/history/fut_disagg_txt_2026.zip'
               for item, _ in calls)
    assert clock.waits == ([2] if isinstance(failure, dict) and failure.get('status_code') == 429 else [.5])
    assert diagnostics[-1]['attempts'] == 2 and diagnostics[-1]['recovered']
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('error,reason', [({'error': 'timeout'}, 'timeout'),
                                       ({'error': 'transport_error'}, 'transport_error'),
                                       (response(503), 'http_error')])
def test_transient_cftc_exhausts_three_attempts_without_sticky_failure(monkeypatch, error, reason):
    clock, source = Clock(), cftc_source.CFTCSource()
    calls = install_transport(monkeypatch, [error, error, error, response()])
    with request_policy.provider_budget('cftc', 10, clock=clock, sleep=clock.sleep,
            random_fn=lambda: 0, limits=()):
        with pytest.raises(SourceFetchError) as exc:
            fetch(source)
    assert exc.value.reason_code == reason and exc.value.attempts == 3
    assert len(calls) == 3 and clock.waits == [.5, 1.0]
    assert fetch(source).iloc[0]['M_Money_Positions_Short_All'] == 40
    assert len(calls) == 4


def test_permanent_403_is_once_and_later_normal_attempt_can_recover(monkeypatch):
    calls = install_transport(monkeypatch, [response(403, b'forbidden'), response()])
    source = cftc_source.CFTCSource()
    with pytest.raises(SourceFetchError) as exc:
        fetch(source)
    assert exc.value.reason_code == 'http_error' and exc.value.http_status == 403
    assert exc.value.attempts == 1 and len(calls) == 1
    assert len(fetch(source)) == 1 and len(calls) == 2


@pytest.mark.parametrize('body', [b'not a zip', archive('../f_year.txt'),
                                archive(content='wrong,schema\n1,2\n'), corrupt_archive()],
                         ids=['not_zip', 'wrong_member', 'wrong_schema', 'corrupt_deflate'])
def test_malformed_archive_is_nonretryable_and_never_cached(monkeypatch, body):
    source = cftc_source.CFTCSource()
    calls = install_transport(monkeypatch, [response(body=body), response()])
    with pytest.raises(SourceFetchError) as exc:
        fetch(source)
    assert exc.value.reason_code == 'invalid_response' and len(calls) == 1
    assert len(fetch(source)) == 1 and len(calls) == 2


def test_cftc_deadline_prevents_follow_on_retry(monkeypatch):
    clock = Clock()
    def late_timeout():
        clock.now = 2
        return {'error': 'timeout'}
    calls = install_transport(monkeypatch, [late_timeout])
    with request_policy.provider_budget('cftc', 2, clock=clock, sleep=clock.sleep, limits=()):
        with pytest.raises(SourceFetchError) as exc:
            fetch()
    assert exc.value.reason_code == 'timeout' and len(calls) == 1
    assert calls[0][1] <= 2


def test_binary_worker_preserves_bytes_and_ignores_error_bodies(monkeypatch):
    class Response:
        status_code, headers = 200, {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, chunk_size): yield b'\x00\xffPK\x03\x04'
    seen = []
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: seen.append(kw) or Response())
    payload = {'kind': 'http_get_bytes', 'url': 'https://cftc.gov/fixture',
               'timeout': 1, 'max_bytes': 16}
    result = transport_worker.execute(payload)
    assert base64.b64decode(result['body_base64']) == b'\x00\xffPK\x03\x04'
    assert seen[0]['stream'] is True
    Response.status_code, Response.headers = 403, {'Retry-After': '2'}
    def forbidden_body(self, chunk_size):
        pytest.fail('HTTP error body should not be read')
    monkeypatch.setattr(Response, 'iter_content', forbidden_body)
    result = transport_worker.execute(payload)
    assert result['status_code'] == 403 and result['headers']['Retry-After'] == '2'
    assert result['body_base64'] == ''


def test_binary_worker_rejects_oversized_body(monkeypatch):
    class Response:
        status_code, headers = 200, {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, chunk_size): yield b'x' * 17
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: Response())
    result = transport_worker.execute({'kind': 'http_get_bytes', 'url': 'https://cftc.gov/fixture',
                                      'timeout': 1, 'max_bytes': 16})
    assert result['error'] == 'invalid_response'


def test_existing_transport_reaps_hung_binary_worker(monkeypatch):
    original, children = subprocess.Popen, []
    def hung_worker(*args, **kwargs):
        process = original([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(runtime_deadline.subprocess, 'Popen', hung_worker)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        runtime_deadline.bounded_transport({'kind': 'http_get_bytes'}, .1)
    assert time.monotonic() - started < 2
    assert len(children) == 1 and children[0].poll() is not None
