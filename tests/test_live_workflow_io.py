import concurrent.futures
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def restore_httpx_hooks():
    import httpx
    original = httpx.Client._send_single_request, httpx.AsyncClient._send_single_request
    yield
    httpx.Client._send_single_request, httpx.AsyncClient._send_single_request = original


def io_module():
    from tools.live_workflow import recorded_io
    return importlib.reload(recorded_io)


def setup_tape(tmp_path, mode='live_record', identity=None, phase='daily'):
    io = io_module()
    io.configure_tape(mode=mode, tape_dir=tmp_path, run_identity=identity or {'candidate': 'abc', 'profile': 'def'}, phase=phase)
    return io


def event(io, request=None):
    return io.reserve_event(boundary='sdk', operation='query', public_request=request or {'symbol': 'ABC'})


def test_occurrences_and_exact_identity(tmp_path):
    io = setup_tape(tmp_path)
    for value in [1, 2]:
        io.finish_event(event(io), sdk_value=value)
    io = setup_tape(tmp_path, 'offline_replay')
    assert [io.replay_event(boundary='sdk', operation='query', public_request={'symbol': 'ABC'})['sdk_value'] for _ in range(2)] == [1, 2]
    with pytest.raises(io.TapeError, match='missing'):
        io.replay_event(boundary='sdk', operation='query', public_request={'symbol': 'ABC'})
    io = io_module()
    with pytest.raises(io.TapeError, match='identity'):
        io.configure_tape(mode='offline_replay', tape_dir=tmp_path, run_identity={'candidate': 'wrong'}, phase='daily')


def test_phase_and_unconsumed_records(tmp_path):
    io = setup_tape(tmp_path)
    io.finish_event(event(io), sdk_value=1)
    io = setup_tape(tmp_path, 'offline_replay', phase='preflight')
    with pytest.raises(io.TapeError, match='missing'):
        io.replay_event(boundary='sdk', operation='query', public_request={'symbol': 'ABC'})
    io = setup_tape(tmp_path, 'offline_replay')
    with pytest.raises(io.TapeError, match='unconsumed'):
        io.assert_replay_complete()


