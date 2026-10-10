"""CourtListener data source for federal litigation tracking.

CourtListener authenticated default limits: 5/minute, 50/hour and 125/day.
Actual account entitlement requires the official Usage API; it is not inferred here.
Used by P10 (pre-filing litigation/investigation detection).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from urllib.parse import urlsplit, parse_qs

from .fetch_errors import SourceFetchError, source_fetch_error, source_text, source_date, source_number

from .evidence import CoverageRecords, bounded_coverage, collection_envelope
from .request_policy import provider_request, provider_timeout, read_bounded_response, provider_budget, current_provider_deadline

logger = logging.getLogger(__name__)

BASE_URL = "https://www.courtlistener.com/api/rest/v4"
_RATE_DELAY = 0.5
# Upstream ESCursorPagination uses exact parent-docket hits below this limit;
# at/above it, `count` is an approximate distinct-docket cardinality.
_EXACT_DOCKET_COUNT_LIMIT = 10000


def _page_json(raw: bytes):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError("nonfinite JSON")
    try:
        return json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise SourceFetchError("Provider page JSON invalid", reason_code="invalid_response") from None


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
            return {**exc.partial_data, "error": str(exc)}
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

        if current_provider_deadline("courtlistener") is None:
            with provider_budget("courtlistener", time.monotonic()+60):
                return self.search_dockets(query, court, date_filed_after, page_size,
                                           date_filed_before=date_filed_before)
        if (type(page_size) is not int or not 1 <= page_size <= 250
                or any(value is not None and (not source_date(value) or len(value) != 10)
                       for value in (date_filed_after, date_filed_before))
                or (date_filed_after and date_filed_before and date_filed_after > date_filed_before)):
            raise SourceFetchError("CourtListener window invalid", reason_code="invalid_response")
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

        results, seen, visited, used_bytes = [], set(), set(), 0
        url = f"{BASE_URL}/search/"
        request_params = params
        coverage = {"mode": "exhaustive_window", "complete": False, "query": query,
                    "date_filed_after": date_filed_after, "date_filed_before": date_filed_before, "pages": 0}
        try:
            for page in range(1, 1001):
                response = None
                try:
                    response = provider_request("courtlistener", "GET", url, params=request_params,
                        headers={"Authorization": f"Token {self._token}"},
                        timeout=provider_timeout("courtlistener"), stream=True, allow_redirects=False)
                    if response.status_code != 200:
                        raise SourceFetchError("CourtListener request failed", reason_code="http_error", http_status=response.status_code)
                    raw = read_bounded_response(response, provider="courtlistener", max_bytes=32*1024*1024-used_bytes)
                    used_bytes += len(raw)
                    data = _page_json(raw)
                    provider_timeout("courtlistener")
                finally:
                    if response is not None:
                        response.close()
                coverage["pages"] = page
                if (not isinstance(data, dict) or not isinstance(data.get("results"), list)
                        or type(data.get("count")) is not int or data["count"] < 0
                        or "next" not in data or (data["next"] is not None and not source_text(data["next"]))):
                    raise SourceFetchError("CourtListener response invalid", reason_code="invalid_response")
                for item in data["results"]:
                    if (not isinstance(item, dict) or not source_text(item.get("caseName"))
                            or type(item.get("docket_id")) is not int or item["docket_id"] <= 0
                            or not source_date(item.get("dateFiled")) or not source_text(item.get("court"))
                            or (date_filed_after and item["dateFiled"][:10] < date_filed_after)
                            or (date_filed_before and item["dateFiled"][:10] > date_filed_before)
                            or item["docket_id"] in seen):
                        raise SourceFetchError("CourtListener docket invalid", reason_code="invalid_response")
                    seen.add(item["docket_id"])
                    if len(seen) > 20000:
                        raise SourceFetchError("CourtListener row limit reached", reason_code="invalid_response")
                    results.append({"docket_id": item["docket_id"], "case_name": item["caseName"],
                        "court": item["court"], "date_filed": item["dateFiled"],
                        "date_terminated": item.get("dateTerminated"), "cause": item.get("cause", ""),
                        "nature_of_suit": item.get("suitNature", ""), "jury_demand": item.get("juryDemand", "")})
                coverage["total"] = data.get("count")
                if data["next"] is None:
                    # A genuinely complete population below the provider's hit
                    # threshold must have an exact count. Do not select this
                    # branch using the reported estimate, which may undercount.
                    if len(seen) < _EXACT_DOCKET_COUNT_LIMIT:
                        if data["count"] != len(seen):
                            raise SourceFetchError("CourtListener terminal count inconsistent", reason_code="invalid_response")
                        coverage["count_validation"] = "exact_population_match"
                    else:
                        coverage["count_validation"] = "approximate_not_compared"
                    coverage.update(complete=True, has_next=False, termination="null_next")
                    provider_timeout("courtlistener")
                    return CoverageRecords(results, coverage=coverage)
                nxt = data["next"]
                parsed, expected = urlsplit(nxt), urlsplit(f"{BASE_URL}/search/")
                query_params = parse_qs(parsed.query, keep_blank_values=True)
                if (parsed.scheme != expected.scheme or parsed.netloc != expected.netloc
                        or parsed.path != expected.path or parsed.fragment
                        or any(query_params.get(k) != [str(v)] for k, v in params.items())
                        or any(k not in {*params, "cursor", "page"} for k in query_params)
                        or not any(query_params.get(k) for k in ("cursor", "page"))
                        or nxt in visited or not data["results"]):
                    raise SourceFetchError("CourtListener pagination invalid", reason_code="invalid_response")
                visited.add(nxt)
                url, request_params = nxt, None
            raise SourceFetchError("CourtListener page limit reached", reason_code="invalid_response")
        except Exception as exc:
            error = source_fetch_error("CourtListener search_dockets failed", exc)
            error.partial_data = {"dockets": results, "coverage": {**coverage, "complete": False}}
            raise error from None

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
