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

from .evidence import CoverageRecords, bounded_coverage, collection_envelope
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
        *, date_filed_before: str | None = None,
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
        if date_filed_before:
            params["filed_before"] = date_filed_before

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
            return CoverageRecords(results, coverage=bounded_coverage(returned=len(results), limit=page_size,
                total=data.get('count'), has_next=bool(data['next']) if 'next' in data else None,
                query=query, date_filed_after=date_filed_after, date_filed_before=date_filed_before))
        except Exception as exc:
            safe_error = source_fetch_error("CourtListener search_dockets failed", exc)
            logger.error("%s", safe_error)
            raise safe_error from None

    def search_opinions(
        self,
        query: str,
        date_filed_after: str | None = None,
        page_size: int = 20,
        *, date_filed_before: str | None = None,
    ) -> list[dict]:
        """Search opinion clusters, retaining their distinct nested opinion references."""
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
        if date_filed_before:
            params["filed_before"] = date_filed_before

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
                        or type(item.get("cluster_id")) is not int or item["cluster_id"] <= 0
                        or not source_date(item.get("dateFiled"))
                        or not isinstance(item.get("opinions"), list) or not item["opinions"]
                        or not all(isinstance(opinion, dict) and type(opinion.get("id")) is int
                                   and opinion["id"] > 0 for opinion in item["opinions"])
                        or len({opinion["id"] for opinion in item["opinions"]}) != len(item["opinions"])):
                    raise SourceFetchError("CourtListener opinion record invalid", reason_code="invalid_response",
                                           partial_data={"opinions": results})
                results.append({
                    "cluster_id": item["cluster_id"],
                    "docket_id": item.get("docket_id"),
                    "opinion_ids": [opinion["id"] for opinion in item["opinions"]],
                    "opinions": [{"opinion_id": opinion["id"], "type": opinion.get("type", ""),
                                  "snippet": opinion.get("snippet", ""),
                                  "download_url": opinion.get("download_url"),
                                  "local_path": opinion.get("local_path"),
                                  "author_id": opinion.get("author_id")}
                                 for opinion in item["opinions"]],
                    "case_name": item.get("caseName", ""),
                    "date_filed": item.get("dateFiled", ""),
                    "court": item.get("court", ""),
                    "type": item.get("type", ""),
                })
            return CoverageRecords(results, coverage=bounded_coverage(returned=len(results), limit=page_size,
                total=data.get('count'), has_next=bool(data['next']) if 'next' in data else None,
                query=query, date_filed_after=date_filed_after, date_filed_before=date_filed_before,
                unit='opinion_clusters'))
        except Exception as exc:
            safe_error = source_fetch_error("CourtListener search_opinions failed", exc)
            logger.error("%s", safe_error)
            raise safe_error from None

    def clear_cache(self) -> None:
        self._cache.clear()

    def _dispatch_search_dockets(self, params: dict[str, Any]) -> dict[str, Any]:
        return collection_envelope(self.search_dockets(
            query=params.get("query", ""),
            court=params.get("court"),
            date_filed_after=params.get("date_filed_after"),
            page_size=params.get("page_size",20),
            date_filed_before=params.get("date_filed_before"),
        ))

    def _dispatch_search_opinions(self, params: dict[str, Any]) -> dict[str, Any]:
        return collection_envelope(self.search_opinions(
            query=params.get("query", ""),
            date_filed_after=params.get("date_filed_after"),
            page_size=params.get("page_size",20),
            date_filed_before=params.get("date_filed_before"),
        ))