def test_reservations_concurrent_and_private(tmp_path):
    io = setup_tape(tmp_path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        events = list(pool.map(lambda _: event(io), range(30)))
    assert sorted(e['occurrence'] for e in events) == list(range(30))
    assert all(p.stat().st_mode & 0o077 == 0 for p in tmp_path.rglob('*'))


def test_tampered_and_incomplete_body_rejected(tmp_path):
    io = setup_tape(tmp_path)
    body = tmp_path / 'input'
    body.write_bytes(b'hello')
    record = io.finish_event(event(io), response_file=body)
    (tmp_path / record['response_file']).write_bytes(b'evil')
    io = setup_tape(tmp_path, 'offline_replay')
    with pytest.raises(io.TapeError, match='digest'):
        io.replay_event(boundary='sdk', operation='query', public_request={'symbol': 'ABC'})


def test_public_request_drops_secrets_and_preserves_semantics():
    io = io_module()
    result = io.public_http_request('POST', 'https://api.finra.org/data/group/otcMarket/name/otcDailyShortSaleVolume?api_key=SECRET&limit=10', body=b'{"symbols":["B","A"],"token":"SECRET"}', headers={'Authorization': 'Bearer SECRET', 'Accept': 'application/json'})
    assert 'SECRET' not in json.dumps(result)
    assert result['body']['symbols'] == ['B', 'A']
    assert result['headers'] == {'accept': 'application/json'}
    assert 'limit=10' in result['url']


def test_finra_pagination_headers_survive_capture_and_exact_replay(tmp_path):
    io=setup_tape(tmp_path)
    headers={'Record-Total':'12','Record-Offset':'0','Record-Limit':'13',
        'Total-Records-On-Page':'12','Set-Cookie':'PRIVATE','Authorization':'PRIVATE'}
    io.finish_event(event(io),status=200,safe_headers=headers)
    io=setup_tape(tmp_path,'offline_replay')
    record=io.replay_event(boundary='sdk',operation='query',public_request={'symbol':'ABC'})
    assert record['safe_headers'] == {'record-total':'12','record-offset':'0',
        'record-limit':'13','total-records-on-page':'12'}
    io.assert_replay_complete()


def test_stream_capture_is_lazy_and_partial_is_failure(tmp_path):
    io = setup_tape(tmp_path)
    consumed = []
    def source():
        for part in [b'one', b'two']:
            consumed.append(part)
            yield part
    capture = io.BodyCapture(event(io), status=200, safe_headers={}, max_bytes=100)
    stream = capture.iterate(source())
    assert consumed == []
    assert next(stream) == b'one'
    assert consumed == [b'one']
    capture.close()
    record = io.verify_tape(tmp_path)[0]
    assert record['failure']['type'] == 'IncompleteBody'
    assert (tmp_path / record['response_file']).read_bytes() == b'one'


def test_capture_limit_preserves_original_data(tmp_path):
    io = setup_tape(tmp_path)
    capture = io.BodyCapture(event(io), status=200, safe_headers={}, max_bytes=3)
    assert list(capture.iterate(iter([b'ab', b'cdef']))) == [b'ab', b'cdef']
    record = io.verify_tape(tmp_path)[0]
    assert record['failure']['type'] == 'CaptureLimit'
    assert record['response_bytes'] == 3


def test_mutation_preserves_parent(tmp_path):
    parent, child = tmp_path / 'parent', tmp_path / 'child'
    io = setup_tape(parent)
    recorded = io.finish_event(event(io), sdk_value={'rows': [1]})
    before = (parent / 'events' / (recorded['event_id'] + '.json')).read_bytes()
    io.create_mutation(parent_tape=parent, destination=child, event_id=recorded['event_id'], label='missing_rows', changes={'sdk_value': {'rows': []}})
    assert (parent / 'events' / (recorded['event_id'] + '.json')).read_bytes() == before
    io = setup_tape(child, 'offline_replay')
    mutated = io.replay_event(boundary='sdk', operation='query', public_request={'symbol': 'ABC'})
    assert mutated['sdk_value'] == {'rows': []}
    assert mutated['lane'] == 'controlled_mutation'
    assert mutated['parent']['event_sha256']


def test_httpx_real_stream_completion_and_timeout_forwarded(tmp_path):
    io = setup_tape(tmp_path)
    import httpx
    read = []
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for value in [b'one', b'two']:
                read.append(value)
                yield value
    observed = []
    def handle(request):
        observed.append(request.extensions['timeout'])
        return httpx.Response(200, stream=Stream())
    io._install_httpx(httpx)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with client.stream('GET', 'https://example.org/data', timeout=2.5) as response:
            assert read == []
            assert b''.join(response.iter_bytes()) == b'onetwo'
    record = io.verify_tape(tmp_path)[0]
    assert record['failure'] is None
    assert observed[0]['read'] == 2.5


def test_secret_crumb_not_in_descriptor():
    io = io_module()
    result = io.public_http_request('GET', 'https://query1.finance.yahoo.com/v8/chart/A?crumb=SECRET&period1=1')
    assert 'SECRET' not in json.dumps(result)


def test_cross_process_occurrence_reservation(tmp_path):
    import subprocess
    import sys
    io = setup_tape(tmp_path)
    script = '''from tools.live_workflow.recorded_io import *
import sys
configure_tape(mode='live_record', tape_dir=sys.argv[1], run_identity={'candidate':'abc','profile':'def'}, phase='daily')
for _ in range(5):
    e=reserve_event(boundary='sdk', operation='query', public_request={'symbol':'ABC'})
    finish_event(e,sdk_value=e['occurrence'])
'''
    children = [subprocess.Popen([sys.executable, '-c', script, str(tmp_path)]) for _ in range(4)]
    assert all(child.wait(timeout=20) == 0 for child in children)
    records = io.verify_tape(tmp_path)
    assert sorted(r['occurrence'] for r in records) == list(range(20))


def test_requests_record_and_replay_without_server(tmp_path):
    import subprocess
    import sys
    script = '''from tools.live_workflow.recorded_io import install_source_io,verify_tape
from http.server import BaseHTTPRequestHandler,HTTPServer
from threading import Thread
import json,requests,sys
mode,root,url=sys.argv[1:]
server=None
if mode=='live_record':
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header('Content-Length','6'); self.end_headers(); self.wfile.write(b'onetwo')
        def log_message(self,*args): pass
    server=HTTPServer(('127.0.0.1',0),Handler)
    Thread(target=server.serve_forever,daemon=True).start()
    url='http://127.0.0.1:'+str(server.server_port)+'/data'
install_source_io(mode=mode,tape_dir=root,run_identity={'candidate':'abc'},phase='daily')
with requests.get(url,stream=True,timeout=1) as response:
    assert b''.join(response.iter_content(3))==b'onetwo'
if server: server.shutdown();server.server_close()
print(url)
'''
    live = subprocess.run([sys.executable, '-c', script, 'live_record', str(tmp_path), 'unused'], capture_output=True, text=True, timeout=20)
    assert live.returncode == 0, live.stderr
    replay = subprocess.run([sys.executable, '-c', script, 'offline_replay', str(tmp_path), live.stdout.strip()], capture_output=True, text=True, timeout=20)
    assert replay.returncode == 0, replay.stderr


def test_httpx_async_stream_complete(tmp_path):
    import asyncio
    import httpx
    io = setup_tape(tmp_path)
    io._install_httpx(httpx)
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'hello'
    async def handle(request):
        return httpx.Response(200, stream=Stream())
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            async with client.stream('GET', 'https://example.org/data') as response:
                assert b''.join([chunk async for chunk in response.aiter_bytes()]) == b'hello'
    asyncio.run(run())
    assert io.verify_tape(tmp_path)[0]['failure'] is None


def test_replay_auth_transport_never_falls_through(tmp_path):
    import subprocess
    import sys
    setup_tape(tmp_path)
    script = '''from tools.live_workflow.recorded_io import *
import httpx,sys
install_source_io(mode='offline_replay',tape_dir=sys.argv[1],run_identity={'candidate':'abc','profile':'def'},phase='daily')
def forbidden(request): raise AssertionError('native transport reached')
with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
    try: client.post('https://auth.openai.com/oauth/token',json={'refresh_token':'SECRET'})
    except TapeError: pass
    else: raise AssertionError('auth request unexpectedly allowed')
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_record_payload_tampering_rejected(tmp_path):
    io = setup_tape(tmp_path)
    record = io.finish_event(event(io), sdk_value={'rows': [1]})
    path = tmp_path / 'events' / (record['event_id'] + '.json')
    record['sdk_value'] = {'rows': [2]}
    path.write_text(json.dumps(record))
    with pytest.raises(io.TapeError, match='record digest'):
        io.verify_tape(tmp_path)


def test_cross_process_replay_occurrences_share_explicit_cursor(tmp_path):
    import subprocess
    import sys
    import os
    tape, cursor = tmp_path / 'tape', tmp_path / 'cursor'
    io = setup_tape(tape)
    for value in range(4):
        io.finish_event(event(io), sdk_value=value)
    script = '''from tools.live_workflow.recorded_io import *
import sys
configure_tape(mode='offline_replay',tape_dir=sys.argv[1],run_identity={'candidate':'abc','profile':'def'},phase='daily')
print(replay_event(boundary='sdk',operation='query',public_request={'symbol':'ABC'})['sdk_value'])
'''
    env = {**os.environ, 'EVENTEDGE_LIVE_REPLAY_STATE': str(cursor)}
    children = [subprocess.Popen([sys.executable, '-c', script, str(tape)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for _ in range(4)]
    results = [child.communicate(timeout=20) for child in children]
    assert all(child.returncode == 0 for child in children), results
    assert sorted(int(out) for out, _ in results) == list(range(4))


def test_form_descriptor_preserves_duplicates():
    io = io_module()
    descriptor = io.public_http_request('POST', 'https://example.org/query', body='symbol=A&symbol=B&api_key=SECRET')
    assert descriptor['body'] == [['symbol', 'A'], ['symbol', 'B']]


def test_total_capture_budget_does_not_change_delivered_data(tmp_path, monkeypatch):
    monkeypatch.setenv('EVENTEDGE_LIVE_CAPTURE_RUN_BYTES', '4')
    io = setup_tape(tmp_path)
    first = io.BodyCapture(event(io), status=200, safe_headers={})
    second = io.BodyCapture(event(io), status=200, safe_headers={})
    assert list(first.iterate(iter([b'abc']))) == [b'abc']
    assert list(second.iterate(iter([b'def']))) == [b'def']
    records = io.verify_tape(tmp_path)
    assert sum(record['response_bytes'] for record in records) == 4
    assert any(record['failure'] and record['failure']['type'] == 'CaptureLimit' for record in records)


def test_swallowed_replay_miss_still_fails_completion(tmp_path):
    io = setup_tape(tmp_path)
    io.finish_event(event(io), sdk_value=1)
    io = setup_tape(tmp_path, 'offline_replay')
    try:
        io.replay_event(boundary='sdk', operation='query', public_request={'symbol': 'wrong'})
    except io.TapeError:
        pass
    io.replay_event(boundary='sdk', operation='query', public_request={'symbol': 'ABC'})
    with pytest.raises(io.TapeError, match='replay failures'):
        io.assert_replay_complete()


def test_recorded_source_timeout_replays_native_exception(tmp_path):
    import requests
    io = setup_tape(tmp_path)
    request = {'method': 'GET', 'url': 'https://example.org/data', 'body': None, 'headers': {}}
    reserved = io.reserve_event(boundary='http_decoded', operation='requests.send', public_request=request)
    record = io.finish_event(reserved, failure={'type': 'ReadTimeout', 'reason': 'native source call raised'})
    io = setup_tape(tmp_path, 'offline_replay')
    with pytest.raises(requests.exceptions.ReadTimeout):
        io._replay_body(record, 'requests')


def test_http_replay_body_is_streamed_and_partial_timeout_occurs_after_bytes(tmp_path):
    import requests
    io = setup_tape(tmp_path)
    source = tmp_path / 'input'
    source.write_bytes(b'one')
    record = io.finish_event(event(io), response_file=source, status=200, failure={'type': 'ReadTimeout', 'reason': 'body read failed'})
    io = setup_tape(tmp_path, 'offline_replay')
    body = io._replay_body(record, 'requests')
    assert body.read(3) == b'one'
    with pytest.raises(requests.exceptions.ReadTimeout):
        body.read(3)
    body.close()


@pytest.mark.parametrize('family', ['urllib', 'httpx', 'httpx2', 'curl', 'async_curl'])
def test_enabled_families_record_actual_local_http(tmp_path, family):
    import subprocess
    import sys
    script = '''from tools.live_workflow.recorded_io import install_source_io,verify_tape
from http.server import BaseHTTPRequestHandler,HTTPServer
from threading import Thread
import sys,asyncio,importlib
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200);self.send_header('Content-Length','5');self.end_headers();self.wfile.write(b'hello')
    def log_message(self,*args):pass
