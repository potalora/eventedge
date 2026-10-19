"""Narrowing bulk acquisition preserves the enclosing provider contract."""
import pytest
import requests

from tradingagents.strategies.data_sources import request_policy as policy
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


@pytest.mark.parametrize('parent, expected', [(300, 180), (20, 20)])
def test_bulk_subbudget_inherits_clock_diagnostics_and_attempt_limit(parent, expected):
    now, seen, diagnostics = [0.0], [], []
    with policy.provider_budget('noaa', parent, clock=lambda: now[0],
            sleep=lambda delay: None, limits=(), max_attempts=1, diagnostics=diagnostics):
        with policy.provider_subbudget('noaa', maximum_seconds=180, absolute_deadline=1000):
            def operation():
                seen.append(policy.current_provider_deadline('noaa'))
                now[0] = expected
                raise requests.Timeout('do not expose transport details')
            with pytest.raises(SourceFetchError):
                policy.provider_call('noaa', 'yearly_observations', operation)
        assert policy.current_provider_deadline('noaa') == parent
    assert seen == [expected]
    assert diagnostics == [{'provider': 'noaa', 'operation': 'yearly_observations',
        'reason_code': 'timeout', 'http_status': None, 'attempts': 1, 'recovered': False}]
