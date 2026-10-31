"""Private exact-request tape and transparent source HTTP observation.

HTTP bodies are decoded bytes delivered to the application, never claimed as
compressed wire bytes. Incomplete, oversized and unsupported observations remain
explicit failures. This is an acceptance hook, not a replacement HTTP stack.
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import functools
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_BODY_BYTES = 256 * 1024 * 1024
MAX_RUN_BODY_BYTES = 8 * 1024 * 1024 * 1024
MAX_RECORD_BYTES = 8 * 1024 * 1024
_CONFIG = None
_LOCK = threading.RLock()
_INSTALLED = False
_REPLAY_COUNTS = {}
_CONSUMED = set()
_REPLAY_FAILURES = []
_SECRET_KEYS = frozenset({'authorization', 'proxy_authorization', 'cookie', 'set_cookie', 'apikey', 'api_key', 'api_token', 'token', 'access_token', 'refresh_token', 'client_secret', 'password', 'secret', 'key', 'x_api_key', 'crumb', 'csrftoken', 'csrf_token', 'sessionid', 'session_id'})
_PUBLIC_HEADERS = frozenset({'accept', 'content-type', 'range', 'if-modified-since', 'if-none-match'})
_RESPONSE_HEADERS = frozenset({'content-type', 'content-length', 'content-encoding', 'date', 'etag', 'last-modified', 'retry-after', 'x-ratelimit-remaining',
    'record-total', 'record-offset', 'record-limit', 'total-records-on-page'})


class TapeError(RuntimeError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _private_write(path, data, *, replace=False):
    path = Path(path)
    fd, scratch = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if replace:
            os.replace(scratch, path)
        else:
            os.link(scratch, path)
            os.unlink(scratch)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)


@contextlib.contextmanager
def _tape_lock(root):
    with _LOCK:
        fd = os.open(root / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def configure_tape(*, mode, tape_dir, run_identity, phase):
    global _CONFIG
    if mode not in {'live_record', 'offline_replay'} or not phase or not isinstance(run_identity, dict) or not run_identity:
        raise TapeError('invalid tape configuration')
    root = Path(tape_dir).absolute()
    cursor = os.environ.get('EVENTEDGE_LIVE_REPLAY_STATE') if mode == 'offline_replay' else None
    cursor = Path(cursor).absolute() if cursor else None
    body_limit = int(os.environ.get('EVENTEDGE_LIVE_CAPTURE_BODY_BYTES', MAX_BODY_BYTES))
    run_limit = int(os.environ.get('EVENTEDGE_LIVE_CAPTURE_RUN_BYTES', MAX_RUN_BODY_BYTES))
    if min(body_limit, run_limit) < 1:
        raise TapeError('capture limits must be positive')
    config = {'mode': mode, 'root': root, 'identity': json.loads(_canonical(run_identity)), 'phase': str(phase), 'cursor': cursor, 'body_limit': body_limit, 'run_limit': run_limit}
    if _CONFIG is not None:
        if config != _CONFIG:
            raise TapeError('incompatible tape reconfiguration')
        return
    if root.is_symlink():
        raise TapeError('tape symlink denied')
    if mode == 'live_record':
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        for name in ('events', 'bodies', 'reservations'):
            directory = root / name
            if directory.is_symlink():
                raise TapeError('tape symlink denied')
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        with _tape_lock(root):
            identity_file = root / 'identity.json'
            if not identity_file.exists():
                _private_write(identity_file, _canonical(run_identity))
    identity_file = root / 'identity.json'
    if not identity_file.is_file() or json.loads(identity_file.read_bytes()) != run_identity:
        raise TapeError('tape identity mismatch')
    if cursor is not None:
        if cursor == root or root in cursor.parents or cursor.is_symlink():
            raise TapeError('replay cursor must be outside immutable tape')
        cursor.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(cursor, 0o700)
        with _tape_lock(cursor):
            binding = {'tape': str(root.resolve()), 'identity': run_identity}
            binding_path = cursor / 'binding.json'
            if not binding_path.exists():
                _private_write(binding_path, _canonical(binding))
            elif json.loads(binding_path.read_bytes()) != binding:
                raise TapeError('replay cursor identity mismatch')
    _CONFIG = config


def _configuration():
    if _CONFIG is None:
        raise TapeError('tape not configured')
    return _CONFIG


def _key(*, boundary, operation, public_request):
    cfg = _configuration()
    return {'schema_version': 1, 'run_identity': cfg['identity'], 'phase': cfg['phase'],
            'boundary': boundary, 'operation': operation, 'public_request': public_request,
            'request_sha256': _digest(_canonical(public_request))}


def _event_id(key, occurrence):
    return _digest(_canonical({**key, 'occurrence': occurrence}))


def reserve_event(*, boundary, operation, public_request):
    cfg = _configuration()
    if cfg['mode'] != 'live_record':
        raise TapeError('reserve requires live_record')
    key = _key(boundary=boundary, operation=operation, public_request=public_request)
    base_id = _digest(_canonical(key))
    with _tape_lock(cfg['root']):
        counter_file = cfg['root'] / 'reservations' / (base_id + '.counter')
        occurrence = int(counter_file.read_text()) if counter_file.exists() else 0
        _private_write(counter_file, str(occurrence + 1).encode(), replace=True)
        event = {**key, 'occurrence': occurrence, 'event_id': _event_id(key, occurrence),
                 'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 'representation': boundary, 'lane': 'native_observation'}
        _private_write(cfg['root'] / 'reservations' / (event['event_id'] + '.json'), _canonical(event))
    return event


def _safe_headers(headers):
    return {str(k).lower(): str(v)[:2048] for k, v in (headers or {}).items() if str(k).lower() in _RESPONSE_HEADERS}


def finish_event(event, *, response_file=None, sdk_value=None, status=None, safe_headers=None, failure=None, representation=None):
    cfg = _configuration()
    if cfg['mode'] != 'live_record':
        raise TapeError('finish requires live_record')
    root = cfg['root']
    reservation = root / 'reservations' / (event['event_id'] + '.json')
    if not reservation.is_file() or json.loads(reservation.read_bytes()) != event:
        raise TapeError('invalid event reservation')
    record = {**event, 'sdk_value': sdk_value, 'status': status, 'safe_headers': _safe_headers(safe_headers),
              'failure': failure, 'finished_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'response_file': None, 'response_sha256': None, 'response_bytes': 0}
    if representation is not None:
        record['representation'] = representation
    if response_file is not None:
        source = Path(response_file)
        target = root / 'bodies' / (event['event_id'] + '.bin')
        if source != target:
            # Bounded copy avoids loading source captures into memory.
            with source.open('rb') as src:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
        digest, size = _file_digest(target)
        record.update(response_file=str(target.relative_to(root)), response_sha256=digest, response_bytes=size)
    if failure is not None and len(_canonical(failure)) > 8192:
        raise TapeError('failure exceeds public bound')
    payload = _canonical(record)
    if len(payload) > MAX_RECORD_BYTES:
        raise TapeError('record exceeds bound')
    record['record_sha256'] = _digest(payload)
    _private_write(root / 'events' / (event['event_id'] + '.json'), _canonical(record))
    return record


def _file_digest(path):
    digest, size = hashlib.sha256(), 0
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _validate_record(root, path):
    record = json.loads(path.read_bytes())
    if record.get('record_sha256') != _digest(_canonical({k: v for k, v in record.items() if k != 'record_sha256'})):
        raise TapeError('record digest mismatch')
    if record['response_file'] is not None:
        body = root / record['response_file']
        if body.is_symlink() or body.resolve().parent != (root / 'bodies').resolve():
            raise TapeError('invalid response body path')
        if not body.is_file() or _file_digest(body) != (record['response_sha256'], record['response_bytes']):
            raise TapeError('response digest mismatch')
    return record


def verify_tape(tape_dir):
    root = Path(tape_dir)
    return [_validate_record(root, p) for p in sorted((root / 'events').glob('*.json'))]


def _replay_event(*, boundary, operation, public_request):
    cfg = _configuration()
    if cfg['mode'] != 'offline_replay':
        raise TapeError('replay requires offline_replay')
    key = _key(boundary=boundary, operation=operation, public_request=public_request)
    base = _digest(_canonical(key))
    cursor = cfg['cursor']
    with _tape_lock(cursor) if cursor is not None else _LOCK:
        counter_file = cursor / (base + '.counter') if cursor is not None else None
        occurrence = int(counter_file.read_text()) if counter_file is not None and counter_file.exists() else _REPLAY_COUNTS.get(base, 0)
        event_id = _event_id(key, occurrence)
        path = cfg['root'] / 'events' / (event_id + '.json')
        if not path.is_file():
            raise TapeError('missing exact tape entry')
        record = _validate_record(cfg['root'], path)
        if any(record.get(k) != v for k, v in key.items()) or record.get('occurrence') != occurrence or record.get('event_id') != event_id:
            raise TapeError('record identity mismatch')
        _REPLAY_COUNTS[base] = occurrence + 1
        if counter_file is not None:
            _private_write(counter_file, str(occurrence + 1).encode(), replace=True)
            _private_write(cursor / (event_id + '.consumed'), _canonical({'phase': cfg['phase']}))
        _CONSUMED.add(event_id)
    return record



def record_replay_failure(reason):
    """Audit a public reason code even when native application catches TapeError."""
    if not isinstance(reason, str) or len(reason) > 128 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- ' for c in reason):
        reason = 'invalid_replay_outcome'
    _REPLAY_FAILURES.append(reason)
    cfg = _configuration()
    if cfg['cursor'] is not None:
        fd, path = tempfile.mkstemp(prefix='failure-', suffix='.json', dir=cfg['cursor'])
        with os.fdopen(fd, 'wb') as output:
            output.write(_canonical({'phase': cfg['phase'], 'reason': reason}))


def replay_event(*, boundary, operation, public_request):
    try:
        return _replay_event(boundary=boundary, operation=operation, public_request=public_request)
    except (TapeError, ValueError, OSError):
        record_replay_failure('exact_lookup_failed')
        raise

def assert_replay_complete():
    cfg = _configuration()
    if _REPLAY_FAILURES or (cfg['cursor'] is not None and any(cfg['cursor'].glob('failure-*.json'))):
        raise TapeError('recorded replay failures')
    expected = {r['event_id'] for r in verify_tape(cfg['root']) if r['phase'] == cfg['phase']}
    consumed = _CONSUMED
    if cfg['cursor'] is not None:
        consumed = {p.stem for p in cfg['cursor'].glob('*.consumed') if json.loads(p.read_bytes())['phase'] == cfg['phase']}
    if expected != consumed:
        raise TapeError('unconsumed tape entries')


def create_mutation(*, parent_tape, destination, event_id, label, changes):
    parent, destination = Path(parent_tape), Path(destination)
    if not label or set(changes) - {'sdk_value', 'failure', 'status', 'safe_headers', 'response_body'}:
        raise TapeError('invalid mutation')
    records = verify_tape(parent)
    if not any(r['event_id'] == event_id for r in records):
        raise TapeError('mutation parent event missing')
    if destination.exists() or parent.resolve() in destination.resolve().parents:
        raise TapeError('mutation destination must be new and outside parent')
    shutil.copytree(parent, destination)
    changed = None
    for record in records:
        record['lane'] = 'controlled_mutation'
        if record['event_id'] == event_id:
            record['parent'] = {'event_id': event_id, 'event_sha256': record['record_sha256'], 'label': label}
            record['representation'] = 'controlled_mutation'
            record.update({k: v for k, v in changes.items() if k != 'response_body'})
            if 'response_body' in changes:
                body = changes['response_body']
                if not isinstance(body, bytes) or len(body) > MAX_BODY_BYTES:
                    raise TapeError('invalid mutated body')
                relative = 'bodies/' + event_id + '.bin'
                _private_write(destination / relative, body, replace=True)
                record.update(response_file=relative, response_sha256=_digest(body), response_bytes=len(body))
            changed = record
        record.pop('record_sha256')
        record['record_sha256'] = _digest(_canonical(record))
        _private_write(destination / 'events' / (record['event_id'] + '.json'), _canonical(record), replace=True)
    return changed


def _redact(value):
    if isinstance(value, dict):
        return {str(k): _redact(v) for k, v in value.items() if str(k).lower().replace('-', '_') not in _SECRET_KEYS}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if _CONFIG is not None and _CONFIG['mode'] == 'offline_replay':
        record_replay_failure('unsupported_public_request_value')
    raise TapeError('unsupported public request value')


def public_http_request(method, url, *, body=None, headers=None):
    parsed = urlsplit(str(url))
    if parsed.username or parsed.password:
        if _CONFIG is not None and _CONFIG['mode'] == 'offline_replay':
            record_replay_failure('credential_url_unsupported')
        raise TapeError('credential URL unsupported')
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if k.lower().replace('-', '_') not in _SECRET_KEYS]
    public_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ''))
    public_body = None
    if body is not None:
        if isinstance(body, (bytes, str)):
            raw = body.decode('utf-8') if isinstance(body, bytes) else body
            try:
                public_body = _redact(json.loads(raw))
            except (ValueError, UnicodeError):
                if '=' in raw:
                    public_body = [[k, v] for k, v in parse_qsl(raw, keep_blank_values=True) if k.lower().replace('-', '_') not in _SECRET_KEYS]
                else:
                    # Unknown bodies might contain credentials: retain a digest,
                    # never arbitrary body text in the descriptor.
                    public_body = {'opaque_sha256': _digest(raw.encode()), 'bytes': len(raw.encode())}
        elif isinstance(body, (dict, list)):
            public_body = _redact(body)
        else:
            if _CONFIG is not None and _CONFIG['mode'] == 'offline_replay':
                record_replay_failure('streamed_request_body_unsupported')
            raise TapeError('streamed request body capture unsupported')
    return {'method': str(method).upper(), 'url': public_url, 'body': public_body,
            'headers': {str(k).lower(): str(v) for k, v in (headers or {}).items() if str(k).lower() in _PUBLIC_HEADERS}}



def _claim_capture_budget(wanted):
    cfg = _configuration()
    with _tape_lock(cfg['root']):
        path = cfg['root'] / 'capture-budget.json'
        budget = json.loads(path.read_bytes()) if path.exists() else {'limit': cfg['run_limit'], 'claimed': 0}
        if budget['limit'] != cfg['run_limit']:
            raise TapeError('inconsistent cross-process capture limit')
        granted = min(wanted, max(0, budget['limit'] - budget['claimed'])) if wanted >= 0 else wanted
        budget['claimed'] += granted
        _private_write(path, _canonical(budget), replace=True)
        return granted

class BodyCapture:
    """Observe only bytes consumed; exceeding the bound never changes delivery."""
    def __init__(self, event, *, status, safe_headers, max_bytes=None, representation=None):
        self.event, self.status, self.headers = event, status, safe_headers
        self.representation = representation
        self.path = _configuration()['root'] / 'bodies' / (event['event_id'] + '.bin')
        self.file = os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb')
        self.maximum = max_bytes if max_bytes is not None else _configuration()['body_limit']
        self.count, self.done, self.overflow, self.budget_credit = 0, False, False, 0

    def add(self, chunk):
        if self.done or self.overflow:
            return
        if not isinstance(chunk, bytes):
            self.close({'type': 'UnsupportedBody', 'reason': 'non-byte transport body'})
            return
        wanted = min(len(chunk), max(0, self.maximum - self.count))
        if wanted > self.budget_credit:
            self.budget_credit += _claim_capture_budget(max(wanted - self.budget_credit, 1024 * 1024))
        retained = chunk[:min(wanted, self.budget_credit)]
        self.budget_credit -= len(retained)
        self.file.write(retained)
        self.count += len(retained)
        self.overflow |= len(retained) != len(chunk)

    def close(self, failure=None, *, complete=False):
        if self.done:
            return
        self.done = True
        self.file.close()
        if self.budget_credit:
            _claim_capture_budget(-self.budget_credit)
            self.budget_credit = 0
        if self.overflow:
            failure = {'type': 'CaptureLimit', 'reason': 'body capture exceeded bound'}
        elif failure is None and not complete:
            failure = {'type': 'IncompleteBody', 'reason': 'response closed before EOF'}
        finish_event(self.event, response_file=self.path, status=self.status, safe_headers=self.headers, failure=failure, representation=self.representation)

    def iterate(self, chunks):
        try:
            for chunk in chunks:
                self.add(chunk)
                yield chunk
        except Exception as exc:
            self.close({'type': type(exc).__name__, 'reason': 'body read failed'})
            raise
        else:
            self.close(complete=True)


def _failure(exc):
    return {'type': type(exc).__name__, 'reason': 'native source call raised'}


def _recordable(url):
    parsed = urlsplit(str(url))
    host = (parsed.hostname or '').lower()
    # Native model/auth transports have their own logical recording boundary.
    # Token and crumb responses are secrets, not source observations.
    excluded = any(host == d or host.endswith('.' + d) for d in ('openai.com', 'chatgpt.com', 'anthropic.com'))
    excluded |= host.rstrip('.') in {'guce.yahoo.com', 'consent.yahoo.com', 'login.yahoo.com'}
    path = parsed.path.lower()
    excluded |= any(part in path for part in ('/oauth', '/login', '/auth/', '/getcrumb'))
    if excluded and _configuration()['mode'] == 'offline_replay':
        record_replay_failure('unrecorded_authentication_http')
        raise TapeError('unrecorded authentication/model HTTP boundary denied in replay')
    return not excluded


def _http_record(boundary, operation, request):
    cfg = _configuration()
    if cfg['mode'] == 'offline_replay':
        return replay_event(boundary=boundary, operation=operation, public_request=request)
    return reserve_event(boundary=boundary, operation=operation, public_request=request)


class _ReplayBody:
    def __init__(self, stream, failure):
        self._source, self.failure = stream, failure
    def __getattr__(self, name):
        return getattr(self._source, name)
    def read(self, amount=-1, **kwargs):
        data = self._source.read(amount)
        if self.failure is not None and (not data or amount is None or amount < 0):
            raise self.failure
        return data
    def readline(self, limit=-1):
        data = self._source.readline(limit)
        if not data and self.failure is not None:
            raise self.failure
        return data
    def __iter__(self):
        return self
    def __next__(self):
        data = self.readline()
        if not data:
            raise StopIteration
        return data


def _replay_body(record, family):
    failure = None
    if record['failure'] is not None:
        kind = record['failure'].get('type')
        allowed = {'Timeout', 'TimeoutError', 'ReadTimeout', 'ConnectTimeout', 'PoolTimeout', 'WriteTimeout',
                   'ConnectionError', 'ConnectError', 'ReadError', 'RemoteProtocolError', 'URLError'}
        if kind not in allowed:
            record_replay_failure('unreplayable_source_capture')
            raise TapeError('source recording failed or incomplete')
        if kind == 'TimeoutError':
            failure = TimeoutError('recorded source timeout')
        else:
            module = importlib.import_module('requests.exceptions' if family == 'requests' else 'urllib.error' if family == 'urllib' else family)
            cls = getattr(module, kind, None)
            if cls is None:
                record_replay_failure('unsupported_source_failure_type')
                raise TapeError('unsupported source failure type')
            failure = cls('recorded source failure')
        if record['status'] is None:
            raise failure
    stream = (_configuration()['root'] / record['response_file']).open('rb') if record['response_file'] else io.BytesIO()
    return _ReplayBody(stream, failure)


def _httpx_replay_response(module, record, request, asynchronous):
    body = _replay_body(record, module.__name__)
    if asynchronous:
        class Stream(module.AsyncByteStream):
            async def __aiter__(self):
                while chunk := body.read(65536):
                    yield chunk
            async def aclose(self):
                body.close()
    else:
        class Stream(module.SyncByteStream):
            def __iter__(self):
                while chunk := body.read(65536):
                    yield chunk
            def close(self):
                body.close()
    headers = dict(record['safe_headers'])
    if record['representation'] == 'http_decoded':
        headers.pop('content-encoding', None)
        headers['content-length'] = str(record['response_bytes'])
    return module.Response(record['status'], headers=headers, stream=Stream(), request=request)


def _install_requests():
    import requests
    original = requests.adapters.HTTPAdapter.send
    if getattr(original, '_acceptance_capture', False):
        return
    @functools.wraps(original)
    def send(self, request, *args, **kwargs):
        if not _recordable(request.url):
            return original(self, request, *args, **kwargs)
        descriptor = public_http_request(request.method, request.url, body=request.body, headers=request.headers)
        event = _http_record('http_decoded', 'requests.send', descriptor)
        if _configuration()['mode'] == 'offline_replay':
            response = requests.Response()
            response.status_code, response.headers, response.url = event['status'], requests.structures.CaseInsensitiveDict(event['safe_headers']), request.url
            response.request = request
            response.raw = _replay_body(event, 'requests')
            return response
        try:
            response = original(self, request, *args, **kwargs)
        except Exception as exc:
            finish_event(event, failure=_failure(exc))
            raise
        capture = BodyCapture(event, status=response.status_code, safe_headers=response.headers)
        original_iter, original_close = response.iter_content, response.close
        def iter_content(chunk_size=1, decode_unicode=False):
            # Decode Unicode only after retaining the byte representation, using
            # Requests' own incremental decoder as the unwrapped method does.
            chunks = capture.iterate(original_iter(chunk_size=chunk_size, decode_unicode=False))
            if decode_unicode:
                chunks = requests.utils.stream_decode_response_unicode(chunks, response)
            return chunks
        def close():
            try:
                return original_close()
            finally:
                capture.close()
        response.iter_content, response.close = iter_content, close
        return response
    send._acceptance_capture = True
    requests.adapters.HTTPAdapter.send = send


class _UrllibResponse:
    def __init__(self, response, capture):
        self._response, self._capture = response, capture
    def __getattr__(self, name):
        return getattr(self._response, name)
    def read(self, amount=None):
        try:
            value = self._response.read() if amount is None else self._response.read(amount)
        except Exception as exc:
            self._capture.close(_failure(exc))
            raise
        self._capture.add(value)
        if not value or amount is None or amount < 0:
            self._capture.close(complete=True)
        return value
    def readline(self, limit=-1):
        value = self._response.readline(limit)
        self._capture.add(value)
        if not value:
            self._capture.close(complete=True)
        return value
    def readinto(self, buffer):
        value = self.read(len(buffer))
        buffer[:len(value)] = value
        return len(value)
    def __iter__(self):
        return self
    def __next__(self):
        value = self.readline()
        if not value:
            raise StopIteration
        return value
    def close(self):
        try:
            return self._response.close()
        finally:
            self._capture.close()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()


def _install_urllib():
    import urllib.request
    original = urllib.request.OpenerDirector.open
    if getattr(original, '_acceptance_capture', False):
        return
    @functools.wraps(original)
    def open_recorded(self, fullurl, data=None, *args, **kwargs):
        if isinstance(fullurl, urllib.request.Request):
            method, url, headers = fullurl.get_method(), fullurl.full_url, dict(fullurl.header_items())
            body = data if data is not None else fullurl.data
        else:
            method, url, headers, body = 'POST' if data is not None else 'GET', fullurl, {}, data
        if not _recordable(url):
            return original(self, fullurl, data, *args, **kwargs)
        event = _http_record('http_body', 'urllib.open', public_http_request(method, url, body=body, headers=headers))
        if _configuration()['mode'] == 'offline_replay':
            from email.message import Message
            from urllib.response import addinfourl
            message = Message()
            for key, value in event['safe_headers'].items():
                message[key] = value
            return addinfourl(_replay_body(event, 'urllib'), message, url, event['status'])
        try:
            response = original(self, fullurl, data, *args, **kwargs)
        except Exception as exc:
            finish_event(event, failure=_failure(exc))
            raise
        return _UrllibResponse(response, BodyCapture(event, status=response.status, safe_headers=response.headers))
    open_recorded._acceptance_capture = True
    urllib.request.OpenerDirector.open = open_recorded


def _observe_httpx_response(response, event, asynchronous):
    capture = BodyCapture(event, status=response.status_code, safe_headers=response.headers)
    if response.is_stream_consumed:
        capture.representation = 'http_decoded'
        capture.add(response.content)
        capture.close(complete=True)
        return response
    # iter_bytes calls iter_raw internally. Only the outermost application
    # consumer records; the nested iterator retains its original behavior.
    reading = [0]
    if asynchronous:
        original_close = response.aclose
        def wrap_iterator(original, representation):
            async def iterator(*args, **kwargs):
                if reading[0]:
                    async for chunk in original(*args, **kwargs):
                        yield chunk
                    return
                reading[0] += 1
                capture.representation = representation
                try:
                    async for chunk in original(*args, **kwargs):
                        capture.add(chunk)
                        yield chunk
                except Exception as exc:
                    capture.close(_failure(exc))
                    raise
                else:
                    capture.close(complete=True)
                finally:
                    reading[0] -= 1
                    capture.close()
            return iterator
        async def aclose():
            try:
                return await original_close()
            finally:
                if not reading[0]:
                    capture.close()
        response.aiter_bytes = wrap_iterator(response.aiter_bytes, 'http_decoded')
        response.aiter_raw = wrap_iterator(response.aiter_raw, 'http_raw_content')
        response.aclose = aclose
    else:
        original_close = response.close
        def wrap_iterator(original, representation):
            def iterator(*args, **kwargs):
                if reading[0]:
                    yield from original(*args, **kwargs)
                    return
                reading[0] += 1
                capture.representation = representation
                try:
                    yield from capture.iterate(original(*args, **kwargs))
                finally:
                    reading[0] -= 1
                    capture.close()
            return iterator
        def close():
            try:
                return original_close()
            finally:
                if not reading[0]:
                    capture.close()
        response.iter_bytes = wrap_iterator(response.iter_bytes, 'http_decoded')
        response.iter_raw = wrap_iterator(response.iter_raw, 'http_raw_content')
        response.close = close
    return response


def _install_httpx(module):
    for cls, asynchronous in ((module.Client, False), (module.AsyncClient, True)):
        original = cls._send_single_request
        if getattr(original, '_acceptance_capture', False):
            continue
        def make_wrapper(original, asynchronous):
            def prepare(request):
                try:
                    body = request.content
                except module.RequestNotRead:
                    if _configuration()['mode'] == 'offline_replay':
                        record_replay_failure('unsupported_streamed_httpx_request')
                    raise TapeError('streamed HTTPX request body unsupported') from None
                return _http_record('http_response', module.__name__ + '.send', public_http_request(request.method, str(request.url), body=body or None, headers=request.headers))
            if asynchronous:
                @functools.wraps(original)
                async def wrapped(self, request):
                    if not _recordable(request.url):
                        return await original(self, request)
                    event = prepare(request)
                    if _configuration()['mode'] == 'offline_replay':
                        return _httpx_replay_response(module, event, request, asynchronous)
                    try:
                        response = await original(self, request)
                    except Exception as exc:
                        finish_event(event, failure=_failure(exc))
                        raise
                    return _observe_httpx_response(response, event, True)
            else:
                @functools.wraps(original)
                def wrapped(self, request):
                    if not _recordable(request.url):
                        return original(self, request)
                    event = prepare(request)
                    if _configuration()['mode'] == 'offline_replay':
                        return _httpx_replay_response(module, event, request, asynchronous)
                    try:
                        response = original(self, request)
                    except Exception as exc:
                        finish_event(event, failure=_failure(exc))
                        raise
                    return _observe_httpx_response(response, event, False)
            wrapped._acceptance_capture = True
            return wrapped
        cls._send_single_request = make_wrapper(original, asynchronous)


def _install_curl():
    try:
        from curl_cffi import requests as module
    except ImportError:
        return
    for cls, asynchronous in ((module.Session, False), (module.AsyncSession, True)):
        original = cls.request
        if getattr(original, '_acceptance_capture', False):
            continue
        def make_wrapper(original, asynchronous):
            def prepare(session, method, url, kwargs):
                # Session public arguments only; cookies/proxies/auth are omitted.
                from urllib.parse import urlencode
                if kwargs.get('params'):
                    url = str(url) + ('&' if '?' in str(url) else '?') + urlencode(kwargs['params'], doseq=True)
                descriptor = public_http_request(method, url, body=kwargs.get('json', kwargs.get('data')), headers=kwargs.get('headers'))
                descriptor['session_params'] = _redact(getattr(session, 'params', None))
                descriptor['session_headers'] = {str(k).lower(): str(v) for k, v in getattr(session, 'headers', {}).items() if str(k).lower() in _PUBLIC_HEADERS}
                base_url = getattr(session, 'base_url', None)
                descriptor['base_url'] = public_http_request('GET', base_url)['url'] if base_url else None
                return _http_record('sdk_decoded_http', 'curl_cffi.request', descriptor)
            def observe(response, event, stream):
                if stream:
                    # curl_cffi performs streaming in an executor. The native
                    # transport remains unchanged; this boundary is not replayable.
                    finish_event(event, status=response.status_code, safe_headers=response.headers, failure={'type': 'UnsupportedBoundary', 'reason': 'curl streaming body not retained'})
                else:
                    capture = BodyCapture(event, status=response.status_code, safe_headers=response.headers)
                    capture.add(response.content)
                    capture.close(complete=True)
                return response
            if asynchronous:
                @functools.wraps(original)
                async def wrapped(self, method, url, *args, **kwargs):
                    if not _recordable(url):
                        return await original(self, method, url, *args, **kwargs)
                    event = prepare(self, method, url, kwargs)
                    if _configuration()['mode'] == 'offline_replay':
                        record_replay_failure('unsupported_curl_sdk_replay')
                        raise TapeError('curl SDK replay unsupported; retained evidence only')
                    try:
                        response = await original(self, method, url, *args, **kwargs)
                    except Exception as exc:
                        finish_event(event, failure=_failure(exc))
                        raise
                    return observe(response, event, kwargs.get('stream', False))
            else:
                @functools.wraps(original)
                def wrapped(self, method, url, *args, **kwargs):
                    if not _recordable(url):
                        return original(self, method, url, *args, **kwargs)
                    event = prepare(self, method, url, kwargs)
                    if _configuration()['mode'] == 'offline_replay':
                        record_replay_failure('unsupported_curl_sdk_replay')
                        raise TapeError('curl SDK replay unsupported; retained evidence only')
                    try:
                        response = original(self, method, url, *args, **kwargs)
                    except Exception as exc:
                        finish_event(event, failure=_failure(exc))
                        raise
                    return observe(response, event, kwargs.get('stream', False))
            wrapped._acceptance_capture = True
            return wrapped
        cls.request = make_wrapper(original, asynchronous)


def install_source_io(*, mode, tape_dir, run_identity, phase):
    global _INSTALLED
    configure_tape(mode=mode, tape_dir=tape_dir, run_identity=run_identity, phase=phase)
    if _INSTALLED:
        return
    from .transport_safety import install_transport_safety
    # Offline replay wrappers intercept captured boundaries before guarded native
    # sends. OS network denial remains mandatory for all other paths.
    install_transport_safety()
    _install_requests()
    _install_urllib()
    for name in ('httpx', 'httpx2'):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        _install_httpx(module)
    _install_curl()
    _INSTALLED = True