server=HTTPServer(('127.0.0.1',0),Handler)
Thread(target=server.serve_forever,daemon=True).start()
url='http://127.0.0.1:'+str(server.server_port)+'/data'
family=sys.argv[2]
install_source_io(mode='live_record',tape_dir=sys.argv[1],run_identity={'candidate':'abc'},phase='daily')
if family=='urllib':
    import urllib.request
    with urllib.request.urlopen(url,timeout=1) as response: assert response.read()==b'hello'
elif family in {'httpx','httpx2'}:
    module=importlib.import_module(family)
    with module.Client() as client:
        with client.stream('GET',url,timeout=1) as response: assert b''.join(response.iter_bytes())==b'hello'
elif family=='curl':
    from curl_cffi.requests import Session
    with Session(params={'symbol':['B','A']}) as client: assert client.get(url,timeout=1).content==b'hello'
else:
    from curl_cffi.requests import AsyncSession
    async def run():
        async with AsyncSession() as client: assert (await client.get(url,timeout=1)).content==b'hello'
    asyncio.run(run())
server.shutdown();server.server_close()
records=verify_tape(sys.argv[1])
assert len(records)==1, records
assert records[0]['failure'] is None, records
assert records[0]['response_bytes']==5, records
if family=='curl': assert records[0]['public_request']['session_params']=={'symbol':['B','A']}
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path), family], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('url', [
    'https://guce.yahoo.com/consent',
    'https://guce.yahoo.com/copyConsent?sessionId=SESSION_SENTINEL',
    'https://consent.yahoo.com/v2/collectConsent?sessionId=SESSION_SENTINEL',
    'https://login.yahoo.com/lookup',
])
def test_yahoo_auth_is_excluded_and_offline_denial_is_sticky(tmp_path, url):
    io = setup_tape(tmp_path)
    assert not io._recordable(url)
    assert list((tmp_path / 'events').iterdir()) == []
    io = setup_tape(tmp_path, 'offline_replay')
    with pytest.raises(io.TapeError):
        io._recordable(url)
    with pytest.raises(io.TapeError, match='replay failures'):
        io.assert_replay_complete()


