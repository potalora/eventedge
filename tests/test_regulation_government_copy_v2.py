"""V2 explicitly admits finite FDA/FTC notice forms; V1 keeps its EPA scope."""

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.scheduled_acceptance

from tradingagents.strategies.data_sources.regulation_government_copy import (
    extract_government_copy, government_copy_plan,
)
from tradingagents.strategies.data_sources.regulation_evidence import validate_regulation_source
from tradingagents.strategies.orchestration.regulation_inputs import validate_regulation_policy

FIXTURES = Path(__file__).parent / "fixtures"
V2 = "verified_government_copy_v2"
CASES = [("fda_traceability", "FDA-2025-D-6952-0001", "2026-20721", "FDA"),
         ("ftc_impersonation", "FTC-2026-1552-0001", "2026-20143", "FTC"),
         ("ftc_petition", "FTC-2026-1453-0001", "2026-20078", "FTC")]


def originals(label):
    return ((FIXTURES / ("regulation_detail_" + label + ".json")).read_bytes(),
            (FIXTURES / ("regulation_govinfo_" + label + ".htm")).read_bytes())


def crosswalk_arguments(label):
    if label != "ftc_impersonation":
        return {}
    from tradingagents.strategies.data_sources.regulation_evidence import original_receipt
    return {"crosswalk_receipt": original_receipt(
        (FIXTURES / "regulation_fr_crosswalk_ftc_impersonation.json").read_bytes(),
        observed_at="2026-10-15T02:00:00+00:00",
        url="https://www.federalregister.gov/api/v1/documents/2026-20143.json")}


@pytest.mark.parametrize("label,docid,fr,agency", CASES)
def test_explicit_v2_extracts_only_complete_same_document_notice(label, docid, fr, agency):
    detail, body = originals(label)
    result = extract_government_copy(detail, body, retrieval_policy=V2, **crosswalk_arguments(label))
    assert result["identity"]["document_id"] == docid
    assert result["identity"]["agency_id"] == agency
    assert result["identity"]["fr_document_number"] == fr
    assert result["extraction_version"] == "govinfo-fr-pre-v2"
    assert result["text"].endswith("BILLING CODE " + ("4164-01-P" if agency == "FDA" else "6750-01-P") + "\n\n\n")
    assert "SUPPLEMENTARY INFORMATION:" in result["text"]
    if agency == "FTC":
        assert result["identity"]["catalogue_page_count"] == 0
        assert result["identity"]["page_count"] > 0
    with pytest.raises(ValueError, match="(?:invalid|unsupported)_regulation_"):
        extract_government_copy(detail, body)


def acquire(monkeypatch, label, *, metadata_raw=None, metadata_status=200, conflicting_duplicate=False):
    from tradingagents.strategies.data_sources import regulations_source as source
    from test_regulation_source_evidence import Response
    detail, body = originals(label)
    item = json.loads(detail)["data"]
    attributes, docid = item["attributes"], item["id"]
    original_url = next(row["fileUrl"] for row in attributes["fileFormats"] if row["format"] == "html")
    copy_url = government_copy_plan(detail, retrieval_policy=V2)["url"]
    discovered = [{"id": docid, "attributes": attributes}]
    discovery = json.dumps({"data": discovered,
                           "meta": {"totalPages": 1, "totalElements": len(discovered)}}).encode()
    transport = {source.BASE_URL + "/documents": (discovery, 200),
                 source.BASE_URL + "/documents/" + docid: (detail, 200),
                 original_url: (b"Access denied", 403), copy_url: (body, 200)}
    if label == "ftc_impersonation":
        transport["https://www.federalregister.gov/api/v1/documents/2026-20143.json"] = (
            (FIXTURES / "regulation_fr_crosswalk_ftc_impersonation.json").read_bytes()
                if metadata_raw is None else metadata_raw, metadata_status)
    discovery_calls = []
    def request(provider, method, url, **kwargs):
        assert provider == "regulations" and kwargs["allow_redirects"] is False
        if url == copy_url or url.startswith("https://www.federalregister.gov/"):
            assert "api_key" not in kwargs.get("params", {})
            assert kwargs["headers"] == {}
        raw, status = transport[url]
        if conflicting_duplicate and url == source.BASE_URL + "/documents":
            if discovery_calls:
                raw = json.dumps({"data": [{"id": docid, "attributes": {
                    **attributes, "title": "Conflicting title"}}],
                    "meta": {"totalPages": 1, "totalElements": 1}}).encode()
            discovery_calls.append(True)
        return Response(raw, status=status)
    monkeypatch.setattr(source, "provider_request", request)
    return source.RegulationsSource(api_key="fixture-key").get_recent_proposed_rules(
        agencies=[attributes["agencyId"]] * (2 if conflicting_duplicate else 1),
        days_back=14, as_of="2026-10-14",
        evidence_policy="complete_document_text_v1", retrieval_policy=V2)


