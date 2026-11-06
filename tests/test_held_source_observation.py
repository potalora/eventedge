"""Synthetic failed acquisition receipts validate monitoring only."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from test_focused_courtlistener import scope, docket
from tradingagents.strategies.data_sources.courtlistener_scope import (
    CONTENT_KIND, POLICY, declared_queries, digest, evidence_digest,
)


def partial_court():
    selected = scope(("A Corp", "B Corp"))
    definitions = declared_queries(selected)
    native = docket()
    native["dateFiled"] = "2026-10-09"
    rows = [{"docket_id": native["docket_id"], "court": native["court"],
             "case_name": native["caseName"], "date_filed": native["dateFiled"],
             "cause": native["cause"], "nature_of_suit": native["suitNature"],
             "jury_demand": "", "date_terminated": None, "content_kind": CONTENT_KIND, "native_record": native}]
    for offset in range(1, 7):
        duplicate = deepcopy(rows[0])
        duplicate["docket_id"] += offset
        duplicate["native_record"]["docket_id"] += offset
        rows.append(duplicate)
    start, end = "2026-09-25", "2026-10-09"
    coverage = [{"mode": "exhaustive_window", "query": q["query"], "date_filed_after": start,
        "date_filed_before": end, "complete": i == 0, "pages": 1 if i == 0 else 7,
        "total": i * 20} for i, q in enumerate(definitions)]
    coverage[0].update(termination="null_next", has_next=False, count_validation="exact_population_match")
    queries = [dict(definitions[0], status="complete", returned=0, docket_ids=[], coverage=coverage[0]),
               dict(definitions[1], status="failed", returned=0, reason_code="invalid_response", http_status=None,
                    coverage=coverage[1])]
    reqs = [{"provider": "courtlistener", "operation": "get", "http_status": 200, "reason_code": None,
        "attempts": 1 if i else 3, "recovered": i == 0} for i in range(8)]
    result = {"courtlistener_scope_policy": POLICY, "error": "Focused Court declared scope incomplete",
        "_request_diagnostics": reqs, "dockets": rows, "coverage": {
            "policy": POLICY, "mode": "declared_focused_scope", "complete": False,
            "content_kind": CONTENT_KIND, "scope": selected, "scope_sha256": digest(selected),
            "date_filed_after": start, "date_filed_before": end, "queries": queries,
            "requests": 10, "request_cap": 10, "budget_exhausted": False, "request_cap_exhausted": True,
            "returned": len(rows), "acquisition_started_at": end+"T22:01:00+00:00",
            "acquired_at": end+"T22:02:00+00:00", "absolute_deadline": 240.5, "subbudget_seconds": 120,
            "prior_reuse_status": "not_supplied", "response_bytes": 1234}}
    result["coverage"]["evidence_sha256"] = evidence_digest(result)
    return result, selected, end, datetime(2026,10,9,22,3,tzinfo=timezone.utc)


def test_qualified_court_hold_requires_bound_failed_query_and_request_receipts():
    from tradingagents.strategies.orchestration import held_court_observation as monitor
    payload, selected, session, acquired = partial_court()
    assert monitor.validate_held_court_observation(payload, selected, session, acquired) is None
    for mutate in (
        lambda p: p["coverage"].update(complete=True),
        lambda p: p["coverage"].update(request_cap_exhausted=False),
        lambda p: p["coverage"].update(requests=True),
        lambda p: p["coverage"].update(acquired_at="2026-10-10T00:00:00+00:00"),
        lambda p: p["coverage"]["queries"][-1].update(reason_code="provider_error"),
        lambda p: p["coverage"]["queries"][0]["coverage"].update(total=20),
        lambda p: p["coverage"]["queries"][-1]["coverage"].update(pages=4),
        lambda p: p["coverage"]["queries"][-1]["coverage"].update(total=0),
        lambda p: p["dockets"][0]["native_record"].update(docket_id=666),
        lambda p: p["_request_diagnostics"][0].update(attempts=1),
        lambda p: p["_request_diagnostics"][0].update(http_status=503),
    ):
        damaged = deepcopy(payload)
        mutate(damaged)
        damaged["coverage"]["evidence_sha256"] = evidence_digest(damaged)
        with pytest.raises(ValueError):
            monitor.validate_held_court_observation(damaged, selected, session, acquired)


def test_unavailable_projection_command_returns_failure_with_visible_results(monkeypatch, capsys, tmp_path):
    from scripts import source_repair
    monkeypatch.setattr(source_repair, "refresh_intervention_proposals", lambda *args, **kwargs: {
        "proposals": [{"status":"available", "path":"/source", "diagnostics":[]}],
        "enrichment_monitors": [{"status":"unavailable", "path":None, "diagnostics":["enrichment_monitor_unavailable"]}]})
    assert source_repair.main(["interventions", "--repo-root", str(tmp_path)]) == 2
    assert "enrichment_monitor_unavailable" in capsys.readouterr().out