def test_yahoo_auth_sentinels_redacted_from_public_descriptor():
    io = io_module()
    value = io.public_http_request('POST', 'https://example.org/data?csrfToken=CSRF_SENTINEL&sessionId=SESSION_SENTINEL&symbol=A',
                                  body={'csrfToken': 'CSRF_SENTINEL', 'sessionId': 'SESSION_SENTINEL', 'symbol': 'A'})
    assert 'SENTINEL' not in json.dumps(value)
    assert value['body'] == {'symbol': 'A'}


@pytest.mark.parametrize('asynchronous', [False, True])
def test_httpx_raw_stream_recording_distinct_and_no_double_capture(tmp_path, asynchronous):
    import asyncio
    import httpx
    io = setup_tape(tmp_path)
    io._install_httpx(httpx)
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'one'
            yield b'two'
    class AsyncStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'one'
            yield b'two'
    if asynchronous:
        async def call():
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=AsyncStream()))) as client:
                async with client.stream('GET', 'https://example.org/data') as response:
                    assert b''.join([c async for c in response.aiter_raw()]) == b'onetwo'
        asyncio.run(call())
    else:
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))) as client:
            with client.stream('GET', 'https://example.org/data') as response:
                assert b''.join(response.iter_raw()) == b'onetwo'
    record = io.verify_tape(tmp_path)[0]
    assert record['failure'] is None
    assert record['representation'] == 'http_raw_content'
    assert record['response_bytes'] == 6


