"""CourtListener data source for federal litigation tracking.

Free account token from courtlistener.com. 5/minute, 50/hour and 125/day on the verified account.
Used by P10 (pre-filing litigation/investigation detection).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from .fetch_errors import SourceFetchError, source_fetch_error, source_text, source_date, source_number

from .request_policy import provider_request

logger = logging.getLogger(__name__)

BASE_URL = "https://www.courtlistener.com/api/rest/v4"
_RATE_DELAY = 0.5


class CourtListenerSource:
    """Data source for federal court dockets and opinions."""

    name: str = "courtlistener"
    requires_api_key: bool = True

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("COURTLISTENER_TOKEN", "")
        self._cache: dict[str, Any] = {}

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "search_dockets")
        dispatch = {
            "search_dockets": self._dispatch_search_dockets,
            "search_opinions": self._dispatch_search_opinions,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except SourceFetchError as exc:
            return {"error": str(exc)}
        except Exception:
            logger.error("CourtListenerSource.fetch(%s) failed", method)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        if not self._token:
            return False
        try:
            import requests  # noqa: F401
            return True
        except ImportError:
            return False

    def search_dockets(
        self,
        query: str,
        court: str | None = None,
        date_filed_after: str | None = None,
        page_size: int = 20,
    ) -> list[dict]:
        """Search federal court dockets.

        Args:
            query: Search text (company name, case type, etc.).
            court: Court identifier (e.g. "cacd" for Central District of CA).
            date_filed_after: YYYY-MM-DD filter.
            page_size: Max results.

        Returns:
            List of docket dicts.
        """
        import requests

        if not self._token:
            raise SourceFetchError("CourtListener access missing", reason_code="provider_error")
        params: dict[str, Any] = {
            "q": query,
            "type": "r",  # RECAP dockets
            "page_size": page_size,
            "order_by": "dateFiled desc",
        }
        if court:
            params["court"] = court
        if date_filed_after:
            params["filed_after"] = date_filed_after

        try:
            resp = provider_request("courtlistener", "GET",
                f"{BASE_URL}/search/",
                params=params,
                headers={"Authorization": f"Token {self._token}"},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning("CourtListener returned %d", resp.status_code)
                raise SourceFetchError("CourtListener request failed", reason_code="http_error", http_status=resp.status_code)

            data = resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise SourceFetchError("CourtListener response invalid", reason_code="invalid_response")
            results = []
            for item in data["results"]:
                if (not isinstance(item, dict) or not source_text(item.get("caseName"))
                        or not item.get("docket_id") or not source_date(item.get("dateFiled"))
                        or not source_text(item.get("court"))):
                    raise SourceFetchError("CourtListener docket record invalid", reason_code="invalid_response",
                                           partial_data={"dockets": results})
                results.append({
                    "docket_id": item.get("docket_id", ""),
                    "case_name": item.get("caseName", ""),
                    "court": item.get("court", ""),
                    "date_filed": item.get("dateFiled", ""),
                    "date_terminated": item.get("dateTerminated"),
                    "cause": item.get("cause", ""),
                    "nature_of_suit": item.get("suitNature", ""),
                    "jury_demand": item.get("juryDemand", ""),
                })
            return results
        except Exception as exc:
            safe_error = source_fetch_error("CourtListener search_dockets failed", exc)
            logger.error("%s", safe_error)
            raise safe_error from None

    def search_opinions(
        self,
        query: str,
        date_filed_after: str | None = None,
        page_size: int = 20,
    ) -> list[dict]:
        """Search court opinions."""
        import requests

        if not self._token:
            raise SourceFetchError("CourtListener access missing", reason_code="provider_error")
        params: dict[str, Any] = {
            "q": query,
            "type": "o",  # opinions
            "page_size": page_size,
            "order_by": "dateFiled desc",
        }
        if date_filed_after:
            params["filed_after"] = date_filed_after

        try:
            resp = provider_request("courtlistener", "GET",
                f"{BASE_URL}/search/",
                params=params,
                headers={"Authorization": f"Token {self._token}"},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning("CourtListener opinions returned %d", resp.status_code)
                raise SourceFetchError("CourtListener request failed", reason_code="http_error", http_status=resp.status_code)

            data = resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise SourceFetchError("CourtListener response invalid", reason_code="invalid_response")
            results = []
            for item in data["results"]:
                if (not isinstance(item, dict) or not source_text(item.get("caseName"))
                        or not item.get("id") or not source_date(item.get("dateFiled"))):
                    raise SourceFetchError("CourtListener opinion record invalid", reason_code="invalid_response",
                                           partial_data={"opinions": results})
                results.append({
                    "opinion_id": item.get("id", ""),
                    "case_name": item.get("caseName", ""),
                    "date_filed": item.get("dateFiled", ""),
                    "court": item.get("court", ""),
                    "type": item.get("type", ""),
                })
            return results
        except Exception as exc:
            safe_error = source_fetch_error("CourtListener search_opinions failed", exc)
            logger.error("%s", safe_error)
            raise safe_error from None

    def clear_cache(self) -> None:
        self._cache.clear()

    def _dispatch_search_dockets(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"data": self.search_dockets(
            query=params.get("query", ""),
            court=params.get("court"),
            date_filed_after=params.get("date_filed_after"),
        )}

    def _dispatch_search_opinions(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"data": self.search_opinions(
            query=params.get("query", ""),
            date_filed_after=params.get("date_filed_after"),
        )}
