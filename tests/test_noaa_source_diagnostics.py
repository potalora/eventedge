"""The observed NOAA 503/timeouts remain failed and explainable after screening.

Only the HTTP boundary is replaced. No native trace bodies or credentials belong
in this regression; the observed status/exception classes are its counterexample.
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest
import requests


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def sleep(self, delay):
        with self.lock:
            self.now += delay


class _WeatherScreen:
    name = "fixture"
    data_sources = ("noaa",)

    def get_default_params(self, **kwargs):
        return {}

    def screen(self, data, trading_date, params):
        return []


@pytest.mark.parametrize("failure", ["http_503", "read_timeout"])
def test_native_noaa_failure_survives_outer_fetch_and_preflight(
    failure, monkeypatch, tmp_path
):
    from tradingagents.strategies.data_sources import noaa_source as noaa
    from tradingagents.strategies.data_sources import request_policy as policy
    from tradingagents.strategies.data_sources.registry import DataSourceRegistry
    from tradingagents.strategies.orchestration.generation_manager import (
        _preflight_subprocess_result,
        normalize_preflight_report,
    )
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies.orchestration.preflight import run_preflight
    from tradingagents.strategies.orchestration.source_inputs import successful_source

    clock = _Clock()
    monkeypatch.setattr(policy.time, "monotonic", clock)
    monkeypatch.setattr(policy.time, "sleep", clock.sleep)
    monkeypatch.setattr(policy, "_HISTORY", {})
    monkeypatch.setattr(noaa, "current_session_date", lambda: "2026-10-16", raising=False)
    monkeypatch.setenv("AUTORESEARCH_FETCH_TIMEOUT_S", "900")
    physical_requests = []

    def transport(url, **options):
        physical_requests.append((url, options))
        # The candidate's public source and main's CDO source share this
        # regression. The catalogs contain one station for every requested state.
        stations = [(f"US{i:09d}", state) for i, state in enumerate(noaa.AG_STATES)]
        if url.endswith("ghcnd-stations.txt"):
            return SimpleNamespace(status_code=200, headers={}, text="\n".join(
                station + " " * 27 + state for station, state in stations
            ))
        if url.endswith("ghcnd-inventory.txt"):
            return SimpleNamespace(status_code=200, headers={}, text="\n".join(
                station + " " * 20 + "TMAX 2020 2026" for station, _ in stations
            ))
        if failure == "read_timeout":
            raise requests.ReadTimeout("https://private.invalid?token=PRIVATE_NOAA_TOKEN")
        return SimpleNamespace(
            status_code=503,
            headers={"Set-Cookie": "PRIVATE_RESPONSE_COOKIE"},
            close=lambda: None,
        )

    monkeypatch.setattr(policy.requests, "get", transport)
    source = noaa.NOAASource(token="PRIVATE_NOAA_TOKEN")
    monkeypatch.setattr(source, "_get_session", lambda: SimpleNamespace(get=transport))
    registry = DataSourceRegistry()
    registry.register(source)
    config = {"autoresearch": {
        "state_dir": str(tmp_path / "state"),
        "source_cache_dir": str(tmp_path / "cache"),
    }}
    engine = MultiStrategyEngine(config, strategies=[_WeatherScreen()], registry=registry)
    payload = engine._fetch_all_data("2026-07-18", "2026-10-16")["noaa"]

    assert successful_source(payload) is False
    assert payload["states_reporting"] == 0
    assert payload["observations"] == 0
    coverage = payload["_coverage"]
    assert coverage["reason_code"] == "batch_failure"
    assert set(coverage["failed_operations"]) == set(noaa.AG_STATES)
    expected_reason = "http_error" if failure == "http_503" else "timeout"
    assert set(coverage["failed_operations"].values()) == {expected_reason}
    assert coverage["attempts"] == 0  # Aggregate error, never physical request count.
    assert physical_requests
    logical_operations = {
        (url, json.dumps(options.get("params", {}), sort_keys=True))
        for url, options in physical_requests
    }
    assert len(payload["_request_diagnostics"]) == len(logical_operations)

    # The second stage consumes the exact captured failure without acquisition.
    monkeypatch.setattr(engine, "_fetch_all_data", lambda *args, **kwargs: {"noaa": payload})
    report = run_preflight(config, "2026-10-16", engine=engine, mode="screen")
    wire = normalize_preflight_report(report, mode="screen", trading_date="2026-10-16")
    assert wire["ok"] is False
    assert wire["screen_ok"] is False
    assert wire["screen_failure_count"] == 1
    assert wire["screen_source_failures"] == [{
        "source": "noaa", "reason_code": "batch_failure",
        "http_status": 503 if failure == "http_503" else None,
        "attempts": 3,
        "operation_count": len(logical_operations),
    }]
    result = _preflight_subprocess_result(
        stdout=json.dumps(wire, indent=2), stderr="", returncode=1, elapsed=1,
        mode="screen", trading_date="2026-10-16",
    )
    assert result["success"] is False
    assert result["screen_source_failures"] == wire["screen_source_failures"]
    for private in ("PRIVATE_NOAA_TOKEN", "PRIVATE_RESPONSE_COOKIE", "private.invalid", "Set-Cookie"):
        assert private not in json.dumps(payload)
        assert private not in json.dumps(result)


def test_narrowed_same_provider_budget_retains_safe_parent_diagnostics(monkeypatch):
    from tradingagents.strategies.data_sources import request_policy as policy
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError

    clock = _Clock()
    monkeypatch.setattr(policy, "_HISTORY", {})
    monkeypatch.setattr(policy.requests, "get", lambda *args, **kwargs: SimpleNamespace(
        status_code=503, headers={"Set-Cookie": "PRIVATE_RESPONSE_COOKIE"}, close=lambda: None,
    ))
    events = []
    with policy.provider_budget("noaa", 900, clock=clock, sleep=clock.sleep, limits=(), diagnostics=events):
        with policy.provider_budget("noaa", 180, clock=clock, sleep=clock.sleep, limits=()):
            with pytest.raises(SourceFetchError):
                policy.provider_request("noaa", "GET", "https://private.invalid?token=PRIVATE_NOAA_TOKEN")
        assert policy.current_provider_deadline("noaa") == 900
    assert events == [{
        "provider": "noaa", "operation": "get", "reason_code": "http_error",
        "http_status": 503, "attempts": 3, "recovered": False,
    }]
    assert "PRIVATE" not in json.dumps(events)


def test_other_provider_and_explicit_diagnostic_sinks_remain_isolated(monkeypatch):
    from tradingagents.strategies.data_sources import request_policy as policy

    clock = _Clock()
    monkeypatch.setattr(policy, "_HISTORY", {})
    monkeypatch.setattr(policy.requests, "get", lambda *args, **kwargs: SimpleNamespace(status_code=200))
    parent, explicit = [], []
    with policy.provider_budget("noaa", 900, clock=clock, sleep=clock.sleep, limits=(), diagnostics=parent):
        with policy.provider_budget("noaa", 180, clock=clock, sleep=clock.sleep, limits=(), diagnostics=explicit):
            policy.provider_request("noaa", "GET", "https://public.invalid")
        with policy.provider_budget("fred", 180, clock=clock, sleep=clock.sleep, limits=()):
            policy.provider_request("fred", "GET", "https://public.invalid")
    assert parent == []
    assert len(explicit) == 1
    assert explicit[0]["provider"] == "noaa"