def test_forbidden_model_auth_replay_is_sticky(tmp_path):
    io = setup_tape(tmp_path)
    io = setup_tape(tmp_path, 'offline_replay')
    with pytest.raises(io.TapeError):
        io._recordable('https://auth.openai.com/oauth/token')
    with pytest.raises(io.TapeError, match='replay failures'):
        io.assert_replay_complete()


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('status', [200, 401])
def test_curl_exact_replay_without_native_transport(tmp_path, monkeypatch, asynchronous, status):
    import asyncio
    import curl_cffi
    from curl_cffi.requests import Session, AsyncSession, Response, Headers, exceptions
    io = setup_tape(tmp_path)
    calls = []
    def native(self, method, url, *args, **kwargs):
        calls.append(url)
        response = Response()
        response.status_code = status
        response.ok = 200 <= status < 400
        response.content = b'{"value":"caf\xe9","occurrence":' + str(len(calls)).encode() + b'}'
        response.headers = Headers({'Content-Type':'application/json; charset=iso-8859-1',
            'Content-Encoding':'gzip', 'X-Private':'not retained'})
        return response
    async def async_native(self, method, url, *args, **kwargs):
        return native(self, method, url, *args, **kwargs)
    monkeypatch.setattr(Session, 'request', native)
    monkeypatch.setattr(AsyncSession, 'request', async_native)
    io._install_curl()
    def call():
        if asynchronous:
            async def run():
                async with AsyncSession() as client:
                    return await client.get('https://example.org/data')
            return asyncio.run(run())
        with Session() as client:
            return client.get('https://example.org/data')
    originals = [call(), call()]
    def forbidden(*args, **kwargs):
        pytest.fail('native Curl operation attempted')
    monkeypatch.setattr(curl_cffi.Curl, 'perform', forbidden)
    monkeypatch.setattr(curl_cffi.AsyncCurl, 'add_handle', forbidden)
    io = setup_tape(tmp_path, 'offline_replay')
    for original in originals:
        response = call()
        assert type(response) is Response and response.curl is None
        assert response.status_code == original.status_code
        assert response.ok == original.ok
        assert response.content == original.content
        assert response.text == original.text
        assert response.json() == original.json()
        assert response.headers['Content-Encoding'] == 'gzip'
        assert 'X-Private' not in response.headers
        assert response.request.url == 'https://example.org/data'
        assert response.request.method == 'GET'
        if status >= 400:
            with pytest.raises(exceptions.HTTPError) as error:
                response.raise_for_status()
            assert error.value.response is response
        else:
            response.raise_for_status()
    assert len(calls) == 2
    io.assert_replay_complete()
    with pytest.raises(io.TapeError, match='missing exact'):
        call()
    with pytest.raises(io.TapeError, match='replay failures'):
        io.assert_replay_complete()


