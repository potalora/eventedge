"""Provider failures remain visible through real adapters, fetch and screening."""

from __future__ import annotations

import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest
import requests

from tradingagents.strategies.data_sources.courtlistener_source import (
    CourtListenerSource,
)
from tradingagents.strategies.data_sources.fred_source import FREDSource
from tradingagents.strategies.data_sources.registry import DataSourceRegistry
from tradingagents.strategies.data_sources.usaspending_source import USASpendingSource
from tradingagents.strategies.orchestration.multi_strategy_engine import (
    MultiStrategyEngine,
)
from tradingagents.strategies.orchestration.preflight import run_preflight

_SECRET = "https://provider.invalid/?api_key=fixture-secret"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def reject(*args, **kwargs):
        pytest.fail("unexpected network access")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket, "getaddrinfo", reject)
    monkeypatch.setattr(
        "tradingagents.strategies.data_sources.courtlistener_source.time.sleep",
        lambda _: None,
    )


class _NoEventStrategy:
    name = "source_observer"
    track = "paper_trade"

    def __init__(self, source):
        self.data_sources = [source]

    def get_default_params(self, horizon="30d"):
        return {}

    def screen(self, data, trading_date, params):
        return []


def _engine(tmp_path, source):
    registry = DataSourceRegistry()
    registry.register(source)
    config = {"autoresearch": {"state_dir": str(tmp_path)}}
    engine = MultiStrategyEngine(
        config,
        registry=registry,
        strategies=[_NoEventStrategy(source.name)],
        use_llm=False,
    )
    return config, engine


def _response(payload, status=200):
    return SimpleNamespace(status_code=status, headers={}, json=lambda: payload, iter_content=lambda chunk_size: iter([json.dumps(payload).encode()]), close=lambda: None)


def _assert_failure_visible(config, engine, source):
    data = engine._fetch_all_data("2026-07-01", "2026-10-01")
    assert data[source].get("error"), (
        "failed provider must not appear as healthy empty data"
    )
    assert _SECRET not in str(data[source].get("error"))
    _, _, health = engine.screen_and_enrich(
        "2026-10-01", data, epoch_id="epoch-test", policy_id="30d"
    )
    assert health[0].status == "data_failure"
    assert health[0].evidence["provider_errors"][source] == data[source]["error"]
    assert _SECRET not in json.dumps(health[0].evidence)
    report = run_preflight(config, "2026-10-01", engine=engine)
    assert report["ok"] is False
    assert report["failures"]
    assert any(source in item["error"] for item in report["failures"])
    assert _SECRET not in json.dumps(report)
    return data[source]


@pytest.mark.parametrize("provider", ["usaspending", "courtlistener"])
@pytest.mark.parametrize(
    "failure", ["transport", "http", "malformed", "missing_results"]
)
def test_http_failure_reaches_daily_health_and_preflight(
    tmp_path, monkeypatch, provider, failure
):
    if failure == "transport":
        request = Mock(side_effect=requests.Timeout(_SECRET))
    else:
        payload = (
            []
            if failure == "malformed"
            else (
                {"detail": "unavailable"}
                if failure == "missing_results"
                else {"results": []}
            )
        )
        request = Mock(
            return_value=_response(payload, 503 if failure == "http" else 200)
        )
    monkeypatch.setattr(
        "requests.post" if provider == "usaspending" else "requests.get", request
    )
    source = (
        USASpendingSource()
        if provider == "usaspending"
        else CourtListenerSource(token="offline")
    )
    config, engine = _engine(tmp_path, source)
    _assert_failure_visible(config, engine, provider)


@pytest.mark.parametrize("provider", ["usaspending", "courtlistener"])
def test_successful_empty_provider_is_legitimate_no_event(
    tmp_path, monkeypatch, provider
):
    monkeypatch.setattr(
        "requests.post" if provider == "usaspending" else "requests.get",
        lambda *args, **kwargs: _response({"results": [], "count": 0, "next": None, "page_metadata": {"page": 1, "hasNext": False}}),
    )
    source = (
        USASpendingSource()
        if provider == "usaspending"
        else CourtListenerSource(token="offline")
    )
    config, engine = _engine(tmp_path, source)
    data = engine._fetch_all_data("2026-07-01", "2026-10-01")
    _, _, health = engine.screen_and_enrich(
        "2026-10-01", data, epoch_id="epoch-test", policy_id="30d"
    )
    assert health[0].status == "legitimate_no_event"
    assert run_preflight(config, "2026-10-01", engine=engine)["ok"] is True


