"""Ephemeral exact-request reuse, published only after candidate validation.

The horizon owner creates one scope. Committee and standalone analyzer calls
have no candidate transaction and cannot use its entries. Nothing is persisted.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import time
from threading import Lock
from pathlib import Path

from .runtime_deadline import current_model_deadline, model_timeout


@dataclass(repr=False)
class _Response:
    text: str
    provenance: dict


@dataclass(repr=False)
class _Memo:
    entries: dict[tuple, _Response] = field(default_factory=dict)
    # Exact private settings comparisons assign opaque, invocation-local IDs.
    # Credentials/headers/endpoints never enter serialized keys or provenance.
    client_settings: list[tuple] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock, repr=False)


@dataclass(repr=False)
class _CandidateRequest:
    memo: _Memo
    context: tuple
    key: tuple | None = None
    digest: str | None = None
    pending: _Response | None = None


_MEMO: ContextVar[_Memo | None] = ContextVar("candidate_response_memo", default=None)
_CANDIDATE: ContextVar[_CandidateRequest | None] = ContextVar("candidate_response_request", default=None)


@contextmanager
def candidate_response_memo():
    """A new memo for exactly one frozen-input horizon-screening invocation."""
    token = _MEMO.set(_Memo())
    candidate_token = _CANDIDATE.set(None)
    try:
        yield
    finally:
        _CANDIDATE.reset(candidate_token)
        _MEMO.reset(token)


def begin_candidate_response(strategy: str, analysis_type: str, discovery_id: str, optional: bool):
    memo = _MEMO.get()
    request = (_CandidateRequest(memo, (strategy, analysis_type, discovery_id, optional))
               if memo is not None and isinstance(discovery_id, str) and discovery_id else None)
    return _CANDIDATE.set(request)


def end_candidate_response(token):
    _CANDIDATE.reset(token)


def _client_namespace(memo: _Memo, client) -> int | None:
    from .chatgpt_plan_client import ChatGPTPlanClient
    if isinstance(client, ChatGPTPlanClient):
        from .plan_auth import PlanAuthError, read_profile_identity
        try:
            deadline = current_model_deadline()
            identity = read_profile_identity(Path(client.auth_path) if client.auth_path else None,
                deadline=min(time.monotonic() + 2, deadline if deadline is not None else float("inf")))
        except (PlanAuthError, OSError, ValueError):
            return None
        if identity != client.profile_identity:
            return None
        settings = ("chatgpt_plan", client.auth_path, client.profile_identity)
    else:
        return _native_client_namespace(memo, client)
    return _settings_namespace(memo, settings)


def _native_client_namespace(memo: _Memo, client) -> int | None:
    native_bases = {base.__module__.split(".")[0] for base in type(client).__mro__}
    provider = "openai" if "openai" in native_bases else "anthropic" if "anthropic" in native_bases else None
    if provider is None:
        return None
    headers = tuple(sorted((key, value) for key, value in client.default_headers.items() if isinstance(value, str)))
    settings = (provider, str(client.base_url), client.api_key, headers,
                client.organization if provider == "openai" else client.auth_token,
                client.project if provider == "openai" else None)
    if any(value is not None and not isinstance(value, str) for value in settings[1:3] + settings[4:]):
        return None
    return _settings_namespace(memo, settings)


def _settings_namespace(memo: _Memo, settings: tuple) -> int:
    with memo.lock:
        for index, accepted in enumerate(memo.client_settings):
            if accepted == settings:
                return index
        memo.client_settings.append(settings)
        return len(memo.client_settings) - 1


def reused_candidate_response(client, *, model: str, system: str, prompt: str,
                              max_tokens: int, temperature: float, effort: str, role: str):
    """Look up an exact request; unsupported client/settings conservatively miss."""
    request = _CANDIDATE.get()
    if request is None:
        return None
    request.key, request.digest, request.pending = None, None, None
    try:
        namespace = _client_namespace(request.memo, client)
        if namespace is None:
            return None
        from .llm_utils import uses_responses
        # Keep requested and effective limits, even when Responses raises the cap.
        from .chatgpt_plan_client import ChatGPTPlanClient
        effective_limit = None if isinstance(client, ChatGPTPlanClient) else max(16384, max_tokens) if uses_responses(model) else max_tokens
        key = ("validated-candidate-response-v1", namespace, request.context,
               model, role, effort, max_tokens, effective_limit, temperature, system, prompt)
        hash(key)  # Unsupported mutable request values cannot authorize reuse.
        digest = _request_digest(key)
        request.key = key
        request.digest = digest
        with request.memo.lock:
            accepted = request.memo.entries.get(key)
    except (AttributeError, TypeError, ValueError):
        return None
    if accepted is None:
        return None
    model_timeout()
    provenance = deepcopy(accepted.provenance)
    provenance["request_reuse"] = {"reused": True, "scope": "horizon_screening",
                                   "request_digest": request.digest}
    return accepted.text, provenance


def _request_digest(key: tuple) -> str:
    # The key contains an opaque configuration namespace, never its secrets.
    return hashlib.sha256(json.dumps(key, ensure_ascii=False, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


def retain_candidate_response(text: str, provenance: dict):
    """Hold a transport success privately until the caller validates its candidate."""
    request = _CANDIDATE.get()
    if request is not None and request.key is not None and isinstance(text, str) and text.strip():
        request.pending = _Response(text, deepcopy(provenance))


def commit_candidate_response():
    """Only call after full existing schema/entity validation; failures discard it."""
    # Validation itself can consume the remaining deadline, including on a hit
    # or when reuse is bypassed. No candidate fields may commit after it expires.
    model_timeout()
    request = _CANDIDATE.get()
    if request is None or request.key is None or request.pending is None:
        return None
    accepted = request.pending
    response = _Response(accepted.text, deepcopy(accepted.provenance))
    with request.memo.lock:
        model_timeout()
        request.memo.entries[request.key] = response
    request.pending = None
    return {"reused": False, "scope": "horizon_screening", "request_digest": request.digest}
