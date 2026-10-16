"""Manual intervention cases use simulated observations, never native calls."""

from copy import deepcopy
from datetime import datetime
import hashlib
import importlib.util
import json

import pytest

from test_source_repair import COMMIT, GENERATION, NOW, pair, report
from test_source_repair import evidence as evidence


def api():
    name = "tradingagents.strategies.orchestration.source_repair_intervention"
    assert importlib.util.find_spec(name) is not None, (
        "intervention projector must exist"
    )
    from tradingagents.strategies.orchestration import source_repair_intervention

    return source_repair_intervention


def build(value, outcomes=None, now=NOW):
    return api().build_intervention_cases(
        *pair(value),
        outcomes or [],
        expected_generation=GENERATION,
        expected_commit=COMMIT,
        expected_session=value["session"],
        now=now,
    )


def outcome(value, reason="repair_failed", kind=None, state="requires_user"):
    from tradingagents.strategies.orchestration.source_repair import (
        build_repair_requests,
    )

    projected = build_repair_requests(
        *pair(value),
        expected_generation=GENERATION,
        expected_commit=COMMIT,
        expected_session=value["session"],
        now=datetime.fromisoformat(value["session"] + "T23:00:00+00:00"),
    )
    request = (projected["requests"] + projected["requires_user"])[0]
    return {
        "version": "eventedge-source-repair-outcome-v1",
        "incident_id": request["incident_id"],
        "state": state,
        "reason_code": reason,
        "evidence_kind": kind,
        "evidence_sha256": "c" * 64 if kind else None,
        "publication_url": None,
    }


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_has_urgent_grounded_case_and_numbered_ready_options(status):
    value = report(reason="http_error", status=status)
    value["source_health_failures"] = [
        {
            "health_id": "health1",
            "epoch_id": "epoch1",
            "session": value["session"],
            "policy_id": "policy1",
            "strategy": "macro_surprise",
            "status": "data_failure",
            "sources": ["fred"],
            "affected_cohorts": ["horizon_30d_size_5k"],
        }
    ]
    (case,) = build(value)
    assert case["severity"] == "urgent" and case["disposition"] == "requires_user"
    assert case["reason_code"] == "access_required"
    assert case["recommended_option"] == "restore_access"
    assert case["impact"] == {
        "report_outcome": "degraded",
        "input_coverage_valid": False,
        "affected_strategies": ["macro_surprise"],
        "affected_cohorts": ["horizon_30d_size_5k"],
    }
    assert [item["number"] for item in case["options"]] == [1, 2, 3, 4]
    assert [item["option"] for item in case["options"]] == [
        "restore_access",
        "prepare_replacement",
        "retire_source",
        "acknowledge",
    ]
    rendered = api().render_intervention_case(case)
    assert rendered.startswith("URGENT:") and "1." in rendered and "4." in rendered
    assert (
        "explicit approval" in rendered
        and "does not authorize production changes" in rendered
    )
    assert "paused" not in rendered


@pytest.mark.parametrize(
    "reason,kind,expected",
    [
        ("paid_only", "provider_contract", "paid_access_required"),
        ("source_shutdown", "operator_verified", "source_shutdown"),
        ("permanent_unavailability", "provider_contract", "permanent_unavailability"),
        ("broad_semantic_change", "independent_review", "semantic_change_required"),
        ("paid_only", None, "assessment_required"),
        ("source_shutdown", "independent_review", "assessment_required"),
        ("broad_semantic_change", "provider_contract", "assessment_required"),
        ("repair_failed", None, "repair_failed"),
        ("native_auth_unavailable", "operator_verified", "assessment_required"),
        ("unknown_completion", None, "assessment_required"),
    ],
)
def test_explicit_reliable_evidence_controls_classification(reason, kind, expected):
    value = report()
    (case,) = build(value, [outcome(value, reason, kind)])
    assert case["reason_code"] == expected
    if expected in {
        "paid_access_required",
        "source_shutdown",
        "permanent_unavailability",
        "semantic_change_required",
    }:
        assert case["recommended_option"] == "prepare_replacement"
    assert "finnhub" not in api().render_intervention_case(case)


