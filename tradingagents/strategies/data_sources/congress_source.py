from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .request_policy import provider_request
from .fetch_errors import SourceFetchError, source_fetch_error, source_text, source_date, source_number

logger = logging.getLogger(__name__)

CAPITOLTRADES_URL = "https://www.capitoltrades.com/trades"
FMP_BASE_URL = "https://financialmodelingprep.com/stable"
FMP_FREE_LIMIT = 25

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


def _canonical_disclosure_url(value: object) -> str:
    """Retain a portable disclosure locator without tracking noise."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return ""
    tracking = {"fbclid", "gclid", "dclid", "msclkid"}
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in tracking
            and not key.casefold().startswith(("utm_", "mc_"))
        )
    )
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path.rstrip("/"),
            query,
            "",
        )
    )


def _normalize_trade(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a CapitolTrades trade object to our standard format."""
    issuer = raw.get("issuer", {}) or {}
    politician = raw.get("politician", {}) or {}

    raw_ticker = issuer.get("issuerTicker") or ""
    # CapitolTrades uses "AAPL:US" format — strip the exchange suffix
    ticker = raw_ticker.split(":")[0] if raw_ticker else ""

    raw_value = raw.get("value", 0)

    native_disclosure_id = (
        raw.get("disclosureId")
        or raw.get("disclosure_id")
        or raw.get("transactionId")
        or raw.get("transaction_id")
        or ""
    )
    source_url = raw.get("url") or raw.get("link") or ""

    return {
        "source": "capitoltrades",
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
        "publication_date": raw.get("pubDate", ""),
        "owner": raw.get("owner", ""),
        "comment": raw.get("comment", ""),
        "native_disclosure_id": native_disclosure_id,
        "source_url": source_url,
        "canonical_disclosure_url": _canonical_disclosure_url(source_url),
    }


def _normalize_fmp_trade(raw: dict[str, Any], chamber: str) -> dict[str, Any]:
    """Convert an FMP latest-disclosure record to our standard format."""
    first = str(raw.get("firstName") or "").strip()
    last = str(raw.get("lastName") or "").strip()
    representative = str(raw.get("office") or "").strip()
    if not representative:
        representative = f"{first} {last}".strip()

    native_disclosure_id = (
        raw.get("disclosureId")
        or raw.get("disclosure_id")
        or raw.get("transactionId")
        or raw.get("transaction_id")
        or ""
    )
    source_url = raw.get("link") or raw.get("url") or ""
    return {
        "source": "fmp",
        "ticker": str(raw.get("symbol") or "").upper().strip(),
        "issuer_name": raw.get("assetDescription", ""),
        "sector": "",
        "transaction_date": raw.get("transactionDate", ""),
        "transaction_type": raw.get("type", ""),
        "amount": raw.get("amount", ""),
        "amount_raw": 0,
        "chamber": chamber,
        "representative": representative,
        "party": "",
        "state": raw.get("district", ""),
        "pub_date": raw.get("disclosureDate", ""),
        "publication_date": raw.get("disclosureDate", ""),
        "owner": raw.get("owner", ""),
        "comment": raw.get("comment", ""),
        "native_disclosure_id": native_disclosure_id,
        "source_url": source_url,
        "canonical_disclosure_url": _canonical_disclosure_url(source_url),
    }


