"""EventEdge-owned SIWC authorization. No inference or API-key fallback.

OAuth/PKCE and OIDC verification use Authlib and joserfc. Credential files are
private per Unix user. Call credential resolution only inside a disposable
transport worker: socket timeouts alone cannot bound DNS or filesystem stalls.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
from functools import wraps
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time
from urllib.parse import parse_qs, urlsplit
import uuid
import webbrowser

from authlib.integrations.requests_client import OAuth2Session
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.jwk import KeySet

ISSUER = 'https://auth.openai.com'
AUTHORIZE = ISSUER + '/api/accounts/authorize'
TOKEN = ISSUER + '/api/accounts/oauth/token'
DISCOVERY = ISSUER + '/.well-known/openid-configuration'
JWKS = ISSUER + '/.well-known/jwks.json'
RESOURCE = 'https://api.openai.com/v1'
SCOPE = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
_REQUIRED_SCOPES = frozenset(SCOPE.split())
_MAX_BYTES = 1024 * 1024
_CLIENT = re.compile(r'[A-Za-z0-9_-]{1,200}\Z')


class PlanAuthError(RuntimeError):
    """A fixed local error code; never a provider body or exception message."""


class PlanAuthDeadlineExceeded(PlanAuthError, TimeoutError):
    """The existing disposable-worker timeout contract also covers auth."""


@dataclass(frozen=True, repr=False)
class PlanCredentials:
    access_token: str = field(repr=False)
    client_id: str = field(repr=False)
    subject: str = field(repr=False)
    profile_identity: str
    expires_at: float

    def __repr__(self):
        return 'PlanCredentials(<redacted>)'


def _safe(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except PlanAuthError:
            raise
        except Exception:
            raise PlanAuthError('auth_failed') from None
    return wrapped


def _remaining(deadline):
    if type(deadline) not in (float, int) or not math.isfinite(deadline):
        raise PlanAuthError('invalid_deadline')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PlanAuthDeadlineExceeded('auth_deadline_exhausted')
    return remaining


def _private(info, mode):
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode or info.st_nlink != 1:
        raise PlanAuthError('storage_unsafe')


def _path(auth_path):
    path = Path(auth_path) if auth_path is not None else Path.home()/'.config'/'eventedge'/'plan-auth.json'
    path = path.absolute()
    if '..' in path.parts or path.is_symlink():
        raise PlanAuthError('storage_unsafe')
    for parent in path.parents:
        if parent.is_symlink() or (parent/'.git').exists():
            raise PlanAuthError('storage_unsafe')
    return path


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlanAuthError('storage_invalid')
        result[key] = value
    return result


class _SnapshotReplaced(Exception):
    """The old file disappeared after an atomic writer replaced it."""


class _Store:
    def __init__(self, fd, name, deadline):
        self.fd, self.name, self.deadline = fd, name, deadline

    def read(self, *, snapshot=False):
        _remaining(self.deadline)
        try:
            fd = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if snapshot and info.st_nlink == 0:
                raise _SnapshotReplaced
            _private(info, 0o600)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
                raise PlanAuthError('storage_unsafe')
            raw = stream.read(_MAX_BYTES+1)
        if len(raw) > _MAX_BYTES:
            raise PlanAuthError('storage_invalid')
        result = json.loads(raw, object_pairs_hook=_unique)
        if not isinstance(result, dict) or result.get('version') != 1 or result.get('issuer') != ISSUER:
            raise PlanAuthError('storage_invalid')
        try:
            if not result['host_id'].startswith('urn:uuid:'):
                raise ValueError
            uuid.UUID(result['host_id'][9:])
            if result.get('client_id') is not None and not _issued(result['client_id']):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise PlanAuthError('storage_invalid') from None
        _remaining(self.deadline)
        return result

    def write(self, value):
        _remaining(self.deadline)
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        if len(raw) > _MAX_BYTES:
            raise PlanAuthError('storage_invalid')
        name = '.plan-auth-' + secrets.token_hex(16)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(name, dir_fd=self.fd)
            except FileNotFoundError:
                pass
        _remaining(self.deadline)


@contextmanager
def _store(auth_path, deadline):
    _remaining(deadline)
    path = _path(auth_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = path.parent.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
        raise PlanAuthError('storage_unsafe')
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = None
    try:
        lock = os.open('.plan-auth.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        info = os.fstat(lock)
        _private(info, 0o600)
        if not stat.S_ISREG(info.st_mode):
            raise PlanAuthError('storage_unsafe')
        while True:
            _remaining(deadline)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(.025, _remaining(deadline)))
        yield _Store(fd, path.name, deadline)
    finally:
        if lock is not None:
            os.close(lock)
        os.close(fd)


def _snapshot_record(auth_path, deadline):
    """An atomic old/new file snapshot never waits for a refresh lock."""
    _remaining(deadline)
    path = _path(auth_path)
    try:
        info = path.parent.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PlanAuthError('storage_unsafe')
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        while True:
            _remaining(deadline)
            try:
                return _Store(fd, path.name, deadline).read(snapshot=True)
            except _SnapshotReplaced:
                continue
    finally:
        os.close(fd)


class _Session(OAuth2Session):
    """No ambient credentials/proxy, redirects, retries or unbounded bodies."""
    def __init__(self, deadline, **kwargs):
        self.deadline = deadline
        super().__init__(token_endpoint_auth_method='none', **kwargs)
        self.trust_env = False

    def send(self, request, **kwargs):
        _remaining(self.deadline)
        if request.url not in {DISCOVERY, JWKS, TOKEN}:
            raise PlanAuthError('endpoint_invalid')
        kwargs.update(timeout=min(10.0, _remaining(self.deadline)), stream=True, allow_redirects=False)
        response = super().send(request, **kwargs)
        try:
            if response.url != request.url or response.status_code != 200:
                raise PlanAuthError('authorization_rejected')
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                _remaining(self.deadline)
                size += len(chunk)
                if size > _MAX_BYTES:
                    raise PlanAuthError('response_too_large')
                chunks.append(chunk)
            response._content = b''.join(chunks)
            response._content_consumed = True
            _remaining(self.deadline)
            return response
        finally:
            response.close()


def _metadata(session):
    metadata = session.get(DISCOVERY, withhold_token=True).json()
    expected = {'issuer': ISSUER, 'authorization_endpoint': AUTHORIZE,
                'token_endpoint': TOKEN, 'jwks_uri': JWKS}
    if not isinstance(metadata, dict) or any(metadata.get(key) != value for key, value in expected.items()):
        raise PlanAuthError('endpoint_invalid')
    algorithms = metadata.get('id_token_signing_alg_values_supported', [])
    allowed = [value for value in algorithms if value in ('RS256', 'ES256')]
    if not allowed:
        raise PlanAuthError('identity_invalid')
    return allowed


def _issued(value):
    return type(value) is str and _CLIENT.fullmatch(value) is not None and value != 'dynamic_agent_client'


def _token(token, previous_scope=None, *, retained_id_token=None):
    if not isinstance(token, dict):
        raise PlanAuthError('token_invalid')
    if 'id_token' not in token and retained_id_token is not None:
        token = dict(token, id_token=retained_id_token)
    for key in ('access_token', 'refresh_token', 'id_token'):
        if type(token.get(key)) is not str or not token[key] or len(token[key]) > 65536:
            raise PlanAuthError('token_invalid')
    if type(token.get('token_type')) is not str or token['token_type'].lower() != 'bearer':
        raise PlanAuthError('token_invalid')
    seconds = token.get('expires_in')
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 7*86400:
        raise PlanAuthError('token_invalid')
    scope = token.get('scope', previous_scope)
    if type(scope) is not str or not _REQUIRED_SCOPES.issubset(scope.split()):
        raise PlanAuthError('plan_scope_missing')
    return {key: token[key] for key in ('access_token', 'refresh_token', 'id_token', 'token_type', 'expires_in')} | {'scope': scope}


def _identity(session, token, client_id, nonce, *, received_at=None):
    algorithms = _metadata(session)
    keys = KeySet.import_key_set(session.get(JWKS, withhold_token=True).json())
    try:
        decoded = jwt.decode(token['id_token'], keys, algorithms=algorithms)
        claims = CodeIDToken(decoded.claims, decoded.header,
                            {'iss': {'essential': True, 'value': ISSUER},
                             'aud': {'essential': True, 'value': client_id}},
                            {'client_id': client_id, 'nonce': nonce, 'access_token': token['access_token']})
        for key in ('exp', 'iat'):
            if type(claims.get(key)) not in (int, float) or not math.isfinite(claims[key]):
                raise ValueError
        claims.validate(now=received_at, leeway=5)
        if type(claims.get('sub')) is not str or not claims['sub'] or len(claims['sub']) > 1024:
            raise ValueError
        return claims['sub']
    except Exception:
        raise PlanAuthError('identity_invalid') from None


def _activate(store, record, token, subject, received_at, *, login=False):
    if record.get('subject') is not None and record['subject'] != subject:
        raise PlanAuthError('account_mismatch')
    result = dict(record)
    result.update(token, subject=subject, expires_at=received_at+token['expires_in'])
    result.pop('pending_refresh', None)
    if login:
        result['profile_identity'] = str(uuid.uuid4())
    store.write(result)
    return result


def _credentials(record):
    try:
        identity = str(uuid.UUID(record['profile_identity']))
        if identity != record['profile_identity'] or not _issued(record['client_id']):
            raise ValueError
        if type(record['subject']) is not str or not record['subject']:
            raise ValueError
        _token(record)
        expiry = record['expires_at']
        if type(expiry) not in (int, float) or not math.isfinite(expiry):
            raise ValueError
        return PlanCredentials(record['access_token'], record['client_id'], record['subject'], identity, expiry)
    except (ValueError, KeyError, TypeError):
        raise PlanAuthError('sign_in_required') from None


@_safe
def get_plan_credentials(*, auth_path: Path | None = None, deadline: float) -> PlanCredentials:
    """Resolve this user's EventEdge grant; refresh only under its file lock."""
    with _store(auth_path, deadline) as store:
        record = store.read()
        if not record or not record.get('subject'):
            raise PlanAuthError('sign_in_required')
        credentials = _credentials(record)
        pending = record.get('pending_refresh')
        if pending is None and credentials.expires_at > time.time()+60:
            _remaining(deadline)
            return credentials
        with _Session(deadline, client_id=record['client_id']) as session:
            if pending is None:
                token = session.refresh_token(TOKEN, refresh_token=record['refresh_token'], resource=RESOURCE)
                received_at = time.time()
                verify_identity = 'id_token' in token
                token = _token(token, record['scope'], retained_id_token=record['id_token'])
                # Persist rotating credentials before JWKS I/O, without activating them.
                pending = {'token': token, 'received_at': received_at, 'verify_identity': verify_identity}
                record = dict(record, pending_refresh=pending)
                store.write(record)
            else:
                if not isinstance(pending, dict) or set(pending) != {'token', 'received_at', 'verify_identity'}:
                    raise PlanAuthError('storage_invalid')
                if type(pending['verify_identity']) is not bool:
                    raise PlanAuthError('storage_invalid')
                token = _token(pending['token'])
                received_at = pending['received_at']
                if type(received_at) not in (int, float) or not math.isfinite(received_at) or received_at > time.time()+5:
                    raise PlanAuthError('storage_invalid')
            # OAuth refresh is bound to this verified account's issued client.
            # A new ID token is optional; validate it when the issuer supplies one.
            subject = (_identity(session, token, record['client_id'], None, received_at=received_at)
                       if pending['verify_identity'] else record['subject'])
            record = _activate(store, record, token, subject, received_at)
            credentials = _credentials(record)
            if credentials.expires_at <= time.time():
                raise PlanAuthError('refresh_required')
            _remaining(deadline)
            return credentials