def test_generic_timeout_does_not_invent_paid_or_permanent_cause():
    previous = None
    for day in ("2026-10-06", "2026-10-07", "2026-10-08"):
        previous = report(day, previous=previous, reason="timeout")
    (case,) = build(
        previous,
        [outcome(previous)],
        now=datetime.fromisoformat("2026-10-08T23:00:00+00:00"),
    )
    assert case["reason_code"] == "repair_failed"
    assert "paid" not in case["summary"] and "shutdown" not in case["summary"]


def test_case_identity_stable_across_sessions_and_replay_but_changes_on_classification():
    first = report(reason="http_error", status=403)
    second = report("2026-10-07", previous=first, reason="http_error", status=403)
    (a,) = build(first)
    (b,) = build(second, now=datetime.fromisoformat("2026-10-07T23:00:00+00:00"))
    assert a["case_id"] == b["case_id"]
    assert api().case_sha256(a) != api().case_sha256(b)
    identity = [a["version"], a["incident_id"], a["reason_code"]]
    assert (
        a["case_id"]
        == hashlib.sha256(
            json.dumps(identity, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert a == build(first)[0]
    value = report()
    (c,) = build(value, [outcome(value, "source_shutdown", "operator_verified")])
    (d,) = build(value, [outcome(value)])
    assert c["case_id"] != d["case_id"]


@pytest.mark.parametrize("state", ["accepted", "running", "pr_ready"])
def test_nonmanual_worker_states_and_recovered_sources_do_not_open_cases(state):
    value = report()
    row = outcome(value, None, state=state)
    if state == "pr_ready":
        row["publication_url"] = "https://github.com/potalora/eventedge/pull/123"
    assert build(value, [row]) == []
    healthy = report(healthy=True)
    assert build(healthy, [outcome(value)]) == []


@pytest.mark.parametrize(
    "damage",
    [
        "extra",
        "duplicate",
        "unknown_reason",
        "fake_evidence",
        "bad_url",
        "wrong_provider",
        "too_many",
    ],
)
def test_outcome_and_case_contracts_reject_untrusted_fields(damage):
    value = report()
    row = outcome(value)
    values = [row]
    if damage == "extra":
        row["cause"] = "PRIVATE token or paid shutdown speculation"
    elif damage == "duplicate":
        values.append(deepcopy(row))
    elif damage == "unknown_reason":
        row["reason_code"] = "shell"
    elif damage == "fake_evidence":
        row["evidence_kind"] = "operator_verified"
    elif damage == "bad_url":
        row["publication_url"] = "https://evil.invalid/"
    elif damage == "wrong_provider":
        row["provider"] = "fred"
    elif damage == "too_many":
        values = values * 129
    with pytest.raises(ValueError):
        build(value, values)


def test_renderer_rejects_modified_summary_or_option_instructions():
    (case,) = build(report(reason="http_error", status=403))
    case["options"][0]["next_step"] = "run sudo systemctl restart trade"
    with pytest.raises(ValueError):
        api().render_intervention_case(case)


def test_publisher_emits_auth_cases_in_fixed_nested_report_sidecar(tmp_path):
    from tradingagents.strategies.orchestration.operational_report import (
        write_operational_report,
    )

    value = report(reason="http_error", status=403)
    paths = write_operational_report(value, tmp_path)
    result = api().publish_intervention_proposal(value, paths, now=NOW)
    destination = tmp_path / "source-repair/2026-10-06-gen_020-source-intervention.json"
    assert result["path"] == str(destination)
    envelope = json.loads(destination.read_text())
    assert envelope["status"] == "available"
    assert envelope["cases"][0]["reason_code"] == "access_required"
    assert envelope["diagnostics"] == []
    assert (
        envelope["report_sha256"]
        == hashlib.sha256(pair(value)[0] + b"\0" + pair(value)[1]).hexdigest()
    )


def test_invalid_outcome_becomes_clear_assessment_without_echoing_private_content(
    tmp_path,
):
    from tradingagents.strategies.orchestration.operational_report import (
        write_operational_report,
    )

    value = report()
    paths = write_operational_report(value, tmp_path)
    row = outcome(value)
    folder = tmp_path / "source-repair"
    folder.mkdir()
    (folder / (row["incident_id"] + ".json")).write_text(
        "{PRIVATE token or paid-only shutdown speculation"
    )
    result = api().publish_intervention_proposal(value, paths, now=NOW)
    envelope = json.loads(open(result["path"]).read())
    assert envelope["diagnostics"] == ["broker_outcome_invalid"]
    assert envelope["cases"][0]["reason_code"] == "assessment_required"
    assert "PRIVATE" not in json.dumps(envelope)
    assert "paid_access_required" not in json.dumps(envelope)


def test_report_cli_permission_denial_is_explicit_without_breaking_report(
    tmp_path, monkeypatch, capsys
):
    from scripts import generate_operational_report

    value = report("2025-10-06")
    monkeypatch.setattr(
        generate_operational_report,
        "build_operational_report",
        lambda *a, **k: deepcopy(value),
    )

    def denied(*a, **k):
        raise PermissionError("root-owned sidecar directory")

    assert hasattr(generate_operational_report, "publish_intervention_proposal"), (
        "report CLI must integrate intervention publisher"
    )
    monkeypatch.setattr(
        generate_operational_report, "publish_intervention_proposal", denied
    )
    assert (
        generate_operational_report.main(
            [
                "--repo-root",
                str(tmp_path),
                "--generation",
                GENERATION,
                "--date",
                value["session"],
                "--output-dir",
                str(tmp_path / "reports"),
                "--snapshot-guaranteed",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)["reports"][0]
    assert result["source_intervention"]["status"] == "unavailable"
    assert result["source_intervention"]["path"] is None
    assert result["source_intervention"]["diagnostics"] == ["publication_denied"]


def test_trusted_refresh_uses_latest_manifest_identity_and_only_writes_case_artifact(
    evidence,
):
    root, request, source, manifest_path, output = evidence
    folder = root / "docs/reports/source-repair"
    folder.mkdir()
    row = {
        "version": "eventedge-source-repair-outcome-v1",
        "incident_id": request["incident_id"],
        "state": "failed",
        "reason_code": "repair_failed",
        "evidence_kind": None,
        "evidence_sha256": None,
        "publication_url": None,
    }
    (folder / (request["incident_id"] + ".json")).write_text(json.dumps(row))
    protected = [
        source,
        manifest_path,
        root / "data/operational/eventedge-runtime.lock",
        root / "docs/reports/2026-10-06-gen_020-operational-report.json",
        root / "docs/reports/2026-10-06-gen_020-operational-report.md",
    ]
    before = {path: path.read_bytes() for path in protected}
    result = api().refresh_intervention_proposals(root, now=NOW)
    assert len(result["proposals"]) == 1
    envelope = json.loads(open(result["proposals"][0]["path"]).read())
    assert envelope["cases"][0]["reason_code"] == "repair_failed"
    assert envelope["cases"][0]["generation_commit"] == request["generation_commit"]
    assert {path: path.read_bytes() for path in protected} == before


def test_refresh_rejects_manifest_commit_mismatch(evidence):
    root, request, source, manifest_path, output = evidence
    document = json.loads(manifest_path.read_text())
    document["generations"][0]["git_commit"] = "b" * 40
    manifest_path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="HEAD"):
        api().refresh_intervention_proposals(root, now=NOW)
    assert not (root / "docs/reports/source-repair").exists()


def test_publisher_refuses_symlinked_sidecar_directory(tmp_path):
    from tradingagents.strategies.orchestration.operational_report import (
        write_operational_report,
    )

    value = report(reason="http_error", status=403)
    folder, outside = tmp_path.resolve() / "reports", tmp_path.resolve() / "outside"
    paths = write_operational_report(value, folder)
    outside.mkdir()
    (folder / "source-repair").symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        api().publish_intervention_proposal(value, paths, now=NOW)
    assert list(outside.iterdir()) == []


def test_missing_report_for_one_active_generation_does_not_hide_later_urgent_case(
    evidence,
):
    from test_source_repair import git
    from tradingagents.strategies.orchestration.operational_report import (
        write_operational_report,
    )

    root, request, source, manifest_path, output = evidence
    generation = "gen_021"
    worktree = root / ".worktrees" / generation
    git(
        root, "worktree", "add", "--detach", str(worktree), request["generation_commit"]
    )
    state = root / "data/generations" / generation
    state.mkdir()
    document = json.loads(manifest_path.read_text())
    document["generations"].append(
        {
            "gen_id": generation,
            "git_commit": request["generation_commit"],
            "status": "active",
            "worktree_path": str(worktree),
            "state_dir": str(state),
            "run_history": [],
        }
    )
    manifest_path.write_text(json.dumps(document))
    value = report(reason="http_error", status=401, commit=request["generation_commit"])
    value["generation_id"] = generation
    write_operational_report(value, root / "docs/reports")
    (root / "docs/reports/2026-10-06-gen_020-operational-report.json").unlink()
    result = api().refresh_intervention_proposals(root, now=NOW)
    assert result["proposals"][0]["status"] == "unavailable"
    assert result["proposals"][0]["diagnostics"] == ["report_unavailable"]
    valid = result["proposals"][1]
    assert valid["status"] == "available"
    envelope = json.loads(open(valid["path"]).read())
    assert envelope["generation_id"] == generation
    assert envelope["cases"][0]["reason_code"] == "access_required"


def test_intervention_cli_distinguishes_busy_lock_from_invalid_evidence(
    tmp_path, monkeypatch
):
    from scripts import source_repair
    from tradingagents.strategies.orchestration.runtime_lock import RuntimeLockBusy

    def busy(*a, **k):
        raise RuntimeLockBusy(tmp_path / "lock")

    monkeypatch.setattr(source_repair, "refresh_intervention_proposals", busy)
    assert source_repair.main(["interventions", "--repo-root", str(tmp_path)]) == 75
    monkeypatch.setattr(source_repair, "current_repair_requests", busy)
    assert source_repair.main(["requests", "--repo-root", str(tmp_path)]) == 2


def _publish_with_worker_umask(directory):
    """Managed report directories already permit consumer traversal."""
    import os
    from tradingagents.strategies.orchestration.operational_report import write_operational_report

    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    (directory / 'source-repair').mkdir(mode=0o755, exist_ok=True)
    (directory / 'source-repair').chmod(0o755)
    value = report(reason='http_error', status=403)
    paths = write_operational_report(value, directory)
    previous = os.umask(0o077)
    try:
        result = api().publish_intervention_proposal(value, paths, now=NOW)
    finally:
        os.umask(previous)
    from pathlib import Path
    return Path(result['path'])


def test_intervention_publication_overrides_restrictive_worker_umask(tmp_path):
    import stat
    import subprocess
    import sys

    destination = _publish_with_worker_umask(tmp_path)
    # Execute an actual reader as well as checking the publication mode.
    # The Linux/root test below exercises a different unprivileged consumer UID.
    result = subprocess.run(
        [sys.executable, '-c',
         'import json,sys; print(json.load(open(sys.argv[1]))["status"])',
         str(destination)], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'available'
    assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    # A refresh also repairs a previously published private-mode projection.
    destination.chmod(0o600)
    assert _publish_with_worker_umask(tmp_path) == destination
    assert stat.S_IMODE(destination.stat().st_mode) == 0o644


def test_root_projection_is_readable_but_not_writable_by_different_unprivileged_uid():
    import os
    from pathlib import Path
    import pwd
    import subprocess
    import sys
    import tempfile

    if sys.platform != 'linux' or os.geteuid() != 0:
        pytest.skip('cross-UID permission check requires Linux root publisher')
    consumer = pwd.getpwnam('nobody')
    executable = Path('/usr/bin/python3')
    if not executable.exists():
        pytest.skip('system Python required for privilege-drop reader')
    with tempfile.TemporaryDirectory(prefix='eventedge-intervention-', dir='/tmp') as folder:
        directory = Path(folder).resolve()
        directory.chmod(0o755)
        destination = _publish_with_worker_umask(directory)
        assert destination.stat().st_uid == 0
        result = subprocess.run(
            [str(executable), '-c', '''import json,os,sys
assert os.geteuid() != 0
with open(sys.argv[1]) as source:
    assert json.load(source)['status'] == 'available'
try:
    open(sys.argv[1], 'ab').close()
except PermissionError:
    print('readable_not_writable')
else:
    raise AssertionError('consumer must not write root projection')
''', str(destination)], user=consumer.pw_uid, group=consumer.pw_gid,
            extra_groups=[], cwd='/tmp', capture_output=True, text=True, check=True)
        assert result.stdout.strip() == 'readable_not_writable'
