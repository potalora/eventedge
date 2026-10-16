"""Subscription selection must never fall back to API billing."""
import sys
from types import SimpleNamespace

import pytest


def test_plan_descriptor_contains_no_credentials_and_missing_profile_fails(monkeypatch):
    from tradingagents.strategies.chatgpt_plan_client import configured_plan_client
    fake = SimpleNamespace(read_profile_identity=lambda path, **kwargs: 'profile-one')
    monkeypatch.setitem(sys.modules, 'tradingagents.strategies.plan_auth', fake)
    config = {'autoresearch': {'llm_billing_mode': 'chatgpt_plan'}}
    client = configured_plan_client(config, 'gpt-6-astra')
    assert client.profile_identity == 'profile-one'
    assert not hasattr(client, 'api_key')
    assert 'profile-one' not in repr(client)
    with pytest.raises(ValueError):
        configured_plan_client(config, 'claude-sonnet-5')
    fake.read_profile_identity = lambda path, **kwargs: (_ for _ in ()).throw(RuntimeError('profile unavailable'))
    with pytest.raises(RuntimeError):
        configured_plan_client(config, 'gpt-6-astra')


def test_api_mode_is_explicit_and_plan_guard_rejects_paid_clients(monkeypatch):
    from tradingagents.strategies.chatgpt_plan_client import (
        ChatGPTPlanClient, configured_plan_client, require_configured_billing,
    )
    assert configured_plan_client({}, 'gpt-6-luna') is None
    with pytest.raises(ValueError):
        configured_plan_client({'autoresearch': {'llm_billing_mode': 'auto'}}, 'gpt-6-luna')
    with pytest.raises(ValueError):
        require_configured_billing(object(), {'autoresearch': {'llm_billing_mode': 'chatgpt_plan'}}, 'gpt-6-luna')
    with pytest.raises(ValueError):
        require_configured_billing(ChatGPTPlanClient(None, 'profile-one'), {}, 'gpt-6-luna')


def test_configured_account_path_cannot_be_replaced_by_cached_or_injected_client(monkeypatch):
    from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient, require_configured_billing
    reads = []
    monkeypatch.setitem(sys.modules, 'tradingagents.strategies.plan_auth', SimpleNamespace(
        read_profile_identity=lambda path, **kwargs: reads.append(path) or 'other-profile'))
    config = {'autoresearch': {'llm_billing_mode': 'chatgpt_plan',
                              'chatgpt_plan_auth_path': '/private/selected.json'}}
    with pytest.raises(ValueError, match='chatgpt_plan_profile_changed'):
        require_configured_billing(ChatGPTPlanClient('/private/other.json', 'other-profile'), config, 'gpt-6-luna')
    assert reads == []


def test_plan_descriptor_uses_disposable_worker_without_parent_sdk_or_token(monkeypatch):
    from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient
    from tradingagents.strategies import llm_utils
    payloads = []
    def send(payload, timeout):
        payloads.append(payload)
        return {'text': '{}', 'provenance': {'configured_model': 'gpt-6-astra', 'billing_mode': 'chatgpt_plan'}}
    monkeypatch.setattr(llm_utils, 'bounded_transport', send)
    provenance = {}
    result = llm_utils.call_analysis_model(ChatGPTPlanClient('/private/profile.json', 'profile-one'),
        model='gpt-6-astra', system='exact system', prompt='exact evidence',
        max_tokens=100, temperature=0, effort='high', provenance=provenance)
    assert result == '{}'
    assert payloads[0]['provider'] == 'chatgpt_plan'
    assert payloads[0]['client'] == {'auth_path': '/private/profile.json', 'profile_identity': 'profile-one'}
    assert payloads[0]['request']['prompt'] == 'exact evidence'
    assert provenance['billing_mode'] == 'chatgpt_plan'


def test_worker_uses_only_own_oauth_token_and_blocks_redirects_and_paid_environment(monkeypatch):
    from tradingagents.strategies.transport_worker import execute
    from tradingagents.strategies import chatgpt_plan_transport
    captured = {}
    class Client:
        def __init__(self, **kwargs): captured.update(kwargs)
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setenv('OPENAI_API_KEY', 'paid-key-must-not-be-used')
    monkeypatch.setenv('OPENAI_PROJECT_ID', 'paid-project-must-not-be-used')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://wrong.invalid')
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=Client,
        DefaultHttpxClient=lambda **kwargs: kwargs))
    monkeypatch.setitem(sys.modules, 'tradingagents.strategies.plan_auth', SimpleNamespace(
        get_plan_credentials=lambda **kwargs: SimpleNamespace(access_token='own-oauth-token',
            profile_identity='profile-one')))
    def stream(client, **kwargs):
        captured['request'] = kwargs
        kwargs['provenance'].update(returned_model=kwargs['model'])
        return '{}'
    monkeypatch.setattr(chatgpt_plan_transport, 'stream_plan_response', stream)
    result = execute(_payload())
    assert result['text'] == '{}'
    assert captured['api_key'] == 'own-oauth-token'
    assert captured['base_url'] == 'https://api.openai.com/v1'
    assert captured['max_retries'] == 0
    assert captured['http_client']['follow_redirects'] is False
    assert captured['http_client']['trust_env'] is False
    assert 'max_tokens' not in captured['request']
    assert 'temperature' not in captured['request']
    assert captured['request']['effort'] == 'high'
    assert result['provenance']['effective_output_limit'] is None


