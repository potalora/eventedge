"""Acceptance tests for lightweight Corn Belt NOAA station acquisition."""
from __future__ import annotations

import pytest

from tradingagents.strategies.data_sources import noaa_yearly_source as module
from tradingagents.strategies.data_sources.registry import build_default_registry


def test_registered_noaa_source_handles_growing_and_off_season(monkeypatch, tmp_path):
    source = build_default_registry({}).get("noaa")
    assert isinstance(source, module.NOAAYearlySource)

    # Off-season (Nov): clean skip without network
    monkeypatch.setattr(module, "current_vintage_date", lambda: "2026-11-21")
    off_season = source.fetch({"date": "2026-11-21"})
    assert off_season["off_season_skipped"] is True
    assert off_season["_coverage"] == {"status": "ok", "reason": "outside_growing_season"}

    # Growing season (Jun): kilobyte-sized station CSV
    monkeypatch.setattr(module, "current_vintage_date", lambda: "2026-06-30")
    lines = ['"STATION","DATE","PRCP","PRCP_ATTRIBUTES","TMAX","TMAX_ATTRIBUTES","TMIN","TMIN_ATTRIBUTES"']
    from datetime import date, timedelta
    start = date(2026, 5, 25)
    for station in sorted(module.CORN_BELT_STATIONS):
        for offset in range(37):
            day = (start + timedelta(days=offset)).isoformat()
            lines.append(f'"{station}","{day}","20",",,1,2400","290",",,1","150",",,1"')
    payload = ("\n".join(lines) + "\n").encode("utf-8")

    class Resp:
        status_code = 200
        headers = {"Content-Length": str(len(payload))}
        def iter_content(self, chunk_size): yield payload
        def close(self): pass

    monkeypatch.setattr(module, "_yearly_http_get", lambda *a, **kw: Resp())
    summary = source.fetch({"date": "2026-06-30"})
    assert summary["coverage"]["complete"] is True
    assert summary["states_reporting"] == 10
    assert summary["in_growing_season"] is True
    assert summary["coverage"]["files"][0]["bytes"] < 100_000