class CongressSource:
    """Data source for congressional stock trading disclosures.

    Uses FMP's authenticated latest House and Senate disclosure endpoints when
    a key is configured. Missing access is an explicit coverage failure.
    Results are cached in-memory only after both chambers succeed.
    """

    name: str = "congress"
    requires_api_key: bool = True

    def __init__(self, fmp_api_key: str | None = None) -> None:
        self._fmp_api_key = fmp_api_key or os.environ.get("FMP_API_KEY", "")
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
        except SourceFetchError as exc:
            return {**exc.partial_data, "error": str(exc)}
        except Exception:
            logger.error("CongressSource.fetch(%s) failed", method)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        """Congress requires a configured stable FMP feed."""
        if not self._fmp_api_key:
            return False
        try:
            import requests  # noqa: F401

            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Public data methods
    # ------------------------------------------------------------------

    def _fetch_fmp_latest(self) -> list[dict[str, Any]]:
        """Fetch the latest free-tier page for both congressional chambers.

        FMP's Basic plan allows at most 25 records and page zero for these
        endpoints. Two calls per daily run stay well inside the 250-call daily
        allowance while covering the most recent disclosures.
        """
        if not self._fmp_api_key:
            raise SourceFetchError("FMP congressional access missing", reason_code="provider_error")
        if "fmp_latest" in self._cache:
            return self._cache["fmp_latest"]

        import requests

        trades: list[dict[str, Any]] = []
        failures, statuses = {}, {}
        for chamber in ("House", "Senate"):
            endpoint = f"{chamber.lower()}-latest"
            try:
                response = provider_request("congress", "GET", f"{FMP_BASE_URL}/{endpoint}",
                    operation=endpoint, params={"page": 0, "limit": FMP_FREE_LIMIT,
                                                "apikey": self._fmp_api_key}, timeout=20)
                payload = response.json()
                if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
                    raise SourceFetchError("FMP disclosures invalid", reason_code="invalid_response")
                invalid_rows = False
                for item in payload:
                    representative = item.get("office") or " ".join(str(item.get(key) or "") for key in ("firstName", "lastName"))
                    if (not all(source_text(item.get(key)) for key in ("symbol", "type", "amount"))
                            or not source_text(representative)
                            or not all(source_date(item.get(key)) for key in ("transactionDate", "disclosureDate"))):
                        invalid_rows = True
                        continue
                    trades.append(_normalize_fmp_trade(item, chamber))
                if invalid_rows:
                    raise SourceFetchError("FMP disclosure records invalid", reason_code="invalid_response")
            except Exception as exc:
                error = source_fetch_error("FMP disclosures failed", exc)
                failures[endpoint] = error.reason_code
                if error.http_status is not None:
                    statuses[endpoint] = error.http_status
        if failures:
            raise SourceFetchError("FMP congressional coverage incomplete", reason_code="batch_failure",
                failed_operations=failures, failed_http_statuses=statuses,
                partial_data={"recent_trades": trades})
        self._cache["fmp_latest"] = trades
        return trades

    def fetch_all_trades(self, max_pages: int = 3) -> list[dict[str, Any]]:
        """Fetch the stable FMP House/Senate pages; max_pages is legacy-only."""
        if "all_trades" not in self._cache:
            self._cache["all_trades"] = self._fetch_fmp_latest()
        return self._cache["all_trades"]

    def get_recent_trades(
        self, days_back: int = 30, as_of: str | None = None
    ) -> list[dict[str, Any]]:
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
        def in_window(trades):
            recent = []
            for trade in trades:
                trade_date = self._parse_trade_date(trade)
                publication_date = self._parse_trade_date({"transaction_date": trade.get("publication_date")})
                if trade_date and publication_date and cutoff <= trade_date <= ref_date and publication_date <= ref_date:
                    recent.append(trade)
            return recent
        try:
            all_trades = self.fetch_all_trades()
        except SourceFetchError as exc:
            exc.partial_data = {"recent_trades": in_window(exc.partial_data.get("recent_trades", []))}
            raise
        recent = in_window(all_trades)

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
        if source_date(raw):
            return datetime.strptime(raw[:10], "%Y-%m-%d")

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
        trades = self.get_recent_trades(days_back=days_back, as_of=params.get("as_of"))
        return {"data": trades, "count": len(trades)}

    def _dispatch_trades_by_ticker(self, params: dict[str, Any]) -> dict[str, Any]:
        ticker = params.get("ticker", "")
        trades = self.get_trades_by_ticker(ticker)
        return {"data": trades, "count": len(trades)}