def test_usaspending_failure_is_not_cached_as_empty_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "requests.post",
        Mock(side_effect=[requests.Timeout(_SECRET)] * 3 + [_response({"results": [], "next": None, "page_metadata": {"page": 1, "hasNext": False}})]),
    )
    source = USASpendingSource()
    _, engine = _engine(tmp_path, source)
    failed = engine._fetch_usaspending_data("2026-10-01")
    recovered = engine._fetch_usaspending_data("2026-10-01")
    assert failed.get("error")
    assert not recovered.get("error")
    assert recovered["data"]["contracts"] == []
    assert recovered["coverage"]["complete"] is True


def test_fred_partial_failure_keeps_valid_series_and_reaches_health(
    tmp_path, monkeypatch
):
    def series(self, series_id, **kwargs):
        if series_id == "CPIAUCSL":
            raise requests.Timeout(_SECRET)
        return pd.Series([3.0], index=pd.to_datetime(["2026-09-01"]))

    monkeypatch.setattr("tradingagents.strategies.data_sources.fred_source.FREDSource._get_series", series)
    source = FREDSource(api_key="offline")
    config, engine = _engine(tmp_path, source)
    payload = _assert_failure_visible(config, engine, "fred")
    assert payload["UNRATE"].iloc[0] == 3.0
    assert payload["PAYEMS"].iloc[0] == 3.0
    assert payload["BAMLH0A0HYM2"].iloc[0] == 3.0


def test_fred_successful_empty_series_is_not_fetch_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tradingagents.strategies.data_sources.fred_source.FREDSource._get_series", lambda *args, **kwargs: pd.Series(dtype=float)
    )
    source = FREDSource(api_key="offline")
    config, engine = _engine(tmp_path, source)
    assert run_preflight(config, "2026-10-01", engine=engine)["ok"] is True
    assert not engine._fetch_fred_data("2026-07-01", "2026-10-01").get("error")


def test_courtlistener_partial_failure_retains_other_query_results(
    tmp_path, monkeypatch
):
    def request(*args, **kwargs):
        if kwargs["params"]["q"] == "SEC enforcement":
            raise requests.Timeout(_SECRET)
        return _response(
            {"results": [{"docket_id": 123, "caseName": "Fixture litigation", "dateFiled": "2026-09-30", "court": "cacd"}], "count":1, "next":None}
        )

    monkeypatch.setattr("requests.get", request)
    _, engine = _engine(tmp_path, CourtListenerSource(token="offline"))
    data = engine._fetch_courtlistener_data()
    assert data.get("error")
    assert [item["docket_id"] for item in data["dockets"]] == [123, 123]


@pytest.mark.parametrize("method", ["search_dockets", "search_opinions"])
def test_courtlistener_generic_dispatch_retains_safe_error(monkeypatch, method):
    monkeypatch.setattr("requests.get", Mock(side_effect=requests.Timeout(_SECRET)))
    result = CourtListenerSource(token="offline").fetch({"method": method})
    assert result.get("error")
    assert _SECRET not in json.dumps(result)


def test_usaspending_generic_dispatch_retains_safe_error(monkeypatch):
    monkeypatch.setattr("requests.post", Mock(side_effect=requests.Timeout(_SECRET)))
    result = USASpendingSource().fetch({"method": "recent_large_contracts"})
    assert result.get("error")
    assert _SECRET not in json.dumps(result)


def test_fred_generic_batch_retains_partial_data_and_safe_error(monkeypatch):
    def series(self, series_id, **kwargs):
        if series_id == "CPIAUCSL":
            raise requests.Timeout(_SECRET)
        return pd.Series([3.0], index=["2026-09-01"])

    monkeypatch.setattr("tradingagents.strategies.data_sources.fred_source.FREDSource._get_series", series)
    result = FREDSource(api_key="offline").fetch(
        {"method": "multi_series", "series_ids": ["CPIAUCSL", "UNRATE"],
         "start": "2026-07-01", "end": "2026-10-01", "as_of": "2026-10-01"}
    )
    assert result.get("error")
    assert result["data"]["UNRATE"] == {"2026-09-01": 3.0}
    assert _SECRET not in json.dumps(result)


