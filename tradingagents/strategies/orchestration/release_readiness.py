"""Read-only continuity gate for a parallel paper-trading generation.

This gate measures observed sessions. It does not predict future provider
availability or replace incident replay tests before starting a candidate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from tradingagents.strategies.modules import get_paper_trade_strategies
from tradingagents.strategies.orchestration.runtime_lock import (
    canonical_runtime_lock_path,
    runtime_lock,
)
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


def _one_valid_row(connection: sqlite3.Connection, table: str, session: str) -> bool:
    rows = connection.execute(
        f"SELECT valid, completed_at FROM {table} WHERE session = ?", (session,)
    ).fetchall()
    return len(rows) == 1 and rows[0][0] == 1 and bool(rows[0][1])


def _accounting_and_staging(
    state_dir: Path,
    session: str,
    epoch_id: str,
    generation_id: str,
    policy_id: str | None,
) -> tuple[bool, bool]:
    accounting_complete = True
    staging_complete = True
    for cohort in COHORTS:
        try:
            with closing(
                _read_only_db(state_dir / cohort / "portfolio.db")
            ) as connection:
                ledger_epoch = connection.execute(
                    "SELECT generation_id, status, start_session, end_session FROM metric_epochs WHERE epoch_id = ?",
                    (epoch_id,),
                ).fetchall()
                if (
                    len(ledger_epoch) != 1
                    or ledger_epoch[0][0] != generation_id
                    or ledger_epoch[0][1] not in {"open", "closed"}
                    or not (
                        ledger_epoch[0][2] <= session
                        and (
                            ledger_epoch[0][3] is None or session <= ledger_epoch[0][3]
                        )
                    )
                ):
                    accounting_complete = staging_complete = False
                # Refuse mixed identities, rather than silently filtering them away.
                for table in (
                    "session_runs",
                    "session_phases",
                    "session_invalidations",
                    "staging_runs",
                    "account_snapshots",
                    "benchmark_observations",
                    "session_execution_contexts",
                ):
                    if connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE session = ? AND cohort_id != ?",
                        (session, cohort),
                    ).fetchone()[0]:
                        accounting_complete = staging_complete = False
                for table in (
                    "staging_runs",
                    "account_snapshots",
                    "benchmark_observations",
                    "session_execution_contexts",
                ):
                    if connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE session = ? AND epoch_id != ?",
                        (session, epoch_id),
                    ).fetchone()[0]:
                        accounting_complete = staging_complete = False
                contexts = connection.execute(
                    "SELECT epoch_id FROM session_execution_contexts WHERE session = ?",
                    (session,),
                ).fetchall()
                if contexts != [(epoch_id,)]:
                    accounting_complete = False
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
                    "SELECT completed_at, policy_id FROM staging_runs WHERE session = ?",
                    (session,),
                ).fetchall()
                if (
                    len(staged) != 1
                    or not staged[0][0]
                    or staged[0][1]
                    != (policy_id or f"foundation-{cohort.split('_')[1]}")
                ):
                    staging_complete = False
        except (OSError, sqlite3.Error, ValueError, TypeError):
            accounting_complete = False
            staging_complete = False
    return accounting_complete, staging_complete


def _candidate_and_health(
    state_dir: Path, session: str, epoch_id: str, policy_id: str | None
) -> tuple[bool, bool]:
    try:
        with closing(_read_only_db(state_dir / "metrics_v2.sqlite3")) as connection:
            for table in (
                "strategy_health",
                "candidate_input_issues",
                "candidate_bar_recoveries",
            ):
                if connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE session = ? AND epoch_id != ?",
                    (session, epoch_id),
                ).fetchone()[0]:
                    return False, False
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
    for row in recovery_rows + health:
        if row.get("epoch_id") != epoch_id or row.get("session") != session:
            return False, False
    if any(
        not isinstance(row.get(key), str)
        for row in health
        for key in ("policy_id", "strategy", "status")
    ):
        return False, False
    if any(
        row.get("outcome") not in ("accepted", "recovered", "quarantined")
        for row in recovery_rows
    ):
        return False, False
    expected = {
        (
            f"{policy_id}:health:{horizon}" if policy_id else f"foundation-{horizon}",
            strategy.name,
        )
        for horizon in ("30d", "3m", "6m", "1y")
        for strategy in get_paper_trade_strategies()
    }
    quarantined = any(row.get("outcome") == "quarantined" for row in recovery_rows)
    identities = {(row.get("policy_id"), row.get("strategy")) for row in health}
    health_complete = (
        len(health) == 48
        and identities == expected
        and all(row.get("status") in HEALTHY_STRATEGY_STATUSES for row in health)
    )
    return not issues and not quarantined and bool(health), health_complete


def _assess_generation(
    repo: Path,
    generation_id: str,
    expected_commit: str,
    through: date,
    *,
    sessions: int = 5,
    policy_id: str | None = None,
    snapshot: Path,
) -> dict[str, Any]:
    """Require clean continuity before retiring a previous generation."""
    if sessions < 1 or not is_session(through):
        raise ValueError(
            "sessions must be positive and through must be an XNYS session"
        )
    manifest_path = repo / "data" / "generations" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    generations = [
        item for item in manifest["generations"] if item.get("gen_id") == generation_id
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
    _verify_worktree(repo, generation_id, generation, expected_commit)
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
        epoch_id = _session_epoch(snapshot, session, generation_id, expected_commit)
        if epoch_id is None:
            accounting = staging = candidates = health = False
        else:
            accounting, staging = _accounting_and_staging(
                snapshot, session, epoch_id, generation_id, policy_id
            )
            candidates, health = _candidate_and_health(
                snapshot, session, epoch_id, policy_id
            )
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


def _session_epoch(
    state_dir: Path, session: str, generation_id: str, commit: str
) -> str | None:
    try:
        with closing(_read_only_db(state_dir / "metrics_v2.sqlite3")) as connection:
            matches = []
            for identity, payload in connection.execute(
                "SELECT epoch_id, payload_json FROM metric_epochs"
            ):
                row = json.loads(payload)
                if row["start_session"] <= session and (
                    row.get("end_session") is None or session <= row["end_session"]
                ):
                    if (
                        row["epoch_id"] != identity
                        or row["generation_id"] != generation_id
                        or row["generation_commit"] != commit
                        or row["status"] not in {"open", "closed"}
                    ):
                        return None
                    matches.append(identity)
            return matches[0] if len(matches) == 1 else None
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return None


def _verify_worktree(
    repo: Path, generation_id: str, generation: dict, commit: str
) -> None:
    worktree = (repo / ".worktrees" / generation_id).resolve()
    if Path(generation.get("worktree_path", "")).resolve() != worktree:
        raise ValueError("candidate worktree path is not canonical")

    def git(*args):
        return subprocess.run(
            ["git", "--no-optional-locks", "-C", str(worktree), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    if git("rev-parse", "HEAD") != commit:
        raise ValueError("candidate worktree commit differs from expected commit")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("candidate worktree has modified tracked source")


def _fingerprint(paths: list[Path]) -> tuple:
    evidence = []
    for path in paths:
        if path.parent.resolve() != path.parent:
            raise ValueError("state parent must not contain symlinks")
        try:
            before = path.lstat()
        except FileNotFoundError:
            evidence.append((str(path), None))
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("state source must be a regular file")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        after = path.lstat()
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if identity(before) != identity(after):
            raise ValueError("state changed during readiness assessment")
        evidence.append((str(path), identity(after), digest))
    return tuple(evidence)


def assess_generation(
    repo: Path,
    generation_id: str,
    expected_commit: str,
    through: date,
    *,
    sessions: int = 5,
    policy_id: str | None = None,
) -> dict[str, Any]:
    """Read certified temporary copies while holding the existing runtime lock.

    The checker never creates lock files or opens authoritative SQLite databases.
    policy_id is the configured paper_ledger policy override; omit for defaults.
    """
    if not re.fullmatch(r"gen_[0-9]+", generation_id) or not re.fullmatch(
        r"[0-9a-f]{40}", expected_commit
    ):
        raise ValueError("invalid generation identity or full commit SHA")
    if policy_id is not None and (
        not isinstance(policy_id, str)
        or not policy_id.strip()
        or policy_id != policy_id.strip()
    ):
        raise ValueError("policy_id must be non-empty canonical text")
    repo = repo.resolve()
    state = repo / "data" / "generations" / generation_id
    if state.resolve() != state:
        raise ValueError("state path must not contain symlinks")
    manifest = state.parent / "manifest.json"
    databases = [
        state / "metrics_v2.sqlite3",
        *(state / cohort / "portfolio.db" for cohort in COHORTS),
    ]
    paths = [
        manifest,
        *(
            Path(str(db) + suffix)
            for db in databases
            for suffix in ("", "-wal", "-shm", "-journal")
        ),
    ]
    lock_path = canonical_runtime_lock_path(repo)
    fd = os.open(lock_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with runtime_lock(
            lock_path, exclusive=True, inherited_fd=fd, inherited_exclusive=True
        ):
            before = _fingerprint(paths)
            with tempfile.TemporaryDirectory(
                prefix="eventedge-readiness-"
            ) as temporary:
                snapshot = Path(temporary)
                for database in databases:
                    destination = snapshot / database.relative_to(state)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    for suffix in ("", "-wal", "-journal"):
                        source = Path(str(database) + suffix)
                        if source.exists():
                            shutil.copyfile(source, Path(str(destination) + suffix))
                if before != _fingerprint(paths):
                    raise ValueError("state changed during readiness assessment")
                result = _assess_generation(
                    repo,
                    generation_id,
                    expected_commit,
                    through,
                    sessions=sessions,
                    policy_id=policy_id,
                    snapshot=snapshot,
                )
            if before != _fingerprint(paths):
                raise ValueError("state changed during readiness assessment")
            generation = next(
                row
                for row in json.loads(manifest.read_text())["generations"]
                if row["gen_id"] == generation_id
            )
            _verify_worktree(repo, generation_id, generation, expected_commit)
            return result
    finally:
        os.close(fd)
