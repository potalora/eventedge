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
    obb = MagicMock()
    return obb


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
            # importlib.import_module raises for None sentinel
            assert source.is_available() is False

    def test_unknown_method_returns_error(self, source):
        result = source.fetch({"method": "nonexistent_method"})
        assert "error" in result

    def test_datasource_protocol(self, source):
        from tradingagents.autoresearch.data_sources.registry import DataSource
        assert isinstance(source, DataSource)


# ---------------------------------------------------------------------------
# equity_profile
# ---------------------------------------------------------------------------

class TestEquityProfile:
    def test_equity_profile_success(self, source, mock_obb):
        mock_obb.equity.profile.return_value = _make_obbject([{
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "market_cap": 3_000_000_000_000,
            "description": "Apple designs consumer electronics.",
        }])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_profile", "symbol": "AAPL"})

        assert result["symbol"] == "AAPL"
        assert result["name"] == "Apple Inc."
        assert result["sector"] == "Technology"
        assert result["market_cap"] == 3_000_000_000_000
        mock_obb.equity.profile.assert_called_once_with(symbol="AAPL", provider="yfinance")

    def test_equity_profile_missing_symbol(self, source, mock_obb):
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_profile"})
        assert "error" in result

    def test_equity_profile_empty_results(self, source, mock_obb):
        mock_obb.equity.profile.return_value = _make_obbject([])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_profile", "symbol": "AAPL"})
        assert "error" in result

    def test_equity_profile_api_error(self, source, mock_obb):
        mock_obb.equity.profile.side_effect = Exception("API down")
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_profile", "symbol": "AAPL"})
        assert "error" in result


# ---------------------------------------------------------------------------
# equity_estimates
# ---------------------------------------------------------------------------

class TestEquityEstimates:
    def test_equity_estimates_success(self, source, mock_obb):
        mock_obb.equity.estimates.consensus.return_value = _make_obbject([{
            "symbol": "AAPL",
            "target_high": 250.0,
            "target_low": 180.0,
            "target_consensus": 215.0,
            "target_median": 212.0,
        }])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_estimates", "symbol": "AAPL"})

        assert result["symbol"] == "AAPL"
        assert result["target_consensus"] == 215.0
        assert result["target_high"] == 250.0
        mock_obb.equity.estimates.consensus.assert_called_once_with(
            symbol="AAPL", provider="fmp"
        )

    def test_equity_estimates_missing_symbol(self, source, mock_obb):
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_estimates"})
        assert "error" in result

    def test_equity_estimates_api_error(self, source, mock_obb):
        mock_obb.equity.estimates.consensus.side_effect = Exception("timeout")
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_estimates", "symbol": "AAPL"})
        assert "error" in result


# ---------------------------------------------------------------------------
# equity_insider_trading
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
            },
        ])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({
                "method": "equity_insider_trading",
                "symbol": "AAPL",
                "limit": 50,
            })

        assert len(result["trades"]) == 2
        assert result["trades"][0]["owner_name"] == "Tim Cook"
        mock_obb.equity.ownership.insider_trading.assert_called_once_with(
            symbol="AAPL", limit=50, provider="sec"
        )

    def test_insider_trading_missing_symbol(self, source, mock_obb):
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_insider_trading"})
        assert "error" in result

    def test_insider_trading_default_limit(self, source, mock_obb):
        mock_obb.equity.ownership.insider_trading.return_value = _make_obbject([])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_insider_trading", "symbol": "AAPL"})
        # Should use default limit=100
        mock_obb.equity.ownership.insider_trading.assert_called_once_with(
            symbol="AAPL", limit=100, provider="sec"
        )


# ---------------------------------------------------------------------------
# equity_short_interest
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
        }])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({
                "method": "equity_short_interest",
                "symbol": "AAPL",
            })

        assert result["records"][0]["symbol"] == "AAPL"
        assert result["records"][0]["current_short_position"] == 120_000_000
        mock_obb.equity.shorts.short_interest.assert_called_once_with(
            symbol="AAPL", provider="finra"
        )

    def test_short_interest_missing_symbol(self, source, mock_obb):
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_short_interest"})
        assert "error" in result

    def test_short_interest_api_error(self, source, mock_obb):
        mock_obb.equity.shorts.short_interest.side_effect = Exception("FINRA down")
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_short_interest", "symbol": "AAPL"})
        assert "error" in result


# ---------------------------------------------------------------------------
# equity_government_trades
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
            },
        ])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({
                "method": "equity_government_trades",
                "symbol": "AAPL",
            })

        assert len(result["trades"]) == 1
        assert result["trades"][0]["representative"] == "Nancy Pelosi"
        mock_obb.equity.ownership.government_trades.assert_called_once_with(
            symbol="AAPL", chamber="all", limit=100, provider="fmp"
        )

    def test_government_trades_with_chamber(self, source, mock_obb):
        mock_obb.equity.ownership.government_trades.return_value = _make_obbject([])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            source.fetch({
                "method": "equity_government_trades",
                "symbol": "AAPL",
                "chamber": "senate",
            })
        mock_obb.equity.ownership.government_trades.assert_called_once_with(
            symbol="AAPL", chamber="senate", limit=100, provider="fmp"
        )

    def test_government_trades_no_symbol(self, source, mock_obb):
        """Should work without symbol (returns recent trades for all)."""
        mock_obb.equity.ownership.government_trades.return_value = _make_obbject([])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "equity_government_trades"})
        # No error — symbol is optional for government_trades
        assert "trades" in result


# ---------------------------------------------------------------------------
# derivatives_options_unusual
# ---------------------------------------------------------------------------

