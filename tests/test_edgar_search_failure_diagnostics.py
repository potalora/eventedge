"""Semantic EFTS failures retain bounded cause evidence without provider text."""
from types import SimpleNamespace

import pytest

from tradingagents.strategies.data_sources import edgar_source as module
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.learning.event_monitor import EventMonitor


def hit(number=1):
    return {"_source": {"form": "SCHEDULE 13G", "file_date": "2026-10-09",
            "adsh": f"0000000001-26-{number:06d}", "ciks": ["0000000001"],
            "display_names": ["Example (EXM)"]}}


def page(hits, total):
    return {"hits": {"hits": hits, "total": {"value": total, "relation": "eq"}}}


def transport(monkeypatch, pages):
    responses = iter(pages)
    monkeypatch.setattr(module, "provider_request", lambda *a, **k:
                        SimpleNamespace(json=lambda: next(responses)))


@pytest.mark.parametrize("payload,branch", [
    ({}, "hits_shape"),
    (page([{}], 1), "hit_fields"),
    (page([hit()], True), "total_shape"),
])
def test_invalid_page_has_safe_fixed_diagnostic(monkeypatch, payload, branch):
    transport(monkeypatch, [payload])
    with pytest.raises(SourceFetchError) as raised:
        module.EDGARSource().search_filings("SCHEDULE 13G")
    error = raised.value
    assert error.reason_code == "invalid_response"
    coverage = error.partial_data["coverage"]
    assert coverage["complete"] is False
    assert coverage["diagnostic"]["branch"] == branch
    assert coverage["diagnostic"]["offset"] == 0
    assert "Example" not in str(coverage)


@pytest.mark.parametrize("second,total,branch", [
    ([hit(2)], 3, "total_changed"), ([], 2, "empty_page"),
])
def test_later_page_failure_retains_original_partial_population(monkeypatch, second, total, branch):
    transport(monkeypatch, [page([hit()], 2), page(second, total)])
    with pytest.raises(SourceFetchError) as raised:
        module.EDGARSource().search_filings("SCHEDULE 13G")
    error = raised.value
    assert [row["adsh"] for row in error.partial_data["filings"]] == [hit()["_source"]["adsh"]]
    assert error.partial_data["coverage"]["diagnostic"] == {
        "branch": branch, "offset": 1, "page_count": len(second),
        "expected_total": 2, "provider_total": total,
    }


def test_monitor_keeps_failure_branch_inside_frozen_window_coverage(monkeypatch):
    transport(monkeypatch, [page([hit()], 2), page([hit(2)], 3)])
    source = module.EDGARSource()
    monitor = EventMonitor(SimpleNamespace(get=lambda name: source))
    monitor.as_of = "2026-10-09"
    with pytest.raises(SourceFetchError) as raised:
        monitor.poll_edgar_filings(["SCHEDULE 13G"], fetch_text=False)
    rows = raised.value.partial_data["filings"]
    assert len(rows) == 1
    assert raised.value.failed_operations == {"form_0": "invalid_response"}
    assert rows.coverage["complete"] is False
    assert rows.coverage["windows"]["SCHEDULE 13G"]["diagnostic"]["branch"] == "total_changed"


def test_keyword_monitor_preserves_same_failed_window_diagnostic(monkeypatch):
    transport(monkeypatch, [page([hit()], 2), page([hit(2)], 3)])
    source = module.EDGARSource()
    monitor = EventMonitor(SimpleNamespace(get=lambda name: source))
    monitor.as_of = "2026-10-09"
    with pytest.raises(SourceFetchError) as raised:
        monitor.poll_keyword_filings(["SCHEDULE 13G"], ["ownership"], fetch_text=False)
    rows = raised.value.partial_data["pqc_filings"]
    assert len(rows) == 1 and rows.coverage["complete"] is False
    assert rows.coverage["windows"]["keyword_0_0"]["diagnostic"]["branch"] == "total_changed"


def test_invalid_fields_retain_flags_but_not_offending_provider_values(monkeypatch):
    bad = hit()
    bad["_source"].update(form="", file_date="sensitive-provider-text", ciks=["invalid-private-value"])
    transport(monkeypatch, [page([hit(), bad], 2)])
    with pytest.raises(SourceFetchError) as raised:
        module.EDGARSource().search_filings("SCHEDULE 13G")
    assert len(raised.value.partial_data["filings"]) == 1
    diagnostic = raised.value.partial_data["coverage"]["diagnostic"]
    assert diagnostic["hit_index"] == 1
    assert diagnostic["fields_valid"] == {
        "source": True, "form": False, "file_date": False,
        "adsh": True, "display_names": True, "ciks": False,
    }
    assert "private" not in str(diagnostic) and "sensitive" not in str(diagnostic)


def test_json_failure_has_fixed_branch_without_exception_message(monkeypatch):
    def invalid_json():
        raise ValueError("private provider payload")
    monkeypatch.setattr(module, "provider_request", lambda *a, **k:
                        SimpleNamespace(json=invalid_json))
    with pytest.raises(SourceFetchError) as raised:
        module.EDGARSource().search_filings("SCHEDULE 13G")
    assert raised.value.partial_data["coverage"]["diagnostic"] == {
        "branch": "json_decode", "offset": 0,
    }
    assert "private" not in str(raised.value)


@pytest.mark.parametrize("bad_cik", ["²", "9" * 5000], ids=["unicode_digit", "oversize_integer"])
def test_nonconvertible_digit_cik_is_safe_and_preserves_earlier_filings(monkeypatch, bad_cik):
    bad = hit(2)
    bad["_source"].update(form="", ciks=[bad_cik])
    transport(monkeypatch, [page([hit(), bad], 2)])
    with pytest.raises(SourceFetchError) as raised:
        module.EDGARSource().search_filings("SCHEDULE 13G")
    assert len(raised.value.partial_data["filings"]) == 1
    diagnostic = raised.value.partial_data["coverage"]["diagnostic"]
    assert diagnostic["branch"] == "hit_fields"
    assert diagnostic["fields_valid"]["ciks"] is False
    assert bad_cik not in str(diagnostic)
