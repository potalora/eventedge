"""Typed, secret-free generation configuration loader and validator.

Supports canonical ``GenerationConfig`` profiles (e.g.
``config/profiles/gen003-clean.json``) validated strictly through
``tradingagents.strategies.config.GenerationConfig`` as well as legacy
``{"execution": ..., "autoresearch": ...}`` overlay dicts.
"""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

from tradingagents.strategies.config import (
    ConfigValidationError,
    GenerationConfig,
    MAX_CONFIG_BYTES,
)
from tradingagents.strategies.orchestration._io import load_json_strict

CONFIG_FILE_ENV = "EVENTEDGE_GENERATION_CONFIG_FILE"
CONFIG_SHA_ENV = "EVENTEDGE_GENERATION_CONFIG_SHA256"
FROZEN_CONFIG_NAME = "generation-config.json"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY = re.compile(
    r"api.?key|token|password|secret|credential|authorization|cookie|private.?key",
    re.I,
)
_SECRET_VALUE = re.compile(
    r"^(?:sk-|Bearer\s)|-----BEGIN .*PRIVATE KEY-----",
    re.I,
)


def _invalid() -> None:
    raise ValueError("generation_config_invalid") from None


def _validate_secret_free(value: Any, depth: int = 0) -> None:
    if depth > 64:
        _invalid()
    if type(value) is dict:
        for key, item in value.items():
            if (
                key != "chatgpt_plan_auth_path"
                and _SECRET_KEY.search(key)
                and item not in (None, "")
            ):
                _invalid()
            _validate_secret_free(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _validate_secret_free(item, depth + 1)
    elif type(value) is str and _SECRET_VALUE.search(value):
        _invalid()


def is_typed_generation_payload(payload: Mapping[str, Any]) -> bool:
    """Return True if ``payload`` uses the canonical GenerationConfig top-level schema."""
    return isinstance(payload, Mapping) and (
        "enabled_strategies" in payload
        or (
            "autoresearch" not in payload
            and "execution" not in payload
            and bool(set(payload) & GenerationConfig._ALLOWED_KEYS)
        )
    )


def read_generation_config(
    path: str | Path, expected_sha256: str | None = None
) -> tuple[bytes, dict[str, Any], str]:
    """Read exact regular-file bytes once, rejecting symlinks and secret material."""
    try:
        target = Path(path).absolute()
        if any(part.is_symlink() for part in (target, *target.parents)):
            _invalid()
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_CONFIG_BYTES:
                _invalid()
            body = handle.read(MAX_CONFIG_BYTES + 1)
        if not 0 < len(body) <= MAX_CONFIG_BYTES:
            _invalid()
        digest = hashlib.sha256(body).hexdigest()
        if expected_sha256 is not None and (
            type(expected_sha256) is not str
            or not _SHA256_RE.fullmatch(expected_sha256)
            or digest != expected_sha256
        ):
            _invalid()
        overlay = load_json_strict(body.decode("utf-8"), max_bytes=MAX_CONFIG_BYTES)
        if type(overlay) is not dict:
            _invalid()
        _validate_secret_free(overlay)
        if "source_retirement" in overlay or "_source_retirement_profile_sha256" in overlay:
            _invalid()
        if is_typed_generation_payload(overlay):
            # Strict fail-closed validation against Wave 0 GenerationConfig contract.
            GenerationConfig.from_dict(overlay)
        else:
            ar = overlay.get("autoresearch", {})
            if (
                type(ar) is not dict
                or "state_dir" in ar
                or type(ar.get("paper_ledger", {})) is not dict
            ):
                _invalid()
            policy_id = ar.get("paper_ledger", {}).get("policy_id")
            if policy_id is not None and (type(policy_id) is not str or not policy_id.strip()):
                _invalid()
        return body, overlay, digest
    except (OSError, ValueError, TypeError, RecursionError, ConfigValidationError):
        _invalid()
        raise  # pragma: no cover


def _merge(target: dict[str, Any], overlay: Mapping[str, Any]) -> None:
    for key, value in overlay.items():
        if type(value) is dict and type(target.get(key)) is dict:
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def apply_generation_config(
    defaults: Mapping[str, Any], path: str | Path, expected_sha256: str
) -> dict[str, Any]:
    """Validate and apply a frozen profile, binding its exact ledger policy_id."""
    _, overlay, digest = read_generation_config(path, expected_sha256)
    if is_typed_generation_payload(overlay):
        typed = GenerationConfig.from_dict(overlay)
        runtime_overlay = typed.to_runtime_dict()
        if defaults.get("autoresearch", {}).get("state_dir"):
            runtime_overlay.get("autoresearch", {}).pop("state_dir", None)
        result = copy.deepcopy(dict(defaults))
        _merge(result, runtime_overlay)
        ar_typed = result.setdefault("autoresearch", {})
        ar_typed.setdefault("decision_clock_policy", "prospective_next_open_v1")
        ar_typed.setdefault("research_resilience_policy", "dependency_isolation_v1")
        ar_typed.setdefault("stabilization_profile_policy", "gen003_clean_v1")
        if "govt_contracts" in typed.enabled_strategies and not (
            ar_typed.get("disabled_strategies") or {}
        ).get("govt_contracts"):
            ar_typed.setdefault("award_attribution_policy", "verified_listed_targets_v1")
        result["_generation_config"] = typed
    else:
        result = copy.deepcopy(dict(defaults))
        _merge(result, overlay)
    ar = result.setdefault("autoresearch", {})
    ledger = ar.setdefault("paper_ledger", {})
    configured = ledger.get("policy_id")
    ledger["policy_id"] = (
        ((configured + "-") if configured else "") + "generation-config-" + digest
    )
    return result
