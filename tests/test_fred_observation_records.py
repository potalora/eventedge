"""Provider-shaped FRED pagination and dated observation lineage contracts."""
from datetime import datetime, timezone
import json

import pandas as pd
import pytest
import requests

from tradingagents.strategies.data_sources.fred_source import FREDSource
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


def row(period, value, first="2026-09-18", last="9999-12-31"):
    return {"date": period, "value": value, "realtime_start": first, "realtime_end": last}


def install_pages(monkeypatch, pages):
    calls = []
    def transport(url, **options):
        calls.append(dict(options["params"]))
        payload = pages[len(calls) - 1]
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        return response
    monkeypatch.setattr(FREDSource, "_transport_get", staticmethod(transport))
    return calls


def test_snapshot_pages_all_declared_observations(monkeypatch):
    calls = install_pages(monkeypatch, [
        {"count": 2, "offset": 0, "limit": 1, "observations": [row("2026-07-01", "4.5")]},
        {"count": 2, "offset": 1, "limit": 1, "observations": [row("2026-08-01", "4.4")]},
    ])
    result = FREDSource("offline").fetch_series("TXUR", "2026-07-01", "2026-10-11")
    assert result.to_list() == [4.5, 4.4]
    assert calls[1]["offset"] == 1
    assert all(c["realtime_start"] == c["realtime_end"] == "2026-10-11" for c in calls)


@pytest.mark.parametrize("second", [
    {"count": 2, "offset": 1, "limit": 1, "observations": []},
    {"count": 2, "offset": 0, "limit": 1, "observations": [row("2026-08-01", "4.4")]},
    {"count": 3, "offset": 1, "limit": 1, "observations": [row("2026-08-01", "4.4")]},
])
def test_incomplete_or_changing_pages_fail_instead_of_healthy_prefix(monkeypatch, second):
    calls = install_pages(monkeypatch, [
        {"count": 2, "offset": 0, "limit": 1, "observations": [row("2026-07-01", "4.5")]}, second,
    ])
    source = FREDSource("offline")
    with pytest.raises(SourceFetchError, match="invalid_response"):
        source.fetch_series("TXUR", "2026-07-01", "2026-10-11")
    assert len(calls) == 2
    assert not source._cache


def test_snapshot_retains_plain_records_and_forward_capture_clock(monkeypatch):
    install_pages(monkeypatch, [{"count": 1, "offset": 0, "limit": 100000,
        "observations": [row("2026-08-01", "4.4", "2026-10-11", "2026-10-11")]}])
    before = datetime.now(timezone.utc)
    result = FREDSource("offline").fetch_series("TXUR", "2026-07-01", "2026-10-11")
    after = datetime.now(timezone.utc)
    assert isinstance(result, pd.Series)
    assert result.attrs["observation_records"] == [{"observation_date": "2026-08-01", "value": 4.4,
        "realtime_start": "2026-10-11", "realtime_end": "2026-10-11"}]
    captured = datetime.fromisoformat(result.attrs["acquired_at"])
    assert before <= captured <= after
    assert result.attrs["available_at"] == result.attrs["acquired_at"]
    assert result.attrs["availability_basis"] == "forward_acquisition_time"


