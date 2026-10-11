"""Regulations.gov data source for proposed federal rules.

Free API key from api.data.gov. 1,000 requests/hour.
Used by P5 (regulatory pipeline → affected companies).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from .evidence import current_session_date, CoverageRecords, bounded_coverage, collection_envelope
from .request_policy import provider_request, provider_timeout, read_bounded_response, provider_budget, current_provider_deadline
from .fetch_errors import SourceFetchError, source_fetch_error, source_text, source_date, source_number

logger = logging.getLogger(__name__)

BASE_URL = "https://api.regulations.gov/v4"
_RATE_DELAY = 0.5  # Conservative rate limiting


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
        except SourceFetchError as exc:
            return {**exc.partial_data, "error": str(exc)}
        except Exception:
            logger.error("RegulationsSource.fetch(%s) failed", method)
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
        self, search_term: str | None = None, agency_id: str | None = None,
        document_type: str = "Proposed Rule", posted_date_from: str | None = None,
        page_size: int = 100, *, posted_date_to: str | None = None,
    ) -> list[dict]:
        """Read a complete sorted date window, or an explicitly bounded latest page.

        The broken server-side postedDate filter remains unused. Every raw date
        is checked before a strictly older row can prove the window boundary.
        """
        if current_provider_deadline("regulations") is None:
            with provider_budget("regulations", time.monotonic()+60):
                return self.search_documents(search_term, agency_id, document_type,
                    posted_date_from, page_size, posted_date_to=posted_date_to)
        if not self._api_key:
            raise SourceFetchError("Regulations access missing", reason_code="provider_error")
        if (type(page_size) is not int or not 1 <= page_size <= 250
                or (posted_date_from is not None and (not source_date(posted_date_from) or len(posted_date_from) != 10))
                or (posted_date_to is not None and (not source_date(posted_date_to) or len(posted_date_to) != 10))
                or (posted_date_from and posted_date_to and posted_date_from > posted_date_to)):
            raise SourceFetchError("Regulations window invalid", reason_code="invalid_response")
        params = {"filter[documentType]": document_type, "page[size]": page_size,
                  "sort": "-postedDate"}
        if search_term:
            params["filter[searchTerm]"] = search_term
        if agency_id:
            params["filter[agencyId]"] = agency_id
        results, seen, previous, used_bytes = [], set(), None, 0
        pagination_metadata = None
        coverage = {"mode": "exhaustive_window" if posted_date_from else "bounded_sample",
                    "complete": False, "agency": agency_id,
                    "posted_date_from": posted_date_from, "posted_date_to": posted_date_to,
                    "pages": 0}
        try:
            for page in range(1, 201):
                response = None
                try:
                    response = provider_request("regulations", "GET", f"{BASE_URL}/documents",
                        params={**params, "page[number]": page},
                        headers={"X-Api-Key": self._api_key}, timeout=provider_timeout("regulations"),
                        stream=True, allow_redirects=False)
                    if response.status_code != 200:
                        raise SourceFetchError("Regulations request failed", reason_code="http_error", http_status=response.status_code)
                    raw = read_bounded_response(response, provider="regulations", max_bytes=32*1024*1024-used_bytes)
                    used_bytes += len(raw)
                    data = _page_json(raw)
                    provider_timeout("regulations")
                finally:
                    if response is not None:
                        response.close()
                coverage["pages"] = page
                if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                    raise SourceFetchError("Regulations response invalid", reason_code="invalid_response")
                meta = data.get("meta")
                total_pages = meta.get("totalPages") if isinstance(meta, dict) else None
                total_elements = meta.get("totalElements") if isinstance(meta, dict) else None
                metadata = (total_pages, "totalElements" in meta, total_elements) if isinstance(meta, dict) else None
                if (type(total_pages) is not int or total_pages < 0
                        or (total_pages and page > total_pages)
                        or ("totalElements" in meta and (type(total_elements) is not int or total_elements < 0))
                        or (total_pages == 0 and (data["data"] or total_elements not in (None, 0)))
                        or (pagination_metadata is not None and metadata != pagination_metadata)):
                    raise SourceFetchError("Regulations pagination invalid", reason_code="invalid_response")
                pagination_metadata = metadata
                older = False
                for item in data["data"]:
                    attrs = item.get("attributes") if isinstance(item, dict) else None
                    if (not isinstance(attrs, dict) or not source_text(item.get("id"))
                            or not all(source_text(attrs.get(k)) for k in ("title", "agencyId", "documentType"))
                            or not source_date(attrs.get("postedDate"))
                            or (agency_id and attrs["agencyId"] != agency_id)
                            or attrs["documentType"] != document_type):
                        raise SourceFetchError("Regulations document invalid", reason_code="invalid_response")
                    day = attrs["postedDate"][:10]
                    if previous is not None and day > previous:
                        raise SourceFetchError("Regulations ordering invalid", reason_code="invalid_response")
                    previous = day
                    if item["id"] in seen:
                        raise SourceFetchError("Regulations repeated document", reason_code="invalid_response")
                    seen.add(item["id"])
                    if len(seen) > 50000:
                        raise SourceFetchError("Regulations row limit reached", reason_code="invalid_response")
                    older |= bool(posted_date_from and day < posted_date_from)
                    if ((posted_date_from and day < posted_date_from)
                            or (posted_date_to and day > posted_date_to)):
                        continue
                    results.append({"document_id": item["id"], "title": attrs["title"],
                        "agency_id": attrs["agencyId"], "document_type": attrs["documentType"],
                        "posted_date": attrs["postedDate"], "comment_end_date": attrs.get("commentEndDate", ""),
                        "summary": (attrs.get("summary") or "")[:500], "docket_id": attrs.get("docketId", "")})
                terminal = page >= total_pages
                if (total_elements is not None and
                        (len(seen) > total_elements or (terminal and len(seen) != total_elements))):
                    raise SourceFetchError("Regulations pagination totals inconsistent", reason_code="invalid_response")
                coverage.update(total=meta.get("totalElements"), has_next=not terminal)
                if older or terminal or not posted_date_from:
                    coverage.update(complete=bool(posted_date_from),
                                    termination="older_than_window" if older else "terminal_page" if terminal else "bounded_page")
                    provider_timeout("regulations")
                    return CoverageRecords(results, coverage=coverage)
                if not data["data"]:
                    raise SourceFetchError("Regulations nonterminal empty page", reason_code="invalid_response")
            raise SourceFetchError("Regulations page limit reached", reason_code="invalid_response")
        except Exception as exc:
            error = source_fetch_error("Regulations search failed", exc)
            error.partial_data = {"proposed_rules": results, "coverage": {**coverage, "complete": False}}
            raise error from None

    def get_recent_proposed_rules(
        self,
        agencies: list[str] | None = None,
        days_back: int = 30,
        *, as_of: str | None = None,
    ) -> list[dict]:
        """Get recently proposed rules, optionally filtered by agency."""
        from datetime import datetime, timedelta

        date_from = ((datetime.fromisoformat(as_of) if as_of else datetime.fromisoformat(current_session_date())) - timedelta(days=days_back)).strftime("%Y-%m-%d")
        results = []

        failures, statuses, coverages = {}, {}, {}
        for agency in agencies or [None]:
            try:
                docs = self.search_documents(agency_id=agency, document_type="Proposed Rule",
                                             posted_date_from=date_from, posted_date_to=as_of or current_session_date())
                coverages[agency or "all_agencies"] = getattr(docs, "coverage", {})
                if as_of:
                    docs = [row for row in docs if (row.get("posted_date") or "")[:10] <= as_of]
                results.extend(docs)
            except SourceFetchError as exc:
                partial = exc.partial_data.get("proposed_rules", [])
                if as_of:
                    partial = [row for row in partial if (row.get("posted_date") or "")[:10] <= as_of]
                results.extend(partial)
                identity = agency or "all_agencies"
                coverages[identity] = exc.partial_data.get("coverage", {"complete": False})
                failures[identity] = exc.reason_code
                if exc.http_status is not None:
                    statuses[identity] = exc.http_status
        if failures:
            raise SourceFetchError("Regulations agency coverage incomplete", reason_code="batch_failure",
                                   failed_operations=failures, failed_http_statuses=statuses,
                                   partial_data={"proposed_rules": results, "coverage":{"mode":"exhaustive_window", "complete":False, "samples":coverages}})

        return CoverageRecords(results, coverage={"mode":"exhaustive_window", "complete":True, "samples":coverages, "as_of":as_of})

    def clear_cache(self) -> None:
        self._cache.clear()

    def _dispatch_search(self, params: dict[str, Any]) -> dict[str, Any]:
        return collection_envelope(self.search_documents(
            search_term=params.get("search_term"),
            agency_id=params.get("agency_id"),
            document_type=params.get("document_type", "Proposed Rule"),
            posted_date_from=params.get("posted_date_from"),
            page_size=params.get("page_size",100),
            posted_date_to=params.get("posted_date_to"),
        ))

    def _dispatch_recent_proposed(self, params: dict[str, Any]) -> dict[str, Any]:
        return collection_envelope(self.get_recent_proposed_rules(
            agencies=params.get("agencies"),
            days_back=params.get("days_back", 30),
            as_of=params.get("as_of"),
        ))
