"""Native-shaped responses at the SDK boundary; no provider/network calls."""
from datetime import date
from types import SimpleNamespace as N

import pytest

from tradingagents.strategies.data_sources.openbb_source import OpenBBSource
from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.orchestration.cohort_orchestrator import CohortOrchestrator


def profile(symbol, **changes):
    fields = dict(symbol=symbol, name=f"{symbol} company", sector="Technology",
                  industry_category="Software", market_cap=100,
                  long_description="Native description")
    return N(**(fields | changes))


def short_row(**changes):
    fields = dict(symbol="AAPL", settlement_date=date(2026, 9, 30),
                  issue_name="Apple", market_class="Q", current_short_position=400,
                  previous_short_position=300, avg_daily_volume=100,
                  days_to_cover=4.0, change=100, change_pct=33.33)
    return N(**(fields | changes))


def source_with(profile_call=None, rows=None):
    source = OpenBBSource(fmp_api_key="offline")
    source._obb = N(equity=N(profile=profile_call,
                            shorts=N(short_interest=lambda **_: N(results=rows))),
                    famafrench=N(factors=lambda **_: N(results=[])))
    return source


def test_profiles_batch_size_order_mapping_and_scalar_cache_reuse():
    calls = []

    def native(**kwargs):
        calls.append(kwargs)
        symbols = kwargs["symbol"].split(",")
        return N(results=[profile(s) for s in reversed(symbols)])

    source = source_with(native)
    tickers = [f"T{i:02}" for i in range(19)]
    result = source.fetch_profiles(tickers + ["T00"])
    assert list(result["profiles"]) == tickers
    assert result["errors"] == {}
    assert calls == [
        {"symbol": "T00,T01,T02,T03,T04,T05,T06,T07", "provider": "yfinance"},
        {"symbol": "T08,T09,T10,T11,T12,T13,T14,T15", "provider": "yfinance"},
        {"symbol": "T16,T17,T18", "provider": "yfinance"},
    ]
    assert result["profiles"]["T00"] == {
        "sector": "Technology", "industry": "Software", "market_cap": 100,
        "name": "T00 company", "description": "Native description",
    }
    assert source.fetch({"method": "equity_profile", "ticker": "T18"})["name"] == "T18 company"
    assert len(calls) == 3


@pytest.mark.parametrize("bad_rows,failed", [
    ([profile("A")], {"B"}),
    ([profile("A"), profile("A"), profile("B")], {"A"}),
    ([profile("A"), profile("A", sector="Finance"), profile("B")], {"A"}),
    ([profile("A"), profile("OTHER")], {"A", "B"}),
    ([profile("A"), profile(None)], {"A", "B"}),
])
def test_profile_response_identity_failures_are_explicit_and_not_cached(bad_rows, failed):
    calls = []

    def native(**kwargs):
        calls.append(kwargs["symbol"])
        return N(results=bad_rows if len(calls) == 1 else [profile(s) for s in kwargs["symbol"].split(",")])

    source = source_with(native)
    result = source.fetch_profiles(["A", "B"])
    assert set(result["errors"]) == failed
    assert set(result["profiles"]) == {"A", "B"} - failed
    assert all("reason_code" in error for error in result["errors"].values())
    retry = source.fetch_profiles(["A", "B"])
    assert set(retry["profiles"]) == {"A", "B"}
    assert calls[1] == ",".join(t for t in ["A", "B"] if t in failed)


def test_profile_batch_inherited_deadline_prevents_later_dispatch():
    clock = [0.0]
    calls = []

    def native(**kwargs):
        calls.append(kwargs["symbol"])
        clock[0] = 11.0
        return N(results=[profile(s) for s in kwargs["symbol"].split(",")])

    source = source_with(native)
    with provider_budget("openbb", 10.0, clock=lambda: clock[0], limits=()):
        result = source.fetch_profiles([f"T{i}" for i in range(9)])
    assert len(calls) == 1
    assert set(result["profiles"]) == {f"T{i}" for i in range(8)}
    assert result["errors"]["T8"]["reason_code"] == "timeout"