def test_fred_malformed_series_is_visible_and_not_cached(tmp_path, monkeypatch):
    malformed = True

    def series(self, series_id, **kwargs):
        if series_id == "CPIAUCSL" and malformed:
            return {"unexpected": "provider shape"}
        return pd.Series([3.0], index=pd.to_datetime(["2026-09-01"]))

    monkeypatch.setattr("tradingagents.strategies.data_sources.fred_source.FREDSource._get_series", series)
    _, engine = _engine(tmp_path, FREDSource(api_key="offline"))
    failed = engine._fetch_fred_data("2026-07-01", "2026-10-01")
    assert failed.get("error")
    malformed = False
    recovered = engine._fetch_fred_data("2026-07-01", "2026-10-01")
    assert not recovered.get("error")
    assert recovered["CPIAUCSL"].iloc[0] == 3.0


def test_source_failure_preflight_reaches_worker_exit_status(tmp_path, monkeypatch):
    from scripts.run_cohorts import _preflight_exit_status
    from tradingagents.strategies.orchestration.generation_manager import (
        normalize_preflight_report,
    )

    monkeypatch.setattr("requests.post", Mock(side_effect=requests.Timeout(_SECRET)))
    config, engine = _engine(tmp_path, USASpendingSource())
    report = run_preflight(config, "2026-10-01", engine=engine)
    normalized = normalize_preflight_report(
        report, mode="screen", trading_date="2026-10-01"
    )
    assert normalized is not None
    assert normalized["screen_failure_count"] == 1
    assert _preflight_exit_status(report, "screen", "2026-10-01") == (
        1,
        "PREFLIGHT SCREEN FAILED: 1 failure(s)",
    )


@pytest.mark.parametrize("missing_required", [False, True])
def test_weather_optional_openbb_absence_preserves_health_and_preflight(
    tmp_path, monkeypatch, missing_required
):
    from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
    from tradingagents.strategies.data_sources.usda_source import ConditionObservations
    from tradingagents.strategies.modules.weather_ag import WeatherAgStrategy

    # Keep the real shared fetcher, Yahoo adapter, and weather screen. Only
    # external price delivery and weather-provider responses are substituted.
    dates = pd.bdate_range(end="2026-10-01", periods=70)

    def download(tickers, **kwargs):
        if tickers == "^VIX":
            return pd.DataFrame({"Close": [15.0] * len(dates)}, index=dates)
        columns = pd.MultiIndex.from_product([["Close"], tickers])
        return pd.DataFrame(
            [[100.0 + i] * len(tickers) for i in range(len(dates))],
            index=dates,
            columns=columns,
        )

    monkeypatch.setattr("yfinance.download", download)
    registry = DataSourceRegistry()
    registry.register(YFinanceSource())
    registry.register(
        SimpleNamespace(
            name="noaa",
            is_available=lambda: True,
            fetch_ag_weather_summary=lambda *a, **kw: {
                "heat_stress_days": 0,
                "precip_deficit_pct": 0,
                "frost_events": 0,
                "coverage": {"complete": True},
                "observation_date": "2026-09-30",
                "available_at": "2026-10-01T20:00:00+00:00",
            },
        )
    )
    if not missing_required:
        registry.register(
            SimpleNamespace(
                name="usda",
                is_available=lambda: True,
                fetch_crop_progress=lambda *a, **kw: ConditionObservations([{
                    "week_ending": "2026-09-27", "state": "IA", "good_pct": 50,
                    "excellent_pct": 20, "available_at": "2026-10-01T20:00:00+00:00",
                }], {"complete": True, "scope_mode": "explicit_states", "requested_states": ["IA"]}),
            )
        )
    registry.register(
        SimpleNamespace(
            name="drought_monitor",
            is_available=lambda: True,
            fetch_drought_severity=lambda *a, **kw: {"IA": {
                "None": 0, "D0": 0, "D1": 0, "D2": 100, "D3": 0, "D4": 0,
                "observation_date": "2026-09-29", "available_at": "2026-10-01T20:00:00+00:00",
            }},
            fetch_composite_score=lambda *a, **kw: 2.0,
        )
    )
    config = {"autoresearch": {"state_dir": str(tmp_path)}}
    engine = MultiStrategyEngine(
        config,
        registry=registry,
        strategies=[WeatherAgStrategy()],
        use_llm=False,
    )
    engine._analyzer = SimpleNamespace(analyze_ag_weather=lambda *a, **kw: {
        "direction": "long", "conviction": 0.8,
        "rationale": "Iowa severe drought creates an agricultural supply disruption.",
        "evidence_claim": "Iowa reports D2 drought across 100 percent of its area.",
    })
    data = engine._fetch_all_data("2026-07-01", "2026-10-01")
    signals, _, health = engine.screen_and_enrich(
        "2026-10-01",
        data,
        epoch_id="epoch-test",
        policy_id="30d",
    )
    assert signals, "real weather screen must exercise its configured universe"
    assert health[0].status == ("data_failure" if missing_required else "signals")
    if missing_required:
        assert set(health[0].evidence["provider_errors"]) == {"usda", "analysis"}
        assert health[0].evidence["non_actionable_reasons"] == ["incomplete_environmental_inputs"]
        assert all(signal["journal_only"] for signal in signals)
    report = run_preflight(config, "2026-10-01", engine=engine)
    assert report["ok"] is (not missing_required)
    assert report["source_warnings"] == [
        {"source": "openbb", "error": "optional enrichment unavailable"}
    ]
    assert all(
        row["weather_ag"]["staged"] > 0 and not row["weather_ag"]["errors"]
        for row in report["horizons"].values()
    )
    assert len(report["failures"]) == int(missing_required)
    if missing_required:
        assert "usda" in report["failures"][0]["error"]


