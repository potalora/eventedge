"""Replay a bounded native recipient proof; recipient parents prove no ownership.

Digests detect accidental changes, not authenticity. Trust still comes from the
adapter's official HTTPS requests; this validator binds their retained bodies to
one unchanged award and a complete, bounded transaction result set.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
from typing import Any

from .fetch_errors import SourceFetchError, source_number, source_text

RECIPIENT_FALLBACK_POLICY = "exact_award_recipient_v1"
_API = "https://api.usaspending.gov/api/v2/"
_TRANSACTION_FIELDS = [
    "Award ID", "Recipient Name", "Recipient UEI", "recipient_id", "Action Date",
    "Transaction Amount", "Awarding Agency", "Transaction Description",
]


def canonical_json_sha256(value: Any) -> str:
    """Hash parsed JSON independently of response whitespace or object key order."""
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def detail_request_operation(url: str) -> str:
    """Match the adapter's bounded per-request failure operation identity."""
    return "award_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:58]


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("invalid recipient proof")


def _timestamp(value: Any) -> datetime:
    _require(isinstance(value, str))
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(result.tzinfo is not None and result.utcoffset() is not None)
    return result.astimezone(timezone.utc)


def _day(value: Any) -> date:
    _require(isinstance(value, str) and len(value) == 10)
    return date.fromisoformat(value)


def _money(value: Any) -> Decimal:
    _require(source_number(value))
    result = Decimal(str(value))
    _require(result == result.quantize(Decimal("0.01")))
    return result


def _envelope(item: Any, *, method: str, url: str, request: Any, status: int) -> datetime:
    _require(isinstance(item, dict))
    _require(item["method"] == method and item["url"] == url)
    _require(type(item["status"]) is int and item["status"] == status)
    _require(item["request"] == request)
    # GET must retain null, including when a caller supplied a false-like value.
    if method == "GET":
        _require(item["request"] is None)
    _require(isinstance(item["body"], dict))
    _require(item["body_sha256"] == canonical_json_sha256(item["body"]))
    return _timestamp(item["observed_at"])


def validate_recipient_fallback(contract: dict, *, now: datetime | None = None) -> dict:
    """Return an independent retained proof or a bounded invalid-response error.

    Old observations are valid for deterministic replay. Their internal order,
    ten-minute capture bound, and absence of future observations are enforced.
    No network, persistence, crosswalk, or parent inference happens here.
    """
    try:
        return _validate(contract, now=now)
    except (KeyError, TypeError, ValueError, OverflowError, ArithmeticError, RecursionError):
        raise SourceFetchError(
            "USAspending recipient fallback evidence is invalid",
            reason_code="invalid_response",
        ) from None


