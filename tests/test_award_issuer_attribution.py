"""Native recipient identity must survive source parsing and admission."""
from unittest.mock import patch

import pytest

from tradingagents.strategies.data_sources import usaspending_source as usa
from tradingagents.strategies.data_sources.award_identity import resolve_award_issuer
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.modules.govt_contracts import GovtContractsStrategy


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def award(**changes):
    return dict({
        "Award ID": "NEW-PIID", "Recipient Name": "BOEING COUNTY PLUMBING LLC",
        "Award Amount": 250_000_000, "Base Obligation Date": "2026-10-01",
        "generated_internal_id": "CONT_AWD_NEW-PIID_9700_-NONE-_-NONE-",
        "internal_id": 42,
    }, **changes)


def parse_and_screen(row, detail=None):
    search = Response({
        "results": [row], "page_metadata": {"page": 1, "hasNext": False},
    })
    responses = [search] + ([Response(detail)] if detail is not None else [])
    with patch.object(usa, "provider_request", side_effect=responses):
        records = usa.USASpendingSource()._search_contracts_page(
            date_from="2026-09-09", date_to="2026-10-09")
    return GovtContractsStrategy().screen(
        {"usaspending": {"data": {"contracts": records}}}, "2026-10-09", {})


def test_boeing_substring_is_retained_without_actionable_issuer():
    candidates = parse_and_screen(award())
    assert len(candidates) == 1
    assert candidates[0].ticker == ""
    assert candidates[0].journal_only
    assert candidates[0].metadata["non_actionable_reason"] == "unverified_recipient_issuer"
    discovery = candidates.admission_manifest["discovered"]
    assert len(discovery) == 1 and discovery[0]["journal_only"]
    assert candidates[0].metadata["award_key"] == "CONT_AWD_NEW-PIID_9700_-NONE-_-NONE-"
    assert discovery[0]["event_key"].startswith("event_key_")


def test_reviewed_native_recipient_uei_resolves_without_name_matching():
    candidates = parse_and_screen(award(**{
        "Recipient Name": "THE BOEING COMPANY", "Recipient UEI": "WZWRLY4G3PL8",
        "recipient_id": "22d282c6-4508-7d30-97f1-7401ea73b12a-C",
    }))
    assert len(candidates) == 1 and candidates[0].ticker == "BA"
    assert not candidates[0].journal_only
    attribution = candidates[0].metadata["issuer_attribution"]
    assert attribution["verified"] and attribution["cik"] == "0000012927"
    assert attribution["matched_uei"] == "WZWRLY4G3PL8"
    assert attribution["relationship"] == "recipient"
    assert any("sec.gov/Archives" in url for url in attribution["provenance"])


CHILD_ID = "df8638e3-b642-fae8-6730-21c1ec716038-C"


def subsidiary_detail(**changes):
    return dict({
        "id": 42,
        "generated_unique_award_id": "CONT_AWD_NEW-PIID_9700_-NONE-_-NONE-",
        "recipient": {
            "recipient_hash": CHILD_ID, "recipient_name": "BOEING SPACE OPERATIONS",
            "recipient_uei": "M6BRZ1FHEZQ1",
            "parent_recipient_uei": "NU2UC8MX6NK1",
            "parent_recipient_name": "THE BOEING COMPANY",
        },
    }, **changes)


def child_award():
    return award(**{"Recipient Name": "BOEING SPACE OPERATIONS",
                    "Recipient UEI": "M6BRZ1FHEZQ1", "recipient_id": CHILD_ID})


def test_award_specific_native_parent_uei_verifies_subsidiary_through_admission():
    candidates = parse_and_screen(child_award(), subsidiary_detail())
    assert len(candidates) == 1 and candidates[0].ticker == "BA"
    assert not candidates[0].journal_only
    metadata = candidates[0].metadata
    assert metadata["issuer_attribution"]["relationship"] == "parent"
    assert metadata["issuer_attribution"]["matched_uei"] == "NU2UC8MX6NK1"
    assert metadata["recipient_identity_status"] == "native_award_verified"
    assert candidates.admission_manifest["admitted"][0]["journal_only"] is False