def test_history_preserves_initial_and_revised_values_as_distinct_dated_records(monkeypatch):
    calls = install_pages(monkeypatch, [{"count": 2, "offset": 0, "limit": 100000,
        "observations": [row("2026-07-01", "4.5", "2026-08-21", "2026-09-17"),
                         row("2026-07-01", "4.4", "2026-09-18", "2026-10-11")]}])
    result = FREDSource("offline").fetch_observation_history("TXUR", "2026-07-01", "2026-08-01", as_of="2026-10-11")
    assert calls[0]["realtime_start"] == "1776-07-04"
    assert calls[0]["realtime_end"] == "2026-10-11"
    assert result["vintage_date"] == "2026-10-11"
    assert result["series_id"] == "TXUR"
    assert [r["value"] for r in result["observations"]] == [4.5, 4.4]
    assert [r["realtime_start"] for r in result["observations"]] == ["2026-08-21", "2026-09-18"]
    assert result["available_at"] == result["acquired_at"]
    assert result["coverage"] == {"complete": True, "observation_count": 2}
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("broken", [
    row("2026-07-01", "4.5", "not-a-date"),
    row("2026-07-01", "4.5", "2026-09-18", "2026-08-20"),
    row("2026-07-01", "Infinity"),
    {"date": "2026-07-01", "value": "4.5"},
])
def test_history_requires_valid_original_realtime_bounds_and_finite_values(monkeypatch, broken):
    install_pages(monkeypatch, [{"count": 1, "offset": 0, "limit": 100000, "observations": [broken]}])
    with pytest.raises(SourceFetchError, match="invalid_response"):
        FREDSource("offline").fetch_observation_history("TXUR", "2026-07-01", "2026-08-01", as_of="2026-10-11")


def test_history_missing_marker_is_plain_none_and_query_dates_are_exact(monkeypatch):
    calls = install_pages(monkeypatch, [{"count": 1, "offset": 0, "limit": 100000,
        "observations": [row("2026-07-01", ".", "2026-08-21", "2026-10-11")]}])
    source = FREDSource("offline")
    result = source.fetch_observation_history("TXUR", "2026-07-01", "2026-08-01", as_of="2026-10-11")
    assert result["observations"][0]["value"] is None
    with pytest.raises(SourceFetchError, match="invalid_response"):
        source.fetch_observation_history("TXUR", "2026-07-01T00:00:00", "2026-08-01", as_of="2026-10-11")
    assert len(calls) == 1


def test_history_is_not_cached_and_clocks_survive_frozen_codec(monkeypatch):
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    pages = [{"count": 1, "offset": 0, "limit": 100000,
        "observations": [row("2026-07-01", "4.5", "2026-08-21", "2026-10-11")]}] * 2
    calls = install_pages(monkeypatch, pages)
    source = FREDSource("offline")
    first = source.fetch_observation_history("TXUR", "2026-07-01", "2026-08-01", as_of="2026-10-11")
    second = source.fetch_observation_history("TXUR", "2026-07-01", "2026-08-01", as_of="2026-10-11")
    assert len(calls) == 2
    assert first["observations"] == second["observations"]
    assert SourceInputStore.decode(SourceInputStore.encode(first)) == first
    assert not source._cache


def test_later_page_deadline_failure_cannot_cache_a_successful_prefix(monkeypatch):
    import tradingagents.strategies.data_sources.fred_source as fred
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs["params"]["offset"])
        if len(calls) == 2:
            raise SourceFetchError("provider deadline exhausted", reason_code="timeout")
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({"count": 2, "offset": 0, "limit": 1,
            "observations": [row("2026-07-01", "4.5")]}).encode()
        return response
    monkeypatch.setattr(fred, "provider_request", request)
    source = FREDSource("offline")
    with pytest.raises(SourceFetchError, match="timeout") as caught:
        source.fetch_series("TXUR", "2026-07-01", "2026-10-11")
    assert caught.value.failed_operations == {"TXUR": "timeout"}
    assert calls == [0, 1]
    assert not source._cache


@pytest.mark.parametrize("payload", [
    {"observations": [row("2026-07-01", "4.5")]},
    {"count": 1, "offset": 0, "limit": 100000,
     "observations": [{"date": "2026-07-01", "value": "4.5"}]},
])
def test_native_snapshot_requires_complete_pagination_and_realtime_metadata(monkeypatch, payload):
    install_pages(monkeypatch, [payload])
    with pytest.raises(SourceFetchError, match="invalid_response"):
        FREDSource("offline").fetch_series("TXUR", "2026-07-01", "2026-10-11")


MAINTENANCE_HTML = b"""<!doctype html><html><head>
<title>Site Maintenance | St. Louis Fed</title></head>
<body><h1>We'll be back soon.</h1></body></html>"""


