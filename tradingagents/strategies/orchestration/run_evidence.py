"""Durable operational evidence for generation run attempts."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_EVIDENCE_FIELDS = (
    "schema_version",
    "generation_id",
    "generation_commit",
    "requested_session",
    "action",
    "preflight_mode",
    "started_at",
    "finished_at",
    "process_return_code",
    "process_status",
    "stdout",
    "stderr",
    "result",
)
_CREDENTIAL_NAME_PATTERN = (
    r"(?:[a-z0-9]+[_-])*(?:api[_-]?key|apikey|access[_-]?token|"
    r"auth[_-]?token|refresh[_-]?token|token|secret(?:[_-]key)?|"
    r"password|passwd|credentials?|authorization)(?:[_-][a-z0-9]+)*"
)
_SENSITIVE_NAME_RE = re.compile(
    r"(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|"
    r"password|passwd|credential|authorization)",
    re.IGNORECASE,
)
_TOKEN_USAGE_NAMES = frozenset({"prompt_tokens", "completion_tokens", "token_count"})
# A worker may die halfway through a quoted value. Treat the remainder of that
# line as sensitive, including a final incomplete escape.
_QUOTED_VALUE_PATTERN = (
    r"""(?:"(?:\\[^\r\n]|[^"\\\r\n])*(?:"|\\?(?=\r?$))"""
    r"|'(?:\\[^\r\n]|[^'\\\r\n])*(?:'|\\?(?=\r?$)))"
)
_AUTH_VALUE_RE = re.compile(
    r"(?P<prefix>[\"']?\b(?:authorization|proxy-authorization)\b[\"']?"
    r"\s*[:=]\s*)(?P<value>" + _QUOTED_VALUE_PATTERN + r"|[^\r\n]+)",
    re.IGNORECASE | re.MULTILINE,
)
_NAMED_VALUE_RE = re.compile(
    r"(?P<prefix>(?<![a-z0-9_-])[\"']?(?P<name>" + _CREDENTIAL_NAME_PATTERN + r")[\"']?"
    r"\s*[:=]\s*)"
    r"(?P<value>" + _QUOTED_VALUE_PATTERN + r"|[^\s&,;}\]\"']+)",
    re.IGNORECASE | re.MULTILINE,
)
_SCHEME_VALUE_RE = re.compile(
    r"\b(?P<scheme>bearer|basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE
)
_URL_USERINFO_RE = re.compile(
    r"(?P<prefix>\b[a-z][a-z0-9+.-]*://)[^/@\s]+@", re.IGNORECASE
)


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _credential_values() -> tuple[str, ...]:
    raw_values = {
        value
        for name, value in os.environ.items()
        if len(value) >= 4 and _SENSITIVE_NAME_RE.search(name)
    }
    # Tracebacks and provider errors can serialize the same credential several
    # ways. Match the escaped contents as well as the original value.
    values = {
        representation
        for value in raw_values
        for representation in (
            value,
            repr(value)[1:-1],
            json.dumps(value)[1:-1],
            json.dumps(value, ensure_ascii=False)[1:-1],
        )
    }
    return tuple(sorted(values, key=len, reverse=True))


def _sanitize_text(
    value: str | bytes | None, credential_values: tuple[str, ...]
) -> str:
    sanitized = _text(value)
    for secret in credential_values:
        sanitized = sanitized.replace(secret, "<redacted>")

    def replace_value(match: re.Match[str]) -> str:
        if match.groupdict().get("name", "").lower() in _TOKEN_USAGE_NAMES:
            return match.group(0)
        value = match.group("value")
        quote = value[0] if value[:1] in {'"', "'"} else ""
        return f"{match.group('prefix')}{quote}<redacted>{quote}"

    sanitized = _AUTH_VALUE_RE.sub(replace_value, sanitized)
    sanitized = _NAMED_VALUE_RE.sub(replace_value, sanitized)
    sanitized = _SCHEME_VALUE_RE.sub(r"\g<scheme> <redacted>", sanitized)
    return _URL_USERINFO_RE.sub(r"\g<prefix><redacted>@", sanitized)


def sanitize_evidence_text(value: str | bytes | None) -> str:
    """Redact credential material from an operator-facing diagnostic string."""
    return _sanitize_text(value, _credential_values())


def _sanitize(value: Any, credential_values: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {
            _sanitize_text(str(key), credential_values): (
                "<redacted>"
                if _SENSITIVE_NAME_RE.search(str(key))
                and str(key).lower() not in _TOKEN_USAGE_NAMES
                else _sanitize(item, credential_values)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, credential_values) for item in value]
    if isinstance(value, (str, bytes)) or value is None:
        return _sanitize_text(value, credential_values) if value is not None else None
    return value


def persist_run_evidence(repo_root: str | Path, evidence: dict) -> Path:
    """Persist one immutable attempt artifact."""
    attempts_dir = Path(repo_root) / "data" / "logs" / "run_attempts"
    attempts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    credential_values = _credential_values()
    payload = {
        field: _sanitize(evidence[field], credential_values)
        for field in _EVIDENCE_FIELDS
    }
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = attempts_dir / f"{timestamp}_{uuid.uuid4().hex}.json"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=attempts_dir, prefix=".attempt-", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary_path, path)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError as error:
            # Once linked, the archive is complete. A leftover temporary alias
            # must not mask that success or an earlier publication failure.
            logger.warning(
                "Failed to remove temporary attempt evidence (%s)",
                type(error).__name__,
            )
    return path
