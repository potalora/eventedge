"""A narrowed audit scope cannot retry or reset its owner's request policy."""
from types import SimpleNamespace

import pytest
import requests

from tradingagents.strategies.data_sources import request_policy as policy
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


class Clock:
    def __init__(self):
        self.now = 10.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay


def test_narrowed_attempt_cap_is_one_physical_request_and_parent_restores():
    clock, calls, diagnostics = Clock(), [], []

    def fail():
        calls.append(clock.now)
        raise requests.Timeout()

    with policy.provider_budget('congress', 200, clock=clock, sleep=clock.sleep,
                                random_fn=lambda: 0, limits=(), max_attempts=3,
                                diagnostics=diagnostics):
        with policy.provider_subbudget('congress', maximum_seconds=30,
                                       absolute_deadline=150, max_attempts=1):
            assert policy.current_provider_deadline('congress') == 40
            with pytest.raises(SourceFetchError) as error:
                policy.provider_call('congress', 'audit', fail)
            assert error.value.attempts == 1
        assert policy.current_provider_deadline('congress') == 200
        with pytest.raises(SourceFetchError) as error:
            policy.provider_call('congress', 'legacy', fail)
        assert error.value.attempts == 3
    assert len(calls) == 4
    assert clock.waits == [.5, 1.0]
    assert [row['attempts'] for row in diagnostics] == [1, 3]


def test_requested_attempts_cannot_widen_parent_or_its_deadline():
    clock, calls = Clock(), []
    with policy.provider_budget('congress', 15, clock=clock, sleep=clock.sleep,
                                limits=(), max_attempts=1):
        with policy.provider_subbudget('congress', maximum_seconds=90,
                                       absolute_deadline=200, max_attempts=5):
            assert policy.current_provider_deadline('congress') == 15
            with pytest.raises(SourceFetchError) as error:
                policy.provider_call('congress', 'audit', lambda: calls.append(1) or (_ for _ in ()).throw(requests.Timeout()))
            assert error.value.attempts == 1
    assert calls == [1]
    assert clock.waits == []


def test_exception_restores_nested_policy_without_reserving_a_slot():
    clock = Clock()
    with policy.provider_budget('congress', 200, clock=clock, sleep=clock.sleep,
                                limits=((1, 10),), max_attempts=3):
        with pytest.raises(RuntimeError, match='test exception'):
            with policy.provider_subbudget('congress', maximum_seconds=90,
                                           absolute_deadline=150, max_attempts=1):
                with policy.provider_subbudget('congress', maximum_seconds=5,
                                               absolute_deadline=100, max_attempts=2):
                    assert policy.current_provider_deadline('congress') == 15
                    raise RuntimeError('test exception')
        assert policy.current_provider_deadline('congress') == 200
        assert policy.provider_call('congress', 'one', lambda: SimpleNamespace(status_code=200)).status_code == 200
    assert clock.waits == []


@pytest.mark.parametrize('value', [True, False, 0, 6, -1, 1.0, '1'])
def test_invalid_attempt_cap_is_rejected_before_scope_entry(value):
    entered = False
    with pytest.raises(ValueError):
        with policy.provider_subbudget('congress', maximum_seconds=5,
                                       absolute_deadline=100, max_attempts=value):
            entered = True
    assert entered is False


def test_default_subbudget_preserves_retry_count():
    clock, calls = Clock(), []
    with policy.provider_budget('congress', 200, clock=clock, sleep=clock.sleep,
                                random_fn=lambda: 0, limits=(), max_attempts=2):
        with policy.provider_subbudget('congress', maximum_seconds=30, absolute_deadline=100):
            with pytest.raises(SourceFetchError) as error:
                policy.provider_call('congress', 'legacy', lambda: calls.append(1) or (_ for _ in ()).throw(requests.Timeout()))
            assert error.value.attempts == 2
    assert calls == [1, 1]


def test_unparented_subbudget_honors_single_attempt(monkeypatch):
    clock, calls = Clock(), []
    monkeypatch.setattr(policy.time, 'monotonic', clock)
    with policy.provider_subbudget('congress', maximum_seconds=30,
                                   absolute_deadline=100, max_attempts=1):
        with pytest.raises(SourceFetchError) as error:
            policy.provider_call('congress', 'audit', lambda: calls.append(1) or (_ for _ in ()).throw(requests.Timeout()))
        assert error.value.attempts == 1
    assert calls == [1]
    assert clock.waits == []