@pytest.fixture
def curl_recording(tmp_path, monkeypatch):
    import asyncio
    from curl_cffi import requests as module
    io = setup_tape(tmp_path)
    calls = []
    def native(self, method, url, *args, **kwargs):
        calls.append(url)
        response = module.Response()
        response.content = b'{"value":1}'
        return response
    async def async_native(self, method, url, *args, **kwargs):
        return native(self, method, url, *args, **kwargs)
    monkeypatch.setattr(module.Session, 'request', native)
    monkeypatch.setattr(module.AsyncSession, 'request', async_native)
    io._install_curl()
    def call(asynchronous=False, *args, **kwargs):
        if asynchronous:
            async def run():
                async with module.AsyncSession() as client:
                    return await client.request('GET', 'https://example.org/data', *args, **kwargs)
            return asyncio.run(run())
        with module.Session() as client:
            return client.request('GET', 'https://example.org/data', *args, **kwargs)
    return io, calls, call


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('unsupported', [
    {'stream':True}, {'content_callback':lambda data:None}, {'content':b'uncaptured'},
    {'files':{'file':b'uncaptured'}}, {'multipart':object()},
    {'default_encoding':lambda data:'utf-8'},
])
def test_curl_unsupported_replay_forms_fail_sticky(tmp_path, curl_recording, asynchronous, unsupported):
    io, calls, call = curl_recording
    call(asynchronous)
    io = setup_tape(tmp_path, 'offline_replay')
    with pytest.raises(io.TapeError, match='unsupported curl replay request'):
        call(asynchronous, **unsupported)
    assert len(calls) == 1
    with pytest.raises(io.TapeError, match='replay failures'):
        io.assert_replay_complete()


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('fault', ['incomplete', 'representation', 'status', 'missing_body', 'unfinished'])
def test_curl_incompatible_capture_fails_sticky(tmp_path, curl_recording, asynchronous, fault):
    io, calls, call = curl_recording
    call(asynchronous)
    record = io.verify_tape(tmp_path)[0]
    path = tmp_path/'events'/(record['event_id']+'.json')
    if fault == 'unfinished':
        path.unlink()
    else:
        if fault == 'incomplete':
            record['failure'] = {'type':'IncompleteBody', 'reason':'controlled incomplete body'}
        elif fault == 'representation':
            record['representation'] = 'http_raw'
        elif fault == 'status':
            record['status'] = None
        else:
            record.update(response_file=None, response_sha256=None, response_bytes=0)
        record.pop('record_sha256')
        record['record_sha256'] = io._digest(io._canonical(record))
        path.write_bytes(io._canonical(record))
    io = setup_tape(tmp_path, 'offline_replay')
    with pytest.raises(io.TapeError, match='missing exact|incompatible curl capture'):
        call(asynchronous)
    assert len(calls) == 1
    with pytest.raises(io.TapeError, match='replay failures'):
        io.assert_replay_complete()