@pytest.mark.parametrize("provider", ["usaspending", "courtlistener"])
@pytest.mark.parametrize(
    "failure,code",
    [
        ("timeout", "timeout"),
        ("transport", "transport_error"),
        ("http", "http_error"),
        ("malformed", "invalid_response"),
    ],
)
def test_http_failure_keeps_safe_reason_in_health(
    tmp_path, monkeypatch, provider, failure, code
):
    if failure in {"timeout", "transport"}:
        error = (
            requests.Timeout(_SECRET)
            if failure == "timeout"
            else requests.ConnectionError(_SECRET)
        )
        request = Mock(side_effect=error)
    else:
        request = Mock(
            return_value=_response(
                [] if failure == "malformed" else {"results": []},
                503 if failure == "http" else 200,
            )
        )
    monkeypatch.setattr(
        "requests.post" if provider == "usaspending" else "requests.get", request
    )
    source = (
        USASpendingSource()
        if provider == "usaspending"
        else CourtListenerSource(token="offline")
    )
    config, engine = _engine(tmp_path, source)
    payload = _assert_failure_visible(config, engine, provider)
    assert code in payload["error"]
    if provider == "courtlistener":
        assert "securities_class_action" in payload["error"]
        assert "sec_enforcement" in payload["error"]
        assert "antitrust" in payload["error"]
    if failure == "http":
        assert "503" in payload["error"]


def test_fred_batch_preserves_failed_series_and_reason_across_groups(
    tmp_path, monkeypatch
):
    def series(self, series_id, **kwargs):
        if series_id == "CPIAUCSL":
            raise requests.Timeout(_SECRET)
        if series_id == "BAMLH0A0HYM2":
            return {"malformed": True}
        return pd.Series([3.0], index=["2026-09-01"])

    monkeypatch.setattr("tradingagents.strategies.data_sources.fred_source.FREDSource._get_series", series)
    config, engine = _engine(tmp_path, FREDSource(api_key="offline"))
    payload = _assert_failure_visible(config, engine, "fred")
    assert "CPIAUCSL:timeout" in payload["error"]
    assert "BAMLH0A0HYM2:invalid_response" in payload["error"]
    assert payload["UNRATE"].iloc[0] == 3.0


@pytest.mark.parametrize("provider", ["usaspending", "courtlistener", "fred"])
def test_generic_dispatch_preserves_safe_reason(provider, monkeypatch):
    if provider == "fred":
        monkeypatch.setattr(
            "tradingagents.strategies.data_sources.fred_source.FREDSource._get_series", Mock(side_effect=requests.Timeout(_SECRET))
        )
        result = FREDSource(api_key="offline").fetch(
            {"method": "series", "series_id": "CPIAUCSL", "start": "2026-07-01", "end": "2026-10-01", "as_of": "2026-10-01"}
        )
    else:
        monkeypatch.setattr(
            "requests.post" if provider == "usaspending" else "requests.get",
            Mock(side_effect=requests.Timeout(_SECRET)),
        )
        source = (
            USASpendingSource()
            if provider == "usaspending"
            else CourtListenerSource(token="offline")
        )
        result = source.fetch({})
    assert "timeout" in result["error"]
    assert _SECRET not in json.dumps(result)


