"""Availability gate fixtures modeled on the September 2026 incident window."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

from tradingagents.strategies.orchestration.release_readiness import (
    COHORTS,
    assess_generation,
)
from tradingagents.strategies.orchestration.session_executor import PHASES

SESSIONS = (
    "2026-09-15",
    "2026-09-16",
    "2026-09-17",
    "2026-09-18",
    "2026-09-21",
)
COMMIT = "a" * 40


@contextmanager
def _db(path):
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _insert(connection, table, **values):
    # Production DDL, with neutral values for economic columns irrelevant to this gate.
    for _, name, kind, required, default, _ in connection.execute(
        f"PRAGMA table_info({table})"
    ):
        if name not in values and required and default is None:
            values[name] = 0 if kind == "INTEGER" else ""
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(values.values())
    )


def _fixture(tmp_path):
    from tradingagents.strategies.metrics.store import _SCHEMA
    from tradingagents.strategies.modules import get_paper_trade_strategies
    from tradingagents.strategies.state.portfolio_ledger import _DDL

    _git(tmp_path, "init")
    (tmp_path / "source.py").write_text("pass\n")
    _git(tmp_path, "add", "source.py")
    _git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    commit = _git(tmp_path, "rev-parse", "HEAD")
    worktree = tmp_path / ".worktrees" / "gen_015"
    _git(tmp_path, "worktree", "add", "--detach", str(worktree), commit)
    lock = tmp_path / "data" / "operational" / "eventedge-runtime.lock"
    lock.parent.mkdir(parents=True)
    lock.touch()
    state = tmp_path / "data" / "generations" / "gen_015"
    state.mkdir(parents=True)
    manifest = {
        "generations": [
            {
                "gen_id": "gen_015",
                "git_commit": commit,
                "worktree_path": str(worktree),
                "status": "active",
                "state_dir": str(state),
                "run_history": [
                    {
                        "action": "daily",
                        "date": session,
                        "outcome": "clean",
                        "success": True,
                    }
                    for session in SESSIONS
                ],
            }
        ]
    }
    (state.parent / "manifest.json").write_text(json.dumps(manifest))
    for cohort in COHORTS:
        cohort_dir = state / cohort
        cohort_dir.mkdir()
        with _db(cohort_dir / "portfolio.db") as connection:
            for statement in _DDL:
                connection.execute(statement)
            _insert(
                connection,
                "metric_epochs",
                epoch_id="epoch",
                generation_id="gen_015",
                schema_version=2,
                status="open",
                start_session=SESSIONS[0],
            )
            for session in SESSIONS:
                base = {"session": session, "cohort_id": cohort}
                _insert(
                    connection,
                    "session_runs",
                    **base,
                    session_run_id=session,
                    valid=1,
                    completed_at=session,
                )
                _insert(
                    connection,
                    "account_snapshots",
                    **base,
                    snapshot_id=session,
                    epoch_id="epoch",
                    valid=1,
                )
                _insert(
                    connection,
                    "session_execution_contexts",
                    **base,
                    execution_context_id=session,
                    epoch_id="epoch",
                )
                _insert(
                    connection,
                    "staging_runs",
                    **base,
                    staging_run_id=session,
                    epoch_id="epoch",
                    policy_id=f"foundation-{cohort.split('_')[1]}",
                    completed_at=session,
                )
                for symbol in ("SPY", "BIL"):
                    _insert(
                        connection,
                        "benchmark_observations",
                        **base,
                        observation_id=session + symbol,
                        epoch_id="epoch",
                        symbol=symbol,
                        valid=1,
                    )
                for phase in PHASES:
                    _insert(
                        connection,
                        "session_phases",
                        **base,
                        session_phase_id=session + phase,
                        phase=phase,
                        completed_at=session,
                    )
    with _db(state / "metrics_v2.sqlite3") as connection:
        connection.executescript(_SCHEMA)
        epoch = {
            "epoch_id": "epoch",
            "generation_id": "gen_015",
            "generation_commit": commit,
            "status": "open",
            "start_session": SESSIONS[0],
            "end_session": None,
        }
        _insert(
            connection,
            "metric_epochs",
            epoch_id="epoch",
            payload_json=json.dumps(epoch),
        )
        for session in SESSIONS:
            for horizon in ("30d", "3m", "6m", "1y"):
                for strategy in get_paper_trade_strategies():
                    payload = {
                        "epoch_id": "epoch",
                        "session": session,
                        "policy_id": f"foundation-{horizon}",
                        "strategy": strategy.name,
                        "status": "signals",
                    }
                    _insert(
                        connection,
                        "strategy_health",
                        health_id=session + horizon + strategy.name,
                        epoch_id="epoch",
                        session=session,
                        payload_json=json.dumps(payload),
                    )
    return tmp_path, state, manifest


def _assess(repo, **kwargs):
    commit = json.loads((repo / "data/generations/manifest.json").read_text())[
        "generations"
    ][0]["git_commit"]
    return assess_generation(repo, "gen_015", commit, date(2026, 9, 21), **kwargs)


def test_five_consecutive_complete_sessions_are_ready(tmp_path):
    repo, _, _ = _fixture(tmp_path)
    report = _assess(repo)
    assert report["ready"] is True
    assert [row["session"] for row in report["sessions"]] == list(SESSIONS)
    assert all(row["ready"] for row in report["sessions"])


def test_candidate_quarantine_cannot_be_called_clean(tmp_path):
    repo, state, manifest = _fixture(tmp_path)
    manifest["generations"][0]["run_history"][-1].update(
        {
            "outcome": "degraded",
            "success": False,
            "candidate_input_issues": [{"ticker": "QVCG"}],
        }
    )
    (state.parent / "manifest.json").write_text(json.dumps(manifest))
    with _db(state / "metrics_v2.sqlite3") as connection:
        _insert(
            connection,
            "candidate_input_issues",
            issue_id="issue",
            epoch_id="epoch",
            session=SESSIONS[-1],
        )
    row = _assess(repo)["sessions"][-1]
    assert row["manifest_clean"] is False
    assert row["candidate_coverage_complete"] is False
    assert row["accounting_complete"] is True
    assert row["staging_complete"] is True
    assert row["ready"] is False


def test_missing_daily_and_incomplete_cohort_block_readiness(tmp_path):
    repo, state, manifest = _fixture(tmp_path)
    manifest["generations"][0]["run_history"] = [
        run
        for run in manifest["generations"][0]["run_history"]
        if run["date"] != "2026-09-16"
    ]
    (state.parent / "manifest.json").write_text(json.dumps(manifest))
    with _db(state / COHORTS[0] / "portfolio.db") as connection:
        connection.execute(
            "DELETE FROM account_snapshots WHERE session = ?", ("2026-09-17",)
        )
        connection.execute(
            "DELETE FROM staging_runs WHERE session = ?", ("2026-09-18",)
        )
    rows = {row["session"]: row for row in _assess(repo)["sessions"]}
    assert rows["2026-09-16"]["manifest_clean"] is False
    assert rows["2026-09-17"]["accounting_complete"] is False
    assert rows["2026-09-18"]["staging_complete"] is False


def test_silent_or_failed_strategy_health_blocks_readiness(tmp_path):
    repo, state, _ = _fixture(tmp_path)
    with _db(state / "metrics_v2.sqlite3") as connection:
        connection.execute(
            "DELETE FROM strategy_health WHERE rowid = "
            "(SELECT rowid FROM strategy_health WHERE session = ? LIMIT 1)",
            (SESSIONS[-1],),
        )
    assert _assess(repo)["sessions"][-1]["strategy_health_complete"] is False


def test_missing_benchmark_or_provider_failure_blocks_readiness(tmp_path):
    repo, state, _ = _fixture(tmp_path)
    with _db(state / COHORTS[0] / "portfolio.db") as connection:
        connection.execute(
            "DELETE FROM benchmark_observations WHERE session = ? AND symbol = 'BIL'",
            (SESSIONS[-1],),
        )
    with _db(state / "metrics_v2.sqlite3") as connection:
        connection.execute(
            "UPDATE strategy_health SET payload_json = "
            "json_set(payload_json, '$.status', 'data_failure') "
            "WHERE rowid = (SELECT rowid FROM strategy_health WHERE session = ? LIMIT 1)",
            (SESSIONS[-1],),
        )
    row = _assess(repo)["sessions"][-1]
    assert row["accounting_complete"] is False
    assert row["strategy_health_complete"] is False


def test_quarantined_recovery_blocks_even_without_issue_row(tmp_path):
    repo, state, _ = _fixture(tmp_path)
    with _db(state / "metrics_v2.sqlite3") as connection:
        _insert(
            connection,
            "candidate_bar_recoveries",
            recovery_id="recovery",
            epoch_id="epoch",
            session=SESSIONS[-1],
            payload_json=json.dumps(
                {"outcome": "quarantined", "epoch_id": "epoch", "session": SESSIONS[-1]}
            ),
        )
    assert _assess(repo)["sessions"][-1]["candidate_coverage_complete"] is False


def test_wrong_commit_or_state_path_refuses_assessment(tmp_path):
    repo, state, manifest = _fixture(tmp_path)
    with pytest.raises(ValueError, match="commit"):
        assess_generation(repo, "gen_015", "b" * 40, date(2026, 9, 21))
    manifest["generations"][0]["state_dir"] = str(state.parent)
    (state.parent / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="canonical"):
        _assess(repo)


@pytest.mark.parametrize(
    "field,value",
    [
        ("strategy", "invented"),
        ("policy_id", "invented"),
        ("strategy", []),
        ("epoch_id", "other"),
    ],
)
def test_wrong_health_identity_fails_closed(tmp_path, field, value):
    repo, state, _ = _fixture(tmp_path)
    with _db(state / "metrics_v2.sqlite3") as connection:
        row_id, payload = connection.execute(
            "SELECT rowid, payload_json FROM strategy_health LIMIT 1"
        ).fetchone()
        record = json.loads(payload)
        record[field] = value
        connection.execute(
            "UPDATE strategy_health SET payload_json = ? WHERE rowid = ?",
            (json.dumps(record), row_id),
        )
    assert _assess(repo)["ready"] is False


def test_wrong_epoch_commit_fails_closed(tmp_path):
    repo, state, _ = _fixture(tmp_path)
    with _db(state / "metrics_v2.sqlite3") as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS metric_epochs (epoch_id TEXT, payload_json TEXT)"
        )
        connection.execute("DELETE FROM metric_epochs")
        connection.execute(
            "INSERT INTO metric_epochs VALUES (?, ?)",
            ("epoch", json.dumps({"generation_commit": "b" * 40})),
        )
    assert _assess(repo)["ready"] is False


@pytest.mark.parametrize(
    "table,field,value",
    [
        ("staging_runs", "policy_id", "wrong"),
        ("account_snapshots", "epoch_id", "wrong"),
        ("benchmark_observations", "cohort_id", "wrong"),
        ("session_execution_contexts", "epoch_id", "wrong"),
    ],
)
def test_wrong_ledger_identity_blocks_readiness(tmp_path, table, field, value):
    repo, state, _ = _fixture(tmp_path)
    with _db(state / COHORTS[0] / "portfolio.db") as connection:
        connection.execute(
            f"UPDATE {table} SET {field} = ? WHERE session = ?", (value, SESSIONS[-1])
        )
    assert _assess(repo)["ready"] is False


def test_dirty_worktree_refuses_assessment(tmp_path):
    repo, _, manifest = _fixture(tmp_path)
    (Path(manifest["generations"][0]["worktree_path"]) / "source.py").write_text(
        "changed\n"
    )
    with pytest.raises(ValueError, match="modified tracked"):
        _assess(repo)


def test_wrong_actual_worktree_commit_refuses_assessment(tmp_path):
    repo, _, manifest = _fixture(tmp_path)
    worktree = Path(manifest["generations"][0]["worktree_path"])
    _git(
        worktree,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "different",
    )
    with pytest.raises(ValueError, match="worktree commit"):
        _assess(repo)


def test_busy_lock_refuses_and_missing_lock_is_not_created(tmp_path):
    from tradingagents.strategies.orchestration.runtime_lock import (
        RuntimeLockBusy,
        runtime_lock,
    )

    repo, _, _ = _fixture(tmp_path)
    lock = repo / "data/operational/eventedge-runtime.lock"
    with runtime_lock(lock, exclusive=True), pytest.raises(RuntimeLockBusy):
        _assess(repo)
    lock.unlink()
    with pytest.raises(FileNotFoundError):
        _assess(repo)
    assert not lock.exists()


def test_concurrent_state_change_refuses_assessment(tmp_path, monkeypatch):
    from tradingagents.strategies.orchestration import release_readiness as readiness

    repo, state, _ = _fixture(tmp_path)
    original = readiness._accounting_and_staging

    def changed(*args):
        with _db(state / COHORTS[0] / "portfolio.db") as connection:
            connection.execute("UPDATE session_runs SET valid = 0")
        return original(*args)

    monkeypatch.setattr(readiness, "_accounting_and_staging", changed)
    with pytest.raises(ValueError, match="state changed"):
        _assess(repo)


def test_configured_policy_override_and_read_only_wal(tmp_path):
    repo, state, _ = _fixture(tmp_path)
    for cohort in COHORTS:
        with _db(state / cohort / "portfolio.db") as connection:
            connection.execute("UPDATE staging_runs SET policy_id = 'custom'")
    connection = sqlite3.connect(state / "metrics_v2.sqlite3")
    try:
        # Keep WAL open: assessment must see committed WAL while preserving source bytes.
        for row_id, payload in connection.execute(
            "SELECT rowid, payload_json FROM strategy_health"
        ).fetchall():
            row = json.loads(payload)
            horizon = row["policy_id"].removeprefix("foundation-")
            row["policy_id"] = f"custom:health:{horizon}"
            connection.execute(
                "UPDATE strategy_health SET payload_json = ? WHERE rowid = ?",
                (json.dumps(row), row_id),
            )
        connection.commit()
        before = {str(p): p.read_bytes() for p in state.rglob("*") if p.is_file()}
        assert _assess(repo, policy_id="custom")["ready"] is True
        assert {
            str(p): p.read_bytes() for p in state.rglob("*") if p.is_file()
        } == before
    finally:
        connection.close()


def test_script_imports_checkout_from_unrelated_cwd(tmp_path):
    import sys

    script = (
        Path(__file__).resolve().parents[1] / "scripts/check_generation_readiness.py"
    )
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--expected-commit" in result.stdout
