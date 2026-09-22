"""Availability gate fixtures modeled on the September 2026 incident window."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

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


def _fixture(tmp_path):
    state = tmp_path / "data" / "generations" / "gen_015"
    state.mkdir(parents=True)
    manifest = {
        "generations": [
            {
                "gen_id": "gen_015",
                "git_commit": COMMIT,
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
        with sqlite3.connect(cohort_dir / "portfolio.db") as connection:
            connection.executescript(
                "CREATE TABLE session_runs (session TEXT, valid INTEGER, completed_at TEXT);"
                "CREATE TABLE account_snapshots (session TEXT, valid INTEGER);"
                "CREATE TABLE session_invalidations (session TEXT);"
                "CREATE TABLE session_phases (session TEXT, phase TEXT, completed_at TEXT);"
                "CREATE TABLE staging_runs (session TEXT, completed_at TEXT);"
                "CREATE TABLE benchmark_observations (session TEXT, symbol TEXT, valid INTEGER);"
            )
            for session in SESSIONS:
                connection.execute(
                    "INSERT INTO session_runs VALUES (?, 1, ?)", (session, session)
                )
                connection.execute(
                    "INSERT INTO account_snapshots VALUES (?, 1)", (session,)
                )
                connection.execute(
                    "INSERT INTO staging_runs VALUES (?, ?)", (session, session)
                )
                connection.executemany(
                    "INSERT INTO benchmark_observations VALUES (?, ?, 1)",
                    [(session, "SPY"), (session, "BIL")],
                )
                connection.executemany(
                    "INSERT INTO session_phases VALUES (?, ?, ?)",
                    [(session, phase, session) for phase in PHASES],
                )
    with sqlite3.connect(state / "metrics_v2.sqlite3") as connection:
        connection.executescript(
            "CREATE TABLE candidate_input_issues (session TEXT);"
            "CREATE TABLE candidate_bar_recoveries (session TEXT, payload_json TEXT);"
            "CREATE TABLE strategy_health (session TEXT, payload_json TEXT);"
        )
        for session in SESSIONS:
            connection.executemany(
                "INSERT INTO strategy_health VALUES (?, ?)",
                [
                    (
                        session,
                        json.dumps(
                            {
                                "policy_id": f"policy_{horizon}",
                                "strategy": f"strategy_{strategy}",
                                "status": "signals",
                            }
                        ),
                    )
                    for horizon in range(4)
                    for strategy in range(12)
                ],
            )
    return tmp_path, state, manifest


def _assess(repo):
    return assess_generation(repo, "gen_015", COMMIT, date(2026, 9, 21))


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
    with sqlite3.connect(state / "metrics_v2.sqlite3") as connection:
        connection.execute(
            "INSERT INTO candidate_input_issues VALUES (?)", (SESSIONS[-1],)
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
    with sqlite3.connect(state / COHORTS[0] / "portfolio.db") as connection:
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
    with sqlite3.connect(state / "metrics_v2.sqlite3") as connection:
        connection.execute(
            "DELETE FROM strategy_health WHERE rowid = "
            "(SELECT rowid FROM strategy_health WHERE session = ? LIMIT 1)",
            (SESSIONS[-1],),
        )
    assert _assess(repo)["sessions"][-1]["strategy_health_complete"] is False


def test_missing_benchmark_or_provider_failure_blocks_readiness(tmp_path):
    repo, state, _ = _fixture(tmp_path)
    with sqlite3.connect(state / COHORTS[0] / "portfolio.db") as connection:
        connection.execute(
            "DELETE FROM benchmark_observations WHERE session = ? AND symbol = 'BIL'",
            (SESSIONS[-1],),
        )
    with sqlite3.connect(state / "metrics_v2.sqlite3") as connection:
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
    with sqlite3.connect(state / "metrics_v2.sqlite3") as connection:
        connection.execute(
            "INSERT INTO candidate_bar_recoveries VALUES (?, ?)",
            (SESSIONS[-1], json.dumps({"outcome": "quarantined"})),
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
