"""Shared helpers for model-specific autoresearch LLM behavior."""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any

from .runtime_deadline import (ModelDeadlineExceeded, ModelTransportError, bounded_transport, model_timeout)

SOL_MODEL = "gpt-6.2-sol"
LUNA_MODEL = "gpt-6-luna"
RESPONSES_MODELS = frozenset({"gpt-6-luna", "gpt-6.1-sol", "gpt-6.2-sol"})

GEMINI_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_DEFAULT_MAX_OUTPUT_TOKENS = 65535
GEMINI_THESIS_MODEL = "gemini-4-neon"
GEMINI_BOUNDED_MODEL = "gemini-4-hydrogen"
GEMINI4_XHIGH_MODELS = frozenset({"gemini-4-neon", "gemini-4-hydrogen"})
_GEMINI_MODEL_RE = re.compile(r"^gemini-[A-Za-z0-9._-]+$")
_GEMINI_BASE_THINKING_LEVELS = frozenset({"low", "medium", "high"})
_GEMINI_XHIGH_THINKING_LEVELS = frozenset({"low", "medium", "high", "xhigh"})
_GEMINI_REFUSED_FINISH_REASONS = frozenset({
    "SAFETY",
    "RECITATION",
    "PROHIBITED_CONTENT",
    "BLOCKLIST",
    "SPII",
})
_GEMINI_INCOMPLETE_FINISH_REASONS = frozenset({"MAX_TOKENS", "CONTINUATION"})


def uses_responses(model: str) -> bool:
    return model in RESPONSES_MODELS


def is_gemini_model(model: str) -> bool:
    return isinstance(model, str) and model.startswith("gemini-")


def model_provider(model: str) -> str:
    """Fail-closed provider mapping from model identifier."""
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"Unsupported model provider for {model!r}")
    if is_gemini_model(model):
        return "gemini"
    if uses_responses(model) or model.startswith(("gpt-", "o1-", "o3-", "o4-")) or model in {"o1", "o3", "o4"}:
        return "openai"
    if model.startswith("claude-"):
        return "anthropic"
    raise ValueError(f"Unsupported model provider for {model!r}")


def _supports_gemini_xhigh(model: str) -> bool:
    return model in GEMINI4_XHIGH_MODELS or model.startswith(("gemini-4-neon-", "gemini-4-hydrogen-"))


def _gemini_thinking_level(model: str, effort: Any) -> str:
    """Validate and normalize Gemini 4 thinkingConfig.thinkingLevel fail-closed."""
    if not isinstance(effort, str):
        raise ValueError(f"Invalid Gemini reasoning effort {effort!r}")
    level = effort.strip().lower()
    if not level or level in {"none", "minimal"}:
        raise ValueError(
            f"Unsupported Gemini thinkingLevel {effort!r}; thinking cannot be disabled on Gemini 4"
        )
    xhigh_supported = _supports_gemini_xhigh(model)
    if level == "max":
        if xhigh_supported:
            return "xhigh"
        raise ValueError(f"Unsupported Gemini thinkingLevel {effort!r} for model {model!r}")
    allowed = _GEMINI_XHIGH_THINKING_LEVELS if xhigh_supported else _GEMINI_BASE_THINKING_LEVELS
    if level not in allowed:
        raise ValueError(f"Unsupported Gemini thinkingLevel {effort!r} for model {model!r}")
    return level


def _gemini_http_error_code(status_code: int) -> str:
    if status_code == 400:
        return "invalid_request"
    if status_code in (401, 403):
        return "auth_denied"
    if status_code == 404:
        return "model_unavailable"
    if status_code == 429:
        return "rate_limit_exceeded"
    if 500 <= status_code <= 599:
        return "provider_error"
    return "provider_error"


