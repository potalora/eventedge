"""Shared helpers for model-specific autoresearch LLM behavior."""
from __future__ import annotations

from typing import Any

from .runtime_deadline import (ModelDeadlineExceeded, ModelTransportError, bounded_transport, model_timeout)

SONNET_5_MODEL = "claude-sonnet-5"
LUNA_MODEL = "gpt-6-luna"
RESPONSES_MODELS = frozenset({"gpt-6-luna", "gpt-6-astra", "gpt-6.1-sol"})


def uses_responses(model: str) -> bool:
    return model in RESPONSES_MODELS


_VALID_EFFORTS = frozenset({"low", "medium", "high", "max"})


def anthropic_request_options(
    *, model: str, temperature: float, effort: str
) -> dict[str, Any]:
    """Return request options that are valid for the selected Claude model.

    Sonnet 5 rejects non-default sampling parameters. It uses adaptive thinking
    by default, with ``output_config.effort`` as the supported cost/quality
    control. Older configured models retain the existing temperature behavior.
    """
    if model != SONNET_5_MODEL:
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


def _call_model_direct(
    client: Any, *, model: str, system: str, prompt: str,
    max_tokens: int, temperature: float, effort: str, provenance: dict | None = None,
) -> str:
    """Keep Claude calls and route supported GPT models to OpenAI Responses.

    GPT output budgets include hidden reasoning. Reserve room beyond the
    short visible JSON budget and reject incomplete output before JSON repair.
    """
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
) -> str:
    """Bound real SDK transport including startup, retries and response reading.

    Injectable clients retain the same call primitive for deterministic tests.
    Native supported clients run in a disposable process, with SDK retries off.
    """
    timeout = model_timeout()
    request = dict(model=model, system=system, prompt=prompt, max_tokens=max_tokens,
                   temperature=temperature, effort=effort)
    if provenance is not None:
        provenance.clear()
        provenance.update(configured_model=model, returned_model=None, returned_revision=None,
                          identity_status="unpinned", response_id=None, reasoning_effort=effort)
    # Include supported SDK subclasses; injected call primitives are not SDKs.
    native_bases = {base.__module__.split(".")[0] for base in type(client).__mro__}
    module = "openai" if "openai" in native_bases else "anthropic" if "anthropic" in native_bases else None
    if module is None:
        text = _call_model_direct(client, **request, provenance=provenance)
        model_timeout()
        return text
    # These are supported public SDK constructor properties; preserve explicit
    # endpoint/auth/header configuration without placing it in argv or logs.
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
        raise ModelTransportError(result["error"], result.get("status_code"))
    if provenance is not None:
        provenance.update(result["provenance"])
    model_timeout()  # A response arriving after the shared deadline is unusable.
    return result["text"]
