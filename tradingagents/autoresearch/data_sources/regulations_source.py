"""Regulations.gov data source for proposed federal rules.

Free API key from api.data.gov. 1,000 requests/hour.
Used by P5 (regulatory pipeline → affected companies).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

BASE_URL = "https://api.regulations.gov/v4"
_RATE_DELAY = 0.5  # Conservative rate limiting


class RegulationsSource:
    """Data source for federal regulations from regulations.gov."""

    name: str = "regulations"
    requires_api_key: bool = True

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("REGULATIONS_API_KEY", "")
        self._cache: dict[str, Any] = {}

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "search_documents")
        dispatch = {
            "search_documents": self._dispatch_search,
            "recent_proposed_rules": self._dispatch_recent_proposed,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except Exception:
            logger.error("RegulationsSource.fetch(%s) failed", method, exc_info=True)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        if not self._api_key:
            return False
        try:
            import requests  # noqa: F401
            return True
        except ImportError:
            return False

    def search_documents(
        self,
        search_term: str | None = None,
        agency_id: str | None = None,
        document_type: str = "Proposed Rule",
        posted_date_from: str | None = None,
        page_size: int = 25,
    ) -> list[dict]:
        """Search regulations.gov for documents.

        Args:
            search_term: Text search.
            agency_id: Filter by agency (e.g. "EPA", "SEC", "FDA").
            document_type: "Proposed Rule", "Rule", "Notice", etc.
            posted_date_from: YYYY-MM-DD filter.
            page_size: Max results.

        Returns:
            List of document dicts.
        """
        import requests

        params: dict[str, Any] = {
            "filter[documentType]": document_type,
            "page[size]": page_size,
            "sort": "-postedDate",
        }
        if search_term:
            params["filter[searchTerm]"] = search_term
        if agency_id:
            params["filter[agencyId]"] = agency_id
        if posted_date_from:
            params["filter[postedDate][ge]"] = posted_date_from

        time.sleep(_RATE_DELAY)
        try:
            resp = requests.get(
                f"{BASE_URL}/documents",
                params=params,
                headers={"X-Api-Key": self._api_key},
                timeout=15,
            )
            if resp.status_code != 200:
                logger.warning("regulations.gov returned %d", resp.status_code)
                return []

            data = resp.json()
            results = []
            for item in data.get("data", []):
                attrs = item.get("attributes", {})
                results.append({
                    "document_id": item.get("id", ""),
                    "title": attrs.get("title", ""),
                    "agency_id": attrs.get("agencyId", ""),
                    "document_type": attrs.get("documentType", ""),
                    "posted_date": attrs.get("postedDate", ""),
                    "comment_end_date": attrs.get("commentEndDate", ""),
                    "summary": attrs.get("summary", "")[:500],
                    "docket_id": attrs.get("docketId", ""),
                })
            return results
        except Exception:
            logger.error("search_documents failed", exc_info=True)
            return []

    def get_recent_proposed_rules(
        self,
        agencies: list[str] | None = None,
        days_back: int = 30,
    ) -> list[dict]:
        """Get recently proposed rules, optionally filtered by agency."""
        from datetime import datetime, timedelta

        date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        results = []

        if agencies:
            for agency in agencies:
                docs = self.search_documents(
                    agency_id=agency,
                    document_type="Proposed Rule",
                    posted_date_from=date_from,
                )
                results.extend(docs)
        else:
            results = self.search_documents(
                document_type="Proposed Rule",
                posted_date_from=date_from,
            )

        return results

    def clear_cache(self) -> None:
        self._cache.clear()

    def _dispatch_search(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"data": self.search_documents(
            search_term=params.get("search_term"),
            agency_id=params.get("agency_id"),
            document_type=params.get("document_type", "Proposed Rule"),
            posted_date_from=params.get("posted_date_from"),
        )}

    def _dispatch_recent_proposed(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"data": self.get_recent_proposed_rules(
            agencies=params.get("agencies"),
            days_back=params.get("days_back", 30),
        )}
