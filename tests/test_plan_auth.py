"""Synthetic-only tests for EventEdge's own ChatGPT plan registration."""
import importlib
import time


def test_plan_auth_has_separate_registration_api():
    try:
        module = importlib.import_module('tradingagents.strategies.plan_auth')
    except ModuleNotFoundError:
        module = None
    assert module is not None, 'EventEdge plan-auth module is missing'
    assert callable(module.get_plan_credentials)
    assert callable(module.login)
    assert callable(module.status)

import hashlib
import base64
import http.client
import io
import json
import os
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import requests
from joserfc import jwt
from joserfc.jwk import RSAKey


@pytest.fixture
def auth():
    try:
        return importlib.import_module('tradingagents.strategies.plan_auth')
    except ModuleNotFoundError:
        pytest.fail('EventEdge plan-auth module is missing')


@pytest.fixture
def provider(monkeypatch):
    """Actual Authlib/joserfc; only the outbound HTTP transport is synthetic."""
    key = RSAKey.generate_key(2048, parameters={'kid': 'synthetic-1'})
    class Provider:
        nonce = None
        changes = {}
        token_changes = {}
        callback_changes = {}
        requests = []
        threads = []
        failures = set()
        refreshes = 0
        client_id = 'oaiapp_eventedge_test'
        def response(self, request):
            self.requests.append(request)
            url = request.url
            if url in self.failures:
                raise requests.ConnectionError('PRIVATE simulated provider message')
            if url == 'https://auth.openai.com/.well-known/openid-configuration':
                data = {'issuer': 'https://auth.openai.com',
                        'authorization_endpoint': 'https://auth.openai.com/api/accounts/authorize',
                        'token_endpoint': 'https://auth.openai.com/api/accounts/oauth/token',
                        'jwks_uri': 'https://auth.openai.com/.well-known/jwks.json',
                        'id_token_signing_alg_values_supported': ['RS256']}
            elif url == 'https://auth.openai.com/.well-known/jwks.json':
                data = {'keys': [key.as_dict(private=False)]}
            elif url == 'https://auth.openai.com/api/accounts/oauth/token':
                fields = parse_qs(request.body.decode() if isinstance(request.body, bytes) else request.body)
                assert fields['client_id'] == [self.client_id]
                assert fields['resource'] == ['https://api.openai.com/v1']
                assert 'client_secret' not in fields
                now = int(time.time())
                claims = {'iss': 'https://auth.openai.com', 'sub': 'synthetic-subject',
                          'aud': self.client_id, 'iat': now, 'exp': now + 3600}
                if fields['grant_type'] == ['authorization_code']:
                    assert fields['redirect_uri'] == [self.redirect_uri]
                    verifier = fields['code_verifier'][0]
                    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
                    assert challenge == self.challenge
                    claims['nonce'] = self.nonce
                else:
                    assert fields['grant_type'] == ['refresh_token']
                    assert 'scope' not in fields
                    assert fields['refresh_token'] == ['synthetic-refresh-1']
                    self.refreshes += 1
                claims.update(self.changes)
                data = {'access_token': 'synthetic-access-2' if self.refreshes else 'synthetic-access-1',
                        'refresh_token': 'synthetic-refresh-2' if self.refreshes else 'synthetic-refresh-1',
                        'token_type': 'Bearer', 'expires_in': 3600,
                        'scope': 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct',
                        'id_token': jwt.encode({'alg': 'RS256', 'kid': 'synthetic-1'}, claims, key)}
                data.update(self.token_changes)
            else:
                pytest.fail('Unapproved external HTTP request')
            response = requests.Response()
            response.status_code = 200
            response.url = request.url
            response.request = request
            response.headers['content-type'] = 'application/json'
            response._content = json.dumps(data).encode()
            response._content_consumed = True
            return response
        def browser(self, url):
            parsed = urlsplit(url)
            assert parsed.scheme == 'https' and parsed.netloc == 'auth.openai.com'
            params = parse_qs(parsed.query)
            assert params['code_challenge_method'] == ['S256']
            assert params['ext_agent_host_id'][0].startswith('urn:uuid:')
            assert 'id_token_hint' not in params
            self.auth_params = params
            self.nonce = params['nonce'][0]
            self.challenge = params['code_challenge'][0]
            self.redirect_uri = params['redirect_uri'][0]
            redirect = urlsplit(self.redirect_uri)
            query = {'state': params['state'][0], 'code': 'synthetic-code', 'client_id': self.client_id}
            query.update(self.callback_changes)
            def callback():
                connection = http.client.HTTPConnection(redirect.hostname, redirect.port, timeout=5)
                connection.request('GET', redirect.path + '?' + urlencode(query))
                connection.getresponse().read()
                connection.close()
            thread = threading.Thread(target=callback)
            self.threads.append(thread)
            thread.start()
            return True
    instance = Provider()
    monkeypatch.setattr(requests.Session, 'send', lambda session, request, **kw: instance.response(request))
    yield instance
    for thread in instance.threads:
        thread.join(5)
        assert not thread.is_alive()


