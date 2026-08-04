"""Tests for GenerationManager and GenerationComparison.

Uses temporary git repos to test real worktree operations.
All subprocess calls for daily/learning runs are mocked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (tmp_path / "hello.py").write_text("print('hello')\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
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
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _head_branch(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _add_commit(repo: Path, filename: str, content: str, message: str) -> str:
    """Add a file, commit, and return the new HEAD sha."""
    (repo / filename).write_text(content)
    subprocess.run(
        ["git", "-C", str(repo), "add", filename],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", message],
        check=True,
        capture_output=True,
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
            "git_commit": info.git_commit,
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
        assert kwargs["env"]["EVENTEDGE_GENERATION_ID"] == info.gen_id
        assert kwargs["env"]["EVENTEDGE_GENERATION_COMMIT"] == info.git_commit
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

    def test_run_daily_records_degraded_history(self, git_repo, manager):
        """Candidate quarantine is alertable without becoming an execution failure."""
        manager.start_generation("degraded history test")

        with patch.object(manager, "_run_cohorts_subprocess") as mock_rcs:
            mock_rcs.return_value = {
                "success": False,
                "degraded": True,
                "execution_valid": True,
                "elapsed_s": 2.1,
                "error": "1/1 cohorts degraded: candidate_quarantined",
            }
            manager.run_daily("2026-03-31")

        gen = manager.get_generation("gen_001")
        assert gen is not None
        entry = gen.run_history[0]
        assert entry["success"] is False
        assert entry["degraded"] is True
        assert entry["execution_valid"] is True

    def test_run_daily_records_mixed_failure_and_degradation_history(
        self, git_repo, manager
    ):
        manager.start_generation("mixed failure and degradation history test")

        with patch.object(manager, "_run_cohorts_subprocess") as mock_rcs:
            mock_rcs.return_value = {
                "success": False,
                "degraded": True,
                "execution_valid": False,
                "candidate_bar_quarantines": ["ALX"],
                "elapsed_s": 2.1,
                "error": "1/2 cohorts failed; 1/2 cohorts degraded",
            }
            manager.run_daily("2026-03-31")

        gen = manager.get_generation("gen_001")
        assert gen is not None
        entry = gen.run_history[0]
        assert entry["success"] is False
        assert entry["degraded"] is True
        assert entry["execution_valid"] is False
        assert entry["candidate_bar_quarantines"] == ["ALX"]

    def test_subprocess_result_fails_when_any_cohort_is_invalid(
        self, git_repo, manager
    ):
        """An invalid lifecycle result is a failed generation even at exit code zero."""
        info = manager.start_generation("invalid cohort")
        import tradingagents.strategies.orchestration.generation_manager as gm_mod

        process = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "horizon_30d_size_5k": {
                        "error": False,
                        "valid": False,
                        "invalid_reason": "missing required mark",
                    },
                    "horizon_30d_size_10k": {"error": False, "valid": True},
                },
                indent=2,
            ),
            stderr="",
        )
        gen_data = {
            "gen_id": info.gen_id,
            "git_commit": info.git_commit,
            "state_dir": info.state_dir,
            "worktree_path": info.worktree_path,
        }
        with patch.object(gm_mod.subprocess, "run", return_value=process):
            result = manager._run_cohorts_subprocess(gen_data, ["--date", "2026-03-31"])

        assert result["success"] is False
        assert "horizon_30d_size_5k" in result["error"]

    def test_candidate_provider_failure_without_quarantine_preserves_p0_validity_in_history(
        self, git_repo, manager
    ):
        """P0-valid candidate failures remain alertable without a quarantine record."""
        info = manager.start_generation("candidate provider failure")
        import tradingagents.strategies.orchestration.generation_manager as gm_mod

        process = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "horizon_30d_size_5k": {
                        "error": True,
                        "invalid_reason": "candidate provider failed",
                        "degraded": False,
                        "execution_valid": True,
                        "staging_valid": False,
                        "candidate_bar_quarantines": [],
                    },
                },
                indent=2,
            ),
            stderr="",
        )
        with patch.object(gm_mod.subprocess, "run", return_value=process):
            results = manager.run_daily("2026-03-31")

        result = results[info.gen_id]
        assert result["success"] is False
        assert result["execution_valid"] is True
        entry = manager.get_generation(info.gen_id).run_history[0]
        assert entry["execution_valid"] is True

    def test_run_daily_cli_prints_all_results_then_exits_nonzero(
        self, monkeypatch, capsys
    ):
        """The scheduled top-level command must propagate generation failure."""
        from scripts import run_generations

        results = {
            "gen_001": {"success": False, "elapsed_s": 1.0, "error": "invalid"},
            "gen_002": {"success": True, "elapsed_s": 2.0},
        }
        monkeypatch.setattr(GenerationManager, "__init__", lambda self, *a, **k: None)
        monkeypatch.setattr(GenerationManager, "run_daily", lambda self, date: results)
        monkeypatch.setattr(
            sys,
            "argv",
            ["run_generations.py", "run-daily", "--date", "2026-07-31"],
        )

        with pytest.raises(SystemExit) as raised:
            run_generations.main()

        output = capsys.readouterr().out
        assert raised.value.code == 1
        assert "gen_001: FAILED" in output
        assert "gen_002: OK" in output

    def test_run_daily_cli_prints_degraded_then_exits_nonzero(
        self, monkeypatch, capsys
    ):
        """Monitoring must distinguish candidate quarantine from execution failure."""
        from scripts import run_generations

        results = {
            "gen_001": {
                "success": False,
                "degraded": True,
                "execution_valid": True,
                "elapsed_s": 1.0,
                "error": "1/1 cohorts degraded: candidate_quarantined",
            },
        }
        monkeypatch.setattr(GenerationManager, "__init__", lambda self, *a, **k: None)
        monkeypatch.setattr(GenerationManager, "run_daily", lambda self, date: results)
        monkeypatch.setattr(
            sys,
            "argv",
            ["run_generations.py", "run-daily", "--date", "2026-07-31"],
        )

        with pytest.raises(SystemExit) as raised:
            run_generations.main()

        output = capsys.readouterr().out
        assert raised.value.code == 1
        assert "gen_001: DEGRADED" in output


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
    def test_compare_delegates_explicit_pair(self):
        from datetime import date
        from unittest.mock import Mock

        from tradingagents.strategies.metrics.models import PairedComparison
        from tradingagents.strategies.metrics.service import MetricsService
        from tradingagents.strategies.orchestration.generation_comparison import (
            ComparisonPair,
            GenerationComparison,
        )

        candidate = Mock(spec=MetricsService)
        baseline = Mock(spec=MetricsService)
        candidate.compare.return_value = PairedComparison(
            "candidate-epoch",
            "baseline-epoch",
            (date(2026, 8, 4),),
            0.02,
            0.01,
            0.01,
        )
        pair = ComparisonPair(
            "gen-candidate",
            "candidate-cohort",
            "candidate-epoch",
            "gen-baseline",
            "baseline-cohort",
            "baseline-epoch",
        )

        result = GenerationComparison(
            {"gen-candidate": candidate, "gen-baseline": baseline}
        ).compare((pair,))

        assert result["metric_schema_version"] == 2
        assert result["comparisons"][0]["common_sessions"] == (date(2026, 8, 4),)
        candidate.compare.assert_called_once_with(
            "candidate-cohort",
            "candidate-epoch",
            baseline,
            "baseline-cohort",
            "baseline-epoch",
        )

    def test_compare_rejects_unknown_generation(self):
        from unittest.mock import Mock

        from tradingagents.strategies.metrics.service import MetricsService
        from tradingagents.strategies.orchestration.generation_comparison import (
            ComparisonPair,
            GenerationComparison,
        )

        service = Mock(spec=MetricsService)
        pair = ComparisonPair("missing", "a", "e1", "known", "b", "e2")
        with pytest.raises(KeyError, match="unknown generation"):
            GenerationComparison({"known": service}).compare((pair,))


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


class TestRunLogPersistence:
    """A generation run's captured output must be persisted for silent-strategy diagnosis."""

    def test_write_run_log_persists_stdout_stderr(self, tmp_path):
        from tradingagents.strategies.orchestration.generation_manager import (
            GenerationManager,
        )

        mgr = GenerationManager(str(tmp_path))
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        gen_data = {"gen_id": "gen_099", "state_dir": str(state_dir)}

        mgr._write_run_log(
            gen_data, "Finnhub fetch: 9 earnings, 1135 news", "WARN throttled"
        )

        log = state_dir / "last_run_output.log"
        assert log.exists()
        text = log.read_text()
        assert "gen_099" in text
        assert "Finnhub fetch: 9 earnings, 1135 news" in text
        assert "WARN throttled" in text

    def test_run_subprocess_writes_log_on_success(self, tmp_path, monkeypatch):
        from tradingagents.strategies.orchestration import generation_manager as gm

        mgr = gm.GenerationManager(str(tmp_path))
        state_dir = tmp_path / "s"
        state_dir.mkdir()
        wt = tmp_path / "wt"
        wt.mkdir()
        gen_data = {
            "gen_id": "gen_100",
            "git_commit": "frozen-commit-100",
            "state_dir": str(state_dir),
            "worktree_path": str(wt),
        }

        class _Proc:
            returncode = 0
            stdout = "Regulations.gov fetch: 20 proposed rules"
            stderr = ""

        monkeypatch.setattr(gm.subprocess, "run", lambda *a, **k: _Proc())
        result = mgr._run_cohorts_subprocess(
            gen_data, ["run-daily", "--date", "2026-05-29"]
        )
        assert result["success"] is True
        log = state_dir / "last_run_output.log"
        assert log.exists()
        assert "Regulations.gov fetch: 20 proposed rules" in log.read_text()
