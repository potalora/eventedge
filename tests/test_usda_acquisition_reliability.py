"""Offline regressions for USDA acquisition failure diagnostics and seasonal scope."""
from __future__ import annotations

import hashlib
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.registry import DataSourceRegistry
from tradingagents.strategies.data_sources.usda_source import (
    USDASource,
    WHEAT_CLASS_STATES,
)
from tradingagents.strategies.orchestration.data_acquisition import DataAcquisitionService
from tradingagents.strategies.orchestration.operational_report import _sources
from tradingagents.strategies.orchestration.source_incidents import assess_source_incidents
from tradingagents.strategies.orchestration.source_inputs import (
    CONTRACT_VERSION,
    SourceInputStore,
    configuration_fingerprint,
    successful_source,
)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("offline regression must not open network sockets")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr("tradingagents.strategies.data_sources.request_policy.PROVIDER_LIMITS", {})
    monkeypatch.setattr("time.sleep", lambda _: None)


def _service_with_usda(source: USDASource) -> DataAcquisitionService:
    registry = DataSourceRegistry()
    registry.register(source)
    return DataAcquisitionService(config={}, registry=registry)


def _write_accepted_envelope(
    tmp_path: Path,
    payload: dict,
    *,
    session: str = "2026-11-27",
    generation: str = "gen_006",
    commit: str = "a" * 40,
    configuration: str = "b" * 64,
) -> Path:
    state = tmp_path / "state"
    source_dir = state / "source_inputs"
    source_dir.mkdir(parents=True, exist_ok=True)
    slot = configuration_fingerprint({"generation": generation, "session": session})
    encoded_payload = SourceInputStore.encode(payload)
    envelope = {
        "version": CONTRACT_VERSION,
        "identity": {
            "generation": generation,
            "session": session,
            "commit": commit,
            "configuration": configuration,
        },
        "acquired_at": f"{session}T23:03:43+00:00",
        "digest": hashlib.sha256(encoded_payload.encode()).hexdigest(),
        "payload": payload,
    }
    (source_dir / f"{slot}.json").write_text(SourceInputStore.encode(envelope))
    return state


def _report_for_sources(
    tmp_path: Path,
    usda_payload: dict,
    *,
    session: str = "2026-11-27",
    generation: str = "gen_006",
    commit: str = "a" * 40,
) -> tuple[dict, list]:
    base_payload = {
        "cftc": {"copper": {}},
        "drought_monitor": {"composite_score": 0.5, "states": {"IA": {"D0": 10}}},
        "finnhub": {"transcripts": []},
        "fred": {"VIXCLS": {}},
        "noaa": {"off_season_skipped": True, "_coverage": {"status": "ok", "reason": "outside_growing_season"}},
        "usaspending": {"data": {"contracts": []}},
        "yfinance": {"prices": {}},
        "usda": usda_payload,
    }
    state = _write_accepted_envelope(tmp_path, base_payload, session=session, generation=generation, commit=commit)
    diagnostics: list = []
    sources_proj = _sources(state, generation, session, commit, lambda *args: diagnostics.append(args))
    report = {
        "generation_id": generation,
        "generation_commit": commit,
        "session": session,
        "evidence_complete": True,
        "source_health_failures": [],
        "sources": sources_proj,
    }
    return report, diagnostics


