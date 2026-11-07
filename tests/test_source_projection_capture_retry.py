"""Projection retries fresh captures; export and invalid evidence still fail closed."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from test_source_repair import NOW, evidence as evidence, pair
from tradingagents.strategies.orchestration import source_repair as contract
from tradingagents.strategies.orchestration import source_repair_intervention as projector
from scripts import source_repair as cli


@pytest.mark.parametrize("changed_bytes", [False, True])
def test_final_capture_change_requires_fresh_capture(tmp_path, changed_bytes):
    path = tmp_path / "evidence.json"
    path.write_bytes(b'{"value":1}\n')
    capture = contract._Capture(tmp_path)
    capture.read(path.name)
    replacement = path.with_name("replacement")
    payload = b'{"value":2}\n' if changed_bytes else path.read_bytes()
    replacement.write_bytes(payload)
    replacement.replace(path)
    with pytest.raises(ValueError) as raised:
        capture.verify()
    assert type(raised.value).__name__ == "EvidenceChangedDuringCapture"
    # The old capture remains invalid. Recovery must read and verify anew.
    with pytest.raises(ValueError):
        capture.verify()
    fresh = contract._Capture(tmp_path)
    assert fresh.read(path.name) == payload
    fresh.verify()


@pytest.mark.parametrize("stage", ["during_read", "before_path_recheck"])
def test_capture_change_during_one_read_is_specific(tmp_path, monkeypatch, stage):
    path = tmp_path / "evidence.json"
    path.write_bytes(b'{"value":1}\n')
    inode = path.stat().st_ino
    changed = []
    original_read, original_stat = os.read, os.stat

    def read(descriptor, size):
        body = original_read(descriptor, size)
        if body and os.fstat(descriptor).st_ino == inode and not changed:
            path.write_bytes(b'{"value":22}\n')
            changed.append(True)
        return body

    def stat(path_argument, *args, **kwargs):
        if path_argument == path.name and kwargs.get("dir_fd") is not None and not changed:
            replacement = path.with_name("replacement")
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
            changed.append(True)
        return original_stat(path_argument, *args, **kwargs)

    monkeypatch.setattr(os, "read" if stage == "during_read" else "stat", read if stage == "during_read" else stat)
    with pytest.raises(ValueError) as raised:
        contract._Capture(tmp_path).read(path.name)
    assert type(raised.value).__name__ == "EvidenceChangedDuringCapture"


@pytest.mark.parametrize("stage", ["outer_final", "inner_final", "outcome"])
def test_projection_recaptures_after_actual_report_or_outcome_replacement(
    evidence, monkeypatch, capsys, stage
):
    root, request, _, _, _ = evidence
    report_path = root / "docs/reports/2026-10-06-gen_020-operational-report.json"
    outcome_path = report_path.parent / "source-repair" / (request["incident_id"] + ".json")
    outcome_path.parent.mkdir()
    outcome_path.write_text(json.dumps({
        "version": "eventedge-source-repair-outcome-v1",
        "incident_id": request["incident_id"],
        "state": "requires_user", "reason_code": "evidence_unavailable",
        "evidence_kind": None, "evidence_sha256": None, "publication_url": None,
    }))
    monkeypatch.setattr(contract, "_now", lambda now=None: NOW)
    replaced = []
    original_verify, original_read = contract._Capture.verify, contract._Capture.read

    def replace(path):
        if path == report_path:
            changed = json.loads(report_path.read_bytes())
            changed.update(outcome="incomplete", evidence_complete=False)
            raw, markdown = pair(changed)
            files = [(report_path, raw), (report_path.with_suffix(".md"), markdown)]
        else:
            outcome = json.loads(outcome_path.read_bytes())
            outcome.update(state="failed", reason_code="repair_failed")
            files = [(outcome_path, json.dumps(outcome).encode())]
        for destination, body in files:
            replacement = destination.with_name(".race-replacement")
            replacement.write_bytes(body)
            replacement.replace(destination)
        replaced.append(path)

    def verify(capture):
        selected = root if stage == "outer_final" else report_path.parent
        if stage != "outcome" and capture.root == selected and not replaced:
            replace(report_path)
        return original_verify(capture)

    def read(capture, relative, *args):
        value = original_read(capture, relative, *args)
        if stage == "outcome" and Path(relative).name == outcome_path.name and not replaced:
            replace(outcome_path)
            return original_read(capture, relative, *args)
        return value

    monkeypatch.setattr(contract._Capture, "verify", verify)
    monkeypatch.setattr(contract._Capture, "read", read)
    assert cli.main(["interventions", "--repo-root", str(root)]) == 75
    first = capsys.readouterr()
    assert not first.out
    assert "evidence changed during capture" in first.err
    assert "race-replacement" not in first.err
    assert replaced
    # The source-only fixture has no frozen enrichment profile or decision inputs.
    # A stable recapture can publish its current case while the batch must fail.
    assert cli.main(["interventions", "--repo-root", str(root)]) == 2
    result = json.loads(capsys.readouterr().out)
    source, = result["proposals"]
    enrichment, = result["enrichment_monitors"]
    assert source["status"] == "available" and source["cases_count"] == 1 and source["diagnostics"] == []
    assert enrichment["status"] == "unavailable" and enrichment["diagnostics"] == ["accepted_enrichment_unavailable"]
    proposal = json.loads(Path(source["path"]).read_bytes())
    monitor = json.loads(Path(enrichment["path"]).read_bytes())
    raw, markdown = report_path.read_bytes(), report_path.with_suffix(".md").read_bytes()
    digest = hashlib.sha256(raw + b"\0" + markdown).hexdigest()
    assert proposal["report_sha256"] == monitor["report_sha256"] == digest
    assert (digest == request["report_sha256"]) is (stage == "outcome")
    expected = projector.build_intervention_cases(raw, markdown, [json.loads(outcome_path.read_bytes())],
        expected_generation=request["generation_id"], expected_commit=request["generation_commit"],
        expected_session=request["session"], now=NOW)
    assert proposal["status"] == "available" and proposal["cases"] == expected
    case, = proposal["cases"]
    assert case["incident_id"] == request["incident_id"]
    assert case["reason_code"] == ("repair_failed" if stage == "outcome" else "assessment_required")
    assert monitor["status"] == "unavailable" and monitor["diagnostics"] == enrichment["diagnostics"]
    assert monitor["accepted_enrichment_sha256"] is None and monitor["healthy_scope"] == []
    assert cli.main(["verify-projections", "--repo-root", str(root)]) == 2


@pytest.mark.parametrize("operation", ["requests", "export"])
def test_actual_capture_change_never_retries_request_or_export(
    evidence, monkeypatch, capsys, operation
):
    root, request, source, _, output = evidence
    original_verify = contract._Capture.verify
    monkeypatch.setattr(contract, "_now", lambda now=None: NOW)

    def verify(capture):
        # requests captures the report pair; export additionally captures source.
        path = source if operation == "export" else root / "docs/reports/2026-10-06-gen_020-operational-report.json"
        replacement = path.with_name(".nonretryable-replacement")
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        return original_verify(capture)

    monkeypatch.setattr(contract._Capture, "verify", verify)
    arguments = [operation, "--repo-root", str(root)]
    if operation == "export":
        request_path = root / (request["incident_id"] + ".json")
        request_path.write_text(json.dumps(request))
        arguments += ["--request-file", str(request_path), "--output-dir", str(output)]
    assert cli.main(arguments) == 2
    assert not capsys.readouterr().out
    assert not output.exists()


@pytest.mark.parametrize("damage", ["head", "manifest_schema", "manifest_symlink"])
def test_invalid_identity_schema_or_path_is_not_transient(evidence, monkeypatch, damage):
    root, _, _, manifest, _ = evidence
    monkeypatch.setattr(contract, "_now", lambda now=None: NOW)
    if damage == "head":
        document = json.loads(manifest.read_bytes())
        document["generations"][0]["git_commit"] = "b" * 40
        manifest.write_text(json.dumps(document))
    elif damage == "manifest_schema":
        manifest.write_text('{"generations":[{}]}')
    else:
        saved = manifest.with_name("saved-manifest.json")
        manifest.replace(saved)
        manifest.symlink_to(saved)
    assert cli.main(["interventions", "--repo-root", str(root)]) == 2


def test_invalid_capture_path_is_not_a_capture_change(tmp_path):
    capture = contract._Capture(tmp_path)
    for path in ["../outside", "/outside"]:
        with pytest.raises(ValueError) as raised:
            capture.read(path)
        assert type(raised.value) is ValueError


def test_enrichment_capture_change_is_not_swallowed(evidence, monkeypatch):
    from tradingagents.strategies.orchestration import enrichment_monitor

    root, _, _, _, _ = evidence
    original_verify = contract._Capture.verify

    def changed_enrichment(*args, **kwargs):
        capture = contract._Capture(root)
        path = root / "optional-enrichment-proof.json"
        path.write_text('{"proof":"simulated"}')
        capture.read(path.name)
        replacement = path.with_name("replacement.json")
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        return original_verify(capture)

    monkeypatch.setattr(enrichment_monitor, "publish_enrichment_monitor", changed_enrichment)
    with pytest.raises(ValueError) as raised:
        projector.refresh_intervention_proposals(root, now=NOW)
    assert type(raised.value).__name__ == "EvidenceChangedDuringCapture"