class TestDerivativesOptionsUnusual:
    def test_options_chains_success(self, source, mock_obb):
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
                "symbol": "AAPL",
            })

        assert len(result["contracts"]) == 1
        assert result["contracts"][0]["strike"] == 200.0
        mock_obb.derivatives.options.chains.assert_called_once_with(
            symbol="AAPL", provider="yfinance"
        )

    def test_options_missing_symbol(self, source, mock_obb):
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "derivatives_options_unusual"})
        assert "error" in result

    def test_options_api_error(self, source, mock_obb):
        mock_obb.derivatives.options.chains.side_effect = Exception("no data")
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({
                "method": "derivatives_options_unusual",
                "symbol": "AAPL",
            })
        assert "error" in result


# ---------------------------------------------------------------------------
# regulators_sec_litigation
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
            },
            {
                "published": "2026-03-18T10:00:00",
                "title": "SEC Charges Company Y",
                "summary": "Insider trading case.",
                "id": "LR-12346",
                "link": "https://sec.gov/litigation/12346",
            },
        ])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "regulators_sec_litigation"})

        assert len(result["releases"]) == 2
        assert result["releases"][0]["title"] == "SEC Charges Company X"
        mock_obb.regulators.sec.rss_litigation.assert_called_once_with(provider="sec")

    def test_sec_litigation_api_error(self, source, mock_obb):
        mock_obb.regulators.sec.rss_litigation.side_effect = Exception("SEC down")
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "regulators_sec_litigation"})
        assert "error" in result


# ---------------------------------------------------------------------------
# factors_fama_french
# ---------------------------------------------------------------------------

class TestFactorsFamaFrench:
    def test_fama_french_success(self, source, mock_obb):
        mock_obb.famafrench.factors.return_value = _make_obbject(
            [
                {"date": "2026-03-01", "mkt_rf": 0.01, "smb": 0.002, "hml": -0.001,
                 "rmw": 0.003, "cma": -0.002, "rf": 0.004},
                {"date": "2026-02-01", "mkt_rf": -0.02, "smb": 0.001, "hml": 0.003,
                 "rmw": -0.001, "cma": 0.001, "rf": 0.004},
            ],
            extra={"results_metadata": {"dataset": "F-F_Research_Data_5_Factors_2x3"}},
        )
        with patch.object(source, "_get_obb", return_value=mock_obb):
            result = source.fetch({"method": "factors_fama_french"})

        assert len(result["factors"]) == 2
        assert result["factors"][0]["mkt_rf"] == 0.01
        mock_obb.famafrench.factors.assert_called_once_with(provider="famafrench")

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
        """Second call with same params should use cache, not call API."""
        mock_obb.equity.profile.return_value = _make_obbject([{
            "symbol": "AAPL", "name": "Apple", "sector": "Tech",
            "industry": "CE", "market_cap": 3e12, "description": "desc",
        }])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            r1 = source.fetch({"method": "equity_profile", "symbol": "AAPL"})
            r2 = source.fetch({"method": "equity_profile", "symbol": "AAPL"})

        assert r1 == r2
        # Only one actual API call
        assert mock_obb.equity.profile.call_count == 1

    def test_cache_different_params(self, source, mock_obb):
        """Different params should not use cache."""
        mock_obb.equity.profile.return_value = _make_obbject([{
            "symbol": "AAPL", "name": "Apple", "sector": "Tech",
            "industry": "CE", "market_cap": 3e12, "description": "desc",
        }])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            source.fetch({"method": "equity_profile", "symbol": "AAPL"})
            source.fetch({"method": "equity_profile", "symbol": "MSFT"})

        assert mock_obb.equity.profile.call_count == 2

    def test_clear_cache(self, source, mock_obb):
        """clear_cache() should invalidate all entries."""
        mock_obb.equity.profile.return_value = _make_obbject([{
            "symbol": "AAPL", "name": "Apple", "sector": "Tech",
            "industry": "CE", "market_cap": 3e12, "description": "desc",
        }])
        with patch.object(source, "_get_obb", return_value=mock_obb):
            source.fetch({"method": "equity_profile", "symbol": "AAPL"})
            source.clear_cache()
            source.fetch({"method": "equity_profile", "symbol": "AAPL"})

        assert mock_obb.equity.profile.call_count == 2


# ---------------------------------------------------------------------------
# Graceful degradation when OpenBB not installed
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    def test_import_without_openbb(self):
        """OpenBBSource should import even if openbb is not installed."""
        # Temporarily pretend openbb doesn't exist
        saved = sys.modules.get("openbb")
        sys.modules["openbb"] = None  # sentinel for missing
        try:
            # Re-import the module
            if "tradingagents.autoresearch.data_sources.openbb_source" in sys.modules:
                importlib.reload(
                    sys.modules["tradingagents.autoresearch.data_sources.openbb_source"]
                )
            else:
                import tradingagents.autoresearch.data_sources.openbb_source  # noqa: F401
            # Should not raise
        finally:
            if saved is not None:
                sys.modules["openbb"] = saved
            else:
                sys.modules.pop("openbb", None)

    def test_fetch_without_openbb(self, source):
        """fetch() should return error if OpenBB cannot be imported."""
        with patch.object(source, "_get_obb", side_effect=ImportError("no openbb")):
            result = source.fetch({"method": "equity_profile", "symbol": "AAPL"})
        assert "error" in result


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_openbb_in_default_registry(self):
        """OpenBBSource should be registered in build_default_registry."""
        from tradingagents.autoresearch.data_sources.registry import build_default_registry
        registry = build_default_registry()
        source = registry.get("openbb")
        assert source is not None
        assert source.name == "openbb"

    def test_openbb_in_exports(self):
        """OpenBBSource should be exported from data_sources package."""
        from tradingagents.autoresearch.data_sources import OpenBBSource
        assert OpenBBSource is not None
