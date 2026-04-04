"""FRED (Federal Reserve Economic Data) source.

Provides macro indicators: credit spreads, unemployment, CPI, yield curve,
state-level data, etc. Free API key from fred.stlouisfed.org.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# Common FRED series used by strategies
SERIES_MAP = {
    "hy_spread": "BAMLH0A0HYM2",       # ICE BofA US HY OAS
    "ig_spread": "BAMLC0A4CBBB",        # ICE BofA BBB Corp OAS
    "fed_funds": "FEDFUNDS",             # Fed Funds Rate
    "yield_curve": "T10Y2Y",             # 10Y-2Y spread
    "unemployment": "UNRATE",            # Unemployment Rate
    "cpi": "CPIAUCSL",                   # CPI All Urban
    "payrolls": "PAYEMS",                # Total Nonfarm Payrolls
    "initial_claims": "ICSA",            # Initial Jobless Claims
    "vix": "VIXCLS",                     # VIX (FRED version)
}


class FREDSource:
    """Data source backed by the FRED API via fredapi."""

    name: str = "fred"
    requires_api_key: bool = True

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("FRED_API_KEY", "")
        self._cache: dict[str, Any] = {}

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "series")
        dispatch = {
            "series": self._dispatch_series,
            "multi_series": self._dispatch_multi_series,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except Exception:
            logger.error("FREDSource.fetch(%s) failed", method, exc_info=True)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        if not self._api_key:
            return False
        try:
            from fredapi import Fred  # noqa: F401
            return True
        except ImportError:
            logger.warning("fredapi not installed — run: pip install fredapi")
            return False

    def fetch_series(
        self, series_id: str, start: str, end: str
    ) -> pd.Series:
        """Fetch a single FRED series."""
        cache_key = f"series|{series_id}|{start}|{end}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        from fredapi import Fred

        fred = Fred(api_key=self._api_key)
        try:
            data = fred.get_series(series_id, observation_start=start, observation_end=end)
            self._cache[cache_key] = data
            return data
        except Exception:
            logger.error("Failed to fetch FRED series %s", series_id, exc_info=True)
            return pd.Series(dtype=float)

    def fetch_multi_series(
        self, series_ids: list[str], start: str, end: str
    ) -> dict[str, pd.Series]:
        """Fetch multiple FRED series."""
        results: dict[str, pd.Series] = {}
        for sid in series_ids:
            results[sid] = self.fetch_series(sid, start, end)
        return results

    def fetch_credit_spreads(self, start: str, end: str) -> dict[str, pd.Series]:
        """Fetch HY and IG credit spread data."""
        return self.fetch_multi_series(
            [SERIES_MAP["hy_spread"], SERIES_MAP["ig_spread"]], start, end
        )

    def fetch_economic_indicators(self, start: str, end: str) -> dict[str, pd.Series]:
        """Fetch core economic indicators (unemployment, CPI, payrolls, claims)."""
        ids = [
            SERIES_MAP["unemployment"],
            SERIES_MAP["cpi"],
            SERIES_MAP["payrolls"],
            SERIES_MAP["initial_claims"],
        ]
        return self.fetch_multi_series(ids, start, end)

    def clear_cache(self) -> None:
        self._cache.clear()

    def _dispatch_series(self, params: dict[str, Any]) -> dict[str, Any]:
        series_id = params.get("series_id", "")
        start = params.get("start", "")
        end = params.get("end", "")
        data = self.fetch_series(series_id, start, end)
        return {"data": data.to_dict() if not data.empty else {}}

    def _dispatch_multi_series(self, params: dict[str, Any]) -> dict[str, Any]:
        series_ids = params.get("series_ids", [])
        start = params.get("start", "")
        end = params.get("end", "")
        results = self.fetch_multi_series(series_ids, start, end)
        return {"data": {k: v.to_dict() for k, v in results.items()}}