@pytest.mark.parametrize("label,docid,fr,agency", CASES)
def test_v2_acquisition_replays_and_requires_matching_frozen_policy(monkeypatch, label, docid, fr, agency):
    rules = acquire(monkeypatch, label)
    payload = {"proposed_rules": list(rules), "coverage": rules.coverage,
               "regulation_evidence": rules.regulation_evidence}
    assert validate_regulation_source(payload)["content_complete"] is True
    assert payload["regulation_evidence"]["retrieval_policy"] == V2
    unit = payload["regulation_evidence"]["corpus"][docid]["units"][0]
    assert unit["primary_failure"]["http_status"] == 403
    if label == "ftc_impersonation":
        assert unit["government_copy"]["crosswalk_receipt"]["bytes"] > 0
    config = {"regulation_evidence_policy": "complete_document_text_v1", "regulation_retrieval_policy": V2}
    assert validate_regulation_policy({"regulations": payload}, config)["retrieval_policy"] == V2
    with pytest.raises(ValueError, match="invalid_regulation_evidence_policy"):
        validate_regulation_policy({"regulations": payload},
            {**config, "regulation_retrieval_policy": "verified_government_copy_v1"})


@pytest.mark.parametrize("raw,status", [(b"Access denied", 403), (b"{not-json", 200),
    (b"{}", 200), (b"x" * (2 * 1024**2 + 1), 200)])
def test_unavailable_invalid_or_oversized_official_crosswalk_stays_failed(monkeypatch, raw, status):
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    with pytest.raises(SourceFetchError) as refused:
        acquire(monkeypatch, "ftc_impersonation", metadata_raw=raw, metadata_status=status)
    payload = refused.value.partial_data
    assert validate_regulation_source(payload)["content_complete"] is False
    failure = payload["regulation_evidence"]["acquisition_failures"]["FTC-2026-1552-0001"]
    assert len(failure["retrieval_attempts"]) == 3
    assert failure["retrieval_attempts"][-1]["receipt"]["url"] == "https://www.federalregister.gov/api/v1/documents/2026-20143.json"


def test_conflicting_duplicate_retains_original_official_crosswalk_in_failed_replay(monkeypatch):
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    from tradingagents.strategies.data_sources.regulation_evidence import receipt_bytes, MAX_DETAIL_BYTES
    with pytest.raises(SourceFetchError) as refused:
        acquire(monkeypatch, "ftc_impersonation", conflicting_duplicate=True)
    payload = refused.value.partial_data
    scope = validate_regulation_source(payload)
    assert scope["content_complete"] is False
    assert scope["complete_rows"] == 0 and scope["failed_rows"] == scope["required_rows"] == 2
    assert payload["regulation_evidence"]["corpus"] == {}
    failure = payload["regulation_evidence"]["acquisition_failures"]["FTC-2026-1552-0001"]
    assert failure["failure"]["code"] == "invalid_regulation_identity"
    attempts = failure["retrieval_attempts"]
    assert [row["receipt"]["url"] for row in attempts] == [
        "https://downloads.regulations.gov/FTC-2026-1552-0001/content.html",
        "https://www.govinfo.gov/content/pkg/FR-2026-10-01/html/2026-20143.htm",
        "https://www.federalregister.gov/api/v1/documents/2026-20143.json",
    ]
    assert attempts[-1]["http_status"] == 200 and attempts[-1]["complete"] is True
    assert receipt_bytes(attempts[-1]["receipt"], MAX_DETAIL_BYTES) == (
        FIXTURES / "regulation_fr_crosswalk_ftc_impersonation.json").read_bytes()


@pytest.mark.parametrize("field,value", [("agencyId", "SEC"), ("docketId", "FTC-other"),
    ("pageCount", 9), ("startEndPage", None), ("cfrPart", None), ("frDocNum", "2026-20079"),
    ("postedDate", "2026-10-01T04:00:00Z"), ("restrictReason", "restricted")])
def test_v2_metadata_changes_cannot_bind_wrong_notice(field, value):
    detail, body = originals("ftc_petition")
    document = json.loads(detail)
    document["data"]["attributes"][field] = value
    with pytest.raises(ValueError, match="(?:invalid|unsupported)_regulation_"):
        extract_government_copy(json.dumps(document).encode(), body, retrieval_policy=V2)


