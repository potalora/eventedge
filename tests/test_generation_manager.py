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


def _init_empty_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)


_DAILY_RESULT_PREFIX = "EVENTEDGE_DAILY_RESULT_V1="


def _valid_daily_results():
    from tradingagents.strategies.orchestration.cohort_orchestrator import (
        build_default_cohorts,
    )

    return {
        cohort.name: {
            "error": False,
            "degraded": False,
            "execution_valid": True,
            "staging_valid": True,
        }
        for cohort in build_default_cohorts({})
    }


def _valid_daily_stdout(*, log_line=""):
    envelope = {"wire_version": 1, "cohort_results": _valid_daily_results()}
    return f"{log_line}{_DAILY_RESULT_PREFIX}{json.dumps(envelope)}\n"


# ------------------------------------------------------------------
# TestGenerationStart
# ------------------------------------------------------------------


class TestGenerationStart:
    def test_manager_in_linked_worktree_uses_main_repo_runtime_lock(self, git_repo):
        linked = git_repo.parent / "linked-manager"
        subprocess.run(
            ["git", "-C", str(git_repo), "worktree", "add", str(linked), "--detach"],
            check=True,
            capture_output=True,
        )

        linked_manager = GenerationManager(str(linked))

        assert linked_manager._runtime_lock_path == (
            git_repo / "data" / "operational" / "eventedge-runtime.lock"
        ).resolve()

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
    def test_direct_run_cohorts_daily_locks_before_orchestrator_construction(
        self, monkeypatch
    ):
        from contextlib import contextmanager

        from scripts import run_cohorts
        from tradingagents.strategies.orchestration import cohort_orchestrator

        active = []

        @contextmanager
        def lock_context(*, exclusive):
            assert exclusive is True
            active.append(True)
            try:
                yield MagicMock()
            finally:
                active.pop()

        class Orchestrator:
            def __init__(self, *args, **kwargs):
                assert active == [True]

            def run_daily(self, trading_date):
                assert active == [True]
                return {}

        monkeypatch.setattr(run_cohorts, "_runtime_lock_context", lock_context)
        monkeypatch.setattr(cohort_orchestrator, "CohortOrchestrator", Orchestrator)
        monkeypatch.setattr(cohort_orchestrator, "build_default_cohorts", lambda config: [])
        monkeypatch.setenv("EVENTEDGE_GENERATION_ID", "gen_001")
        monkeypatch.setenv("EVENTEDGE_GENERATION_COMMIT", "a" * 40)
        monkeypatch.setattr(
            sys,
            "argv",
            ["run_cohorts.py", "--date", "2026-08-06"],
        )

        run_cohorts.main()
        assert active == []

    def test_manager_preflight_preserves_whole_generation_tree(
        self, git_repo, manager, monkeypatch
    ):
        import hashlib

        import tradingagents.strategies.orchestration.generation_manager as gm

        manager.start_generation("read-only preflight")
        tree = git_repo / "data" / "generations"

        def identity():
            result = {}
            for path in sorted(tree.rglob("*")):
                relative = str(path.relative_to(tree))
                stat_result = path.stat()
                result[relative] = (
                    path.is_dir(),
                    stat_result.st_ino,
                    stat_result.st_size,
                    stat_result.st_mtime_ns,
                    (
                        ""
                        if path.is_dir()
                        else hashlib.sha256(path.read_bytes()).hexdigest()
                    ),
                )
            return result

        report = {
            "ok": True,
            "screen_ok": True,
            "screen_failures": [],
            "failures": [],
            "horizons": {},
        }
        process = MagicMock(
            returncode=0, stdout=json.dumps(report, indent=2), stderr=""
        )
        monkeypatch.setattr(gm.subprocess, "run", lambda *args, **kwargs: process)
        before = identity()

        results = manager.run_preflight("2026-08-06", mode="screen")

        assert results["gen_001"]["success"] is True
        assert identity() == before
        assert not any(tree.rglob("last_preflight_output.log"))

    def test_daily_busy_is_bounded_and_starts_no_child_or_history(
        self, git_repo, manager
    ):
        from tradingagents.strategies.orchestration.runtime_lock import (
            RuntimeLockBusy,
            runtime_lock,
        )

        manager.start_generation("busy daily")
        before = manager.get_generation("gen_001").run_history
        with patch.object(manager, "_run_cohorts_subprocess") as run_child:
            with runtime_lock(manager._runtime_lock_path, exclusive=False):
                with pytest.raises(RuntimeLockBusy, match="runtime lock is busy"):
                    manager.run_daily("2026-08-06")

        run_child.assert_not_called()
        assert manager.get_generation("gen_001").run_history == before == []

    def test_daily_exclusive_lock_spans_child_history_and_manifest_save(
        self, git_repo, manager, monkeypatch
    ):
        from tradingagents.strategies.orchestration.runtime_lock import (
            RuntimeLockBusy,
            runtime_lock,
        )

        manager.start_generation("exclusive daily")
        observed = []

        def child(gen_data, extra_args, *, inherited_lock=None, **kwargs):
            assert inherited_lock is not None and inherited_lock.exclusive is True
            with pytest.raises(RuntimeLockBusy):
                with runtime_lock(manager._runtime_lock_path, exclusive=False):
                    raise AssertionError("unreachable")
            observed.append("child")
            return {"outcome": "clean", "success": True, "elapsed_s": 1.0}

        original_save = manager._save_manifest

        def save_while_locked(manifest):
            with pytest.raises(RuntimeLockBusy):
                with runtime_lock(manager._runtime_lock_path, exclusive=False):
                    raise AssertionError("unreachable")
            observed.append("save")
            original_save(manifest)

        monkeypatch.setattr(manager, "_run_cohorts_subprocess", child)
        monkeypatch.setattr(manager, "_save_manifest", save_while_locked)

        manager.run_daily("2026-08-06")

        assert observed == ["child", "save"]

    def test_daily_lock_releases_when_child_raises(self, git_repo, manager):
        from tradingagents.strategies.orchestration.runtime_lock import runtime_lock

        manager.start_generation("raising child")
        with patch.object(
            manager, "_run_cohorts_subprocess", side_effect=RuntimeError("boom")
        ):
            with pytest.raises(RuntimeError, match="boom"):
                manager.run_daily("2026-08-06")

        with runtime_lock(manager._runtime_lock_path, exclusive=True):
            pass

    def test_subprocess_inherits_verified_lock_fd_and_mode(
        self, git_repo, manager
    ):
        import tradingagents.strategies.orchestration.generation_manager as gm_mod
        from tradingagents.strategies.orchestration.runtime_lock import runtime_lock

        info = manager.start_generation("inherited lock handoff")
        captured = {}

        def capture_run(*args, **kwargs):
            captured.update(kwargs)
            return MagicMock(
                returncode=0, stdout=_valid_daily_stdout(), stderr=""
            )

        gen_data = {
            "gen_id": info.gen_id,
            "git_commit": info.git_commit,
            "state_dir": info.state_dir,
            "worktree_path": info.worktree_path,
        }
        with runtime_lock(manager._runtime_lock_path, exclusive=True) as lock:
            with patch.object(gm_mod.subprocess, "run", side_effect=capture_run):
                manager._run_cohorts_subprocess(
                    gen_data, ["--date", "2026-08-06"], inherited_lock=lock
                )

        assert captured["pass_fds"] == (lock.fd,)
        assert captured["env"]["EVENTEDGE_RUNTIME_LOCK_FD"] == str(lock.fd)
        assert captured["env"]["EVENTEDGE_RUNTIME_LOCK_MODE"] == "exclusive"

    def test_run_daily_executes_subprocess(self, git_repo, manager):
        """Verify env vars and cwd are set correctly for subprocess calls."""
        # Start generation with real git (no mocking yet)
        manager.start_generation("test daily")

        # Now mock only the _run_cohorts_subprocess internal method
        with patch.object(manager, "_run_cohorts_subprocess") as mock_rcs:
            mock_rcs.return_value = {
                "outcome": "clean",
                "success": True,
                "elapsed_s": 1.5,
            }

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
            mock_result.stdout = _valid_daily_stdout()
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
            mock_rcs.return_value = {
                "outcome": "clean",
                "success": True,
                "elapsed_s": 2.1,
            }
            manager.run_daily("2026-03-31")

        gen = manager.get_generation("gen_001")
        assert gen is not None
        assert len(gen.run_history) == 1
        entry = gen.run_history[0]
        assert entry["date"] == "2026-03-31"
        assert entry["outcome"] == "clean"
        assert entry["success"] is True
        assert "elapsed_s" in entry

    def test_run_daily_records_degraded_history(self, git_repo, manager):
        """Candidate quarantine is alertable without becoming an execution failure."""
        manager.start_generation("degraded history test")

        with patch.object(manager, "_run_cohorts_subprocess") as mock_rcs:
            mock_rcs.return_value = {
                "outcome": "degraded",
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
        assert entry["outcome"] == "degraded"
        assert entry["success"] is False
        assert entry["degraded"] is True
        assert entry["execution_valid"] is True

    def test_run_daily_persists_failed_outcome(self, git_repo, manager):
        manager.start_generation("failed history test")
        with patch.object(manager, "_run_cohorts_subprocess") as run:
            run.return_value = {
                "outcome": "failed",
                "success": False,
                "execution_valid": False,
                "elapsed_s": 0.5,
                "error": "governed input invalid",
            }
            manager.run_daily("2026-03-31")
        entry = manager.get_generation("gen_001").run_history[0]
        assert entry["outcome"] == "failed"
        assert entry["success"] is False

    def test_run_daily_records_governed_recovery_and_failure_history(
        self, git_repo, manager
    ):
        manager.start_generation("governed history test")
        summary = {
            "ticker": "ESS",
            "session": "2026-08-10",
            "recovery_id": "governed_bar_recovery:" + "b" * 64,
            "contract_version": "yfinance-60m-v1",
            "evidence_digest": "sha256:" + "a" * 64,
            "affected_cohort_ids": ["cohort-a"],
        }
        with patch.object(manager, "_run_cohorts_subprocess") as mock_rcs:
            mock_rcs.return_value = {
                "outcome": "degraded",
                "success": False,
                "degraded": True,
                "execution_valid": True,
                "governed_bar_recoveries": [summary],
                "governed_failure_map": {"SPY": "invalid_benchmark SPY/2026-08-10"},
                "elapsed_s": 2.1,
                "error": "governed market data",
            }
            manager.run_daily("2026-08-10")

        entry = manager.get_generation("gen_001").run_history[0]
        assert entry["governed_bar_recoveries"] == [summary]
        assert entry["governed_failure_map"] == {
            "SPY": "invalid_benchmark SPY/2026-08-10"
        }

    def test_daily_history_sorts_canonical_governed_evidence_and_omits_raw_values(
        self, git_repo, manager
    ):
        manager.start_generation("canonical governed history")

        def summary(ticker, digest):
            return {
                "ticker": ticker,
                "session": "2026-08-06",
                "recovery_id": "governed_bar_recovery:" + digest * 64,
                "contract_version": "yfinance-60m-v1",
                "evidence_digest": "sha256:" + digest * 64,
                "affected_cohort_ids": ["cohort-a", "cohort-b"],
            }

        with patch.object(manager, "_run_cohorts_subprocess") as run_child:
            run_child.side_effect = (
                {
                    "outcome": "degraded",
                    "success": False,
                    "degraded": True,
                    "execution_valid": True,
                    "governed_bar_recoveries": [summary("ZZZ", "b"), summary("AAA", "a")],
                    "governed_failure_map": {
                        "SPY": "invalid SPY/2026-08-06",
                        "BIL": "missing BIL/2026-08-06",
                    },
                    "elapsed_s": 1.0,
                },
                {
                    "outcome": "failed",
                    "success": False,
                    "governed_bar_recoveries": [
                        {"ticker": "RAW", "provider_secret": "do-not-persist"}
                    ],
                    "governed_failure_map": {
                        "RAW": "provider payload do-not-persist"
                    },
                    "elapsed_s": 1.0,
                },
            )
            manager.run_daily("2026-08-06")
            manager.run_daily("2026-08-06")

        history = manager.get_generation("gen_001").run_history
        assert [row["ticker"] for row in history[0]["governed_bar_recoveries"]] == [
            "AAA",
            "ZZZ",
        ]
        assert history[0]["governed_bar_recoveries"][0][
            "affected_cohort_ids"
        ] == ["cohort-a", "cohort-b"]
        assert list(history[0]["governed_failure_map"]) == ["BIL", "SPY"]
        assert "governed_bar_recoveries" not in history[1]
        assert "governed_failure_map" not in history[1]

    def test_daily_history_deduplicates_exact_summaries_and_omits_conflicts(
        self, git_repo, manager
    ):
        manager.start_generation("canonical governed conflicts")

        def summary(ticker, digest, cohorts):
            return {
                "ticker": ticker,
                "session": "2026-08-06",
                "recovery_id": "governed_bar_recovery:" + digest * 64,
                "contract_version": "yfinance-60m-v1",
                "evidence_digest": "sha256:" + digest * 64,
                "affected_cohort_ids": cohorts,
            }

        accepted = summary("AAA", "a", ["cohort-a"])
        conflicted = summary("ESS", "b", ["cohort-a"])
        with patch.object(manager, "_run_cohorts_subprocess") as run_child:
            run_child.return_value = {
                "outcome": "clean",
                "success": True,
                "elapsed_s": 1.0,
                "governed_bar_recoveries": [
                    accepted,
                    dict(accepted),
                    conflicted,
                    {**conflicted, "affected_cohort_ids": ["cohort-b"]},
                    {**accepted, "provider_secret": "do-not-persist"},
                ],
                "governed_failure_map": {
                    "SPY": "invalid SPY/2026-08-06",
                    "PROVIDER SECRET": "invalid PROVIDER SECRET/2026-08-06",
                },
            }
            manager.run_daily("2026-08-06")

        entry = manager.get_generation("gen_001").run_history[0]
        assert entry["governed_bar_recoveries"] == [accepted]
        assert entry["governed_failure_map"] == {
            "SPY": "invalid SPY/2026-08-06"
        }
        assert "SECRET" not in json.dumps(entry)

    def test_run_daily_records_mixed_failure_and_degradation_history(
        self, git_repo, manager
    ):
        manager.start_generation("mixed failure and degradation history test")

        with patch.object(manager, "_run_cohorts_subprocess") as mock_rcs:
            mock_rcs.return_value = {
                "outcome": "failed",
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
        assert entry["outcome"] == "failed"
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

        cohort_results = _valid_daily_results()
        cohort_results["horizon_30d_size_5k"].update(
            {"valid": False, "invalid_reason": "missing required mark"}
        )
        process = MagicMock(
            returncode=0,
            stdout=(
                _DAILY_RESULT_PREFIX
                + json.dumps(
                    {"wire_version": 1, "cohort_results": cohort_results}
                )
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

        cohort_results = _valid_daily_results()
        cohort_results["horizon_30d_size_5k"] = {
            "error": True,
            "invalid_reason": "candidate provider failed",
            "degraded": False,
            "execution_valid": True,
            "staging_valid": False,
            "candidate_bar_quarantines": [],
        }
        process = MagicMock(
            returncode=0,
            stdout=(
                _DAILY_RESULT_PREFIX
                + json.dumps(
                    {"wire_version": 1, "cohort_results": cohort_results}
                )
            ),
            stderr="",
        )
        with patch.object(gm_mod.subprocess, "run", return_value=process):
            results = manager.run_daily("2026-03-31")

        result = results[info.gen_id]
        assert result["outcome"] == "failed"
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
            "gen_001": {
                "outcome": "failed",
                "success": False,
                "elapsed_s": 1.0,
                "error": "invalid",
            },
            "gen_002": {"outcome": "clean", "success": True, "elapsed_s": 2.0},
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

    def test_run_daily_cli_prints_degraded_and_completes(
        self, monkeypatch, capsys
    ):
        """Monitoring must distinguish candidate quarantine from execution failure."""
        from scripts import run_generations

        results = {
            "gen_001": {
                "outcome": "degraded",
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

        run_generations.main()

        output = capsys.readouterr().out
        assert "gen_001: DEGRADED" in output

    def test_run_daily_cli_completes_when_clean_and_degraded_are_mixed(
        self, monkeypatch, capsys
    ):
        from scripts import run_generations

        results = {
            "gen_001": {"outcome": "clean", "success": True, "elapsed_s": 1.0},
            "gen_002": {
                "outcome": "degraded",
                "success": False,
                "degraded": True,
                "execution_valid": True,
                "elapsed_s": 2.0,
                "error": "candidate quarantined",
            },
        }
        monkeypatch.setattr(GenerationManager, "__init__", lambda self, *a, **k: None)
        monkeypatch.setattr(GenerationManager, "run_daily", lambda self, date: results)
        monkeypatch.setattr(
            sys,
            "argv",
            ["run_generations.py", "run-daily", "--date", "2026-07-31"],
        )

        run_generations.main()

        output = capsys.readouterr().out
        assert "gen_001: OK" in output
        assert "gen_002: DEGRADED" in output

    def test_run_daily_cli_prints_degradation_error_from_authoritative_outcome(
        self, monkeypatch, capsys
    ):
        from scripts import run_generations

        results = {
            "gen_001": {
                "outcome": "degraded",
                "success": True,
                "elapsed_s": 1.0,
                "error": "candidate quarantined",
            },
        }
        monkeypatch.setattr(GenerationManager, "__init__", lambda self, *a, **k: None)
        monkeypatch.setattr(GenerationManager, "run_daily", lambda self, date: results)
        monkeypatch.setattr(
            sys,
            "argv",
            ["run_generations.py", "run-daily", "--date", "2026-07-31"],
        )

        run_generations.main()

        output = capsys.readouterr().out
        assert "gen_001: DEGRADED" in output
        assert "candidate quarantined" in output


# ------------------------------------------------------------------
# TestFailureIsolation
# ------------------------------------------------------------------


class TestFailureIsolation:
    def test_one_failure_doesnt_block_others(self, git_repo, manager):
        """gen_001 fails, gen_002 succeeds -- both get results."""
        manager.start_generation("gen one")
        manager.start_generation("gen two")

        call_count = {"n": 0}

        def side_effect(gen_data, extra_args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return {
                    "outcome": "failed",
                    "success": False,
                    "elapsed_s": 0.5,
                    "error": "simulated",
                }
            return {"outcome": "clean", "success": True, "elapsed_s": 1.0}

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

        _init_empty_git_repo(tmp_path)
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

        _init_empty_git_repo(tmp_path)
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
            stdout = _valid_daily_stdout(
                log_line="Regulations.gov fetch: 20 proposed rules\n"
            )
            stderr = ""

        monkeypatch.setattr(gm.subprocess, "run", lambda *a, **k: _Proc())
        result = mgr._run_cohorts_subprocess(
            gen_data, ["run-daily", "--date", "2026-05-29"]
        )
        assert result["success"] is True
        log = state_dir / "last_run_output.log"
        assert log.exists()
        assert "Regulations.gov fetch: 20 proposed rules" in log.read_text()
