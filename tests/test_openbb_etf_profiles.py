"""ETF profiles use native issuance evidence, without inventing sector exposure."""
from types import SimpleNamespace as N

import pytest

from test_openbb_native_enrichment import source_with
from test_portfolio_policy import _context, _position, _recommendation
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
from tradingagents.strategies.trading.portfolio_policy import PortfolioPolicy


def etf(symbol="SPY", **changes):
    # Selected fields from the isolated 2026-10-22 native OpenBB/Yahoo probe.
    return N(**(dict(symbol=symbol, name="State Street SPDR S&P 500 ETF Trust",
                    issue_type="ETF", sector=None, industry_category=None,
                    market_cap=None, long_description="Provider description") | changes))


def test_native_etf_classification_bulk_and_scalar_cache_agree():
    calls = []
    def native(**kwargs):
        calls.append(kwargs)
        return N(results=[etf()])
    source = source_with(native)
    result = source.fetch_profiles(["SPY"])
    assert result["errors"] == {}
    profile = result["profiles"]["SPY"]
    assert profile == {
        "asset_type": "ETF", "corporate_sector_status": "not_applicable",
        "lookthrough_sector_status": "unknown", "market_cap": None,
        "name": "State Street SPDR S&P 500 ETF Trust", "description": "Provider description",
    }
    assert source.fetch({"method": "equity_profile", "ticker": "SPY"}) == profile
    assert len(calls) == 1
    # The uncached scalar route applies exactly the same qualification.
    assert source_with(native).fetch({"method": "equity_profile", "ticker": "SPY"}) == profile


@pytest.mark.parametrize("changes", [
    {"issue_type": None}, {"issue_type": "EQUITY"}, {"issue_type": "MUTUALFUND"},
    {"issue_type": "etf"}, {"name": " "}, {"market_cap": float("nan")},
    {"market_cap": float("inf")}, {"market_cap": -1}, {"market_cap": True},
    {"symbol": "OTHER"},
])
def test_etf_exception_never_relaxes_identity_name_metric_or_corporate_guards(changes):
    source = source_with(lambda **_: N(results=[etf(**changes)]))
    bulk = source.fetch_profiles(["SPY"])
    assert bulk["profiles"] == {}
    assert bulk["errors"]["SPY"]["reason_code"] == "invalid_response"
    assert source.fetch({"method": "equity_profile", "ticker": "SPY"})["reason_code"] == "invalid_response"


def test_etf_prompt_explains_unavailable_lookthrough_without_assigning_sector():
    profile = source_with(lambda **_: N(results=[etf()])).fetch_profiles(["SPY"])["profiles"]["SPY"]
    prompt = PortfolioCommittee({})._build_prompt([], {}, {}, [], 5000, {
        "profiles": {"SPY": profile, "AAPL": {"sector": "Technology"}},
    })
    assert "SPY: asset type ETF; corporate sector not applicable; look-through sector unknown" in prompt
    assert "AAPL: Technology" in prompt


def test_etf_remains_in_existing_unknown_correlated_short_risk_group():
    profile = source_with(lambda **_: N(results=[etf()])).fetch_profiles(["SPY"])["profiles"]["SPY"]
    # Match the engine's existing profile-to-risk-context boundary.
    sectors = {"SPY": profile.get("sector", "Unknown")}
    assert sectors == {"SPY": "Unknown"}
    assert PortfolioPolicy().validate(
        _recommendation("SPY", 0.02, direction="short"),
        _context(size="50k", positions=(_position("GLD", .02, direction="short", sector="Unknown"),),
                 pending=(_position("VTI", .02, direction="short", sector="Unknown"),),
                 sectors=sectors, borrow_available={"SPY": True}),
    ) == (False, "max_correlated_shorts")
