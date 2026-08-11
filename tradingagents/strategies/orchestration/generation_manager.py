"""Core generation management for multi-version autoresearch.

Each generation is a frozen snapshot of the codebase at a specific commit,
running in its own git worktree with isolated state. This allows comparing
trading performance across code versions side-by-side.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from tradingagents.strategies.metrics.models import GOVERNED_BAR_RECOVERY_CONTRACT
from tradingagents.strategies.orchestration.runtime_lock import (
    canonical_runtime_lock_path,
)

logger = logging.getLogger(__name__)

_MAX_RUN_HISTORY = 100

# Wall-clock ceiling for a single generation's cohort run. This is an outer
# backstop: if a run gets suspended (e.g. the laptop sleeps on battery) or a
# fetch genuinely hangs, the subprocess is killed after this many seconds.
_GENERATION_TIMEOUT_S = 3600
_PREFLIGHT_MODES = frozenset({"all", "screen", "governed"})
_MAX_GOVERNED_REPORT_ITEMS = 256
_MAX_GOVERNED_REPORT_COHORTS = 64
_MAX_REPORT_TEXT = 4_096
_LOWER_HEX = frozenset("0123456789abcdef")
_MARKET_TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.^_-]{0,31}")
_COHORT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_GOVERNED_FAILURE_KINDS = frozenset(
    {"missing", "incoherent", "invalid", "invalid_benchmark"}
)
_RECOVERY_SUMMARY_KEYS = frozenset(
    {
        "ticker",
        "session",
        "recovery_id",
        "contract_version",
        "evidence_digest",
        "affected_cohort_ids",
    }
)
_STATE_STATUSES = frozenset(
    {"uninitialized", "ready", "state_already_invalid", "invalid"}
)
_GOVERNED_PROBE_STATUSES = frozenset(
    {"ready", "not_ready", "state_already_invalid", "failed"}
)


def _extract_cohort_results(stdout: str) -> dict | None:
    """Extract the trailing top-level JSON results object that run_cohorts.py
    prints (``json.dumps(result, indent=2)``) from its mixed stdout.

    Lets the parent detect both cohort execution failures and candidate-data
    quarantine, including from a frozen worktree whose runner exits 0. Returns
    None when no top-level object is parseable, in which case callers fall back
    to the exit code alone.
    """
    if not stdout:
        return None
    lines = stdout.splitlines()
    end = next(
        (i for i in range(len(lines) - 1, -1, -1) if lines[i].rstrip() == "}"), None
    )
    if end is None:
        return None
    start = next((i for i in range(end, -1, -1) if lines[i].startswith("{")), None)
    if start is None:
        return None
    try:
        obj = json.loads("\n".join(lines[start : end + 1]))
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _extract_preflight_report(stdout: str) -> dict | None:
    """Extract preflight JSON independently from daily cohort result parsing."""
    return _extract_cohort_results(stdout)


def _fixed_digest(value: object, prefix: str) -> bool:
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    suffix = value.removeprefix(prefix)
    return len(suffix) == 64 and all(character in _LOWER_HEX for character in suffix)


def _canonical_recoveries(
    value: object, trading_date: str, *, strict: bool = True
) -> list[dict] | None:
    """Validate top-level recovery evidence with the Task 5 report grammar."""
    if not isinstance(value, (list, tuple)):
        return None
    malformed = len(value) > _MAX_GOVERNED_REPORT_ITEMS
    normalized: dict[tuple[str, str, str], dict] = {}
    conflicts: set[tuple[str, str, str]] = set()
    for item in value[:_MAX_GOVERNED_REPORT_ITEMS]:
        if not isinstance(item, dict) or set(item) != _RECOVERY_SUMMARY_KEYS:
            malformed = True
            continue
        ticker = item.get("ticker")
        session_text = item.get("session")
        recovery_id = item.get("recovery_id")
        contract = item.get("contract_version")
        digest = item.get("evidence_digest")
        cohorts = item.get("affected_cohort_ids")
        texts = (ticker, session_text, recovery_id, contract, digest)
        if (
            any(
                not isinstance(text, str)
                or not text
                or len(text) > _MAX_REPORT_TEXT
                for text in texts
            )
            or ticker != ticker.strip().upper()
            or _MARKET_TICKER_RE.fullmatch(ticker) is None
            or session_text != trading_date
            or contract != GOVERNED_BAR_RECOVERY_CONTRACT
            or not _fixed_digest(recovery_id, "governed_bar_recovery:")
            or not _fixed_digest(digest, "sha256:")
            or not isinstance(cohorts, (list, tuple))
            or not cohorts
            or len(cohorts) > _MAX_GOVERNED_REPORT_COHORTS
            or any(
                not isinstance(cohort, str)
                or not cohort.strip()
                or len(cohort) > _MAX_REPORT_TEXT
                or _COHORT_ID_RE.fullmatch(cohort) is None
                for cohort in cohorts
            )
        ):
            malformed = True
            continue
        try:
            if date.fromisoformat(session_text).isoformat() != session_text:
                raise ValueError
        except ValueError:
            malformed = True
            continue
        canonical_cohorts = list(cohorts)
        if canonical_cohorts != sorted(set(canonical_cohorts)):
            malformed = True
            continue
        canonical = {
            "ticker": ticker,
            "session": session_text,
            "recovery_id": recovery_id,
            "contract_version": contract,
            "evidence_digest": digest,
            "affected_cohort_ids": canonical_cohorts,
        }
        key = (ticker, session_text, recovery_id)
        existing = normalized.get(key)
        if existing is not None and existing != canonical:
            conflicts.add(key)
        else:
            normalized[key] = canonical
    if strict and (malformed or conflicts):
        return None
    return [normalized[key] for key in sorted(normalized) if key not in conflicts]


def _canonical_failure_map(
    value: object, trading_date: str, *, strict: bool = True
) -> dict[str, str] | None:
    """Validate exact normalized P0 failures and omit unsafe history entries."""
    if not isinstance(value, dict):
        return None
    malformed = len(value) > _MAX_GOVERNED_REPORT_ITEMS
    normalized: dict[str, str] = {}
    keys = sorted(ticker for ticker in value if isinstance(ticker, str))
    if len(keys) != len(value):
        malformed = True
    for ticker in keys[:_MAX_GOVERNED_REPORT_ITEMS]:
        reason = value[ticker]
        if (
            not ticker
            or ticker != ticker.upper()
            or _MARKET_TICKER_RE.fullmatch(ticker) is None
            or len(ticker) > _MAX_REPORT_TEXT
            or not isinstance(reason, str)
            or len(reason) > _MAX_REPORT_TEXT
        ):
            malformed = True
            continue
        kind, separator, scope = reason.partition(" ")
        if (
            not separator
            or kind not in _GOVERNED_FAILURE_KINDS
            or scope != f"{ticker}/{trading_date}"
        ):
            malformed = True
            continue
        normalized[ticker] = reason
    if strict and malformed:
        return None
    return {ticker: normalized[ticker] for ticker in sorted(normalized)}


def normalize_preflight_report(
    report: object, *, mode: str, trading_date: str
) -> dict | None:
    """Return one exact, bounded, idempotent preflight wire report."""
    if mode not in _PREFLIGHT_MODES or not isinstance(report, dict):
        return None
    wire_keys = {"ok", "preflight_mode"}
    if mode in {"screen", "all"}:
        wire_keys.update({"screen_ok", "screen_failure_count"})
    if mode in {"governed", "all"}:
        wire_keys.update(
            {
                "governed_ok",
                "state_status",
                "governed_probe_status",
                "governed_bar_recoveries",
                "governed_failure_map",
            }
        )
    is_wire_report = "preflight_mode" in report
    if is_wire_report:
        if set(report) != wire_keys or report.get("preflight_mode") != mode:
            return None
    elif (
        not isinstance(report.get("failures"), list)
        or not isinstance(report.get("horizons"), dict)
    ):
        return None
    if type(report.get("ok")) is not bool:
        return None
    normalized: dict = {"ok": report["ok"], "preflight_mode": mode}
    screen_ok: bool | None = None
    governed_ok: bool | None = None
    if mode in {"screen", "all"}:
        if type(report.get("screen_ok")) is not bool:
            return None
        screen_ok = report["screen_ok"]
        if is_wire_report:
            failure_count = report.get("screen_failure_count")
            if (
                type(failure_count) is not int
                or failure_count < 0
                or failure_count > _MAX_GOVERNED_REPORT_ITEMS
            ):
                return None
        else:
            screen_failures = report.get("screen_failures", report["failures"])
            if not isinstance(screen_failures, list):
                return None
            failure_count = min(
                len(screen_failures), _MAX_GOVERNED_REPORT_ITEMS
            )
        normalized["screen_ok"] = screen_ok
        normalized["screen_failure_count"] = failure_count
    if mode in {"governed", "all"}:
        state_status = report.get("state_status")
        probe_status = report.get("governed_probe_status")
        governed_ok_value = report.get("governed_ok")
        if (
            type(governed_ok_value) is not bool
            or state_status not in _STATE_STATUSES
            or probe_status not in _GOVERNED_PROBE_STATUSES
        ):
            return None
        governed_ok = governed_ok_value
        recoveries = _canonical_recoveries(
            report.get("governed_bar_recoveries"), trading_date
        )
        failure_map = _canonical_failure_map(
            report.get("governed_failure_map"), trading_date
        )
        if recoveries is None or failure_map is None:
            return None
        recovery_tickers = {row["ticker"] for row in recoveries}
        if recovery_tickers & set(failure_map):
            return None
        if probe_status == "ready":
            consistent = (
                governed_ok is True
                and state_status in {"ready", "uninitialized"}
                and not failure_map
            )
        elif probe_status == "not_ready":
            consistent = (
                governed_ok is True
                and state_status in {"ready", "uninitialized"}
                and not recoveries
                and not failure_map
            )
        elif probe_status == "state_already_invalid":
            consistent = (
                governed_ok is False
                and state_status == "state_already_invalid"
                and not recoveries
                and not failure_map
            )
        else:
            consistent = (
                governed_ok is False
                and state_status in {"ready", "uninitialized", "invalid"}
                and not recoveries
            )
        if not consistent:
            return None
        normalized.update(
            {
                "governed_ok": governed_ok,
                "state_status": state_status,
                "governed_probe_status": probe_status,
                "governed_bar_recoveries": recoveries,
                "governed_failure_map": failure_map,
            }
        )
    expected_ok = (
        bool(screen_ok and governed_ok)
        if mode == "all"
        else bool(governed_ok)
        if mode == "governed"
        else bool(screen_ok)
    )
    if report["ok"] is not expected_ok:
        return None
    return normalized


def _preflight_subprocess_result(
    *,
    stdout: str,
    stderr: str,
    returncode: int,
    elapsed: float,
    mode: str,
    trading_date: str,
) -> dict:
    report = _extract_preflight_report(stdout)
    normalized = normalize_preflight_report(
        report, mode=mode, trading_date=trading_date
    )
    if normalized is None:
        raw_error = (stderr or stdout or "").strip()
        return {
            "success": False,
            "elapsed_s": round(elapsed, 2),
            "error": (
                "unrecognized arguments: --preflight"
                if "unrecognized arguments" in raw_error
                else "malformed preflight report"
            ),
        }
    result: dict = {
        "success": False,
        "elapsed_s": round(elapsed, 2),
        "preflight_mode": mode,
    }
    result.update({key: value for key, value in normalized.items() if key != "ok"})
    ready = (
        result.get("governed_probe_status") == "ready"
        if mode in {"governed", "all"}
        else True
    )
    result["success"] = bool(returncode == 0 and normalized["ok"] is True and ready)
    if not result["success"]:
        status = result.get("governed_probe_status", "failed")
        result["error"] = f"preflight {mode} status: {status}"
    return result


@dataclass
class GenerationInfo:
    """Metadata for a single generation (frozen code snapshot)."""

    gen_id: str  # "gen_001", "gen_002", ...
    git_commit: str  # Full SHA
    git_branch: str  # Branch name at creation time
    worktree_path: str  # Absolute path to .worktrees/gen_NNN
    state_dir: str  # Absolute path to data/generations/gen_NNN
    created_at: str  # ISO timestamp
    status: str  # "active", "paused", "retired"
    description: str  # User-provided description
    run_history: list[dict] = field(default_factory=list)


class GenerationManager:
    """Manage multiple frozen code generations via git worktrees."""

    def __init__(
        self,
        repo_root: str,
        generations_dir: str = "data/generations",
    ):
        self._repo_root = Path(repo_root).resolve()
        self._generations_dir = (self._repo_root / generations_dir).resolve()
        self._worktrees_dir = (self._repo_root / ".worktrees").resolve()
        self._manifest_path = self._generations_dir / "manifest.json"
        self._runtime_lock_path = canonical_runtime_lock_path(self._repo_root)

        # Use the venv python WITHOUT resolving symlinks — resolve() follows
        # the symlink chain to the base interpreter, which loses the venv
        # context (site-packages, installed packages like openbb).
        repo_venv = self._repo_root / ".venv" / "bin" / "python"
        if repo_venv.exists():
            self._venv_python = repo_venv.absolute()
        else:
            self._venv_python = Path(sys.executable).absolute()

        # Ensure directories exist
        self._generations_dir.mkdir(parents=True, exist_ok=True)
        self._worktrees_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_generation(self, description: str) -> GenerationInfo:
        """Create a new generation from current HEAD.

        1. Capture current commit and branch.
        2. Allocate next gen_id.
        3. Create a detached git worktree at the commit.
        4. Create an isolated state directory.
        5. Persist to manifest.
        """
        # 0. Warn about uncommitted changes (worktree won't include them)
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
        )
        if status_result.stdout.strip():
            logger.warning(
                "Working directory has uncommitted changes. "
                "The generation worktree will only contain committed code. "
                "Commit your changes first for a clean snapshot."
            )

        # 1. Get commit and branch
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        # 2. Allocate gen_id
        gen_id = self._next_gen_id()

        # 3. Create worktree
        worktree_path = self._worktrees_dir / gen_id
        subprocess.run(
            ["git", "worktree", "add", str(worktree_path), commit, "--detach"],
            cwd=self._repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        logger.info("Created worktree %s at %s", gen_id, commit[:12])

        # 4. Create state directory
        state_dir = self._generations_dir / gen_id
        state_dir.mkdir(parents=True, exist_ok=True)

        # 5. Build info and persist
        info = GenerationInfo(
            gen_id=gen_id,
            git_commit=commit,
            git_branch=branch,
            worktree_path=str(worktree_path),
            state_dir=str(state_dir),
            created_at=datetime.now().isoformat(),
            status="active",
            description=description,
            run_history=[],
        )

        manifest = self._load_manifest()
        manifest["generations"].append(asdict(info))
        self._save_manifest(manifest)

        logger.info(
            "Started generation %s: branch=%s commit=%s",
            gen_id,
            branch,
            commit[:12],
        )
        return info

    def run_daily(self, trading_date: str | None = None) -> dict[str, dict]:
        """Run daily trading for all active generations.

        Each generation runs in its own worktree via subprocess, with
        AUTORESEARCH_STATE_DIR and PYTHONPATH set for isolation.

        Returns:
            {gen_id: {"success": bool, "elapsed_s": float, "error"?: str}}
        """
        if not trading_date:
            trading_date = datetime.now().strftime("%Y-%m-%d")

        from tradingagents.strategies.orchestration.runtime_lock import runtime_lock

        results: dict[str, dict] = {}
        with runtime_lock(self._runtime_lock_path, exclusive=True) as lock:
            manifest = self._load_manifest()

            for gen_data in manifest["generations"]:
                if gen_data["status"] != "active":
                    continue

                gen_id = gen_data["gen_id"]
                logger.info("Running daily for %s (date=%s)", gen_id, trading_date)

                result = self._run_cohorts_subprocess(
                    gen_data,
                    ["--date", trading_date],
                    inherited_lock=lock,
                )
                results[gen_id] = result
                history_entry = {
                    "date": trading_date,
                    "action": "daily",
                    "success": result["success"],
                    "elapsed_s": result["elapsed_s"],
                    **({"degraded": True} if result.get("degraded") else {}),
                    **(
                        {"execution_valid": result["execution_valid"]}
                        if "execution_valid" in result
                        else {}
                    ),
                    **(
                        {
                            "candidate_bar_quarantines": result[
                                "candidate_bar_quarantines"
                            ]
                        }
                        if "candidate_bar_quarantines" in result
                        else {}
                    ),
                    **({"error": result["error"]} if "error" in result else {}),
                }
                recoveries = _canonical_recoveries(
                    result.get("governed_bar_recoveries"),
                    trading_date,
                    strict=False,
                )
                if recoveries:
                    history_entry["governed_bar_recoveries"] = recoveries
                failure_map = _canonical_failure_map(
                    result.get("governed_failure_map"),
                    trading_date,
                    strict=False,
                )
                if failure_map:
                    history_entry["governed_failure_map"] = failure_map
                gen_data["run_history"].append(history_entry)
                gen_data["run_history"] = gen_data["run_history"][-_MAX_RUN_HISTORY:]

            self._save_manifest(manifest)
        return results

    def run_learning(self) -> dict[str, dict]:
        """Refuse retired production learning without touching generation state."""
        raise RuntimeError("production learning is disabled; no subprocess was started")

    def run_preflight(
        self, trading_date: str | None = None, *, mode: str = "all"
    ) -> dict[str, dict]:
        """Run the no-write integrity preflight for every active generation.

        Each generation's frozen worktree runs ``run_cohorts.py --preflight``:
        live shared fetch, per-horizon screens, and the event-identity staging
        gates — with a throwaway state dir, no LLM, no ledger writes. Nothing
        is recorded in run_history and the manifest is untouched, so the
        check can repeat freely ahead of the scheduled cycle.

        Worktrees frozen before the ``--preflight`` flag existed report
        ``unsupported`` instead of a hard failure.

        Returns:
            {gen_id: {"success": bool, "elapsed_s": float,
                      "error"?: str, "unsupported"?: bool}}
        """
        if mode not in _PREFLIGHT_MODES:
            raise ValueError(f"invalid preflight mode {mode!r}")
        if not trading_date:
            trading_date = datetime.now().strftime("%Y-%m-%d")

        from tradingagents.strategies.orchestration.runtime_lock import runtime_lock

        results: dict[str, dict] = {}
        with runtime_lock(self._runtime_lock_path, exclusive=False) as lock:
            manifest = self._load_manifest()

            for gen_data in manifest["generations"]:
                if gen_data["status"] != "active":
                    continue

                gen_id = gen_data["gen_id"]
                logger.info("Preflight for %s (date=%s)", gen_id, trading_date)

                result = self._run_cohorts_subprocess(
                    gen_data,
                    [
                        "--date",
                        trading_date,
                        "--preflight",
                        "--preflight-mode",
                        mode,
                    ],
                    preflight_mode=mode,
                    write_log=False,
                    inherited_lock=lock,
                )
                if not result["success"] and "unrecognized arguments" in str(
                    result.get("error", "")
                ):
                    result["unsupported"] = True
                    result["error"] = (
                        "worktree predates --preflight support; "
                        "start a new generation from current main to enable it"
                    )
                results[gen_id] = result

        return results

    def pause_generation(self, gen_id: str) -> None:
        """Set a generation's status to 'paused'."""
        manifest = self._load_manifest()
        gen_data = self._find_gen(manifest, gen_id)
        if gen_data is None:
            raise ValueError(f"Generation {gen_id} not found")
        if gen_data["status"] == "retired":
            raise ValueError(f"Cannot pause retired generation {gen_id}")
        gen_data["status"] = "paused"
        self._save_manifest(manifest)
        logger.info("Paused generation %s", gen_id)

    def resume_generation(self, gen_id: str) -> None:
        """Resume a paused generation back to 'active'."""
        manifest = self._load_manifest()
        gen_data = self._find_gen(manifest, gen_id)
        if gen_data is None:
            raise ValueError(f"Generation {gen_id} not found")
        if gen_data["status"] != "paused":
            raise ValueError(
                f"Can only resume paused generations, {gen_id} is {gen_data['status']}"
            )
        gen_data["status"] = "active"
        self._save_manifest(manifest)
        logger.info("Resumed generation %s", gen_id)

    def retire_generation(
        self,
        gen_id: str,
        delete_worktree: bool = True,
    ) -> None:
        """Retire a generation. Optionally remove its git worktree.

        The state directory is preserved for historical comparison.
        """
        manifest = self._load_manifest()
        gen_data = self._find_gen(manifest, gen_id)
        if gen_data is None:
            raise ValueError(f"Generation {gen_id} not found")

        gen_data["status"] = "retired"

        if delete_worktree:
            worktree_path = gen_data["worktree_path"]
            if Path(worktree_path).exists():
                try:
                    subprocess.run(
                        ["git", "worktree", "remove", worktree_path, "--force"],
                        cwd=self._repo_root,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    logger.info("Removed worktree for %s", gen_id)
                except subprocess.CalledProcessError as e:
                    logger.warning(
                        "Failed to remove worktree for %s: %s",
                        gen_id,
                        e.stderr.strip(),
                    )

        self._save_manifest(manifest)
        logger.info("Retired generation %s", gen_id)

    def list_generations(self) -> list[GenerationInfo]:
        """Return all generations from manifest."""
        manifest = self._load_manifest()
        return [GenerationInfo(**gen_data) for gen_data in manifest["generations"]]

    def get_generation(self, gen_id: str) -> GenerationInfo | None:
        """Look up a single generation by ID."""
        manifest = self._load_manifest()
        gen_data = self._find_gen(manifest, gen_id)
        if gen_data is None:
            return None
        return GenerationInfo(**gen_data)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _run_cohorts_subprocess(
        self,
        gen_data: dict,
        extra_args: list[str],
        log_name: str = "last_run_output.log",
        *,
        preflight_mode: str | None = None,
        write_log: bool = True,
        inherited_lock: object | None = None,
    ) -> dict:
        """Run scripts/run_cohorts.py in a generation's worktree.

        Sets AUTORESEARCH_STATE_DIR and PYTHONPATH for isolation.
        Returns {"success": bool, "elapsed_s": float, "error"?: str}.
        """
        env = os.environ.copy()
        env["AUTORESEARCH_STATE_DIR"] = str(Path(gen_data["state_dir"]).resolve())
        env["PYTHONPATH"] = str(Path(gen_data["worktree_path"]).resolve())
        env["EVENTEDGE_GENERATION_ID"] = gen_data["gen_id"]
        env["EVENTEDGE_GENERATION_COMMIT"] = gen_data["git_commit"]
        pass_fds: tuple[int, ...] = ()
        if inherited_lock is not None:
            inherited_fd = int(getattr(inherited_lock, "fd"))
            inherited_exclusive = bool(getattr(inherited_lock, "exclusive"))
            env["EVENTEDGE_RUNTIME_LOCK_FD"] = str(inherited_fd)
            env["EVENTEDGE_RUNTIME_LOCK_MODE"] = (
                "exclusive" if inherited_exclusive else "shared"
            )
            pass_fds = (inherited_fd,)

        cmd = [
            str(self._venv_python),
            "scripts/run_cohorts.py",
            *extra_args,
        ]

        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                cwd=gen_data["worktree_path"],
                env=env,
                capture_output=True,
                text=True,
                timeout=_GENERATION_TIMEOUT_S,
                pass_fds=pass_fds,
            )
            elapsed = time.monotonic() - start

            # Persist the run's full stdout/stderr (per-source fetch counts,
            # per-strategy signal counts, etc.) so a silent strategy can always
            # be diagnosed after the fact — required by the "never ignore silent
            # strategies" rule. Kept on success too, not just on failure.
            if write_log:
                self._write_run_log(gen_data, proc.stdout, proc.stderr, log_name)

            if preflight_mode is not None:
                try:
                    date_index = extra_args.index("--date") + 1
                    preflight_date = extra_args[date_index]
                except (ValueError, IndexError):
                    preflight_date = ""
                return _preflight_subprocess_result(
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    returncode=proc.returncode,
                    elapsed=elapsed,
                    mode=preflight_mode,
                    trading_date=preflight_date,
                )

            # The printed cohort results are authoritative for the distinction
            # between execution failure and candidate-data quarantine.  Parse
            # them before interpreting the worker's nonzero alert exit code so
            # old frozen runners (which may exit zero) retain the same status.
            cohort_results = _extract_cohort_results(proc.stdout)
            if cohort_results is not None:
                from tradingagents.strategies.orchestration.cohort_orchestrator import (
                    aggregate_governed_reporting,
                    count_degraded_cohorts,
                    count_failed_cohorts,
                )

                n_failed, n_total, failed = count_failed_cohorts(cohort_results)
                n_degraded, _, degraded = count_degraded_cohorts(cohort_results)
                execution_valid = bool(cohort_results) and all(
                    isinstance(result, dict) and result.get("execution_valid") is True
                    for result in cohort_results.values()
                )
                quarantined_tickers = sorted(
                    {
                        str(ticker)
                        for name in degraded
                        for ticker in cohort_results[name].get(
                            "candidate_bar_quarantines", []
                        )
                    }
                )
                governed_recoveries, governed_failures = aggregate_governed_reporting(
                    cohort_results
                )
                if governed_recoveries and quarantined_tickers:
                    degradation_label = (
                        "candidate data quarantined; governed bar recovery"
                    )
                elif governed_recoveries:
                    degradation_label = "governed bar recovery"
                else:
                    degradation_label = "candidate data quarantined"
                if n_failed:
                    msg = f"{n_failed}/{n_total} cohorts failed: {', '.join(failed)}"
                    if n_degraded:
                        msg += (
                            f"; {n_degraded}/{n_total} cohorts degraded "
                            f"({degradation_label}): {', '.join(degraded)}"
                        )
                        if quarantined_tickers:
                            msg += "; quarantined tickers: " + ", ".join(
                                quarantined_tickers
                            )
                    logger.error("Generation %s: %s", gen_data["gen_id"], msg)
                    failure = {
                        "success": False,
                        "elapsed_s": round(elapsed, 2),
                        "error": msg,
                        "execution_valid": execution_valid,
                    }
                    if n_degraded:
                        failure.update(
                            {
                                "degraded": True,
                                "candidate_bar_quarantines": quarantined_tickers,
                            }
                        )
                    if governed_recoveries:
                        failure["governed_bar_recoveries"] = governed_recoveries
                    if governed_failures:
                        failure["governed_failure_map"] = governed_failures
                    return failure

                if n_degraded:
                    msg = (
                        f"{n_degraded}/{n_total} cohorts degraded "
                        f"({degradation_label}): {', '.join(degraded)}"
                    )
                    if quarantined_tickers:
                        msg += "; quarantined tickers: " + ", ".join(
                            quarantined_tickers
                        )
                    logger.warning("Generation %s: %s", gen_data["gen_id"], msg)
                    degraded_result = {
                        "success": False,
                        "degraded": True,
                        "execution_valid": execution_valid,
                        "candidate_bar_quarantines": quarantined_tickers,
                        "elapsed_s": round(elapsed, 2),
                        "error": msg,
                    }
                    if governed_recoveries:
                        degraded_result["governed_bar_recoveries"] = governed_recoveries
                    if governed_failures:
                        degraded_result["governed_failure_map"] = governed_failures
                    return degraded_result

            if proc.returncode != 0:
                error_msg = (proc.stderr or proc.stdout or "").strip()
                # Truncate long error output
                if len(error_msg) > 2000:
                    error_msg = error_msg[:2000] + "...(truncated)"
                logger.error(
                    "Generation %s failed (rc=%d): %s",
                    gen_data["gen_id"],
                    proc.returncode,
                    error_msg[:200],
                )
                return {
                    "success": False,
                    "elapsed_s": round(elapsed, 2),
                    "error": error_msg,
                }

            logger.info(
                "Generation %s completed in %.1fs",
                gen_data["gen_id"],
                elapsed,
            )
            return {"success": True, "elapsed_s": round(elapsed, 2)}

        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            logger.error(
                "Generation %s timed out after %.0fs", gen_data["gen_id"], elapsed
            )
            return {
                "success": False,
                "elapsed_s": round(elapsed, 2),
                "error": f"Timed out after {_GENERATION_TIMEOUT_S}s",
            }
        except Exception as e:
            elapsed = time.monotonic() - start
            logger.error("Generation %s error: %s", gen_data["gen_id"], e)
            return {
                "success": False,
                "elapsed_s": round(elapsed, 2),
                "error": str(e),
            }

    def _write_run_log(
        self,
        gen_data: dict,
        stdout: str,
        stderr: str,
        log_name: str = "last_run_output.log",
    ) -> None:
        """Write a generation run's captured output to its state dir.

        Overwrites {state_dir}/{log_name} each run (bounded, no growth).
        This is the durable record of what each strategy's data fetch returned —
        the evidence needed to classify any silent strategy.
        """
        try:
            log_path = Path(gen_data["state_dir"]) / log_name
            log_path.parent.mkdir(parents=True, exist_ok=True)
            header = f"=== {gen_data.get('gen_id', '?')} run @ {datetime.now().isoformat()} ===\n"
            log_path.write_text(
                header
                + "----- STDOUT -----\n"
                + (stdout or "")
                + "\n----- STDERR -----\n"
                + (stderr or "")
            )
        except Exception:
            logger.warning(
                "Failed to persist run log for %s",
                gen_data.get("gen_id"),
                exc_info=True,
            )

    def _next_gen_id(self) -> str:
        """Return the next sequential gen_id like 'gen_001', 'gen_002'."""
        manifest = self._load_manifest()
        if not manifest["generations"]:
            return "gen_001"

        # Parse existing IDs to find the max
        max_num = 0
        for gen_data in manifest["generations"]:
            try:
                num = int(gen_data["gen_id"].split("_")[1])
                max_num = max(max_num, num)
            except (IndexError, ValueError):
                continue
        return f"gen_{max_num + 1:03d}"

    def _find_gen(self, manifest: dict, gen_id: str) -> dict | None:
        """Find a generation dict in the manifest by ID."""
        for gen_data in manifest["generations"]:
            if gen_data["gen_id"] == gen_id:
                return gen_data
        return None

    def _load_manifest(self) -> dict:
        """Load manifest.json. Returns empty structure if not found."""
        if not self._manifest_path.exists():
            return {"generations": []}
        try:
            with open(self._manifest_path) as f:
                data = json.load(f)
            if "generations" not in data:
                data["generations"] = []
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load manifest: %s", e)
            return {"generations": []}

    def _save_manifest(self, data: dict) -> None:
        """Atomic write of manifest.json."""
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self._manifest_path.parent,
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, default=str)
                f.write("\n")
            os.replace(tmp, self._manifest_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