def test_sikorsky_native_parent_identifies_lockheed_without_subsidiary_name_alias():
    # Identifiers/relationship from the native SPE4A125F1406 award, independently
    # observed 2026-10-09. Amount/date/key below remain synthetic test economics.
    row = award(**{"Recipient Name": "SIKORSKY AIRCRAFT CORPORATION",
                   "Recipient UEI": "UTJWTSLMFNG4",
                   "recipient_id": "d64537c9-5482-dd40-cdd4-5cf07b66329d-C"})
    detail = subsidiary_detail()
    detail["recipient"].update(recipient_hash="d64537c9-5482-dd40-cdd4-5cf07b66329d-C",
                               recipient_name="SIKORSKY AIRCRAFT CORPORATION",
                               recipient_uei="UTJWTSLMFNG4",
                               parent_recipient_uei="ZFN2JJXBLZT3",
                               parent_recipient_name="LOCKHEED MARTIN CORP")
    candidates = parse_and_screen(row, detail)
    assert len(candidates) == 1 and candidates[0].ticker == "LMT"
    assert not candidates[0].journal_only
    assert candidates[0].metadata["issuer_attribution"]["relationship"] == "parent"


@pytest.mark.parametrize("field,value", [
    ("generated_unique_award_id", "CONT_AWD_DIFFERENT"),
    ("id", 43),
    ("recipient_uei", "WZWRLY4G3PL8"),
    ("recipient_hash", "different-recipient-C"),
    ("parent_recipient_uei", ["NU2UC8MX6NK1", "ZFN2JJXBLZT3"]),
])
def test_mismatched_or_ambiguous_detail_cannot_attribute_award(field, value):
    detail = subsidiary_detail()
    if field in ("generated_unique_award_id", "id"):
        detail[field] = value
    else:
        detail["recipient"][field] = value
    candidates = parse_and_screen(child_award(), detail)
    assert len(candidates) == 1 and candidates[0].ticker == ""
    assert candidates[0].journal_only
    assert not candidates[0].metadata["issuer_attribution"]["verified"]


def test_native_unrelated_recipient_is_explicitly_unresolved_despite_name():
    row = child_award()
    row["Recipient Name"] = "BOEING COUNTY PLUMBING LLC"
    detail = subsidiary_detail()
    detail["recipient"]["recipient_name"] = row["Recipient Name"]
    detail["recipient"]["parent_recipient_uei"] = "UNKNOWN12345"
    candidates = parse_and_screen(row, detail)
    assert candidates[0].ticker == "" and candidates[0].journal_only
    assert candidates[0].metadata["non_actionable_reason"] == "unverified_recipient_issuer"


def test_name_only_and_caller_supplied_ticker_never_verify_issuer():
    contract = {
        "award_id": "NEW-PIID", "award_key": "CONT_AWD_NEW-PIID_9700_-NONE-_-NONE-",
        "recipient_name": "THE BOEING COMPANY", "ticker": "BA",
        "parent_recipient_uei": "NU2UC8MX6NK1",  # no bound native relationship
        "issuer_attribution": {"verified": True, "ticker": "BA"},
        "amount": 250_000_000, "base_obligation_date": "2026-10-01",
        "award_scope": "new_awards_only", "amount_basis": "cumulative_award_obligations",
        "observed_at": "2026-10-09T00:00:00+00:00",
    }
    candidates = GovtContractsStrategy().screen(
        {"usaspending": {"data": {"contracts": [contract]}}}, "2026-10-09", {})
    assert candidates[0].ticker == "" and candidates[0].journal_only


def test_conflicting_verified_recipient_and_parent_are_not_actionable():
    result = resolve_award_issuer({
        "recipient_uei": "WZWRLY4G3PL8", "parent_recipient_uei": "ZFN2JJXBLZT3",
        "recipient_identity_status": "native_award_verified",
    })
    assert result == {"verified": False, "reason": "conflicting_recipient_issuer"}


def test_failed_native_identity_lookup_preserves_award_and_coverage_without_error_text():
    search = Response({"results": [child_award()], "page_metadata": {"page": 1, "hasNext": False}})
    with patch.object(usa, "provider_request", side_effect=[
        search, SourceFetchError("private-provider-body", reason_code="timeout"),
    ]):
        records = usa.USASpendingSource().search_contracts(
            date_from="2026-09-09", date_to="2026-10-09")
    assert len(records) == 1 and records[0]["recipient_identity_status"] == "lookup_failed"
    assert records.coverage["complete"]  # award window exhausted
    identity = records.coverage["issuer_attribution"]
    assert identity["lookup_failures"] == 1 and identity["unresolved"] == 1
    assert not identity["complete"]
    assert "private-provider-body" not in repr(records)
    candidates = GovtContractsStrategy().screen(
        {"usaspending": {"data": {"contracts": records}}}, "2026-10-09", {})
    assert candidates[0].ticker == "" and candidates[0].journal_only


