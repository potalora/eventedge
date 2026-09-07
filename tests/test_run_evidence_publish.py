"""Failure injection for archive publication: keep earlier evidence intact."""

from unittest.mock import Mock

import pytest

from tradingagents.strategies.orchestration import run_evidence


def _attempt():
    return {
        "schema_version": 1,
        "generation_id": "gen_013",
        "generation_commit": "a" * 40,
        "requested_session": "2026-09-04",
        "action": "preflight",
        "preflight_mode": "governed",
        "started_at": "2026-09-04T22:00:00+00:00",
        "finished_at": "2026-09-04T22:00:04+00:00",
        "process_return_code": 1,
        "process_status": "completed",
        "stdout": "original failure",
        "stderr": "",
        "result": {"success": False},
    }


def test_collision_cannot_replace_previous_attempt(tmp_path, monkeypatch):
    clock = Mock()
    clock.now.return_value.strftime.return_value = "fixed-time"
    monkeypatch.setattr(run_evidence, "datetime", clock)
    monkeypatch.setattr(run_evidence.uuid, "uuid4", lambda: Mock(hex="fixed-id"))
    path = run_evidence.persist_run_evidence(tmp_path, _attempt())
    before = path.read_bytes()

    changed = {**_attempt(), "stdout": "different attempt"}
    with pytest.raises(FileExistsError):
        run_evidence.persist_run_evidence(tmp_path, changed)

    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


def test_failed_publication_leaves_no_final_or_temporary_artifact(
    tmp_path, monkeypatch
):
    def fail_publish(*args):
        raise OSError("simulated publication failure")

    monkeypatch.setattr(run_evidence.os, "link", fail_publish)
    with pytest.raises(OSError, match="simulated publication failure"):
        run_evidence.persist_run_evidence(tmp_path, _attempt())

    assert list((tmp_path / "data/logs/run_attempts").iterdir()) == []