class GeminiTransportError(RuntimeError):
    """Sanitized Gemini transport/response failure carrying only fixed enum codes."""

    def __init__(self, code: str, *, status_code: int | None = None, response: Any = None) -> None:
        self.code = code
        self.status_code = status_code
        self.response = response
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class GeminiClient:
    """Leak-safe descriptor for Google AI Studio Gemini REST transport."""

    api_key: str
    base_url: str = GEMINI_DEFAULT_BASE_URL

    def __post_init__(self) -> None:
        if type(self.api_key) is not str or not self.api_key.strip():
            raise ValueError("Missing GEMINI_API_KEY or GOOGLE_API_KEY")
        if type(self.base_url) is not str or not self.base_url.strip():
            raise ValueError("Invalid GEMINI_BASE_URL")
        object.__setattr__(self, "api_key", self.api_key.strip())
        object.__setattr__(self, "base_url", self.base_url.strip().rstrip("/"))

    @classmethod
    def from_env(cls) -> GeminiClient:
        api_key = (
            os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
        )
        if not api_key:
            raise ValueError("Missing GEMINI_API_KEY or GOOGLE_API_KEY")
        base_url = os.environ.get("GEMINI_BASE_URL", "").strip() or GEMINI_DEFAULT_BASE_URL
        return cls(api_key=api_key, base_url=base_url)

    def __repr__(self) -> str:
        return "GeminiClient(<redacted>)"

    def close(self) -> None:
        """No persistent connections are held in the parent process."""


_VALID_EFFORTS = frozenset({"low", "medium", "high", "max"})
_ANTHROPIC_EFFORT_MODELS = frozenset({"claude-sonnet-4-6", "claude-opus-4-6"})


def anthropic_request_options(
    *, model: str, temperature: float, effort: str
) -> dict[str, Any]:
    """Return request options that are valid for the selected Claude model."""
    if model not in _ANTHROPIC_EFFORT_MODELS:
        return {"temperature": temperature}
    normalized_effort = str(effort or "medium").lower()
    if normalized_effort not in _VALID_EFFORTS:
        raise ValueError(
            f"Invalid Claude effort {effort!r}; expected one of {sorted(_VALID_EFFORTS)}"
        )
    return {"output_config": {"effort": normalized_effort}}


def anthropic_response_text(response: Any) -> str:
    """Extract text even when adaptive-thinking blocks precede it."""
    text_parts = [
        str(getattr(block, "text", ""))
        for block in getattr(response, "content", [])
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    ]
    text = "".join(text_parts).strip()
    if not text:
        raise RuntimeError("Anthropic response contained no text block")
    return text


