from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source)
    path.chmod(0o755)


def _shell_environment(
    tmp_path: Path, *, screen_rc: int = 0, governed_rc: int = 0, daily_rc: int = 0
) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.txt"
    python_stub = bin_dir / "python-stub"
    _write_executable(
        python_stub,
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CALLS_FILE"
case "$*" in
  *"preflight"*"--preflight-mode screen"*) exit "${SCREEN_RC:-0}" ;;
  *"preflight"*"--preflight-mode governed"*) exit "${GOVERNED_RC:-0}" ;;
  *"run-daily"*) exit "${DAILY_RC:-0}" ;;
esac
exit 99
""",
    )
    _write_executable(
        bin_dir / "date",
        """#!/usr/bin/env bash
case "${1:-}" in
  +%Y-%m-%d) echo 2026-08-06 ;;
  +%u) echo 4 ;;
  *) echo 'Thu Aug  6 22:05:00 EDT 2026' ;;
esac
""",
    )
    _write_executable(
        bin_dir / "caffeinate",
        """#!/usr/bin/env bash
shift
exec "$@"
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "EVENTEDGE_PYTHON": str(python_stub),
            "EVENTEDGE_LOG_DIR": str(tmp_path / "logs"),
            "CALLS_FILE": str(calls),
            "SCREEN_RC": str(screen_rc),
            "GOVERNED_RC": str(governed_rc),
            "DAILY_RC": str(daily_rc),
        }
    )
    return env, calls


def _calls(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def test_screen_failure_continues_to_governed_then_daily(tmp_path: Path) -> None:
    env, calls_path = _shell_environment(tmp_path, screen_rc=1, governed_rc=0)

    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "daily_trading.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    calls = _calls(calls_path)
    assert len(calls) == 3
    assert "preflight --date 2026-08-06 --preflight-mode screen" in calls[0]
    assert "preflight --date 2026-08-06 --preflight-mode governed" in calls[1]
    assert "run-daily --date 2026-08-06" in calls[2]


@pytest.mark.parametrize("label", ("failed", "busy"))
def test_governed_nonzero_never_invokes_daily(tmp_path: Path, label: str) -> None:
    env, calls_path = _shell_environment(tmp_path, governed_rc=1)
    env["GOVERNED_CASE"] = label

    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "daily_trading.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    calls = _calls(calls_path)
    assert len(calls) == 2
    assert "--preflight-mode screen" in calls[0]
    assert "--preflight-mode governed" in calls[1]
    assert all("run-daily" not in call for call in calls)


def test_governed_ready_with_recovery_invokes_daily_exactly_once(tmp_path: Path) -> None:
    env, calls_path = _shell_environment(tmp_path)

    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "daily_trading.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    calls = _calls(calls_path)
    assert sum("run-daily" in call for call in calls) == 1
    assert ["--preflight-mode screen" in calls[0], "--preflight-mode governed" in calls[1]] == [
        True,
        True,
    ]


def test_daily_failure_propagates_nonzero(tmp_path: Path) -> None:
    env, calls_path = _shell_environment(tmp_path, daily_rc=1)

    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "daily_trading.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    calls = _calls(calls_path)
    assert len(calls) == 3
    assert "run-daily --date 2026-08-06" in calls[2]


def test_midday_preflight_is_explicitly_screen_only(tmp_path: Path) -> None:
    env, calls_path = _shell_environment(tmp_path)

    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "preflight.sh")],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    calls = _calls(calls_path)
    assert len(calls) == 1
    assert "preflight --date 2026-08-06 --preflight-mode screen" in calls[0]
