"""Scheduled profile boundaries through real entrypoints and subprocess locks."""
import hashlib
import json
from pathlib import Path

import pytest

from scheduled_acceptance.harness import create_harness


pytestmark = pytest.mark.scheduled_acceptance


def retained_state(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in Path(root).rglob("*") if path.is_file()}


@pytest.mark.parametrize("instant", [
    "2026-10-14T15:45:00+00:00",  # 11:45 EDT, regular session.
    "2026-11-02T16:45:00+00:00",  # 11:45 EST after the DST transition.
    "2026-11-27T17:59:59+00:00",  # One second before the 13:00 early close.
])
def test_screen_schedule_uses_real_profile_without_decision_or_state_authority(tmp_path, instant):
    harness = create_harness(tmp_path)
    before = retained_state(harness.state)
    result = harness.run(instant, entrypoint="preflight")
    # The approved rollback deliberately leaves EDGAR research unavailable.
    # Restoring diagnostic execution must not relabel that coverage loss clean.
    assert result.returncode == 1, result.stdout + result.stderr
    attempt = harness.attempts()[-1]
    assert attempt["action"] == "preflight"
    assert attempt["preflight_mode"] == "screen"
    assert attempt["result"]["success"] is False
    assert attempt["result"]["screen_runtime_failures"] == []
    assert {row["source"] for row in attempt["result"]["screen_source_failures"]} == {"edgar"}
    assert retained_state(harness.state) == before
    assert not [row for row in harness.trace() if row["kind"] in {"model", "forbidden_network"}]


@pytest.mark.parametrize("instant,status", [
    ("2026-10-14T19:59:59+00:00", "not_ready"),
    ("2026-10-14T20:00:01+00:00", "not_ready"),
    ("2026-10-14T20:14:59+00:00", "not_ready"),
    ("2026-10-14T20:15:00+00:00", "ready"),
    ("2026-11-27T17:59:59+00:00", "not_ready"),
    ("2026-11-27T18:00:01+00:00", "not_ready"),
    ("2026-11-27T18:15:00+00:00", "ready"),
])
def test_governed_schedule_preserves_exact_close_and_sip_delay(tmp_path, instant, status):
    harness = create_harness(tmp_path)
    before = retained_state(harness.state)
    result = harness.run(instant, entrypoint="governed")
    attempt = harness.attempts()[-1]
    assert attempt["result"]["governed_probe_status"] == status, attempt
    assert result.returncode == (0 if status == "ready" else 1)
    assert retained_state(harness.state) == before
    assert not [row for row in harness.trace() if row["kind"] in {"model", "forbidden_network"}]


@pytest.mark.parametrize("instant", [
    "2026-10-30T21:59:59+00:00",  # 17:59:59 EDT.
    "2026-11-02T22:59:59+00:00",  # 17:59:59 EST.
    "2026-11-27T18:15:00+00:00",  # Early close does not move the 18:00 schedule.
    "2026-11-26T23:00:00+00:00",  # Thanksgiving: weekday but not an XNYS session.
])
def test_controller_does_not_launch_outside_ordinary_due_window(tmp_path, instant):
    harness = create_harness(tmp_path)
    before = retained_state(harness.state)
    result = harness.run(instant, entrypoint="controller")
    assert result.returncode == 0, result.stdout + result.stderr
    assert harness.attempts() == []
    assert retained_state(harness.state) == before
    assert not [row for row in harness.trace() if row["kind"] in {"http", "sdk", "model", "forbidden_network"}]


@pytest.mark.parametrize("instant", [
    "2026-11-02T23:00:00+00:00",  # 18:00 EST after the DST transition.
    "2026-11-27T23:00:00+00:00",  # 18:00 EST despite the 13:00 early close.
])
def test_controller_dispatches_at_eighteen_local_after_dst_and_early_close(tmp_path, instant):
    from test_scheduled_timeout_acceptance import protected, assert_quiescent, cleanup_owned

    harness = create_harness(tmp_path, created_at=instant[:10] + "T00:00:00+00:00")
    before = protected(harness)
    try:
        # Stop at an external transport after real controller/shell dispatch.
        # The full positive financial path is exercised on ordinary sessions.
        result = harness.run(instant, fault="stall_transport", preflight_timeout=60,
                             daily_timeout=10, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr
        status = json.loads(result.stdout)
        assert status["status"] == "recovering"
        assert any(row["session"] == instant[:10] and row["reason_code"] == "worker_timeout"
                   for row in status["incidents"]), status
        stalled = [row for row in harness.trace() if row["kind"] == "stalled_transport"]
        assert stalled and all(row["url"].endswith("/v2/assets") for row in stalled)
        assert all(any("run_cohorts.py" in arg for arg in row["argv"]) for row in stalled)
        assert_quiescent(harness, before)
    finally:
        cleanup_owned(harness)


def test_weekend_wrapper_skips_without_constructing_a_historical_sample(tmp_path):
    harness = create_harness(tmp_path)
    before = retained_state(harness.state)
    result = harness.run("2026-10-17T15:45:00+00:00", entrypoint="preflight")
    assert result.returncode == 0
    assert "Weekend" in result.stdout
    assert harness.attempts() == []
    assert retained_state(harness.state) == before


def test_delayed_timer_records_missed_session_without_current_source_backfill(tmp_path):
    harness = create_harness(tmp_path)
    result = harness.run("2026-10-15T14:00:00+00:00", entrypoint="controller")
    assert result.returncode == 0, result.stdout + result.stderr
    journal = json.loads((harness.repo / "data/runtime_recovery/journal.json").read_text())
    records = list(journal["incidents"].values())
    assert len(records) == 1
    assert records[0]["identity"]["session"] == "2026-10-14"
    assert records[0]["disposition"] == "wait_next_session"
    assert records[0]["reason_code"] == "missed_session"
    assert harness.attempts() == []
    assert not [row for row in harness.trace() if row["kind"] in {"http", "sdk", "model", "forbidden_network"}]
