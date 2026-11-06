"""Retained public EPA notices expose finite publisher-layout defects."""

import json
from pathlib import Path
import re

import pytest

pytestmark = pytest.mark.scheduled_acceptance

from tradingagents.strategies.data_sources.regulation_government_copy import (
    extract_government_copy,
    government_copy_plan,
)

FIXTURES = Path(__file__).parent / "fixtures"


def originals(label):
    return ((FIXTURES / ("regulation_detail_" + label + ".json")).read_bytes(),
            (FIXTURES / ("regulation_govinfo_" + label + ".htm")).read_bytes())


def test_oregon_notice_without_decorative_equal_separator_is_complete():
    detail, body = originals("oregon")
    result = extract_government_copy(detail, body)
    assert result["identity"]["document_id"] == "EPA-R10-OAR-2026-3763-0012"
    assert "Air Plan Approval; OR; Lane County Title 36 Rule" in result["text"]
    assert re.findall(r"^\[\[Page ([0-9]+)\]\]$", result["text"], re.M) == ["62381", "62382", "62383"]
    assert result["text"].endswith("BILLING CODE 6560-50-P\n\n\n")


def test_arizona_notice_preserves_first_page_marker_and_superscript_footnotes():
    detail, body = originals("arizona")
    result = extract_government_copy(detail, body)
    assert re.findall(r"^\[\[Page ([0-9]+)\]\]$", result["text"], re.M) == ["64321", "64322", "64323", "64324"]
    (span,) = [row for row in result["transformations"] if row["kind"] == "govinfo_sup_notation_v1"]
    assert span["value"] == "4 5"
    assert body.decode()[span["source_codepoint_start"]:span["source_codepoint_end"]] == "<SUP>4 5</SUP>"
    assert result["text"][span["text_codepoint_start"]:span["text_codepoint_end"]] == "4 5"
    assert "emissions increases.4 5" in result["text"]
    assert result["identity"]["document_id"] == "EPA-R09-OAR-2026-4654-0001"


@pytest.mark.parametrize("old,new", [
    (b"<SUP>4 5</SUP>", b"<SUP hidden=\"yes\">4 5</SUP>"),
    (b"<SUP>4 5</SUP>", b"<SUP>unparsed material</SUP>"),
    (b"<SUP>4 5</SUP>", b"<SUP><a href=\"https://example.test\">4</a></SUP>"),
    (b"[[Page 64321]]", b"[[Page 64320]]"),
    (b"[[Page 64321]]", b"[[Page 64321]]\n[[Page 64321]]"),
    (b"[[Page 64323]]", b""),
    (b"[[Page 64323]]", b"[[Page 64324]]"),
])
def test_new_publisher_layout_support_keeps_markup_and_page_checks_strict(old, new):
    detail, body = originals("arizona")
    with pytest.raises(ValueError, match="(?:invalid|unsupported)_regulation_"):
        extract_government_copy(detail, body.replace(old, new))


def test_first_page_marker_must_stay_before_agency_preamble():
    detail, body = originals("arizona")
    moved = body.replace(b"[[Page 64321]]\n", b"").replace(
        b"SUMMARY:", b"[[Page 64321]]\n\nSUMMARY:", 1)
    with pytest.raises(ValueError, match="invalid_regulation_government_pages"):
        extract_government_copy(detail, moved)


@pytest.mark.parametrize("label", ["oregon", "arizona"])
def test_retained_notice_passes_actual_source_wrapper_and_model_request(monkeypatch, label):
    from tradingagents.strategies.data_sources import regulations_source as source
    from tradingagents.strategies.data_sources.regulation_assessment import prepare_request
    from tradingagents.strategies.data_sources.regulation_evidence import validate_regulation_source
    from test_regulation_source_evidence import Response

    detail, body = originals(label)
    item = json.loads(detail)["data"]
    docid, attributes = item["id"], item["attributes"]
    original_url = next(row["fileUrl"] for row in attributes["fileFormats"] if row["format"] == "html")
    copy_url = government_copy_plan(detail)["url"]
    discovery = json.dumps({"data": [{"id": docid, "attributes": attributes}],
                            "meta": {"totalPages": 1, "totalElements": 1}}).encode()
    transport = {source.BASE_URL + "/documents": (discovery, 200),
                 source.BASE_URL + "/documents/" + docid: (detail, 200),
                 original_url: (b"Access denied", 403), copy_url: (body, 200)}
    def request(provider, method, url, **kwargs):
        assert provider == "regulations" and kwargs["allow_redirects"] is False
        if url == copy_url:
            assert "api_key" not in kwargs.get("params", {})
        raw, status = transport[url]
        return Response(raw, status=status)
    monkeypatch.setattr(source, "provider_request", request)
    rules = source.RegulationsSource(api_key="fixture-key").get_recent_proposed_rules(
        agencies=["EPA"], days_back=14, as_of="2026-10-14",
        evidence_policy="complete_document_text_v1", retrieval_policy="verified_government_copy_v1")
    payload = {"proposed_rules": list(rules), "coverage": rules.coverage,
               "regulation_evidence": rules.regulation_evidence}
    assert validate_regulation_source(payload)["content_complete"] is True
    evidence = rules.regulation_evidence["corpus"][docid]
    unit = evidence["units"][0]
    assert unit["primary_failure"]["http_status"] == 403
    assert unit["primary_failure"]["receipt"]["bytes"] == len(b"Access denied")
    projected = json.loads(prepare_request(evidence).user)["units"][0]
    if label == "arizona":
        (annotation,) = [span for span in projected["text_annotations"] if span["kind"] == "superscript"]
        assert projected["text"][annotation["text_codepoint_start"]:annotation["text_codepoint_end"]] == "4 5"