def test_login_validates_real_signed_identity_and_private_registration(auth, provider, tmp_path, capsys):
    path = tmp_path / 'private' / 'auth.json'
    result = auth.login(auth_path=path, deadline=time.monotonic() + 10, open_browser=provider.browser)
    assert result['status'] == 'connected' and result['plan_usage'] is True
    assert provider.auth_params['client_id'] == ['dynamic_agent_client']
    assert provider.auth_params['agent_name_hint'] == ['EventEdge']
    saved = json.loads(path.read_text())
    assert saved['client_id'] == provider.client_id
    assert saved['subject'] == 'synthetic-subject'
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    credentials = auth.get_plan_credentials(auth_path=path, deadline=time.monotonic() + 5)
    assert credentials.access_token == 'synthetic-access-1'
    assert 'synthetic-access' not in repr(credentials)
    assert 'synthetic-access' not in json.dumps(result)
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('changes', [
    {'iss': 'https://example.invalid'}, {'aud': 'other-client'}, {'sub': ''},
    {'exp': 0}, {'iat': int(time.time()) + 3600}, {'nonce': 'other-nonce'},
])
def test_signed_but_wrong_identity_never_becomes_usable(auth, provider, tmp_path, changes):
    provider.changes = changes
    path = tmp_path / 'private' / 'auth.json'
    with pytest.raises(auth.PlanAuthError, match='identity_invalid'):
        auth.login(auth_path=path, deadline=time.monotonic() + 10, open_browser=provider.browser)
    assert json.loads(path.read_text())['client_id'] == provider.client_id
    with pytest.raises(auth.PlanAuthError):
        auth.get_plan_credentials(auth_path=path, deadline=time.monotonic() + 2)


@pytest.mark.parametrize('changes', [
    {'state': 'wrong-state'}, {'client_id': 'dynamic_agent_client'}, {'error': 'access_denied'},
])
def test_bad_callback_cannot_exchange_code(auth, provider, tmp_path, changes):
    provider.callback_changes = changes
    with pytest.raises(auth.PlanAuthError):
        auth.login(auth_path=tmp_path/'private'/'auth.json', deadline=time.monotonic()+5, open_browser=provider.browser)
    assert not any(request.method == 'POST' for request in provider.requests)


@pytest.mark.parametrize('changes', [
    {'scope': 'openid email'}, {'scope': None}, {'access_token': ''},
    {'token_type': 'MAC'}, {'expires_in': True}, {'expires_in': -1}, {'refresh_token': ''},
])
def test_incomplete_or_unconsented_token_cannot_enable_inference(auth, provider, tmp_path, changes):
    provider.token_changes = changes
    with pytest.raises(auth.PlanAuthError):
        auth.login(auth_path=tmp_path/'private'/'auth.json', deadline=time.monotonic()+10, open_browser=provider.browser)


def signed_in(auth, provider, tmp_path):
    path = tmp_path / 'private' / 'auth.json'
    auth.login(auth_path=path, deadline=time.monotonic()+10, open_browser=provider.browser)
    return path


def expire(path):
    saved = json.loads(path.read_text())
    saved['expires_at'] = time.time() - 1
    path.write_text(json.dumps(saved))


