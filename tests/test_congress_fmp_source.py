from __future__ import annotations

from unittest.mock import MagicMock, patch

from tradingagents.strategies.data_sources.congress_source import (
    FMP_FREE_LIMIT,
    CongressSource,
)


def _response(payload: list[dict], status: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    return response


def test_fmp_latest_fetches_both_chambers_and_normalizes():
    house = [{
        "symbol": "AAPL",
        "disclosureDate": "2026-07-13",
        "transactionDate": "2026-07-08",
        "firstName": "Jane",
        "lastName": "Doe",
        "office": "Jane Doe",
        "district": "CA01",
        "owner": "Self",
        "assetDescription": "Apple Inc.",
        "type": "Purchase",
        "amount": "$15,001 - $50,000",
        "comment": "",
        "link": "https://example.test/house.pdf",
    }]
    senate = [{
        "symbol": "MSFT",
        "disclosureDate": "2026-07-12",
        "transactionDate": "2026-07-07",
        "firstName": "John",
        "lastName": "Smith",
        "assetDescription": "Microsoft Corp.",
        "type": "Sale",
        "amount": "$1,001 - $15,000",
    }]
    source = CongressSource(fmp_api_key="test-key")

    with patch("requests.get", side_effect=[_response(house), _response(senate)]) as get:
        trades = source.fetch_all_trades()

    assert get.call_count == 2
    assert all(call.kwargs["params"]["limit"] == FMP_FREE_LIMIT for call in get.call_args_list)
    assert all(call.kwargs["params"]["page"] == 0 for call in get.call_args_list)
    assert trades[0]["ticker"] == "AAPL"
    assert trades[0]["chamber"] == "House"
    assert trades[0]["transaction_type"] == "Purchase"
    assert trades[1]["ticker"] == "MSFT"
    assert trades[1]["chamber"] == "Senate"
    assert trades[1]["representative"] == "John Smith"


def test_fmp_results_are_cached_without_more_api_calls():
    source = CongressSource(fmp_api_key="test-key")
    payload = [{"symbol": "AAPL", "transactionDate": "2026-07-08"}]

    with patch("requests.get", side_effect=[_response(payload), _response([])]) as get:
        first = source.fetch_all_trades()
        second = source.fetch_all_trades()

    assert first == second
    assert get.call_count == 2


def test_recent_trades_excludes_records_after_as_of_date():
    source = CongressSource()
    source._cache["all_trades"] = [
        {"ticker": "PAST", "transaction_date": "2026-03-15"},
        {"ticker": "FUTURE", "transaction_date": "2026-04-05"},
    ]

    recent = source.get_recent_trades(days_back=30, as_of="2026-04-03")

    assert [trade["ticker"] for trade in recent] == ["PAST"]
