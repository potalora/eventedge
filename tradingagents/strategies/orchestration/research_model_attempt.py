"""Bind the logical model-call policy and payload, without retaining prompts.

The seven digest fields are model, role, effort, max_tokens, temperature, system,
and prompt. Role belongs to the analyzer/committee wrapper, not the transport
kwargs. max_tokens is the requested value at call_analysis_model; provider wire
adaptations and effective limits are unchanged and are not certified here.
This receipt records an attempted logical call (including reuse or a deadline
failure), not transport success, response validity, or trading authority.
"""
from __future__ import annotations

import hashlib
import json
import math


VERSION = 'research-model-request-v1'


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('utf-8')


def request_receipt(*, model: str, role: str, effort: str, max_tokens: int,
                    temperature: float, system: str, prompt: str) -> dict:
    """Hash exact UTF-8 payloads and all logical request fields; never strip."""
    try:
        if (any(type(value) is not str or not value for value in (model, role, effort))
                or type(system) is not str or type(prompt) is not str
                or type(max_tokens) is not int or max_tokens <= 0
                or type(temperature) not in (int, float) or not math.isfinite(temperature)):
            raise ValueError()
        system_bytes, prompt_bytes = system.encode('utf-8'), prompt.encode('utf-8')
        fields = dict(model=model, role=role, effort=effort, max_tokens=max_tokens,
                      temperature=temperature)
        return {
            'version': VERSION, **fields,
            'system_sha256': hashlib.sha256(system_bytes).hexdigest(), 'system_bytes': len(system_bytes),
            'prompt_sha256': hashlib.sha256(prompt_bytes).hexdigest(), 'prompt_bytes': len(prompt_bytes),
            'request_sha256': hashlib.sha256(_canonical({**fields, 'system': system, 'prompt': prompt})).hexdigest(),
        }
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('invalid_research_model_request') from error


def validate_request_receipt(receipt: dict, *, model: str, role: str, effort: str,
                             max_tokens: int, temperature: float, system: str, prompt: str) -> dict:
    """Recompute from the independent exact logical request; reject extra fields."""
    expected = request_receipt(model=model, role=role, effort=effort, max_tokens=max_tokens,
                               temperature=temperature, system=system, prompt=prompt)
    try:
        # Canonical bytes distinguish bool/int and int/float without accepting
        # Python's otherwise permissive numeric equality for receipt fields.
        if type(receipt) is not dict or _canonical(receipt) != _canonical(expected):
            raise ValueError()
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('invalid_research_model_request') from error
    return expected
