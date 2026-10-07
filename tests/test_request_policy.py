from types import SimpleNamespace

import pytest
import requests

from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay


def response(status=200, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {})


def policy():
    from tradingagents.strategies.data_sources import request_policy
    return request_policy


def test_recovers_transient_using_get_seam_and_safe_diagnostics(monkeypatch):
    p = policy()
    clock = Clock()
    replies = iter([response(503), response()])
    seen = []
    monkeypatch.setattr(requests, 'get', lambda url, **kw: seen.append(kw) or next(replies))
    events = []
    with p.provider_budget('edgar', 10, clock=clock, sleep=clock.sleep, random_fn=lambda: 0, limits=(), diagnostics=events):
        assert p.provider_request('edgar', 'GET', 'https://secret.test?key=private').status_code == 200
    assert len(seen) == 2
    assert all(0 < item['timeout'] <= 10 for item in seen)
    assert events == [dict(provider='edgar', operation='get', reason_code=None, http_status=200, attempts=2, recovered=True)]
    assert 'private' not in str(events)


@pytest.mark.parametrize('status', [400,401,403,404])
def test_terminal_http_attempted_once(monkeypatch, status):
    p = policy()
    clock = Clock()
    calls = []
    monkeypatch.setattr(requests, 'post', lambda *a, **kw: calls.append(kw) or response(status))
    with p.provider_budget('usaspending', 10, clock=clock, sleep=clock.sleep, limits=()):
        with pytest.raises(SourceFetchError) as exc:
            p.provider_request('usaspending', 'POST', 'https://secret.test')
    assert exc.value.http_status == status
    assert exc.value.attempts == 1
    assert len(calls) == 1
    assert clock.waits == []


def test_retry_after_and_provider_quota_share_deadline(monkeypatch):
    p = policy()
    clock = Clock()
    replies = iter([response(429, {'Retry-After':'4'}), response()])
    monkeypatch.setattr(requests,'get',lambda *a,**kw: next(replies))
    with p.provider_budget('courtlistener',10,clock=clock,sleep=clock.sleep,random_fn=lambda:0,limits=((1,5),)):
        p.provider_request('courtlistener','GET','https://test')
    assert sum(clock.waits) == 5
    assert p.PROVIDER_LIMITS['courtlistener'] == ((5,60),(50,3600),(125,86400))


def test_exhausted_deadline_never_starts_late_retry(monkeypatch):
    p = policy()
    clock = Clock()
    calls=[]
    def timeout(*a,**kw):
        calls.append(kw)
        clock.now += 2
        raise requests.Timeout('https://private?token=secret')
    monkeypatch.setattr(requests,'get',timeout)
    with p.provider_budget('test',2,clock=clock,sleep=clock.sleep,limits=()):
        with pytest.raises(SourceFetchError) as exc:
            p.provider_request('test','GET','https://private?token=secret')
    assert len(calls)==1
    assert exc.value.reason_code=='timeout'
    assert 'secret' not in str(exc.value)


def test_sdk_retries_typed_transient_only_and_preserves_partial():
    p=policy()
    clock=Clock()
    count=[0]
    def sdk():
        count[0]+=1
        if count[0]<3: raise requests.ConnectionError('secret')
        return []
    with p.provider_budget('fred',20,clock=clock,sleep=clock.sleep,random_fn=lambda:0,limits=()):
        assert p.provider_call('fred','series',sdk)==[]
    assert count==[3]
    error=SourceFetchError('partial',reason_code='invalid_response',partial_data={'valid':[1]})
    with p.provider_budget('fred',20,clock=clock,sleep=clock.sleep,limits=()):
        with pytest.raises(SourceFetchError) as exc:
            p.provider_call('fred','series',lambda: (_ for _ in ()).throw(error))
    assert exc.value.partial_data=={'valid':[1]}


@pytest.mark.parametrize('kind', ['httpx_timeout', 'httpx_status', 'urllib3_timeout'])
def test_sdk_http_types_recover_without_message_matching(kind):
    import httpx
    from urllib3.exceptions import ReadTimeoutError
    p = policy()
    clock = Clock()
    if kind == 'httpx_timeout':
        error = httpx.ReadTimeout('secret')
    elif kind == 'httpx_status':
        response = httpx.Response(503, request=httpx.Request('GET', 'https://private?token=secret'))
        error = httpx.HTTPStatusError('secret', request=response.request, response=response)
    else:
        error = ReadTimeoutError(None, 'https://private?token=secret', 'secret')
    calls = [0]
    def call():
        calls[0] += 1
        if calls[0] == 1:
            raise error
        return []
    with p.provider_budget('sdk', 10, clock=clock, sleep=clock.sleep, limits=()):
        assert p.provider_call('sdk', 'operation', call) == []
    assert calls == [2]


def test_exhausted_error_responses_are_closed(monkeypatch):
    p = policy()
    clock = Clock()
    closed = []
    replies = [SimpleNamespace(status_code=503, headers={}, close=lambda i=i: closed.append(i)) for i in range(3)]
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: replies.pop(0))
    with p.provider_budget('alpaca', 10, clock=clock, sleep=clock.sleep, limits=()):
        with pytest.raises(SourceFetchError):
            p.provider_request('alpaca', 'GET', 'https://offline.test')
    assert closed == [0, 1, 2]
    assert p.PROVIDER_LIMITS['alpaca'] == ((200, 60),)


def test_success_response_remains_caller_owned(monkeypatch):
    p = policy()
    clock = Clock()
    closed = []
    success = SimpleNamespace(status_code=200, headers={}, close=lambda: closed.append('success'))
    replies = iter([response(503), success])  # SDK-shaped failures may have no close method.
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: next(replies))
    with p.provider_budget('alpaca', 10, clock=clock, sleep=clock.sleep, limits=()):
        assert p.provider_request('alpaca', 'GET', 'https://offline.test') is success
    assert not closed
