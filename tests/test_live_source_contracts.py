"""Offline regressions for observed public provider response shapes."""
from types import SimpleNamespace

import pytest

from tradingagents.strategies.data_sources.congress_source import CongressSource
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.noaa_source import NOAASource
from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.data_sources.usda_source import USDASource


def response(payload):
    return SimpleNamespace(status_code=200, json=lambda: payload)


def disclosure(**changes):
    return dict(symbol="TEST", transactionDate="2026-09-01", disclosureDate="2026-09-27",
                office="Example Senator", type="Purchase", amount="$1,001 - $15,000",
                assetDescription="Example asset", **changes)


@pytest.mark.parametrize("asset_type", ["Other", "Non-Public Stock"])
def test_congress_skips_valid_nontradable_disclosures_without_symbols(monkeypatch, asset_type):
    private = disclosure()
    private.update(symbol="", assetType=asset_type)
    payloads = iter([[disclosure()], [private]])
    monkeypatch.setattr("requests.get", lambda *a, **kw: response(next(payloads)))
    source = CongressSource(fmp_api_key="offline")
    assert [row["ticker"] for row in source.fetch_all_trades()] == ["TEST"]
    assert "fmp_latest" in source._cache


@pytest.mark.parametrize("changes", [
    {"symbol": "", "assetType": "Stock"},
    {"symbol": "", "assetType": ""},
    {"symbol": "", "assetType": "Other", "transactionDate": "invalid"},
    {"symbol": "", "assetType": "Non-Public Stock", "amount": ""},
    {"symbol": "", "assetType": "Other", "office": ""},
    {"symbol": "", "assetType": "Other", "assetDescription": ""},
    {"symbol": None, "assetType": "Other"},
])
def test_congress_missing_public_symbol_or_malformed_private_envelope_fails(monkeypatch, changes):
    row = disclosure()
    row.update(changes)
    monkeypatch.setattr("requests.get", lambda *a, **kw: response([row]))
    source = CongressSource(fmp_api_key="offline")
    with pytest.raises(SourceFetchError) as exc:
        source.fetch_all_trades()
    assert set(exc.value.failed_operations.values()) == {"invalid_response"}
    assert not source._cache


def observation(index):
    return {"date": "2026-10-01T00:00:00", "datatype": "TMAX",
            "station": f"GHCND:TEST{index:05d}", "value": 80}


def test_noaa_completes_observed_iowa_resultset_beyond_five_pages():
    offsets = []
    def get(*args, **kwargs):
        offset = kwargs["params"]["offset"]
        offsets.append(offset)
        rows = [observation(i) for i in range(offset, min(offset + 1000, 15348))]
        return response({"results": rows, "metadata": {"resultset": {
            "count": 15347, "limit": 1000, "offset": offset}}})
    source = NOAASource(token="offline")
    source._session = SimpleNamespace(get=get)
    with provider_budget("noaa", 100, clock=lambda: 0, sleep=lambda _: None, limits=()):
        rows = source.fetch_state_daily("FIPS:19", "2026-10-01", "2026-10-06")
    assert len(rows) == 15347
    assert offsets == list(range(1, 15348, 1000))
    assert source.fetch_state_daily("FIPS:19", "2026-10-01", "2026-10-06") == rows


@pytest.mark.parametrize("failure", ["duplicate", "no_progress", "resource_cap"])
def test_noaa_incomplete_resultsets_are_rejected_without_caching(failure):
    calls = []
    def get(*args, **kwargs):
        calls.append(kwargs["params"]["offset"])
        rows = [observation(1)] if len(calls) == 1 or failure != "no_progress" else []
        return response({"results": rows, "metadata": {"resultset": {
            "count": 100001 if failure == "resource_cap" else 2,
            "offset": calls[-1], "limit": 1000}}})
    source = NOAASource(token="offline")
    source._session = SimpleNamespace(get=get)
    with provider_budget("noaa", 100, clock=lambda: 0, sleep=lambda _: None, limits=()):
        with pytest.raises(SourceFetchError) as exc:
            source.fetch_state_daily("FIPS:19", "2026-10-01", "2026-10-06")
    assert exc.value.reason_code == "invalid_response"
    assert not source._cache
    assert len(calls) <= 2