def _validate(contract: dict, *, now: datetime | None) -> dict:
    _require(isinstance(contract, dict))
    evidence = contract["recipient_identity_evidence"]
    _require(isinstance(evidence, dict) and evidence["policy"] == RECIPIENT_FALLBACK_POLICY)
    raw = evidence["search_award"]
    _require(isinstance(raw, dict))
    for field in ("Award ID", "Recipient Name", "Recipient UEI", "recipient_id",
                  "generated_internal_id", "Awarding Agency", "Description"):
        _require(source_text(raw[field]))
    generated_id = raw["generated_internal_id"]
    recipient_id = raw["recipient_id"]
    _require(re.fullmatch(r"CONT_AWD_[A-Za-z0-9_-]+", generated_id) is not None)
    _require(re.fullmatch(r"[A-Z0-9]{12}", raw["Recipient UEI"]) is not None)
    _require(re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}-C", recipient_id) is not None)
    _require(type(raw["internal_id"]) is int and raw["internal_id"] > 0)
    mapping = {
        "award_id": "Award ID", "award_key": "generated_internal_id",
        "generated_internal_id": "generated_internal_id", "internal_id": "internal_id",
        "recipient_name": "Recipient Name", "recipient_uei": "Recipient UEI",
        "recipient_id": "recipient_id", "agency": "Awarding Agency", "description": "Description",
    }
    for normalized, native in mapping.items():
        _require(type(contract[normalized]) is type(raw[native]) and contract[normalized] == raw[native])
    award_amount = _money(raw["Award Amount"])
    _require(award_amount > 0 and _money(contract["amount"]) == award_amount)
    base_day = _day(raw["Base Obligation Date"])
    _require(_day(contract["base_obligation_date"]) == base_day)
    _require(_day(contract["start_date"]) == _day(raw["Start Date"]))
    modified = raw["Last Modified Date"]
    _require(isinstance(modified, str))
    datetime.fromisoformat(modified)  # Validate the entire native date/time, not just its prefix.
    _require(_day(contract["last_modified_date"]) == _day(modified[:10]))
    _require(contract["award_scope"] == "new_awards_only")
    _require(contract["amount_basis"] == "cumulative_award_obligations")
    _require(contract["parent_recipient_uei"] == "")
    _require(contract["recipient_identity_status"] == "native_recipient_verified")
    profile_url = _API + f"recipient/{recipient_id}/?year=all"
    _require(contract["recipient_identity_source"] == profile_url)
    search_at = _timestamp(contract["observed_at"])
    _require(base_day <= search_at.date())
    # The provider's modification time is naive. Compare calendar days without
    # inventing a timezone, while rejecting evidence from a future day.
    _require(_day(modified[:10]) <= search_at.date())
    detail = evidence["detail_attempt"]
    detail_at = _envelope(detail, method="GET", url=_API + f"awards/{generated_id}/",
                          request=None, status=404)
    _require(detail["body"] == {"detail": "No Award found with: " +
                               repr({"generated_unique_award_id_legacy": generated_id})})
    _require(search_at <= detail_at)

    pages = evidence["transaction_pages"]
    _require(isinstance(pages, list) and 1 <= len(pages) <= 10)
    prior_at = detail_at
    seen_rows: set[str] = set()
    action_days = []
    amounts = []
    for page_number, page in enumerate(pages, start=1):
        request = {"filters": {"award_type_codes": ["A", "B", "C", "D"],
                               "award_unique_id": generated_id},
                   "fields": _TRANSACTION_FIELDS, "page": page_number, "limit": 10,
                   "sort": "Transaction Amount", "order": "desc"}
        page_at = _envelope(page, method="POST", url=_API + "search/spending_by_transaction/",
                            request=request, status=200)
        # JSON bools must not masquerade as numeric pagination fields.
        _require(type(page["request"]["page"]) is int and type(page["request"]["limit"]) is int)
        _require(prior_at <= page_at)
        prior_at = page_at
        body = page["body"]
        _require(type(body["limit"]) is int and body["limit"] == 10)
        rows, metadata = body["results"], body["page_metadata"]
        _require(isinstance(rows, list) and 1 <= len(rows) <= 10 and isinstance(metadata, dict))
        has_next = page_number < len(pages)
        _require(type(metadata["page"]) is int and metadata["page"] == page_number)
        _require(metadata["hasNext"] is has_next and metadata["hasPrevious"] is (page_number > 1))
        for key, wanted in (("next", page_number + 1 if has_next else None),
                            ("previous", page_number - 1 if page_number > 1 else None)):
            _require(type(metadata[key]) is type(wanted) and metadata[key] == wanted)
        _require(not has_next or len(rows) == 10)
        for row in rows:
            _require(isinstance(row, dict))
            row_hash = canonical_json_sha256(row)
            _require(row_hash not in seen_rows)
            seen_rows.add(row_hash)
            for native in ("Award ID", "Recipient Name", "Recipient UEI", "recipient_id",
                           "generated_internal_id", "internal_id", "Awarding Agency"):
                _require(type(row[native]) is type(raw[native]) and row[native] == raw[native])
            _require(row["Transaction Description"] == raw["Description"])
            action_day = _day(row["Action Date"])
            _require(base_day <= action_day <= search_at.date())
            action_days.append(action_day)
            amounts.append(_money(row["Transaction Amount"]))
    _require(min(action_days) == base_day and sum(amounts, Decimal(0)) == award_amount)
    _require(amounts == sorted(amounts, reverse=True))
    profile = evidence["recipient_profile"]
    profile_at = _envelope(profile, method="GET", url=profile_url, request=None, status=200)
    _require(prior_at <= profile_at and profile_at - search_at <= timedelta(minutes=10))
    current = now if now is not None else datetime.now(timezone.utc)
    _require(isinstance(current, datetime) and current.tzinfo is not None and current.utcoffset() is not None)
    _require(profile_at <= current.astimezone(timezone.utc))
    for key, native in (("name", "Recipient Name"), ("uei", "Recipient UEI"), ("recipient_id", "recipient_id")):
        _require(profile["body"][key] == raw[native])
    _require(profile["body"]["recipient_level"] == "C")
    return deepcopy(evidence)