def test_serialized_refresh_replaces_credentials_and_account_together(auth, provider, tmp_path):
    path = signed_in(auth, provider, tmp_path)
    expire(path)
    results = []
    threads = [threading.Thread(target=lambda: results.append(auth.get_plan_credentials(auth_path=path, deadline=time.monotonic()+5))) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(6)
    assert len(results) == 2 and provider.refreshes == 1
    assert all(result.access_token == 'synthetic-access-2' for result in results)
    saved = json.loads(path.read_text())
    assert saved['refresh_token'] == 'synthetic-refresh-2'
    assert saved.get('pending_refresh') is None


def test_rotated_refresh_survives_key_service_failure_without_reusing_old_refresh(auth, provider, tmp_path):
    path = signed_in(auth, provider, tmp_path)
    expire(path)
    provider.failures = {'https://auth.openai.com/.well-known/jwks.json'}
    with pytest.raises(auth.PlanAuthError):
        auth.get_plan_credentials(auth_path=path, deadline=time.monotonic()+5)
    saved = json.loads(path.read_text())
    assert saved['pending_refresh']['token']['refresh_token'] == 'synthetic-refresh-2'
    provider.failures = set()
    result = auth.get_plan_credentials(auth_path=path, deadline=time.monotonic()+5)
    assert result.access_token == 'synthetic-access-2'
    assert provider.refreshes == 1


def test_refresh_identity_switch_rejected_and_original_registration_preserved(auth, provider, tmp_path):
    path = signed_in(auth, provider, tmp_path)
    expire(path)
    provider.changes = {'sub': 'different-subject'}
    with pytest.raises(auth.PlanAuthError, match='account_mismatch'):
        auth.get_plan_credentials(auth_path=path, deadline=time.monotonic()+5)
    assert json.loads(path.read_text())['subject'] == 'synthetic-subject'


def test_returning_login_reuses_host_and_issued_client(auth, provider, tmp_path):
    path = signed_in(auth, provider, tmp_path)
    host = json.loads(path.read_text())['host_id']
    auth.login(auth_path=path, deadline=time.monotonic()+10, open_browser=provider.browser)
    assert provider.auth_params['client_id'] == [provider.client_id]
    assert 'agent_name_hint' not in provider.auth_params
    assert provider.auth_params['ext_agent_host_id'] == [host]


@pytest.mark.parametrize('deadline', [float('nan'), float('inf'), True, 0.0])
def test_invalid_or_expired_deadline_stops_before_storage_or_network(auth, provider, tmp_path, deadline):
    path = tmp_path/'private'/'auth.json'
    with pytest.raises(auth.PlanAuthError, match='deadline'):
        auth.get_plan_credentials(auth_path=path, deadline=deadline)
    assert not path.parent.exists() and not provider.requests


def test_status_never_refreshes_or_returns_tokens(auth, provider, tmp_path):
    path = signed_in(auth, provider, tmp_path)
    expire(path)
    before = len(provider.requests)
    result = auth.status(auth_path=path, deadline=time.monotonic()+3)
    assert result['status'] == 'expired'
    assert 'synthetic-' not in json.dumps(result)
    assert len(provider.requests) == before


@pytest.mark.parametrize('kind', ['file_mode', 'dir_mode', 'symlink', 'hardlink', 'repo'])
def test_unsafe_auth_storage_is_rejected_without_token_disclosure(auth, provider, tmp_path, kind):
    path = signed_in(auth, provider, tmp_path)
    if kind == 'file_mode': path.chmod(0o644)
    if kind == 'dir_mode': path.parent.chmod(0o755)
    if kind == 'symlink':
        saved = path.with_name('original.json')
        path.rename(saved)
        path.symlink_to(saved)
    if kind == 'hardlink': os.link(path, path.with_name('copy.json'))
    if kind == 'repo': (path.parent/'.git').mkdir()
    with pytest.raises(auth.PlanAuthError, match='storage_unsafe'):
        auth.get_plan_credentials(auth_path=path, deadline=time.monotonic()+5)


def test_exchange_failure_keeps_issued_registration_without_disclosing_provider_error(auth, provider, tmp_path):
    provider.failures = {'https://auth.openai.com/api/accounts/oauth/token'}
    path = tmp_path/'private'/'auth.json'
    with pytest.raises(auth.PlanAuthError) as error:
        auth.login(auth_path=path, deadline=time.monotonic()+10, open_browser=provider.browser)
    assert 'PRIVATE' not in str(error.value)
    saved = json.loads(path.read_text())
    assert saved['client_id'] == provider.client_id and 'access_token' not in saved


def test_profile_namespace_changes_only_after_successful_login(auth, provider, tmp_path):
    path = signed_in(auth, provider, tmp_path)
    first = auth.read_profile_identity(path)
    expire(path)
    assert auth.get_plan_credentials(auth_path=path, deadline=time.monotonic()+5).profile_identity == first
    auth.login(auth_path=path, deadline=time.monotonic()+10, open_browser=provider.browser)
    assert auth.read_profile_identity(path) != first


def test_changed_client_on_returning_login_cannot_replace_registration(auth, provider, tmp_path):
    path = signed_in(auth, provider, tmp_path)
    before = path.read_bytes()
    provider.callback_changes = {'client_id': 'oaiapp_other_registration'}
    with pytest.raises(auth.PlanAuthError):
        auth.login(auth_path=path, deadline=time.monotonic()+10, open_browser=provider.browser)
    assert path.read_bytes() == before


def test_forged_signed_payload_is_rejected(auth, provider, tmp_path):
    path = signed_in(auth, provider, tmp_path)
    saved = json.loads(path.read_text())
    header, payload, signature = saved['id_token'].split('.')
    claims = json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
    claims['sub'] = 'forged-subject'
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b'=').decode()
    provider.token_changes = {'id_token': '.'.join((header, payload, signature))}
    with pytest.raises(auth.PlanAuthError, match='identity_invalid'):
        auth.login(auth_path=path, deadline=time.monotonic()+10, open_browser=provider.browser)
    assert json.loads(path.read_text())['subject'] == 'synthetic-subject'


