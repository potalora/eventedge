"""Regression tests: a daily run where cohorts errored must never be recorded
as a clean success.

2026-06-01 incident: FD exhaustion errored all 16 cohorts, but run_cohorts.py
exited 0 (per-cohort errors are caught and returned as {"error": true}), so the
manifest logged success:true and the failure was invisible until the missing
report was noticed.
"""
from __future__ import annotations

import json
from unittest.mock import patch

from tradingagents.strategies.orchestration.cohort_orchestrator import count_failed_cohorts
from tradingagents.strategies.orchestration.generation_manager import (
    GenerationManager,
    _extract_cohort_results,
)


# --- count_failed_cohorts (pure) ---

def test_count_failed_cohorts_all_errored():
    results = {f"c{i}": {"error": True} for i in range(16)}
    n_failed, n_total, failed = count_failed_cohorts(results)
    assert n_failed == 16
    assert n_total == 16
    assert failed == sorted(results)


def test_count_failed_cohorts_partial():
    results = {
        "a": {"error": True},
        "b": {"signals": [], "trades_opened": []},
        "c": {"error": True},
    }
    assert count_failed_cohorts(results) == (2, 3, ["a", "c"])


def test_count_failed_cohorts_none():
    results = {"a": {"signals": []}, "b": {"trades_opened": [1]}}
    assert count_failed_cohorts(results) == (0, 2, [])


def test_count_failed_cohorts_ignores_non_dicts_and_falsey_error():
    results = {"a": {"error": False}, "b": "weird", "c": {"error": None}}
    assert count_failed_cohorts(results) == (0, 3, [])


# --- _extract_cohort_results (parse run_cohorts.py stdout) ---

def test_extract_cohort_results_from_mixed_stdout():
    results = {"h_30d_5k": {"error": True}, "h_30d_10k": {"signals": []}}
    stdout = (
        "2026-06-01 10:00 INFO Registered data source: yfinance\n"
        "Selected: disaggregated_fut\n"
        "\nDaily trading completed for 2026-06-01 in 560.8s\n"
        + json.dumps(results, indent=2, default=str)
        + "\n"
    )
    extracted = _extract_cohort_results(stdout)
    assert extracted == results
    assert count_failed_cohorts(extracted)[0] == 1


def test_extract_cohort_results_garbage_returns_none():
    assert _extract_cohort_results("no json here\njust logs\n") is None
    assert _extract_cohort_results("") is None


# --- the core defect: rc==0 but cohorts failed => success False ---

class _FakeProc:
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_with_proc(tmp_path, proc):
    mgr = GenerationManager.__new__(GenerationManager)  # bypass __init__
    mgr._venv_python = "python"
    gen_data = {
        "gen_id": "gen_001",
        "state_dir": str(tmp_path / "state"),
        "worktree_path": str(tmp_path / "wt"),
    }
    with patch(
        "tradingagents.strategies.orchestration.generation_manager.subprocess.run",
        return_value=proc,
    ), patch.object(GenerationManager, "_write_run_log", lambda self, *a, **k: None):
        return mgr._run_cohorts_subprocess(gen_data, ["--date", "2026-06-01"])


def test_rc0_but_all_cohorts_failed_is_marked_failed(tmp_path):
    results = {f"c{i}": {"error": True} for i in range(16)}
    stdout = (
        "Daily trading completed for 2026-06-01 in 560.8s\n"
        + json.dumps(results, indent=2, default=str)
        + "\n"
    )
    result = _run_with_proc(tmp_path, _FakeProc(0, stdout))
    assert result["success"] is False
    assert "16/16 cohorts failed" in result["error"]


def test_rc0_partial_failure_is_marked_failed(tmp_path):
    results = {
        "c0": {"error": True},
        "c1": {"signals": [], "trades_opened": []},
    }
    stdout = "done\n" + json.dumps(results, indent=2, default=str) + "\n"
    result = _run_with_proc(tmp_path, _FakeProc(0, stdout))
    assert result["success"] is False
    assert "1/2 cohorts failed" in result["error"]


def test_rc0_all_success_stays_success(tmp_path):
    results = {f"c{i}": {"signals": [], "trades_opened": []} for i in range(16)}
    stdout = "done\n" + json.dumps(results, indent=2, default=str) + "\n"
    result = _run_with_proc(tmp_path, _FakeProc(0, stdout))
    assert result["success"] is True


def test_nonzero_rc_still_failed(tmp_path):
    result = _run_with_proc(tmp_path, _FakeProc(1, "boom\n", "traceback\n"))
    assert result["success"] is False
