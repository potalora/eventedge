from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.migrate_metrics_v2 import build_legacy_registry
from tradingagents.strategies.metrics.models import LEGACY_SCHEMA_LABEL
from tradingagents.strategies.orchestration.generation_manager import (
    GenerationManager,
)


EXPECTED_LEGACY = {
    "metric_schema": LEGACY_SCHEMA_LABEL,
    "promotion_eligible": False,
    "reason": "legacy_same_bar_close_and_unreconciled_costs",
}


def _manifest() -> dict:
    return {
        "generations": [
            {"gen_id": "gen_004", "state_dir": "/must/not/read/gen_004"},
            {"gen_id": "gen_002", "state_dir": "/must/not/read/gen_002"},
            {"gen_id": "gen_000", "state_dir": "/must/not/read/gen_000"},
            {"gen_id": "gen_003", "state_dir": "/must/not/read/gen_003"},
            {"gen_id": "gen_001", "state_dir": "/must/not/read/gen_001"},
            {"gen_id": "generation-2", "state_dir": "/must/not/read/other"},
        ]
    }


def test_legacy_registry_is_pure_exact_and_generation_history_independent(
    monkeypatch,
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("generation artifacts must not be read or written")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)

    assert build_legacy_registry(_manifest()) == {
        "gen_001": EXPECTED_LEGACY,
        "gen_002": EXPECTED_LEGACY,
        "gen_003": EXPECTED_LEGACY,
    }


def test_legacy_registry_does_not_rewrite_generation_files(tmp_path) -> None:
    artifact = tmp_path / "gen_003" / "signal_journal.jsonl"
    artifact.parent.mkdir()
    artifact.write_text('{"legacy": true}\n')
    before = artifact.read_bytes()

    registry = build_legacy_registry(
        {"generations": [{"gen_id": "gen_003", "path": str(artifact.parent)}]}
    )

    assert registry == {"gen_003": EXPECTED_LEGACY}
    assert artifact.read_bytes() == before


def test_cli_dry_run_prints_registry_without_creating_output(tmp_path) -> None:
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "requested-registry.json"
    manifest_path.write_text(json.dumps(_manifest()))

    result = subprocess.run(
        [
            sys.executable,
            "scripts/migrate_metrics_v2.py",
            "--manifest",
            str(manifest_path),
            "--output",
            str(output_path),
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout) == build_legacy_registry(_manifest())
    assert not output_path.exists()


def test_cli_write_writes_only_requested_registry_file(tmp_path) -> None:
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "requested-registry.json"
    sentinel = tmp_path / "sentinel.txt"
    manifest_path.write_text(json.dumps(_manifest()))
    sentinel.write_text("unchanged")
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}

    subprocess.run(
        [
            sys.executable,
            "scripts/migrate_metrics_v2.py",
            "--manifest",
            str(manifest_path),
            "--output",
            str(output_path),
            "--write",
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(output_path.read_text()) == build_legacy_registry(_manifest())
    assert sentinel.read_text() == "unchanged"
    assert manifest_path.read_bytes() == before[manifest_path]
    assert sentinel.read_bytes() == before[sentinel]
    assert set(tmp_path.iterdir()) == {manifest_path, sentinel, output_path}


def test_cli_write_refuses_to_overwrite_manifest(tmp_path) -> None:
    manifest_path = tmp_path / "manifest.json"
    original = json.dumps(_manifest())
    manifest_path.write_text(original)

    result = subprocess.run(
        [
            sys.executable,
            "scripts/migrate_metrics_v2.py",
            "--manifest",
            str(manifest_path),
            "--output",
            str(manifest_path),
            "--write",
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "protected generation history" in result.stderr
    assert manifest_path.read_text() == original


def test_cli_write_refuses_output_inside_legacy_generation(tmp_path) -> None:
    legacy_state = tmp_path / "gen_003"
    legacy_state.mkdir()
    manifest = {
        "generations": [
            {"gen_id": "gen_003", "state_dir": str(legacy_state)},
            {"gen_id": "gen_004", "state_dir": str(tmp_path / "gen_004")},
        ]
    }
    manifest_path = tmp_path / "manifest.json"
    output_path = legacy_state / "metrics-registry.json"
    manifest_path.write_text(json.dumps(manifest))

    result = subprocess.run(
        [
            sys.executable,
            "scripts/migrate_metrics_v2.py",
            "--manifest",
            str(manifest_path),
            "--output",
            str(output_path),
            "--write",
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "protected generation history" in result.stderr
    assert not output_path.exists()


def test_cli_write_refuses_output_inside_newer_generation(tmp_path) -> None:
    generation_state = tmp_path / "gen_004"
    generation_state.mkdir()
    manifest = {
        "generations": [
            {"gen_id": "gen_004", "state_dir": str(generation_state)},
        ]
    }
    manifest_path = tmp_path / "manifest.json"
    output_path = generation_state / "metrics-registry.json"
    manifest_path.write_text(json.dumps(manifest))

    result = subprocess.run(
        [
            sys.executable,
            "scripts/migrate_metrics_v2.py",
            "--manifest",
            str(manifest_path),
            "--output",
            str(output_path),
            "--write",
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "protected generation history" in result.stderr
    assert not output_path.exists()


def test_generation_subprocess_env_includes_exact_generation_metadata(
    tmp_path,
) -> None:
    repo = tmp_path / "repo"
    worktree = repo / "frozen"
    state_dir = repo / "state"
    worktree.mkdir(parents=True)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    manager = GenerationManager(str(repo))
    captured: dict = {}

    def capture_run(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock(returncode=0, stdout="", stderr="")

    gen_data = {
        "gen_id": "gen_004",
        "git_commit": "abc123def456",
        "state_dir": str(state_dir),
        "worktree_path": str(worktree),
    }
    module = "tradingagents.strategies.orchestration.generation_manager.subprocess.run"
    with patch(module, side_effect=capture_run):
        result = manager._run_cohorts_subprocess(gen_data, ["--date", "2026-08-03"])

    assert result["success"] is True
    assert captured["env"]["EVENTEDGE_GENERATION_ID"] == "gen_004"
    assert captured["env"]["EVENTEDGE_GENERATION_COMMIT"] == "abc123def456"
    assert captured["env"]["AUTORESEARCH_STATE_DIR"] == str(state_dir.resolve())
    assert captured["env"]["PYTHONPATH"] == str(worktree.resolve())
    assert captured["env"] is not os.environ
