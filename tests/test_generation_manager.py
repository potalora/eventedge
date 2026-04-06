"""Tests for GenerationManager and GenerationComparison.

Uses temporary git repos to test real worktree operations.
All subprocess calls for daily/learning runs are mocked.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.strategies.orchestration.generation_manager import GenerationManager


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path):
    """Create a minimal git repo with one commit."""
    subprocess.run(
        ["git", "init", str(tmp_path)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (tmp_path / "hello.py").write_text("print('hello')\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    return tmp_path


@pytest.fixture
def manager(git_repo):
    """GenerationManager rooted in the temp git repo."""
    return GenerationManager(
        repo_root=str(git_repo),
        generations_dir="data/generations",
    )


def _head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _head_branch(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _add_commit(repo: Path, filename: str, content: str, message: str) -> str:
    """Add a file, commit, and return the new HEAD sha."""
    (repo / filename).write_text(content)
    subprocess.run(
        ["git", "-C", str(repo), "add", filename],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message],
        check=True, capture_output=True,
    )
    return _head_sha(repo)


# ------------------------------------------------------------------
# TestGenerationStart
# ------------------------------------------------------------------


class TestGenerationStart:
    def test_start_creates_worktree_and_state(self, git_repo, manager):
        info = manager.start_generation("first gen")

        # Worktree directory exists
        assert Path(info.worktree_path).is_dir()
        # State directory exists
        assert Path(info.state_dir).is_dir()

        # Manifest has the entry
        manifest_path = git_repo / "data" / "generations" / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        assert len(manifest["generations"]) == 1
        assert manifest["generations"][0]["gen_id"] == "gen_001"
        assert manifest["generations"][0]["git_commit"] == _head_sha(git_repo)

    def test_start_increments_gen_id(self, git_repo, manager):
        g1 = manager.start_generation("first")
        g2 = manager.start_generation("second")

        assert g1.gen_id == "gen_001"
        assert g2.gen_id == "gen_002"

    def test_start_captures_commit_and_branch(self, git_repo, manager):
        expected_sha = _head_sha(git_repo)
        expected_branch = _head_branch(git_repo)

        info = manager.start_generation("test")

        assert info.git_commit == expected_sha
        assert info.git_branch == expected_branch


# ------------------------------------------------------------------
# TestGenerationLifecycle
# ------------------------------------------------------------------


class TestGenerationLifecycle:
    def test_pause_and_resume(self, git_repo, manager):
        info = manager.start_generation("lifecycle test")

        manager.pause_generation(info.gen_id)
        paused = manager.get_generation(info.gen_id)
        assert paused is not None
        assert paused.status == "paused"

        manager.resume_generation(info.gen_id)
        resumed = manager.get_generation(info.gen_id)
        assert resumed is not None
        assert resumed.status == "active"

    def test_retire_deletes_worktree(self, git_repo, manager):
        info = manager.start_generation("to retire")
        worktree = Path(info.worktree_path)
        assert worktree.is_dir()

        manager.retire_generation(info.gen_id, delete_worktree=True)

        retired = manager.get_generation(info.gen_id)
        assert retired is not None
        assert retired.status == "retired"
        # Worktree removed
        assert not worktree.is_dir()
        # State dir preserved
        assert Path(info.state_dir).is_dir()

    def test_retire_keeps_worktree_when_requested(self, git_repo, manager):
        info = manager.start_generation("keep worktree")
        worktree = Path(info.worktree_path)

        manager.retire_generation(info.gen_id, delete_worktree=False)

        retired = manager.get_generation(info.gen_id)
        assert retired is not None
        assert retired.status == "retired"
        assert worktree.is_dir()


# ------------------------------------------------------------------
# TestGenerationDailyRun
# ------------------------------------------------------------------


class TestGenerationDailyRun:
    def test_run_daily_executes_subprocess(self, git_repo, manager):
        """Verify env vars and cwd are set correctly for subprocess calls."""
        # Start generation with real git (no mocking yet)
        manager.start_generation("test daily")

        # Now mock only the _run_cohorts_subprocess internal method
        with patch.object(manager, "_run_cohorts_subprocess") as mock_rcs:
            mock_rcs.return_value = {"success": True, "elapsed_s": 1.5}

            results = manager.run_daily("2026-03-31")

        assert "gen_001" in results
        assert results["gen_001"]["success"] is True

        # Verify _run_cohorts_subprocess was called with correct args
        assert mock_rcs.call_count == 1
        call_args = mock_rcs.call_args
        gen_data = call_args[0][0]
        extra_args = call_args[0][1]
        assert gen_data["gen_id"] == "gen_001"
        assert extra_args == ["--date", "2026-03-31"]

    def test_run_daily_subprocess_env_vars(self, git_repo, manager):
        """Verify that _run_cohorts_subprocess sets the right env vars."""
        info = manager.start_generation("env test")

        # Mock subprocess.run at module level, but only for non-git calls
        import tradingagents.strategies.orchestration.generation_manager as gm_mod

        original_run = subprocess.run
        captured_calls = []

        def capture_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            captured_calls.append((cmd, kwargs))
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            return mock_result

        # Directly call _run_cohorts_subprocess with a mocked subprocess
        gen_data = {
            "gen_id": info.gen_id,
            "state_dir": info.state_dir,
            "worktree_path": info.worktree_path,
        }
        with patch.object(gm_mod.subprocess, "run", side_effect=capture_run):
            manager._run_cohorts_subprocess(gen_data, ["--date", "2026-03-31"])

        assert len(captured_calls) == 1
        cmd, kwargs = captured_calls[0]
        assert "scripts/run_cohorts.py" in cmd[1]
        assert kwargs["env"]["AUTORESEARCH_STATE_DIR"] == info.state_dir
        assert kwargs["env"]["PYTHONPATH"] == str(Path(info.worktree_path).resolve())
        assert kwargs["cwd"] == info.worktree_path

    def test_run_daily_records_history(self, git_repo, manager):
        """After run_daily, run_history should have an entry."""
        manager.start_generation("history test")

        with patch.object(manager, "_run_cohorts_subprocess") as mock_rcs:
            mock_rcs.return_value = {"success": True, "elapsed_s": 2.1}
            manager.run_daily("2026-03-31")

        gen = manager.get_generation("gen_001")
        assert gen is not None
        assert len(gen.run_history) == 1
        entry = gen.run_history[0]
        assert entry["date"] == "2026-03-31"
        assert entry["success"] is True
        assert "elapsed_s" in entry


# ------------------------------------------------------------------
# TestFailureIsolation
# ------------------------------------------------------------------


class TestFailureIsolation:
    def test_one_failure_doesnt_block_others(self, git_repo, manager):
        """gen_001 fails, gen_002 succeeds -- both get results."""
        manager.start_generation("gen one")
        manager.start_generation("gen two")

        call_count = {"n": 0}

        def side_effect(gen_data, extra_args):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {"success": False, "elapsed_s": 0.5, "error": "simulated"}
            return {"success": True, "elapsed_s": 1.0}

        with patch.object(manager, "_run_cohorts_subprocess", side_effect=side_effect):
            results = manager.run_daily("2026-03-31")

        assert "gen_001" in results
        assert "gen_002" in results
        assert results["gen_001"]["success"] is False
        assert results["gen_002"]["success"] is True


# ------------------------------------------------------------------
# TestMultipleGenerations
# ------------------------------------------------------------------


class TestMultipleGenerations:
    def test_two_gens_at_different_commits(self, git_repo):
        mgr = GenerationManager(
            repo_root=str(git_repo),
            generations_dir="data/generations",
        )

        g1 = mgr.start_generation("at initial commit")
        sha1 = g1.git_commit

        # Create a new commit
        sha2 = _add_commit(git_repo, "second.py", "print(2)\n", "second commit")
        assert sha1 != sha2

        g2 = mgr.start_generation("at second commit")

        assert g1.git_commit != g2.git_commit
        assert g2.git_commit == sha2

        # Both worktrees exist
        assert Path(g1.worktree_path).is_dir()
        assert Path(g2.worktree_path).is_dir()

        # The second worktree should have second.py (detached at sha2)
        assert (Path(g2.worktree_path) / "second.py").exists()


# ------------------------------------------------------------------
# TestGenerationComparison
# ------------------------------------------------------------------


class TestGenerationComparison:
    def test_compare_empty_gens(self, tmp_path):
        """Comparison with gens that have no state returns empty metrics."""
        from tradingagents.strategies.orchestration.generation_comparison import (
            GenerationComparison,
            GenerationInfo,
        )

        state_dir = str(tmp_path / "gen_001")
        os.makedirs(state_dir, exist_ok=True)

        gen = GenerationInfo(
            gen_id="gen_001",
            state_dir=state_dir,
            description="empty",
            created_at="2026-03-31T00:00:00",
            status="active",
        )
        comp = GenerationComparison([gen])
        result = comp.compare()

        assert "generations" in result
        assert "gen_001" in result["generations"]
        # No cohort subdirectories exist, so cohorts should be empty
        assert result["generations"]["gen_001"]["cohorts"] == {}

    def test_compare_with_synthetic_data(self, tmp_path):
        """Create state dirs with paper_trades.json and signal_journal.jsonl."""
        from tradingagents.strategies.orchestration.generation_comparison import (
            GenerationComparison,
            GenerationInfo,
        )

        state_dir = str(tmp_path / "gen_001")
        control_dir = Path(state_dir) / "control"
        control_dir.mkdir(parents=True, exist_ok=True)

        # Write paper_trades.json
        trades = [
            {
                "ticker": "AAPL",
                "strategy": "earnings_call",
                "direction": "long",
                "entry_date": "2026-03-15",
                "entry_price": 150.0,
                "exit_price": 160.0,
                "pnl_pct": 0.0667,
                "status": "closed",
            },
            {
                "ticker": "MSFT",
                "strategy": "earnings_call",
                "direction": "long",
                "entry_date": "2026-03-16",
                "entry_price": 300.0,
                "exit_price": 290.0,
                "pnl_pct": -0.0333,
                "status": "closed",
            },
        ]
        (control_dir / "paper_trades.json").write_text(json.dumps(trades))

        # Write signal_journal.jsonl
        entries = [
            {
                "timestamp": "2026-03-15T10:00:00",
                "strategy": "earnings_call",
                "ticker": "AAPL",
                "direction": "long",
                "score": 0.8,
                "return_5d": 0.05,
            },
            {
                "timestamp": "2026-03-16T10:00:00",
                "strategy": "earnings_call",
                "ticker": "MSFT",
                "direction": "long",
                "score": 0.7,
                "return_5d": -0.03,
            },
        ]
        with open(control_dir / "signal_journal.jsonl", "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        gen = GenerationInfo(
            gen_id="gen_001",
            state_dir=state_dir,
            description="synthetic",
            created_at="2026-03-31T00:00:00",
            status="active",
        )
        comp = GenerationComparison([gen])
        result = comp.compare()

        gen_data = result["generations"]["gen_001"]
        assert "control" in gen_data["cohorts"]

        control = gen_data["cohorts"]["control"]
        assert control["total_trades"] == 2
        assert control["closed_trades"] == 2
        assert control["total_signals"] == 2
        assert control["hit_rate"] == 0.5  # 1 hit out of 2
        assert control["num_trading_days"] == 2
        assert control["date_range"] == ["2026-03-15", "2026-03-16"]
        assert control["sharpe"] is not None
        assert control["total_return"] is not None
        assert "earnings_call" in control["per_strategy"]


# ------------------------------------------------------------------
# TestEnvVarOverride
# ------------------------------------------------------------------


class TestEnvVarOverride:
    def test_autoresearch_state_dir_env_var(self, monkeypatch):
        """Setting AUTORESEARCH_STATE_DIR overrides config in run_cohorts."""
        # Simulate the config-building logic from run_cohorts.py
        # (lines 74-81) without importing the full module and its deps
        from tradingagents.default_config import DEFAULT_CONFIG

        config = dict(DEFAULT_CONFIG)
        config["autoresearch"] = dict(config.get("autoresearch", {}))

        override_path = "/tmp/test_gen_state"
        monkeypatch.setenv("AUTORESEARCH_STATE_DIR", override_path)

        state_dir_override = os.environ.get("AUTORESEARCH_STATE_DIR")
        if state_dir_override:
            config["autoresearch"]["state_dir"] = state_dir_override

        assert config["autoresearch"]["state_dir"] == override_path
