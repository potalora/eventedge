"""Model failures preserve bounded diagnostics without retaining private errors."""
import json
import sys
from types import SimpleNamespace

import pytest

from tradingagents.strategies import transport_worker


def model_failure(monkeypatch, *, code, error_type, headers=None):
    class Synthetic429(Exception):
        status_code = 429
        def __init__(self):
            super().__init__('PRIVATE message account-secret')
            self.code = code
            self.type = error_type
            self.body = {'message': 'PRIVATE body account-secret'}
            self.response = SimpleNamespace(headers=headers or {})
            self.request_id = 'PRIVATE request-id'
    class Client:
        def __init__(self, **settings):
            assert settings == {'api_key': 'PRIVATE key', 'timeout': 7, 'max_retries': 0}
            self.responses = SimpleNamespace(create=self.create)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def create(self, **request):
            assert request == {'model': 'gpt-6-luna', 'instructions': 'sys', 'input': 'prompt',
                               'reasoning': {'effort': 'high'}, 'max_output_tokens': 16384, 'store': False}
            raise Synthetic429()
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=Client))
    return transport_worker.execute({'kind': 'model', 'provider': 'openai', 'client': {'api_key': 'PRIVATE key'},
        'timeout': 7, 'request': {'model': 'gpt-6-luna', 'system': 'sys', 'prompt': 'prompt',
                                'max_tokens': 10, 'temperature': 0, 'effort': 'high'}})


def test_throttle_and_exhausted_credit_remain_distinguishable_without_private_error(monkeypatch):
    throttle = model_failure(monkeypatch, code='rate_limit_exceeded', error_type='rate_limit_error',
                             headers={'Retry-After': '2.5', 'x-ratelimit-remaining-tokens': '0'})
    quota = model_failure(monkeypatch, code='credit_balance_exhausted', error_type='insufficient_quota')
    assert throttle == {'error': 'provider_error', 'status_code': 429, 'error_metadata': {
        'error_code': 'rate_limit_exceeded', 'error_type': 'rate_limit_error',
        'retry_after_seconds': 2.5, 'rate_limit_remaining_tokens': 0}}
    assert quota == {'error': 'provider_error', 'status_code': 429, 'error_metadata': {
        'error_code': 'credit_balance_exhausted', 'error_type': 'insufficient_quota'}}
    assert 'PRIVATE' not in json.dumps([throttle, quota])


@pytest.mark.parametrize('value', ['NaN', 'inf', '-1', '1e3', '1, 2', ' 2', '2 ', '604801',
                                  'Wed, 21 Oct 2026 07:28:00 GMT', True, 2, None])
def test_malformed_retry_header_is_never_retained(monkeypatch, value):
    result = model_failure(monkeypatch, code='PRIVATE secret', error_type='PRIVATE secret',
                           headers={'Retry-After': value, 'Authorization': 'PRIVATE secret'})
    assert result['error_metadata'] == {}


def test_numeric_rate_headers_are_bounded_and_durations_use_seconds(monkeypatch):
    result = model_failure(monkeypatch, code='slow_down', error_type='tokens', headers={
        'x-ratelimit-limit-requests': '100', 'x-ratelimit-remaining-requests': '0',
        'x-ratelimit-limit-tokens': '1000000000001',
        'x-ratelimit-remaining-tokens': '-1',
        'x-ratelimit-reset-requests': '6m0s', 'x-ratelimit-reset-tokens': '1s250ms',
        'x-ratelimit-limit-project-tokens': '1000',
        'x-ratelimit-remaining-project-tokens': '50',
        'x-ratelimit-reset-project-tokens': '2h', 'retry-after-ms': '250',
        'x-should-retry': 'true', 'x-request-id': 'PRIVATE id'})
    assert result['error_metadata'] == {
        'error_code': 'slow_down', 'error_type': 'tokens',
        'rate_limit_limit_requests': 100, 'rate_limit_remaining_requests': 0,
        'rate_limit_reset_requests_seconds': 360, 'rate_limit_reset_tokens_seconds': 1.25,
        'rate_limit_limit_project_tokens': 1000, 'rate_limit_remaining_project_tokens': 50,
        'rate_limit_reset_project_tokens_seconds': 7200, 'retry_after_ms_seconds': .25}


def test_duplicate_header_names_and_unexpected_objects_fail_closed(monkeypatch):
    result = model_failure(monkeypatch, code={'PRIVATE': 'secret'}, error_type=['tokens'],
        headers={'Retry-After': '2', 'retry-after': '2', 'x-ratelimit-reset-tokens': '1s, 2s'})
    assert result['error_metadata'] == {}
    class BrokenHeaders:
        def items(self): raise RuntimeError('PRIVATE secret')
    assert model_failure(monkeypatch, code='rate_limit_exceeded', error_type='tokens',
                         headers=BrokenHeaders())['error_metadata'] == {}


def test_parent_revalidates_metadata_without_changing_original_exception():
    from tradingagents.strategies.runtime_deadline import ModelTransportError
    error = ModelTransportError('provider_error', 429, error_metadata={
        'error_code': 'PRIVATE secret', 'error_type': 'tokens', 'retry_after_seconds': float('nan'),
        'rate_limit_reset_tokens_seconds': float('inf'), 'rate_limit_limit_tokens': True,
        'retry_after_ms_seconds': 10**1000,
        'rate_limit_remaining_requests': -1, 'raw_body': 'PRIVATE secret',
        'rate_limit_remaining_tokens': 0})
    assert error.error_metadata == {'error_type': 'tokens', 'rate_limit_remaining_tokens': 0}
    assert str(error) == 'Model transport failed (provider_error, status=429)'
    assert error.status_code == 429
    assert ModelTransportError('provider_error', 429, error_metadata=[]).error_metadata == {}


def test_real_sdk_error_metadata_crosses_disposable_transport_once():
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from openai import OpenAI
    from tradingagents.strategies.llm_utils import call_analysis_model
    from tradingagents.strategies.runtime_deadline import ModelTransportError, model_budget
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            body = json.dumps({'error': {'message': 'PRIVATE body account-secret',
                'type': 'insufficient_quota', 'code': 'project_spend_limit_exceeded', 'param': None}}).encode()
            self.send_response(429)
            for key, value in {'Content-Type': 'application/json', 'Content-Length': str(len(body)),
                               'Retry-After': '3', 'x-ratelimit-remaining-tokens': '0',
                               'x-request-id': 'PRIVATE id', 'Set-Cookie': 'PRIVATE cookie'}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Even an SDK configured to retry must be reconstructed with zero retries.
        with OpenAI(api_key='offline-private', base_url=f'http://127.0.0.1:{server.server_port}/v1', max_retries=4) as client:
            with model_budget(time.monotonic() + 5), pytest.raises(ModelTransportError) as caught:
                call_analysis_model(client, model='gpt-6-luna', system='sys', prompt='prompt',
                                    max_tokens=10, temperature=0, effort='high')
        assert caught.value.status_code == 429
        assert caught.value.error_metadata == {'error_code': 'project_spend_limit_exceeded',
            'error_type': 'insufficient_quota', 'retry_after_seconds': 3,
            'rate_limit_remaining_tokens': 0}
        assert 'PRIVATE' not in json.dumps(caught.value.error_metadata)
        assert len(requests) == 1
        assert requests[0]['max_output_tokens'] == 16384
        assert requests[0]['reasoning'] == {'effort': 'high'}
        assert requests[0]['store'] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