def test_usda_timeout_propagates_coverage_without_spurious_provider_error(tmp_path, monkeypatch):
    """Nov 27 offline repro: USDA timeout must propagate _coverage(timeout) and emit one reliability pattern."""
    monkeypatch.setattr(
        "tradingagents.strategies.data_sources.usda_source.current_session_date",
        lambda: "2026-11-27",
    )
    source = USDASource(api_key="test-key")
    service = _service_with_usda(source)

    def fake_get(url, **kwargs):
        if "quickstats.nass.usda.gov" in url:
            raise requests.Timeout("quickstats stalled")
        if "esmis.nal.usda.gov/concern/publications/" in url:
            resp = MagicMock(status_code=200)
            resp.text = '<a href="/sites/default/release-files/796160/prog4726.txt">txt</a>'
            return resp
        if "prog4726.txt" in url:
            resp = MagicMock(status_code=200)
            resp.text = "Crop Progress\n"
            return resp
        raise AssertionError(f"unexpected URL: {url}")

    with patch("requests.get", side_effect=fake_get):
        data = service.fetch_all_data("2026-10-01", "2026-11-27", needed_sources={"usda"})

    usda = data["usda"]
    assert usda["error"]
    assert usda["coverage"]["complete"] is False
    assert usda["_coverage"] == {
        "status": "unavailable",
        "reason_code": "timeout",
        "http_status": None,
        "attempts": 3,
    }

    report, diag = _report_for_sources(tmp_path, usda)
    assert diag == []
    unresolved = report["sources"]["unresolved"]
    assert unresolved == [
        {
            "attempts": 3,
            "http_status": None,
            "operation": "crop_condition",
            "provider": "usda",
            "reason_code": "timeout",
            "recovered": False,
            "source": "usda",
        }
    ]

    incidents = assess_source_incidents(report)
    assert len(incidents["open_incidents"]) == 1
    assert incidents["open_incidents"][0]["provider"] == "usda"
    assert incidents["open_incidents"][0]["status"] == "degraded"
    assert incidents["open_incidents"][0]["reason_code"] == "timeout"


def test_usda_semantic_invalid_response_after_http_200_stays_repair_required(tmp_path, monkeypatch):
    """Contract failure after HTTP 200 must propagate invalid_response -> repair_required, never provider_error."""
    monkeypatch.setattr(
        "tradingagents.strategies.data_sources.usda_source.current_session_date",
        lambda: "2026-11-27",
    )
    source = USDASource(api_key="test-key")
    service = _service_with_usda(source)

    bad_resp = MagicMock(status_code=200)
    bad_resp.json.return_value = {"unexpected": "schema"}
    with patch("requests.get", return_value=bad_resp):
        data = service.fetch_all_data("2026-10-01", "2026-11-27", needed_sources={"usda"})

    usda = data["usda"]
    assert usda["_coverage"]["reason_code"] == "invalid_response"
    assert usda["_coverage"]["status"] == "unavailable"

    report, diag = _report_for_sources(tmp_path, usda)
    assert diag == []
    assert report["sources"]["unresolved"] == [
        {"http_status": None, "provider": "usda", "reason_code": "invalid_response"}
    ]
    incidents = assess_source_incidents(report)
    assert incidents["open_incidents"][0]["status"] == "repair_required"
    assert incidents["open_incidents"][0]["reason_code"] == "invalid_response"


def test_usda_mixed_failures_surface_terminal_reason_over_transient(tmp_path, monkeypatch):
    """During active season, a terminal contract error on one crop must not be masked by a timeout on another."""
    monkeypatch.setattr(
        "tradingagents.strategies.data_sources.usda_source.current_session_date",
        lambda: "2026-07-15",
    )

    class _MixedUSDA:
        name = "usda"
        requires_api_key = True

        def is_available(self):
            return True

        def fetch_crop_progress(self, commodity, year, **kwargs):
            if commodity == "CORN":
                raise SourceFetchError("Provider acquisition failed", reason_code="timeout", attempts=3)
            if commodity == "SOYBEANS":
                raise SourceFetchError("USDA crop response invalid", reason_code="invalid_response", attempts=1)
            raise SourceFetchError("Provider acquisition failed", reason_code="timeout", attempts=3)

    service = _service_with_usda(_MixedUSDA())  # type: ignore[arg-type]
    usda = service.fetch_usda_data("2026-07-15")
    assert usda["_coverage"]["reason_code"] == "invalid_response"
    assert usda["_coverage"]["status"] == "unavailable"
    assert usda["_coverage"]["attempts"] == 3

    # Verify _sources() keeps distinct _coverage(invalid_response) alongside _request_diagnostics(timeout)
    usda_with_diag = {
        **usda,
        "_request_diagnostics": [
            {
                "provider": "usda",
                "operation": "crop_condition",
                "reason_code": "timeout",
                "http_status": None,
                "attempts": 3,
                "recovered": False,
            }
        ],
    }
    report, diag = _report_for_sources(tmp_path, usda_with_diag, session="2026-07-15")
    assert diag == []
    assert any(
        row.get("operation") == "crop_condition" and row["reason_code"] == "timeout"
        for row in report["sources"]["unresolved"]
    )
    assert {"http_status": None, "provider": "usda", "reason_code": "invalid_response"} in report["sources"]["unresolved"]
    incidents = assess_source_incidents(report)
    assert incidents["open_incidents"][0]["status"] == "repair_required"
    assert incidents["open_incidents"][0]["reason_code"] == "invalid_response"


