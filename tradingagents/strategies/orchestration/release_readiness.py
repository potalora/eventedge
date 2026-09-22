"""Read-only continuity gate for a parallel paper-trading generation.

This gate measures observed sessions. It does not predict future provider
availability or replace incident replay tests before starting a candidate.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from tradingagents.strategies.orchestration.session_executor import PHASES
from tradingagents.strategies.orchestration.trading_calendar import (
    is_session,
    previous_session,
)

COHORTS = tuple(
    f"horizon_{horizon}_size_{size}"
    for horizon in ("30d", "3m", "6m", "1y")
    for size in ("5k", "10k", "50k", "100k")
)
HEALTHY_STRATEGY_STATUSES = frozenset({"signals", "legitimate_no_event"})


def _read_only_db(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"missing database: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _one_valid_row(
    connection: sqlite3.Connection, table: str, session: str
) -> bool:
    rows = connection.execute(
        f"SELECT valid, completed_at FROM {table} WHERE session = ?", (session,)
    ).fetchall()
    return len(rows) == 1 and rows[0][0] == 1 and bool(rows[0][1])


def _accounting_and_staging(state_dir: Path, session: str) -> tuple[bool, bool]:
    accounting_complete = True
    staging_complete = True
    for cohort in COHORTS:
        try:
            with closing(_read_only_db(state_dir / cohort / "portfolio.db")) as connection:
                if not _one_valid_row(connection, "session_runs", session):
                    accounting_complete = False
                snapshots = connection.execute(
                    "SELECT valid FROM account_snapshots WHERE session = ?",
                    (session,),
                ).fetchall()
                invalidations = connection.execute(
                    "SELECT COUNT(*) FROM session_invalidations WHERE session = ?",
                    (session,),
                ).fetchone()[0]
                phases = connection.execute(
                    "SELECT phase, completed_at FROM session_phases WHERE session = ?",
                    (session,),
                ).fetchall()
                if (
                    len(snapshots) != 1
                    or snapshots[0][0] != 1
                    or invalidations
                    or len(phases) != len(PHASES)
                    or {phase for phase, _ in phases} != set(PHASES)
                    or any(not completed_at for _, completed_at in phases)
                ):
                    accounting_complete = False
                benchmarks = connection.execute(
                    "SELECT symbol, valid FROM benchmark_observations WHERE session = ?",
                    (session,),
                ).fetchall()
                if (
                    len(benchmarks) != 2
                    or {symbol for symbol, _ in benchmarks} != {"SPY", "BIL"}
                    or any(valid != 1 for _, valid in benchmarks)
                ):
                    accounting_complete = False
                staged = connection.execute(
                    "SELECT completed_at FROM staging_runs WHERE session = ?",
                    (session,),
                ).fetchall()
                if len(staged) != 1 or not staged[0][0]:
                    staging_complete = False
        except (OSError, sqlite3.Error, ValueError):
            accounting_complete = False
            staging_complete = False
    return accounting_complete, staging_complete


def _candidate_and_health(state_dir: Path, session: str) -> tuple[bool, bool]:
    try:
        with closing(_read_only_db(state_dir / "metrics_v2.sqlite3")) as connection:
            issues = connection.execute(
                "SELECT COUNT(*) FROM candidate_input_issues WHERE session = ?",
                (session,),
            ).fetchone()[0]
            recoveries = connection.execute(
                "SELECT payload_json FROM candidate_bar_recoveries WHERE session = ?",
                (session,),
            ).fetchall()
            recovery_rows = [json.loads(payload) for (payload,) in recoveries]
            health = [
                json.loads(payload)
                for (payload,) in connection.execute(
                    "SELECT payload_json FROM strategy_health WHERE session = ?",
                    (session,),
                )
            ]
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
        return False, False
    if not all(isinstance(row, dict) for row in recovery_rows + health):
        return False, False
    quarantined = any(row.get("outcome") == "quarantined" for row in recovery_rows)
    identities = {(row.get("policy_id"), row.get("strategy")) for row in health}
    health_complete = (
        len(health) == 48
        and len(identities) == 48
        and all(row.get("status") in HEALTHY_STRATEGY_STATUSES for row in health)
    )
    return not issues and not quarantined and bool(health), health_complete


def assess_generation(
    repo: Path,
    generation_id: str,
    expected_commit: str,
    through: date,
    *,
    sessions: int = 5,
) -> dict[str, Any]:
    """Require clean continuity before retiring a previous generation."""
    if sessions < 1 or not is_session(through):
        raise ValueError("sessions must be positive and through must be an XNYS session")
    manifest_path = repo / "data" / "generations" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    generations = [
        item
        for item in manifest["generations"]
        if item.get("gen_id") == generation_id
    ]
    if len(generations) != 1:
        raise ValueError("generation identity is missing or ambiguous")
    generation = generations[0]
    if generation.get("status") != "active":
        raise ValueError("candidate generation is not active")
    if generation.get("git_commit") != expected_commit:
        raise ValueError("candidate generation commit differs from expected commit")
    state_dir = (repo / "data" / "generations" / generation_id).resolve()
    if Path(generation.get("state_dir", "")).resolve() != state_dir:
        raise ValueError("candidate generation state directory is not canonical")
    history: dict[str, list[dict[str, Any]]] = {}
    for run in generation.get("run_history", []):
        if run.get("action") == "daily":
            history.setdefault(run.get("date", ""), []).append(run)
    dates = [through]
    for _ in range(sessions - 1):
        dates.append(previous_session(dates[-1]))
    rows = []
    for session_date in reversed(dates):
        session = session_date.isoformat()
        daily = history.get(session, [])
        run = daily[0] if len(daily) == 1 else None
        manifest_clean = bool(
            run
            and run.get("outcome") == "clean"
            and run.get("success") is True
            and not run.get("degraded")
            and not run.get("candidate_input_issues")
            and not run.get("candidate_bar_quarantines")
        )
        accounting, staging = _accounting_and_staging(state_dir, session)
        candidates, health = _candidate_and_health(state_dir, session)
        row = {
            "session": session,
            "manifest_clean": manifest_clean,
            "accounting_complete": accounting,
            "staging_complete": staging,
            "candidate_coverage_complete": candidates,
            "strategy_health_complete": health,
        }
        row["ready"] = all(value for key, value in row.items() if key != "session")
        rows.append(row)
    return {
        "generation_id": generation_id,
        "expected_commit": expected_commit,
        "required_consecutive_sessions": sessions,
        "through": through.isoformat(),
        "ready": all(row["ready"] for row in rows),
        "sessions": rows,
    }
