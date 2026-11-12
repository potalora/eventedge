"""Compare frozen admission evidence without providers, models or trading state."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
import math
import platform
from statistics import median
from time import perf_counter_ns

from .selection import select_candidates


POLICIES = {"legacy": "legacy_v1", "alternative": "issuer_event_v1"}


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("invalid availability timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("availability timestamp requires timezone")
    return result


def _availability(case):
    cutoff = _timestamp(case["cutoff_at"]) if case.get("cutoff_at") else None
    missing = 0
    for row in case["rows"]:
        clocks = row.get("availability", {})
        if not isinstance(clocks, dict):
            raise ValueError("invalid availability fields")
        known = [_timestamp(value) for value in clocks.values() if value is not None]
        if cutoff is not None and any(value > cutoff for value in known):
            raise ValueError("evidence availability exceeds retained decision cutoff")
        if not clocks.get("envelope_acquired_at") or cutoff is None:
            missing += 1
    return {"status": "bounded_by_retained_acquisition" if not missing else "unmeasured",
            "cutoff_at": case.get("cutoff_at"), "missing_rows": missing,
            "meaning": "Retained acquisition is a conservative availability bound, not first public availability."}


def _hypothesis(row):
    return row["ticker"], row["event_key"], row["direction"]


def _metrics(rows, previous, had_prior_session):
    eligible = [row for row in rows if not row["journal_only"]
                and row.get("universe", {}).get("decision") == "eligible"]
    counts = Counter(row["ticker"] for row in eligible)
    repeated = identical = changed = unknown = 0
    for row in rows:
        versions = previous.get(_hypothesis(row))
        if versions is None:
            continue
        repeated += 1
        fingerprint = row.get("evidence_sha256")
        if not fingerprint or not versions or None in versions:
            unknown += 1
        elif fingerprint in versions:
            identical += 1
        else:
            changed += 1
    return {
        "selected_count": len(rows), "eligible_selected_count": len(eligible),
        "journal_only_selected_count": sum(row["journal_only"] for row in rows),
        "distinct_eligible_symbols": len(counts),
        "eligible_symbols": sorted(counts), "selected_by_symbol": dict(sorted(counts.items())),
        "max_eligible_symbol_share": round(max(counts.values(), default=0) / len(eligible), 6) if eligible else None,
        "distinct_source_event_groups": len({row.get("source_event_key") or row["event_key"] for row in rows}),
        "source_event_grouping_status": "source_native" if all(row.get("source_event_key") for row in rows) else "ledger_identity_fallback",
        "screening_score_sum": round(sum(row["score"] for row in rows), 12),
        "history_status": "prior_retained_sessions_only" if had_prior_session else "no_prior_session",
        "repeated_source_hypotheses": repeated if had_prior_session else None,
        "repeated_identical_content": identical if had_prior_session else None,
        "changed_retained_content": changed if had_prior_session else None,
        "repeated_content_unmeasured": unknown if had_prior_session else None,
        "new_source_hypotheses": len(rows) - repeated if had_prior_session else None,
    }


def compare_fixture(fixture):
    """Reconstruct the recorded baseline before considering an alternative.

    The fixture is a source-backed projection, not an authority override. Only
    the frozen baseline's admitted/budget-excluded hypotheses can be reranked.
    Historical metrics are diagnostic; neither policy uses future sessions.
    """
    if not isinstance(fixture, dict) or fixture.get("schema_version") != 1:
        raise ValueError("unsupported selection fixture schema")
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("selection fixture requires cases")
    identities = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("rows"), list):
            raise ValueError("invalid selection case")
        date.fromisoformat(case["session"])
        identity = (case["session"], case["horizon"], case["strategy"])
        if identity in identities:
            raise ValueError("duplicate case in selection fixture")
        identities.add(identity)

    result = {"schema_version": 1, "generation": fixture.get("generation"),
              "code_commit": fixture.get("code_commit"),
              "session_count": len({item[0] for item in identities}),
              "model_calls": 0, "provider_calls": 0,
              "policies": POLICIES, "cases": [],
              "limits": ["Retrospective coverage comparison; no return or model-quality measurement.",
                         "Symbols are issuer proxies, not consolidated legal issuer identities.",
                         "Source-native event groups do not establish substantive economic-event equivalence.",
                         "Shared horizons and overlapping source windows are not independent samples.",
                         "Changed retained content is not proof of a substantively new update.",
                         "Acquisition budgets and upstream exclusions are unchanged."]}
    history = defaultdict(lambda: defaultdict(set))
    previous_sessions = set()
    for case in sorted(cases, key=lambda item: (item["session"], item["horizon"], item["strategy"])):
        availability = _availability(case)
        manifests = {label: select_candidates(case["rows"], case["budget"],
                                             strategy=case["strategy"], policy=policy)
                     for label, policy in POLICIES.items()}
        legacy_ids = [row["discovery_id"] for row in manifests["legacy"]["admitted"]]
        expected = case.get("expected_admitted_ids")
        recorded = {row["discovery_id"] for row in case["rows"] if row["reason"] == "admitted"}
        if legacy_ids != expected or set(legacy_ids) != recorded:
            raise ValueError(f"baseline does not reproduce retained admission: {case['session']} {case['strategy']} {case['horizon']}")
        item = {key: case[key] for key in ("session", "horizon", "strategy", "budget")}
        item.update(discovered_count=len(case["rows"]), availability=availability,
                    portfolio_relevance="unmeasured", substantive_event_groups="unmeasured")
        for label, manifest in manifests.items():
            key = (case["horizon"], case["strategy"], label)
            rows = manifest["admitted"]
            item[label] = {"selected_ids": [row["discovery_id"] for row in rows],
                           "metrics": _metrics(rows, history[key], key in previous_sessions),
                           "excluded_by_reason": dict(sorted(Counter(row["reason"] for row in manifest["excluded"]).items()))}
            for row in rows:
                history[key][_hypothesis(row)].add(row.get("evidence_sha256"))
            previous_sessions.add(key)
        old = set(item["legacy"]["selected_ids"])
        new = set(item["alternative"]["selected_ids"])
        item["selected_added"] = sorted(new - old)
        item["selected_removed"] = sorted(old - new)
        reasons = {label: {row["discovery_id"]: row["reason"] for row in manifest["discovered"]}
                   for label, manifest in manifests.items()}
        item["dispositions"] = [{"discovery_id": row["discovery_id"], "original_reason": row["reason"],
                                "legacy_reason": reasons["legacy"][row["discovery_id"]],
                                "alternative_reason": reasons["alternative"][row["discovery_id"]]}
                               for row in sorted(case["rows"], key=lambda row: row["discovery_id"])]
        result["cases"].append(item)
    return result


def benchmark_fixture(fixture, *, repetitions=50):
    """Measure local selector CPU/wall latency only; no inferred model savings."""
    if type(repetitions) is not int or not 1 <= repetitions <= 10000:
        raise ValueError("benchmark repetitions must be an integer from 1 to 10000")
    compare_fixture(fixture)
    cases = []
    for case in sorted(fixture["cases"], key=lambda item: (item["session"], item["horizon"], item["strategy"])):
        output = {key: case[key] for key in ("session", "horizon", "strategy", "budget")}
        durations = {label: [] for label in POLICIES}
        # Alternate execution order to avoid always warming the same policy.
        for iteration in range(repetitions):
            order = list(POLICIES.items())
            if iteration % 2:
                order.reverse()
            for label, policy in order:
                start = perf_counter_ns()
                select_candidates(case["rows"], case["budget"], strategy=case["strategy"], policy=policy)
                durations[label].append((perf_counter_ns() - start) / 1_000_000)
        for label, values in durations.items():
            values.sort()
            output[label] = {"median_ms": round(median(values), 6),
                             "p95_ms": round(values[math.ceil(.95 * len(values)) - 1], 6)}
        cases.append(output)
    return {"repetitions_per_case_policy": repetitions, "python": platform.python_version(),
            "platform": platform.system() + " " + platform.machine(),
            "scope": "Local selector including validation/copy; excludes source, model and trading work.",
            "cases": cases}