def test_sources_dedup_preserves_legacy_missing_coverage_and_distinct_transport_provider(tmp_path):
    """_sources() only suppresses the summary row when typed _coverage matches same-provider diagnostic."""
    # Legacy payload without _coverage keeps fallback provider_error summary row alongside diagnostic
    legacy_usda = {
        "error": "WHEAT: Provider acquisition failed [timeout]",
        "_request_diagnostics": [
            {
                "provider": "usda",
                "operation": "crop_condition",
                "reason_code": "timeout",
                "http_status": None,
                "attempts": 3,
                "recovered": False,
            }
        ],
    }
    legacy_report, diag = _report_for_sources(tmp_path / "legacy", legacy_usda)
    assert diag == []
    assert {"http_status": None, "provider": "usda", "reason_code": "provider_error"} in legacy_report["sources"]["unresolved"]

    # Distinct transport provider (provider != source) preserves both rows even with matching typed _coverage
    cross_provider_usda = {
        "error": "WHEAT: Provider acquisition failed [timeout]",
        "_coverage": {"status": "unavailable", "reason_code": "timeout", "http_status": None, "attempts": 3},
        "_request_diagnostics": [
            {
                "provider": "alpaca",
                "operation": "crop_condition",
                "reason_code": "timeout",
                "http_status": None,
                "attempts": 3,
                "recovered": False,
            }
        ],
    }
    cross_report, diag2 = _report_for_sources(tmp_path / "cross", cross_provider_usda)
    assert diag2 == []
    assert {"http_status": None, "provider": "usda", "reason_code": "timeout"} in cross_report["sources"]["unresolved"]
    assert any(row.get("provider") == "alpaca" and row.get("source") == "usda" for row in cross_report["sources"]["unresolved"])


def test_usda_nov27_skips_inactive_corn_and_soybeans_while_enforcing_active_winter_wheat(monkeypatch):
    """On Nov 27 only WHEAT (WINTER) is active; CORN/SOYBEANS must not waste QuickStats timeouts."""
    monkeypatch.setattr(
        "tradingagents.strategies.data_sources.usda_source.current_session_date",
        lambda: "2026-11-27",
    )
    queried: list[tuple[str, str]] = []

    def fake_get(url, **kwargs):
        if "quickstats.nass.usda.gov" in url:
            params = kwargs["params"]
            queried.append((params["commodity_desc"], params["year"]))
            raise requests.Timeout("quickstats stalled")
        if "esmis.nal.usda.gov/concern/publications/" in url:
            resp = MagicMock(status_code=200)
            resp.text = '<a href="/sites/default/release-files/796160/prog4726.txt">txt</a>'
            return resp
        if "prog4726.txt" in url:
            resp = MagicMock(status_code=200)
            resp.text = "Crop Progress\n"
            return resp
        raise AssertionError(f"unexpected URL: {url}")

    source = USDASource(api_key="test-key")
    service = _service_with_usda(source)
    with patch("requests.get", side_effect=fake_get):
        data = service.fetch_all_data("2026-10-01", "2026-11-27", needed_sources={"usda"})

    # Only WHEAT (reporting year 2027) is queried (3 bounded attempts); CORN & SOYBEANS are never requested.
    assert queried == [("WHEAT", "2027")] * 3
    usda = data["usda"]
    assert usda["coverage"]["complete"] is False
    assert usda["coverage"]["crops"]["CORN"]["complete"] is True
    assert usda["coverage"]["crops"]["CORN"]["off_season_skipped"] is True
    assert usda["coverage"]["crops"]["SOYBEANS"]["complete"] is True
    assert usda["coverage"]["crops"]["SOYBEANS"]["off_season_skipped"] is True
    assert usda["coverage"]["crops"]["WHEAT"]["complete"] is False
    assert usda["crop_progress"]["CORN"] == []
    assert usda["crop_progress"]["SOYBEANS"] == []
    assert usda["_coverage"]["reason_code"] == "timeout"
    assert usda["error"].startswith("WHEAT:")
    assert "CORN" not in usda["error"] and "SOYBEANS" not in usda["error"]


