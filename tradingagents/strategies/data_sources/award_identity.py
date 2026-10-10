"""Reviewed native UEIs, never recipient-name guesses.

This deliberately small crosswalk is a source-coverage limit. Add identifiers
only with primary recipient/issuer evidence; a contractor name is not an ID.
Parent links must come from the *same award's* native detail, not the recipient
profile's potentially multiple historical parents.
"""
from __future__ import annotations

import re


_BOEING_LISTING = "https://www.sec.gov/Archives/edgar/data/12927/000162828026004357/ba-20251231.htm"
_LOCKHEED_LISTING = "https://www.sec.gov/Archives/edgar/data/936468/000162828026004195/lmt-20251231.htm"
_BOEING_RECIPIENT = "https://api.usaspending.gov/api/v2/recipient/22d282c6-4508-7d30-97f1-7401ea73b12a-C/"
_LOCKHEED_AWARD = "https://api.usaspending.gov/api/v2/awards/CONT_AWD_FA868224CB001_9700_-NONE-_-NONE-/"

# Independently reviewed 2026-10-09. Recipient -> legal issuer is established
# by the issuer's own UEI publication / federal award notice. Award-specific
# parent links establish the two parent UEIs; SEC covers CIK and common stock.
VERIFIED_UEI_ISSUERS = {
    "WZWRLY4G3PL8": {
        "ticker": "BA", "cik": "0000012927", "issuer_name": "THE BOEING COMPANY",
        "provenance": [
            "https://www.boeingsuppliers.com/content/dam/boeing/boeingsuppliers/boeing-suppliers/becoming/terms/ccr/p/P1229-CCR-8-30-22.pdf",
            _BOEING_LISTING,
        ],
    },
    "NU2UC8MX6NK1": {
        "ticker": "BA", "cik": "0000012927", "issuer_name": "THE BOEING COMPANY",
        "provenance": [_BOEING_RECIPIENT, _BOEING_LISTING],
    },
    "H7PNSVNN5827": {
        "ticker": "LMT", "cik": "0000936468", "issuer_name": "LOCKHEED MARTIN CORPORATION",
        "provenance": [
            "https://sam.gov/opp/c22a155c16d6429baf021aeaf7d66c47/view",
            _LOCKHEED_LISTING,
        ],
    },
    "ZFN2JJXBLZT3": {
        "ticker": "LMT", "cik": "0000936468", "issuer_name": "LOCKHEED MARTIN CORPORATION",
        "provenance": [_LOCKHEED_AWARD, _LOCKHEED_LISTING],
    },
}


def native_uei(value: object) -> str:
    """UEIs are twelve uppercase alphanumeric characters, with no coercion."""
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z0-9]{12}", value) else ""


def resolve_award_issuer(contract: dict) -> dict:
    """Resolve only native identifiers; never trust a supplied ticker/flag."""
    recipient = native_uei(contract.get("recipient_uei"))
    parent = (native_uei(contract.get("parent_recipient_uei"))
              if contract.get("recipient_identity_status") == "native_award_verified" else "")
    direct, ultimate = VERIFIED_UEI_ISSUERS.get(recipient), VERIFIED_UEI_ISSUERS.get(parent)
    if direct and ultimate and direct["cik"] != ultimate["cik"]:
        return {"verified": False, "reason": "conflicting_recipient_issuer"}
    identity = direct or ultimate
    if not identity:
        return {"verified": False, "reason": "unverified_recipient_issuer"}
    return {**identity, "provenance": list(identity["provenance"]), "verified": True,
            "matched_uei": recipient if direct else parent,
            "relationship": "recipient" if direct else "parent",
            "crosswalk_version": "reviewed_2026-10-09",
            "relationship_source": contract.get("recipient_identity_source", "")}
