"""Tests for USDA NASS QuickStats data source.

All API calls are mocked — no real requests.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def source():
    from tradingagents.autoresearch.data_sources.usda_source import USDASource
    return USDASource(api_key="test-key-123")


@pytest.fixture()
def source_no_key():
    from tradingagents.autoresearch.data_sources.usda_source import USDASource
    return USDASource(api_key="")


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
        from tradingagents.autoresearch.data_sources.registry import DataSource
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
            result = source.fetch_crop_progress("CORN", 2025)

        assert result == []

    def test_graceful_degradation_on_network_error(self, source):
        import requests as req
        with patch("requests.get", side_effect=req.RequestException("timeout")):
            result = source.fetch_crop_progress("CORN", 2025)

        assert result == []

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
            result = source.fetch_crop_progress("CORN", 2025)

        # Non-numeric values should be skipped gracefully
        assert isinstance(result, list)

    def test_no_key_returns_empty(self, source_no_key):
        result = source_no_key.fetch_crop_progress("CORN", 2025)
        assert result == []


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
