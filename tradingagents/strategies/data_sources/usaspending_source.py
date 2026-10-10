from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from .award_identity import native_uei, resolve_award_issuer

from .fetch_errors import SourceFetchError, source_fetch_error, source_text, source_date, source_number

from .evidence import current_session_date, CoverageRecords, collection_envelope
from .request_policy import provider_request, provider_budget, current_provider_deadline

logger = logging.getLogger(__name__)

BASE_URL = "https://api.usaspending.gov/api/v2/"


def _normalize_award_date(value: object) -> str:
    """Normalize USASpending date fields to date-only ISO strings.

    The API returns modification timestamps as naive datetime strings such as
    ``"2026-08-05 10:11:25"``. Downstream event-identity staging rejects
    naive timestamps, so truncate to the calendar date (``YYYY-MM-DD``),
    which the date-only availability path interprets unambiguously as UTC.
    Unparseable values become ``""`` so callers treat them as absent.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        candidate = value.strip()
        for separator in (" ", "T"):
            if separator in candidate:
                candidate = candidate.split(separator, 1)[0]
                break
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            return ""
    return ""


def _new_award_window(date_from: str | None, date_to: str | None) -> tuple[str, str]:
    end = date_to or current_session_date()
    if not source_date(end) or (date_from is not None and not source_date(date_from)):
        raise SourceFetchError("USASpending award window invalid", reason_code="invalid_response")
    start = date_from or (date.fromisoformat(end) - timedelta(days=30)).isoformat()
    if start > end:
        raise SourceFetchError("USASpending award window invalid", reason_code="invalid_response")
    return start, end


class USASpendingSource:
    """Data source for federal contract awards via USASpending.gov API.

    No API key required. Provides search over government contract awards
    useful for identifying companies winning large federal contracts.
    """

    name: str = "usaspending"
    requires_api_key: bool = False

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        """Generic dispatcher.

        Supported params["method"] values:
            search_contracts, recent_large_contracts
        """
        method = params.get("method", "search_contracts")
        dispatch = {
            "search_contracts": self._dispatch_search_contracts,
            "recent_large_contracts": self._dispatch_recent_large,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except SourceFetchError as exc:
            return {"error": str(exc)}
        except Exception:
            logger.error("USASpendingSource.fetch(%s) failed", method)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        """USASpending is available if requests is installed."""
        try:
            import requests  # noqa: F401

            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Public data methods
    # ------------------------------------------------------------------

    def search_contracts(self, keywords: list[str] | None = None, recipient: str | None = None,
                         date_from: str | None = None, date_to: str | None = None,
                         min_amount: float | None = None) -> list[dict[str, Any]]:
        """Exhaust new-award windows; budget failures retain incomplete evidence."""
        date_from, date_to = _new_award_window(date_from, date_to)
        if current_provider_deadline("usaspending") is None:
            with provider_budget("usaspending", time.monotonic() + 60):
                return self.search_contracts(keywords, recipient, date_from, date_to, min_amount)
        records, seen, page = [], set(), 1
        while True:
            try:
                rows = self._search_contracts_page(keywords, recipient, date_from, date_to, min_amount, page)
            except SourceFetchError as exc:
                exc.partial_data = {"contracts":records + exc.partial_data.get("contracts",[]),
                                    "coverage":{"mode":"exhaustive_window", "complete":False}}
                raise
            for row in rows:
                if row["award_key"] in seen:
                    raise SourceFetchError("USASpending pagination repeated awards", reason_code="invalid_response",
                                           partial_data={"contracts":records})
                seen.add(row["award_key"])
                records.append(row)
            if not rows.coverage['has_next']:
                return CoverageRecords(records, coverage={"mode":"exhaustive_window", "complete":True,
                    "returned":len(records), "date_from":date_from, "date_to":date_to, "pages":page,
                    "date_type":"new_awards_only", "amount_basis":"cumulative_award_obligations",
                    "issuer_attribution": self._identity_coverage(records)})
            if not rows or page >= 200:
                raise SourceFetchError("USASpending window incomplete", reason_code="invalid_response",
                                       partial_data={"contracts":records})
            page += 1

    def _search_contracts_page(
        self,
        keywords: list[str] | None = None,
        recipient: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        min_amount: float | None = None,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Search newly originated federal awards by base obligation date.

        Amount is the award's cumulative obligations at acquisition, not its
        initial obligation or a modification flow. Performance start and record
        maintenance dates do not establish new-award timing or publication.

        Args:
            keywords: Text search terms.
            recipient: Recipient/company name to filter by.
            date_from: Start date (YYYY-MM-DD).
            date_to: End date (YYYY-MM-DD).
            min_amount: Minimum award amount in USD.

        Returns:
            List of contract dicts with keys:
            award_id, recipient_name, amount, agency, start_date,
            last_modified_date, description.
        """
        import requests

        date_from, date_to = _new_award_window(date_from, date_to)
        filters: dict[str, Any] = {
            "award_type_codes": ["A", "B", "C", "D"],  # Contract types
        }
        if keywords:
            filters["keywords"] = keywords
        if recipient:
            filters["recipient_search_text"] = [recipient]
        if date_from or date_to:
            time_period = {"date_type": "new_awards_only"}
            if date_from:
                time_period["start_date"] = date_from
            if date_to:
                time_period["end_date"] = date_to
            filters["time_period"] = [time_period]
        if min_amount is not None:
            filters["award_amounts"] = [{"lower_bound": min_amount}]

        payload = {
            "filters": filters,
            "fields": [
                "Award ID",
                "Recipient Name",
                "Recipient UEI",
                "recipient_id",
                "Award Amount",
                "Base Obligation Date",
                "generated_internal_id",
                "Awarding Agency",
                "Start Date",
                "Last Modified Date",
                "Description",
            ],
            "page": page,
            "limit": 50,
            "sort": "Award Amount",
            "order": "desc",
        }

        try:
            resp = provider_request("usaspending", "POST",
                f"{BASE_URL}search/spending_by_award/",
                json=payload,
                timeout=30,
            )
            if resp.status_code != 200:
                logger.warning("USASpending search returned %d", resp.status_code)
                raise SourceFetchError("USASpending request failed", reason_code="http_error", http_status=resp.status_code)

            data = resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise SourceFetchError("USASpending response invalid", reason_code="invalid_response")
            results: list[dict[str, Any]] = []
            observed_at = datetime.now(timezone.utc).isoformat()
            for row in data["results"]:
                base_date = _normalize_award_date(row.get("Base Obligation Date")) if isinstance(row, dict) else ""
                generated_id = row.get("generated_internal_id") if isinstance(row, dict) else None
                internal_id = row.get("internal_id") if isinstance(row, dict) else None
                award_key = generated_id if source_text(generated_id) else (
                    f"internal_id:{internal_id}" if type(internal_id) is int and internal_id > 0 else "")
                if (not isinstance(row, dict) or not all(source_text(row.get(key)) for key in ("Award ID", "Recipient Name"))
                        or not source_number(row.get("Award Amount"), minimum=0)
                        or not source_date(base_date) or not award_key
                        or (date_from and base_date < date_from) or (date_to and base_date > date_to)):
                    raise SourceFetchError("USASpending award record invalid", reason_code="invalid_response",
                                           partial_data={"contracts": results})
                contract = {
                        "award_id": row.get("Award ID", ""),
                        "award_key": award_key,
                        "generated_internal_id": generated_id if source_text(generated_id) else None,
                        "internal_id": internal_id if type(internal_id) is int and internal_id > 0 else None,
                        "recipient_name": row.get("Recipient Name", ""),
                        "recipient_uei": native_uei(row.get("Recipient UEI")),
                        "recipient_id": row.get("recipient_id") if source_text(row.get("recipient_id")) else "",
                        "base_obligation_date": base_date,
                        "award_scope": "new_awards_only",
                        "amount_basis": "cumulative_award_obligations",
                        "observed_at": observed_at,
                        "amount": row.get("Award Amount", 0),
                        "agency": row.get("Awarding Agency", ""),
                        "start_date": _normalize_award_date(row.get("Start Date", "")),
                        "last_modified_date": _normalize_award_date(
                            row.get("Last Modified Date", "")
                        ),
                        "description": row.get("Description", ""),
                    }
                self._enrich_recipient_identity(contract)
                results.append(contract)
            metadata = data.get('page_metadata', {})
            has_next = metadata.get('hasNext') if isinstance(metadata, dict) else None
            if type(has_next) is not bool or (metadata.get('page', page) != page):
                raise SourceFetchError("USASpending pagination metadata invalid", reason_code="invalid_response",
                                       partial_data={"contracts":results})
            return CoverageRecords(results, coverage={"has_next":has_next, "date_type":"new_awards_only",
                "amount_basis":"cumulative_award_obligations",
                "issuer_attribution": self._identity_coverage(results)})
        except Exception as exc:
            safe_error = source_fetch_error("USASpending contract fetch failed", exc)
            logger.error("%s", safe_error)
            raise safe_error from None

    @staticmethod
    def _identity_coverage(records: list[dict]) -> dict:
        verified = sum(bool(row.get("issuer_attribution", {}).get("verified")) for row in records)
        return {"mode": "reviewed_native_uei_crosswalk", "complete": verified == len(records), "verified": verified,
                "unresolved": len(records) - verified,
                "lookup_failures": sum(row.get("recipient_identity_status") == "lookup_failed" for row in records),
                "crosswalk_version": "reviewed_2026-10-09"}

    def _enrich_recipient_identity(self, contract: dict) -> None:
        """Bind parent identity to this award; failures retain unknown evidence."""
        contract["parent_recipient_uei"] = ""
        contract["recipient_identity_status"] = "search_recipient"
        contract["recipient_identity_source"] = f"{BASE_URL}search/spending_by_award/"
        contract["issuer_attribution"] = resolve_award_issuer(contract)
        if contract["issuer_attribution"]["verified"]:
            return
        if not (contract["recipient_uei"] or contract["recipient_id"]):
            contract["recipient_identity_status"] = "missing_native_recipient"
            return

        native_key = contract.get("generated_internal_id") or contract.get("internal_id")
        url = f"{BASE_URL}awards/{quote(str(native_key), safe='')}/"
        try:
            response = provider_request("usaspending", "GET", url, timeout=15)
            if response.status_code != 200:
                raise ValueError("identity lookup unavailable")
            detail = response.json()
            recipient = detail.get("recipient") if isinstance(detail, dict) else None
            if not isinstance(recipient, dict):
                raise ValueError("identity detail invalid")
            if (contract.get("generated_internal_id") and
                    detail.get("generated_unique_award_id") != contract["generated_internal_id"]):
                raise ValueError("award identity mismatch")
            if contract.get("internal_id") is not None and (
                    type(detail.get("id")) is not int or detail["id"] != contract["internal_id"]):
                raise ValueError("award internal identity mismatch")
            uei = native_uei(recipient.get("recipient_uei"))
            if (not uei or (contract["recipient_uei"] and contract["recipient_uei"] != uei)
                    or (contract["recipient_id"] and contract["recipient_id"] != recipient.get("recipient_hash"))):
                raise ValueError("recipient identity mismatch")
            parent = recipient.get("parent_recipient_uei")
            if parent is not None and not native_uei(parent):
                raise ValueError("parent identity invalid")
            contract.update(recipient_uei=uei, parent_recipient_uei=native_uei(parent),
                            recipient_identity_status="native_award_verified", recipient_identity_source=url)
            contract["issuer_attribution"] = resolve_award_issuer(contract)
        except Exception:
            # Award acquisition remains complete. Attribution is separately
            # incomplete and cannot create an actionable issuer or guessed ID.
            contract["recipient_identity_status"] = "lookup_failed"

    def get_recent_large_contracts(
        self,
        min_amount: float = 100_000_000,
        days_back: int = 30,
        as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find recent large federal contract awards.

        Args:
            min_amount: Minimum award amount in USD (default $100M).
            days_back: How many days back to search.
            as_of: Reference date string (YYYY-MM-DD). Defaults to today if None.

        Returns:
            List of contract dicts (same format as search_contracts).
        """
        cache_key = f"recent_large|{min_amount}|{days_back}|{as_of}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        ref_date = datetime.strptime(as_of, "%Y-%m-%d") if as_of else datetime.fromisoformat(current_session_date())
        date_from = (ref_date - timedelta(days=days_back)).strftime("%Y-%m-%d")
        date_to = ref_date.strftime("%Y-%m-%d")

        results = self.search_contracts(
            date_from=date_from,
            date_to=date_to,
            min_amount=min_amount,
        )
        self._cache[cache_key] = results
        return results

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def clear_cache(self) -> None:
        """Clear all cached data."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Internal dispatch helpers
    # ------------------------------------------------------------------

    def _dispatch_search_contracts(self, params: dict[str, Any]) -> dict[str, Any]:
        return collection_envelope(self.search_contracts(
            keywords=params.get("keywords"), recipient=params.get("recipient"),
            date_from=params.get("date_from"), date_to=params.get("date_to"),
            min_amount=params.get("min_amount")))

    def _dispatch_recent_large(self, params: dict[str, Any]) -> dict[str, Any]:
        return collection_envelope(self.get_recent_large_contracts(
            min_amount=params.get("min_amount",100_000_000),
            days_back=params.get("days_back",30), as_of=params.get("as_of")))
