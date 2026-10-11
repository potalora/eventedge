"""One disposable native HTTP or model operation; protocol is private JSON pipes."""
from __future__ import annotations

import json
import sys


def execute(payload):
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
                    "status_code": status if isinstance(status, int) else None}
    raise ValueError("unknown transport")


if __name__ == "__main__":
    try:
        result = execute(json.load(sys.stdin))
    except Exception:
        result = {"error": "worker_failure"}
    sys.stdout.write(json.dumps(result))