def test_noaa_pages_share_existing_absolute_deadline():
    clock = [0.0]
    calls = []
    def get(*args, **kwargs):
        calls.append(kwargs["timeout"])
        clock[0] += 6
        return response({"results": [observation(len(calls))],
                         "metadata": {"resultset": {"count": 20}}})
    source = NOAASource(token="offline")
    source._session = SimpleNamespace(get=get)
    with provider_budget("noaa", 10, clock=lambda: clock[0], sleep=lambda _: None, limits=()):
        with pytest.raises(SourceFetchError) as exc:
            source.fetch_state_daily("FIPS:19", "2026-10-01", "2026-10-06")
    assert exc.value.reason_code == "timeout"
    assert len(calls) == 2
    assert len(exc.value.partial_data["observations"]) == 2
    assert not source._cache


def wheat_rows(week="2025-11-16", year=2026):
    return [{"week_ending": week, "year": year, "commodity_desc": "WHEAT",
             "state_alpha": "KS", "class_desc": "WINTER", "unit_desc": unit, "Value": str(value)}
            for unit, value in [("PCT EXCELLENT", 15), ("PCT GOOD", 50), ("PCT FAIR", 25),
                                ("PCT POOR", 8), ("PCT VERY POOR", 2)]]


@pytest.mark.parametrize("reporting_year", [2026, "2026"])
def test_usda_winter_wheat_uses_reporting_year_for_prior_calendar_year(monkeypatch, reporting_year):
    monkeypatch.setattr("requests.get", lambda *a, **kw: response({"data": wheat_rows(year=reporting_year)}))
    rows = USDASource(api_key="offline").fetch_crop_progress("WHEAT", 2026, "KS")
    assert len(rows) == 1
    assert rows[0]["week_ending"] == "2025-11-16"
    assert rows[0]["good_pct"] == 50
    assert rows[0]["crop_class"] == "WINTER"


@pytest.mark.parametrize("failure", ["different_year", "malformed_year", "missing_year", "missing_good", "conflict"])
def test_usda_reporting_year_change_retains_invalid_record_rejection(monkeypatch, failure):
    rows = wheat_rows()
    if failure == "different_year":
        rows = wheat_rows(week="2026-04-05", year=2025)
    elif failure == "malformed_year":
        rows = wheat_rows(week="2026-04-05", year="invalid")
    elif failure == "missing_year":
        for row in rows:
            row.pop("year")
    elif failure == "missing_good":
        rows = [row for row in rows if row["unit_desc"] != "PCT GOOD"]
    elif failure == "conflict":
        rows.append(dict(rows[1], Value="40"))
    monkeypatch.setattr("requests.get", lambda *a, **kw: response({"data": rows}))
    source = USDASource(api_key="offline")
    with pytest.raises(SourceFetchError) as exc:
        source.fetch_crop_progress("WHEAT", 2026, "KS")
    assert exc.value.reason_code == "invalid_response"
    assert not source._cache


def test_noaa_direct_adapter_pages_use_one_default_sixty_second_budget(monkeypatch):
    clock = [0.0]
    calls = []
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr("tradingagents.strategies.data_sources.request_policy.PROVIDER_LIMITS", {})
    def get(*args, **kwargs):
        calls.append(kwargs["timeout"])
        clock[0] += 35
        return response({"results": [observation(len(calls))],
                         "metadata": {"resultset": {"count": 20}}})
    source = NOAASource(token="offline")
    source._session = SimpleNamespace(get=get)
    with pytest.raises(SourceFetchError) as exc:
        source.fetch_state_daily("FIPS:19", "2026-10-01", "2026-10-06")
    assert exc.value.reason_code == "timeout"
    assert len(calls) == 2
    assert not source._cache