def test_fred_direct_diagnostic_retains_safe_series_identity(monkeypatch):
    from urllib.error import URLError

    monkeypatch.setattr(
        "tradingagents.strategies.data_sources.fred_source.FREDSource._get_series", Mock(side_effect=URLError(TimeoutError(_SECRET)))
    )
    source = FREDSource(api_key="offline")
    result = source.fetch({"method": "series", "series_id": "CPIAUCSL", "start": "2026-07-01", "end": "2026-10-01", "as_of": "2026-10-01"})
    assert "CPIAUCSL:timeout" in result["error"]
    assert _SECRET not in result["error"]
    unsafe_identity = source.fetch({"method": "series", "series_id": _SECRET, "start": "2026-07-01", "end": "2026-10-01", "as_of": "2026-10-01"})
    assert _SECRET not in unsafe_identity["error"]


@pytest.mark.parametrize("provider", ["usaspending", "courtlistener"])
def test_invalid_json_is_a_safe_invalid_response(provider, monkeypatch):
    response = SimpleNamespace(
        status_code=200, headers={}, close=lambda:None, iter_content=lambda chunk_size: iter([b"invalid synthetic JSON"]), json=Mock(side_effect=json.JSONDecodeError(_SECRET, "", 0))
    )
    monkeypatch.setattr(
        "requests.post" if provider == "usaspending" else "requests.get",
        lambda *a, **kw: response,
    )
    source = (
        USASpendingSource()
        if provider == "usaspending"
        else CourtListenerSource(token="offline")
    )
    result = source.fetch({})
    assert "invalid_response" in result["error"]
    assert _SECRET not in result["error"]


@pytest.mark.parametrize(
    "body",
    [
        b'<error message="fixture-secret"/>',
        b"<html><title>Bad Gateway</title><body>fixture-secret",
    ],
)
def test_fred_actual_http_wrapper_retains_status_and_redacts_body(
    tmp_path, monkeypatch, body
):
    def transport(url, **options):
        series_id = options["params"]["series_id"]
        if series_id == "CPIAUCSL":
            response = _response({})
            response.status_code = 502
            return response
        return _response({"observations": [{"date": "2026-09-01", "value": "3.0"}]})

    monkeypatch.setattr(FREDSource, "_transport_get", staticmethod(transport))
    source = FREDSource(api_key="fixture-secret")
    config, engine = _engine(tmp_path, source)
    payload = _assert_failure_visible(config, engine, "fred")
    assert "CPIAUCSL:http_error" in payload["error"]
    assert "502" in payload["error"]
    assert "fixture-secret" not in payload["error"]
    assert payload["UNRATE"].iloc[0] == 3.0
    result = source.fetch(
        {
            "method": "series",
            "series_id": "CPIAUCSL",
            "start": "2026-07-01",
            "end": "2026-10-01",
        }
    )
    assert "http_error" in result["error"]
    assert "502" in result["error"]
    assert "fixture-secret" not in json.dumps(result)


def test_source_error_chain_cycles_and_deep_wrappers_are_bounded():
    from tradingagents.strategies.data_sources.fetch_errors import source_fetch_error

    cyclic = ValueError(_SECRET)
    cyclic.__context__ = cyclic
    assert (
        source_fetch_error("FRED fetch failed", cyclic).reason_code == "provider_error"
    )

    wrapped = requests.Timeout(_SECRET)
    for _ in range(20):
        parent = ValueError(_SECRET)
        parent.__cause__ = wrapped
        wrapped = parent
    result = source_fetch_error("FRED fetch failed", wrapped)
    assert result.reason_code == "provider_error"
    assert _SECRET not in str(result)


@pytest.fixture(autouse=True)
def bounded_offline_acquisition(monkeypatch):
    # This suite verifies health propagation; policy pacing is independently
    # tested with a fake clock in test_request_policy.py.
    monkeypatch.setattr('tradingagents.strategies.data_sources.request_policy.PROVIDER_LIMITS', {})
    monkeypatch.setattr('time.sleep', lambda _: None)
