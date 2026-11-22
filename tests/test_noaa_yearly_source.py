"""Direct Corn Belt ASOS station GHCNd daily acquisition tests."""
from __future__ import annotations

from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import find_spec
from pathlib import Path
import subprocess
import threading
import time

import pytest

from tradingagents.strategies.data_sources import noaa_yearly_source as module
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget, provider_request


class StreamedCSVResponse:
    status_code = 200

    def __init__(self, body: bytes, *, status_code: int = 200, headers: dict[str, str] | None = None, after_chunk=None):
        self.body = body
        self.status_code = status_code
        self.headers = {"Content-Length": str(len(body))} | (headers or {})
        self.closed = False
        self.consumed = 0
        self.after_chunk = after_chunk

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.body), chunk_size):
            chunk = self.body[offset : offset + chunk_size]
            self.consumed += len(chunk)
            yield chunk
            if self.after_chunk:
                self.after_chunk()

    def close(self):
        self.closed = True


def _build_cornbelt_csv(start: str, end: str, *, heat_days: int = 2, frost_days: int = 0) -> bytes:
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    lines = ['"STATION","DATE","PRCP","PRCP_ATTRIBUTES","TMAX","TMAX_ATTRIBUTES","TMIN","TMIN_ATTRIBUTES"']
    total_days = (d1 - d0).days + 1
    for station in sorted(module.CORN_BELT_STATIONS):
        for offset in range(total_days):
            day = (d0 + timedelta(days=offset)).isoformat()
            # 356 tenths C = 96.08F (>95F heat stress); 250 tenths C = 77.0F
            tmax = 356 if offset < heat_days else 250
            # -10 tenths C = 30.2F (<32F frost); 150 tenths C = 59.0F
            tmin = -10 if offset < frost_days else 150
            # 254 tenths mm -> 1.0 in, 25 tenths mm -> ~0.0984 in
            prcp = 25
            lines.append(f'"{station}","{day}","{prcp}",",,1,2400","{tmax}",",,1","{tmin}",",,1"')
    return ("\n".join(lines) + "\n").encode("utf-8")


def test_explicit_station_adapter_available_without_token(monkeypatch):
    assert find_spec("tradingagents.strategies.data_sources.noaa_yearly_source") is not None
    monkeypatch.delenv("NOAA_CDO_TOKEN", raising=False)
    source = module.NOAAYearlySource()
    assert source.name == "noaa"
    assert source.requires_api_key is False
    assert source.is_available() is True
    assert 20 <= len(module.CORN_BELT_STATIONS) <= 25
    assert set(module.AG_STATES) == set(module.CORN_BELT_STATIONS.values())


@pytest.mark.parametrize(
    "ref_date,lookback,vintage",
    [
        ("2026-07-18", 30, "2026-07-18"),
        ("bad", 30, "2026-07-19"),
        ("2026-07-20", 30, "2026-07-19"),
        ("2026-07-19", 0, None),
        ("2026-07-19", True, None),
        ("2026-07-19", 61, None),
    ],
)
def test_invalid_or_historical_vintage_never_starts_acquisition(monkeypatch, ref_date, lookback, vintage):
    monkeypatch.setattr(module, "current_vintage_date", lambda: "2026-07-19")
    with pytest.raises(SourceFetchError) as raised:
        module.NOAAYearlySource().fetch_ag_weather_summary(ref_date, lookback, vintage_as_of=vintage)
    assert raised.value.partial_data["coverage"]["complete"] is False
    assert raised.value.partial_data["heat_stress_days"] is None
    assert raised.value.partial_data["_coverage"]["reason_code"] in ("invalid_response", "provider_error")


def test_expired_parent_budget_withholds_metrics_before_network(monkeypatch):
    monkeypatch.setattr(module, "current_vintage_date", lambda: "2026-07-19")
    with provider_budget("noaa", 10, clock=lambda: 11, limits=()):
        with pytest.raises(SourceFetchError) as raised:
            module.NOAAYearlySource().fetch_ag_weather_summary("2026-07-19")
    assert raised.value.reason_code == "timeout"
    assert raised.value.partial_data["avg_daily_prcp"] is None


@pytest.mark.parametrize("off_season_date", ["2026-10-18", "2026-11-21", "2026-01-15", "2026-03-31"])
def test_off_season_oct_to_mar_skips_fetch_cleanly(monkeypatch, off_season_date):
    monkeypatch.setattr(module, "current_vintage_date", lambda: off_season_date)
    monkeypatch.setattr(
        module,
        "_yearly_http_get",
        lambda *a, **kw: pytest.fail("No network fetch allowed outside Apr-Sep growing season"),
    )
    res = module.NOAAYearlySource().fetch_ag_weather_summary(off_season_date)
    assert res["off_season_skipped"] is True
    assert res["in_growing_season"] is False
    assert res["_coverage"] == {"status": "ok", "reason": "outside_growing_season"}
    assert res["coverage"]["complete"] is True


