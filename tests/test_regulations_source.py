"""Tests for RegulationsSource date filtering.

regulations.gov's server-side `filter[postedDate][ge]` is broken: it pins
results to the boundary date and overrides `sort=-postedDate`, so agency
queries return ~nothing useful. We sort newest-first and filter by date
client-side instead. These tests pin that behavior (all mocked, no network).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from tradingagents.strategies.data_sources.regulations_source import RegulationsSource


def _mock_response(docs):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "data": [
            {
                "id": d["id"],
                "attributes": {
                    "title": d.get("title", ""),
                    "agencyId": d.get("agency", ""),
                    "documentType": "Proposed Rule",
                    "postedDate": d["posted"],
                    "summary": "",
                    "docketId": "",
                    "commentEndDate": "",
                },
            }
            for d in docs
        ]
    }
    return resp


def test_date_filter_is_client_side_and_omits_broken_param():
    src = RegulationsSource(api_key="test")
    docs = [
        {"id": "1", "posted": "2026-05-29T00:00:00Z", "agency": "EPA"},
        {"id": "2", "posted": "2026-05-15T00:00:00Z", "agency": "EPA"},
        {"id": "3", "posted": "2026-04-01T00:00:00Z", "agency": "EPA"},
    ]
    with patch("time.sleep"), patch("requests.get", return_value=_mock_response(docs)) as mget:
        results = src.search_documents(agency_id="EPA", posted_date_from="2026-05-10")

    # Client-side filter keeps only postedDate >= 2026-05-10
    assert [r["document_id"] for r in results] == ["1", "2"]

    # The broken server-side date filter must NOT be sent; sort + agency must be.
    sent = mget.call_args.kwargs["params"]
    assert "filter[postedDate][ge]" not in sent
    assert sent["sort"] == "-postedDate"
    assert sent["filter[agencyId]"] == "EPA"


def test_no_date_filter_returns_all_parsed_docs():
    src = RegulationsSource(api_key="test")
    docs = [
        {"id": "1", "posted": "2026-05-29T00:00:00Z"},
        {"id": "2", "posted": "2026-01-01T00:00:00Z"},
    ]
    with patch("time.sleep"), patch("requests.get", return_value=_mock_response(docs)):
        results = src.search_documents(document_type="Proposed Rule")
    assert len(results) == 2


def test_get_recent_proposed_rules_filters_each_agency_by_date():
    src = RegulationsSource(api_key="test")
    # Two agencies; the API returns a mix of recent and old rules for each.
    docs = [
        {"id": "new", "posted": "2026-05-28T00:00:00Z", "agency": "EPA"},
        {"id": "old", "posted": "2026-01-01T00:00:00Z", "agency": "EPA"},
    ]
    with patch("time.sleep"), patch("requests.get", return_value=_mock_response(docs)):
        rules = src.get_recent_proposed_rules(agencies=["EPA", "SEC"], days_back=14)
    # Only the recent rule survives the client-side date filter (returned for each agency call).
    assert all(r["document_id"] == "new" for r in rules)
    assert len(rules) >= 1
