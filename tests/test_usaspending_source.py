"""Tests for the USASpending data source.

All API calls are mocked — no real requests.

Regression focus: USASpending returns ``Last Modified Date`` as naive
datetime strings (e.g. ``"2026-08-05 10:11:25"``). Passing those through
to candidate metadata made event-identity staging raise
``candidate timestamp last_modified_date requires timezone awareness``,
which aborted all 16 cohorts of every active generation from
2026-08-03 through 2026-08-06. The source must normalize award dates to
date-only ISO strings at the boundary.
"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def source():
    from tradingagents.strategies.data_sources.usaspending_source import (
        USASpendingSource,
    )

    return USASpendingSource()


def _api_response(rows):
    """Build a mocked spending_by_award response with raw API field names."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"results": rows}
    return mock_resp


# Raw shapes observed from the live API (2026-08-06): naive datetime strings.
API_ROW = {
    "Award ID": "AWARD-1",
    "Recipient Name": "LOCKHEED MARTIN CORP",
    "Award Amount": 250_000_000,
    "Awarding Agency": "DEFENSE, DEPARTMENT OF",
    "Start Date": "2026-07-01 00:00:00",
    "Last Modified Date": "2026-07-07 17:57:06",
    "Description": "test award",
}


# ---------------------------------------------------------------------------
# _normalize_award_date
# ---------------------------------------------------------------------------


class TestNormalizeAwardDate:
    @pytest.fixture()
    def normalize(self):
        from tradingagents.strategies.data_sources.usaspending_source import (
            _normalize_award_date,
        )

        return _normalize_award_date

    def test_naive_datetime_string_space_separator(self, normalize):
        assert normalize("2026-08-05 10:11:25") == "2026-08-05"

    def test_iso_datetime_string_t_separator(self, normalize):
        assert normalize("2026-08-05T10:11:25") == "2026-08-05"

    def test_date_only_string_unchanged(self, normalize):
        assert normalize("2026-08-05") == "2026-08-05"

    def test_datetime_object(self, normalize):
        assert normalize(datetime(2026, 8, 5, 10, 11, 25)) == "2026-08-05"

    def test_date_object(self, normalize):
        assert normalize(date(2026, 8, 5)) == "2026-08-05"

    def test_empty_string(self, normalize):
        assert normalize("") == ""

    def test_unparseable_string(self, normalize):
        assert normalize("08/05/2026") == ""

    def test_none(self, normalize):
        assert normalize(None) == ""

    def test_whitespace_padded(self, normalize):
        assert normalize("  2026-08-05 10:11:25  ") == "2026-08-05"


# ---------------------------------------------------------------------------
# search_contracts normalization
# ---------------------------------------------------------------------------


class TestSearchContractsNormalization:
    def test_naive_api_timestamps_normalized(self, source):
        with patch("requests.post", return_value=_api_response([API_ROW])):
            results = source.search_contracts(min_amount=10_000_000)

        assert len(results) == 1
        contract = results[0]
        assert contract["last_modified_date"] == "2026-07-07"
        assert contract["start_date"] == "2026-07-01"
        assert contract["award_id"] == "AWARD-1"
        assert contract["recipient_name"] == "LOCKHEED MARTIN CORP"

    def test_missing_date_fields_become_empty(self, source):
        row = {"Award ID": "AWARD-2", "Recipient Name": "BOEING CO"}
        with patch("requests.post", return_value=_api_response([row])):
            results = source.search_contracts()

        assert results[0]["last_modified_date"] == ""
        assert results[0]["start_date"] == ""

    def test_recent_large_contracts_normalized(self, source):
        with patch("requests.post", return_value=_api_response([API_ROW])):
            results = source.get_recent_large_contracts(
                min_amount=50_000_000, days_back=30, as_of="2026-08-06"
            )

        assert results[0]["last_modified_date"] == "2026-07-07"


# ---------------------------------------------------------------------------
# End-to-end regression: screen -> event-identity staging
# ---------------------------------------------------------------------------


class TestGovtContractsStagingRegression:
    """Reproduces the 2026-08-03..06 outage end to end (mocked HTTP)."""

    def test_screen_candidates_pass_event_identity_staging(self, source):
        from tradingagents.strategies.modules.govt_contracts import (
            GovtContractsStrategy,
        )
        from tradingagents.strategies.orchestration.event_identity import (
            canonical_event_key,
            canonical_observation_time,
        )

        with patch("requests.post", return_value=_api_response([API_ROW])):
            contracts = source.get_recent_large_contracts(
                min_amount=50_000_000, days_back=30, as_of="2026-08-06"
            )

        strategy = GovtContractsStrategy()
        candidates = strategy.screen(
            {"usaspending": {"data": {"contracts": contracts}}},
            "2026-08-06",
            strategy.get_default_params(),
        )
        assert candidates, "expected at least one govt_contracts candidate"

        for candidate in candidates:
            metadata = candidate.metadata
            observed_at = canonical_observation_time("govt_contracts", metadata)
            assert observed_at.tzinfo is not None
            # Identity keying must still resolve for dedupe.
            assert canonical_event_key(
                "govt_contracts", candidate.ticker, metadata, date(2026, 8, 6)
            )

    def test_raw_naive_timestamp_would_fail_staging(self):
        """Guard: confirms the strict validator still rejects naive values,
        i.e. the fix works because the source normalizes, not because the
        validator was loosened."""
        from tradingagents.strategies.orchestration.event_identity import (
            canonical_observation_time,
        )

        with pytest.raises(ValueError, match="timezone awareness"):
            canonical_observation_time(
                "govt_contracts",
                {
                    "source": "usaspending",
                    "award_id": "AWARD-1",
                    "last_modified_date": "2026-07-07 17:57:06",
                },
            )
