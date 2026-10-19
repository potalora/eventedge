"""Constructed IO acceptance for the exact checked-in sixteen-book profile."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from scheduled_acceptance.harness import ROOT, create_harness

pytestmark = pytest.mark.scheduled_acceptance


@pytest.mark.parametrize("variant", ["stabilization_v1", "stabilization_v2"])
def test_stabilization_harness_retains_exact_policy_and_model_routes(tmp_path, variant):
    harness = create_harness(tmp_path, profile_variant=variant)
    source = ROOT / "config/profiles" / (variant.replace("_", "-") + ".json")
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
    if variant == "stabilization_v2":
        assert fixture["autoresearch"]["enrichment_capability_policy"] == "optional_enrichment_retirement_v1"
        assert fixture["autoresearch"]["retired_enrichment_capabilities"] == {
            "commodity_futures_curves": "maturity_identified_curve_evidence_unavailable"}
        assert proof["enrichment_capability_policy"] == "optional_enrichment_retirement_v1"
        assert proof["retired_enrichment_capabilities"] == fixture["autoresearch"]["retired_enrichment_capabilities"]


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
    if harness.audit["profile_variant"] == "stabilization_v2":
        _assert_accepted_retirement_scope(harness, session)
        if continuity:
            assert report["outcome"] == "clean", report
    return report


def _assert_accepted_retirement_scope(harness, session):
    """Reconstruct the shared receipt from immutable enrichment, across all books."""
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment, digest
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore, source_codec_limits
    ar = json.loads(harness.profile.read_text())["autoresearch"]
    limits = source_codec_limits({"autoresearch": ar})
    accepted = [SourceInputStore.decode(path.read_text(), **limits)
                for path in (harness.state / "source_inputs/decision_inputs").glob("*.json")]
    accepted = [row for row in accepted if row["identity"]["session"] == session]
    assert len(accepted) == 1
    original = accepted[0]["payload"]["enrichment"]
    _, scope = project_enrichment(original, capability_config=ar, codec_limits=limits)
    assert scope["original_sha256"] == accepted[0]["payload"]["research_binding"]["enrichment_digest"]
    assert scope["original_sha256"] == digest(original, codec_limits=limits)
    assert scope["supported_enrichment_complete"] is True
    assert scope["research_coverage_complete"] is False
    assert scope["order_authority"] is False
    assert scope["declared_exclusions"]["retired_capabilities"] == {
        "commodity_futures_curves": "maturity_identified_curve_evidence_unavailable"}
    decisions = harness.book_rows("committee_decisions")
    assert len(decisions) == 16
    for rows in decisions.values():
        current = [json.loads(row["payload_json"])["status"] for row in rows if row["session"] == session]
        assert len(current) == 1
        assert current[0]["research_enrichment_scope"] == scope
        assert current[0]["degraded"] is False
        assert not current[0].get("enrichment_failures")


@pytest.mark.parametrize("variant", ["stabilization_v1", "stabilization_v2"])
def test_exact_stabilization_profile_two_sessions_positive_fills_and_zero_io_replay(tmp_path, variant):
    harness = create_harness(tmp_path, profile_variant=variant)
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
    if variant == "stabilization_v2":
        _reject_locally_valid_scope_substitution(harness, before_io)


def _reject_locally_valid_scope_substitution(harness, before_io):
    """A valid retirement declaration cannot replace the accepted acquisition."""
    from tradingagents.strategies.orchestration.enrichment_capabilities import (
        declaration, DECLARATION_KEY, supported_complete)
    ar = json.loads(harness.profile.read_text())["autoresearch"]
    path = sorted(harness.state.glob("horizon_*/portfolio.db"))[0]
    with sqlite3.connect(path) as connection:
        original = connection.execute("SELECT payload_json FROM committee_decisions WHERE session=?",
                                      ("2026-10-15",)).fetchone()[0]
        document = json.loads(original)
        scope = document["status"]["research_enrichment_scope"]
        assert scope["declared_exclusions"]["underlying_symbols"] == []
        scope["declared_exclusions"] = declaration(ar, ["NG", "SI"])[DECLARATION_KEY]
        assert supported_complete(scope, ar) is True
        connection.execute("UPDATE committee_decisions SET payload_json=? WHERE session=?",
            (json.dumps(document, sort_keys=True, separators=(",", ":")), "2026-10-15"))
    try:
        invalid = harness.readiness("2026-10-15", sessions=2)
        assert invalid["ready"] is False and invalid["operational_continuity_complete"] is False, invalid
        assert any(row.get("completion_reason_codes") for row in invalid["sessions"]), invalid
    finally:
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE committee_decisions SET payload_json=? WHERE session=?",
                               (original, "2026-10-15"))
    restored = harness.readiness("2026-10-15", sessions=2)
    assert restored["ready"] is True, restored
    assert [row for row in harness.trace() if row["kind"] in {"http", "sdk", "model"}] == before_io


def test_stabilization_v2_native_ng_si_collection_uses_physical_sdk_fixtures(tmp_path):
    harness = create_harness(tmp_path, profile_variant="stabilization_v2")
    # Establish only the constructed clock; this probe does not claim a screen,
    # committee selection, scheduled run or point-in-time provider observation.
    harness.effective_config_proof()
    code = '''import json,os,sqlite3,sys
from pathlib import Path
from types import SimpleNamespace
from tradingagents.strategies.data_sources.openbb_source import OpenBBSource
from tradingagents.strategies.orchestration.cohort_orchestrator import CohortOrchestrator
from tradingagents.strategies.orchestration.research_resilience import project_enrichment
config=json.loads(Path(sys.argv[1]).read_text())
# Extend the external FINRA response cache, using its native schema. Every
# normalization, batching, acquisition and retirement decision remains real.
with sqlite3.connect(Path(os.environ['EVENTEDGE_ACCEPTANCE_BASE'])/'finra-response.db') as connection:
 for index,symbol in enumerate(['SLV','UNG'],100):
  connection.execute('INSERT INTO short_interest VALUES (?,?,?,?,?,?,?,?,?,?,?)',
   (index,symbol,symbol+' fund','N',1000,900,100000,0.01,11.1,100,'2026-10-13'))
source=OpenBBSource(fmp_api_key='offline-dummy')
owner=CohortOrchestrator.__new__(CohortOrchestrator)
owner._base_config=config
owner.cohorts=[{'engine':SimpleNamespace(registry={'openbb':source})}]
original=owner._fetch_openbb_enrichment([{'ticker':'UNG'},{'ticker':'SLV'},{'ticker':'UNG'}])
projected,scope=project_enrichment(original,capability_config=config['autoresearch'])
print(json.dumps({'original':original,'projected':projected,'scope':scope}))
'''
    result = subprocess.run([str(harness.python), "-c", code, str(harness.profile)],
                            cwd=harness.repo, env=harness.env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert value["scope"]["declared_exclusions"]["underlying_symbols"] == ["NG", "SI"]
    assert value["scope"]["supported_enrichment_complete"] is True
    assert value["scope"]["research_coverage_complete"] is False
    assert not value["scope"]["excluded"]
    assert "commodity_futures_curves" not in value["original"]
    assert "errors" not in value["original"]
    assert set(value["projected"]["profiles"]) == {"SLV", "UNG"}
    assert set(value["projected"]["short_interest"]) == {"SLV", "UNG"}
    assert value["projected"]["factors"]
    trace = harness.trace()
    assert not any(row["kind"] in {"forbidden_network", "unexpected_httpx", "http", "model"} for row in trace)
    assert {row["endpoint"] for row in trace if row["kind"] == "sdk"} == {
        "openbb.profile", "openbb.factors", "finra.prepare_data"}


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