def test_unsigned_id_token_is_rejected(auth, provider, tmp_path):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()
    provider.token_changes = {'id_token': encode({'alg':'none'})+'.'+encode({'sub':'forged'})+'.'}
    with pytest.raises(auth.PlanAuthError, match='identity_invalid'):
        auth.login(auth_path=tmp_path/'private'/'auth.json', deadline=time.monotonic()+10, open_browser=provider.browser)


def test_lock_wait_expires_without_network_or_storage_change(auth, provider, tmp_path):
    import fcntl
    path = signed_in(auth, provider, tmp_path)
    before = path.read_bytes()
    calls = len(provider.requests)
    with (path.parent/'.plan-auth.lock').open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(auth.PlanAuthError, match='deadline'):
            auth.get_plan_credentials(auth_path=path, deadline=time.monotonic()+.04)
    assert path.read_bytes() == before
    assert len(provider.requests) == calls


def test_cli_status_is_local_and_redacts_credentials(auth, provider, tmp_path, capsys):
    path = signed_in(auth, provider, tmp_path)
    before = len(provider.requests)
    assert auth.main(['status', '--auth-path', str(path)]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)['status'] == 'connected'
    assert 'synthetic' not in output and len(provider.requests) == before


def test_browser_failure_keeps_host_but_no_grant_and_closes_listener(auth, provider, tmp_path):
    path = tmp_path/'private'/'auth.json'
    urls = []
    with pytest.raises(auth.PlanAuthError, match='browser_unavailable'):
        auth.login(auth_path=path, deadline=time.monotonic()+5, open_browser=lambda url: urls.append(url) or False)
    assert 'access_token' not in json.loads(path.read_text())
    redirect = urlsplit(parse_qs(urlsplit(urls[0]).query)['redirect_uri'][0])
    import socket
    with socket.socket() as connection:
        assert connection.connect_ex((redirect.hostname, redirect.port)) != 0
    assert not provider.requests


@pytest.mark.parametrize('kind', ['wrong_host', 'wrong_path', 'duplicate_state'])
def test_callback_origin_shape_cannot_exchange_code(auth, provider, tmp_path, kind):
    threads = []
    def browser(url):
        params = parse_qs(urlsplit(url).query)
        redirect = urlsplit(params['redirect_uri'][0])
        query = urlencode({'state': params['state'][0], 'code': 'test-code', 'client_id': provider.client_id})
        if kind == 'duplicate_state': query += '&state='+params['state'][0]
        def callback():
            connection = http.client.HTTPConnection(redirect.hostname, redirect.port, timeout=5)
            path = '/wrong' if kind == 'wrong_path' else redirect.path
            headers = {'Host': 'example.invalid'} if kind == 'wrong_host' else {}
            connection.request('GET', path+'?'+query, headers=headers)
            connection.getresponse().read()
            connection.close()
        thread = threading.Thread(target=callback)
        threads.append(thread)
        thread.start()
        return True
    with pytest.raises(auth.PlanAuthError, match='callback_invalid'):
        auth.login(auth_path=tmp_path/'private'/'auth.json', deadline=time.monotonic()+5, open_browser=browser)
    for thread in threads: thread.join(5)
    assert not provider.requests


