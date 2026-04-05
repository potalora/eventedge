from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

CAPITOLTRADES_URL = "https://www.capitoltrades.com/trades"

# RSC (React Server Components) request headers
_RSC_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "RSC": "1",
    "Next-Router-State-Tree": (
        "%5B%22%22%2C%7B%22children%22%3A%5B%22(public)%22%2C%7B%22children"
        "%22%3A%5B%22trades%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B"
        "%7D%5D%7D%5D%7D%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
    ),
}


def _extract_trades_from_rsc(text: str) -> list[dict[str, Any]]:
    """Extract trade objects from CapitolTrades RSC flight response.

    The RSC format embeds JSON objects in a streaming text format.
    Trade objects contain ``_issuerId``, ``txDate``, ``txType``, etc.
    """
    trades: list[dict[str, Any]] = []
    for m in re.finditer(r'"_issuerId":\d+', text):
        start = text.rfind("{", max(0, m.start() - 5), m.start())
        if start < 0:
            continue
        # Walk forward counting braces to find matching close
        depth = 0
        end = start
        for i in range(start, min(start + 5000, len(text))):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > start:
            try:
                obj = json.loads(text[start:end])
                if "txDate" in obj:
                    trades.append(obj)
            except json.JSONDecodeError:
                pass
    return trades


def _value_to_bucket(value: int | float) -> str:
    """Convert a numeric dollar amount to a congressional disclosure bucket string.

    Congressional disclosures use standardized dollar range buckets.
    CapitolTrades returns numeric midpoint/max values; map them back.
    """
    _BUCKETS = [
        (1_001, 15_000, "$1,001 - $15,000"),
        (15_001, 50_000, "$15,001 - $50,000"),
        (50_001, 100_000, "$50,001 - $100,000"),
        (100_001, 250_000, "$100,001 - $250,000"),
        (250_001, 500_000, "$250,001 - $500,000"),
        (500_001, 1_000_000, "$500,001 - $1,000,000"),
        (1_000_001, 5_000_000, "$1,000,001 - $5,000,000"),
        (5_000_001, 25_000_000, "$5,000,001 - $25,000,000"),
        (25_000_001, 50_000_000, "$25,000,001 - $50,000,000"),
    ]
    if not value or value < 1_001:
        return "$1,001 - $15,000"
    for low, high, label in _BUCKETS:
        if value <= high:
            return label
    return "$25,000,001 - $50,000,000"


