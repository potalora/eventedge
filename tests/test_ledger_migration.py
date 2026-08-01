"""Release-readiness and exact-session tests for the P0 ledger cutover."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    SIZE_PROFILES,
    build_default_cohorts,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "scripts" / "migrate_ledger_state.py"
RUN_COHORTS = REPO_ROOT / "scripts" / "run_cohorts.py"
RUN_GENERATIONS = REPO_ROOT / "scripts" / "run_generations.py"


def _cohort_names() -> list[str]:
    return [cohort.name for cohort in build_default_cohorts(DEFAULT_CONFIG)]


def _legacy_tree(root: Path) -> Path:
    legacy = root / "legacy"
    for index, name in enumerate(_cohort_names()):
        cohort = legacy / name
        cohort.mkdir(parents=True)
        # Deliberately malformed evidence: migration may inventory and hash it,
        # but must never deserialize it into the authoritative ledger.
        if index == 0:
            (cohort / "paper_trades.json").write_text("{not-json\n")
        elif index != 1:
            (cohort / "paper_trades.json").write_text("[]\n")
        (cohort / "signal_journal.jsonl").write_text("not-jsonl\n")
    (legacy / "unexpected.bin").write_bytes(b"legacy-evidence\x00")
    return legacy


def _hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _run_migration(legacy: Path, output: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(MIGRATION),
            "--legacy-state",
            str(legacy),
            "--output-dir",
            str(output),
            mode,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def test_run_cohorts_rejects_non_xnys_date_exactly(tmp_path):
    env = os.environ.copy()
    state = tmp_path / "state"
    env["AUTORESEARCH_STATE_DIR"] = str(state)
    env["EVENTEDGE_GENERATION_ID"] = "gen_test"
    env["EVENTEDGE_GENERATION_COMMIT"] = "test-commit"
    result = subprocess.run(
        [sys.executable, str(RUN_COHORTS), "--date", "2026-07-03", "--no-llm"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "2026-07-03 is not an XNYS session" in result.stdout + result.stderr
    assert not state.exists()


def test_run_generations_rejects_non_xnys_date_instead_of_rolling():
    result = subprocess.run(
        [sys.executable, str(RUN_GENERATIONS), "run-daily", "--date", "2026-07-03"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "2026-07-03 is not an XNYS session" in result.stdout + result.stderr
    assert "2026-07-02" not in result.stdout + result.stderr


def test_dry_run_hashes_and_inventories_without_importing_legacy(tmp_path):
    legacy = _legacy_tree(tmp_path)
    before = _hashes(legacy)
    output = tmp_path / "dry-run"

    result = _run_migration(legacy, output, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert _hashes(legacy) == before
    assert sorted(path.relative_to(output).as_posix() for path in output.rglob("*")) == [
        "migration-report.txt"
    ]
    report = (output / "migration-report.txt").read_text()
    assert "legacy_execution_model=same_session_close" in report
    assert "authoritative_import=false" in report
    assert "eligible_for_promotion=false" in report
    assert "cohort_count=16" in report
    assert "status=malformed" in report
    assert "status=missing" in report
    assert "status=unexpected" in report
    hash_lines = [line for line in report.splitlines() if line.startswith("sha256 ")]
    assert [line.split(" path=", 1)[1].split(" before=", 1)[0] for line in hash_lines] == sorted(before)
    assert all(" unchanged=true" in line for line in hash_lines)
    assert not list(output.rglob("portfolio.db"))


def test_source_hash_change_between_inventory_passes_fails_closed(tmp_path, monkeypatch):
    from scripts import migrate_ledger_state

    legacy = _legacy_tree(tmp_path)
    output = tmp_path / "changed"
    actual_hash = migrate_ledger_state._hash_regular_inputs
    calls = 0

    def changed_second_pass(root):
        nonlocal calls
        calls += 1
        hashes = actual_hash(root)
        if calls == 2:
            first = sorted(hashes)[0]
            hashes[first] = "0" * 64
        return hashes

    monkeypatch.setattr(migrate_ledger_state, "_hash_regular_inputs", changed_second_pass)

    with pytest.raises(RuntimeError, match="legacy input changed during inspection"):
        migrate_ledger_state.run_migration(legacy, output, initialize_clean=False)
    assert not list(output.rglob("portfolio.db"))


def test_initialize_clean_creates_only_opening_cash_and_empty_economics(tmp_path):
    legacy = _legacy_tree(tmp_path)
    output = tmp_path / "clean"

    result = _run_migration(legacy, output, "--initialize-clean")

    assert result.returncode == 0, result.stderr
    expected = {cohort.name: cohort.size_profile for cohort in build_default_cohorts(DEFAULT_CONFIG)}
    assert sorted(path.parent.name for path in output.glob("*/portfolio.db")) == sorted(expected)
    for cohort_name, size_name in expected.items():
        db = output / cohort_name / "portfolio.db"
        connection = sqlite3.connect(db)
        try:
            cohort_id = connection.execute(
                "SELECT metadata_value FROM schema_metadata WHERE metadata_key='cohort_id'"
            ).fetchone()[0]
            opening = connection.execute(
                "SELECT amount FROM cash_events WHERE event_type='opening'"
            ).fetchall()
            assert cohort_id == cohort_name
            assert opening == [(str(SIZE_PROFILES[size_name].total_capital),)]
            for table in (
                "signals",
                "order_intents",
                "fills",
                "lots",
                "marks",
                "account_snapshots",
                "benchmark_observations",
            ):
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        finally:
            connection.close()


@pytest.mark.parametrize("tamper", ["cohort", "cash"])
def test_initialize_clean_is_idempotent_but_wrong_existing_ledger_fails_closed(
    tmp_path, tamper
):
    legacy = _legacy_tree(tmp_path)
    output = tmp_path / "clean"
    first = _run_migration(legacy, output, "--initialize-clean")
    assert first.returncode == 0, first.stderr
    second = _run_migration(legacy, output, "--initialize-clean")
    assert second.returncode == 0, second.stderr

    target = output / _cohort_names()[0] / "portfolio.db"
    connection = sqlite3.connect(target)
    try:
        if tamper == "cohort":
            connection.execute(
                "UPDATE schema_metadata SET metadata_value='wrong-cohort' "
                "WHERE metadata_key='cohort_id'"
            )
        else:
            connection.execute(
                "UPDATE cash_events SET amount='9999' WHERE event_type='opening'"
            )
        connection.commit()
    finally:
        connection.close()

    failed = _run_migration(legacy, output, "--initialize-clean")
    assert failed.returncode != 0
    expected = "wrong-cohort" if tamper == "cohort" else "opening cash mismatch"
    assert expected in failed.stdout + failed.stderr


@pytest.mark.parametrize(
    "tamper",
    ["additional_cash_event", "non_bootstrap_accounting", "unlisted_history_table"],
)
def test_initialize_clean_rejects_any_non_bootstrap_ledger_state(tmp_path, tamper):
    legacy = _legacy_tree(tmp_path)
    output = tmp_path / "clean"
    initialized = _run_migration(legacy, output, "--initialize-clean")
    assert initialized.returncode == 0, initialized.stderr

    cohort_name = _cohort_names()[0]
    target = output / cohort_name / "portfolio.db"
    connection = sqlite3.connect(target)
    try:
        if tamper == "additional_cash_event":
            connection.execute(
                "INSERT INTO cash_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "unexpected-cash",
                    cohort_name,
                    "2026-07-31",
                    "adjustment",
                    "1",
                    "2026-07-31T20:00:00+00:00",
                    "must fail readiness",
                ),
            )
        elif tamper == "non_bootstrap_accounting":
            connection.execute(
                "UPDATE accounting_state SET realized_pnl='1', commission_cost='0.25'"
            )
        else:
            # metric_epochs is intentionally outside the old short
            # _ECONOMIC_TABLES list, proving future/history tables fail closed.
            connection.execute(
                "INSERT INTO metric_epochs VALUES (?, ?, ?, ?, ?, ?)",
                ("unexpected-epoch", "gen-test", 1, "active", "2026-07-31", None),
            )
        connection.commit()
    finally:
        connection.close()

    failed = _run_migration(legacy, output, "--initialize-clean")

    assert failed.returncode != 0
    assert "existing ledger" in failed.stdout + failed.stderr


def test_clean_readiness_rejects_broken_ledger_symlink_marker(tmp_path):
    from scripts.migrate_ledger_state import _assert_clean_existing_ledger

    ledger_path = tmp_path / "portfolio.db"
    ledger_path.symlink_to(tmp_path / "missing-ledger.db")

    with pytest.raises(RuntimeError, match="not a regular file"):
        _assert_clean_existing_ledger(
            ledger_path, "horizon_30d_size_5k", Decimal("5000")
        )


def test_initialize_clean_rejects_symlinked_cohort_parent(tmp_path):
    legacy = _legacy_tree(tmp_path)
    output = tmp_path / "clean"
    outside = tmp_path / "outside"
    outside.mkdir()
    output.mkdir()
    cohort_name = _cohort_names()[0]
    (output / cohort_name).symlink_to(outside, target_is_directory=True)

    failed = _run_migration(legacy, output, "--initialize-clean")

    assert failed.returncode != 0
    assert "cohort directory" in failed.stdout + failed.stderr
    assert not (outside / "portfolio.db").exists()


def test_checkpointed_clean_ledgers_without_sidecars_remain_idempotent(tmp_path):
    legacy = _legacy_tree(tmp_path)
    output = tmp_path / "clean"
    initialized = _run_migration(legacy, output, "--initialize-clean")
    assert initialized.returncode == 0, initialized.stderr

    for database in sorted(output.glob("*/portfolio.db")):
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        # A normal last-close may remove these transient files. Remove any
        # platform leftovers after the completed checkpoint to reproduce that
        # legitimate WAL lifecycle deterministically.
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database}{suffix}")
            if os.path.lexists(sidecar):
                sidecar.unlink()
            assert not os.path.lexists(sidecar)

    repeated = _run_migration(legacy, output, "--initialize-clean")

    assert repeated.returncode == 0, repeated.stderr
    assert len(list(output.glob("*/portfolio.db"))) == 16


def test_initialize_clean_rejects_symlinked_sqlite_sidecar(tmp_path):
    legacy = _legacy_tree(tmp_path)
    output = tmp_path / "clean"
    initialized = _run_migration(legacy, output, "--initialize-clean")
    assert initialized.returncode == 0, initialized.stderr
    database = next(iter(sorted(output.glob("*/portfolio.db"))))
    sidecar = Path(f"{database}-wal")
    if os.path.lexists(sidecar):
        sidecar.unlink()
    outside = tmp_path / "outside-wal"
    outside.write_bytes(b"")
    sidecar.symlink_to(outside)

    failed = _run_migration(legacy, output, "--initialize-clean")

    assert failed.returncode != 0
    assert "sidecar" in failed.stdout + failed.stderr


@pytest.mark.parametrize("orphan_suffix", ["-shm", "-wal"])
def test_initialize_clean_rejects_orphan_regular_sqlite_sidecar(
    tmp_path, orphan_suffix
):
    legacy = _legacy_tree(tmp_path)
    output = tmp_path / "clean"
    initialized = _run_migration(legacy, output, "--initialize-clean")
    assert initialized.returncode == 0, initialized.stderr
    database = next(iter(sorted(output.glob("*/portfolio.db"))))
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{database}{suffix}")
        if os.path.lexists(sidecar):
            sidecar.unlink()
    orphan = Path(f"{database}{orphan_suffix}")
    orphan.write_bytes(b"\0" * 32768 if orphan_suffix == "-shm" else b"")

    failed = _run_migration(legacy, output, "--initialize-clean")

    assert failed.returncode != 0
    assert "sidecar pair" in failed.stdout + failed.stderr


def test_initialize_clean_accepts_legitimate_paired_zero_wal_sidecars(tmp_path):
    legacy = _legacy_tree(tmp_path)
    output = tmp_path / "clean"
    initialized = _run_migration(legacy, output, "--initialize-clean")
    assert initialized.returncode == 0, initialized.stderr
    database = next(iter(sorted(output.glob("*/portfolio.db"))))
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert Path(f"{database}-wal").stat().st_size == 0
        assert Path(f"{database}-shm").is_file()

        repeated = _run_migration(legacy, output, "--initialize-clean")
    finally:
        connection.close()

    assert repeated.returncode == 0, repeated.stderr


def test_initialize_clean_consults_paired_live_wal_and_rejects_hidden_history(
    tmp_path,
):
    legacy = _legacy_tree(tmp_path)
    output = tmp_path / "clean"
    initialized = _run_migration(legacy, output, "--initialize-clean")
    assert initialized.returncode == 0, initialized.stderr
    database = next(iter(sorted(output.glob("*/portfolio.db"))))
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "INSERT INTO metric_epochs VALUES (?, ?, ?, ?, ?, ?)",
            ("hidden-epoch", "gen-test", 1, "active", "2026-07-31", None),
        )
        connection.commit()
        assert Path(f"{database}-wal").stat().st_size > 0
        assert Path(f"{database}-shm").is_file()

        failed = _run_migration(legacy, output, "--initialize-clean")
    finally:
        connection.close()

    assert failed.returncode != 0
    assert "metric_epochs=1" in failed.stdout + failed.stderr


@pytest.mark.parametrize(
    "tamper",
    [
        "seventeenth_cohort",
        "extra_cohort_file",
        "extra_empty_user_table",
        "missing_expected_table",
    ],
)
def test_initialize_clean_requires_exact_schema_and_output_topology(tmp_path, tamper):
    legacy = _legacy_tree(tmp_path)
    output = tmp_path / "clean"
    initialized = _run_migration(legacy, output, "--initialize-clean")
    assert initialized.returncode == 0, initialized.stderr
    cohort_dir = output / _cohort_names()[0]
    database = cohort_dir / "portfolio.db"

    if tamper == "seventeenth_cohort":
        extra = output / "unexpected_cohort"
        extra.mkdir()
        (extra / "portfolio.db").write_bytes(b"")
    elif tamper == "extra_cohort_file":
        (cohort_dir / "unexpected.txt").write_text("not part of clean topology\n")
    else:
        connection = sqlite3.connect(database)
        try:
            if tamper == "extra_empty_user_table":
                connection.execute("CREATE TABLE unexpected_history (id TEXT PRIMARY KEY)")
            else:
                connection.execute("DROP TABLE metric_epochs")
            connection.commit()
        finally:
            connection.close()

    failed = _run_migration(legacy, output, "--initialize-clean")

    assert failed.returncode != 0
    assert "exact clean" in failed.stdout + failed.stderr


@pytest.mark.parametrize("kind", ["equal", "nested", "symlink-parent"])
def test_output_must_be_outside_resolved_legacy_tree(tmp_path, kind):
    legacy = _legacy_tree(tmp_path)
    if kind == "equal":
        output = legacy
    elif kind == "nested":
        output = legacy / "generated"
    else:
        alias = tmp_path / "legacy-alias"
        alias.symlink_to(legacy, target_is_directory=True)
        output = alias / "generated"

    result = _run_migration(legacy, output, "--dry-run")

    assert result.returncode != 0
    assert "outside legacy state" in result.stdout + result.stderr
