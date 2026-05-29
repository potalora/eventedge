"""Adapter turning YFinanceSource into the engine's price_fn callable."""
from __future__ import annotations

import logging
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)


def yfinance_price_fn(source: Any | None = None) -> Callable[[str, str, str], dict[str, float]]:
    """Build a price_fn(ticker, start, end) -> {date: close} backed by YFinanceSource.

    The returned callable closes over one source instance so its in-memory cache
    is shared across all tickers in a single event study run.
    """
    if source is None:
        from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource

        source = YFinanceSource()

    def price_fn(ticker: str, start: str, end: str) -> dict[str, float]:
        df = source.fetch_prices([ticker], start, end)
        if df is None or df.empty:
            return {}
        try:
            close = df["Close"][ticker]
        except (KeyError, TypeError):
            logger.warning("No Close column for %s", ticker)
            return {}
        close = close.dropna()
        out: dict[str, float] = {}
        for ts, val in close.items():
            key = ts.strftime("%Y-%m-%d") if isinstance(ts, pd.Timestamp) else str(ts)[:10]
            out[key] = float(val)
        return out

    return price_fn
