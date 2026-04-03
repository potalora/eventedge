"""Tests for OpenBBSource data source.

All OpenBB calls are mocked — no real API calls.
"""
from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helper: build a mock OBBject (what OpenBB returns from every endpoint)
# ---------------------------------------------------------------------------

def _make_obbject(results: list[dict], extra: dict | None = None):
    """Create a mock OBBject that behaves like openbb_core.app.model.obbject.OBBject."""
    items = [SimpleNamespace(**r) for r in results]
    obj = MagicMock()
    obj.results = items
    obj.extra = extra or {}
    return obj


# ---------------------------------------------------------------------------
# Fixture: build an OpenBBSource with mocked obb
# ---------------------------------------------------------------------------

@pytest.fixture()
def source():
    """Return an OpenBBSource instance (no real OpenBB import)."""
    from tradingagents.autoresearch.data_sources.openbb_source import OpenBBSource
    return OpenBBSource(fmp_api_key="test-fmp-key")


@pytest.fixture()
def mock_obb():
    """Return a deeply mocked obb object matching the SDK structure."""
    return MagicMock()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_name(self, source):
        assert source.name == "openbb"

    def test_requires_api_key(self, source):
        assert source.requires_api_key is False

    def test_is_available_when_openbb_importable(self, source):
        with patch.dict(sys.modules, {"openbb": MagicMock()}):
            assert source.is_available() is True

    def test_is_available_when_openbb_missing(self, source):
        with patch.dict(sys.modules, {"openbb": None}):
            assert source.is_available() is False

    def test_unknown_method_returns_error(self, source):
        result = source.fetch({"method": "nonexistent_method"})
        assert "error" in result

    def test_datasource_protocol(self, source):
        from tradingagents.autoresearch.data_sources.registry import DataSource
        assert isinstance(source, DataSource)


# ---------------------------------------------------------------------------
# equity_profile — normalized keys: sector, industry, market_cap, name, description
# ---------------------------------------------------------------------------

class TestEquityProfile:
    def test_equity_profile_success(self, source, mock_obb):
        mock_obb.equity.profile.return_value = _make_obbject([{
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "market_cap": 3_000_000_000_000,
            "long_business_summary": "Apple designs consumer electronics.",
        }])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_profile", "ticker": "AAPL"})

        assert result["name"] == "Apple Inc."
        assert result["sector"] == "Technology"
        assert result["industry"] == "Consumer Electronics"
        assert result["market_cap"] == 3_000_000_000_000
        assert "Apple designs" in result["description"]
        mock_obb.equity.profile.assert_called_once_with(symbol="AAPL", provider="yfinance")

    def test_equity_profile_missing_ticker(self, source, mock_obb):
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_profile"})
        assert "error" in result

    def test_equity_profile_empty_results(self, source, mock_obb):
        mock_obb.equity.profile.return_value = _make_obbject([])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_profile", "ticker": "AAPL"})
        assert "error" in result

    def test_equity_profile_api_error(self, source, mock_obb):
        mock_obb.equity.profile.side_effect = Exception("API down")
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_profile", "ticker": "AAPL"})
        assert "error" in result


# ---------------------------------------------------------------------------
# equity_estimates — normalized keys: consensus_eps, consensus_revenue,
#   price_target_mean, price_target_high, price_target_low, num_analysts
# ---------------------------------------------------------------------------

class TestEquityEstimates:
    def test_equity_estimates_success(self, source, mock_obb):
        mock_obb.equity.estimates.consensus.return_value = _make_obbject([{
            "symbol": "AAPL",
            "estimated_eps_avg": 7.12,
            "estimated_revenue_avg": 410_000_000_000,
            "target_consensus": 215.0,
            "target_high": 250.0,
            "target_low": 180.0,
            "number_of_analysts": 38,
        }])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_estimates", "ticker": "AAPL"})

        assert result["consensus_eps"] == 7.12
        assert result["consensus_revenue"] == 410_000_000_000
        assert result["price_target_mean"] == 215.0
        assert result["price_target_high"] == 250.0
        assert result["price_target_low"] == 180.0
        assert result["num_analysts"] == 38

    def test_equity_estimates_missing_ticker(self, source, mock_obb):
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_estimates"})
        assert "error" in result

    def test_equity_estimates_api_error(self, source, mock_obb):
        mock_obb.equity.estimates.consensus.side_effect = Exception("timeout")
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_estimates", "ticker": "AAPL"})
        assert "error" in result


