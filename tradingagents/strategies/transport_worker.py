"""One disposable native HTTP or model operation; protocol is private JSON pipes."""
from __future__ import annotations

import json
import math
import re
import sys


# Adapted from the private capacity extractor prototype. Never retain raw error
# text, bodies, IDs, URLs or headers. Unknown values provide no retry authority.
_ERROR_CODES = frozenset({"rate_limit_exceeded", "slow_down", "insufficient_quota",
    "credit_balance_exhausted", "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded", "organization_usage_limit_exceeded",
    "subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable",
    "auth_denied", "profile_changed", "model_unavailable", "provider_error",
    "stream_interrupted", "stream_incomplete", "stream_invalid", "response_refused",
    "response_empty", "response_too_large", "invalid_request"})
_ERROR_TYPES = frozenset({"rate_limit_error", "insufficient_quota", "requests", "tokens"})
_COUNTER_BOUND = 10**12
_SECONDS_BOUND = 7 * 24 * 60 * 60
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]{1,6})?")
_DURATION = re.compile(r"(?:(\d+(?:\.\d{1,6})?)d)?(?:(\d+(?:\.\d{1,6})?)h)?"
    r"(?:(\d+(?:\.\d{1,6})?)m)?(?:(\d+(?:\.\d{1,6})?)s)?(?:(\d+(?:\.\d{1,6})?)ms)?")
_HEADER_RULES = {
    **{f"x-ratelimit-{kind}-{unit}": (f"rate_limit_{kind}_{unit.replace('-', '_')}", "counter")
       for kind in ("limit", "remaining") for unit in ("requests", "tokens", "project-tokens")},
    **{f"x-ratelimit-reset-{unit}": (f"rate_limit_reset_{unit.replace('-', '_')}_seconds", "duration")
       for unit in ("requests", "tokens", "project-tokens")},
    "retry-after": ("retry_after_seconds", "seconds"),
    "retry-after-ms": ("retry_after_ms_seconds", "milliseconds"),
}


def validate_error_metadata(value):
    """Revalidate normalized worker metadata at the parent exception boundary."""
    if type(value) is not dict:
        return {}
    result = {}
    for key, allowed in (("error_code", _ERROR_CODES), ("error_type", _ERROR_TYPES)):
        item = value.get(key)
        if type(item) is str and item in allowed:
            result[key] = item
    for key, kind in _HEADER_RULES.values():
        item = value.get(key)
        if kind == "counter":
            if type(item) is int and 0 <= item <= _COUNTER_BOUND:
                result[key] = item
        elif type(item) in (int, float) and 0 <= item <= _SECONDS_BOUND and math.isfinite(item):
            result[key] = item
    return result


def extract_error_metadata(exc):
    """Extract only fixed enums and finite, bounded numeric response metadata."""
    try:
        values = {"error_code": getattr(exc, "code", None), "error_type": getattr(exc, "type", None)}
        headers = getattr(getattr(exc, "response", None), "headers", None)
        normalized = {}
        duplicates = set()
        if headers is not None:
            for name, value in headers.items():
                if type(name) is not str:
                    continue
                name = name.lower()
                if name in normalized:
                    duplicates.add(name)
                normalized[name] = value
        for name, (key, kind) in _HEADER_RULES.items():
            value = normalized.get(name)
            if name in duplicates or type(value) is not str or not 0 < len(value) <= 80:
                continue
            if kind == "counter":
                if re.fullmatch(r"[0-9]+", value):
                    values[key] = int(value)
            elif kind == "duration":
                match = _DURATION.fullmatch(value)
                if match and any(part is not None for part in match.groups()):
                    values[key] = round(sum(float(part) * scale for part, scale in
                        zip(match.groups(), (86400, 3600, 60, 1, .001)) if part is not None), 6)
            elif _NUMBER.fullmatch(value):
                values[key] = float(value) / (1000 if kind == "milliseconds" else 1)
        return validate_error_metadata(values)
    except Exception:
        # Diagnostic extraction must never replace the original failure outcome.
        return {}