def test_congress_malformed_asset_type_is_invalid_response(monkeypatch):
    row = disclosure()
    row.update(symbol="", assetType=["Other"])
    monkeypatch.setattr("requests.get", lambda *a, **kw: response([row]))
    with pytest.raises(SourceFetchError) as exc:
        CongressSource(fmp_api_key="offline").fetch_all_trades()
    assert set(exc.value.failed_operations.values()) == {"invalid_response"}


def test_noaa_direct_summary_uses_one_deadline_for_all_states(monkeypatch):
    clock = [0.0]
    calls = []
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr("tradingagents.strategies.data_sources.request_policy.PROVIDER_LIMITS", {})
    monkeypatch.setattr("tradingagents.strategies.data_sources.noaa_source.AG_STATES",
                        {"IA": "FIPS:19", "IL": "FIPS:17", "KS": "FIPS:20"})
    def get(*args, **kwargs):
        calls.append(kwargs["params"]["locationid"])
        clock[0] += 35
        import pandas as pd
        days = pd.date_range(end="2026-10-06", periods=30)
        rows = [{"date":str(day.date())+"T00:00:00","datatype":dtype,"station":"GHCND:TEST","value":value}
                for day in days for dtype,value in [("TMAX",80),("TMIN",50),("PRCP",.12)]]
        return response({"results":rows, "metadata":{"resultset":{"count":len(rows)}}})
    source = NOAASource(token="offline")
    source._session = SimpleNamespace(get=get)
    with pytest.raises(SourceFetchError) as exc:
        source.fetch_ag_weather_summary("2026-10-06")
    assert exc.value.failed_operations == {"KS": "timeout"}
    assert calls == ["FIPS:19", "FIPS:17"]
    assert exc.value.partial_data["states_reporting"] == 2


@pytest.mark.parametrize("commodity,crop_class,week", [
    ("CORN", "ALL CLASSES", "2025-11-16"),
    ("WHEAT", "SPRING, (EXCL DURUM)", "2025-11-16"),
    ("WHEAT", "SPRING, DURUM", "2025-11-16"),
    ("WHEAT", "WINTER", "2024-11-17"),
    ("CORN", "ALL CLASSES", "2027-04-04"),
    ("WHEAT", "WINTER", "2027-04-04"),
])
def test_usda_matching_reporting_year_does_not_admit_unrelated_observation_years(
    monkeypatch, commodity, crop_class, week
):
    rows = [dict(row, commodity_desc=commodity, class_desc=crop_class)
            for row in wheat_rows(week=week)]
    monkeypatch.setattr("requests.get", lambda *a, **kw: response({"data": rows}))
    source = USDASource(api_key="offline")
    with pytest.raises(SourceFetchError) as exc:
        source.fetch_crop_progress(commodity, 2026, "KS")
    assert exc.value.reason_code == "invalid_response"
    assert not source._cache


def test_usda_future_corn_weeks_cannot_create_twenty_point_decline(monkeypatch):
    rows = []
    for week, good in [("2027-04-04", 70), ("2027-04-11", 50)]:
        for row in wheat_rows(week=week):
            row.update(commodity_desc="CORN", class_desc="ALL CLASSES")
            if row["unit_desc"] == "PCT GOOD":
                row["Value"] = str(good)
            rows.append(row)
    monkeypatch.setattr("requests.get", lambda *a, **kw: response({"data": rows}))
    source = USDASource(api_key="offline")
    with pytest.raises(SourceFetchError) as exc:
        source.fetch_crop_progress("CORN", 2026, "KS")
    assert exc.value.reason_code == "invalid_response"
    assert not source._cache


@pytest.fixture(autouse=True)
def current_noaa_fixture_date(monkeypatch):
    # These are current acquisitions on the fixture date, not vintage replays.
    monkeypatch.setattr("tradingagents.strategies.data_sources.noaa_source.current_session_date",lambda:"2026-10-06")