# ---------------------------------------------------------------------------
# equity_insider_trading — normalized keys per trade:
#   owner, title, transaction_type, shares, price, value, date, ownership_type
# ---------------------------------------------------------------------------

class TestEquityInsiderTrading:
    def test_insider_trading_success(self, source, mock_obb):
        mock_obb.equity.ownership.insider_trading.return_value = _make_obbject([
            {
                "symbol": "AAPL",
                "filing_date": "2026-03-15",
                "transaction_date": "2026-03-10",
                "owner_name": "Tim Cook",
                "owner_title": "CEO",
                "transaction_type": "S-Sale",
                "securities_transacted": 50000,
                "price": 195.5,
                "owner_type": "officer",
            },
            {
                "symbol": "AAPL",
                "filing_date": "2026-03-12",
                "transaction_date": "2026-03-08",
                "owner_name": "Luca Maestri",
                "owner_title": "CFO",
                "transaction_type": "P-Purchase",
                "securities_transacted": 10000,
                "price": 190.0,
                "owner_type": "officer",
            },
        ])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({
                "method": "equity_insider_trading",
                "ticker": "AAPL",
            })

        assert len(result["trades"]) == 2
        trade = result["trades"][0]
        assert trade["owner"] == "Tim Cook"
        assert trade["title"] == "CEO"
        assert trade["transaction_type"] == "S-Sale"
        assert trade["shares"] == 50000
        assert trade["price"] == 195.5
        assert trade["value"] == 50000 * 195.5
        assert trade["date"] == "2026-03-15"
        assert trade["ownership_type"] == "officer"

    def test_insider_trading_missing_ticker(self, source, mock_obb):
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_insider_trading"})
        assert "error" in result

    def test_insider_trading_empty(self, source, mock_obb):
        mock_obb.equity.ownership.insider_trading.return_value = _make_obbject([])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_insider_trading", "ticker": "AAPL"})
        assert result == {"trades": []}


# ---------------------------------------------------------------------------
# equity_short_interest — normalized flat dict:
#   short_interest, short_pct_of_float, days_to_cover, date
# ---------------------------------------------------------------------------

class TestEquityShortInterest:
    def test_short_interest_success(self, source, mock_obb):
        mock_obb.equity.shorts.short_interest.return_value = _make_obbject([{
            "settlement_date": "2026-03-15",
            "symbol": "AAPL",
            "current_short_position": 120_000_000,
            "previous_short_position": 115_000_000,
            "average_daily_volume": 80_000_000,
            "days_to_cover": 1.5,
            "short_percent_of_float": 0.98,
        }])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({
                "method": "equity_short_interest",
                "ticker": "AAPL",
            })

        assert result["short_interest"] == 120_000_000
        assert result["short_pct_of_float"] == 0.98
        assert result["days_to_cover"] == 1.5
        assert result["date"] == "2026-03-15"
        # Flat dict, not wrapped in records array
        assert "records" not in result

    def test_short_interest_missing_ticker(self, source, mock_obb):
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_short_interest"})
        assert "error" in result

    def test_short_interest_api_error(self, source, mock_obb):
        mock_obb.equity.shorts.short_interest.side_effect = Exception("FINRA down")
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_short_interest", "ticker": "AAPL"})
        assert "error" in result


# ---------------------------------------------------------------------------
# equity_government_trades — normalized keys per trade:
#   ticker, representative, chamber, transaction_type, amount, transaction_date, district
# ---------------------------------------------------------------------------

