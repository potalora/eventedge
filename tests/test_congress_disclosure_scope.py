"""Offline regressions for symbol-free bond and LLC disclosure scope."""
from types import SimpleNamespace

import pytest

from tradingagents.strategies.data_sources.congress_source import CongressSource, FMP_FREE_LIMIT
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.registry import DataSourceRegistry
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.orchestration.preflight import run_preflight


def disclosure(**changes):
    row = {"symbol": "", "assetType": "Government Securities",
           "assetDescription": "Example Treasury bill", "office": "Example Representative",
           "transactionDate": "2026-09-18", "disclosureDate": "2026-10-05",
           "type": "Purchase", "amount": "$15,001 - $50,000"}
    row.update(changes)
    return row


def house_disclosures():
    # Preserve the four observed instrument shapes without politician records.
    return [
        disclosure(),
        disclosure(assetType="Other Securities", assetDescription="Example investment LLC"),
        disclosure(assetDescription="Example university municipal bond"),
        disclosure(assetDescription="Example state general obligation bond", type="Sale (Full)"),
        disclosure(symbol="AAPL", assetType="Stock", assetDescription="Apple Inc."),
    ]


def mock_feed(monkeypatch, house, senate=None):
    calls = []
    def request(url, **kwargs):
        calls.append((url, kwargs))
        payload = house if url.endswith("house-latest") else (senate or [])
        return SimpleNamespace(status_code=200, json=lambda: payload)
    monkeypatch.setattr("requests.get", request)
    return calls


def test_current_house_bond_and_llc_shapes_preserve_stock_and_free_tier_calls(monkeypatch):
    senate = [disclosure(assetType="Other", assetDescription="Example partnership"),
              disclosure(assetType="Non-Public Stock", assetDescription="Example private company")]
    calls = mock_feed(monkeypatch, house_disclosures(), senate)
    source = CongressSource(fmp_api_key="offline")
    trades = source.fetch_all_trades()
    assert [(trade["ticker"], trade["chamber"]) for trade in trades] == [("AAPL", "House")]
    assert source.fetch_all_trades() == trades
    assert len(calls) == 2
    assert all(kwargs["params"]["page"] == 0 and kwargs["params"]["limit"] == FMP_FREE_LIMIT
               for _, kwargs in calls)


def test_native_senate_corporate_bonds_preserve_valid_stock_and_disclosure_clock(monkeypatch):
    # Anonymized instrument shapes observed from Senate page zero on 2026-10-09.
    senate = [
        disclosure(assetType="Corporate Bond", assetDescription="Example issuer bond A",
                   transactionDate="2026-09-16", disclosureDate="2026-10-07"),
        disclosure(assetType="Corporate Bond", assetDescription="Example issuer bond B",
                   transactionDate="2026-09-15", disclosureDate="2026-10-07"),
        disclosure(symbol="MSFT", assetType="Stock", assetDescription="Microsoft Corp.",
                   transactionDate="2026-09-18", disclosureDate="2026-10-07"),
    ]
    calls = mock_feed(monkeypatch, [], senate)
    source = CongressSource(fmp_api_key="offline")
    trades = source.fetch_all_trades()
    assert [(trade["ticker"], trade["chamber"]) for trade in trades] == [("MSFT", "Senate")]
    assert trades[0]["transaction_date"] == "2026-09-18"
    assert trades[0]["publication_date"] == "2026-10-07"
    assert trades[0]["representative"] == "Example Representative"
    assert source.get_recent_trades(as_of="2026-10-06") == []
    assert [row["ticker"] for row in source.get_recent_trades(as_of="2026-10-07")] == ["MSFT"]
    assert source.fetch_all_trades() == trades
    assert len(calls) == 2
    assert all(kwargs["params"]["page"] == 0 and kwargs["params"]["limit"] == FMP_FREE_LIMIT
               for _, kwargs in calls)


def test_corporate_bond_missing_symbol_remains_invalid(monkeypatch):
    row = disclosure(assetType="Corporate Bond")
    row.pop("symbol")
    mock_feed(monkeypatch, [], [row])
    source = CongressSource(fmp_api_key="offline")
    with pytest.raises(SourceFetchError) as exc:
        source.fetch_all_trades()
    assert exc.value.failed_operations == {"senate-latest": "invalid_response"}
    assert not source._cache


@pytest.mark.parametrize("asset_type", ["Government Securities", "Other Securities", "Corporate Bond"])
@pytest.mark.parametrize("changes", [
    {"symbol": None}, {"symbol": 0}, {"assetDescription": ""},
    {"transactionDate": "invalid"}, {"disclosureDate": "2026-13-01"},
    {"type": ""}, {"amount": ""}, {"office": ""},
])
def test_scope_exclusions_still_require_valid_disclosure_envelopes(monkeypatch, asset_type, changes):
    house = house_disclosures()
    house[0] = disclosure(assetType=asset_type, **changes)
    mock_feed(monkeypatch, house)
    source = CongressSource(fmp_api_key="offline")
    with pytest.raises(SourceFetchError) as exc:
        source.fetch_all_trades()
    assert exc.value.failed_operations == {"house-latest": "invalid_response"}
    assert [row["ticker"] for row in exc.value.partial_data["recent_trades"]] == ["AAPL"]
    assert not source._cache


@pytest.mark.parametrize("asset_type", ["Stock", "Public Stock", "Unknown", "", None, ["Government Securities"]])
def test_unknown_or_public_assets_without_symbols_remain_visible_failures(monkeypatch, asset_type):
    mock_feed(monkeypatch, [disclosure(assetType=asset_type)])
    source = CongressSource(fmp_api_key="offline")
    with pytest.raises(SourceFetchError) as exc:
        source.fetch_all_trades()
    assert exc.value.failed_operations == {"house-latest": "invalid_response"}
    assert not source._cache


class CongressObserver:
    name = "source_observer"
    track = "paper_trade"
    data_sources = ["congress"]

    def get_default_params(self, horizon="30d"):
        return {}

    def screen(self, data, trading_date, params):
        # Assert that adapter scope filtering survives the native engine path.
        assert [row["ticker"] for row in data["congress"]["recent_trades"]] == ["AAPL"]
        return []


@pytest.mark.parametrize("malformed", [False, True])
def test_disclosure_scope_reaches_native_engine_health_and_preflight(tmp_path, monkeypatch, malformed):
    house = house_disclosures()
    if malformed:
        house[0]["transactionDate"] = "invalid"
    mock_feed(monkeypatch, house)
    source = CongressSource(fmp_api_key="offline")
    registry = DataSourceRegistry()
    registry.register(source)
    config = {"autoresearch": {"state_dir": str(tmp_path)}}
    engine = MultiStrategyEngine(config, registry=registry, strategies=[CongressObserver()], use_llm=False)
    data = engine._fetch_all_data("2026-09-01", "2026-10-07")
    assert [row["ticker"] for row in data["congress"]["recent_trades"]] == ["AAPL"]
    assert bool(data["congress"].get("error")) is malformed
    _, _, health = engine.screen_and_enrich("2026-10-07", data, epoch_id="scope-test", policy_id="30d")
    assert health[0].status == ("data_failure" if malformed else "legitimate_no_event")
    report = run_preflight(config, "2026-10-07", engine=engine)
    assert report["ok"] is (not malformed)
    if malformed:
        assert report["screen_source_failures"][0]["source"] == "congress"
        assert "house-latest:invalid_response" in data["congress"]["error"]
    else:
        assert report["screen_source_failures"] == []