@pytest.mark.parametrize("old,new", [
    (b"Federal Trade Commission.", b"Other Commission."),
    (b"ACTION: Receipt of petition; request for comment.", b"ACTION: Final rule."),
    (b"FTC-\n2026-1453", b"FTC-\n2026-1454"),
    (b"[Page 61811]", b"[Page 61812]"),
    (b"SUPPLEMENTARY INFORMATION:", b"SUPPLEMENTARY REMARKS:"),
    (b"[FR Doc. 2026-20078 Filed", b"[FR Doc. 2026-20079 Filed"),
    (b"SUMMARY:", b"[GRAPHIC] [TIFF OMITTED] TP30SE26.001\nSUMMARY:"),
])
def test_v2_finite_body_grammar_and_material_checks_remain_strict(old, new):
    detail, body = originals("ftc_petition")
    assert old in body
    with pytest.raises(ValueError, match="(?:invalid|unsupported)_regulation_"):
        extract_government_copy(detail, body.replace(old, new), retrieval_policy=V2)


def test_v2_literal_url_query_delimiter_preserves_visible_publisher_link():
    detail, body = originals("ftc_impersonation")
    result = extract_government_copy(detail, body, retrieval_policy=V2, **crosswalk_arguments("ftc_impersonation"))
    assert "https://www.reginfo.gov/public/do/eAgendaViewRule?pubId=202510&RIN=3084-AB71" in result["text"]


@pytest.mark.parametrize("replacement", [b"&unknown;", b"&amp=3084-AB71", b"&RIN<script>=3084-AB71"])
def test_v2_literal_query_rule_cannot_hide_entities_or_embedded_markup(replacement):
    detail, body = originals("ftc_impersonation")
    with pytest.raises(ValueError, match="(?:invalid|unsupported)_regulation_"):
        extract_government_copy(detail, body.replace(b"&RIN=3084-AB71", replacement), retrieval_policy=V2)


@pytest.mark.parametrize("field,value", [("document_id", "FTC-2026-1453-0001"),
    ("docket_id", "FTC-2026-1453"), ("agency_id", "EPA"), ("regulation_id_number", "3084-AB91")])
def test_official_crosswalk_cannot_bind_another_document_or_rin(field, value):
    from tradingagents.strategies.data_sources.regulation_evidence import original_receipt
    detail, body = originals("ftc_impersonation")
    metadata = json.loads((FIXTURES / "regulation_fr_crosswalk_ftc_impersonation.json").read_bytes())
    metadata["regulations_dot_gov_info"][field] = value
    receipt = original_receipt(json.dumps(metadata).encode(), observed_at="2026-10-15T02:00:00+00:00",
        url="https://www.federalregister.gov/api/v1/documents/2026-20143.json")
    with pytest.raises(ValueError, match="invalid_regulation_government_crosswalk"):
        extract_government_copy(detail, body, retrieval_policy=V2, crosswalk_receipt=receipt)


@pytest.mark.parametrize("label", ["sec_performance", "sec_custody", "sec_interval", "epa_pdf_correction"])
def test_actual_remaining_unsupported_notices_keep_failed_coverage(monkeypatch, label):
    from tradingagents.strategies.data_sources import regulations_source as source
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    from test_regulation_source_evidence import Response
    detail = (FIXTURES / ("regulation_detail_" + label + ".json")).read_bytes()
    item = json.loads(detail)["data"]
    docid, attrs = item["id"], item["attributes"]
    primary = next((row for row in attrs["fileFormats"] if row["format"] == "html"), attrs["fileFormats"][0])
    discovery = json.dumps({"data": [{"id": docid, "attributes": attrs}],
        "meta": {"totalPages": 1, "totalElements": 1}}).encode()
    transport = {source.BASE_URL + "/documents": (discovery, 200),
        source.BASE_URL + "/documents/" + docid: (detail, 200), primary["fileUrl"]: (b"Access denied", 403)}
    def request(provider, method, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        raw, status = transport[url]  # No guessed alternate may be requested.
        return Response(raw, status=status)
    monkeypatch.setattr(source, "provider_request", request)
    with pytest.raises(SourceFetchError) as refused:
        source.RegulationsSource(api_key="fixture-key").get_recent_proposed_rules(
            agencies=[attrs["agencyId"]], days_back=14, as_of="2026-10-14",
            evidence_policy="complete_document_text_v1", retrieval_policy=V2)
    payload = refused.value.partial_data
    scope = validate_regulation_source(payload)
    assert scope["content_complete"] is False and scope["failed_rows"] == scope["required_rows"] == 1
    assert scope["complete_rows"] == 0
    failure = payload["regulation_evidence"]["acquisition_failures"][docid]
    assert failure["retrieval_attempts"][0]["http_status"] == 403
    if attrs["agencyId"] == "SEC":
        assert failure["failure"]["code"] == "unsupported_regulation_government_notice"
    else:
        assert primary["format"] == "pdf" and attrs["frDocNum"] is None
        assert failure["failure"]["http_status"] == 403
