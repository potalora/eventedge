"""Shared helpers for model-specific autoresearch LLM behavior."""
from __future__ import annotations

from typing import Any

SONNET_5_MODEL = "claude-sonnet-5"
LUNA_MODEL = "gpt-6-luna"
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


def call_analysis_model(
    client: Any, *, model: str, system: str, prompt: str,
    max_tokens: int, temperature: float, effort: str,
) -> str:
    """Keep existing Claude calls and route Luna to OpenAI Responses.

    Luna's output budget includes hidden reasoning. Reserve room beyond the
    short visible JSON budget and reject incomplete output before JSON repair.
    """
    if model == LUNA_MODEL:
        if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("Invalid Luna reasoning effort")
        response = client.responses.create(
            model=model, instructions=system, input=prompt,
            reasoning={"effort": effort},
            max_output_tokens=max(16384, max_tokens), store=False,
        )
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
    return anthropic_response_text(response)
