"""Source-only repair contracts; all report/provider evidence below is simulated."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

NOW = datetime(2026, 10, 6, 23, tzinfo=timezone.utc)
GENERATION = "gen_020"
COMMIT = "a" * 40


def api():
    name = "tradingagents.strategies.orchestration.source_repair"
    assert importlib.util.find_spec(name) is not None, (
        "source repair contract must exist"
    )
    from tradingagents.strategies.orchestration import source_repair

    return source_repair


def report(
    session="2026-10-06",
    *,
    previous=None,
    reason="invalid_response",
    status=None,
    healthy=False,
    configuration="cfg123",
    commit=COMMIT,
):
    from tradingagents.strategies.orchestration.source_incidents import (
        assess_source_incidents,
    )

    value = {
        "generation_id": GENERATION,
        "generation_commit": commit,
        "session": session,
        "evidence_complete": True,
        "outcome": "degraded",
        "accounting_valid": True,
        "input_coverage_valid": False,
        "staging_complete": False,
        "staging_valid": False,
        "attempt_counts": {"daily": 1, "preflight": 0},
        "cohorts": {},
        "attempts": [],
        "performance_claims_withheld": True,
        "candidate_input_issues": [],
        "source_health_failures": [],
        "preflight_incidents": [],
        "diagnostics": [],
        "sources": {
            "configuration": configuration,
            "acquired_at": session + "T22:00:00+00:00",
            "observations": {"fred": "success" if healthy else "failed"},
            "unresolved": []
            if healthy
            else [{"provider": "fred", "reason_code": reason, "http_status": status}],
            "recovered": [],
        },
    }
    value["source_incidents"] = assess_source_incidents(value, previous)
    return value


def pair(value):
    from tradingagents.strategies.orchestration.operational_report import (
        render_operational_report,
    )

    return (
        (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(),
        render_operational_report(value).encode(),
    )


def build(value, **kwargs):
    return api().build_repair_requests(
        *pair(value),
        expected_generation=GENERATION,
        expected_commit=kwargs.pop("expected_commit", COMMIT),
        expected_session=kwargs.pop("expected_session", value["session"]),
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


def test_immediate_failure_requests_from_open_snapshot_without_escalation():
    value = report()
    assert value["source_incidents"]["alerts"][0]["kind"] == "opened"
    result = build(value)
    (request,) = result["requests"]
    assert result["requires_user"] == []
    assert request["reason_code"] == "invalid_response"
    assert (
        request["report_sha256"]
        == hashlib.sha256(pair(value)[0] + b"\0" + pair(value)[1]).hexdigest()
    )
    assert api().validate_repair_request(request, now=NOW) == request


def test_persistent_snapshot_has_stable_incident_identity_across_session_and_replay():
    previous = None
    requests = []
    for day in ("2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"):
        value = report(day, previous=previous, reason="timeout")
        now = datetime.fromisoformat(day + "T23:00:00+00:00")
        first = build(value, now=now)
        assert first == build(value, now=now)
        requests.extend(first["requests"])
        previous = value
    assert len(requests) == 2
    assert requests[0]["incident_id"] == requests[1]["incident_id"]
    assert requests[0]["report_sha256"] != requests[1]["report_sha256"]


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_requires_user_never_worker(status):
    result = build(report(reason="http_error", status=status))
    assert result["requests"] == []
    assert result["requires_user"][0]["disposition"] == "requires_user"
    assert result["requires_user"][0]["http_status"] == status


@pytest.mark.parametrize(
    "damage",
    [
        "unknown",
        "unavailable",
        "wrong_config",
        "duplicate",
        "forged_reason",
        "forged_count",
        "old_last_failure",
        "future_acquisition",
        "wrong_commit",
    ],
)
def test_invalid_or_unknown_snapshots_fail_closed(damage):
    value = report()
    row = value["source_incidents"]["open_incidents"][0]
    if damage == "unknown":
        row.update(observation="unknown", consecutive_failed_sessions=0)
    elif damage == "unavailable":
        value["source_incidents"]["assessment_status"] = "unavailable"
    elif damage == "wrong_config":
        value["source_incidents"]["configuration"] = "different"
    elif damage == "duplicate":
        value["source_incidents"]["open_incidents"].append(deepcopy(row))
    elif damage == "forged_reason":
        row["reason_code"] = "execute_shell"
    elif damage == "forged_count":
        row["consecutive_failed_sessions"] = True
    elif damage == "old_last_failure":
        row["last_failed_session"] = "2026-10-05"
    elif damage == "future_acquisition":
        value["sources"]["acquired_at"] = "2026-10-07T00:00:00+00:00"
    elif damage == "wrong_commit":
        value["generation_commit"] = "b" * 40
    with pytest.raises(ValueError):
        build(value)


def test_stale_clock_and_pair_mismatch_are_rejected():
    value = report()
    with pytest.raises(ValueError):
        build(value, now=datetime(2026, 10, 7, 23, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        build(value, now=datetime(2026, 10, 6, 23))
    with pytest.raises(ValueError):
        api().build_repair_requests(
            pair(value)[0],
            b"forged",
            expected_generation=GENERATION,
            expected_commit=COMMIT,
            expected_session=value["session"],
            now=NOW,
        )
    with pytest.raises(ValueError):
        api().build_repair_requests(
            json.dumps(value).encode(),
            pair(value)[1],
            expected_generation=GENERATION,
            expected_commit=COMMIT,
            expected_session=value["session"],
            now=NOW,
        )


@pytest.mark.parametrize(
    "damage",
    [
        "extra",
        "url",
        "commit",
        "hash",
        "date",
        "provider",
        "reason",
        "bool_http",
        "auth_http",
        "first_future",
        "version",
        "generation",
        "oversize",
        "duplicate_key",
    ],
)
def test_request_exact_schema_and_injection_rejection(damage):
    request = build(report())["requests"][0]
    if damage == "extra":
        request["command"] = "touch /tmp/pwn"
    elif damage == "url":
        request["configuration"] = "https://provider.invalid/"
    elif damage == "commit":
        request["generation_commit"] = "abc123"
    elif damage == "hash":
        request["incident_id"] = "0" * 64
    elif damage == "date":
        request["session"] = "2026-10-6"
    elif damage == "provider":
        request["provider"] = "shell"
    elif damage == "reason":
        request["reason_code"] = "missing_source_evidence"
    elif damage == "bool_http":
        request["http_status"] = True
    elif damage == "auth_http":
        request["http_status"] = 403
    elif damage == "first_future":
        request["first_failed_session"] = "2026-10-07"
    elif damage == "version":
        request["version"] = "future"
    elif damage == "generation":
        request["generation_id"] = "../../main"
    payload = json.dumps(request).encode()
    if damage == "oversize":
        payload += b" " * 4096
    elif damage == "duplicate_key":
        payload = payload[:-1] + b',"provider":"fred"}'
    with pytest.raises(ValueError):
        api().validate_repair_request(payload, now=NOW)


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def evidence(tmp_path):
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore

    root = tmp_path.resolve() / "canonical"
    root.mkdir()
    git(root, "init", "-q")
    (root / "README").write_text("simulated reference\n")
    git(root, "add", "README")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "reference",
    )
    commit = git(root, "rev-parse", "HEAD")
    worktree = root / ".worktrees" / GENERATION
    git(root, "worktree", "add", "--detach", str(worktree), commit)
    state = root / "data/generations" / GENERATION
    state.mkdir(parents=True)
    metadata = {
        "gen_id": GENERATION,
        "git_commit": commit,
        "status": "active",
        "worktree_path": str(worktree),
        "state_dir": str(state),
        "run_history": [],
    }
    manifest = root / "data/generations/manifest.json"
    manifest.write_text(json.dumps({"generations": [metadata]}))
    identity = {
        "generation": GENERATION,
        "commit": commit,
        "session": "2026-10-06",
        "configuration": "cfg123",
    }
    store = SourceInputStore(root / "cache", accepted_dir=state / "source_inputs")
    store.freeze(
        identity,
        {
            "fred": {
                "_coverage": {"status": "failed", "reason_code": "invalid_response"}
            }
        },
        acquired_at=NOW,
    )
    source = store._path(identity, frozen=True)
    store.freeze(
        {**identity, "session": "2026-10-05"},
        {"fred": {"_coverage": {"status": "success_empty"}}},
        acquired_at=datetime.fromisoformat("2026-10-05T22:00:00+00:00"),
    )
    from tradingagents.strategies.orchestration.runtime_lock import (
        canonical_runtime_lock_path,
        runtime_lock,
    )

    with runtime_lock(canonical_runtime_lock_path(root), exclusive=True):
        pass  # The normal application owner pre-creates its runtime lock.
    value = report(commit=commit)
    value["sources"]["acquired_at"] = NOW.isoformat()
    folder = root / "docs/reports"
    folder.mkdir(parents=True)
    stem = folder / "2026-10-06-gen_020-operational-report"
    stem.with_suffix(".json").write_bytes(pair(value)[0])
    stem.with_suffix(".md").write_bytes(pair(value)[1])
    request = build(value, expected_commit=commit)["requests"][0]
    return root, request, source, manifest, tmp_path.resolve() / "bundle"


def test_evidence_export_binds_canonical_identity_without_database(evidence):
    root, request, source, manifest_path, output = evidence
    (source.parent / "private.db").write_bytes(b"PRIVATE ledger")
    manifest = api().export_repair_evidence(root, request, output, now=NOW)
    assert manifest["incident_id"] == request["incident_id"]
    assert manifest["generation_commit"] == request["generation_commit"]
    assert manifest["session"] == request["session"]
    assert manifest["source_digest"]
    names = [row["path"] for row in manifest["files"]]
    assert not any(".db" in name or ".sqlite" in name for name in names)
    assert (
        len(names) == 5
    )  # Manifest, pair, current source and healthy origin boundary.
    for item in manifest["files"]:
        path = output / item["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
        assert path.stat().st_mode & 0o222 == 0
    assert json.loads((output / "evidence-manifest.json").read_text()) == manifest


@pytest.mark.parametrize(
    "damage",
    [
        "source_symlink",
        "report_symlink",
        "parent_symlink",
        "source_fifo",
        "source_identity",
        "source_digest",
        "source_acquisition",
        "manifest_path",
        "generation_head",
        "inactive",
        "report_change",
        "output_exists",
    ],
)
def test_evidence_export_rejects_escape_mixed_identity_and_unsafe_files(
    evidence, damage
):
    import os

    root, request, source, manifest_path, output = evidence
    if damage in {"source_symlink", "source_fifo"}:
        source.unlink()
        if damage == "source_symlink":
            source.symlink_to(root / "README")
        else:
            os.mkfifo(source)
    elif damage == "report_symlink":
        path = root / "docs/reports/2026-10-06-gen_020-operational-report.md"
        path.unlink()
        path.symlink_to(root / "README")
    elif damage == "parent_symlink":
        source.parent.rename(source.parent.with_name("other"))
        source.parent.symlink_to(
            source.parent.with_name("other"), target_is_directory=True
        )
    elif damage.startswith("source_"):
        from tradingagents.strategies.orchestration.source_inputs import (
            SourceInputStore,
        )

        payload = SourceInputStore.decode(source.read_text())
        if damage == "source_identity":
            payload["identity"]["commit"] = "b" * 40
        elif damage == "source_digest":
            payload["digest"] = "0" * 64
        elif damage == "source_acquisition":
            payload["acquired_at"] = datetime(2026, 10, 7, tzinfo=timezone.utc)
        source.write_text(SourceInputStore.encode(payload))
    elif damage in {"manifest_path", "inactive"}:
        data = json.loads(manifest_path.read_text())
        data["generations"][0][
            "worktree_path" if damage == "manifest_path" else "status"
        ] = "/tmp/arbitrary" if damage == "manifest_path" else "retired"
        manifest_path.write_text(json.dumps(data))
    elif damage == "generation_head":
        git(
            root / ".worktrees" / GENERATION,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "different",
        )
    elif damage == "report_change":
        (root / "docs/reports/2026-10-06-gen_020-operational-report.md").write_text(
            "changed"
        )
    elif damage == "output_exists":
        output.mkdir()
    with pytest.raises((ValueError, OSError)):
        api().export_repair_evidence(root, request, output, now=NOW)
    assert not (output / "evidence-manifest.json").exists()


def test_report_cli_publishes_bound_proposal_and_historical_report_stays_ineligible(
    tmp_path, monkeypatch
):
    from scripts import generate_operational_report

    value = report("2025-10-06")
    monkeypatch.setattr(
        generate_operational_report,
        "build_operational_report",
        lambda *args, **kwargs: deepcopy(value),
    )
    destination = tmp_path / "reports"
    code = generate_operational_report.main(
        [
            "--repo-root",
            str(tmp_path),
            "--generation",
            GENERATION,
            "--date",
            value["session"],
            "--output-dir",
            str(destination),
            "--snapshot-guaranteed",
        ]
    )
    assert code == 0
    proposal = destination / f"{value['session']}-{GENERATION}-source-repair.json"
    assert proposal.exists(), "report CLI must publish the adjacent proposal"
    result = json.loads(proposal.read_text())
    assert result["version"] == "eventedge-source-repair-proposal-v1"
    assert result["status"] == "stale"
    assert result["requests"] == result["requires_user"] == []
    stem = destination / f"{value['session']}-{GENERATION}-operational-report"
    assert (
        result["report_sha256"]
        == hashlib.sha256(
            stem.with_suffix(".json").read_bytes()
            + b"\0"
            + stem.with_suffix(".md").read_bytes()
        ).hexdigest()
    )


def test_cli_has_only_source_owned_request_and_export_operations():
    path = Path(__file__).resolve().parents[1] / "scripts/source_repair.py"
    assert path.is_file(), "source-only CLI must exist"
    result = subprocess.run(
        [
            sys.executable,
            str(path),
            "--help",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "{requests,interventions,export}" in result.stdout
    assert "--command" not in result.stdout


def test_capture_detects_replacement_after_read(evidence, monkeypatch):
    root, request, source, manifest_path, output = evidence
    module = api()
    verify = module._Capture.verify

    def replaced(capture):
        alternate = source.with_name("replacement.json")
        alternate.write_bytes(source.read_bytes())
        alternate.replace(source)
        verify(capture)

    monkeypatch.setattr(module._Capture, "verify", replaced)
    with pytest.raises(ValueError, match="changed"):
        module.export_repair_evidence(root, request, output, now=NOW)
    assert not output.exists()


def test_export_rejects_forged_report_failure_even_when_pair_and_request_are_valid(
    evidence,
):
    root, request, source, manifest_path, output = evidence
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore

    envelope = SourceInputStore.decode(source.read_text())
    envelope["payload"] = {"fred": {"_coverage": {"status": "success_empty"}}}
    envelope["digest"] = hashlib.sha256(
        SourceInputStore.encode(envelope["payload"]).encode()
    ).hexdigest()
    source.write_text(SourceInputStore.encode(envelope))
    with pytest.raises(ValueError, match="corroborated"):
        api().export_repair_evidence(root, request, output, now=NOW)


@pytest.mark.parametrize("bound", ["MAX_FILES", "MAX_FILE_BYTES", "MAX_TOTAL_BYTES"])
def test_evidence_limits_fail_visibly_without_partial_bundle(
    evidence, monkeypatch, bound
):
    root, request, source, manifest_path, output = evidence
    monkeypatch.setattr(api(), bound, 1)
    with pytest.raises(ValueError):
        api().export_repair_evidence(root, request, output, now=NOW)
    assert not output.exists()


@pytest.mark.parametrize(
    "key",
    [
        "provider",
        "reason_code",
        "configuration",
        "http_status",
        "version",
        "session",
        "generation_commit",
    ],
)
def test_request_nonscalar_types_raise_validation_error(key):
    request = build(report())["requests"][0]
    request[key] = {"untrusted": "value"}
    with pytest.raises(ValueError):
        api().validate_repair_request(request, now=NOW)


@pytest.mark.parametrize(
    "damage", [None, "recovered_middle", "missing_middle", "changed_configuration"]
)
def test_persistent_repair_requires_accepted_failure_history(evidence, damage):
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore

    root, old_request, source, manifest_path, output = evidence
    commit = old_request["generation_commit"]
    store = SourceInputStore(root / "cache", accepted_dir=source.parent)
    previous = None
    source.unlink()
    for day in ("2026-10-06", "2026-10-07", "2026-10-08"):
        identity = {
            "generation": GENERATION,
            "commit": commit,
            "session": day,
            "configuration": "different"
            if damage == "changed_configuration" and day == "2026-10-07"
            else "cfg123",
        }
        if damage != "missing_middle" or day != "2026-10-07":
            status = (
                "success_empty"
                if damage == "recovered_middle" and day == "2026-10-07"
                else "failed"
            )
            store.freeze(
                identity,
                {"fred": {"_coverage": {"status": status, "reason_code": "timeout"}}},
                acquired_at=datetime.fromisoformat(day + "T22:00:00+00:00"),
            )
        # The report proposal claims uninterrupted failure even in damage cases.
        previous = report(day, previous=previous, reason="timeout", commit=commit)
    now = datetime.fromisoformat("2026-10-08T23:00:00+00:00")
    stem = root / "docs/reports/2026-10-08-gen_020-operational-report"
    stem.with_suffix(".json").write_bytes(pair(previous)[0])
    stem.with_suffix(".md").write_bytes(pair(previous)[1])
    request = build(previous, expected_commit=commit, now=now)["requests"][0]
    if damage is None:
        bundle = api().export_repair_evidence(root, request, output, now=now)
        assert (
            len(bundle["files"]) == 7
        )  # Three failures, prior healthy source, manifest/pair.
    else:
        with pytest.raises((ValueError, OSError)):
            api().export_repair_evidence(root, request, output, now=now)
        assert not output.exists()


def test_export_rechecks_actual_clock_before_publication(evidence, monkeypatch):
    root, request, source, manifest_path, output = evidence
    values = iter([NOW, datetime(2026, 10, 7, 23, tzinfo=timezone.utc)])
    monkeypatch.setattr(
        api(), "_now", lambda value=None: next(values) if value is None else value
    )
    with pytest.raises(ValueError, match="stale"):
        api().export_repair_evidence(root, request, output)
    assert not output.exists()


def test_export_never_opens_canonical_runtime_lock_for_writing(evidence, monkeypatch):
    root, request, source, manifest_path, output = evidence
    module = api()
    real_open = module.os.open

    def read_only_lock(path, flags, *args, **kwargs):
        if str(path).endswith("eventedge-runtime.lock"):
            assert not flags & (
                module.os.O_RDWR | module.os.O_WRONLY | module.os.O_CREAT
            ), "repair export must open existing lock read-only"
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", read_only_lock)
    module.export_repair_evidence(root, request, output, now=NOW)


def test_report_cannot_reset_first_failure_to_evade_incident_identity(evidence):
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore

    root, request, source, manifest_path, output = evidence
    store = SourceInputStore(root / "cache", accepted_dir=source.parent)
    identity = {
        "generation": GENERATION,
        "commit": request["generation_commit"],
        "session": "2026-10-05",
        "configuration": "cfg123",
    }
    store._path(identity, frozen=True).unlink()
    store.freeze(
        identity,
        {
            "fred": {
                "_coverage": {"status": "failed", "reason_code": "invalid_response"}
            }
        },
        acquired_at=datetime.fromisoformat("2026-10-05T22:00:00+00:00"),
    )
    with pytest.raises(ValueError, match="origin"):
        api().export_repair_evidence(root, request, output, now=NOW)
    assert not output.exists()


def test_export_fails_closed_without_origin_evidence(evidence):
    from tradingagents.strategies.orchestration.source_inputs import (
        configuration_fingerprint,
    )

    root, request, source, manifest_path, output = evidence
    previous = source.parent / (
        configuration_fingerprint({"generation": GENERATION, "session": "2026-10-05"})
        + ".json"
    )
    previous.unlink()
    with pytest.raises(OSError):
        api().export_repair_evidence(root, request, output, now=NOW)
    assert not output.exists()


def test_missing_runtime_lock_is_never_created_by_repair_export(evidence):
    root, request, source, manifest_path, output = evidence
    lock = root / "data/operational/eventedge-runtime.lock"
    lock.unlink()
    with pytest.raises(OSError):
        api().export_repair_evidence(root, request, output, now=NOW)
    assert not lock.exists() and not output.exists()


def test_calendar_overflow_is_normalized_to_request_validation_error():
    request = build(report())["requests"][0]
    request["first_failed_session"] = "9999-01-01"
    with pytest.raises(ValueError):
        api().validate_repair_request(request, now=NOW)
