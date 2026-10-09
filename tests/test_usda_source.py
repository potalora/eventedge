"""Tests for USDA NASS QuickStats data source.

All API calls are mocked — no real requests.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError


@pytest.fixture()
def source():
    from tradingagents.strategies.data_sources.usda_source import USDASource
    return USDASource(api_key="test-key-123")


@pytest.fixture()
def source_no_key():
    from tradingagents.strategies.data_sources.usda_source import USDASource
    # Patch environment so the source cannot fall back to a real key
    with patch.dict("os.environ", {"USDA_NASS_API_KEY": ""}, clear=False):
        yield USDASource(api_key="")


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_name(self, source):
        assert source.name == "usda"

    def test_requires_api_key(self, source):
        assert source.requires_api_key is True

    def test_is_available_with_key(self, source):
        assert source.is_available() is True

    def test_is_available_without_key(self, source_no_key):
        assert source_no_key.is_available() is False

    def test_unknown_method_returns_error(self, source):
        result = source.fetch({"method": "nonexistent"})
        assert "error" in result

    def test_datasource_protocol(self, source):
        from tradingagents.strategies.data_sources.registry import DataSource
        assert isinstance(source, DataSource)


# ---------------------------------------------------------------------------
# fetch_crop_progress
# ---------------------------------------------------------------------------

MOCK_NASS_RESPONSE = {
    "data": [
        {
            "commodity_desc": "CORN",
            "state_alpha": "IA",
            "week_ending": "2025-06-15",
            "unit_desc": "PCT EXCELLENT",
            "Value": "21",
        },
        {
            "commodity_desc": "CORN",
            "state_alpha": "IA",
            "week_ending": "2025-06-15",
            "unit_desc": "PCT GOOD",
            "Value": "44",
        },
        {
            "commodity_desc": "CORN",
            "state_alpha": "IA",
            "week_ending": "2025-06-15",
            "unit_desc": "PCT FAIR",
            "Value": "22",
        },
        {
            "commodity_desc": "CORN",
            "state_alpha": "IA",
            "week_ending": "2025-06-15",
            "unit_desc": "PCT POOR",
            "Value": "9",
        },
        {
            "commodity_desc": "CORN",
            "state_alpha": "IA",
            "week_ending": "2025-06-15",
            "unit_desc": "PCT VERY POOR",
            "Value": "4",
        },
    ]
}


class TestFetchCropProgress:
    def test_parses_condition_ratings(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_NASS_RESPONSE

        with patch("requests.get", return_value=mock_resp) as mock_get:
            result = source.fetch_crop_progress("CORN", 2025)

        assert len(result) == 1
        week = result[0]
        assert week["commodity"] == "CORN"
        assert week["state"] == "IA"
        assert week["week_ending"] == "2025-06-15"
        assert week["excellent_pct"] == 21
        assert week["good_pct"] == 44
        assert week["fair_pct"] == 22
        assert week["poor_pct"] == 9
        assert week["very_poor_pct"] == 4

        # Verify API called with correct params
        call_kwargs = mock_get.call_args
        assert call_kwargs[1]["params"]["commodity_desc"] == "CORN"
        assert call_kwargs[1]["params"]["key"] == "test-key-123"

    def test_caches_by_commodity_year(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_NASS_RESPONSE

        with patch("requests.get", return_value=mock_resp) as mock_get:
            result1 = source.fetch_crop_progress("CORN", 2025)
            result2 = source.fetch_crop_progress("CORN", 2025)

        assert mock_get.call_count == 1
        assert result1 == result2

    def test_different_commodity_not_cached(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_NASS_RESPONSE

        with patch("requests.get", return_value=mock_resp) as mock_get:
            source.fetch_crop_progress("CORN", 2025)
            source.fetch_crop_progress("SOYBEANS", 2025)

        assert mock_get.call_count == 2

    def test_graceful_degradation_on_api_failure(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("requests.get", return_value=mock_resp):
            with pytest.raises(SourceFetchError):
                source.fetch_crop_progress("CORN", 2025)

    def test_graceful_degradation_on_network_error(self, source):
        import requests as req
        with patch("requests.get", side_effect=req.RequestException("timeout")):
            with pytest.raises(SourceFetchError):
                source.fetch_crop_progress("CORN", 2025)

    def test_handles_missing_value_field(self, source):
        response = {"data": [{
            "commodity_desc": "CORN",
            "state_alpha": "IA",
            "week_ending": "2025-06-15",
            "unit_desc": "PCT EXCELLENT",
            "Value": " (D)",
        }]}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = response

        with patch("requests.get", return_value=mock_resp):
            with pytest.raises(SourceFetchError) as exc:
                source.fetch_crop_progress("CORN", 2025)
            result = exc.value.partial_data["crop_progress"]["CORN"]

        # Non-numeric values should be skipped gracefully
        assert isinstance(result, list)

    def test_no_key_returns_empty(self, source_no_key):
        with pytest.raises(SourceFetchError):
            source_no_key.fetch_crop_progress("CORN", 2025)


# ---------------------------------------------------------------------------
# fetch dispatch
# ---------------------------------------------------------------------------

class TestFetchDispatch:
    def test_dispatch_crop_progress(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_NASS_RESPONSE

        with patch("requests.get", return_value=mock_resp):
            result = source.fetch({
                "method": "crop_progress",
                "commodity": "CORN",
                "year": 2025,
            })

        assert "weeks" in result
        assert len(result["weeks"]) == 1


class TestIncidentRegression:
    def test_request_uses_repeated_states_and_condition_units(self, source):
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": MOCK_NASS_RESPONSE["data"] + [dict(row, state_alpha="IL") for row in MOCK_NASS_RESPONSE["data"]]}
        with patch("requests.get", return_value=response) as get:
            source.fetch_crop_progress("CORN", 2025, "IA,IL")
        params = get.call_args.kwargs["params"]
        assert params["state_alpha"] == ["IA", "IL"]
        assert "unit_desc" not in params
        assert params["agg_level_desc"] == "STATE"

    def test_cache_respects_requested_states(self, source):
        def request(url, **kwargs):
            response = MagicMock(status_code=200)
            state = kwargs["params"]["state_alpha"][0]
            response.json.return_value = {"data": [dict(row, state_alpha=state, week_ending="2026-06-15") for row in MOCK_NASS_RESPONSE["data"]]}
            return response
        with patch("requests.get", side_effect=request) as get:
            source.fetch_crop_progress("CORN", 2026, "IA")
            source.fetch_crop_progress("CORN", 2026, "IL")
        assert get.call_count == 2

    def test_primary_failure_still_falls_back_for_next_crop(self, source):
        import requests
        with patch("requests.get", side_effect=requests.Timeout("offline")), patch.object(source, "_esmis_fallback", return_value=[{"state": "IA"}]) as fallback:
            with pytest.raises(SourceFetchError) as exc:
                source.fetch_crop_progress("SOYBEANS", 2026)
        assert exc.value.partial_data["crop_progress"]["SOYBEANS"] == [{"state": "IA"}]
        fallback.assert_called_once()

    def test_esmis_singular_soybean_and_default_state_scope(self, source):
        report = "Soybean Condition - Selected States: Week Ending September 13, 2026\nIowa ....: 1 4 20 50 25\nTexas ...: 5 10 30 40 15\n"
        with patch.object(source, "_esmis_fetch_latest_report", return_value=report):
            rows = source._esmis_fallback("SOYBEANS", 2026, None)
        assert [row["state"] for row in rows] == ["IA"]
        assert rows[0]["week_ending"] == "2026-09-13"

    def test_esmis_does_not_substitute_another_year(self, source):
        report = "Corn Condition - Selected States: Week Ending September 13, 2026\nIowa ....: 1 4 20 50 25\n"
        with patch.object(source, "_esmis_fetch_latest_report", return_value=report):
            assert source._esmis_fallback("CORN", 2025, "IA") == []

    def test_primary_preserves_wheat_class_identity(self, source):
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": [
            dict(MOCK_NASS_RESPONSE["data"][0], class_desc="WINTER", Value="10"),
            dict(MOCK_NASS_RESPONSE["data"][0], class_desc="SPRING", Value="30"),
            dict(MOCK_NASS_RESPONSE["data"][1], class_desc="WINTER", Value="40"),
            dict(MOCK_NASS_RESPONSE["data"][1], class_desc="SPRING", Value="40"),
        ]}
        with patch("requests.get", return_value=response):
            rows = source.fetch_crop_progress("WHEAT", 2025, "IA")
        assert {(r["crop_class"], r["excellent_pct"]) for r in rows} == {("WINTER", 10), ("SPRING", 30)}

    @pytest.mark.parametrize("latest_good", [None, "(D)", "not-a-number", "-1", "101"])
    def test_missing_or_invalid_good_cannot_manufacture_decline(self, source, latest_good):
        from tradingagents.strategies.modules.weather_ag import WeatherAgStrategy

        def row(week, unit, value):
            return dict(MOCK_NASS_RESPONSE["data"][0], week_ending=week, unit_desc=unit, Value=value)

        rows = [row("2026-09-13", "PCT GOOD", "60"), row("2026-09-13", "PCT EXCELLENT", "20"),
                row("2026-09-20", "PCT EXCELLENT", "20")]
        if latest_good is not None:
            rows.append(row("2026-09-20", "PCT GOOD", latest_good))
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": rows}
        with patch("requests.get", return_value=response):
            with pytest.raises(SourceFetchError) as exc:
                source.fetch_crop_progress("CORN", 2026, "IA")
            observations = exc.value.partial_data["crop_progress"]["CORN"]
        assert len(observations) == 2
        assert observations[-1]["condition_valid"] is False
        assert "good_pct" not in observations[-1]
        assert WeatherAgStrategy._check_crop_decline({"crop_progress": {"CORN": observations}}) == 0

    @pytest.mark.parametrize("values", [("60", "10"), ("10", "60")])
    def test_conflicting_duplicate_category_cannot_manufacture_decline(self, source, values):
        from tradingagents.strategies.modules.weather_ag import WeatherAgStrategy

        rows = []
        for week, unit, value in [
            ("2026-09-13", "PCT GOOD", "60"), ("2026-09-13", "PCT EXCELLENT", "20"),
            ("2026-09-20", "PCT EXCELLENT", "20"),
            ("2026-09-20", "PCT GOOD", values[0]), ("2026-09-20", "PCT GOOD", values[1]),
        ]:
            rows.append(dict(MOCK_NASS_RESPONSE["data"][0], week_ending=week, unit_desc=unit, Value=value))
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": rows}
        with patch("requests.get", return_value=response):
            with pytest.raises(SourceFetchError) as exc:
                source.fetch_crop_progress("CORN", 2026, "IA")
            observations = exc.value.partial_data["crop_progress"]["CORN"]
        assert len(observations) == 2
        assert observations[-1]["condition_valid"] is False
        assert "good_pct" not in observations[-1]
        assert WeatherAgStrategy._check_crop_decline({"crop_progress": {"CORN": observations}}) == 0

    @pytest.mark.parametrize("invalid_latest", ["missing", "conflict", "invalid_only"])
    def test_invalid_latest_week_does_not_reuse_older_decline(self, source, invalid_latest):
        from tradingagents.strategies.modules.weather_ag import WeatherAgStrategy

        observations = [
            ("2026-09-06", "PCT GOOD", "80"), ("2026-09-06", "PCT EXCELLENT", "10"),
            ("2026-09-13", "PCT GOOD", "60"), ("2026-09-13", "PCT EXCELLENT", "10"),
        ]
        if invalid_latest == "invalid_only":
            observations.append(("2026-09-20", "PCT GOOD", "(D)"))
        else:
            observations.append(("2026-09-20", "PCT EXCELLENT", "10"))
        if invalid_latest == "conflict":
            observations.extend([("2026-09-20", "PCT GOOD", "20"), ("2026-09-20", "PCT GOOD", "30")])
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": [
            dict(MOCK_NASS_RESPONSE["data"][0], week_ending=week, unit_desc=unit, Value=value)
            for week, unit, value in observations
        ]}
        with patch("requests.get", return_value=response):
            with pytest.raises(SourceFetchError) as exc:
                source.fetch_crop_progress("CORN", 2026, "IA")
            rows = exc.value.partial_data["crop_progress"]["CORN"]
        assert WeatherAgStrategy._check_crop_decline({"crop_progress": {"CORN": rows}}) == 0
        assert rows[-1]["week_ending"] == "2026-09-20"
        assert rows[-1]["condition_valid"] is False
        assert "good_pct" not in rows[-1]
        assert "excellent_pct" not in rows[-1]


@pytest.fixture(autouse=True)
def offline_request_policy(monkeypatch):
    monkeypatch.setattr('tradingagents.strategies.data_sources.request_policy.PROVIDER_LIMITS', {})
    monkeypatch.setattr('time.sleep', lambda _: None)
    monkeypatch.setattr('tradingagents.strategies.data_sources.usda_source.AG_STATES', 'IA')
