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

def _getfield(item: Any, field: str, default: Any = None) -> Any:
    """Safely get a field from an OBBject result item."""
    return getattr(item, field, default)


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
        ticker = params.get("ticker") or params.get("symbol")
        if not ticker:
            return {"error": "equity_profile requires 'ticker'"}

        ckey = f"equity_profile|{ticker}"
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.equity.profile(symbol=ticker, provider="yfinance")
        if not resp.results:
            return {"error": f"No profile data for {ticker}"}

        item = resp.results[0]
        result = {
            "sector": _getfield(item, "sector", ""),
            "industry": _getfield(item, "industry", ""),
            "market_cap": _getfield(item, "market_cap", 0),
            "name": _getfield(item, "name", ""),
            "description": str(_getfield(item, "long_business_summary", "") or
                               _getfield(item, "description", ""))[:500],
        }
        self._cache[ckey] = result
        return result

    def _equity_estimates(self, params: dict[str, Any]) -> dict[str, Any]:
        ticker = params.get("ticker") or params.get("symbol")
        if not ticker:
            return {"error": "equity_estimates requires 'ticker'"}

        ckey = f"equity_estimates|{ticker}"
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.equity.estimates.consensus(symbol=ticker, provider="fmp")
        if not resp.results:
            return {"error": f"No estimates for {ticker}"}

        item = resp.results[0]
        result = {
            "consensus_eps": _getfield(item, "estimated_eps_avg")
                             or _getfield(item, "target_consensus"),
            "consensus_revenue": _getfield(item, "estimated_revenue_avg"),
            "price_target_mean": _getfield(item, "target_consensus")
                                 or _getfield(item, "price_target_average"),
            "price_target_high": _getfield(item, "target_high")
                                 or _getfield(item, "price_target_high"),
            "price_target_low": _getfield(item, "target_low")
                                or _getfield(item, "price_target_low"),
            "num_analysts": _getfield(item, "number_of_analysts", 0)
                            or _getfield(item, "target_median", 0),
        }
        self._cache[ckey] = result
        return result

    def _equity_insider_trading(self, params: dict[str, Any]) -> dict[str, Any]:
        ticker = params.get("ticker") or params.get("symbol")
        if not ticker:
            return {"error": "equity_insider_trading requires 'ticker'"}

        ckey = f"equity_insider_trading|{ticker}"
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.equity.ownership.insider_trading(
            symbol=ticker, limit=100, provider="sec"
        )
        trades = []
        for item in resp.results or []:
            shares = _getfield(item, "securities_transacted", 0)
            price = _getfield(item, "price", 0.0)
            trades.append({
                "owner": _getfield(item, "owner_name", ""),
                "title": _getfield(item, "owner_title", ""),
                "transaction_type": _getfield(item, "transaction_type", ""),
                "shares": shares,
                "price": price,
                "value": (shares or 0) * (price or 0),
                "date": str(_getfield(item, "filing_date", "")),
                "ownership_type": _getfield(item, "owner_type", ""),
            })

        result = {"trades": trades}
        self._cache[ckey] = result
        return result

    def _equity_short_interest(self, params: dict[str, Any]) -> dict[str, Any]:
        ticker = params.get("ticker") or params.get("symbol")
        if not ticker:
            return {"error": "equity_short_interest requires 'ticker'"}

        ckey = f"equity_short_interest|{ticker}"
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.equity.shorts.short_interest(symbol=ticker, provider="finra")
        if not resp.results:
            return {"error": f"No short interest data for {ticker}"}

        item = resp.results[0]
        short_pos = _getfield(item, "current_short_position", 0)
        avg_vol = _getfield(item, "average_daily_volume", 1)
        result = {
            "short_interest": short_pos,
            "short_pct_of_float": _getfield(item, "short_percent_of_float", 0.0),
            "days_to_cover": _getfield(item, "days_to_cover", 0.0)
                             or (short_pos / avg_vol if avg_vol else 0.0),
            "date": str(_getfield(item, "settlement_date", "")),
        }
        self._cache[ckey] = result
        return result

    def _equity_government_trades(self, params: dict[str, Any]) -> dict[str, Any]:
        ckey = "equity_government_trades"
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.equity.ownership.government_trades(provider="fmp")
        trades = []
        for item in resp.results or []:
            trades.append({
                "ticker": _getfield(item, "symbol", "")
                          or _getfield(item, "ticker", ""),
                "representative": _getfield(item, "representative", ""),
                "chamber": _getfield(item, "chamber", ""),
                "transaction_type": _getfield(item, "type", "")
                                    or _getfield(item, "transaction_type", ""),
                "amount": _getfield(item, "amount", ""),
                "transaction_date": str(_getfield(item, "transaction_date", "")
                                        or _getfield(item, "date", "")),
                "district": _getfield(item, "district", ""),
            })

        result = {"trades": trades}
        self._cache[ckey] = result
        return result

    def _derivatives_options_unusual(self, params: dict[str, Any]) -> dict[str, Any]:
        ticker = params.get("ticker") or params.get("symbol")
        if not ticker:
            return {"error": "derivatives_options_unusual requires 'ticker'"}

        ckey = f"derivatives_options_unusual|{ticker}"
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.derivatives.options.chains(symbol=ticker, provider="yfinance")
        unusual = []
        for item in resp.results or []:
            volume = _getfield(item, "volume", 0) or 0
            oi = _getfield(item, "open_interest", 0) or 0
            unusual.append({
                "ticker": _getfield(item, "underlying_symbol", ticker),
                "contract_type": _getfield(item, "option_type", ""),
                "strike": _getfield(item, "strike", 0.0),
                "expiration": str(_getfield(item, "expiration", "")),
                "volume": volume,
                "open_interest": oi,
                "vol_oi_ratio": round(volume / max(oi, 1), 2),
            })

        result = {"unusual": unusual}
        self._cache[ckey] = result
        return result

    def _regulators_sec_litigation(self, params: dict[str, Any]) -> dict[str, Any]:
        ckey = "regulators_sec_litigation"
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.regulators.sec.rss_litigation(provider="sec")
        releases = []
        for item in resp.results or []:
            releases.append({
                "title": _getfield(item, "title", ""),
                "date": str(_getfield(item, "published", "")),
                "url": _getfield(item, "link", ""),
                "category": _getfield(item, "category", ""),
            })

        result = {"releases": releases}
        self._cache[ckey] = result
        return result

    def _factors_fama_french(self, params: dict[str, Any]) -> dict[str, Any]:
        model = params.get("model", "5")
        ckey = f"factors_fama_french|{model}"
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.famafrench.factors(provider="famafrench")
        if not resp.results:
            return {"factors": {}, "history": {}}

        # Extract latest row and normalize to spec keys
        latest = resp.results[-1]
        factor_map = {
            "Mkt-RF": "mkt_rf", "SMB": "smb", "HML": "hml",
            "RMW": "rmw", "CMA": "cma", "RF": "rf",
        }
        factors = {}
        for spec_key, obb_key in factor_map.items():
            val = _getfield(latest, obb_key)
            if val is not None:
                factors[spec_key] = float(val)

        # Build trailing 12 months history
        history = {}
        for item in resp.results[-12:]:
            date_str = str(_getfield(item, "date", ""))
            row = {}
            for spec_key, obb_key in factor_map.items():
                val = _getfield(item, obb_key)
                if val is not None:
                    row[spec_key] = float(val)
            if date_str:
                history[date_str] = row

        result = {"factors": factors, "history": history}
        self._cache[ckey] = result
        return result
