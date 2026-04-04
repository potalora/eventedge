"""Tests for US Drought Monitor data source.

All API calls are mocked — no real requests.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def source():
    from tradingagents.autoresearch.data_sources.drought_monitor_source import DroughtMonitorSource
    return DroughtMonitorSource()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_name(self, source):
        assert source.name == "drought_monitor"

    def test_requires_api_key(self, source):
        assert source.requires_api_key is False

    def test_is_available(self, source):
        assert source.is_available() is True

    def test_unknown_method_returns_error(self, source):
        result = source.fetch({"method": "nonexistent"})
        assert "error" in result

    def test_datasource_protocol(self, source):
        from tradingagents.autoresearch.data_sources.registry import DataSource
        assert isinstance(source, DataSource)


# ---------------------------------------------------------------------------
# fetch_drought_severity
# ---------------------------------------------------------------------------

MOCK_DROUGHT_RESPONSE = [
    {
        "MapDate": "20250610",
        "StatisticFormatID": 1,
        "StateAbbreviation": "IA",
        "None": 45.2,
        "D0": 20.1,
        "D1": 15.3,
        "D2": 10.5,
        "D3": 6.2,
        "D4": 2.7,
    },
    {
        "MapDate": "20250610",
        "StatisticFormatID": 1,
        "StateAbbreviation": "IL",
        "None": 60.0,
        "D0": 18.0,
        "D1": 12.0,
        "D2": 7.0,
        "D3": 2.5,
        "D4": 0.5,
    },
]


class TestFetchDroughtSeverity:
    def test_parses_state_drought_data(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_DROUGHT_RESPONSE

        with patch("requests.get", return_value=mock_resp):
            result = source.fetch_drought_severity(["IA", "IL"], "2025-06-03", "2025-06-10")

        assert "IA" in result
        assert result["IA"]["D0"] == 20.1
        assert result["IA"]["D2"] == 10.5
        assert result["IA"]["D4"] == 2.7
        assert "IL" in result

    def test_graceful_degradation_on_failure(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("requests.get", return_value=mock_resp):
            result = source.fetch_drought_severity(["IA"], "2025-06-03", "2025-06-10")

        assert result == {}

    def test_graceful_degradation_on_network_error(self, source):
        import requests as req
        with patch("requests.get", side_effect=req.RequestException("timeout")):
            result = source.fetch_drought_severity(["IA"], "2025-06-03", "2025-06-10")

        assert result == {}


# ---------------------------------------------------------------------------
# fetch_composite_score
# ---------------------------------------------------------------------------

class TestFetchCompositeScore:
    def test_computes_weighted_score(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_DROUGHT_RESPONSE

        with patch("requests.get", return_value=mock_resp):
            score = source.fetch_composite_score(["IA", "IL"], "2025-06-10")

        # IA: (20.1*0 + 15.3*1 + 10.5*2 + 6.2*3 + 2.7*4) / 100 = 0.653
        # IL: (18.0*0 + 12.0*1 + 7.0*2 + 2.5*3 + 0.5*4) / 100 = 0.355
        # Average: (0.653 + 0.355) / 2 = 0.504
        assert 0.4 < score < 0.6

    def test_returns_zero_on_no_data(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []

        with patch("requests.get", return_value=mock_resp):
            score = source.fetch_composite_score(["IA"], "2025-06-10")

        assert score == 0.0

    def test_returns_zero_on_failure(self, source):
        import requests as req
        with patch("requests.get", side_effect=req.RequestException("fail")):
            score = source.fetch_composite_score(["IA"], "2025-06-10")

        assert score == 0.0


# ---------------------------------------------------------------------------
# fetch dispatch
# ---------------------------------------------------------------------------

class TestFetchDispatch:
    def test_dispatch_drought_severity(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_DROUGHT_RESPONSE

        with patch("requests.get", return_value=mock_resp):
            result = source.fetch({
                "method": "drought_severity",
                "states": ["IA", "IL"],
                "start": "2025-06-03",
                "end": "2025-06-10",
            })

        assert "states" in result
        assert "IA" in result["states"]

    def test_dispatch_composite_score(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_DROUGHT_RESPONSE

        with patch("requests.get", return_value=mock_resp):
            result = source.fetch({
                "method": "composite_score",
                "states": ["IA", "IL"],
                "date": "2025-06-10",
            })

        assert "composite_score" in result
        assert isinstance(result["composite_score"], float)