def _normalize_trade(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a CapitolTrades trade object to our standard format."""
    issuer = raw.get("issuer", {}) or {}
    politician = raw.get("politician", {}) or {}

    raw_ticker = issuer.get("issuerTicker") or ""
    # CapitolTrades uses "AAPL:US" format — strip the exchange suffix
    ticker = raw_ticker.split(":")[0] if raw_ticker else ""

    raw_value = raw.get("value", 0)

    return {
        "ticker": ticker,
        "issuer_name": issuer.get("issuerName", ""),
        "sector": issuer.get("sector") or "",
        "transaction_date": raw.get("txDate", ""),
        "transaction_type": raw.get("txType", ""),  # buy, sell, exchange
        "amount": _value_to_bucket(raw_value),
        "amount_raw": raw_value,
        "chamber": raw.get("chamber", ""),
        "representative": f"{politician.get('firstName', '')} {politician.get('lastName', '')}".strip(),
        "party": politician.get("party", ""),
        "state": politician.get("_stateId", ""),
        "pub_date": raw.get("pubDate", ""),
        "owner": raw.get("owner", ""),
        "comment": raw.get("comment", ""),
    }


class CongressSource:
    """Data source for congressional stock trading disclosures.

    Fetches trade data from CapitolTrades.com via their RSC endpoint.
    Results are cached in-memory for the session.
    """

    name: str = "congress"
    requires_api_key: bool = False

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        """Generic dispatcher.

        Supported params["method"] values:
            all_trades, recent_trades, trades_by_ticker
        """
        method = params.get("method", "recent_trades")
        dispatch = {
            "all_trades": self._dispatch_all_trades,
            "recent_trades": self._dispatch_recent_trades,
            "trades_by_ticker": self._dispatch_trades_by_ticker,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except Exception:
            logger.error("CongressSource.fetch(%s) failed", method, exc_info=True)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        """Congress data is available if requests is installed."""
        try:
            import requests  # noqa: F401
            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Public data methods
    # ------------------------------------------------------------------

    def _fetch_page(self, page: int = 1, page_size: int = 96) -> list[dict[str, Any]]:
        """Fetch a single page of trades from CapitolTrades."""
        import requests

        cache_key = f"page|{page}|{page_size}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            resp = requests.get(
                CAPITOLTRADES_URL,
                params={"page": page, "pageSize": page_size},
                headers=_RSC_HEADERS,
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning("CapitolTrades returned %d", resp.status_code)
                return []

            raw_trades = _extract_trades_from_rsc(resp.text)
            trades = [_normalize_trade(t) for t in raw_trades]
            self._cache[cache_key] = trades
            return trades
        except Exception:
            logger.error("Failed to fetch CapitolTrades page %d", page, exc_info=True)
            return []

    def fetch_all_trades(self, max_pages: int = 3) -> list[dict[str, Any]]:
        """Fetch recent trades from CapitolTrades (up to max_pages * 96 trades).

        Args:
            max_pages: Maximum number of pages to fetch (96 trades/page).

        Returns:
            List of normalized trade records.
        """
        if "all_trades" in self._cache:
            return self._cache["all_trades"]

        all_trades: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            trades = self._fetch_page(page=page)
            if not trades:
                break
            all_trades.extend(trades)

        self._cache["all_trades"] = all_trades
        logger.info("Loaded %d congressional trades from CapitolTrades", len(all_trades))
        return all_trades

    def get_recent_trades(self, days_back: int = 30, as_of: str | None = None) -> list[dict[str, Any]]:
        """Filter trades to only those within *days_back* days of *as_of* (or today).

        Args:
            days_back: Number of days to look back from the reference date.
            as_of: Reference date string (YYYY-MM-DD). Defaults to today if None.

        Returns:
            Filtered list of trade records.
        """
        cache_key = f"recent|{days_back}|{as_of}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        ref_date = datetime.strptime(as_of, "%Y-%m-%d") if as_of else datetime.now()
        cutoff = ref_date - timedelta(days=days_back)
        all_trades = self.fetch_all_trades()

        recent: list[dict[str, Any]] = []
        for trade in all_trades:
            trade_date = self._parse_trade_date(trade)
            if trade_date and trade_date >= cutoff:
                recent.append(trade)

        self._cache[cache_key] = recent
        return recent

    def get_trades_by_ticker(self, ticker: str) -> list[dict[str, Any]]:
        """Filter all trades for a specific ticker symbol.

        Args:
            ticker: Stock ticker to filter by.

        Returns:
            List of trade records matching the ticker.
        """
        cache_key = f"ticker|{ticker.upper()}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        all_trades = self.fetch_all_trades()
        ticker_upper = ticker.upper()

        matches: list[dict[str, Any]] = []
        for trade in all_trades:
            trade_ticker = trade.get("ticker", "").upper().strip()
            if trade_ticker == ticker_upper:
                matches.append(trade)

        self._cache[cache_key] = matches
        return matches

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def clear_cache(self) -> None:
        """Clear all cached data."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_trade_date(trade: dict[str, Any]) -> datetime | None:
        """Try to parse the transaction date from a trade record."""
        raw = trade.get("transaction_date", "")
        if not raw:
            return None

        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None

    # ------------------------------------------------------------------
    # Internal dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_all_trades(self, params: dict[str, Any]) -> dict[str, Any]:
        trades = self.fetch_all_trades()
        return {"data": trades, "count": len(trades)}

    def _dispatch_recent_trades(self, params: dict[str, Any]) -> dict[str, Any]:
        days_back = params.get("days_back", 30)
        trades = self.get_recent_trades(days_back=days_back)
        return {"data": trades, "count": len(trades)}

    def _dispatch_trades_by_ticker(self, params: dict[str, Any]) -> dict[str, Any]:
        ticker = params.get("ticker", "")
        trades = self.get_trades_by_ticker(ticker)
        return {"data": trades, "count": len(trades)}