def _call_gemini_direct(
    client: GeminiClient,
    *,
    model: str,
    system: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    effort: str,
    provenance: dict | None = None,
    timeout: float = 120.0,
    http_client: Any = None,
) -> str:
    """Call Google AI Studio :generateContent directly over httpx.Client.

    Omits deprecated Gemini 4 sampling parameters (temperature/topP/topK), enforces
    thinkingConfig.thinkingLevel and maxOutputTokens >= 65535, filters thought parts,
    and fails closed on blocked/truncated/empty responses.
    """
    _ = temperature  # Retained in logical request signature/receipts; omitted on Gemini 4 wire.
    if not is_gemini_model(model) or _GEMINI_MODEL_RE.fullmatch(model) is None:
        raise ValueError(f"Invalid Gemini model {model!r}")
    thinking_level = _gemini_thinking_level(model, effort)
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ValueError("Invalid max_tokens")
    effective_max_tokens = max(GEMINI_DEFAULT_MAX_OUTPUT_TOKENS, max_tokens)

    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": effective_max_tokens,
            "thinkingConfig": {"thinkingLevel": thinking_level},
        },
    }
    if system:
        body["systemInstruction"] = {"role": "system", "parts": [{"text": system}]}

    url = f"{client.base_url}/models/{model}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": client.api_key,
    }

    if http_client is None:
        import httpx

        httpx_timeout = httpx.Timeout(timeout, connect=min(10.0, timeout))
        with httpx.Client(timeout=httpx_timeout, follow_redirects=False, trust_env=False) as session:
            resp = session.post(url, headers=headers, json=body)
    else:
        resp = http_client.post(url, headers=headers, json=body)

    if resp.status_code != 200:
        raise GeminiTransportError(
            _gemini_http_error_code(resp.status_code),
            status_code=resp.status_code,
            response=resp,
        )

    try:
        data = resp.json()
    except ValueError:
        raise GeminiTransportError("stream_invalid", status_code=200, response=resp) from None
    if not isinstance(data, dict):
        raise GeminiTransportError("stream_invalid", status_code=200, response=resp)

    raw_version = data.get("modelVersion")
    model_version = raw_version.strip() if isinstance(raw_version, str) and raw_version.strip() else None
    pinned = model_version is not None and model_version != model
    raw_response_id = data.get("responseId")
    response_id = raw_response_id.strip() if isinstance(raw_response_id, str) and raw_response_id.strip() else None
    if provenance is not None:
        provenance.update(
            configured_model=model,
            returned_model=model_version or model,
            returned_revision=model_version if pinned else None,
            identity_status="pinned" if pinned else "unpinned",
            response_id=response_id,
            reasoning_effort=effort,
            billing_mode="api",
            effective_output_limit=effective_max_tokens,
        )

    prompt_feedback = data.get("promptFeedback")
    if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
        raise GeminiTransportError("response_refused", status_code=200, response=resp)

    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise GeminiTransportError("response_empty", status_code=200, response=resp)

    candidate = candidates[0]
    finish_reason = candidate.get("finishReason")
    if finish_reason != "STOP":
        if finish_reason in _GEMINI_REFUSED_FINISH_REASONS:
            raise GeminiTransportError("response_refused", status_code=200, response=resp)
        if finish_reason in _GEMINI_INCOMPLETE_FINISH_REASONS:
            raise GeminiTransportError("stream_incomplete", status_code=200, response=resp)
        raise GeminiTransportError("provider_error", status_code=200, response=resp)

    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise GeminiTransportError("response_empty", status_code=200, response=resp)

    visible_parts = [
        part["text"]
        for part in parts
        if isinstance(part, dict)
        and part.get("thought") is not True
        and isinstance(part.get("text"), str)
    ]
    text = "".join(visible_parts).strip()
    if not text:
        raise GeminiTransportError("response_empty", status_code=200, response=resp)
    return text


def _call_model_direct(
    client: Any, *, model: str, system: str, prompt: str,
    max_tokens: int, temperature: float, effort: str, provenance: dict | None = None,
) -> str:
    """Route Gemini 4, OpenAI Responses, and Anthropic Messages calls fail-closed.

    Reasoning models include hidden thoughts/reasoning in output budgets. Reserve
    room beyond the short visible JSON budget and reject incomplete output before
    JSON repair.
    """
    provider = model_provider(model)
    if provider == "gemini":
        if not isinstance(client, GeminiClient):
            raise ValueError("Gemini model requires GeminiClient")
        return _call_gemini_direct(
            client,
            model=model,
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            effort=effort,
            provenance=provenance,
        )
    if uses_responses(model):
        if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("Invalid Responses reasoning effort")
        response = client.responses.create(
            model=model, instructions=system, input=prompt,
            reasoning={"effort": effort},
            max_output_tokens=max(16384, max_tokens), store=False,
        )
        _record_provenance(provenance, response, model, effort)
        if getattr(response, "status", None) != "completed":
            raise RuntimeError("OpenAI analysis response did not complete")
        for item in getattr(response, "output", []):
            for block in getattr(item, "content", []) or []:
                if getattr(block, "type", None) == "refusal":
                    raise RuntimeError("OpenAI analysis response was refused")
        text = getattr(response, "output_text", None)
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("OpenAI analysis response contained no text")
        return text.strip()
    if provider != "anthropic":
        raise ValueError(f"Unsupported direct model {model!r}")
    response = client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": prompt}],
        **anthropic_request_options(model=model, temperature=temperature, effort=effort),
    )
    _record_provenance(provenance, response, model, effort)
    return anthropic_response_text(response)


def _record_provenance(destination, response, model, effort):
    if destination is None:
        return
    # Only public returned fields; a mutable alias or response ID is not a revision.
    def field(name):
        value = getattr(response, name, None)
        return value if isinstance(value, str) and value else None
    revision = field("model_revision")
    destination.update(configured_model=model, returned_model=field("model"),
                       returned_revision=revision, identity_status="pinned" if revision else "unpinned",
                       response_id=field("id"), reasoning_effort=effort)