class TestEquityGovernmentTrades:
    def test_government_trades_success(self, source, mock_obb):
        mock_obb.equity.ownership.government_trades.return_value = _make_obbject([
            {
                "symbol": "AAPL",
                "date": "2026-03-10",
                "transaction_date": "2026-03-05",
                "representative": "Nancy Pelosi",
                "chamber": "house",
                "owner": "Spouse",
                "asset_type": "Stock",
                "amount": "$1,000,001 - $5,000,000",
                "type": "purchase",
                "district": "CA-11",
            },
        ])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_government_trades"})

        assert len(result["trades"]) == 1
        trade = result["trades"][0]
        assert trade["ticker"] == "AAPL"
        assert trade["representative"] == "Nancy Pelosi"
        assert trade["chamber"] == "house"
        assert trade["transaction_type"] == "purchase"
        assert trade["amount"] == "$1,000,001 - $5,000,000"
        assert trade["transaction_date"] == "2026-03-05"
        assert trade["district"] == "CA-11"

    def test_government_trades_empty(self, source, mock_obb):
        mock_obb.equity.ownership.government_trades.return_value = _make_obbject([])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_government_trades"})
        assert result == {"trades": []}

    def test_government_trades_api_error(self, source, mock_obb):
        mock_obb.equity.ownership.government_trades.side_effect = Exception("FMP down")
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_government_trades"})
        assert "error" in result


# ---------------------------------------------------------------------------
# derivatives_options_unusual — normalized keys:
#   unusual: [{ticker, contract_type, strike, expiration, volume, open_interest, vol_oi_ratio}]
# ---------------------------------------------------------------------------

class TestDerivativesOptionsUnusual:
    def test_options_unusual_success(self, source, mock_obb):
        mock_obb.derivatives.options.chains.return_value = _make_obbject([
            {
                "underlying_symbol": "AAPL",
                "contract_symbol": "AAPL260417C00200000",
                "expiration": "2026-04-17",
                "strike": 200.0,
                "option_type": "call",
                "open_interest": 50000,
                "volume": 12000,
                "implied_volatility": 0.32,
            },
        ])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({
                "method": "derivatives_options_unusual",
                "ticker": "AAPL",
            })

        assert len(result["unusual"]) == 1
        item = result["unusual"][0]
        assert item["ticker"] == "AAPL"
        assert item["contract_type"] == "call"
        assert item["strike"] == 200.0
        assert item["volume"] == 12000
        assert item["open_interest"] == 50000
        assert item["vol_oi_ratio"] == round(12000 / 50000, 2)

    def test_options_missing_ticker(self, source, mock_obb):
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "derivatives_options_unusual"})
        assert "error" in result

    def test_options_api_error(self, source, mock_obb):
        mock_obb.derivatives.options.chains.side_effect = Exception("no data")
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({
                "method": "derivatives_options_unusual",
                "ticker": "AAPL",
            })
        assert "error" in result


# ---------------------------------------------------------------------------
# regulators_sec_litigation — normalized keys:
#   releases: [{title, date, url, category}]
# ---------------------------------------------------------------------------

class TestRegulatorsSecLitigation:
    def test_sec_litigation_success(self, source, mock_obb):
        mock_obb.regulators.sec.rss_litigation.return_value = _make_obbject([
            {
                "published": "2026-03-20T12:00:00",
                "title": "SEC Charges Company X",
                "summary": "Fraud complaint filed.",
                "id": "LR-12345",
                "link": "https://sec.gov/litigation/12345",
                "category": "litigation",
            },
        ])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "regulators_sec_litigation"})

        assert len(result["releases"]) == 1
        release = result["releases"][0]
        assert release["title"] == "SEC Charges Company X"
        assert release["date"] == "2026-03-20T12:00:00"
        assert release["url"] == "https://sec.gov/litigation/12345"
        assert release["category"] == "litigation"

    def test_sec_litigation_api_error(self, source, mock_obb):
        mock_obb.regulators.sec.rss_litigation.side_effect = Exception("SEC down")
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "regulators_sec_litigation"})
        assert "error" in result


# ---------------------------------------------------------------------------
# factors_fama_french — normalized keys:
#   factors: {"Mkt-RF": float, "SMB": float, ...}, history: dict
# ---------------------------------------------------------------------------

