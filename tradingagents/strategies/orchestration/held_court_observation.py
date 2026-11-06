"""Recognize an exhausted focused Court search for monitoring only.

This validator returns no records. Only the normal complete-scope validator
can admit litigation evidence to a strategy.
"""
from datetime import date, datetime, timedelta
import math
from zoneinfo import ZoneInfo

from ..data_sources.courtlistener_scope import (
    CONTENT_KIND, MAX_BYTES, POLICY, canonical_scope, declared_queries, digest, evidence_digest,
)


def _require(condition):
    if not condition:
        raise ValueError("unverified incomplete Court acquisition")


def _int(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _partial_dockets(rows, start, end):
    ids = set()
    for row in rows:
        identity = row["docket_id"]
        _require(type(identity) is int and identity > 0 and identity not in ids
                 and row["content_kind"] == CONTENT_KIND and start <= row["date_filed"] <= end
                 and date.fromisoformat(row["date_filed"]).isoformat() == row["date_filed"])
        # This held contract covers issuer searches only. Native docket lookup
        # has a different response schema and needs its own evidence.
        natives = row.get("additional_native_records", [])
        _require(type(natives) is list)
        for native in [row["native_record"], *natives]:
            _require(type(native["docket_id"]) is int and native["docket_id"] == identity)
            for name, field in (("case_name", "caseName"), ("court", "court"), ("date_filed", "dateFiled")):
                _require(type(row[name]) is str and row[name].strip() and row[name] == native[field])
            for name, field in (("cause", "cause"), ("nature_of_suit", "suitNature"),
                                ("jury_demand", "juryDemand"), ("date_terminated", "dateTerminated")):
                value = native.get(field, None if name == "date_terminated" else "")
                _require((value is None or type(value) is str) and row[name] == value)
        ids.add(identity)
    return ids


def validate_held_court_observation(payload, expected_scope, session, acquired):
    """Validate the recorded failed query, including previous completed searches."""
    try:
        _validate(payload, expected_scope, session, acquired)
    except (AttributeError, KeyError, TypeError, OverflowError, IndexError) as exc:
        raise ValueError("unverified incomplete Court acquisition") from exc


def _validate(payload, expected_scope, session, acquired):
    selected = canonical_scope(expected_scope)
    start = (date.fromisoformat(session) - timedelta(days=14)).isoformat()
    coverage = payload["coverage"]
    _require(set(payload) == {"dockets","coverage","error","courtlistener_scope_policy","_request_diagnostics"}
             and payload["error"] == "Focused Court declared scope incomplete"
             and payload["courtlistener_scope_policy"] == coverage["policy"] == POLICY
             and coverage["mode"] == "declared_focused_scope" and coverage["content_kind"] == CONTENT_KIND
             and coverage["complete"] is False and coverage["budget_exhausted"] is False
             and coverage["request_cap_exhausted"] is True and coverage["scope"] == selected
             and coverage["scope_sha256"] == digest(selected) and coverage["evidence_sha256"] == evidence_digest(payload)
             and coverage["date_filed_after"] == start and coverage["date_filed_before"] == session
             and _int(coverage["requests"], 10, 10) and _int(coverage["request_cap"], 10, 10)
             and coverage["subbudget_seconds"] == 120 and coverage["prior_reuse_status"] == "not_supplied"
             and type(coverage["absolute_deadline"]) in (float, int) and math.isfinite(coverage["absolute_deadline"])
             and _int(coverage["response_bytes"], 1, MAX_BYTES))
    began, ended = (datetime.fromisoformat(coverage[key]) for key in ("acquisition_started_at", "acquired_at"))
    _require(began.utcoffset() is not None and ended.utcoffset() is not None
             and began <= ended <= acquired and (ended - began).total_seconds() <= coverage["subbudget_seconds"]
             and began.astimezone(ZoneInfo("America/New_York")).date().isoformat() == session
             and acquired.astimezone(ZoneInfo("America/New_York")).date().isoformat() == session)
    rows = payload["dockets"]
    _require(type(rows) is list and type(coverage["returned"]) is int and len(rows) == coverage["returned"])
    ids = _partial_dockets(rows, start, session)
    queries, expected = coverage["queries"], declared_queries(selected)
    _require(type(queries) is list and len(queries) == len(expected) and 0 < len(queries) <= 10)
    pages = 0
    completed_ids = set()
    for i, (query, spec) in enumerate(zip(queries, expected)):
        native = query["coverage"]
        _require(spec["kind"] == "issuer_search" and all(query.get(k) == v for k,v in spec.items())
                 and native["mode"] == "exhaustive_window" and native["query"] == spec["query"]
                 and native["date_filed_after"] == start and native["date_filed_before"] == session
                 and _int(native["pages"], 1, 10) and type(native["total"]) is int and native["total"] >= 0)
        pages += native["pages"]
        if i == len(queries) - 1:
            _require(query["status"] == "failed" and native["complete"] is False
                     and query["reason_code"] == "invalid_response" and query["http_status"] is None
                     and _int(query["returned"], 0, 0) and "docket_ids" not in query
                     and "termination" not in native and "count_validation" not in native and "has_next" not in native)
            # Rows can overlap across issuer searches. Dockets outside every
            # completed query must have come from the sole failed search.
            _require(native["total"] >= len(ids - completed_ids) and len(ids) >= native["pages"] - 1)
        else:
            returned = query["docket_ids"]
            _require(query["status"] == "complete" and native["complete"] is True and native["has_next"] is False
                     and native["termination"] == "null_next" and type(returned) is list
                     and len(returned) == len(set(returned)) and all(type(key) is int and key in ids for key in returned)
                     and type(query["returned"]) is int and query["returned"] == len(returned)
                     and native["count_validation"] == "exact_population_match" and native["total"] == len(returned))
            completed_ids.update(returned)
    requests = payload["_request_diagnostics"]
    _require(type(requests) is list and len(requests) == pages <= coverage["requests"])
    for request in requests:
        _require(set(request) == {"provider","operation","reason_code","http_status","attempts","recovered"}
                 and request["provider"] == "courtlistener" and request["operation"] == "get"
                 and request["reason_code"] is None and _int(request["http_status"], 200, 200)
                 and _int(request["attempts"], 1, 3) and request["recovered"] is (request["attempts"] > 1))
    _require(sum(req["attempts"] for req in requests) == coverage["requests"])