def _execute_plan(payload):
    """Refresh and infer inside the same killable call; no API fallback exists."""
    import os
    import time
    from pathlib import Path
    from tradingagents.strategies.chatgpt_plan_transport import (
        ChatGPTPlanTransportError, stream_plan_response,
    )
    deadline = time.monotonic() + payload["timeout"]
    try:
        from tradingagents.strategies.plan_auth import get_plan_credentials
        settings = payload["client"]
        credentials = get_plan_credentials(
            auth_path=Path(settings["auth_path"]) if settings["auth_path"] else None,
            deadline=deadline,
        )
        if credentials.profile_identity != settings["profile_identity"]:
            return {"error": "provider_error", "status_code": None,
                    "error_metadata": {"error_code": "profile_changed"}}
    except Exception as exc:
        return {"error": "timeout" if isinstance(exc, TimeoutError) else "provider_error",
                "status_code": None, "error_metadata": {"error_code": "auth_denied"}}
    try:
        # This disposable process must not inherit a paid tenant, endpoint,
        # SDK logging setting, or any other OpenAI credential configuration.
        for name in tuple(os.environ):
            if name.startswith("OPENAI_"):
                os.environ.pop(name, None)
        from openai import OpenAI, DefaultHttpxClient
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        with OpenAI(api_key=credentials.access_token, base_url="https://api.openai.com/v1",
                    max_retries=0, timeout=remaining,
                    http_client=DefaultHttpxClient(timeout=remaining, follow_redirects=False,
                                                  trust_env=False)) as client:
            provenance = {}
            request = payload["request"]
            text = stream_plan_response(client, model=request["model"], system=request["system"],
                                        prompt=request["prompt"], effort=request["effort"],
                                        provenance=provenance)
            if time.monotonic() >= deadline:
                raise TimeoutError
            provenance.update(billing_mode="chatgpt_plan", effective_output_limit=None)
            return {"text": text, "provenance": provenance}
    except (TimeoutError, ChatGPTPlanTransportError) as exc:
        code = getattr(exc, "code", None)
        return {"error": "timeout" if isinstance(exc, TimeoutError) or code == "transport_timeout" else "provider_error",
                "status_code": None, "error_metadata": validate_error_metadata({"error_code": code})}
    except Exception:
        return {"error": "provider_error", "status_code": None,
                "error_metadata": {"error_code": "provider_error"}}


def execute(payload):
    if payload['kind'] == 'http_get_bytes':
        import base64
        import requests
        limit = payload.get('max_bytes')
        if type(limit) is not int or not 1 <= limit <= 16 * 1024 * 1024:
            return {'error': 'invalid_response'}
        try:
            with requests.get(payload['url'], timeout=payload['timeout'], stream=True) as response:
                result = {'status_code': response.status_code,
                          'headers': {'Retry-After': response.headers.get('Retry-After', '')},
                          'body_base64': ''}
                if not 200 <= response.status_code < 300:
                    return result
                length = response.headers.get('Content-Length')
                if isinstance(length, str) and length.isascii() and length.isdigit():
                    if len(length) > 20 or int(length) > limit:
                        return {'error': 'invalid_response'}
                content = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    if not isinstance(chunk, bytes) or len(content) + len(chunk) > limit:
                        return {'error': 'invalid_response'}
                    content.extend(chunk)
                result['body_base64'] = base64.b64encode(content).decode('ascii')
                return result
        except requests.Timeout:
            return {'error': 'timeout'}
        except requests.RequestException:
            return {'error': 'transport_error'}
    if payload["kind"] == "http_get":
        import requests
        try:
            with requests.get(payload["url"], params=payload.get("params", {}), headers=payload.get("headers", {}), timeout=payload["timeout"]) as response:
                return {"status_code": response.status_code, "headers": {"Retry-After": response.headers.get("Retry-After", "")},
                        "body": response.text}
        except requests.Timeout:
            return {"error": "timeout"}
        except requests.RequestException:
            return {"error": "transport_error"}
    if payload["kind"] == "model":
        if payload["provider"] == "chatgpt_plan":
            return _execute_plan(payload)
        from tradingagents.strategies.llm_utils import _call_model_direct
        if payload["provider"] == "openai":
            from openai import OpenAI
            constructor = OpenAI
        else:
            from anthropic import Anthropic
            constructor = Anthropic
        settings = payload["client"]
        try:
            with constructor(**settings, timeout=payload["timeout"], max_retries=0) as client:
                provenance = {}
                text = _call_model_direct(client, **payload["request"], provenance=provenance)
                return {"text": text, "provenance": provenance}
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            return {"error": "timeout" if "timeout" in type(exc).__name__.lower() else "provider_error",
                    "status_code": status if isinstance(status, int) else None,
                    "error_metadata": extract_error_metadata(exc)}
    raise ValueError("unknown transport")


if __name__ == "__main__":
    try:
        result = execute(json.load(sys.stdin))
    except Exception:
        result = {"error": "worker_failure"}
    sys.stdout.write(json.dumps(result))
