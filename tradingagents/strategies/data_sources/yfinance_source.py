from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import pandas as pd

from .request_policy import provider_call, provider_timeout
from .fetch_errors import SourceFetchError, source_fetch_error, source_number

logger = logging.getLogger(__name__)


def normalize_ticker(ticker: str) -> str:
    """Normalize ticker for yfinance compatibility (e.g. BRK/B → BRK-B)."""
    return ticker.replace("/", "-")


def normalize_tickers(tickers: list[str]) -> list[str]:
    """Normalize a list of tickers for yfinance compatibility."""
    return [normalize_ticker(t) for t in tickers]


class YFinanceSource:
    """Data source backed by the yfinance library.

    Provides raw price history, ETF returns, VIX data, and earnings dates.
    Execution and ledger code must use ``YFinancePriceSource.get_daily_bars``
    rather than this strategy-screen DataFrame interface.
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
        except SourceFetchError as exc:
            return {**exc.partial_data, "error": str(exc)}
        except Exception:
            logger.error("YFinanceSource.fetch(%s) failed", method)
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
            columns for a single ticker. Failures retain usable partial history.
        """
        cache_key = f"prices|{'_'.join(sorted(tickers))}|{start}|{end}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        import yfinance as yf

        try:
            yf_tickers = normalize_tickers(tickers)
            df = provider_call("yfinance", "history", lambda: yf.download(
                yf_tickers,
                start=start,
                end=end,
                auto_adjust=False,
                progress=False,
                timeout=provider_timeout("yfinance", 30),
            ))
            if not isinstance(df, pd.DataFrame) or df.empty:
                # yfinance also returns an empty frame for swallowed HTTP failures;
                # research history needs explicit usable observations.
                raise SourceFetchError("Yahoo research history unavailable", reason_code="invalid_response")

            # Remap normalized ticker names back to original names so callers
            # can continue using original ticker symbols (e.g. BRK/B).
            if isinstance(df.columns, pd.MultiIndex):
                reverse_map = dict(zip(yf_tickers, tickers))
                new_cols = pd.MultiIndex.from_tuples(
                    [(price_col, reverse_map.get(tk, tk)) for price_col, tk in df.columns],
                    names=df.columns.names,
                )
                df.columns = new_cols
            elif len(tickers) == 1:
                df.columns = pd.MultiIndex.from_product([df.columns, tickers])

            partial_frames, failures = [], {}
            for ticker in tickers:
                try:
                    close = df[("Close", ticker)]
                    usable = close.map(lambda value: source_number(value, minimum=0) and value > 0)
                    if usable.any():
                        partial_frames.append(df.loc[usable, df.columns.get_level_values(1) == ticker])
                    if not usable.any() or (close.notna() & ~usable).any() or not usable.iloc[-1]:
                        raise ValueError("unusable Close")
                except (KeyError, ValueError, TypeError):
                    failures[ticker] = "invalid_response"
            if failures:
                partial = pd.concat(partial_frames, axis=1) if partial_frames else pd.DataFrame()
                raise SourceFetchError("Yahoo symbol history incomplete", reason_code="invalid_response",
                                       failed_operations=failures, partial_data={"prices": partial})
            self._cache[cache_key] = df
            return df
        except Exception as exc:
            raise source_fetch_error("Yahoo research history failed", exc) from None

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
            DataFrame with validated VIX closes; failures remain explicit.
        """
        cache_key = f"vix|{start}|{end}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        import yfinance as yf

        try:
            df = provider_call("yfinance", "history", lambda: yf.download(
                "^VIX",
                start=start,
                end=end,
                auto_adjust=False,
                progress=False,
                timeout=provider_timeout("yfinance", 30),
            ))
            if not isinstance(df, pd.DataFrame) or df.empty:
                raise SourceFetchError("Yahoo VIX history unavailable", reason_code="invalid_response")
            # Flatten MultiIndex if present (single ticker)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if "Close" not in df:
                raise SourceFetchError("Yahoo VIX observations invalid", reason_code="invalid_response")
            usable = df["Close"].map(lambda value: source_number(value, minimum=0) and value > 0)
            if not usable.any() or (df["Close"].notna() & ~usable).any() or not usable.iloc[-1]:
                raise SourceFetchError("Yahoo VIX observations invalid", reason_code="invalid_response",
                                       partial_data={"vix": df.loc[usable]})
            self._cache[cache_key] = df
            return df
        except Exception as exc:
            raise source_fetch_error("Yahoo VIX history failed", exc) from None

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
        failures, statuses = {}, {}
        for ticker in tickers:
            try:
                tk = yf.Ticker(normalize_ticker(ticker))
                cal = provider_call("yfinance", "earnings_dates", lambda: tk.get_earnings_dates(limit=8))
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
            except Exception as exc:
                error = source_fetch_error("Yahoo earnings dates failed", exc)
                failures[ticker] = error.reason_code
                if error.http_status is not None:
                    statuses[ticker] = error.http_status
        if failures:
            raise SourceFetchError("Yahoo earnings coverage incomplete", reason_code="batch_failure",
                                   failed_operations=failures, failed_http_statuses=statuses,
                                   partial_data={"earnings_dates": results})
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
