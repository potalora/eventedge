"""A pristine generation has no publication yet; operational health is checked separately."""
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from test_source_repair import evidence, pair, report, NOW, GENERATION
from scripts import source_repair as cli
from tradingagents.strategies.orchestration import source_repair, source_repair_intervention as projection


@pytest.fixture
def pristine(evidence):
    repo, _, _, manifest, _ = evidence
    document = json.loads(manifest.read_bytes())
    active, = document["generations"]
    state = Path(active["state_dir"])
    shutil.rmtree(state/"source_inputs")  # test fixture-only accepted source data
    profile = state/"generation-config.json"
    profile.write_bytes(b'{}\n')
    active.update(git_branch="fixture", description="test generation", created_at="2026-10-03T21:00:00", config_file=str(profile),
                  config_sha256=hashlib.sha256(profile.read_bytes()).hexdigest())
    manifest.write_text(json.dumps(document))
    folder = repo/"docs/reports"
    for current in folder.glob(f"*-{GENERATION}-operational-report.*"):
        current.unlink()
    (folder/"source-repair").mkdir()
    return repo, active


def run(repo, op, monkeypatch, capsys):
    monkeypatch.setattr(source_repair, "_now", lambda *_: NOW)
    rc = cli.main([op, "--repo-root", str(repo)])
    output = capsys.readouterr().out
    return rc, json.loads(output) if output else None


def test_pristine_publisher_requires_no_heartbeat_and_creates_no_artifacts(pristine, monkeypatch, capsys):
    repo, active = pristine
    folder = repo/"docs/reports"
    original = sorted(folder.rglob("*"))
    rc, message = run(repo, "interventions", monkeypatch, capsys)
    assert rc == 0
    pending, = message["enrichment_monitors"]
    assert pending == {"status":"awaiting_first_report", "path":None, "diagnostics":["first_report_pending"],
                       "generation_id":GENERATION, "generation_commit":active["git_commit"]}
    assert message["proposals"] == [dict(pending, cases_count=0)]
    assert run(repo, "verify-projections", monkeypatch, capsys)[0] == 2
    assert sorted(folder.rglob("*")) == original


@pytest.mark.parametrize("fault", ["json_only","markdown_only","past_source","future_enrichment","adjacent_old",
    "unexpected_state","prior_daily","wrong_profile","missing_profile","future_creation","pending_lifecycle","second_active","bad_backlink","missing_identity","source_directory_link"])
def test_publisher_refuses_incomplete_or_mature_scaffolds(pristine, fault, monkeypatch, capsys):
    repo, active = pristine
    folder = repo/"docs/reports"
    manifest = repo/"data/generations/manifest.json"
    doc = json.loads(manifest.read_bytes())
    row, = doc["generations"]
    if fault in ("json_only", "markdown_only"):
        extension = "json" if fault == "json_only" else "md"
        (folder/f"2026-10-06-{GENERATION}-operational-report.{extension}").write_text("malformed")
    elif fault == "past_source":
        (folder/f"source-repair/2026-10-05-{GENERATION}-source-intervention.json").write_bytes(b'{}')
    elif fault == "future_enrichment":
        (folder/f"source-repair/2026-10-07-{GENERATION}-enrichment-monitor.json").symlink_to(folder/"missing")
    elif fault == "adjacent_old":
        (folder/f"2026-10-05-{GENERATION}-source-repair.json").write_bytes(b'{}')
    elif fault == "unexpected_state":
        (Path(row["state_dir"])/"source_inputs").mkdir()
    elif fault == "prior_daily":
        row["run_history"] = [{"action":"daily"}]
    elif fault == "wrong_profile":
        row["config_sha256"] = "0" * 64
    elif fault == "missing_profile":
        Path(row["config_file"]).unlink()
    elif fault == "future_creation":
        row["created_at"] = "2030-01-01T00:00:00Z"
    elif fault == "pending_lifecycle":
        (manifest.parent/".pending-start.json").write_bytes(b'{}')
    elif fault == "bad_backlink":
        pointer = Path(row["worktree_path"])/".git"
        (Path(pointer.read_text().removeprefix("gitdir: ").strip())/"gitdir").write_text("/other/checkout/.git\n")
    elif fault == "missing_identity":
        del row["git_branch"]
    elif fault == "source_directory_link":
        (folder/"source-repair").rmdir()
        (folder/"source-repair").symlink_to(folder)
    elif fault == "second_active":
        doc["generations"].append(dict(row, gen_id="gen_021"))
    manifest.write_text(json.dumps(doc))
    rc, _ = run(repo, "interventions", monkeypatch, capsys)
    assert rc == 2


def test_failed_sidecar_publication_does_not_become_pending(pristine, monkeypatch, capsys):
    repo, active = pristine
    folder = repo/"docs/reports"
    stem = folder/f"2026-10-06-{GENERATION}-operational-report"
    current = report(commit=active["git_commit"])
    for ext, data in zip((".json",".md"), pair(current)):
        stem.with_suffix(ext).write_bytes(data)
    stale = folder/f"source-repair/2026-10-06-{GENERATION}-source-intervention.json"
    stale.write_bytes(b'{"status":"available"}')
    def deny(*_):
        raise PermissionError("simulated writer refusal")
    monkeypatch.setattr(projection, "_publish_sidecar", deny)
    assert run(repo, "interventions", monkeypatch, capsys)[0] == 2
    assert stale.read_bytes() == b'{"status":"available"}'


def test_racing_first_publication_retries_instead_of_confirming_absence(pristine, monkeypatch, capsys):
    repo, _ = pristine
    original = source_repair._Capture.verify
    def changed(capture):
        if capture.directories:
            (repo/f"docs/reports/2026-10-06-{GENERATION}-operational-report.json").write_text("{}")
        original(capture)
    monkeypatch.setattr(source_repair._Capture, "verify", changed)
    assert run(repo, "interventions", monkeypatch, capsys)[0] == 75