def test_profile_sdk_failure_retains_all_symbols_and_safe_reason():
    def native(**_):
        raise ValueError("private URL and credentials must not escape")

    result = source_with(native).fetch_profiles(["A", "B"])
    assert set(result["errors"]) == {"A", "B"}
    assert "private URL" not in str(result)


@pytest.mark.parametrize("rows", [
    [short_row(settlement_date="2021-07-15", current_short_position=100), short_row()],
    [short_row(), short_row(settlement_date="2021-07-15", current_short_position=100)],
])
def test_finra_latest_native_dated_row_is_order_independent(rows):
    result = source_with(rows=rows).fetch({"method": "equity_short_interest", "ticker": "AAPL"})
    assert result["date"] == "2026-09-30"
    assert result["short_interest"] == 400
    assert result["days_to_cover"] == 4.0


def test_finra_native_zero_coverage_is_authoritative():
    result = source_with(rows=[short_row(avg_daily_volume=0, days_to_cover=0)]).fetch(
        {"method": "equity_short_interest", "ticker": "AAPL"})
    assert result["days_to_cover"] == 0


def test_finra_missing_coverage_uses_actual_volume_field():
    result = source_with(rows=[short_row(avg_daily_volume=40, days_to_cover=None)]).fetch(
        {"method": "equity_short_interest", "ticker": "AAPL"})
    assert result["days_to_cover"] == 10


@pytest.mark.parametrize("rows", [
    [short_row(settlement_date=None)], [short_row(settlement_date="bad")],
    [short_row(), short_row(settlement_date="bad")],
    [short_row(symbol="MSFT")],
    [short_row(), short_row(current_short_position=500)],
    [short_row(days_to_cover=-1)], [short_row(days_to_cover=float("nan"))],
    [short_row(days_to_cover=float("inf"))],
    [short_row(days_to_cover=None, avg_daily_volume=0)],
    [short_row(days_to_cover=None, avg_daily_volume=None)],
])
def test_finra_invalid_or_conflicting_response_fails_without_caching(rows):
    source = source_with(rows=rows)
    result = source.fetch({"method": "equity_short_interest", "ticker": "AAPL"})
    assert "error" in result
    source._obb.equity.shorts.short_interest = lambda **_: N(results=[short_row()])
    recovered = source.fetch({"method": "equity_short_interest", "ticker": "AAPL"})
    assert recovered["short_interest"] == 400
    assert recovered["date"] == "2026-09-30"


def test_finra_identical_latest_duplicates_are_accepted():
    result = source_with(rows=[short_row(), short_row()]).fetch(
        {"method": "equity_short_interest", "ticker": "AAPL"})
    assert result["days_to_cover"] == 4


def test_orchestrator_keeps_population_order_and_optional_failure_evidence(monkeypatch):
    calls = []

    def native(**kwargs):
        calls.append(kwargs["symbol"])
        return N(results=[profile(s) for s in reversed(kwargs["symbol"].split(",")) if s != "T03"])

    source = source_with(native, [short_row(symbol="wrong")])
    monkeypatch.setattr(source, "is_available", lambda: True)
    # Keep this orchestration test independent of installed provider versions.
    # The guarded bulk adapter and real-shaped history validation have dedicated tests.
    def short_batch(symbols):
        results = {symbol: source.fetch({"method": "equity_short_interest", "ticker": symbol})
                   for symbol in symbols}
        return {"short_interest": {}, "errors": results,
                "acquisition": {"schema_version": 1, "requested_count": len(symbols),
                                "cached_count": 0, "attempts": [], "population_sha256": "a"*64}}
    monkeypatch.setattr(source, "fetch_short_interest", short_batch)
    owner = object.__new__(CohortOrchestrator)
    owner.cohorts = [{"engine": N(registry={"openbb": source})}]
    signals = [{"ticker": f"T{i:02}"} for i in reversed(range(10))] + [{"ticker": "T00"}]
    result = owner._fetch_openbb_enrichment(signals)
    assert calls == ["T00,T01,T02,T03,T04,T05,T06,T07", "T08,T09"]
    assert list(result["profiles"]) == [f"T{i:02}" for i in range(10) if i != 3]
    assert set(result["errors"]["profiles"]) == {"T03"}
    assert set(result["errors"]["short_interest"]) == {f"T{i:02}" for i in range(10)}
    assert all(error["reason_code"] == "invalid_response"
               for error in result["errors"]["short_interest"].values())