@pytest.mark.parametrize('asynchronous', [False, True])
def test_curl_exact_request_and_phase_still_required(tmp_path, curl_recording, asynchronous):
    io, calls, call = curl_recording
    call(asynchronous, params={'symbol':'ABC'})
    io = setup_tape(tmp_path, 'offline_replay')
    with pytest.raises(io.TapeError, match='missing exact'):
        call(asynchronous, params={'symbol':'XYZ'})
    io = setup_tape(tmp_path, 'offline_replay', phase='preflight')
    with pytest.raises(io.TapeError, match='missing exact'):
        call(asynchronous, params={'symbol':'ABC'})
    assert len(calls) == 1


def test_curl_positional_request_arguments_fail_sticky(tmp_path, curl_recording):
    io, calls, call = curl_recording
    call()
    io = setup_tape(tmp_path, 'offline_replay')
    with pytest.raises(io.TapeError, match='unsupported curl replay request'):
        call(False, {'uncaptured':'positional params'})
    with pytest.raises(io.TapeError, match='replay failures'):
        io.assert_replay_complete()


def test_yahoo_consent_live_html_and_tokens_never_enter_tape(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from curl_cffi.requests import Session
    io = setup_tape(tmp_path)
    response = SimpleNamespace(status_code=200, content=b'<input name="csrfToken" value="CSRF_SENTINEL"><input name="sessionId" value="SESSION_SENTINEL">', headers={})
    def native(self, method, url, *args, **kwargs):
        return response
    monkeypatch.setattr(Session, 'request', native)
    io._install_curl()
    with Session() as client:
        assert client.get('https://guce.yahoo.com/consent') is response
        assert client.post('https://consent.yahoo.com/v2/collectConsent?sessionId=SESSION_SENTINEL', data={'csrfToken': 'CSRF_SENTINEL', 'sessionId': 'SESSION_SENTINEL'}) is response
    assert io.verify_tape(tmp_path) == []
    assert not any(b'SENTINEL' in path.read_bytes() for path in tmp_path.rglob('*') if path.is_file())


@pytest.mark.parametrize('raw', [False, True])
def test_httpx_encoded_and_decoded_replay_keep_exact_bytes(tmp_path, raw):
    import gzip
    import httpx
    io = setup_tape(tmp_path)
    io._install_httpx(httpx)
    encoded = gzip.compress(b'hello')
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield encoded
    def native(request):
        return httpx.Response(200, headers={'Content-Encoding': 'gzip'}, stream=Stream())
    def consume(client):
        with client.stream('GET', 'https://example.org/data') as response:
            return b''.join(response.iter_raw() if raw else response.iter_bytes())
    with httpx.Client(transport=httpx.MockTransport(native)) as client:
        assert consume(client) == (encoded if raw else b'hello')
    record = io.verify_tape(tmp_path)[0]
    assert record['representation'] == ('http_raw_content' if raw else 'http_decoded')
    assert record['response_bytes'] == (len(encoded) if raw else 5)
    io = setup_tape(tmp_path, 'offline_replay')
    def forbidden(request):
        raise AssertionError('network fallback')
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        assert consume(client) == (encoded if raw else b'hello')
    io.assert_replay_complete()


def test_unsupported_public_request_replay_denial_is_sticky(tmp_path):
    io = setup_tape(tmp_path)
    io = setup_tape(tmp_path, 'offline_replay')
    with pytest.raises(io.TapeError):
        io.public_http_request('POST', 'https://example.org/data', body=iter([b'body']))
    with pytest.raises(io.TapeError, match='replay failures'):
        io.assert_replay_complete()
