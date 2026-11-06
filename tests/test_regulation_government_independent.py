"""Independent government-copy checks through the actual provider wrapper."""
import time

import pytest

from tradingagents.strategies.data_sources import request_policy
from tradingagents.strategies.data_sources.regulation_evidence import validate_regulation_source
from test_regulation_government_acquisition import (
    DOC, GOV, acquire, payload, setup_transport,
)
from tradingagents.strategies.data_sources import regulations_source as source
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from test_regulation_source_evidence import Response
from test_request_policy import Clock


@pytest.mark.parametrize('status,failed_body', [(403, b'Access denied'), (404, b''), (410, b'Gone')])
def test_real_provider_wrapper_preserves_http_original_and_allows_verified_copy(monkeypatch, status, failed_body):
    """Mock requests, so production status handling cannot be bypassed."""
    calls, replies = setup_transport(monkeypatch, status=status, failed_body=failed_body)
    fixture_transport = source.provider_request
    monkeypatch.setattr(source, 'provider_request', request_policy.provider_request)
    monkeypatch.setattr(request_policy.requests, 'get',
                        lambda url, **kwargs: fixture_transport('regulations', 'GET', url, **kwargs))
    with request_policy.provider_budget('regulations', time.monotonic() + 60, limits=()):
        result = payload(acquire())
    assert any(url == GOV for url, _ in calls)
    original = result['regulation_evidence']['corpus'][DOC]['units'][0]['primary_failure']
    assert original['http_status'] == status
    assert original['receipt']['bytes'] == len(failed_body)
    assert validate_regulation_source(result)['content_complete'] is True
    assert all(reply.closed for reply in replies)


def test_returned_status_is_opt_in_and_error_response_has_one_close_owner(monkeypatch):
    replies = [Response(b'Forbidden', status=403), Response(b'Forbidden', status=403)]
    pending = iter(replies)
    monkeypatch.setattr(request_policy.requests, 'get', lambda *a, **kw: next(pending))
    clock, events = Clock(), []
    with request_policy.provider_budget('regulations', 10, clock=clock, sleep=clock.sleep,
                                       limits=(), diagnostics=events):
        with pytest.raises(SourceFetchError) as error:
            request_policy.provider_request('regulations', 'GET', 'https://example.test')
        assert error.value.http_status == 403 and replies[0].closed
        returned = request_policy.provider_request('regulations', 'GET', 'https://example.test',
                                                   return_statuses=(403,))
        assert returned is replies[1] and not returned.closed
        returned.close()
    assert all(event['reason_code'] == 'http_error' for event in events)
    assert clock.waits == []


def test_late_returned_http_error_is_closed_and_never_retried(monkeypatch):
    clock, reply, calls = Clock(), Response(b'Forbidden', status=403), []
    def get(*args, **kwargs):
        calls.append(kwargs)
        clock.now = 10
        return reply
    monkeypatch.setattr(request_policy.requests, 'get', get)
    with request_policy.provider_budget('regulations', 10, clock=clock, sleep=clock.sleep, limits=()):
        with pytest.raises(SourceFetchError) as error:
            request_policy.provider_request('regulations', 'GET', 'https://example.test', return_statuses=(403,))
    assert error.value.reason_code == 'timeout'
    assert reply.closed and len(calls) == 1 and clock.waits == []


def test_returned_status_keeps_transient_retry_backoff_and_response_ownership(monkeypatch):
    clock, events = Clock(), []
    transient, terminal = Response(b'Unavailable', status=503), Response(b'Forbidden', status=403)
    replies = iter([transient, terminal])
    monkeypatch.setattr(request_policy.requests, 'get', lambda *a, **kw: next(replies))
    with request_policy.provider_budget('regulations', 10, clock=clock, sleep=clock.sleep,
                                       random_fn=lambda: 0, limits=(), diagnostics=events):
        returned = request_policy.provider_request('regulations', 'GET', 'https://example.test', return_statuses=(403,))
        assert returned is terminal and transient.closed and not terminal.closed
        assert request_policy.current_provider_deadline('regulations') == 10
        returned.close()
    assert clock.waits == [0.5]
    assert events[-1]['attempts'] == 2 and events[-1]['reason_code'] == 'http_error'


def test_preformatted_table_notation_keeps_later_cells_at_publisher_columns():
    from test_regulation_government_copy import DETAIL, MAC
    from tradingagents.strategies.data_sources.regulation_government_copy import extract_government_copy
    grid = b'Pollutant       Limit\nPM<INF>10</INF>            150\nNO<INF>X</INF>             100\n'
    proof = extract_government_copy(DETAIL, MAC.replace(b'SUMMARY:', grid + b'SUMMARY:', 1))
    lines = proof['text'].splitlines()
    start = lines.index('Pollutant       Limit')
    assert proof['representation'] == 'preserves_preformatted_layout'
    assert lines[start + 1].index('150') == lines[start].index('Limit')
    assert lines[start + 2].index('100') == lines[start].index('Limit')