def test_profile_comma_in_symbol_never_expands_native_population():
    calls = []

    def native(**kwargs):
        calls.append(kwargs["symbol"])
        return N(results=[profile("A")])

    result = source_with(native).fetch_profiles(["A", "B,C"])
    assert calls == ["A"]
    assert set(result["profiles"]) == {"A"}
    assert set(result["errors"]) == {"B,C"}


def test_full_866_profile_population_is_accounted_for():
    calls = []

    def native(**kwargs):
        symbols = kwargs["symbol"].split(",")
        calls.append(symbols)
        return N(results=[profile(s) for s in reversed(symbols)])

    tickers = [f"T{i:04}" for i in range(866)]
    result = source_with(native).fetch_profiles(tickers)
    assert list(result["profiles"]) == tickers
    assert result["errors"] == {}
    assert len(calls) == 109
    assert all(len(batch) <= 8 for batch in calls)
    assert [ticker for batch in calls for ticker in batch] == tickers


def test_finra_legacy_volume_alias_only_supplies_explicit_missing_coverage():
    row = short_row(days_to_cover=None)
    del row.avg_daily_volume
    row.average_daily_volume = 80
    result = source_with(rows=[row]).fetch({"method": "equity_short_interest", "ticker": "AAPL"})
    assert result["days_to_cover"] == 5


def test_finra_missing_float_percentage_remains_unknown():
    result = source_with(rows=[short_row()]).fetch(
        {"method": "equity_short_interest", "ticker": "AAPL"})
    assert result["short_pct_of_float"] is None


@pytest.mark.parametrize("percentage", [0, 150.0])
def test_finra_observed_float_percentage_retained_without_artificial_cap(percentage):
    result = source_with(rows=[short_row(short_percent_of_float=percentage)]).fetch(
        {"method": "equity_short_interest", "ticker": "AAPL"})
    assert result["short_pct_of_float"] == percentage


@pytest.mark.parametrize("percentage", [-1, float("nan"), float("inf"), True])
def test_finra_invalid_float_percentage_fails_without_cache(percentage):
    source = source_with(rows=[short_row(short_percent_of_float=percentage)])
    assert "error" in source.fetch({"method": "equity_short_interest", "ticker": "AAPL"})
    source._obb.equity.shorts.short_interest = lambda **_: N(results=[short_row(short_percent_of_float=0)])
    assert source.fetch({"method": "equity_short_interest", "ticker": "AAPL"})["short_pct_of_float"] == 0


def test_finra_latest_unknown_and_observed_zero_are_conflicting_evidence():
    result = source_with(rows=[short_row(), short_row(short_percent_of_float=0)]).fetch(
        {"method": "equity_short_interest", "ticker": "AAPL"})
    assert "error" in result


@pytest.mark.parametrize("percentage,score", [(None, 0.6), (0, 0.6), (6, 0.75)])
def test_supply_chain_unknown_short_percentage_never_invents_squeeze(percentage, score):
    from tradingagents.strategies.modules.supply_chain import SupplyChainStrategy
    data = {"finnhub": {"disruption_news": [{
        "symbol": "AAPL", "id": 1, "headline": "Factory shutdown disrupts supply chain",
        "summary": "Production disrupted", "source": "Offline", "published_at": "2026-10-02T12:00:00Z",
    }]}, "openbb": {"short_interest": {"AAPL": {"short_pct_of_float": percentage}}}}
    result = SupplyChainStrategy().screen(data, "2026-10-02", {})
    assert len(result) == 1
    assert result[0].score == score


def test_optional_enrichment_errors_do_not_enter_committee_prompt():
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    committee = PortfolioCommittee()
    enrichment = {"profiles": {"A": {"sector": "Technology"}}}
    prompt = committee._build_prompt([], {}, {}, [], 5000, enrichment)
    with_errors = enrichment | {"errors": {"profiles": {"B": {"error": "failure diagnostic"}}}}
    assert committee._build_prompt([], {}, {}, [], 5000, with_errors) == prompt
