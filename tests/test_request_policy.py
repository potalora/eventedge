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


@pytest.mark.parametrize("aggregate,expected", [(300, 60), (20, 20)])
def test_sdk_operation_cap_preserves_absolute_parent_clock_and_diagnostics(aggregate, expected):
    p, clock, seen, diagnostics = policy(), Clock(), [], []
    with p.provider_budget("openbb", aggregate, clock=clock, sleep=clock.sleep,
                           random_fn=lambda: 0, limits=(), max_attempts=1, diagnostics=diagnostics):
        def sdk():
            seen.append(p.current_provider_deadline("openbb"))
            clock.now = expected
            raise requests.Timeout("PRIVATE")
        with pytest.raises(SourceFetchError):
            p.provider_call("openbb", "profile", sdk, maximum_seconds=60)
        assert p.current_provider_deadline("openbb") == aggregate
        if aggregate == expected:
            with pytest.raises(SourceFetchError):
                p.provider_call("openbb", "shorts", lambda: seen.append("late"), maximum_seconds=60)
    assert seen == [expected]
    assert diagnostics[0]["reason_code"] == "timeout"
    assert diagnostics[0]["attempts"] == 1
    assert "PRIVATE" not in str(diagnostics)


def test_sdk_operation_retries_do_not_restart_per_operation_clock():
    p, clock, seen = policy(), Clock(), []
    with p.provider_budget("openbb", 300, clock=clock, sleep=clock.sleep,
                           random_fn=lambda: 0, limits=(), max_attempts=3):
        def sdk():
            seen.append(p.current_provider_deadline("openbb"))
            clock.now += 40
            raise requests.Timeout()
        with pytest.raises(SourceFetchError):
            p.provider_call("openbb", "profile", sdk, maximum_seconds=60)
    assert seen == [60, 60]
    assert clock.waits == [.5]


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


@pytest.mark.parametrize('elapsed', [1.0, 2.0])
@pytest.mark.parametrize('kind', ['http', 'sdk'])
def test_expiry_after_rate_admission_prevents_dispatch(monkeypatch, elapsed, kind):
    p, clock, calls, events = policy(), Clock(), [], []
    original_slot = p._slot

    def admitted_then_delayed(budget):
        original_slot(budget)
        clock.now = elapsed

    monkeypatch.setattr(p, '_slot', admitted_then_delayed)
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: calls.append(kw) or response())
    with p.provider_budget('test', 1.0, clock=clock, sleep=clock.sleep,
                           limits=(), max_attempts=1, diagnostics=events):
        with pytest.raises(SourceFetchError) as caught:
            if kind == 'http':
                p.provider_request('test', 'GET', 'https://offline.test')
            else:
                p.provider_call('test', 'operation', lambda: calls.append(True))
        assert p.current_provider_deadline('test') == 1.0
    assert calls == []
    assert caught.value.reason_code == 'timeout' and caught.value.attempts == 0
    assert events[-1]['reason_code'] == 'timeout' and events[-1]['attempts'] == 0


def test_positive_budget_after_rate_admission_keeps_remaining_timeout(monkeypatch):
    p, clock, calls = policy(), Clock(), []
    original_slot = p._slot

    def admitted_then_delayed(budget):
        original_slot(budget)
        clock.now = .25

    monkeypatch.setattr(p, '_slot', admitted_then_delayed)
    monkeypatch.setattr(requests, 'get', lambda *a, **kw: calls.append(kw) or response())
    with p.provider_budget('test', 1.0, clock=clock, sleep=clock.sleep, limits=(), max_attempts=1):
        assert p.provider_request('test', 'GET', 'https://offline.test').status_code == 200
        assert p.current_provider_deadline('test') == 1.0
    assert calls == [{'timeout': .75}]


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


def _maintenance_validator(reply):
    if getattr(reply, 'maintenance', False):
        raise SourceFetchError('Provider maintenance response',
                               reason_code='provider_maintenance',
                               http_status=reply.status_code)


def test_semantic_maintenance_recovers_inside_original_request_budget(monkeypatch):
    p, clock, calls, closed, events = policy(), Clock(), [], [], []
    maintenance = SimpleNamespace(status_code=200, headers={'Retry-After': '2'},
        maintenance=True, close=lambda: closed.append('maintenance'))
    success = SimpleNamespace(status_code=200, headers={},
        close=lambda: closed.append('success'))
    replies = iter([maintenance, success])

    def get(*args, **kwargs):
        calls.append(kwargs)
        assert p.current_provider_deadline('fred') == 5
        return next(replies)

    monkeypatch.setattr(requests, 'get', get)
    with p.provider_budget('fred', 5, clock=clock, sleep=clock.sleep,
                           random_fn=lambda: 0, limits=(), diagnostics=events):
        assert p.provider_request('fred', 'GET', 'https://private?key=SECRET',
            operation='UNRATE', response_validator=_maintenance_validator) is success
    assert calls == [{'timeout': 5}, {'timeout': 3}]
    assert clock.waits == [2]
    assert closed == ['maintenance']
    assert events == [dict(provider='fred', operation='UNRATE', reason_code=None,
                           http_status=200, attempts=2, recovered=True)]
    assert 'SECRET' not in str(events)


