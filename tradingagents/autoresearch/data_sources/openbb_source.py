"""OpenBB Platform data source.

Provides a unified interface to multiple financial data providers via the
OpenBB SDK: equity profiles, estimates, insider trading, short interest,
government trades, options chains, SEC litigation, and Fama-French factors.

OpenBB is lazily imported on first fetch() call. If not installed, the source
reports unavailable and fetch() returns graceful errors.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Fields to extract from each OBBject result (safe getattr with defaults)
_PROFILE_FIELDS = (
    "symbol", "name", "sector", "industry", "market_cap", "description",
)
_ESTIMATES_FIELDS = (
    "symbol", "target_high", "target_low", "target_consensus", "target_median",
)
_INSIDER_FIELDS = (
    "symbol", "filing_date", "transaction_date", "owner_name", "owner_title",
    "transaction_type", "securities_transacted", "price",
)
_SHORT_FIELDS = (
    "settlement_date", "symbol", "current_short_position",
    "previous_short_position", "average_daily_volume", "days_to_cover",
)
_GOVT_TRADE_FIELDS = (
    "symbol", "date", "transaction_date", "representative", "chamber",
    "owner", "asset_type", "amount", "type", "asset_description",
)
_OPTION_FIELDS = (
    "underlying_symbol", "contract_symbol", "expiration", "strike",
    "option_type", "open_interest", "volume", "implied_volatility",
)
_LITIGATION_FIELDS = ("published", "title", "summary", "id", "link")
_FF_FIELDS = ("date", "mkt_rf", "smb", "hml", "rmw", "cma", "rf")


def _extract(item: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """Safely extract fields from an OBBject result item."""
    return {f: getattr(item, f, None) for f in fields}


class OpenBBSource:
    """Data source backed by the OpenBB Platform SDK.

    Lazily initializes ``from openbb import obb`` on first use.
    Results are cached in-memory per session; call ``clear_cache()`` to reset.
    """

    name: str = "openbb"
    requires_api_key: bool = False

    def __init__(self, fmp_api_key: str | None = None) -> None:
        self._fmp_api_key = fmp_api_key or os.environ.get("FMP_API_KEY", "")
        self._obb: Any | None = None
        self._cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "")
        dispatch = {
            "equity_profile": self._equity_profile,
            "equity_estimates": self._equity_estimates,
            "equity_insider_trading": self._equity_insider_trading,
            "equity_short_interest": self._equity_short_interest,
            "equity_government_trades": self._equity_government_trades,
            "derivatives_options_unusual": self._derivatives_options_unusual,
            "regulators_sec_litigation": self._regulators_sec_litigation,
            "factors_fama_french": self._factors_fama_french,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except ImportError:
            logger.error("OpenBB SDK not installed")
            return {"error": "OpenBB SDK not installed"}
        except Exception:
            logger.error("OpenBBSource.fetch(%s) failed", method, exc_info=True)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        try:
            import importlib
            mod = importlib.import_module("openbb")
            return mod is not None
        except (ImportError, ModuleNotFoundError):
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_obb(self) -> Any:
        """Lazy-init and return the OpenBB ``obb`` singleton."""
        if self._obb is None:
            from openbb import obb  # noqa: WPS433

            # Configure FMP key if provided
            if self._fmp_api_key:
                try:
                    obb.user.credentials.fmp_api_key = self._fmp_api_key
                except Exception:
                    logger.debug("Could not set FMP API key on obb instance")
            self._obb = obb
        return self._obb

    def _cache_key(self, method: str, params: dict[str, Any]) -> str:
        """Build a stable cache key from method + relevant params."""
        # Include only params that affect the API call (exclude 'method')
        parts = [method]
        for k in sorted(params.keys()):
            if k == "method":
                continue
            parts.append(f"{k}={params[k]}")
        return "|".join(parts)

    def clear_cache(self) -> None:
        """Clear the in-memory cache."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Method handlers
    # ------------------------------------------------------------------

    def _equity_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        symbol = params.get("symbol")
        if not symbol:
            return {"error": "equity_profile requires 'symbol'"}

        ckey = self._cache_key("equity_profile", params)
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.equity.profile(symbol=symbol, provider="yfinance")
        if not resp.results:
            return {"error": f"No profile data for {symbol}"}

        result = _extract(resp.results[0], _PROFILE_FIELDS)
        self._cache[ckey] = result
        return result

    def _equity_estimates(self, params: dict[str, Any]) -> dict[str, Any]:
        symbol = params.get("symbol")
        if not symbol:
            return {"error": "equity_estimates requires 'symbol'"}

        ckey = self._cache_key("equity_estimates", params)
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.equity.estimates.consensus(symbol=symbol, provider="fmp")
        if not resp.results:
            return {"error": f"No estimates for {symbol}"}

        result = _extract(resp.results[0], _ESTIMATES_FIELDS)
        self._cache[ckey] = result
        return result

    def _equity_insider_trading(self, params: dict[str, Any]) -> dict[str, Any]:
        symbol = params.get("symbol")
        if not symbol:
            return {"error": "equity_insider_trading requires 'symbol'"}

        limit = params.get("limit", 100)
        ckey = self._cache_key("equity_insider_trading", params)
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.equity.ownership.insider_trading(
            symbol=symbol, limit=limit, provider="sec"
        )
        trades = [_extract(item, _INSIDER_FIELDS) for item in (resp.results or [])]
        result = {"trades": trades}
        self._cache[ckey] = result
        return result

    def _equity_short_interest(self, params: dict[str, Any]) -> dict[str, Any]:
        symbol = params.get("symbol")
        if not symbol:
            return {"error": "equity_short_interest requires 'symbol'"}

        ckey = self._cache_key("equity_short_interest", params)
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.equity.shorts.short_interest(symbol=symbol, provider="finra")
        records = [_extract(item, _SHORT_FIELDS) for item in (resp.results or [])]
        result = {"records": records}
        self._cache[ckey] = result
        return result

    def _equity_government_trades(self, params: dict[str, Any]) -> dict[str, Any]:
        symbol = params.get("symbol")  # optional
        chamber = params.get("chamber", "all")
        limit = params.get("limit", 100)

        ckey = self._cache_key("equity_government_trades", params)
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.equity.ownership.government_trades(
            symbol=symbol, chamber=chamber, limit=limit, provider="fmp"
        )
        trades = [_extract(item, _GOVT_TRADE_FIELDS) for item in (resp.results or [])]
        result = {"trades": trades}
        self._cache[ckey] = result
        return result

    def _derivatives_options_unusual(self, params: dict[str, Any]) -> dict[str, Any]:
        symbol = params.get("symbol")
        if not symbol:
            return {"error": "derivatives_options_unusual requires 'symbol'"}

        ckey = self._cache_key("derivatives_options_unusual", params)
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.derivatives.options.chains(symbol=symbol, provider="yfinance")
        contracts = [_extract(item, _OPTION_FIELDS) for item in (resp.results or [])]
        result = {"contracts": contracts}
        self._cache[ckey] = result
        return result

    def _regulators_sec_litigation(self, params: dict[str, Any]) -> dict[str, Any]:
        ckey = self._cache_key("regulators_sec_litigation", params)
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.regulators.sec.rss_litigation(provider="sec")
        releases = [_extract(item, _LITIGATION_FIELDS) for item in (resp.results or [])]
        result = {"releases": releases}
        self._cache[ckey] = result
        return result

    def _factors_fama_french(self, params: dict[str, Any]) -> dict[str, Any]:
        ckey = self._cache_key("factors_fama_french", params)
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.famafrench.factors(provider="famafrench")
        factors = [_extract(item, _FF_FIELDS) for item in (resp.results or [])]
        result = {"factors": factors}
        if hasattr(resp, "extra") and resp.extra:
            result["metadata"] = resp.extra.get("results_metadata", {})
        self._cache[ckey] = result
        return result