class TestFactorsFamaFrench:
    def test_fama_french_success(self, source, mock_obb):
        mock_obb.famafrench.factors.return_value = _make_obbject([
            {"date": "2026-02-01", "mkt_rf": -0.02, "smb": 0.001, "hml": 0.003,
             "rmw": -0.001, "cma": 0.001, "rf": 0.004},
            {"date": "2026-03-01", "mkt_rf": 0.01, "smb": 0.002, "hml": -0.001,
             "rmw": 0.003, "cma": -0.002, "rf": 0.004},
        ])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "factors_fama_french"})

        # Latest row extracted with capitalized/hyphenated keys
        assert result["factors"]["Mkt-RF"] == 0.01
        assert result["factors"]["SMB"] == 0.002
        assert result["factors"]["HML"] == -0.001
        assert result["factors"]["RMW"] == 0.003
        assert result["factors"]["CMA"] == -0.002
        assert result["factors"]["RF"] == 0.004
        # History with trailing months
        assert "2026-03-01" in result["history"]
        assert "2026-02-01" in result["history"]

    def test_fama_french_empty(self, source, mock_obb):
        mock_obb.famafrench.factors.return_value = _make_obbject([])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "factors_fama_french"})
        assert result == {"factors": {}, "history": {}}

    def test_fama_french_api_error(self, source, mock_obb):
        mock_obb.famafrench.factors.side_effect = Exception("dataset unavailable")
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "factors_fama_french"})
        assert "error" in result


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------

class TestCache:
    def test_cache_hit(self, source, mock_obb):
        mock_obb.equity.profile.return_value = _make_obbject([{
            "symbol": "AAPL", "name": "Apple", "sector": "Tech",
            "industry": "CE", "market_cap": 3e12,
            "long_business_summary": "desc",
        }])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            r1 = source.fetch({"method": "equity_profile", "ticker": "AAPL"})
            r2 = source.fetch({"method": "equity_profile", "ticker": "AAPL"})

        assert r1 == r2
        assert mock_obb.equity.profile.call_count == 1

    def test_cache_different_params(self, source, mock_obb):
        mock_obb.equity.profile.return_value = _make_obbject([{
            "symbol": "AAPL", "name": "Apple", "sector": "Tech",
            "industry": "CE", "market_cap": 3e12,
            "long_business_summary": "desc",
        }])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            source.fetch({"method": "equity_profile", "ticker": "AAPL"})
            source.fetch({"method": "equity_profile", "ticker": "MSFT"})

        assert mock_obb.equity.profile.call_count == 2

    def test_clear_cache(self, source, mock_obb):
        mock_obb.equity.profile.return_value = _make_obbject([{
            "symbol": "AAPL", "name": "Apple", "sector": "Tech",
            "industry": "CE", "market_cap": 3e12,
            "long_business_summary": "desc",
        }])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            source.fetch({"method": "equity_profile", "ticker": "AAPL"})
            source.clear_cache()
            source.fetch({"method": "equity_profile", "ticker": "AAPL"})

        assert mock_obb.equity.profile.call_count == 2


# ---------------------------------------------------------------------------
# Graceful degradation when OpenBB not installed
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    def test_import_without_openbb(self):
        saved = sys.modules.get("openbb")
        sys.modules["openbb"] = None
        try:
            if "tradingagents.autoresearch.data_sources.openbb_source" in sys.modules:
                importlib.reload(
                    sys.modules["tradingagents.autoresearch.data_sources.openbb_source"]
                )
            else:
                import tradingagents.autoresearch.data_sources.openbb_source  # noqa: F401
        finally:
            if saved is not None:
                sys.modules["openbb"] = saved
            else:
                sys.modules.pop("openbb", None)

    def test_fetch_without_openbb(self, source):
        with patch.object(source, "_get_obb", side_effect=ImportError("no openbb")):
            result = source.fetch({"method": "equity_profile", "ticker": "AAPL"})
        assert "error" in result


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_openbb_in_default_registry(self):
        from tradingagents.autoresearch.data_sources.registry import build_default_registry
        registry = build_default_registry()
        source = registry.get("openbb")
        assert source is not None
        assert source.name == "openbb"

    def test_openbb_in_exports(self):
        from tradingagents.autoresearch.data_sources import OpenBBSource
        assert OpenBBSource is not None
