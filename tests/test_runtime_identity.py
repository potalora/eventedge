"""Runtime provenance is recorded from the worker, without retaining package names."""
import hashlib
import json
from types import SimpleNamespace

from tradingagents.strategies.orchestration import run_evidence


def test_runtime_identity_reuses_continuity_distribution_fingerprint(tmp_path, monkeypatch):
    distributions = [SimpleNamespace(metadata={"Name": "zebra"}, version="2"),
                     SimpleNamespace(metadata={"Name": "alpha"}, version="1")]
    monkeypatch.setattr("importlib.metadata.distributions", lambda: distributions)
    (tmp_path / "uv.lock").write_bytes(b"lock bytes\n")
    identity = run_evidence.worker_runtime_identity(
        tmp_path, generation_id="gen_900", generation_commit="a" * 40,
        profile_sha256="b" * 64,
    )
    expected = hashlib.sha256(json.dumps([("alpha", "1"), ("zebra", "2")],
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    assert identity["dependencies_sha256"] == expected
    assert identity["lock_sha256"] == hashlib.sha256(b"lock bytes\n").hexdigest()
    assert identity["generation_commit"] == "a" * 40
    assert identity["profile_sha256"] == "b" * 64
    assert identity["python"]
    assert "zebra" not in json.dumps(identity)


def test_runtime_identity_records_absent_lock_without_inventing_one(tmp_path):
    identity = run_evidence.worker_runtime_identity(
        tmp_path, generation_id="gen_900", generation_commit="a" * 40,
        profile_sha256="b" * 64,
    )
    assert identity["lock_sha256"] is None


def test_runtime_identity_emitted_only_for_stabilization_profile(tmp_path, monkeypatch, capsys):
    from scripts import run_cohorts
    monkeypatch.setenv("EVENTEDGE_GENERATION_CONFIG_SHA256", "b" * 64)
    run_cohorts._record_runtime_identity({}, "gen_900", "a" * 40)
    assert capsys.readouterr().err == ""
    run_cohorts._record_runtime_identity(
        {"autoresearch": {"stabilization_profile_policy": "supported_sixteen_books_v1"}},
        "gen_900", "a" * 40,
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    line = captured.err.strip()
    assert line.startswith("EVENTEDGE_RUNTIME_IDENTITY_V1=")
    identity = json.loads(line.split("=", 1)[1])
    assert identity["profile_sha256"] == "b" * 64
    assert identity["generation_id"] == "gen_900"