def test_missing_auth_does_not_fall_back_to_environment_api_key(auth, provider, tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'private-api-key-not-authorized')
    with pytest.raises(auth.PlanAuthError, match='sign_in_required'):
        auth.get_plan_credentials(auth_path=tmp_path/'private'/'auth.json', deadline=time.monotonic()+5)
    assert not provider.requests


def test_transport_rejects_redirect_and_large_body_before_jwt_or_credentials(auth, provider, tmp_path, monkeypatch):
    original = provider.response
    def response(request):
        result = original(request)
        if request.method == 'POST':
            result.status_code = 302
            result.headers['location'] = 'https://example.invalid/steal'
        return result
    monkeypatch.setattr(requests.Session, 'send', lambda self, request, **kw: response(request))
    with pytest.raises(auth.PlanAuthError, match='authorization_rejected'):
        auth.login(auth_path=tmp_path/'private'/'auth.json', deadline=time.monotonic()+10, open_browser=provider.browser)
    assert len(provider.requests) == 1


def test_transport_enforces_body_cap(auth, provider, tmp_path, monkeypatch):
    original = provider.response
    def response(request):
        result = original(request)
        if request.method == 'POST': result._content = b'x'*(1024*1024+1)
        return result
    monkeypatch.setattr(requests.Session, 'send', lambda self, request, **kw: response(request))
    with pytest.raises(auth.PlanAuthError, match='response_too_large'):
        auth.login(auth_path=tmp_path/'private'/'auth.json', deadline=time.monotonic()+10, open_browser=provider.browser)


def test_transport_completion_after_deadline_is_not_accepted(auth, provider, tmp_path, monkeypatch):
    original = provider.response
    deadline = time.monotonic()+5
    def response(request):
        result = original(request)
        monkeypatch.setattr(auth.time, 'monotonic', lambda: deadline+1)
        return result
    monkeypatch.setattr(requests.Session, 'send', lambda self, request, **kw: response(request))
    with pytest.raises(auth.PlanAuthError, match='deadline'):
        auth.login(auth_path=tmp_path/'private'/'auth.json', deadline=deadline, open_browser=provider.browser)
    assert 'access_token' not in json.loads((tmp_path/'private'/'auth.json').read_text())


def test_expired_auth_deadline_preserves_existing_transport_timeout_contract(auth, tmp_path):
    with pytest.raises(TimeoutError) as error:
        auth.get_plan_credentials(auth_path=tmp_path/'private'/'auth.json', deadline=time.monotonic()-1)
    assert isinstance(error.value, auth.PlanAuthError)
    assert str(error.value) == 'auth_deadline_exhausted'


def test_parent_identity_read_honors_callers_absolute_deadline(auth, provider, tmp_path):
    path = signed_in(auth, provider, tmp_path)
    with pytest.raises(TimeoutError):
        auth.read_profile_identity(path, deadline=time.monotonic()-1)


def test_refresh_without_new_id_token_keeps_verified_account_and_rotates_credentials(auth, provider, tmp_path, monkeypatch):
    path = signed_in(auth, provider, tmp_path)
    expire(path)
    saved = json.loads(path.read_text())
    prior_id_token = saved['id_token']
    profile = saved['profile_identity']
    original = provider.response
    def response(request):
        result = original(request)
        if request.method == 'POST':
            value = result.json()
            value.pop('id_token')
            result._content = json.dumps(value).encode()
        return result
    monkeypatch.setattr(requests.Session, 'send', lambda self, request, **kw: response(request))
    before = len(provider.requests)
    credentials = auth.get_plan_credentials(auth_path=path, deadline=time.monotonic()+5)
    saved = json.loads(path.read_text())
    assert credentials.access_token == 'synthetic-access-2'
    assert credentials.profile_identity == profile
    assert saved['subject'] == 'synthetic-subject' and saved['id_token'] == prior_id_token
    assert saved['refresh_token'] == 'synthetic-refresh-2'
    assert len(provider.requests) == before+1


