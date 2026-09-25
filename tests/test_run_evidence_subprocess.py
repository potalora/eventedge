"""Exercise the real process boundary with synthetic workers and isolated state."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tradingagents.strategies.orchestration import generation_manager as gm
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    build_default_cohorts,
)


@pytest.fixture
def worker(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    state = tmp_path / "data" / "generations" / "gen_099"
    state.mkdir(parents=True)
    cohorts = build_default_cohorts({})
    assert len(cohorts) == 16
    for cohort in cohorts:
        with sqlite3.connect(state / f"{cohort.name}.db") as connection:
            connection.execute("CREATE TABLE sentinel (balance INTEGER)")
            connection.execute("INSERT INTO sentinel VALUES (12345)")
    (state / "manifest.json").write_text('{"frozen": true}')
    script = tmp_path / "worker" / "scripts" / "run_cohorts.py"
    script.parent.mkdir(parents=True)
    manager = gm.GenerationManager(str(tmp_path))
    manager._venv_python = Path(sys.executable)
    generation = {
        "gen_id": "gen_099",
        "git_commit": "a" * 40,
        "state_dir": str(state),
        "worktree_path": str(script.parent.parent),
    }
    return manager, generation, script, state


def _snapshot(state):
    return {
        str(path.relative_to(state)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in state.rglob("*")
        if path.is_file()
    }


def _envelope():
    return "EVENTEDGE_DAILY_RESULT_V1=" + json.dumps(
        {
            "wire_version": 1,
            "cohort_results": {
                cohort.name: {
                    "error": False,
                    "degraded": False,
                    "execution_valid": True,
                    "staging_valid": True,
                }
                for cohort in build_default_cohorts({})
            },
        }
    )


def test_real_repeated_success_failure_success_keeps_all_attempts(worker):
    manager, generation, script, state = worker
    before = _snapshot(state)
    artifacts = []
    for index, code in enumerate([0, 7, 0]):
        script.write_text(
            f"import sys\nprint({_envelope()!r})\n"
            f"print('attempt-{index}', file=sys.stderr)\nsys.exit({code})\n"
        )
        result = manager._run_cohorts_subprocess(
            generation,
            ["--date", "2026-09-04"],
            write_log=False,
        )
        assert result["outcome"] == ("clean" if code == 0 else "failed")
        path = Path(result["evidence_path"])
        payload = json.loads(path.read_text())
        assert payload["process_return_code"] == code
        assert payload["process_status"] == "completed"
        assert payload["stderr"] == f"attempt-{index}\n"
        assert payload["stdout"].strip() == _envelope()
        artifacts.append((path, path.read_bytes()))
    assert len({path for path, _ in artifacts}) == 3
    assert all(path.read_bytes() == content for path, content in artifacts)
    assert _snapshot(state) == before


def test_real_timeout_retains_both_streams_and_child_is_reaped(worker, monkeypatch):
    manager, generation, script, state = worker
    before = _snapshot(state)
    script.write_text(
        "import os, sys, time\n"
        "print(f'child_pid={os.getpid()}', flush=True)\n"
        "print('partial diagnostic', file=sys.stderr, flush=True)\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setattr(gm, "_GENERATION_TIMEOUT_S", 1)
    result = manager._run_cohorts_subprocess(
        generation,
        ["--date", "2026-09-04"],
        write_log=False,
    )
    payload = json.loads(Path(result["evidence_path"]).read_text())
    assert result["outcome"] == "failed"
    assert payload["process_status"] == "timeout"
    assert payload["process_return_code"] is None
    assert payload["stderr"] == "partial diagnostic\n"
    child_pid = int(payload["stdout"].strip().split("=")[1])
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert _snapshot(state) == before


@pytest.mark.parametrize("mode", ["all", "screen", "governed"])
def test_real_preflight_failure_keeps_raw_evidence_without_state_mutation(worker, mode):
    manager, generation, script, state = worker
    before = _snapshot(state)
    script.write_text(
        "import sys\nprint('provider failed before report')\n"
        "print('synthetic traceback', file=sys.stderr)\nsys.exit(1)\n"
    )
    result = manager._run_cohorts_subprocess(
        generation,
        ["--date", "2026-09-04", "--preflight"],
        preflight_mode=mode,
        write_log=False,
    )
    payload = json.loads(Path(result["evidence_path"]).read_text())
    assert result["success"] is False
    assert "outcome" not in result
    assert payload["action"] == "preflight"
    assert payload["preflight_mode"] == mode
    assert payload["stderr"] == "synthetic traceback\n"
    assert payload["stdout"] == "provider failed before report\n"
    assert _snapshot(state) == before


def test_real_missing_executable_is_archived(worker):
    manager, generation, _, state = worker
    before = _snapshot(state)
    manager._venv_python = state / "missing-python"
    result = manager._run_cohorts_subprocess(generation, [], write_log=False)
    payload = json.loads(Path(result["evidence_path"]).read_text())
    assert result["outcome"] == "failed"
    assert payload["process_status"] == "launch_error"
    assert "missing-python" in payload["result"]["error"]
    assert _snapshot(state) == before


def test_real_archive_path_failure_does_not_rewrite_worker_outcome(worker):
    manager, generation, script, state = worker
    before = _snapshot(state)
    archive_parent = manager._repo_root / "data" / "logs"
    archive_parent.mkdir(parents=True, exist_ok=True)
    (archive_parent / "run_attempts").write_text("not a directory")
    script.write_text(f"print({_envelope()!r})\n")
    result = manager._run_cohorts_subprocess(generation, [], write_log=False)
    assert result["outcome"] == "clean"
    assert result["success"] is True
    assert "evidence_error" in result
    assert "evidence_path" not in result
    assert _snapshot(state) == before
