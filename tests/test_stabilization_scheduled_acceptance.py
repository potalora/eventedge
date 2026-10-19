"""Constructed IO acceptance for the exact checked-in sixteen-book profile."""
import hashlib
import json
from pathlib import Path

import pytest

from scheduled_acceptance.harness import ROOT, create_harness

pytestmark = pytest.mark.scheduled_acceptance


def test_stabilization_harness_retains_exact_policy_and_model_routes(tmp_path):
    harness = create_harness(tmp_path, profile_variant="stabilization_v1")
    source = ROOT / "config/profiles/stabilization-v1.json"
    assert harness.audit["original_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert harness.audit["candidate_profile_sha256"] == harness.audit["original_sha256"]
    assert harness.audit["policy_overrides"] == []
    candidate = json.loads(source.read_text())
    fixture = json.loads(harness.profile.read_text())
    for change in harness.audit["substitutions"]:
        current = candidate
        for key in change["path"][:-1]:
            current = current[key]
        assert current[change["path"][-1]] == change["original"]
        current[change["path"][-1]] = change["fixture"]
    assert candidate == fixture
    assert fixture["autoresearch"]["bounded_document_registry"] == []
    assert fixture["autoresearch"]["noaa_delivery_policy"] == "ghcnd_yearly_v1"
    assert (harness.repo / "uv.lock").read_bytes() == (ROOT / "uv.lock").read_bytes()
    proof = harness.effective_config_proof()
    assert proof["regulation_retrieval_policy"] is None
    assert proof["blocked_tickers"] == ["GOOGL", "GOOG"]
    assert proof["noaa_delivery_policy"] == "ghcnd_yearly_v1"


def _complete(harness, session, *, continuity=True):
    report = harness.report(session)
    assert report["accounting_valid"] and report["staging_complete"] and report["evidence_complete"], report
    completion = report["completion"]
    assert completion["workflow_complete"] is True, completion
    assert completion["continuity_eligible"] is continuity, completion
    assert completion["research_coverage_complete"] is False
    assert len(completion["declared_exclusions"]) == 6
    assert completion["profile_sha256"] == harness.audit["fixture_sha256"]
    assert completion["generation_commit"] == harness.commit
    screening = harness.accepted_screening(session)
    assert set(screening["horizons"]) == {"30d", "3m", "6m", "1y"}
    assert sum(len(horizon["health"]) for horizon in screening["horizons"].values()) == 48
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    source_records = [SourceInputStore.decode(path.read_text())
                      for path in (harness.state / "source_inputs").glob("*.json")]
    accepted = [row for row in source_records if row["identity"]["session"] == session]
    assert len(accepted) == 1
    weather = accepted[0]["payload"]["noaa"]
    assert not weather.get("error"), weather
    assert weather["delivery_policy"] == "ghcnd_yearly_v1"
    assert weather["numeric_policy"] == "ghcnd_metric_exact_to_standard_v1"
    assert weather["coverage"]["complete"] is True
    assert weather["coverage"]["catalog_station_count"] == 10
    assert weather["observation_date"] == session
    assert weather["vintage_as_of"] == session
    assert all(row["integrity_complete"] is True for row in weather["coverage"]["files"])
    fixture_names = {"ghcnd-stations.txt": "stations-ten-state-original.txt",
                     "ghcnd-inventory.txt": "inventory-ten-state-core-original.txt",
                     "2026.csv.gz": "2026-ten-state-37day-original.csv.gz"}
    assert {row["official_url"].rsplit("/", 1)[-1]: row["sha256"]
            for row in weather["coverage"]["files"]} == {
        endpoint: hashlib.sha256((ROOT / "tests/fixtures/noaa_ghcnd_yearly" / filename).read_bytes()).hexdigest()
        for endpoint, filename in fixture_names.items()}
    for table in ("account_snapshots", "staging_runs"):
        books = harness.book_rows(table)
        assert len(books) == 16
        assert all(any(row["session"] == session and
                       (row["valid"] if table == "account_snapshots" else row["completed_at"])
                       for row in rows) for rows in books.values())
    return report


def test_exact_stabilization_profile_two_sessions_positive_fills_and_zero_io_replay(tmp_path):
    harness = create_harness(tmp_path, profile_variant="stabilization_v1")
    first = harness.run("2026-10-14T22:00:00+00:00")
    assert first.returncode == 0, first.stdout + first.stderr
    _complete(harness, "2026-10-14")
    intents = harness.book_rows("order_intents")
    assert len(intents) == 16
    assert all(any(row["side"] == "buy" and row["requested_qty"] > 0 and row["eligible_session"] == "2026-10-15"
                   for row in rows) for rows in intents.values())
    assert all(not rows for rows in harness.book_rows("fills").values())
    second = harness.run("2026-10-15T22:00:00+00:00")
    assert second.returncode == 0, second.stdout + second.stderr
    _complete(harness, "2026-10-15")
    for name, rows in harness.book_rows("fills").items():
        accepted = [row for row in rows if row["session"] == "2026-10-15" and row["side"] == "buy"]
        assert accepted and all(row["quantity"] > 0 for row in accepted)
        assert {row["intent_id"] for row in accepted} == {
            row["intent_id"] for row in intents[name] if row["side"] == "buy"}
    trace = harness.trace()
    assert not any(row["kind"] in {"forbidden_network", "unexpected_httpx"} for row in trace)
    models = [row for row in trace if row["kind"] == "model"]
    assert {row["role"] for row in models} == {"analysis", "committee"}
    assert all(row["model"] == "gpt-6.1-sol" and row["effort"] == "high" for row in models)
    assert not any("regulations.gov" in row.get("url", "") for row in trace)
    noaa = [row for row in trace if row["kind"] == "http" and "ncei.noaa.gov" in row.get("url", "")]
    assert {row["url"].rsplit("/", 1)[-1] for row in noaa} == {
        "ghcnd-stations.txt", "ghcnd-inventory.txt", "2026.csv.gz"}
    assert all(row["fixture_boundary"] == "noaa_yearly_original_lines" for row in noaa)
    assert all(row["fixture_original_acquisition_date"] == "2026-10-19" for row in noaa)
    runtime_records = []
    for attempt in harness.attempts():
        if attempt["action"] != "daily":
            continue
        records = [json.loads(line.split("=", 1)[1]) for line in attempt["stderr"].splitlines()
                   if line.startswith("EVENTEDGE_RUNTIME_IDENTITY_V1=")]
        assert len(records) == 1, attempt
        runtime_records.extend(records)
    assert runtime_records
    assert all(row["generation_commit"] == harness.commit and row["profile_sha256"] == harness.audit["fixture_sha256"]
               for row in runtime_records)
    assert len({row["dependencies_sha256"] for row in runtime_records}) == 1
    assert all(row["lock_sha256"] == hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
               for row in runtime_records)
    before_books = {table: harness.book_rows(table) for table in
                    ("fills", "cash_events", "account_snapshots", "staging_runs", "order_intents", "committee_decisions")}
    before_inputs = {str(path): path.read_bytes() for path in (harness.state / "source_inputs").rglob("*.json")}
    before_io = [row for row in trace if row["kind"] in {"http", "sdk", "model"}]
    repeat = harness.run("2026-10-15T22:05:00+00:00")
    assert repeat.returncode == 0, repeat.stdout + repeat.stderr
    assert {table: harness.book_rows(table) for table in before_books} == before_books
    assert all(Path(path).read_bytes() == body for path, body in before_inputs.items())
    assert [row for row in harness.trace() if row["kind"] in {"http", "sdk", "model"}] == before_io
    status = json.loads((harness.repo / "data/runtime_recovery/status.json").read_text())
    latest = [row for row in status["incidents"] if row["session"] == "2026-10-15"]
    assert len(latest) == 1 and latest[0]["reason_code"] == "complete", status
    assert latest[0]["disposition"] == "recovered", status
    readiness = harness.readiness("2026-10-15", sessions=2)
    assert readiness["ready"] is True and readiness["operational_continuity_complete"] is True, readiness
    assert readiness["required_consecutive_sessions"] == 2
    assert readiness["research_coverage_complete"] is False
    assert len(readiness["sessions"]) == 2
    assert all(row["workflow_complete"] and row["operational_continuity_complete"]
               and row["accounting_complete"] and row["staging_complete"]
               for row in readiness["sessions"]), readiness


@pytest.mark.parametrize("fault, complete", [("fred_optional_payems_503", True),
                                            ("fred_required_hy_503", False)])
def test_stabilization_source_failure_preserves_exact_required_boundary(tmp_path, fault, complete):
    harness = create_harness(tmp_path, profile_variant="stabilization_v1")
    result = harness.run("2026-10-14T22:00:00+00:00", fault=fault)
    report = harness.report("2026-10-14")
    assert report["accounting_valid"] and report["staging_complete"] and report["evidence_complete"], report
    assert report["completion"]["workflow_complete"] is complete, report["completion"]
    assert report["completion"]["continuity_eligible"] is False
    assert report["outcome"] == "degraded" and report["input_coverage_valid"] is False
    readiness = harness.readiness("2026-10-14", sessions=1)
    assert readiness["ready"] is False and readiness["operational_continuity_complete"] is False, readiness
    assert readiness["sessions"][0]["workflow_complete"] is complete
    assert {row.get("source", row.get("provider")) for row in report["sources"]["unresolved"]} == {"fred"}
    assert all(not rows for rows in harness.book_rows("fills").values())
    if complete:
        assert result.returncode == 0, result.stdout + result.stderr
        _complete(harness, "2026-10-14", continuity=False)
        assert all(rows for rows in harness.book_rows("order_intents").values())
    else:
        assert all(not rows for rows in harness.book_rows("order_intents").values())
        assert not [row for row in harness.trace() if row["kind"] == "model"]
        status = json.loads((harness.repo / "data/runtime_recovery/status.json").read_text())
        assert not any(row["reason_code"] == "complete" or row["disposition"] == "recovered"
                       for row in status["incidents"]), status
    assert not any(row["kind"] in {"forbidden_network", "unexpected_httpx"} for row in harness.trace())