def test_award_search_requests_native_identity_without_changing_new_award_basis():
    with patch.object(usa, "provider_request", return_value=Response({
        "results": [], "page_metadata": {"page": 1, "hasNext": False},
    })) as request:
        usa.USASpendingSource().search_contracts(date_from="2026-09-09", date_to="2026-10-09")
    payload = request.call_args.kwargs["json"]
    assert {"Recipient UEI", "recipient_id", "Base Obligation Date"} <= set(payload["fields"])
    assert payload["filters"]["time_period"] == [{
        "start_date": "2026-09-09", "end_date": "2026-10-09", "date_type": "new_awards_only",
    }]


def test_unknown_recipient_is_retained_even_when_analysis_budget_excludes_it():
    candidates = parse_and_screen(award())
    again = GovtContractsStrategy().screen({"usaspending": {"data": {"contracts": [{
        "award_id": "NEW-PIID", "award_key": "CONT_AWD_NEW-PIID_9700_-NONE-_-NONE-",
        "recipient_name": "BOEING COUNTY PLUMBING LLC", "amount": 250_000_000,
        "base_obligation_date": "2026-10-01", "award_scope": "new_awards_only",
        "amount_basis": "cumulative_award_obligations", "observed_at": "2026-10-09T00:00:00+00:00",
    }]}}}, "2026-10-09", {"analysis_budget": 0})
    assert len(candidates) == 1 and len(again) == 0
    assert len(again.admission_manifest["discovered"]) == 1
    assert again.admission_manifest["excluded"][0]["journal_only"]
    assert again.admission_manifest["excluded"][0]["reason"] == "analysis_budget"


def test_verified_awards_are_admitted_before_larger_unknowns_deterministically():
    rows = [award(**{
        "Award ID": f"NEW-{number}",
        "generated_internal_id": f"CONT_AWD_NEW-{number}_9700_-NONE-_-NONE-",
        "internal_id": number, "Recipient Name": name,
        "Award Amount": amount,
        **({"Recipient UEI": uei} if uei else {}),
    }) for number, name, amount, uei in [
        (1, "UNVERIFIED PRIVATE A", 900_000_000, ""),
        (2, "UNVERIFIED PRIVATE B", 800_000_000, ""),
        (3, "UNVERIFIED PRIVATE C", 700_000_000, ""),
        (4, "THE BOEING COMPANY", 250_000_000, "WZWRLY4G3PL8"),
        (5, "LOCKHEED MARTIN CORP", 150_000_000, "H7PNSVNN5827"),
    ]]
    populations = []
    for ordered_rows in (rows, list(reversed(rows))):
        with patch.object(usa, "provider_request", return_value=Response({
            "results": ordered_rows, "page_metadata": {"page": 1, "hasNext": False},
        })):
            records = usa.USASpendingSource()._search_contracts_page(
                date_from="2026-09-09", date_to="2026-10-09")
        populations.append(GovtContractsStrategy().screen(
            {"usaspending": {"data": {"contracts": records}}},
            "2026-10-09", {"analysis_budget": 3}))
    for candidates in populations:
        assert [candidate.ticker for candidate in candidates] == ["BA", "LMT", ""]
        assert [candidate.metadata["award_id"] for candidate in candidates] == [
            "NEW-4", "NEW-5", "NEW-1"]
        assert candidates[2].journal_only
        assert candidates[2].metadata["non_actionable_reason"] == "unverified_recipient_issuer"
        manifest = candidates.admission_manifest
        assert manifest["policy"] == "verified_issuer_first_score_desc_ticker_source_identity_v1"
        assert len(manifest["discovered"]) == 5
        assert len(manifest["excluded"]) == 2
        assert all(row["journal_only"] and row["reason"] == "analysis_budget"
                   for row in manifest["excluded"])
    assert populations[0].admission_manifest == populations[1].admission_manifest