def call_analysis_model(
    client: Any, *, model: str, system: str, prompt: str,
    max_tokens: int, temperature: float, effort: str,
    provenance: dict | None = None,
    response_observer=None,
    document_inputs=None,
    max_call_seconds=None,
    regulation_graphics_model_policy=None,
) -> str:
    """Bound real SDK/HTTP transport including startup, retries and response reading.

    Injectable clients retain the same call primitive for deterministic tests.
    Native supported clients run in a disposable process, with SDK retries off.
    """
    from .chatgpt_plan_client import ChatGPTPlanClient
    if document_inputs is not None or regulation_graphics_model_policy is not None:
        raise ValueError("invalid_request")
    if max_call_seconds is not None:
        if type(max_call_seconds) not in (int, float) or max_call_seconds != 120:
            raise ValueError("invalid_model_call_cap")
    expected_provider = model_provider(model)
    timeout = model_timeout() if max_call_seconds is None else model_timeout(max_call_seconds)
    request = dict(model=model, system=system, prompt=prompt, max_tokens=max_tokens,
                   temperature=temperature, effort=effort)
    if provenance is not None:
        provenance.clear()
        provenance.update(configured_model=model, returned_model=None, returned_revision=None,
                          identity_status="unpinned", response_id=None, reasoning_effort=effort)
    # Include supported SDK subclasses; injected call primitives are not SDKs.
    native_bases = {base.__module__.split(".")[0] for base in type(client).__mro__}
    module = (
        "chatgpt_plan" if isinstance(client, ChatGPTPlanClient) else
        "gemini" if isinstance(client, GeminiClient) else
        "openai" if "openai" in native_bases else
        "anthropic" if "anthropic" in native_bases else
        None
    )
    if module == "chatgpt_plan":
        if not uses_responses(model):
            raise ValueError("chatgpt_plan_model_unsupported")
    elif module is not None and module != expected_provider:
        raise ValueError(
            f"Model {model!r} requires provider {expected_provider!r}, got {module!r}"
        )
    if expected_provider == "gemini":
        if _GEMINI_MODEL_RE.fullmatch(model) is None:
            raise ValueError(f"Invalid Gemini model {model!r}")
        _gemini_thinking_level(model, effort)
    if module is None:
        text = _call_model_direct(client, **request, provenance=provenance)
        if response_observer is not None:
            response_observer(text)
        model_timeout()
        return text
    # These are supported public constructor properties; preserve explicit
    # endpoint/auth/header configuration without placing it in argv or logs.
    if module == "chatgpt_plan":
        settings = {"auth_path": client.auth_path, "profile_identity": client.profile_identity}
        if provenance is not None:
            provenance.update(billing_mode="chatgpt_plan", effective_output_limit=None)
    elif module == "gemini":
        settings = {"api_key": client.api_key, "base_url": client.base_url}
        if provenance is not None:
            provenance.update(billing_mode="api")
    else:
        settings = {"api_key": client.api_key, "base_url": str(client.base_url),
                    "default_headers": {key: value for key, value in client.default_headers.items() if isinstance(value, str)}}
        if module == "openai":
            settings.update(organization=client.organization, project=client.project)
        else:
            settings["auth_token"] = client.auth_token
    try:
        result = bounded_transport({"kind": "model", "provider": module, "client": settings,
                                    "request": request, "timeout": timeout}, timeout)
    except TimeoutError:
        raise ModelDeadlineExceeded("model_deadline_exhausted") from None
    if result.get("error") == "timeout":
        raise ModelDeadlineExceeded("model_deadline_exhausted")
    if result.get("error"):
        raise ModelTransportError(result["error"], result.get("status_code"),
                                  error_metadata=result.get("error_metadata"))
    if provenance is not None:
        provenance.update(result["provenance"])
    if response_observer is not None:
        response_observer(result["text"])
    model_timeout()  # A response arriving after the shared deadline is unusable.
    return result["text"]
