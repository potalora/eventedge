"""Finnhub data source for earnings transcripts and company news.

Free tier: 60 calls/min. Used by P1/P2 (earnings call analysis)
and P6 (supply chain disruption news).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_RATE_DELAY = 1.1  # ~60 calls/min → 1 call/sec with margin


class FinnhubSource:
    """Data source backed by the Finnhub API."""

    name: str = "finnhub"
    requires_api_key: bool = True

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("FINNHUB_API_KEY", "")
        self._cache: dict[str, Any] = {}
        self._client = None

    def _get_client(self):
        if self._client is None:
            import finnhub
            self._client = finnhub.Client(api_key=self._api_key)
        return self._client

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "earnings_transcripts")
        dispatch = {
            "earnings_transcripts": self._dispatch_transcripts,
            "company_news": self._dispatch_news,
            "supply_chain": self._dispatch_supply_chain,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except Exception:
            logger.error("FinnhubSource.fetch(%s) failed", method, exc_info=True)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        if not self._api_key:
            return False
        try:
            import finnhub  # noqa: F401
            return True
        except ImportError:
            logger.warning("finnhub-python not installed — run: pip install finnhub-python")
            return False

    def fetch_recent_earnings(
        self, date_from: str, date_to: str,
    ) -> list[dict]:
        """Fetch earnings calendar — which companies just reported.

        Returns list of dicts with symbol, date, epsActual, epsEstimate, etc.
        Free tier endpoint (transcripts require paid plan).
        """
        cache_key = f"earnings_cal|{date_from}|{date_to}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        client = self._get_client()
        time.sleep(_RATE_DELAY)
        try:
            result = client.earnings_calendar(
                _from=date_from, to=date_to, symbol="",
            )
            events = result.get("earningsCalendar", [])
            # Filter to those with actual results (already reported)
            reported = [
                e for e in events
                if e.get("epsActual") is not None
            ]
            self._cache[cache_key] = reported
            return reported
        except Exception:
            logger.error("Failed to fetch earnings calendar", exc_info=True)
            return []

    def fetch_earnings_news(
        self, symbol: str, earnings_date: str,
    ) -> list[dict]:
        """Fetch news around an earnings date as a proxy for transcript analysis.

        Gets news from 1 day before to 2 days after earnings to capture
        call commentary, analyst reactions, and guidance discussion.
        """
        from datetime import datetime, timedelta

        dt = datetime.strptime(earnings_date, "%Y-%m-%d")
        date_from = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        date_to = (dt + timedelta(days=2)).strftime("%Y-%m-%d")

        return self.fetch_company_news(symbol, date_from, date_to)

    def fetch_company_news(
        self, symbol: str, date_from: str, date_to: str
    ) -> list[dict]:
        """Fetch company news articles."""
        cache_key = f"news|{symbol}|{date_from}|{date_to}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        client = self._get_client()
        time.sleep(_RATE_DELAY)
        try:
            news = client.company_news(symbol, _from=date_from, to=date_to)
            result = [
                {
                    "headline": n.get("headline", ""),
                    "summary": n.get("summary", ""),
                    "source": n.get("source", ""),
                    "datetime": n.get("datetime", 0),
                    "url": n.get("url", ""),
                    "category": n.get("category", ""),
                }
                for n in (news or [])
            ]
            self._cache[cache_key] = result
            return result
        except Exception:
            logger.error("Failed to fetch news for %s", symbol, exc_info=True)
            return []

    def fetch_supply_chain(self, symbol: str) -> list[dict]:
        """Fetch supply chain relationships (peers/suppliers/customers)."""
        cache_key = f"supply|{symbol}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        client = self._get_client()
        time.sleep(_RATE_DELAY)
        try:
            peers = client.company_peers(symbol)
            result = [{"ticker": p, "relationship": "peer"} for p in (peers or [])]
            self._cache[cache_key] = result
            return result
        except Exception:
            logger.error("Failed to fetch supply chain for %s", symbol, exc_info=True)
            return []

    def fetch_earnings_transcript(
        self, symbol: str, year: int, quarter: int,
    ) -> str | None:
        """Fetch real earnings call transcript from Finnhub.

        Returns full transcript text, or None if unavailable (paid-tier, not found, etc).
        """
        cache_key = f"transcript|{symbol}|{year}|{quarter}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        client = self._get_client()
        time.sleep(_RATE_DELAY)
        try:
            result = client.earnings_call_transcripts(symbol, year, quarter)
        except Exception as exc:
            # 403 / payment-required / network errors
            logger.debug(
                "Transcript unavailable for %s %dQ%d: %s", symbol, year, quarter, exc,
            )
            return None

        if not result:
            return None

        # Response is a list of segments: [{name, speech}, ...]
        transcript = result if isinstance(result, list) else result.get("transcript", [])
        if not transcript:
            return None

        text = "\n".join(
            f"{seg.get('name', 'Speaker')}: {seg.get('speech', '')}"
            for seg in transcript
            if seg.get("speech")
        )
        if not text:
            return None

        self._cache[cache_key] = text
        return text

    def clear_cache(self) -> None:
        self._cache.clear()

    def _dispatch_transcripts(self, params: dict[str, Any]) -> dict[str, Any]:
        date_from = params.get("date_from", "")
        date_to = params.get("date_to", "")
        return {"data": self.fetch_recent_earnings(date_from, date_to)}

    def _dispatch_news(self, params: dict[str, Any]) -> dict[str, Any]:
        symbol = params.get("symbol", "")
        date_from = params.get("date_from", "")
        date_to = params.get("date_to", "")
        return {"data": self.fetch_company_news(symbol, date_from, date_to)}

    def _dispatch_supply_chain(self, params: dict[str, Any]) -> dict[str, Any]:
        symbol = params.get("symbol", "")
        return {"data": self.fetch_supply_chain(symbol)}
