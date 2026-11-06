"""The report CLI must expose the canonical supported-workflow result."""
from contextlib import nullcontext
import json

import pytest

from scripts import generate_operational_report as cli


@pytest.mark.parametrize("completion,expected", [
    ({"workflow_complete": False, "continuity_eligible": False}, 2),
    ({"workflow_complete": True, "continuity_eligible": False}, 0),
    ({"workflow_complete": "true"}, 2),
    ({}, 2),
])
def test_cli_exit_follows_reconstructed_workflow_completion(
    tmp_path, monkeypatch, capsys, completion, expected,
):
    # The canonical report producer is covered separately. This regression
    # checks the service-facing consumer of its completed result.
    report = {
        "outcome": "degraded", "evidence_complete": True,
        "source_incidents": {}, "completion": completion,
    }
    monkeypatch.setattr(cli, "report_runtime_guard", lambda *a, **k: nullcontext())
    monkeypatch.setattr(cli, "build_operational_report", lambda *a, **k: report)
    paths = {"json": tmp_path / "report.json", "markdown": tmp_path / "report.md"}
    monkeypatch.setattr(cli, "write_operational_report", lambda *a, **k: paths)
    monkeypatch.setattr(cli, "publish_repair_proposal", lambda *a: None)
    monkeypatch.setattr(cli, "publish_intervention_proposal", lambda *a: {})

    result = cli.main([
        "--repo-root", str(tmp_path), "--generation", "gen_100",
        "--date", "2026-10-15", "--snapshot-guaranteed",
    ])

    assert result == expected
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["reports"][0]["completion"] == completion


def test_legacy_report_exit_retains_existing_coverage_contract(tmp_path, monkeypatch):
    report = {"outcome": "degraded", "evidence_complete": True, "source_incidents": {}}
    monkeypatch.setattr(cli, "report_runtime_guard", lambda *a, **k: nullcontext())
    monkeypatch.setattr(cli, "build_operational_report", lambda *a, **k: report)
    monkeypatch.setattr(cli, "write_operational_report", lambda *a, **k: {
        "json": tmp_path / "report.json", "markdown": tmp_path / "report.md",
    })
    monkeypatch.setattr(cli, "publish_repair_proposal", lambda *a: None)
    monkeypatch.setattr(cli, "publish_intervention_proposal", lambda *a: {})
    assert cli.main([
        "--repo-root", str(tmp_path), "--generation", "gen_100",
        "--date", "2026-10-15", "--snapshot-guaranteed",
    ]) == 0
