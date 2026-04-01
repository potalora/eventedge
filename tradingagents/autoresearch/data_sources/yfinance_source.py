from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


class YFinanceSource:
    """Data source backed by the yfinance library.

    Provides price history, ETF returns, VIX data, and earnings dates.
    All results are cached in-memory for the duration of one generation run;
    call ``clear_cache()`` between generations.
    """

    name: str = "yfinance"
    requires_api_key: bool = False

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        """Generic dispatcher.

        Supported params["method"] values:
            prices, etf_returns, vix, earnings_dates
        """
        method = params.get("method", "prices")
        dispatch = {
            "prices": self._dispatch_prices,
            "etf_returns": self._dispatch_etf_returns,
            "vix": self._dispatch_vix,
            "earnings_dates": self._dispatch_earnings_dates,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except Exception:
            logger.error("YFinanceSource.fetch(%s) failed", method, exc_info=True)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        """yfinance is always available (no API key required)."""
        try:
            import yfinance  # noqa: F401
            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Public data methods
    # ------------------------------------------------------------------

    def fetch_prices(
        self,
        tickers: list[str],
        start: str,
        end: str,
    ) -> pd.DataFrame:
        """Bulk-download OHLCV data for *tickers* between *start* and *end*.

        Args:
            tickers: List of ticker symbols.
            start: Start date string (YYYY-MM-DD).
            end: End date string (YYYY-MM-DD).

        Returns:
            DataFrame with MultiIndex columns (Price, Ticker) or single-level
            columns for a single ticker.  Returns empty DataFrame on failure.
        """
        cache_key = f"prices|{'_'.join(sorted(tickers))}|{start}|{end}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        import yfinance as yf

        try:
            df = yf.download(tickers, start=start, end=end, progress=False)
            if df.empty:
                logger.warning("yfinance returned empty DataFrame for %s", tickers)
                return df

            # yfinance quirk: single ticker returns flat columns, not MultiIndex.
            # Normalize to always have (Price, Ticker) MultiIndex.
            if len(tickers) == 1 and not isinstance(df.columns, pd.MultiIndex):
                df.columns = pd.MultiIndex.from_product(
                    [df.columns, tickers]
                )

            # Drop NaN-only rows
            df = df.dropna(how="all")

            self._cache[cache_key] = df
            return df
        except Exception:
            logger.error("fetch_prices failed for %s", tickers, exc_info=True)
            return pd.DataFrame()

    def fetch_etf_returns(
        self,
        etf_map: dict[str, str],
        start: str,
        end: str,
    ) -> dict[str, float]:
        """Compute trailing total returns for a map of ETFs.

        Args:
            etf_map: Mapping of label -> ticker (e.g. {"sp500": "SPY"}).
            start: Start date string.
            end: End date string.

        Returns:
            Dict mapping label to total return over the period (as a decimal).
        """
        cache_key = f"etf_returns|{start}|{end}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        tickers = list(etf_map.values())
        df = self.fetch_prices(tickers, start, end)
        if df.empty:
            return {}

        results: dict[str, float] = {}
        for label, ticker in etf_map.items():
            try:
                close = df["Close"][ticker].dropna()
                if len(close) < 2:
                    continue
                ret = (close.iloc[-1] / close.iloc[0]) - 1.0
                results[label] = float(ret)
            except (KeyError, IndexError):
                logger.warning("Could not compute return for %s (%s)", label, ticker)
        self._cache[cache_key] = results
        return results

    def fetch_vix(self, start: str, end: str) -> pd.DataFrame:
        """Get ^VIX history between *start* and *end*.

        Returns:
            DataFrame with OHLCV columns for VIX, or empty DataFrame.
        """
        cache_key = f"vix|{start}|{end}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        import yfinance as yf

        try:
            df = yf.download("^VIX", start=start, end=end, progress=False)
            # Flatten MultiIndex if present (single ticker)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            self._cache[cache_key] = df
            return df
        except Exception:
            logger.error("fetch_vix failed", exc_info=True)
            return pd.DataFrame()

    def fetch_earnings_dates(
        self, tickers: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Get upcoming/recent earnings dates and surprise data.

        Args:
            tickers: List of ticker symbols.

        Returns:
            Dict mapping ticker to list of earnings records, each with
            keys: date, eps_estimate, reported_eps, surprise_pct.
        """
        cache_key = f"earnings|{'_'.join(sorted(tickers))}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        import yfinance as yf

        results: dict[str, list[dict[str, Any]]] = {}
        for ticker in tickers:
            try:
                tk = yf.Ticker(ticker)
                cal = tk.get_earnings_dates(limit=8)
                if cal is None or cal.empty:
                    results[ticker] = []
                    continue
                records: list[dict[str, Any]] = []
                for idx, row in cal.iterrows():
                    records.append({
                        "date": str(idx.date()) if hasattr(idx, "date") else str(idx),
                        "eps_estimate": _safe_float(row.get("EPS Estimate")),
                        "reported_eps": _safe_float(row.get("Reported EPS")),
                        "surprise_pct": _safe_float(row.get("Surprise(%)")),
                    })
                results[ticker] = records
            except Exception:
                logger.warning("earnings_dates failed for %s", ticker, exc_info=True)
                results[ticker] = []
            # Rate-limit between tickers
            time.sleep(0.15)

        self._cache[cache_key] = results
        return results

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def clear_cache(self) -> None:
        """Clear all cached data. Call between generation runs."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Internal dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_prices(self, params: dict[str, Any]) -> dict[str, Any]:
        tickers = params.get("tickers", [])
        start = params.get("start", "")
        end = params.get("end", "")
        df = self.fetch_prices(tickers, start, end)
        return {"data": df.to_dict() if not df.empty else {}}

    def _dispatch_etf_returns(self, params: dict[str, Any]) -> dict[str, Any]:
        etf_map = params.get("etf_map", {})
        start = params.get("start", "")
        end = params.get("end", "")
        return {"data": self.fetch_etf_returns(etf_map, start, end)}

    def _dispatch_vix(self, params: dict[str, Any]) -> dict[str, Any]:
        start = params.get("start", "")
        end = params.get("end", "")
        df = self.fetch_vix(start, end)
        return {"data": df.to_dict() if not df.empty else {}}

    def _dispatch_earnings_dates(self, params: dict[str, Any]) -> dict[str, Any]:
        tickers = params.get("tickers", [])
        return {"data": self.fetch_earnings_dates(tickers)}


def _safe_float(val: Any) -> float | None:
    """Convert a value to float, returning None if not possible."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
