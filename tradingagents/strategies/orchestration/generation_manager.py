"""Core generation management for multi-version autoresearch.

Each generation is a frozen snapshot of the codebase at a specific commit,
running in its own git worktree with isolated state. This allows comparing
trading performance across code versions side-by-side.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timezone
from pathlib import Path
from functools import wraps

from tradingagents.strategies.metrics.models import GOVERNED_BAR_RECOVERY_CONTRACT, GOVERNED_SIP_RECOVERY_CONTRACT
from tradingagents.strategies.orchestration.run_evidence import (
    persist_run_evidence,
    sanitize_evidence_text,
)
from tradingagents.strategies.orchestration.run_outcome import (
    DAILY_RESULT_ENVELOPE_KEYS,
    DAILY_RESULT_PREFIX,
    DAILY_RESULT_WIRE_VERSION,
    RunOutcome,
)
from tradingagents.strategies.orchestration.runtime_lock import (
    canonical_runtime_lock_path,
)

logger = logging.getLogger(__name__)

_MAX_RUN_HISTORY = 100
_PREFLIGHT_REAP_TIMEOUT_S = 5


def _run_preflight_worker(command, *, cwd, env, timeout, pass_fds):
    """Own the diagnostic process group, including native source transports.

    A killed screen cannot reap its own transports. Keep stdout/stderr separate
    and retain subprocess.run's completed/timeout evidence contract while
    terminating the whole owned session on every exit.
    """
    process = None
    pending_signal = None
    previous_handlers = {}

    def kill_group():
        if process is None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def propagate_signal(signum, frame):
        previous = previous_handlers[signum]
        if callable(previous):
            previous(signum, frame)
        elif previous != signal.SIG_IGN:
            # Preserve the original signal exit status, rather than turning
            # controller termination into an ordinary failed preflight.
            signal.signal(signum, previous)
            os.kill(os.getpid(), signum)

    def forward_signal(signum, frame):
        nonlocal pending_signal
        if previous_handlers[signum] == signal.SIG_IGN:
            return
        if process is None:
            # A signal during Popen must not strand a newly detached child.
            # Defer termination until its PID is available, without changing
            # the child's inherited signal mask or using unsafe preexec code.
            pending_signal = (signum, frame)
            return
        kill_group()
        process.wait(timeout=_PREFLIGHT_REAP_TIMEOUT_S)
        propagate_signal(signum, frame)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, forward_signal)
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True,
                                   start_new_session=True, pass_fds=pass_fds)
        if pending_signal is not None:
            signum, frame = pending_signal
            pending_signal = None
            forward_signal(signum, frame)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group()
            stdout, stderr = process.communicate(timeout=_PREFLIGHT_REAP_TIMEOUT_S)
            raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from None
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        # A native child can remain even when the screen itself already exited.
        try:
            kill_group()
            if process is not None and process.poll() is None:
                process.wait(timeout=_PREFLIGHT_REAP_TIMEOUT_S)
        finally:
            try:
                if process is not None:
                    for stream in (process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
            finally:
                for signum, previous in previous_handlers.items():
                    signal.signal(signum, previous)
                if pending_signal is not None:
                    propagate_signal(*pending_signal)

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


def _normalize_daily_candidate_issues(
    result: dict, trading_date: str
) -> dict:
    """Canonicalize run-level candidate issues, failing closed on mismatch."""
    if "candidate_input_issues" not in result:
        return result
    from tradingagents.strategies.orchestration.daily_pipeline import (
        canonical_candidate_input_issue_summaries,
    )

    diagnostics = {
        key: result[key]
        for key in ("evidence_path", "evidence_error")
        if key in result
    }
    try:
        candidate_issues = canonical_candidate_input_issue_summaries(
            result["candidate_input_issues"], trading_date
        )
    except ValueError:
        return {
            "outcome": RunOutcome.FAILED.value,
            "success": False,
            "elapsed_s": result.get("elapsed_s", 0.0),
            "error": "invalid candidate input issue summary",
            **diagnostics,
        }
    outcome = result.get("outcome")
    consistent = result.get("success") is False and (
        (
            outcome == RunOutcome.DEGRADED.value
            and result.get("degraded") is True
            and result.get("execution_valid") is True
        )
        or (
            outcome == RunOutcome.FAILED.value
            and ("degraded" not in result or result.get("degraded") is True)
        )
    )
    if not consistent:
        return {
            "outcome": RunOutcome.FAILED.value,
            "success": False,
            "elapsed_s": result.get("elapsed_s", 0.0),
            "error": "invalid candidate input issue summary",
            **diagnostics,
        }
    normalized = dict(result)
    normalized["candidate_input_issues"] = candidate_issues
    return normalized


def _daily_history_entry(result: dict, trading_date: str) -> dict:
    """Build the bounded manifest history projection for one daily result."""
    history_entry = {
        "date": trading_date,
        "action": "daily",
        "outcome": result["outcome"],
        "success": result["success"],
        "elapsed_s": result["elapsed_s"],
    }
    if result.get("degraded"):
        history_entry["degraded"] = True
    for key in (
        "execution_valid",
        "input_coverage_valid",
        "source_health_failures",
        "source_scope_limits",
        "candidate_bar_quarantines",
        "error",
        "evidence_path",
        "evidence_error",
    ):
        if key in result:
            history_entry[key] = result[key]
    recoveries = _canonical_recoveries(
        result.get("governed_bar_recoveries"), trading_date, strict=False
    )
    if recoveries:
        history_entry["governed_bar_recoveries"] = recoveries
    failure_map = _canonical_failure_map(
        result.get("governed_failure_map"), trading_date, strict=False
    )
    if failure_map:
        history_entry["governed_failure_map"] = failure_map
    if result.get("candidate_input_issues"):
        history_entry["candidate_input_issues"] = result["candidate_input_issues"]
    return history_entry


def _extract_cohort_results(stdout: str) -> dict | None:
    """Extract one final, versioned daily-result envelope from worker stdout."""
    if not stdout:
        return None
    lines = stdout.splitlines()
    marked = [
        (index, line)
        for index, line in enumerate(lines)
        if line.startswith(DAILY_RESULT_PREFIX)
    ]
    if len(marked) != 1:
        return None
    index, line = marked[0]
    last_nonempty = next(
        (i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), None
    )
    if index != last_nonempty:
        return None
    try:
        envelope = json.loads(line.removeprefix(DAILY_RESULT_PREFIX))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(envelope, dict) or set(envelope) != DAILY_RESULT_ENVELOPE_KEYS:
        return None
    if (
        type(envelope["wire_version"]) is not int
        or envelope["wire_version"] != DAILY_RESULT_WIRE_VERSION
        or not isinstance(envelope["cohort_results"], dict)
    ):
        return None
    return envelope["cohort_results"]


def _valid_daily_cohort_results(
    cohort_results: dict, trading_date: str | None = None
) -> bool:
    from tradingagents.strategies.orchestration.daily_pipeline import (
        aggregate_candidate_input_issues,
    )
    from tradingagents.strategies.orchestration.source_coverage import aggregate_source_health_failures, aggregate_source_scope_limits
    from tradingagents.strategies.orchestration.cohort_orchestrator import (
        build_default_cohorts,
    )

    expected = {cohort.name for cohort in build_default_cohorts({})}
    if set(cohort_results) != expected:
        return False
    try:
        candidate_issues = aggregate_candidate_input_issues(
            cohort_results, trading_date
        )
        aggregate_source_health_failures(cohort_results, trading_date, require_coverage=True)
        aggregate_source_scope_limits(cohort_results)
    except ValueError:
        return False
    affected_by_issue = {
        str(cohort)
        for issue in candidate_issues
        for cohort in issue["affected_cohorts"]
    }
    lifecycle_fields = ("degraded", "execution_valid", "staging_valid")
    for cohort_name, result in cohort_results.items():
        if not isinstance(result, dict) or type(result.get("error")) is not bool:
            return False
        if result["error"]:
            if any(
                field in result and type(result[field]) is not bool
                for field in lifecycle_fields
            ):
                return False
            if result.get("staging_valid") is True:
                return False
            continue
        if any(type(result.get(field)) is not bool for field in lifecycle_fields):
            return False
        if result["execution_valid"] is not True:
            return False
        if result["degraded"] is False and result["staging_valid"] is not True:
            return False
        if cohort_name in affected_by_issue and (
            result["degraded"] is not True or result["staging_valid"] is not False
        ):
            return False
    return True


def _extract_preflight_report(stdout: str) -> dict | None:
    """Extract preflight JSON independently from daily cohort result parsing."""
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
            or contract not in {GOVERNED_BAR_RECOVERY_CONTRACT, GOVERNED_SIP_RECOVERY_CONTRACT}
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
        wire_keys.update({"screen_ok", "screen_failure_count", "screen_source_failures"})
        if "screen_runtime_failures" in report:
            wire_keys.add("screen_runtime_failures")
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
            failure_count = len(screen_failures)
            if failure_count > _MAX_GOVERNED_REPORT_ITEMS:
                return None
        from tradingagents.strategies.orchestration.preflight import (
            canonical_screen_source_failures,
            canonical_screen_runtime_failures,
        )
        source_failures = canonical_screen_source_failures(
            report.get("screen_source_failures")
        )
        if (
            source_failures is None
            or len(source_failures) > failure_count
            or screen_ok is not (failure_count == 0)
            or (not is_wire_report and len(source_failures) != sum(
                isinstance(row, dict) and row.get("strategy") == "source_health"
                for row in screen_failures
            ))
        ):
            return None
        normalized["screen_ok"] = screen_ok
        normalized["screen_failure_count"] = failure_count
        normalized["screen_source_failures"] = source_failures
        if "screen_runtime_failures" in report:
            runtime_failures = canonical_screen_runtime_failures(report["screen_runtime_failures"])
            if runtime_failures is None or len(runtime_failures) + len(source_failures) > failure_count:
                return None
            normalized["screen_runtime_failures"] = runtime_failures
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
        runtime = result.get("screen_runtime_failures", [])
        result["error"] = (f"preflight {mode} runtime: " + ', '.join(row['reason_code'] for row in runtime)
                           if runtime else f"preflight {mode} status: {status}")
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
    config_file: str | None = None
    config_sha256: str | None = None


class GenerationManifestError(ValueError):
    """An existing generation roster cannot be safely interpreted."""


def _lifecycle_locked(operation):
    """Serialize the complete read/modify/write operation with daily workers."""
    @wraps(operation)
    def locked(self, *args, **kwargs):
        from tradingagents.strategies.orchestration.runtime_lock import runtime_lock

        with runtime_lock(self._runtime_lock_path, exclusive=True):
            return operation(self, *args, **kwargs)

    return locked


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
        self._start_intent_path = self._generations_dir / ".pending-start.json"
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

    @_lifecycle_locked
    def start_generation(self, description: str, *, config_file=None) -> GenerationInfo:
        """Create a new generation from current HEAD.

        1. Capture current commit and branch.
        2. Allocate next gen_id.
        Record a durable intent before creating the frozen worktree and state.
        Retrying an interrupted request resumes its original snapshot.
        """
        from .generation_config import read_generation_config, FROZEN_CONFIG_NAME

        profile = read_generation_config(config_file) if config_file is not None else None
        manifest = self._load_manifest()
        pending = self._load_start_intent()
        if pending is not None:
            record = pending["record"]
            existing = self._find_gen(manifest, record["gen_id"])
            same_request = (
                record["description"] == description
                and record.get("config_sha256") == (profile[2] if profile is not None else None)
            )
            if existing is not None:
                immutable = record.keys() - {"status", "run_history"}
                if any(existing.get(key) != record[key] for key in immutable):
                    raise GenerationManifestError("pending generation conflicts with manifest; requires operator review")
                self._clear_start_intent()
                if same_request:
                    return GenerationInfo(**existing)
            else:
                if not same_request:
                    raise GenerationManifestError("different generation start is pending; requires operator review")
                return self._finish_generation_start(pending, manifest)
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

        # Reject unknown historical or foreign data before recording ownership.
        worktree_path = self._worktrees_dir / gen_id
        state_dir = self._generations_dir / gen_id
        for path in (worktree_path, state_dir):
            self._reject_lifecycle_symlinks(path)
            if path.exists():
                raise GenerationManifestError("generation target already exists; requires operator review")
        frozen_config = state_dir / FROZEN_CONFIG_NAME if profile is not None else None

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
            config_file=str(frozen_config) if frozen_config is not None else None,
            config_sha256=profile[2] if profile is not None else None,
        )

        record = asdict(info)
        if profile is None:
            record.pop('config_file')
            record.pop('config_sha256')
        pending = {"version": 1, "record": record, "config_text": profile[0].decode("utf-8") if profile is not None else None}
        self._write_atomic(self._start_intent_path, json.dumps(pending).encode("utf-8"))
        return self._finish_generation_start(pending, manifest)

    @staticmethod
    def _reject_lifecycle_symlinks(path: Path) -> None:
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise GenerationManifestError("generation path contains a symlink; requires operator review")

    def _load_start_intent(self) -> dict | None:
        """Only a validated, manager-owned intent authorizes resuming side effects."""
        self._reject_lifecycle_symlinks(self._start_intent_path)
        try:
            fd = os.open(self._start_intent_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        try:
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError("intent is not a regular file")
                body = handle.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise ValueError("intent is too large")
            pending = json.loads(body)
            if (not isinstance(pending, dict) or set(pending) != {"version", "record", "config_text"}
                    or type(pending["version"]) is not int or pending["version"] != 1):
                raise ValueError("invalid intent")
            record = pending["record"]
            self._validate_manifest({"generations": [record]})
            if (set(record) - {item.name for item in fields(GenerationInfo)}
                    or record["status"] != "active" or record["run_history"] != []
                    or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", record["git_commit"])
                    or record["worktree_path"] != str(self._worktrees_dir / record["gen_id"])
                    or record["state_dir"] != str(self._generations_dir / record["gen_id"])):
                raise ValueError("invalid intent identity")
            config_text = pending["config_text"]
            if record.get("config_file") is None:
                if config_text is not None:
                    raise ValueError("unexpected configuration")
            elif (not isinstance(config_text, str)
                    or hashlib.sha256(config_text.encode("utf-8")).hexdigest() != record["config_sha256"]):
                raise ValueError("invalid configuration digest")
            return pending
        except (ValueError, OSError, KeyError, TypeError) as error:
            raise GenerationManifestError("pending generation start is invalid; requires operator review") from error

    def _clear_start_intent(self) -> None:
        self._start_intent_path.unlink()
        self._sync_directory(self._generations_dir)

    def _finish_generation_start(self, pending: dict, manifest: dict) -> GenerationInfo:
        """Complete known interrupted creation without deleting any existing data."""
        record = pending["record"]
        worktree = Path(record["worktree_path"])
        state = Path(record["state_dir"])
        for path in (worktree, state):
            self._reject_lifecycle_symlinks(path)
        registered = subprocess.run(
            ["git", "worktree", "list", "--porcelain", "-z"], cwd=self._repo_root,
            capture_output=True, text=True, check=True,
        ).stdout.split("\0")
        owned = f"worktree {worktree}" in registered
        if not worktree.exists() and not owned:
            subprocess.run(
                ["git", "worktree", "add", str(worktree), record["git_commit"], "--detach"],
                cwd=self._repo_root, capture_output=True, text=True, check=True,
            )
        elif not worktree.is_dir() or not owned:
            raise GenerationManifestError("pending worktree is ambiguous; requires operator review")
        self._verify_pending_worktree(worktree, record["git_commit"])
        if state.exists() and not state.is_dir():
            raise GenerationManifestError("pending state is not a directory; requires operator review")
        state.mkdir(exist_ok=True)
        expected = {Path(record["config_file"]).name} if record.get("config_file") else set()
        if not state.is_dir() or any(item.name not in expected or item.is_symlink() or not item.is_file() for item in state.iterdir()):
            raise GenerationManifestError("pending state contains unknown data; requires operator review")
        if expected:
            config = Path(record["config_file"])
            body = pending["config_text"].encode("utf-8")
            if config.exists() and config.read_bytes() != body:
                raise GenerationManifestError("pending configuration is modified; requires operator review")
            if not config.exists():
                self._write_atomic(config, body)
        manifest["generations"].append(record)
        self._save_manifest(manifest)
        self._clear_start_intent()
        logger.info("Started generation %s at %s", record["gen_id"], record["git_commit"][:12])
        return GenerationInfo(**record)

    def _verify_pending_worktree(self, worktree: Path, commit: str) -> None:
        try:
            # A foreign/modified checkout is never reset or discarded automatically.
            self._reject_lifecycle_symlinks(worktree / ".git")
            if not (worktree / ".git").is_file():
                raise GenerationManifestError("pending worktree Git pointer is invalid; requires operator review")
            common_args = ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]
            expected_common = subprocess.run(
                common_args, cwd=self._repo_root, capture_output=True, text=True, check=True,
            ).stdout.strip()
            actual_common = subprocess.run(
                common_args, cwd=worktree, capture_output=True, text=True, check=True,
            ).stdout.strip()
            if Path(actual_common).resolve() != Path(expected_common).resolve():
                raise GenerationManifestError("pending worktree belongs to another repository; requires operator review")
            admin = Path(subprocess.run(
                ["git", "rev-parse", "--absolute-git-dir"],
                cwd=worktree, capture_output=True, text=True, check=True,
            ).stdout.strip())
            backlink = admin / "gitdir"
            self._reject_lifecycle_symlinks(backlink)
            if (admin.parent != Path(expected_common) / "worktrees"
                    or not backlink.is_file()
                    or backlink.read_text().strip() != str(worktree / ".git")):
                raise GenerationManifestError("pending worktree Git identity conflicts; requires operator review")
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=worktree, capture_output=True, text=True, check=True,
            ).stdout.strip()
            status = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all", "--ignored"],
                cwd=worktree, capture_output=True, text=True, check=True,
            ).stdout
            if head != commit or status.strip():
                raise GenerationManifestError("pending worktree is modified; requires operator review")
        except (OSError, subprocess.CalledProcessError) as error:
            raise GenerationManifestError("pending worktree Git identity is invalid; requires operator review") from error

    def _runtime_guard(self, *, exclusive: bool):
        """Retain a verified controller lease across the complete daily wrapper.

        Standalone commands still acquire their normal shared/exclusive locks.
        Only a verified inherited exclusive lease can pin the whole roster;
        preflight must not downgrade that parent's lease to a shared lock.
        """
        from .runtime_lock import RuntimeLockInvalid, runtime_lock

        fd_value = os.environ.get("EVENTEDGE_RUNTIME_LOCK_FD")
        mode_value = os.environ.get("EVENTEDGE_RUNTIME_LOCK_MODE")
        if fd_value is None and mode_value is None:
            return runtime_lock(self._runtime_lock_path, exclusive=exclusive)
        if (mode_value != "exclusive" or fd_value is None
                or not re.fullmatch(r"[0-9]{1,10}", fd_value)):
            raise RuntimeLockInvalid("inherited controller runtime lock is invalid")
        return runtime_lock(
            self._runtime_lock_path, exclusive=True,
            inherited_fd=int(fd_value), inherited_exclusive=True,
        )

    def run_daily(self, trading_date: str | None = None) -> dict[str, dict]:
        """Run daily trading for all active generations.

        Each generation runs in its own worktree via subprocess, with
        AUTORESEARCH_STATE_DIR and PYTHONPATH set for isolation.

        Returns:
            {gen_id: {"outcome": "clean" | "degraded" | "failed",
                      "success": bool, "elapsed_s": float, "error"?: str}}
        """
        if not trading_date:
            from tradingagents.strategies.orchestration.trading_calendar import exchange_date

            trading_date = exchange_date().isoformat()

        results: dict[str, dict] = {}
        with self._runtime_guard(exclusive=True) as lock:
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
                result = _normalize_daily_candidate_issues(result, trading_date)
                results[gen_id] = result
                history_entry = _daily_history_entry(result, trading_date)
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
            from tradingagents.strategies.orchestration.trading_calendar import exchange_date

            trading_date = exchange_date().isoformat()

        results: dict[str, dict] = {}
        with self._runtime_guard(exclusive=False) as lock:
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

    @_lifecycle_locked
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

    @_lifecycle_locked
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

    @_lifecycle_locked
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

        # Publication precedes cleanup: an interrupted cleanup leaves a retired
        # record and preserved state, never an active record with missing code.
        if gen_data["status"] != "retired":
            gen_data["status"] = "retired"
            self._save_manifest(manifest)
        else:
            # A prior rename may have succeeded before directory fsync failed.
            # Establish durability on retry before deleting the frozen code.
            self._sync_directory(self._manifest_path.parent)

        if delete_worktree:
            worktree_path = gen_data["worktree_path"]
            self._reject_lifecycle_symlinks(Path(worktree_path))
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
        Daily results include a canonical ``outcome``. Preflight results retain
        their independent outcome-free report contract.
        """
        started_at = datetime.now(timezone.utc)
        capture: dict = {
            "stdout": "",
            "stderr": "",
            "returncode": None,
            "status": "launch_error",
        }
        result = self._run_cohorts_subprocess_result(
            gen_data,
            extra_args,
            log_name,
            preflight_mode=preflight_mode,
            write_log=write_log,
            inherited_lock=inherited_lock,
            capture=capture,
        )
        if preflight_mode is not None and "config_sha256" in capture:
            result = {**result, "config_sha256": capture["config_sha256"]}
        finished_at = datetime.now(timezone.utc)
        try:
            date_index = extra_args.index("--date") + 1
            requested_session = extra_args[date_index]
        except (ValueError, IndexError):
            requested_session = ""
        evidence = {
            "schema_version": 1,
            "generation_id": gen_data["gen_id"],
            "generation_commit": gen_data["git_commit"],
            "requested_session": requested_session,
            "action": "preflight" if preflight_mode is not None else "daily",
            "preflight_mode": preflight_mode,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "process_return_code": capture["returncode"],
            "process_status": capture["status"],
            "stdout": capture["stdout"],
            "stderr": capture["stderr"],
            "result": result,
        }
        result = dict(result)
        try:
            result["evidence_path"] = str(
                persist_run_evidence(self._repo_root, evidence)
            )
        except Exception as error:  # noqa: BLE001
            # Diagnostics are best effort and cannot replace a completed run result.
            error_text = str(error) or type(error).__name__
            result["evidence_error"] = sanitize_evidence_text(error_text)
            logger.warning(
                "Failed to persist attempt evidence for %s: %s",
                gen_data.get("gen_id"),
                result["evidence_error"],
            )
        return result

    def _run_cohorts_subprocess_result(
        self,
        gen_data: dict,
        extra_args: list[str],
        log_name: str = "last_run_output.log",
        *,
        preflight_mode: str | None = None,
        write_log: bool = True,
        inherited_lock: object | None = None,
        capture: dict,
    ) -> dict:
        """Produce the existing worker result and collect process diagnostics."""
        env = os.environ.copy()
        from .generation_config import CONFIG_FILE_ENV, CONFIG_SHA_ENV, read_generation_config

        env.pop(CONFIG_FILE_ENV, None)
        env.pop(CONFIG_SHA_ENV, None)
        if gen_data.get('config_file') is not None or gen_data.get('config_sha256') is not None:
            try:
                if not gen_data.get('config_file') or not gen_data.get('config_sha256'):
                    raise ValueError('generation_config_invalid')
                _, _, validated_config_sha256 = read_generation_config(
                    gen_data['config_file'], gen_data['config_sha256']
                )
            except ValueError:
                failure = {'success': False, 'elapsed_s': 0.0, 'error': 'generation_config_invalid'}
                if preflight_mode is None:
                    failure['outcome'] = RunOutcome.FAILED.value
                return failure
            capture['config_sha256'] = validated_config_sha256
            env[CONFIG_FILE_ENV] = gen_data['config_file']
            env[CONFIG_SHA_ENV] = gen_data['config_sha256']
        env["AUTORESEARCH_STATE_DIR"] = str(Path(gen_data["state_dir"]).resolve())
        env["PYTHONPATH"] = str(Path(gen_data["worktree_path"]).resolve())
        env["EVENTEDGE_GENERATION_ID"] = gen_data["gen_id"]
        env["EVENTEDGE_GENERATION_COMMIT"] = gen_data["git_commit"]
        env.setdefault("EVENTEDGE_SOURCE_CACHE_DIR", str(Path(getattr(self, "_repo_root", gen_data["worktree_path"])) / "data" / "source_cache"))
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
            proc = _run_preflight_worker(
                cmd, cwd=gen_data["worktree_path"], env=env,
                timeout=_GENERATION_TIMEOUT_S, pass_fds=pass_fds,
            )
            elapsed = time.monotonic() - start
            capture.update(
                {
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "returncode": proc.returncode,
                    "status": "completed",
                }
            )

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
            # between execution failure and candidate-data quarantine. Parse
            # and validate the versioned worker wire before interpreting its
            # alert exit code; unmarked or contradictory output fails closed.
            cohort_results = _extract_cohort_results(proc.stdout)
            try:
                date_index = extra_args.index("--date") + 1
                daily_trading_date = extra_args[date_index]
            except (ValueError, IndexError):
                daily_trading_date = ""
            if cohort_results is None or not _valid_daily_cohort_results(
                cohort_results, daily_trading_date
            ):
                logger.error(
                    "Generation %s returned an invalid daily worker result",
                    gen_data["gen_id"],
                )
                return {
                    "outcome": RunOutcome.FAILED.value,
                    "success": False,
                    "elapsed_s": round(elapsed, 2),
                    "error": "invalid daily worker result",
                }
            if cohort_results is not None:
                from tradingagents.strategies.orchestration.daily_pipeline import (
                    summarize_cohort_results,
                )

                summary = summarize_cohort_results(cohort_results, daily_trading_date)
                n_failed = len(summary.failed)
                n_total = summary.total
                failed = summary.failed
                n_degraded, degraded = len(summary.degraded), summary.degraded
                execution_valid = summary.execution_valid
                quarantined_tickers = list(summary.candidate_bar_quarantines)
                governed_recoveries = list(summary.governed_bar_recoveries)
                governed_failures = summary.governed_failure_map
                candidate_issues = list(summary.candidate_input_issues)
                degradation_label = (
                    summary.degradation_label or "candidate data quarantined"
                )
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
                        "outcome": RunOutcome.FAILED.value,
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
                    if candidate_issues:
                        failure["candidate_input_issues"] = candidate_issues
                    failure["input_coverage_valid"] = summary.input_coverage_valid
                    failure["source_health_failures"] = list(summary.source_health_failures)
                    from .source_coverage import aggregate_source_scope_limits
                    if limits := aggregate_source_scope_limits(cohort_results):
                        failure['source_scope_limits'] = limits
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
                        "outcome": (
                            RunOutcome.DEGRADED.value
                            if proc.returncode in {0, 2}
                            else RunOutcome.FAILED.value
                        ),
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
                    if candidate_issues:
                        degraded_result["candidate_input_issues"] = candidate_issues
                    degraded_result["input_coverage_valid"] = summary.input_coverage_valid
                    degraded_result["source_health_failures"] = list(summary.source_health_failures)
                    from .source_coverage import aggregate_source_scope_limits
                    if limits := aggregate_source_scope_limits(cohort_results):
                        degraded_result['source_scope_limits'] = limits
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
                    "outcome": RunOutcome.FAILED.value,
                    "success": False,
                    "elapsed_s": round(elapsed, 2),
                    "error": error_msg,
                }

            logger.info(
                "Generation %s completed in %.1fs",
                gen_data["gen_id"],
                elapsed,
            )
            from .source_coverage import aggregate_source_scope_limits
            scope_limits = aggregate_source_scope_limits(cohort_results)
            return {
                "outcome": RunOutcome.CLEAN.value,
                "success": True,
                "execution_valid": summary.execution_valid,
                "input_coverage_valid": summary.input_coverage_valid,
                "source_health_failures": list(summary.source_health_failures),
                **({'source_scope_limits': scope_limits} if scope_limits else {}),
                "elapsed_s": round(elapsed, 2),
            }

        except subprocess.TimeoutExpired as error:
            elapsed = time.monotonic() - start
            capture.update(
                {
                    "stdout": error.stdout or "",
                    "stderr": error.stderr or "",
                    "returncode": None,
                    "status": "timeout",
                }
            )
            logger.error(
                "Generation %s timed out after %.0fs", gen_data["gen_id"], elapsed
            )
            failure = {
                "success": False,
                "elapsed_s": round(elapsed, 2),
                "error": f"Timed out after {_GENERATION_TIMEOUT_S}s",
            }
            if preflight_mode is None:
                failure["outcome"] = RunOutcome.FAILED.value
            return failure
        except Exception as e:
            elapsed = time.monotonic() - start
            logger.error("Generation %s error: %s", gen_data["gen_id"], e)
            failure = {
                "success": False,
                "elapsed_s": round(elapsed, 2),
                "error": str(e),
            }
            if preflight_mode is None:
                failure["outcome"] = RunOutcome.FAILED.value
            return failure

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
        """An absent roster is initial state; corrupt existing state fails closed."""
        try:
            with self._manifest_path.open() as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {"generations": []}
        except (ValueError, OSError) as error:
            raise GenerationManifestError("generation manifest is unreadable or malformed") from error
        self._validate_manifest(data)
        return data

    @staticmethod
    def _validate_manifest(data: dict) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("generations"), list):
            raise GenerationManifestError("generation manifest requires a generations list")
        optional_config = {'config_file', 'config_sha256'}
        required = {item.name for item in fields(GenerationInfo)} - optional_config
        seen = set()
        for row in data["generations"]:
            if not isinstance(row, dict) or not required.issubset(row):
                raise GenerationManifestError("generation manifest has an incomplete record")
            if any(not isinstance(row[key], str) for key in required - {"run_history"}):
                raise GenerationManifestError("generation manifest has invalid field types")
            identity = row["gen_id"]
            if not re.fullmatch(r"gen_[0-9]{3,}", identity) or identity in seen:
                raise GenerationManifestError("generation manifest has invalid or duplicate identities")
            if row["status"] not in {"active", "paused", "retired"}:
                raise GenerationManifestError("generation manifest has an invalid status")
            if not isinstance(row["run_history"], list) or any(
                not isinstance(entry, dict) for entry in row["run_history"]
            ):
                raise GenerationManifestError("generation manifest has an invalid run history")
            if optional_config & row.keys():
                from .generation_config import FROZEN_CONFIG_NAME

                if (type(row.get('config_file')) is not str
                        or row['config_file'] != str(Path(row['state_dir']) / FROZEN_CONFIG_NAME)
                        or type(row.get('config_sha256')) is not str
                        or not re.fullmatch(r'[0-9a-f]{64}', row['config_sha256'])):
                    raise GenerationManifestError('generation manifest has invalid configuration identity')
            seen.add(identity)

    def _save_manifest(self, data: dict) -> None:
        """Atomic write of manifest.json."""
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic(self._manifest_path, (json.dumps(data, indent=2, default=str) + "\n").encode("utf-8"))

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write_atomic(self, path: Path, body: bytes) -> None:
        """Flush payload and directory entry before dependent lifecycle effects."""
        fd, tmp = tempfile.mkstemp(
            dir=self._generations_dir,
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            self._sync_directory(path.parent)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
