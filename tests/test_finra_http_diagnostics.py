"""Later public HTTP probes explain absent archives without certifying acquisition."""
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace

import pytest
import requests

from test_finra_bulk import native, raw
from tradingagents.strategies.data_sources import finra_bulk
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget

ACQUIRED = datetime.fromisoformat('2026-10-22T12:00:00+00:00')
URL = 'https://cdn.finra.org/equity/otcmarket/biweekly/shrt20260930.csv'
REAL_PROBE = finra_bulk.probe_missing_current_archive


@pytest.fixture
def probe(native, monkeypatch):
    monkeypatch.setattr(finra_bulk, 'probe_missing_current_archive', REAL_PROBE)
    state = SimpleNamespace(calls=[], closed=0, session_closed=0, status=403, error=None)
    class Session:
        trust_env = True
        def get(self, url, **kwargs):
            state.calls.append((url, kwargs, self.trust_env))
            if state.error:
                raise state.error
            class Response:
                status_code = state.status
                def close(self):
                    state.closed += 1
                @property
                def text(self):
                    pytest.fail('Diagnostic must not read response body')
            return Response()
        def close(self):
            state.session_closed += 1
    monkeypatch.setattr(requests, 'Session', Session)
    return state


def missing(native):
    attempt = finra_bulk.new_attempt()
    with pytest.raises(SourceFetchError) as caught:
        finra_bulk.acquire(['A'], attempt, acquired_at=ACQUIRED)
    assert caught.value.reason_code == 'provider_error'
    assert caught.value.http_status is None
    assert attempt['archive_coverage']['current_complete'] is False
    return attempt


def test_missing_current_retains_later_http403_without_attributing_sdk_response(native, probe):
    attempt = missing(native)
    diagnostic = attempt['current_archive_probe']
    assert diagnostic['kind'] == 'post_download_http_probe'
    assert diagnostic['settlement_date'] == '2026-09-30'
    assert diagnostic['url'] == URL
    assert diagnostic['http_status'] == 403 and diagnostic['outcome'] == 'http_response'
    assert datetime.fromisoformat(diagnostic['observed_at']).utcoffset() is not None
    assert probe.calls == [(URL, dict(headers={'Range': 'bytes=0-0', 'User-Agent': 'EventEdge-FINRA-diagnostic/1'},
                                     timeout=(1.5, 1.5), allow_redirects=False, stream=True), False)]
    assert probe.closed == probe.session_closed == 1


@pytest.mark.parametrize('status', [200, 206, 301, 302, 404, 429, 503])
def test_any_probe_http_status_keeps_missing_current_failure(native, probe, status):
    probe.status = status
    attempt = missing(native)
    assert attempt['current_archive_probe']['http_status'] == status
    assert attempt['cached_archive_dates'] == []
    assert native.source._cache == {}


@pytest.mark.parametrize('error,outcome', [
    (requests.Timeout('PRIVATE credentials'), 'timeout'),
    (requests.ConnectionError('PRIVATE credentials'), 'transport_error'),
    (ValueError('PRIVATE credentials'), 'probe_error'),
])
def test_failed_probe_is_sanitized_and_never_replaces_original_failure(native, probe, error, outcome):
    probe.error = error
    attempt = missing(native)
    assert attempt['current_archive_probe']['outcome'] == outcome
    assert attempt['current_archive_probe']['http_status'] is None
    assert 'PRIVATE' not in json.dumps(attempt)
    assert probe.session_closed == 1


def test_healthy_archive_never_probes(native, probe):
    native.add([raw('A')])
    attempt = finra_bulk.new_attempt()
    finra_bulk.acquire(['A'], attempt, acquired_at=ACQUIRED)
    assert attempt['current_archive_probe'] is None and probe.calls == []


def test_probe_is_capped_by_existing_deadline_and_does_not_retry(native, probe):
    probe.status = 503
    clock = [0.0]
    native.storage.get_data_from_date_and_store = lambda day: clock.__setitem__(0, 9.75)
    with provider_budget('openbb', 10, clock=lambda: clock[0], limits=()):
        attempt = missing(native)
    assert probe.calls[0][1]['timeout'] == (.125, .125)
    assert len(probe.calls) == 1 and attempt['current_archive_probe']['http_status'] == 503


def test_schema3_probe_is_durable_and_rejects_injected_content(native, probe):
    result = native.source.fetch_short_interest(['A'])
    metadata = result['acquisition']
    assert metadata['schema_version'] == 3
    assert result['errors']['A']['reason_code'] == 'provider_error'
    assert finra_bulk.validated_acquisition(metadata) == metadata
    for change in [lambda d: d.update(url='https://PRIVATE'), lambda d: d.update(http_status=True),
                   lambda d: d.update(headers='PRIVATE'), lambda d: d.update(settlement_date='2026-09-15'),
                   lambda d: d.update(outcome='PRIVATE'), lambda d: d.update(observed_at='2026-10-22'),
                   lambda d: d.update(observed_at='2026-10-21T12:00:00+00:00'),
                   lambda d: d.update(settlement_date='20260930')]:
        modified = deepcopy(metadata)
        change(modified['attempts'][0]['current_archive_probe'])
        with pytest.raises(ValueError):
            finra_bulk.validated_acquisition(modified)


def test_exhausted_probe_budget_starts_no_request(probe):
    with provider_budget('openbb', 1, clock=lambda: 2, limits=()):
        diagnostic = REAL_PROBE('2026-09-30')
    assert diagnostic['outcome'] == 'budget_exhausted'
    assert diagnostic['http_status'] is None and probe.calls == []


def test_invalid_probe_status_cannot_become_a_durable_http_response(native, probe):
    probe.status = True
    attempt = missing(native)
    assert attempt['current_archive_probe']['outcome'] == 'probe_error'
    assert attempt['current_archive_probe']['http_status'] is None


def test_probe_cannot_be_attached_to_a_successful_current_archive(native, probe):
    metadata = native.source.fetch_short_interest(['A'])['acquisition']
    native.add([raw('A')])
    healthy = native.source.fetch_short_interest(['A'])['acquisition']
    healthy['attempts'][0]['current_archive_probe'] = metadata['attempts'][0]['current_archive_probe']
    with pytest.raises(ValueError):
        finra_bulk.validated_acquisition(healthy)


def test_later_transient_probe_does_not_change_acquisition_retry_policy(native, probe):
    probe.status = 503
    result = native.source.fetch_short_interest(['A'])
    assert result['short_interest'] == {} and result['errors']['A']['reason_code'] == 'provider_error'
    assert len(result['acquisition']['attempts']) == native.prepare_calls == len(probe.calls) == 1


def test_successful_native_refresh_does_not_probe(native, probe):
    native.prepare_hook = lambda: native.add([raw('A')])
    result = native.source.fetch_short_interest(['A'])
    assert result['short_interest']['A']['date'] == '2026-09-30'
    assert result['acquisition']['attempts'][0]['current_archive_probe'] is None
    assert probe.calls == []