def test_explicit_headless_login_url_has_no_retained_tokens(auth, provider, tmp_path, capsys, monkeypatch):
    path = signed_in(auth, provider, tmp_path)
    printed = []
    real_print = print
    def terminal_print(value, **kwargs):
        printed.append(str(value))
        if str(value).startswith('https://auth.openai.com/'):
            provider.browser(value)
        else:
            real_print(value, **kwargs)
    monkeypatch.setattr('builtins.print', terminal_print)
    assert auth.main(['login', '--no-browser', '--auth-path', str(path), '--timeout', '5']) == 0
    urls = [value for value in printed if value.startswith('https://')]
    assert len(urls) == 1
    assert 'id_token_hint' not in urls[0]
    assert 'synthetic-access' not in ''.join(printed) and 'synthetic-refresh' not in ''.join(printed)


def test_parent_profile_lookup_rejects_fifo_without_blocking(auth, tmp_path):
    import subprocess
    import sys
    directory = tmp_path/'private'
    directory.mkdir(mode=0o700)
    path = directory/'auth.json'
    os.mkfifo(path, 0o600)
    code = '''
import sys, time
from tradingagents.strategies.plan_auth import read_profile_identity, PlanAuthError
try:
    read_profile_identity(sys.argv[1], deadline=time.monotonic()+.1)
except PlanAuthError as error:
    print(str(error))
else:
    raise AssertionError('FIFO accepted')
'''
    result = subprocess.run([sys.executable, '-B', '-c', code, str(path)], capture_output=True, timeout=2, check=True)
    assert result.stdout == b'storage_unsafe\n'


def test_parent_profile_snapshot_does_not_wait_for_active_refresh_lock(auth, provider, tmp_path):
    import fcntl
    path = signed_in(auth, provider, tmp_path)
    expected = json.loads(path.read_text())['profile_identity']
    with (path.parent/'.plan-auth.lock').open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert auth.read_profile_identity(path, deadline=time.monotonic()+.04) == expected


def test_missing_profile_snapshot_does_not_create_auth_storage(auth, provider, tmp_path):
    path = tmp_path/'missing'/'auth.json'
    with pytest.raises(auth.PlanAuthError, match='sign_in_required'):
        auth.read_profile_identity(path)
    assert not path.parent.exists() and not provider.requests


def test_parent_snapshot_remains_available_during_another_process_refresh_lock(auth, provider, tmp_path):
    import subprocess
    import sys
    path = signed_in(auth, provider, tmp_path)
    identity = auth.read_profile_identity(path)
    code = '''
import fcntl, sys
with open(sys.argv[1], 'r+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    print('locked', flush=True)
    sys.stdin.read(1)
'''
    child = subprocess.Popen([sys.executable, '-B', '-c', code, str(path.parent/'.plan-auth.lock')],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert child.stdout.readline() == b'locked\n'
        assert auth.read_profile_identity(path, deadline=time.monotonic()+.1) == identity
    finally:
        child.communicate(input=b'x', timeout=2)
    assert child.returncode == 0


def test_concurrent_atomic_profile_snapshots_are_complete_old_or_new_records(auth, provider, tmp_path):
    import uuid
    path = signed_in(auth, provider, tmp_path)
    original = json.loads(path.read_text())
    identities = {original['profile_identity'], str(uuid.uuid4())}
    ready = threading.Event()
    finished = threading.Event()
    failures = []
    def writer():
        try:
            with auth._store(path, time.monotonic()+5) as store:
                ready.set()
                for index in range(40):
                    value = dict(original, profile_identity=sorted(identities)[index % 2])
                    store.write(value)
        except Exception as error:
            failures.append(type(error).__name__)
        finally:
            finished.set()
    thread = threading.Thread(target=writer)
    thread.start()
    assert ready.wait(2)
    observed = []
    while not finished.is_set():
        observed.append(auth.read_profile_identity(path, deadline=time.monotonic()+.2))
    thread.join(3)
    assert observed and not failures
    assert set(observed) <= identities
