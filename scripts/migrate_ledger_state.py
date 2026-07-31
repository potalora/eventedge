#!/usr/bin/env python3
"""Inventory legacy JSON evidence and initialize clean P0 cohort ledgers.

Legacy economic history is intentionally never imported.  The only supported
authoritative cutover is a clean set of cohort ledgers with configured opening
cash.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import stat
import sys
from decimal import Decimal
from pathlib import Path


_REPORT_NAME = "migration-report.txt"
_EXPECTED_EVIDENCE = ("paper_trades.json", "signal_journal.jsonl")


def _expected_user_tables() -> frozenset[str]:
    """Derive the canonical user-table set directly from current ledger DDL."""
    from tradingagents.strategies.state.portfolio_ledger import _DDL

    connection = sqlite3.connect(":memory:")
    try:
        for statement in _DDL:
            connection.execute(statement)
        return frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
    finally:
        connection.close()


def _validate_sidecars(path: Path) -> bool:
    """Reject unsafe sidecars and report whether a live WAL must be consulted."""
    sidecars = {suffix: Path(f"{path}{suffix}") for suffix in ("-wal", "-shm")}
    for sidecar in sidecars.values():
        if os.path.lexists(sidecar) and (
            sidecar.is_symlink() or not sidecar.is_file()
        ):
            raise RuntimeError(
                f"existing SQLite sidecar is not a regular file: {sidecar}"
            )
    wal = sidecars["-wal"]
    live_wal = os.path.lexists(wal) and wal.stat().st_size > 0
    if live_wal and not os.path.lexists(sidecars["-shm"]):
        raise RuntimeError(f"nonempty SQLite WAL has no SHM sidecar: {wal}")
    return live_wal


def _validate_output_topology(
    output: Path,
    cohort_names: list[str],
    *,
    require_complete: bool,
    initialize_clean: bool,
) -> None:
    """Require an exact report plus direct 16-ledger output topology."""
    expected_cohorts = set(cohort_names)
    allowed_root = {_REPORT_NAME, *expected_cohorts} if initialize_clean else {_REPORT_NAME}
    actual_root = {entry.name for entry in output.iterdir()}
    unexpected_root = actual_root - allowed_root
    if unexpected_root:
        raise RuntimeError(
            "output is not an exact clean ledger topology: unexpected root entries "
            + ", ".join(sorted(unexpected_root))
        )

    report = output / _REPORT_NAME
    if os.path.lexists(report) and (report.is_symlink() or not report.is_file()):
        raise RuntimeError(
            f"output is not an exact clean ledger topology: invalid report {report}"
        )
    if require_complete and not os.path.lexists(report):
        raise RuntimeError(
            "output is not an exact clean ledger topology: migration report missing"
        )

    if not initialize_clean:
        return
    for cohort_name in cohort_names:
        cohort_dir = output / cohort_name
        if not os.path.lexists(cohort_dir):
            if require_complete:
                raise RuntimeError(
                    "output is not an exact clean ledger topology: "
                    f"missing cohort {cohort_name}"
                )
            continue
        if cohort_dir.is_symlink() or not cohort_dir.is_dir():
            raise RuntimeError(
                "output is not an exact clean ledger topology: "
                f"invalid cohort directory {cohort_dir}"
            )
        allowed_files = {"portfolio.db", "portfolio.db-wal", "portfolio.db-shm"}
        actual_files = {entry.name for entry in cohort_dir.iterdir()}
        unexpected_files = actual_files - allowed_files
        if unexpected_files:
            raise RuntimeError(
                "output is not an exact clean ledger topology: "
                f"unexpected files in {cohort_name}: "
                + ", ".join(sorted(unexpected_files))
            )
        database = cohort_dir / "portfolio.db"
        if require_complete and not os.path.lexists(database):
            raise RuntimeError(
                "output is not an exact clean ledger topology: "
                f"missing portfolio.db for {cohort_name}"
            )
        if os.path.lexists(database) and (
            database.is_symlink() or not database.is_file()
        ):
            raise RuntimeError(
                "output is not an exact clean ledger topology: "
                f"invalid portfolio.db for {cohort_name}"
            )
        if not os.path.lexists(database) and any(
            os.path.lexists(cohort_dir / name)
            for name in ("portfolio.db-wal", "portfolio.db-shm")
        ):
            raise RuntimeError(
                "output is not an exact clean ledger topology: "
                f"orphaned SQLite sidecar for {cohort_name}"
            )
        if os.path.lexists(database):
            _validate_sidecars(database)


def _regular_inputs(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        dirnames[:] = sorted(
            name for name in dirnames if not (base / name).is_symlink()
        )
        for name in sorted(filenames):
            path = base / name
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISREG(mode):
                files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_regular_inputs(root: Path) -> dict[str, str]:
    """Stream-hash every regular legacy input in relative-path order."""
    return {
        path.relative_to(root).as_posix(): _stream_sha256(path)
        for path in _regular_inputs(root)
    }


def _looks_structurally_well_formed(path: Path) -> bool:
    """Bounded byte-shape check only; legacy evidence is never deserialized."""
    try:
        with path.open("rb") as source:
            if path.suffix == ".jsonl":
                saw_content = False
                for raw_line in source:
                    stripped = raw_line.lstrip()
                    if not stripped:
                        continue
                    saw_content = True
                    if not stripped.startswith(b"{"):
                        return False
                return saw_content
            while True:
                chunk = source.read(4096)
                if not chunk:
                    return False
                stripped = chunk.lstrip()
                if stripped:
                    return stripped[:1] in {b"[", b"{"}
    except OSError:
        return False


def _inventory_lines(legacy: Path, cohort_names: list[str]) -> list[str]:
    lines: list[str] = []
    expected_cohorts = set(cohort_names)
    for cohort_name in cohort_names:
        cohort_dir = legacy / cohort_name
        if not cohort_dir.is_dir() or cohort_dir.is_symlink():
            lines.append(f"evidence cohort={cohort_name} path=. status=missing")
            continue
        for evidence_name in _EXPECTED_EVIDENCE:
            evidence = cohort_dir / evidence_name
            relative = evidence.relative_to(legacy).as_posix()
            if not evidence.exists() or evidence.is_symlink() or not evidence.is_file():
                lines.append(
                    f"evidence cohort={cohort_name} path={relative} status=missing"
                )
            elif _looks_structurally_well_formed(evidence):
                lines.append(
                    f"evidence cohort={cohort_name} path={relative} status=present"
                )
            else:
                lines.append(
                    f"evidence cohort={cohort_name} path={relative} status=malformed"
                )

    for entry in sorted(legacy.iterdir(), key=lambda item: item.name):
        if entry.name not in expected_cohorts:
            lines.append(
                f"evidence cohort=- path={entry.relative_to(legacy).as_posix()} "
                "status=unexpected"
            )
    for cohort_name in cohort_names:
        cohort_dir = legacy / cohort_name
        if not cohort_dir.is_dir() or cohort_dir.is_symlink():
            continue
        for entry in sorted(cohort_dir.iterdir(), key=lambda item: item.name):
            if entry.name not in _EXPECTED_EVIDENCE:
                lines.append(
                    f"evidence cohort={cohort_name} "
                    f"path={entry.relative_to(legacy).as_posix()} status=unexpected"
                )
    return lines


def _assert_clean_existing_ledger(
    path: Path,
    cohort_id: str,
    opening_cash: Decimal,
    expected_user_tables: frozenset[str] | None = None,
) -> None:
    from tradingagents.strategies.execution import stable_id
    from tradingagents.strategies.state.portfolio_ledger import SCHEMA_VERSION

    if not os.path.lexists(path):
        return
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"existing ledger path is not a regular file: {path}")
    live_wal = _validate_sidecars(path)
    try:
        uri = path.resolve().as_uri()
        if live_wal:
            uri += "?mode=ro"
        else:
            uri += "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            actual_user_tables = frozenset(
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            )
            canonical_tables = expected_user_tables or _expected_user_tables()
            if actual_user_tables != canonical_tables:
                missing = sorted(canonical_tables - actual_user_tables)
                extra = sorted(actual_user_tables - canonical_tables)
                raise RuntimeError(
                    f"existing ledger {cohort_id} is not an exact clean schema: "
                    f"missing={missing}, extra={extra}"
                )
            metadata = {
                row["metadata_key"]: row["metadata_value"]
                for row in connection.execute(
                    "SELECT metadata_key, metadata_value FROM schema_metadata"
                )
            }
            expected_metadata = {
                "cohort_id": cohort_id,
                "schema_version": str(SCHEMA_VERSION),
            }
            if metadata.get("cohort_id") != cohort_id:
                found = metadata.get("cohort_id", "missing")
                raise RuntimeError(
                    f"existing ledger cohort mismatch: expected {cohort_id}, found {found}"
                )
            if metadata != expected_metadata:
                raise RuntimeError(
                    f"existing ledger {cohort_id} is not clean: schema metadata mismatch"
                )
            cash_rows = connection.execute(
                "SELECT * FROM cash_events ORDER BY cash_event_id"
            ).fetchall()
            expected_cash_id = stable_id("cash", cohort_id, "opening")
            if (
                len(cash_rows) != 1
                or cash_rows[0]["cash_event_id"] != expected_cash_id
                or cash_rows[0]["cohort_id"] != cohort_id
                or cash_rows[0]["session"] is not None
                or cash_rows[0]["event_type"] != "opening"
                or Decimal(str(cash_rows[0]["amount"])) != opening_cash
                or cash_rows[0]["effective_at"] != "1970-01-01T00:00:00"
                or cash_rows[0]["detail"] != "deterministic opening cash"
            ):
                raise RuntimeError(
                    f"existing ledger opening cash mismatch for {cohort_id}: "
                    f"expected {opening_cash}"
                )

            accounting_rows = connection.execute(
                "SELECT * FROM accounting_state ORDER BY cohort_id"
            ).fetchall()
            if len(accounting_rows) != 1:
                raise RuntimeError(
                    f"existing ledger {cohort_id} is not clean: accounting_state "
                    f"rows={len(accounting_rows)}"
                )
            accounting = accounting_rows[0]
            zero_fields = (
                "realized_pnl",
                "slippage_cost",
                "commission_cost",
                "other_fees",
                "borrow_cost",
                "financing_cost",
                "dividend_cash",
            )
            if (
                accounting["cohort_id"] != cohort_id
                or Decimal(str(accounting["cash"])) != opening_cash
                or Decimal(str(accounting["high_water_mark"])) != opening_cash
                or any(Decimal(str(accounting[field])) != 0 for field in zero_fields)
            ):
                raise RuntimeError(
                    f"existing ledger {cohort_id} is not clean: "
                    "accounting_state is not all-cash bootstrap"
                )

            bootstrap_tables = {"schema_metadata", "cash_events", "accounting_state"}
            user_tables = [
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            for table in user_tables:
                if table in bootstrap_tables:
                    continue
                quoted = '"' + table.replace('"', '""') + '"'
                count = connection.execute(
                    f"SELECT COUNT(*) FROM {quoted}"
                ).fetchone()[0]
                if count:
                    raise RuntimeError(
                        f"existing ledger {cohort_id} is not clean: {table}={count}"
                    )
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise RuntimeError(f"invalid existing ledger {path}: {error}") from error


def _initialize_clean_ledgers(output: Path, cohorts: list[object]) -> None:
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

    autoresearch = DEFAULT_CONFIG["autoresearch"]
    expected_tables = _expected_user_tables()
    planned: list[tuple[object, Decimal, Path]] = []
    for cohort in cohorts:
        profile = SIZE_PROFILES[cohort.size_profile]
        cash = Decimal(str(profile.total_capital))
        cohort_dir = output / cohort.name
        if os.path.lexists(cohort_dir) and (
            cohort_dir.is_symlink() or not cohort_dir.is_dir()
        ):
            raise RuntimeError(
                f"existing cohort directory is not a regular directory: {cohort_dir}"
            )
        path = cohort_dir / "portfolio.db"
        _assert_clean_existing_ledger(
            path, cohort.name, cash, expected_user_tables=expected_tables
        )
        planned.append((cohort, cash, path))

    for cohort, cash, path in planned:
        ledger = PortfolioLedger(
            path,
            cohort.name,
            cash,
            paper_ledger_config=autoresearch.get("paper_ledger"),
            short_selling_config=autoresearch.get("short_selling"),
        )
        ledger.close()


def _write_report(path: Path, lines: list[str]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as report:
        report.write("\n".join(lines) + "\n")
        report.flush()
        os.fsync(report.fileno())
    os.replace(temporary, path)


def run_migration(
    legacy_state: Path, output_dir: Path, *, initialize_clean: bool
) -> Path:
    """Run one non-authoritative inventory or clean-ledger initialization."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.strategies.orchestration.cohort_orchestrator import (
        build_default_cohorts,
    )

    legacy = Path(legacy_state).resolve(strict=True)
    if not legacy.is_dir():
        raise RuntimeError(f"legacy state is not a directory: {legacy}")
    output = Path(output_dir).resolve(strict=False)
    if output == legacy or legacy in output.parents:
        raise RuntimeError("output directory must be outside legacy state")

    cohorts = build_default_cohorts(DEFAULT_CONFIG)
    cohort_names = [cohort.name for cohort in cohorts]
    before = _hash_regular_inputs(legacy)
    evidence = _inventory_lines(legacy, cohort_names)
    after = _hash_regular_inputs(legacy)
    if before != after:
        raise RuntimeError("legacy input changed during inspection")

    output.mkdir(parents=True, exist_ok=True)
    if initialize_clean:
        _validate_output_topology(
            output,
            cohort_names,
            require_complete=False,
            initialize_clean=True,
        )
        _initialize_clean_ledgers(output, cohorts)
    else:
        _validate_output_topology(
            output,
            cohort_names,
            require_complete=False,
            initialize_clean=False,
        )

    lines = [
        "legacy_execution_model=same_session_close",
        "authoritative_import=false",
        "eligible_for_promotion=false",
        f"mode={'initialize-clean' if initialize_clean else 'dry-run'}",
        f"legacy_state={legacy}",
        f"output_dir={output}",
        f"cohort_count={len(cohorts)}",
        *evidence,
    ]
    for relative in sorted(before):
        lines.append(
            f"sha256 path={relative} before={before[relative]} "
            f"after={after[relative]} unchanged=true"
        )
    report_path = output / _REPORT_NAME
    _write_report(report_path, lines)
    _validate_output_topology(
        output,
        cohort_names,
        require_complete=True,
        initialize_clean=initialize_clean,
    )
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inventory legacy state or initialize clean cohort ledgers"
    )
    parser.add_argument("--legacy-state", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--initialize-clean", action="store_true")
    args = parser.parse_args()
    try:
        report = run_migration(
            args.legacy_state,
            args.output_dir,
            initialize_clean=args.initialize_clean,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
