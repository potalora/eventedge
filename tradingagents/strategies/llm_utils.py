"""Shared helpers for model-specific autoresearch LLM behavior."""
from __future__ import annotations

from typing import Any

SONNET_5_MODEL = "claude-sonnet-5"
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