def test_usda_active_winter_wheat_success_stays_clean_and_recovers_incident(tmp_path, monkeypatch):
    """When active winter wheat succeeds, off-season CORN/SOYBEANS skip cleanly and an open incident recovers."""
    monkeypatch.setattr(
        "tradingagents.strategies.data_sources.usda_source.current_session_date",
        lambda: "2025-11-28",
    )
    queried: list[str] = []

    def fake_get(url, **kwargs):
        params = kwargs["params"]
        queried.append(params["commodity_desc"])
        assert params["commodity_desc"] == "WHEAT"
        assert params["year"] == "2026"
        rows = []
        for state in WHEAT_CLASS_STATES["WINTER"]:
            for week, good in (("2025-11-16", "52"), ("2025-11-23", "48")):
                rows.extend([
                    {"commodity_desc": "WHEAT", "year": "2026", "class_desc": "WINTER",
                     "state_alpha": state, "week_ending": week, "unit_desc": "PCT GOOD", "Value": good},
                    {"commodity_desc": "WHEAT", "year": "2026", "class_desc": "WINTER",
                     "state_alpha": state, "week_ending": week, "unit_desc": "PCT EXCELLENT", "Value": "10"},
                ])
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"data": rows}
        return resp

    source = USDASource(api_key="test-key")
    service = _service_with_usda(source)
    with patch("requests.get", side_effect=fake_get):
        data = service.fetch_all_data("2025-10-01", "2025-11-28", needed_sources={"usda"})

    assert queried == ["WHEAT"]
    usda = data["usda"]
    assert "error" not in usda
    assert successful_source(usda) is True
    assert usda["coverage"]["complete"] is True
    assert usda["coverage"]["crops"]["WHEAT"]["complete"] is True
    assert len(usda["crop_progress"]["WHEAT"]) == len(WHEAT_CLASS_STATES["WINTER"]) * 2
    assert usda["crop_progress"]["CORN"] == []
    assert usda["crop_progress"]["SOYBEANS"] == []

    report, diag = _report_for_sources(tmp_path, usda, session="2025-11-28")
    assert diag == []
    assert report["sources"]["observations"]["usda"] == "success"
    assert report["sources"]["unresolved"] == []

    prior = {
        "generation_id": "gen_006",
        "generation_commit": "a" * 40,
        "session": "2025-11-26",
        "evidence_complete": True,
        "source_health_failures": [],
        "sources": {
            "configuration": "b" * 64,
            "acquired_at": "2025-11-26T23:03:43+00:00",
            "observations": {"usda": "unavailable"},
            "unresolved": [{"provider": "usda", "reason_code": "timeout", "http_status": None}],
        },
    }
    prior["source_incidents"] = assess_source_incidents(prior)
    recovered_inc = assess_source_incidents(report, prior)
    assert recovered_inc["history_status"] == "available"
    assert recovered_inc["open_incidents"] == []
    assert [a["kind"] for a in recovered_inc["alerts"]] == ["recovered"]


