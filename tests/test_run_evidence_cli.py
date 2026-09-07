"""Operator diagnostics must survive without changing the command's outcome."""

import sys

import pytest

from scripts import run_generations
from tradingagents.strategies.orchestration.generation_manager import GenerationManager


def _run(monkeypatch, command, result):
    monkeypatch.setattr(GenerationManager, "__init__", lambda self, *a, **k: None)
    method = "run_daily" if command == "run-daily" else "run_preflight"
    monkeypatch.setattr(
        GenerationManager, method, lambda self, *a, **k: {"gen_013": result}
    )
    argv = ["run_generations.py", command, "--date", "2026-09-04"]
    if command == "preflight":
        argv += ["--preflight-mode", "governed"]
    monkeypatch.setattr(sys, "argv", argv)
    run_generations.main()


def test_failed_governed_preflight_prints_reason_and_evidence(monkeypatch, capsys):
    result = {
        "success": False,
        "elapsed_s": 3.5,
        "error": "preflight governed status: failed",
        "governed_failure_map": {"BIL": "invalid BIL/2026-09-04"},
        "evidence_path": "/logs/run_attempts/friday.json",
    }
    with pytest.raises(SystemExit) as raised:
        _run(monkeypatch, "preflight", result)

    assert raised.value.code == 1
    output = capsys.readouterr().out
    assert "PREFLIGHT FAILED" in output
    assert "invalid BIL/2026-09-04" in output
    assert "Evidence: /logs/run_attempts/friday.json" in output


@pytest.mark.parametrize("command", ["run-daily", "preflight"])
def test_success_prints_evidence_location(monkeypatch, capsys, command):
    _run(
        monkeypatch,
        command,
        {
            "outcome": "clean",
            "success": True,
            "elapsed_s": 1.0,
            "evidence_path": "/logs/run_attempts/success.json",
        },
    )
    assert "Evidence: /logs/run_attempts/success.json" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["run-daily", "preflight"])
@pytest.mark.parametrize("failed", [False, True])
def test_archive_failure_is_visible_without_changing_exit(
    monkeypatch, capsys, command, failed
):
    result = {
        "outcome": "failed" if failed else "clean",
        "success": not failed,
        "elapsed_s": 1.0,
        "evidence_error": "PermissionError: cannot write attempt evidence",
    }
    if failed:
        with pytest.raises(SystemExit) as raised:
            _run(monkeypatch, command, result)
        assert raised.value.code == 1
    else:
        _run(monkeypatch, command, result)
    assert "Evidence unavailable:" in capsys.readouterr().out


def test_many_governed_failures_have_bounded_preview(monkeypatch, capsys):
    result = {
        "success": False,
        "elapsed_s": 1.0,
        "governed_failure_map": {
            f"T{i}": f"invalid T{i}/2026-09-04" for i in range(8)
        },
        "evidence_path": "/logs/run_attempts/complete.json",
    }
    with pytest.raises(SystemExit):
        _run(monkeypatch, "preflight", result)
    output = capsys.readouterr().out
    assert output.count("Governed data:") == 5
    assert "3 more governed failures" in output
    assert "Evidence: /logs/run_attempts/complete.json" in output