@_safe
def status(*, auth_path: Path | None = None, deadline: float | None = None) -> dict:
    """Local file state only. This does not establish live account entitlement."""
    deadline = time.monotonic()+2 if deadline is None else deadline
    record = _snapshot_record(auth_path, deadline)
    if not record or not record.get('subject'):
        return {'status': 'sign_in_required', 'plan_usage': False}
    credentials = _credentials(record)
    pending = record.get('pending_refresh') is not None
    state = 'verification_pending' if pending else 'connected' if credentials.expires_at > time.time() else 'expired'
    _remaining(deadline)
    return {'status': state, 'plan_usage': state == 'connected', 'profile_identity': credentials.profile_identity}


@_safe
def read_profile_identity(auth_path: Path | None = None, *, deadline: float | None = None) -> str:
    """Read only the opaque memo namespace. Never refresh or use the network."""
    result = status(auth_path=auth_path, deadline=deadline)
    if 'profile_identity' not in result:
        raise PlanAuthError('sign_in_required')
    return result['profile_identity']


@_safe
def login(*, auth_path: Path | None = None, deadline: float,
          open_browser=None, port: int = 0) -> dict:
    """Explicit CLI sign-in only; never invoked implicitly by inference."""
    _remaining(deadline)
    if type(port) is not int or not 0 <= port <= 65535:
        raise PlanAuthError('invalid_port')
    with _store(auth_path, deadline) as store:
        record = store.read()
        if record is None:
            record = {'version': 1, 'issuer': ISSUER, 'host_id': 'urn:uuid:'+str(uuid.uuid4())}
            store.write(record)
        original_client = record.get('client_id')
        nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        callback = {}
        state = secrets.token_urlsafe(32)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                try:
                    _remaining(deadline)
                    parsed = urlsplit(self.path)
                    host = f'127.0.0.1:{self.server.server_port}'
                    if len(self.path) > 8192 or self.headers.get_all('Host') != [host] or parsed.path != '/auth/callback' or parsed.scheme or parsed.netloc or parsed.fragment:
                        raise PlanAuthError('callback_invalid')
                    params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
                    if any(len(value) != 1 for value in params.values()) or not secrets.compare_digest(params.get('state', [''])[0], state):
                        raise PlanAuthError('callback_invalid')
                    if 'error' in params:
                        raise PlanAuthError('authorization_rejected')
                    code = params.get('code', [''])[0]
                    issued = params.get('client_id', [original_client])[0]
                    if not code or not _issued(issued) or (original_client and issued != original_client):
                        raise PlanAuthError('callback_invalid')
                    callback.update(code=code, client_id=issued)
                except Exception as error:
                    callback['error'] = error if isinstance(error, PlanAuthError) else PlanAuthError('callback_invalid')
                self.send_response(200 if 'error' not in callback else 400)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(b'Return to EventEdge. You can close this window.')

        class Server(HTTPServer):
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(min(2.0, _remaining(deadline)))
                return connection, address

            def handle_error(self, *_args):
                callback['error'] = PlanAuthError('callback_invalid')

        with Server(('127.0.0.1', port), Handler) as server:
            redirect = f'http://127.0.0.1:{server.server_port}/auth/callback'
            with _Session(deadline, client_id=original_client or 'dynamic_agent_client',
                          scope=SCOPE, state=state, redirect_uri=redirect, code_challenge_method='S256') as session:
                arguments = dict(nonce=nonce, code_verifier=verifier, resource=RESOURCE,
                                 ext_agent_host_id=record['host_id'])
                if original_client is None:
                    arguments['agent_name_hint'] = 'EventEdge'
                url, _state = session.create_authorization_url(AUTHORIZE, state=state, **arguments)
                if not (open_browser or webbrowser.open)(url):
                    raise PlanAuthError('browser_unavailable')
                while not callback:
                    server.timeout = min(.2, _remaining(deadline))
                    server.handle_request()
                if 'error' in callback:
                    raise callback['error']
                record = dict(record, client_id=callback['client_id'])
                store.write(record)  # Issued registration survives a failed code exchange.
                session.client_id = record['client_id']
                token = session.fetch_token(TOKEN, grant_type='authorization_code', code=callback['code'],
                                            code_verifier=verifier, redirect_uri=redirect, resource=RESOURCE)
                received_at = time.time()
                token = _token(token)
                subject = _identity(session, token, record['client_id'], nonce)
                _activate(store, record, token, subject, received_at, login=True)
                return {'status': 'connected', 'plan_usage': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description='EventEdge Sign in with ChatGPT (no inference)')
    parser.add_argument('command', choices=('login', 'status'))
    parser.add_argument('--auth-path', type=Path)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true', help='Show a temporary sign-in URL instead of opening a browser')
    args = parser.parse_args(argv)
    try:
        deadline = time.monotonic()+args.timeout
        def terminal_url(url):
            # No ID-token or email hint is included in this one-time URL.
            print(url, flush=True)
            return True
        result = (login(auth_path=args.auth_path, deadline=deadline, port=args.port,
                        open_browser=terminal_url if args.no_browser else None)
                  if args.command == 'login' else status(auth_path=args.auth_path, deadline=deadline))
        print(json.dumps(result, sort_keys=True))
        return 0
    except PlanAuthError as error:
        print(json.dumps({'error': str(error)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