def test_usda_all_off_season_skips_network_without_falsely_recovering_open_incident(tmp_path, monkeypatch):
    """On Dec 1 (no active crops), USDA skips QuickStats cleanly, stays usable, and never fakes incident recovery."""
    monkeypatch.setattr(
        "tradingagents.strategies.data_sources.usda_source.current_session_date",
        lambda: "2025-12-01",
    )
    source = USDASource(api_key="test-key")
    service = _service_with_usda(source)

    with patch("requests.get", side_effect=AssertionError("off-season USDA must not call QuickStats")):
        data = service.fetch_all_data("2025-10-01", "2025-12-01", needed_sources={"usda"})

    usda = data["usda"]
    assert "error" not in usda
    assert usda["off_season_skipped"] is True
    assert usda["_coverage"] == {"status": "ok", "reason": "outside_declared_comparison_season"}
    assert usda["coverage"]["complete"] is True
    assert successful_source(usda) is True
    assert usda["crop_progress"] == {"CORN": [], "SOYBEANS": [], "WHEAT": []}
    for crop in ("CORN", "SOYBEANS", "WHEAT"):
        assert usda["coverage"]["crops"][crop]["complete"] is True
        assert usda["coverage"]["crops"][crop]["off_season_skipped"] is True

    # WeatherAgStrategy sees usda_ok=True (no false journal_only degradation)
    import pandas as pd
    from tradingagents.strategies.modules.weather_ag import AG_TICKERS_FULL, WeatherAgStrategy

    dates = pd.bdate_range("2025-10-01", periods=45)
    prices = {t: pd.DataFrame({"Close": [100 + i * 0.5 for i in range(45)]}, index=dates) for t in AG_TICKERS_FULL.values()}
    candidates = WeatherAgStrategy().screen(
        {
            "yfinance": {"prices": prices},
            "noaa": {"off_season_skipped": True, "_coverage": {"status": "ok", "reason": "outside_growing_season"}},
            "drought_monitor": {"composite_score": 1.5, "states": {"IA": {"D2": 25}}},
            "usda": usda,
        },
        "2025-12-01",
        WeatherAgStrategy().get_default_params("30d"),
    )
    assert candidates and all(c.journal_only is False for c in candidates)

    # Report projection has fresh same-session acquired_at and observation='ok', so a prior open USDA incident
    # becomes observation='unknown' (proving 'ok' itself—not staleness—prevents false recovery).
    report, diag = _report_for_sources(tmp_path, usda, session="2025-12-01")
    assert diag == []
    assert report["sources"]["observations"]["usda"] == "ok"
    assert report["sources"]["unresolved"] == []

    from tradingagents.strategies.orchestration.source_incidents import _fresh

    assert _fresh(report) is True

    prior = {
        "generation_id": "gen_006",
        "generation_commit": "a" * 40,
        "session": "2025-11-28",
        "evidence_complete": True,
        "source_health_failures": [],
        "sources": {
            "configuration": "b" * 64,
            "acquired_at": "2025-11-28T23:03:43+00:00",
            "observations": {"usda": "unavailable"},
            "unresolved": [{"provider": "usda", "reason_code": "timeout", "http_status": None}],
        },
    }
    prior["source_incidents"] = assess_source_incidents(prior)
    current_inc = assess_source_incidents(report, prior)
    assert current_inc["history_status"] == "available"
    assert not any(a["kind"] == "recovered" for a in current_inc["alerts"])
    assert current_inc["open_incidents"][0]["provider"] == "usda"
    assert current_inc["open_incidents"][0]["observation"] == "unknown"


def test_usda_all_off_season_still_enforces_current_vintage_and_api_key(monkeypatch):
    """Off-season skip must never bypass historical-vintage rejection or missing API key."""
    monkeypatch.setattr(
        "tradingagents.strategies.data_sources.usda_source.current_session_date",
        lambda: "2026-12-02",
    )
    service = _service_with_usda(USDASource(api_key="test-key"))
    historical = service.fetch_usda_data("2026-12-01")
    assert "error" in historical
    assert historical["coverage"]["complete"] is False
    assert historical["_coverage"]["reason_code"] == "provider_error"

    monkeypatch.setattr(
        "tradingagents.strategies.data_sources.usda_source.current_session_date",
        lambda: "2026-12-01",
    )
    with patch.dict("os.environ", {"USDA_NASS_API_KEY": ""}, clear=False):
        no_key_service = _service_with_usda(USDASource(api_key=""))
        missing_key = no_key_service.fetch_usda_data("2026-12-01")
    assert "error" in missing_key
    assert missing_key["coverage"]["complete"] is False
    assert missing_key["_coverage"]["reason_code"] == "provider_error"


def test_usda_source_has_no_dead_unavailable_attribute():
    """Retired unused _unavailable latch must not remain on USDASource."""
    assert not hasattr(USDASource(api_key="test-key"), "_unavailable")