def _payload():
    return {'kind': 'model', 'provider': 'chatgpt_plan', 'timeout': 3,
        'client': {'auth_path': None, 'profile_identity': 'profile-one'},
        'request': {'model': 'gpt-6-astra', 'system': 'sys', 'prompt': 'prompt',
                    'effort': 'high', 'max_tokens': 100, 'temperature': 0}}


@pytest.mark.parametrize('failure', ['changed_profile', 'auth_error', 'subscription_sharing_usage_limit_exceeded'])
def test_plan_failures_never_construct_paid_fallback_or_accept_partial(monkeypatch, failure):
    from tradingagents.strategies.transport_worker import execute
    from tradingagents.strategies import chatgpt_plan_transport
    calls = []
    class Client:
        def __init__(self, **kwargs): calls.append(kwargs)
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def credentials(**kwargs):
        if failure == 'auth_error': raise RuntimeError('PRIVATE token error')
        return SimpleNamespace(access_token='own-oauth-token', profile_identity=(
            'new-profile' if failure == 'changed_profile' else 'profile-one'))
    def stream(*args, **kwargs):
        raise chatgpt_plan_transport.ChatGPTPlanTransportError(failure)
    monkeypatch.setitem(sys.modules, 'tradingagents.strategies.plan_auth', SimpleNamespace(get_plan_credentials=credentials))
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=Client, DefaultHttpxClient=lambda **kwargs: kwargs))
    monkeypatch.setattr(chatgpt_plan_transport, 'stream_plan_response', stream)
    result = execute(_payload())
    assert result['error'] == 'provider_error'
    assert result['status_code'] is None  # Plan limit failures never trigger API retry loops.
    assert 'text' not in result
    assert 'PRIVATE' not in str(result)
    assert len(calls) == (1 if failure.startswith('subscription_') else 0)


@pytest.mark.parametrize('redirect', [False, True])
def test_real_sdk_worker_omits_paid_tenant_headers_and_never_follows_redirect(monkeypatch, redirect):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import openai
    from tradingagents.strategies.transport_worker import execute
    observed = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            observed.append((dict(self.headers), json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            if redirect:
                body = b'{}'
                self.send_response(307)
                self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/leak')
            else:
                event = {'type': 'response.completed', 'response': {'id': 'offline', 'status': 'completed',
                    'model': 'gpt-6-astra', 'output': [{'id': 'msg', 'type': 'message', 'role': 'assistant',
                    'status': 'completed', 'content': [{'type': 'output_text', 'text': '{}', 'annotations': []}]}]}}
                body = ('data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = openai.OpenAI
    def local_client(**kwargs):
        assert kwargs['base_url'] == 'https://api.openai.com/v1'
        return original(**{**kwargs, 'base_url': f'http://127.0.0.1:{server.server_port}/v1'})
    monkeypatch.setattr(openai, 'OpenAI', local_client)
    monkeypatch.setitem(sys.modules, 'tradingagents.strategies.plan_auth', SimpleNamespace(
        get_plan_credentials=lambda **kwargs: SimpleNamespace(access_token='own-oauth-token', profile_identity='profile-one')))
    monkeypatch.setenv('OPENAI_API_KEY', 'paid-key')
    monkeypatch.setenv('OPENAI_ORG_ID', 'paid-org')
    monkeypatch.setenv('OPENAI_PROJECT_ID', 'paid-project')
    try:
        result = execute(_payload())
        assert len(observed) == 1
        headers = {key.lower(): value for key, value in observed[0][0].items()}
        assert headers['authorization'] == 'Bearer own-oauth-token'
        assert 'openai-organization' not in headers
        assert 'openai-project' not in headers
        assert observed[0][1]['stream'] is True
        assert observed[0][1]['store'] is False
        assert 'max_output_tokens' not in observed[0][1]
        if redirect:
            assert result['error'] == 'provider_error'
            assert 'text' not in result
        else:
            assert result['text'] == '{}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
