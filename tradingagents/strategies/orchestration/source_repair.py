"""Source-owned repair admission and bounded private evidence, never a job runner.

The trusted broker supplies the canonical repository/output roots. Request data
cannot select paths, commands, credentials, a clock, or publication destinations.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

from .decision_clock import latest_completed_session
from .operational_report import _publish, render_operational_report
from .runtime_lock import (
    canonical_runtime_lock_path,
    runtime_lock,
    _open_parent_directory,
)
from .source_incidents import (
    MAX_REPORT_BYTES,
    POLICY,
    _FIELDS,
    _PROVIDERS,
    _REASONS,
    _SAFE_ID,
    assess_source_incidents,
)
from .trading_calendar import is_session, next_session, previous_session

VERSION = "eventedge-source-repair-request-v1"
PROPOSAL_VERSION = "eventedge-source-repair-proposal-v1"
EVIDENCE_VERSION = "eventedge-source-repair-evidence-v1"
REQUEST_FIELDS = frozenset(
    {
        "version",
        "incident_id",
        "generation_id",
        "generation_commit",
        "session",
        "provider",
        "configuration",
        "first_failed_session",
        "reason_code",
        "http_status",
        "report_sha256",
    }
)
MAX_REQUEST_BYTES = 4096
MAX_FILES = 256
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
_GENERATION = re.compile(r"gen_[0-9]{3,}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ATTEMPT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,186}\.json")
REPAIR_REASONS = _REASONS - {"missing_source_evidence"}


class EvidenceQualificationError(ValueError):
    """Fixed missing-history qualification; never permission to weaken proof."""
    def __init__(self, reason_code):
        if reason_code not in {'missing_origin_evidence', 'missing_session_evidence',
                               'acquisition_session_mismatch'}:
            raise ValueError('unknown source repair evidence qualification')
        self.reason_code = reason_code
        super().__init__('Source repair evidence unavailable: ' + reason_code)


def _now(now=None):
    value = datetime.now(timezone.utc) if now is None else now
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("source repair requires an aware clock")
    return value.astimezone(timezone.utc)


def _json_bytes(value):
    return (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        + b"\n"
    )


def _decode(payload, limit):
    if type(payload) is not bytes or len(payload) > limit:
        raise ValueError("source repair payload exceeds bound")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("nonfinite JSON value")

    try:
        return json.loads(payload, object_pairs_hook=unique, parse_constant=constant)
    except (UnicodeError, RecursionError) as error:
        raise ValueError("invalid bounded JSON") from error


def _session(value):
    if type(value) is not str:
        raise ValueError("invalid XNYS session")
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value or not is_session(parsed):
            raise ValueError("invalid XNYS session")
        return parsed
    except OverflowError as error:
        raise ValueError("invalid XNYS session") from error


def _scope(generation, commit, session, now):
    if (
        type(generation) is not str
        or not _GENERATION.fullmatch(generation)
        or type(commit) is not str
        or not _COMMIT.fullmatch(commit)
    ):
        raise ValueError("invalid generation identity")
    if _session(session) != latest_completed_session(_now(now)):
        raise ValueError("source repair session is stale")


def _incident_id(request):
    identity = [
        request[key]
        for key in (
            "version",
            "generation_id",
            "generation_commit",
            "configuration",
            "provider",
            "first_failed_session",
        )
    ]
    return hashlib.sha256(
        json.dumps(identity, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def validate_repair_request(
    payload,
    *,
    expected_generation=None,
    expected_commit=None,
    expected_session=None,
    now=None,
):
    """Reject extra fields, injected destinations, stale sessions and auth repairs."""
    if type(payload) is dict:
        if len(_json_bytes(payload)) > MAX_REQUEST_BYTES:
            raise ValueError("source repair request exceeds bound")
        request = dict(payload)
    else:
        request = _decode(payload, MAX_REQUEST_BYTES)
    if (
        type(request) is not dict
        or set(request) != REQUEST_FIELDS
        or any(
            type(request[key]) is not str for key in REQUEST_FIELDS - {"http_status"}
        )
        or request["version"] != VERSION
    ):
        raise ValueError("invalid source repair request schema")
    _scope(
        request["generation_id"], request["generation_commit"], request["session"], now
    )
    if (
        request["provider"] not in _PROVIDERS
        or request["reason_code"] not in REPAIR_REASONS
        or type(request["configuration"]) is not str
        or not _SAFE_ID.fullmatch(request["configuration"])
        or _session(request["first_failed_session"]) > _session(request["session"])
        or type(request["incident_id"]) is not str
        or not _DIGEST.fullmatch(request["incident_id"])
        or request["incident_id"] != _incident_id(request)
        or type(request["report_sha256"]) is not str
        or not _DIGEST.fullmatch(request["report_sha256"])
        or request["http_status"] is not None
        and (
            type(request["http_status"]) is not int
            or not 100 <= request["http_status"] <= 599
            or request["http_status"] in {401, 403}
        )
    ):
        raise ValueError("invalid source repair request values")
    for key, expected in (
        ("generation_id", expected_generation),
        ("generation_commit", expected_commit),
        ("session", expected_session),
    ):
        if expected is not None and request[key] != expected:
            raise ValueError("source repair request identity mismatch")
    return request


def build_repair_requests(
    report_bytes,
    markdown_bytes,
    *,
    expected_generation,
    expected_commit,
    expected_session,
    now=None,
):
    """Project the current repair-required snapshot, independent of alert transitions.

    The report is an observation proposal, not authoritative repair evidence. The
    broker must call the evidence exporter before admitting an automatic attempt.
    """
    now = _now(now)
    _scope(expected_generation, expected_commit, expected_session, now)
    report = _decode(report_bytes, MAX_REPORT_BYTES)
    if type(report) is not dict:
        raise ValueError("invalid source repair report")
    try:
        canonical = (
            json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
        ).encode()
        if (
            canonical != report_bytes
            or type(markdown_bytes) is not bytes
            or len(markdown_bytes) > MAX_REPORT_BYTES
            or markdown_bytes != render_operational_report(report).encode()
            or (report["generation_id"], report["generation_commit"], report["session"])
            != (expected_generation, expected_commit, expected_session)
        ):
            raise ValueError("source repair report pair mismatch")
        summary, sources = report["source_incidents"], report["sources"]
        configuration = sources["configuration"]
        acquired = datetime.fromisoformat(sources["acquired_at"])
        from zoneinfo import ZoneInfo

        if (
            summary["policy"] != POLICY
            or summary["assessment_status"] != "available"
            or summary["history_status"] not in {"available", "missing", "invalid"}
            or type(configuration) is not str
            or not _SAFE_ID.fullmatch(configuration)
            or summary["configuration"] != configuration
            or acquired.utcoffset() is None
            or acquired > now
            or acquired.astimezone(ZoneInfo("America/New_York")).date()
            != _session(expected_session)
        ):
            raise ValueError("source repair assessment unavailable")
        rows = summary["open_incidents"]
        if type(rows) is not list or len(rows) > len(_PROVIDERS):
            raise ValueError("source repair incident bound")
        # Recompute the actual current failure; history can promote persistence,
        # but cannot invent a provider, failure, reason, status or observation.
        observed = {
            row["provider"]: row
            for row in assess_source_incidents(report)["open_incidents"]
        }
        result = {"requests": [], "requires_user": []}
        seen = set()
        digest = hashlib.sha256(report_bytes + b"\0" + markdown_bytes).hexdigest()
        for row in rows:
            if (
                type(row) is not dict
                or set(row) != _FIELDS
                or row["provider"] not in _PROVIDERS
                or row["provider"] in seen
                or row["status"] not in {"degraded", "repair_required"}
                or row["observation"] not in {"failed", "unknown"}
                or row["reason_code"] not in _REASONS
                or type(row["consecutive_failed_sessions"]) is not int
                or not 0 <= row["consecutive_failed_sessions"] <= 3
                or row["http_status"] is not None
                and (
                    type(row["http_status"]) is not int
                    or not 100 <= row["http_status"] <= 599
                )
            ):
                raise ValueError("invalid source repair incident")
            seen.add(row["provider"])
            if row["status"] != "repair_required":
                continue
            current = observed.get(row["provider"])
            if (
                row["observation"] != "failed"
                or row["reason_code"] not in REPAIR_REASONS
                or row["consecutive_failed_sessions"] < 1
                or row["last_failed_session"] != expected_session
                or _session(row["first_failed_session"]) > _session(expected_session)
                or current is None
                or current["observation"] != "failed"
                or any(
                    current[key] != row[key] for key in ("reason_code", "http_status")
                )
            ):
                raise ValueError("unknown or uncorroborated repair incident")
            request = {
                "version": VERSION,
                "generation_id": expected_generation,
                "generation_commit": expected_commit,
                "session": expected_session,
                "provider": row["provider"],
                "configuration": configuration,
                "first_failed_session": row["first_failed_session"],
                "reason_code": row["reason_code"],
                "http_status": row["http_status"],
                "report_sha256": digest,
            }
            request["incident_id"] = _incident_id(request)
            if row["http_status"] in {401, 403}:
                result["requires_user"].append(
                    {**request, "disposition": "requires_user"}
                )
            else:
                result["requests"].append(validate_repair_request(request, now=now))
        return result
    except (
        KeyError,
        TypeError,
        AttributeError,
        UnicodeError,
        RecursionError,
        OverflowError,
    ) as error:
        raise ValueError("invalid source repair report contract") from error


class _Capture:
    """No-follow descriptor traversal with before/after/final identity checks."""

    def __init__(self, root):
        self.root = Path(root).absolute()
        self.files = {}
        self.total = 0

    @staticmethod
    def _token(info):
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def read(self, relative, limit=MAX_FILE_BYTES):
        path = Path(relative)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {".", ".."} for part in path.parts)
        ):
            raise ValueError("invalid evidence path")
        # Start at / so even an ancestor of the canonical root cannot be a link.
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.root.parts[1:] + path.parts[:-1]:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = child
            leaf = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
            )
            try:
                before = os.fstat(leaf)
                if not stat.S_ISREG(before.st_mode) or before.st_size > min(
                    limit, MAX_FILE_BYTES
                ):
                    raise ValueError("unsafe or oversized evidence file")
                chunks, size = [], 0
                while True:
                    chunk = os.read(
                        leaf, min(1024 * 1024, min(limit, MAX_FILE_BYTES) + 1 - size)
                    )
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > min(limit, MAX_FILE_BYTES):
                        raise ValueError("evidence byte bound")
                    chunks.append(chunk)
                if self._token(before) != self._token(os.fstat(leaf)):
                    raise ValueError("evidence changed during capture")
                data = b"".join(chunks)
                current = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
                if self._token(before) != self._token(current):
                    raise ValueError("evidence replaced during capture")
            finally:
                os.close(leaf)
        finally:
            os.close(fd)
        key = path.as_posix()
        if key in self.files:
            if self.files[key] != (self._token(before), data):
                raise ValueError("evidence changed during capture")
        else:
            if len(self.files) >= MAX_FILES or self.total + len(data) > MAX_TOTAL_BYTES:
                raise ValueError("evidence bundle capacity exhausted")
            self.files[key] = (self._token(before), data)
            self.total += len(data)
        return data

    def verify(self):
        for path in tuple(self.files):
            self.read(path)


def _git(root, *arguments):
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_OPTIONAL_LOCKS="0"
    )
    try:
        return subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(root),
                *arguments,
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("generation Git identity unavailable") from error


def _metadata(capture, generation):
    document = _decode(capture.read("data/generations/manifest.json"), MAX_FILE_BYTES)
    try:
        rows = document["generations"]
        if type(rows) is not list:
            raise ValueError("invalid generation manifest")
        matches = [row for row in rows if row["gen_id"] == generation]
        if len(matches) != 1:
            raise ValueError("generation is not unique")
        row = matches[0]
        if row["status"] != "active" or not _COMMIT.fullmatch(row["git_commit"]):
            raise ValueError("generation is not active")
        for key, relative in (
            ("state_dir", Path("data/generations") / generation),
            ("worktree_path", Path(".worktrees") / generation),
        ):
            declared = Path(row[key])
            if not declared.is_absolute():
                declared = capture.root / declared
            if declared != capture.root / relative:
                raise ValueError("noncanonical generation path")
        return row
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("invalid generation manifest identity") from error


def _head(capture, generation):
    path = Path(".worktrees") / generation
    gitfile = capture.read(path / ".git", 4096).decode()
    if not gitfile.startswith("gitdir: ") or not gitfile.endswith("\n"):
        raise ValueError("invalid generation worktree registration")
    gitdir = Path(gitfile[8:].strip())
    if not gitdir.is_absolute():
        gitdir = capture.root / path / gitdir
    expected_parent = capture.root / ".git/worktrees"
    if gitdir.parent != expected_parent or not _SAFE_ID.fullmatch(gitdir.name):
        raise ValueError("noncanonical generation Git directory")
    capture.read(gitdir.relative_to(capture.root) / "HEAD", 4096)
    capture.read(gitdir.relative_to(capture.root) / "commondir", 4096)
    if _git(
        capture.root / path, "rev-parse", "--path-format=absolute", "--git-common-dir"
    ) != str(capture.root / ".git"):
        raise ValueError("generation Git common directory mismatch")
    return _git(capture.root / path, "rev-parse", "--verify", "HEAD")


def _report_pair(capture, generation, session):
    stem = Path("docs/reports") / f"{session}-{generation}-operational-report"
    return (
        capture.read(stem.with_suffix(".json"), MAX_REPORT_BYTES),
        capture.read(stem.with_suffix(".md"), MAX_REPORT_BYTES),
    )


@contextmanager
def _readonly_runtime_lock(lock):
    """Reuse existing lock validation with an O_RDONLY descriptor; never create."""
    parent = _open_parent_directory(lock, create=False)
    descriptor = None
    try:
        descriptor = os.open(
            lock.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
        )
        with runtime_lock(
            lock, exclusive=False, inherited_fd=descriptor, inherited_exclusive=False
        ):
            yield
    finally:
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        os.close(parent)


def current_repair_requests(repo_root, *, now=None):
    """Read canonical active reports under the application shared runtime lock."""
    now = _now(now)
    capture = _Capture(repo_root)
    lock = canonical_runtime_lock_path(capture.root)
    if lock.parent.parent.parent != capture.root:
        raise ValueError("repair helper requires canonical checkout")
    with _readonly_runtime_lock(lock):
        document = _decode(
            capture.read("data/generations/manifest.json"), MAX_FILE_BYTES
        )
        generations = [
            row["gen_id"]
            for row in document["generations"]
            if row["status"] == "active"
        ]
        if not generations or len(generations) != len(set(generations)):
            raise ValueError("invalid active generation scope")
        result = {"requests": [], "requires_user": []}
        session = latest_completed_session(now).isoformat()
        for generation in sorted(generations):
            if type(generation) is not str or not _GENERATION.fullmatch(generation):
                raise ValueError("invalid active generation")
            metadata = _metadata(capture, generation)
            if _head(capture, generation) != metadata["git_commit"]:
                raise ValueError("active generation HEAD mismatch")
            projected = build_repair_requests(
                *_report_pair(capture, generation, session),
                expected_generation=generation,
                expected_commit=metadata["git_commit"],
                expected_session=session,
                now=now,
            )
            for key in result:
                result[key].extend(projected[key])
        capture.verify()
        return result


def export_repair_evidence(repo_root, request, output_dir, *, now=None):
    """Corroborate proposals against source-owned evidence and export immutably.

    No database is exported. Source bodies remain private untrusted input. Source
    history is replayed to prove persistence/recovery and the first-failure ID;
    missing history or exhausted limits require operator action, never guessing.
    """
    clock_override = now
    now = _now(now)
    request = validate_repair_request(request, now=now)
    capture = _Capture(repo_root)
    output = Path(output_dir).absolute()
    if (
        output.exists()
        or output.is_symlink()
        or capture.root == output
        or capture.root in output.parents
    ):
        raise ValueError("evidence output must be new and outside canonical repository")
    # Validate every output ancestor without following links.
    for ancestor in (output.parent, *output.parent.parents):
        if ancestor.is_symlink() or not ancestor.is_dir():
            raise ValueError("unsafe evidence output parent")
    lock = canonical_runtime_lock_path(capture.root)
    if lock.parent.parent.parent != capture.root:
        raise ValueError("repair exporter requires canonical checkout")
    generation, commit, session = (
        request[key] for key in ("generation_id", "generation_commit", "session")
    )
    stage = None
    with _readonly_runtime_lock(lock):
        try:
            metadata = _metadata(capture, generation)
            if metadata["git_commit"] != commit or _head(capture, generation) != commit:
                raise ValueError("active generation identity mismatch")
            report_bytes, markdown_bytes = _report_pair(capture, generation, session)
            projected = build_repair_requests(
                report_bytes,
                markdown_bytes,
                expected_generation=generation,
                expected_commit=commit,
                expected_session=session,
                now=now,
            )
            if request not in projected["requests"]:
                raise ValueError("repair request does not match current report")
            report = _decode(report_bytes, MAX_REPORT_BYTES)
            stage = Path(tempfile.mkdtemp(prefix=".source-repair-", dir=output.parent))
            selected = {
                "data/generations/manifest.json",
                f"docs/reports/{session}-{generation}-operational-report.json",
                f"docs/reports/{session}-{generation}-operational-report.md",
            }
            from .source_inputs import (
                CONTRACT_VERSION,
                FULL_FILING_MAX_NODES,
                SourceInputStore,
                configuration_fingerprint,
            )
            from .operational_report import _sources

            origin_boundary = previous_session(
                _session(request["first_failed_session"])
            )
            day, previous, source_digest = origin_boundary, None, None
            while day <= _session(session):
                relative = (
                    Path("data/generations")
                    / generation
                    / "source_inputs"
                    / (
                        configuration_fingerprint(
                            {"generation": generation, "session": day.isoformat()}
                        )
                        + ".json"
                    )
                )
                try:
                    raw = capture.read(relative)
                except FileNotFoundError:
                    raise EvidenceQualificationError('missing_origin_evidence'
                        if day == origin_boundary else 'missing_session_evidence') from None
                envelope = SourceInputStore.decode(
                    raw.decode(),
                    max_bytes=MAX_FILE_BYTES,
                    max_nodes=FULL_FILING_MAX_NODES,
                )
                identity = {
                    "generation": generation,
                    "commit": commit,
                    "session": day.isoformat(),
                    "configuration": (
                        envelope.get("identity", {}).get("configuration")
                        if day == origin_boundary
                        else request["configuration"]
                    ),
                }
                acquired = envelope.get("acquired_at")
                if (
                    set(envelope)
                    != {"version", "identity", "acquired_at", "payload", "digest"}
                    or envelope["version"] != CONTRACT_VERSION
                    or envelope["identity"] != identity
                    or type(identity["configuration"]) is not str
                    or not _SAFE_ID.fullmatch(identity["configuration"])
                    or not isinstance(acquired, datetime)
                    or acquired.utcoffset() is None
                    or acquired > now
                    or type(envelope["payload"]) is not dict
                    or envelope["digest"]
                    != hashlib.sha256(
                        SourceInputStore.encode(
                            envelope["payload"],
                            max_bytes=MAX_FILE_BYTES,
                            max_nodes=FULL_FILING_MAX_NODES,
                        ).encode()
                    ).hexdigest()
                ):
                    raise ValueError("source evidence identity or digest mismatch")
                from zoneinfo import ZoneInfo

                if acquired.astimezone(ZoneInfo("America/New_York")).date() != day:
                    raise EvidenceQualificationError('acquisition_session_mismatch')
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                selected.add(relative.as_posix())
                errors = []
                sources = _sources(
                    stage / "data/generations" / generation,
                    generation,
                    day.isoformat(),
                    commit,
                    lambda code, scope=None: errors.append(code),
                )
                if (
                    "accepted_sources_invalid" in errors
                    or sources.get("configuration") != identity["configuration"]
                ):
                    raise ValueError("source projection unavailable")
                reference = {
                    "generation_id": generation,
                    "generation_commit": commit,
                    "session": day.isoformat(),
                    "sources": sources,
                    "source_health_failures": [],
                    "evidence_complete": True,
                }
                reference["source_incidents"] = assess_source_incidents(
                    reference, previous
                )
                if (
                    day == origin_boundary
                    and identity["configuration"] == request["configuration"]
                ):
                    origin_incidents = reference["source_incidents"]["open_incidents"]
                    if sources["observations"].get(request["provider"]) not in {
                        "success",
                        "success_empty",
                        "complete",
                    } or any(
                        row["provider"] == request["provider"]
                        for row in origin_incidents
                    ):
                        raise ValueError(
                            "source evidence does not prove incident origin"
                        )
                previous = reference
                if day.isoformat() == session:
                    source_digest = envelope["digest"]
                    if sources["acquired_at"] != report["sources"]["acquired_at"]:
                        raise ValueError("source/report acquisition mismatch")
                day = next_session(day)
            corroborated = [
                row
                for row in previous["source_incidents"]["open_incidents"]
                if row["provider"] == request["provider"]
            ]
            if (
                len(corroborated) != 1
                or corroborated[0]["status"] != "repair_required"
                or corroborated[0]["observation"] != "failed"
                or any(
                    corroborated[0][key] != request[key]
                    for key in ("first_failed_session", "reason_code", "http_status")
                )
            ):
                raise ValueError(
                    "repair snapshot not corroborated by accepted source history"
                )
            attempts = []
            if (
                type(report["attempts"]) is not list
                or len(report["attempts"]) > MAX_FILES
            ):
                raise ValueError("attempt evidence bound")
            for item in report["attempts"]:
                name = item["attempt_id"]
                if (
                    type(name) is not str
                    or not _ATTEMPT.fullmatch(name)
                    or name in attempts
                ):
                    raise ValueError("invalid attempt identity")
                relative = Path("data/logs/run_attempts") / name
                if item.get("evidence_path") not in (
                    None,
                    str(capture.root / relative),
                    relative.as_posix(),
                ):
                    raise ValueError("noncanonical attempt evidence path")
                value = _decode(capture.read(relative), MAX_FILE_BYTES)
                if (
                    value["generation_id"],
                    value["generation_commit"],
                    value["requested_session"],
                ) != (generation, commit, session):
                    raise ValueError("attempt evidence identity mismatch")
                attempts.append(name)
                selected.add(relative.as_posix())
            if len(selected) + 1 > MAX_FILES:
                raise ValueError("evidence manifest capacity exhausted")
            files = []
            for relative in sorted(selected):
                raw = capture.files[relative][1]
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                files.append(
                    {
                        "path": relative,
                        "size_bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
            manifest = {
                "version": EVIDENCE_VERSION,
                "incident_id": request["incident_id"],
                "generation_id": generation,
                "generation_commit": commit,
                "session": session,
                "configuration": request["configuration"],
                "provider": request["provider"],
                "first_failed_session": request["first_failed_session"],
                "report_sha256": request["report_sha256"],
                "source_digest": source_digest,
                "attempt_ids": attempts,
                "acquired_at": now.isoformat(),
                "files": files,
            }
            encoded = _json_bytes(manifest)
            if sum(row["size_bytes"] for row in files) + len(encoded) > MAX_TOTAL_BYTES:
                raise ValueError("evidence total byte bound")
            (stage / "evidence-manifest.json").write_bytes(encoded)
            capture.verify()
            if _head(capture, generation) != commit:
                raise ValueError("active generation changed before publication")
            validate_repair_request(request, now=_now(clock_override))
            for path in stage.rglob("*"):
                if path.is_file():
                    path.chmod(0o400)
            os.rename(stage, output)
            stage = None
            for path in sorted(output.rglob("*"), reverse=True):
                if path.is_dir():
                    path.chmod(0o500)
            output.chmod(0o500)
            return manifest
        finally:
            if stage is not None:
                shutil.rmtree(stage)


def publish_repair_proposal(report, paths, *, now=None):
    """Publish an adjacent atomic proposal; stale reports keep publishing normally."""
    raw = Path(paths["json"]).read_bytes()
    markdown = Path(paths["markdown"]).read_bytes()
    result = {
        "version": PROPOSAL_VERSION,
        "generation_id": report["generation_id"],
        "generation_commit": report["generation_commit"],
        "session": report["session"],
        "report_sha256": hashlib.sha256(raw + b"\0" + markdown).hexdigest(),
        "status": "unavailable",
        "requests": [],
        "requires_user": [],
    }
    try:
        if _session(report["session"]) != latest_completed_session(_now(now)):
            result["status"] = "stale"
        else:
            result.update(
                build_repair_requests(
                    raw,
                    markdown,
                    expected_generation=report["generation_id"],
                    expected_commit=report["generation_commit"],
                    expected_session=report["session"],
                    now=now,
                )
            )
            result["status"] = "eligible"
    except (ValueError, TypeError, KeyError):
        pass
    destination = Path(paths["json"]).with_name(
        f"{report['session']}-{report['generation_id']}-source-repair.json"
    )
    _publish(destination, _json_bytes(result).decode())
    return destination