def install_responses(monkeypatch, bodies):
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    calls = []
    def transport(url, **options):
        response = requests.Response()
        response.status_code = 200
        response.headers['Content-Type'] = 'text/html' if bodies[len(calls)].startswith(b'<') else 'application/json'
        response._content = bodies[len(calls)]
        calls.append(response)
        return response
    monkeypatch.setattr(FREDSource, '_transport_get', staticmethod(transport))
    return calls, provider_budget('fred', 60, clock=lambda: 0, sleep=lambda _: None,
                                  random_fn=lambda: 0, limits=())


def test_branded_maintenance_retries_before_success_and_keeps_real_http_status(monkeypatch, caplog):
    calls, budget = install_responses(monkeypatch, [MAINTENANCE_HTML] * 3)
    source = FREDSource('SECRET_API_KEY')
    with budget as diagnostics, pytest.raises(SourceFetchError) as caught:
        source.fetch_series('UNRATE', '2026-07-01', '2026-10-11')
    error = caught.value
    assert (error.reason_code, error.http_status, error.attempts) == ('provider_maintenance', 200, 3)
    assert error.failed_operations == {'UNRATE': 'provider_maintenance'}
    assert error.failed_http_statuses == {'UNRATE': 200}
    assert len(calls) == 3 and not source._cache
    assert diagnostics == [{'provider': 'fred', 'operation': 'UNRATE', 'reason_code': 'provider_maintenance',
                           'http_status': 200, 'attempts': 3, 'recovered': False}]
    assert 'SECRET_API_KEY' not in str(error) + caplog.text + json.dumps(diagnostics)


def test_maintenance_then_valid_page_recovers_with_same_vintage(monkeypatch):
    valid = json.dumps({'count': 1, 'offset': 0, 'limit': 100000,
                       'observations': [row('2026-08-01', '4.4')]}).encode()
    calls, budget = install_responses(monkeypatch, [MAINTENANCE_HTML, valid])
    with budget as diagnostics:
        result = FREDSource('offline').fetch_series('UNRATE', '2026-07-01', '2026-10-11')
    assert result.to_list() == [4.4]
    assert result.attrs['vintage_date'] == '2026-10-11'
    assert len(calls) == 2
    assert diagnostics[0]['recovered'] and diagnostics[0]['attempts'] == 2


@pytest.mark.parametrize('body', [b'<html>Maintenance</html>', b'{broken', b'{"observations": {}}'])
def test_unrecognized_html_or_invalid_json_is_terminal_not_maintenance(monkeypatch, body):
    calls, budget = install_responses(monkeypatch, [body])
    with budget as diagnostics, pytest.raises(SourceFetchError) as caught:
        FREDSource('offline').fetch_series('UNRATE', '2026-07-01', '2026-10-11')
    assert caught.value.reason_code == 'invalid_response'
    assert len(calls) == 1 and diagnostics[0]['reason_code'] == 'invalid_response'


def test_history_preserves_maintenance_retry_evidence(monkeypatch):
    _, budget = install_responses(monkeypatch, [MAINTENANCE_HTML] * 3)
    with budget, pytest.raises(SourceFetchError) as caught:
        FREDSource('offline').fetch_observation_history('TXUR', '2026-07-01', '2026-08-01', as_of='2026-10-11')
    assert (caught.value.reason_code, caught.value.attempts) == ('provider_maintenance', 3)
    assert caught.value.failed_http_statuses == {'TXUR': 200}


def test_batch_keeps_independent_maintenance_operation_evidence(monkeypatch):
    _, budget = install_responses(monkeypatch, [MAINTENANCE_HTML] * 6)
    with budget, pytest.raises(SourceFetchError) as caught:
        FREDSource('offline').fetch_multi_series(['UNRATE', 'PAYEMS'], '2026-07-01', '2026-10-11')
    assert caught.value.failed_operations == {'UNRATE': 'provider_maintenance', 'PAYEMS': 'provider_maintenance'}
    assert caught.value.failed_http_statuses == {'UNRATE': 200, 'PAYEMS': 200}
    assert caught.value.attempts == 3