def test_growing_season_fetch_computes_exact_metrics_in_kilobytes(monkeypatch):
    monkeypatch.setattr(module, "current_vintage_date", lambda: "2026-07-19")
    # fetch_start for 2026-07-18 lookback=30 + 7 lag = 2026-06-12 .. 2026-07-18
    csv_bytes = _build_cornbelt_csv("2026-06-12", "2026-07-18", heat_days=10, frost_days=0)
    assert len(csv_bytes) < 100_000  # Kilobytes, not 256 MB!
    responses = []

    def fake_get(url, **kwargs):
        assert "access/services/data/v1" in url
        resp = StreamedCSVResponse(csv_bytes)
        responses.append(resp)
        return resp

    monkeypatch.setattr(module, "_yearly_http_get", fake_get)
    result = module.NOAAYearlySource().fetch_ag_weather_summary("2026-07-18", lookback_days=30)
    assert result["coverage"]["complete"] is True
    assert result["states_reporting"] == 10
    assert result["in_growing_season"] is True
    assert result["observation_lag_days"] == 0
    assert result["heat_stress_days"] == 3  # First 3 days of 30d window (offset 7,8,9 of 37d)
    assert result["frost_events"] == 0
    assert result["avg_tmax"] > 77.0
    assert result["avg_daily_prcp"] == pytest.approx(0.098, abs=0.002)
    assert result["coverage"]["catalog_station_count"] == len(module.CORN_BELT_STATIONS)
    assert all(r.closed and r.consumed == len(csv_bytes) for r in responses)


def test_daily_and_etag_local_file_caching(monkeypatch, tmp_path):
    csv_bytes = _build_cornbelt_csv("2026-06-12", "2026-07-18")
    calls = []

    def fake_get(url, **kwargs):
        calls.append(kwargs.get("headers", {}))
        if kwargs.get("headers", {}).get("If-None-Match") == '"etag-v1"':
            return StreamedCSVResponse(b"", status_code=304, headers={"ETag": '"etag-v1"'})
        return StreamedCSVResponse(csv_bytes, headers={"ETag": '"etag-v1"'})

    monkeypatch.setattr(module, "_yearly_http_get", fake_get)
    monkeypatch.setattr(module, "current_vintage_date", lambda: "2026-07-18")

    src = module.NOAAYearlySource(cache_dir=tmp_path)
    first = src.fetch_ag_weather_summary("2026-07-18")
    assert first["coverage"]["complete"] is True
    assert len(calls) == 1

    # Same vintage day -> served from daily local file cache (0 HTTP calls)
    second = src.fetch_ag_weather_summary("2026-07-18")
    assert second["coverage"]["files"][0]["cache_hit"] == "daily"
    assert len(calls) == 1

    # Next day vintage -> sends If-None-Match ETag and accepts 304
    monkeypatch.setattr(module, "current_vintage_date", lambda: "2026-07-19")
    third = src.fetch_ag_weather_summary("2026-07-18")
    assert third["coverage"]["files"][0]["cache_hit"] == "etag_304"
    assert len(calls) == 2
    assert calls[1].get("If-None-Match") == '"etag-v1"'


def test_transport_preserves_diagnostics_and_never_uses_devnull(monkeypatch):
    spawned = []
    original_popen = subprocess.Popen

    def inspect_popen(command, **kwargs):
        assert kwargs.get("stderr") == subprocess.PIPE
        proc = original_popen(command, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", inspect_popen)
    monkeypatch.setattr(module, "current_vintage_date", lambda: "2026-07-19")

    class FailingHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), FailingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(module, "NCEI_ACCESS_URL", f"http://127.0.0.1:{server.server_port}/v1")
        with provider_budget("noaa", time.monotonic() + 10, max_attempts=1, limits=()):
            res = module.NOAAYearlySource().fetch({"date": "2026-07-18"})
        assert res["_coverage"]["reason_code"] == "http_error"
        assert res["_coverage"]["http_status"] == 503
        assert spawned and all(p.poll() is not None for p in spawned)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_worker_transport_timeout_surfaces_error_class_and_message(monkeypatch):
    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(1.5)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with provider_budget("noaa", time.monotonic() + 0.5, max_attempts=1, limits=()):
            with pytest.raises(SourceFetchError) as raised:
                provider_request(
                    "noaa",
                    "GET",
                    f"http://127.0.0.1:{server.server_port}/slow",
                    transport=module._yearly_http_get,
                    operation="station_daily_csv",
                    stream=True,
                    allow_redirects=False,
                    headers={"Accept-Encoding": "identity"},
                    timeout=(0.2, 0.2),
                    max_bytes=1024,
                )
        assert raised.value.reason_code == "timeout"
        diag = raised.value.partial_data.get("_diagnostics", {})
        assert diag.get("error_class")
        assert diag.get("error_message")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