def test_persistent_semantic_maintenance_retains_real_status_and_attempts(monkeypatch):
    p, clock, closed, events = policy(), Clock(), [], []
    replies = [SimpleNamespace(status_code=200, headers={}, maintenance=True,
        close=lambda i=i: closed.append(i)) for i in range(3)]
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs: replies.pop(0))
    with p.provider_budget('fred', 10, clock=clock, sleep=clock.sleep,
                           random_fn=lambda: 0, limits=(), diagnostics=events):
        with pytest.raises(SourceFetchError) as caught:
            p.provider_request('fred', 'GET', 'https://offline', operation='UNRATE',
                               response_validator=_maintenance_validator)
    assert (caught.value.reason_code, caught.value.http_status, caught.value.attempts) == (
        'provider_maintenance', 200, 3)
    assert clock.waits == [.5, 1]
    assert closed == [0, 1, 2]
    assert events == [dict(provider='fred', operation='UNRATE',
        reason_code='provider_maintenance', http_status=200, attempts=3, recovered=False)]


def test_maintenance_retry_and_later_operation_share_exhausted_deadline(monkeypatch):
    p, clock, calls, closed, events = policy(), Clock(), [], [], []

    def get(*args, **kwargs):
        calls.append(kwargs)
        clock.now += .8
        return SimpleNamespace(status_code=200, headers={'Retry-After': '1'},
            maintenance=True, close=lambda: closed.append(True))

    monkeypatch.setattr(requests, 'get', get)
    with p.provider_budget('fred', 1, clock=clock, sleep=clock.sleep,
                           random_fn=lambda: 0, limits=(), diagnostics=events):
        with pytest.raises(SourceFetchError) as first:
            p.provider_request('fred', 'GET', 'https://offline', operation='UNRATE',
                               response_validator=_maintenance_validator)
        clock.now = 1
        with pytest.raises(SourceFetchError) as later:
            p.provider_request('fred', 'GET', 'https://offline', operation='PAYEMS',
                               response_validator=_maintenance_validator)
    assert first.value.reason_code == 'provider_maintenance'
    assert first.value.http_status == 200 and first.value.attempts == 1
    assert later.value.reason_code == 'timeout' and later.value.attempts == 0
    assert len(calls) == 1 and closed == [True] and clock.waits == []
    assert [event['reason_code'] for event in events] == ['provider_maintenance', 'timeout']


def test_response_validation_cannot_accept_success_after_deadline(monkeypatch):
    p, clock, closed, events = policy(), Clock(), [], []
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs:
        SimpleNamespace(status_code=200, headers={}, close=lambda: closed.append(True)))

    def late_validator(reply):
        clock.now = 1

    with p.provider_budget('fred', 1, clock=clock, sleep=clock.sleep,
                           limits=(), diagnostics=events):
        with pytest.raises(SourceFetchError) as caught:
            p.provider_request('fred', 'GET', 'https://offline',
                               response_validator=late_validator)
    assert caught.value.reason_code == 'timeout' and caught.value.attempts == 1
    assert closed == [True]
    assert events == [dict(provider='fred', operation='get', reason_code='timeout',
                           http_status=None, attempts=1, recovered=False)]


def test_ordinary_invalid_response_validation_is_not_retried(monkeypatch):
    p, clock, closed, events = policy(), Clock(), [], []
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs:
        SimpleNamespace(status_code=200, headers={}, close=lambda: closed.append(True)))

    def invalid_validator(reply):
        raise SourceFetchError('Invalid provider contract', reason_code='invalid_response')

    with p.provider_budget('fred', 10, clock=clock, sleep=clock.sleep,
                           limits=(), diagnostics=events):
        with pytest.raises(SourceFetchError) as caught:
            p.provider_request('fred', 'GET', 'https://offline',
                               response_validator=invalid_validator)
    assert caught.value.reason_code == 'invalid_response' and caught.value.attempts == 1
    assert clock.waits == [] and closed == [True]
    assert events[0]['reason_code'] == 'invalid_response'


@pytest.mark.parametrize('status,returned', [(404, (404,)), (401, ())])
def test_response_validator_does_not_override_http_status_policy(monkeypatch, status, returned):
    p, clock, validated = policy(), Clock(), []
    reply = response(status)
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs: reply)
    with p.provider_budget('fred', 10, clock=clock, sleep=clock.sleep, limits=()):
        if returned:
            assert p.provider_request('fred', 'GET', 'https://offline',
                return_statuses=returned, response_validator=validated.append) is reply
        else:
            with pytest.raises(SourceFetchError) as caught:
                p.provider_request('fred', 'GET', 'https://offline',
                    response_validator=validated.append)
            assert caught.value.reason_code == 'http_error'
    assert validated == []


@pytest.mark.parametrize('validator', [False, 'invalid', 1])
def test_noncallable_response_validator_is_rejected_before_dispatch(monkeypatch, validator):
    dispatched = []
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs: dispatched.append(True))
    with pytest.raises(ValueError, match='response validator'):
        policy().provider_request('fred', 'GET', 'https://offline', response_validator=validator)
    assert dispatched == []
