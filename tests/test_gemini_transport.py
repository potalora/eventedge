"""Comprehensive unit & transport tests for Gemini 4 native :generateContent and credential guard."""
from __future__ import annotations

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from tradingagents.strategies import llm_utils, transport_worker
from tradingagents.strategies.candidate_response_reuse import (
    _Memo,
    _client_namespace,
    begin_candidate_response,
    candidate_response_memo,
    commit_candidate_response,
    end_candidate_response,
    retain_candidate_response,
    reused_candidate_response,
)
from tradingagents.strategies.config import GenerationConfig
from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.llm_utils import (
    GEMINI_DEFAULT_BASE_URL,
    GEMINI_DEFAULT_MAX_OUTPUT_TOKENS,
    GeminiClient,
    GeminiTransportError,
    _call_gemini_direct,
    call_analysis_model,
    is_gemini_model,
    model_provider,
)
from tradingagents.strategies.orchestration.generation_config import (
    missing_gemini_credentials,
)
from tradingagents.strategies.orchestration.run_evidence import (
    sanitize_evidence_text,
)
from tradingagents.strategies.runtime_deadline import (
    ModelDeadlineExceeded,
    ModelTransportError,
    model_budget,
)
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
from tradingagents.strategies.transport_worker import (
    extract_error_metadata,
    validate_error_metadata,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
GEMINI4_PROFILE = REPO_ROOT / "config" / "profiles" / "gemini4-clean.json"


def _make_httpx_response(status: int, body: bytes | dict, headers: dict[str, str] | None = None) -> httpx.Response:
    content = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
    hdrs = {"content-type": "application/json", **(headers or {})}
    return httpx.Response(status, headers=hdrs, content=content)


def _start_local_server(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


# ---------------------------------------------------------------------------
# 1. Provider routing, GeminiClient env/redaction, and thinkingLevel guards
# ---------------------------------------------------------------------------


def test_is_gemini_model_and_fail_closed_model_provider():
    assert is_gemini_model("gemini-4-neon") is True
    assert is_gemini_model("gemini-4-hydrogen") is True
    assert is_gemini_model("gpt-6.2-sol") is False
    assert is_gemini_model("gemini_4_neon") is False

    assert model_provider("gemini-4-neon") == "gemini"
    assert model_provider("gemini-4-hydrogen") == "gemini"
    assert model_provider("gpt-6.2-sol") == "openai"
    assert model_provider("gpt-6-luna") == "openai"
    assert model_provider("gpt-6-astra") == "openai"
    assert model_provider("claude-sonnet-4-6") == "anthropic"

    for bad in ("gemini_4_neon", "unknown-model", "", "   ", None):
        with pytest.raises(ValueError, match="Unsupported model provider"):
            model_provider(bad)  # type: ignore[arg-type]


def test_gemini_client_from_env_fallback_strip_and_redaction(monkeypatch):
    secret = "AIzaSySecretKey1234567890"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)

    with pytest.raises(ValueError, match="Missing GEMINI_API_KEY"):
        GeminiClient.from_env()

    monkeypatch.setenv("GEMINI_API_KEY", "   \t\n  ")
    with pytest.raises(ValueError, match="Missing GEMINI_API_KEY"):
        GeminiClient.from_env()

    monkeypatch.setenv("GOOGLE_API_KEY", f"  {secret}  ")
    monkeypatch.setenv("GEMINI_BASE_URL", " https://custom.gemini.invalid/v1beta/// ")
    client = GeminiClient.from_env()
    assert client.api_key == secret
    assert client.base_url == "https://custom.gemini.invalid/v1beta"
    assert secret not in repr(client)
    assert "custom.gemini.invalid" not in repr(client)
    client.close()

    # GEMINI_API_KEY takes precedence over GOOGLE_API_KEY
    monkeypatch.setenv("GEMINI_API_KEY", "primary-key-9999")
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)
    primary = GeminiClient.from_env()
    assert primary.api_key == "primary-key-9999"
    assert primary.base_url == GEMINI_DEFAULT_BASE_URL


@pytest.mark.parametrize(
    "model,effort,expected",
    [
        ("gemini-4-neon", "low", "low"),
        ("gemini-4-neon", "medium", "medium"),
        ("gemini-4-neon", "high", "high"),
        ("gemini-4-neon", "xhigh", "xhigh"),
        ("gemini-4-neon", "max", "xhigh"),
        ("gemini-4-hydrogen", "xhigh", "xhigh"),
        ("gemini-4-hydrogen", "max", "xhigh"),
        ("gemini-4-pro", "high", "high"),
    ],
)
def test_gemini_thinking_level_valid_mappings(model, effort, expected):
    assert llm_utils._gemini_thinking_level(model, effort) == expected


@pytest.mark.parametrize(
    "model,bad_effort",
    [
        ("gemini-4-neon", "none"),
        ("gemini-4-neon", "minimal"),
        ("gemini-4-neon", ""),
        ("gemini-4-neon", "   "),
        ("gemini-4-neon", None),
        ("gemini-4-neon", "ultra"),
        ("gemini-4-pro", "xhigh"),
        ("gemini-4-pro", "max"),
        ("gemini-4-flash", "xhigh"),
    ],
)
def test_gemini_thinking_level_fails_closed_before_transport(model, bad_effort, monkeypatch):
    client = GeminiClient(api_key="AIzaSyOfflineTestKey12345")
    called = []
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a, **kw: called.append(a))
    with pytest.raises(ValueError, match="Gemini"):
        call_analysis_model(
            client,
            model=model,
            system="sys",
            prompt="prompt",
            max_tokens=1024,
            temperature=0.0,
            effort=bad_effort,
        )
    assert called == []


def test_call_analysis_model_rejects_mismatched_provider_and_model(monkeypatch):
    from anthropic import Anthropic
    from openai import OpenAI

    called = []
    monkeypatch.setattr(llm_utils, "bounded_transport", lambda *a, **kw: called.append(a))
    gemini = GeminiClient(api_key="AIzaSyOfflineTestKey12345")
    with (
        OpenAI(api_key="offline", base_url="https://offline.invalid/v1") as oai,
        Anthropic(api_key="offline", base_url="https://offline.invalid") as ant,
    ):
        for client, model in [
            (gemini, "gpt-6.2-sol"),
            (gemini, "claude-sonnet-4-6"),
            (oai, "gemini-4-neon"),
            (oai, "claude-sonnet-4-6"),
            (ant, "gemini-4-neon"),
            (ant, "gpt-6-luna"),
            (gemini, "gemini_4_neon"),
        ]:
            with pytest.raises(ValueError):
                call_analysis_model(
                    client,
                    model=model,
                    system="sys",
                    prompt="prompt",
                    max_tokens=1024,
                    temperature=0.0,
                    effort="high",
                )
    assert called == []


# ---------------------------------------------------------------------------
# 2. Wire contract, thought filtering, finishReason, promptFeedback, identity
# ---------------------------------------------------------------------------


def test_gemini_direct_wire_contract_omits_deprecated_params_and_filters_thoughts():
    captured: dict = {}

    class FakeHttp:
        def post(self, url, *, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["body"] = json
            return _make_httpx_response(
                200,
                {
                    "responseId": "resp_gemini_1",
                    "modelVersion": "gemini-4-neon",
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {
                                "role": "model",
                                "parts": [
                                    {"thought": True, "text": "hidden chain of thought"},
                                    {"text": "  {\"direction\": \"long\"}  "},
                                ],
                            },
                        }
                    ],
                },
            )

    client = GeminiClient(api_key="AIzaSyWireSecret123456789")
    provenance: dict = {}
    text = _call_gemini_direct(
        client,
        model="gemini-4-neon",
        system="Be concise",
        prompt="Analyze AAPL",
        max_tokens=1024,
        temperature=0.7,
        effort="max",
        provenance=provenance,
        http_client=FakeHttp(),
    )
    assert text == '{"direction": "long"}'
    assert captured["url"] == f"{GEMINI_DEFAULT_BASE_URL}/models/gemini-4-neon:generateContent"
    assert "?" not in captured["url"]
    assert captured["headers"]["x-goog-api-key"] == "AIzaSyWireSecret123456789"
    assert captured["body"]["systemInstruction"] == {"role": "system", "parts": [{"text": "Be concise"}]}
    assert captured["body"]["contents"] == [{"role": "user", "parts": [{"text": "Analyze AAPL"}]}]

    gen_cfg = captured["body"]["generationConfig"]
    assert gen_cfg == {
        "maxOutputTokens": GEMINI_DEFAULT_MAX_OUTPUT_TOKENS,
        "thinkingConfig": {"thinkingLevel": "xhigh"},
    }
    for forbidden in ("temperature", "topP", "topK", "thinkingBudget"):
        assert forbidden not in gen_cfg
        assert forbidden not in gen_cfg["thinkingConfig"]

    # Alias echoed back => unpinned
    assert provenance == {
        "configured_model": "gemini-4-neon",
        "returned_model": "gemini-4-neon",
        "returned_revision": None,
        "identity_status": "unpinned",
        "response_id": "resp_gemini_1",
        "reasoning_effort": "max",
        "billing_mode": "api",
        "effective_output_limit": GEMINI_DEFAULT_MAX_OUTPUT_TOKENS,
    }


def test_gemini_identity_status_pinned_only_when_model_version_differs():
    class FakeHttp:
        def __init__(self, version):
            self.version = version

        def post(self, url, *, headers, json):
            payload = {
                "responseId": "resp_id",
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "{}"}]}}],
            }
            if self.version is not None:
                payload["modelVersion"] = self.version
            return _make_httpx_response(200, payload)

    client = GeminiClient(api_key="AIzaSyOfflineTestKey12345")
    prov_pinned: dict = {}
    _call_gemini_direct(
        client, model="gemini-4-neon", system="", prompt="p",
        max_tokens=70000, temperature=0.0, effort="high",
        provenance=prov_pinned, http_client=FakeHttp("gemini-4-neon-001"),
    )
    assert prov_pinned["identity_status"] == "pinned"
    assert prov_pinned["returned_model"] == "gemini-4-neon-001"
    assert prov_pinned["returned_revision"] == "gemini-4-neon-001"
    assert prov_pinned["effective_output_limit"] == 70000
    assert prov_pinned["billing_mode"] == "api"

    prov_unpinned: dict = {}
    _call_gemini_direct(
        client, model="gemini-4-neon", system="", prompt="p",
        max_tokens=100, temperature=0.0, effort="high",
        provenance=prov_unpinned, http_client=FakeHttp(None),
    )
    assert prov_unpinned["identity_status"] == "unpinned"
    assert prov_unpinned["returned_model"] == "gemini-4-neon"
    assert prov_unpinned["returned_revision"] is None


def test_gemini_prompt_feedback_checked_before_candidates():
    class FakeHttp:
        def post(self, url, *, headers, json):
            # Candidates omitted entirely when promptFeedback.blockReason is set
            return _make_httpx_response(200, {"promptFeedback": {"blockReason": "SAFETY"}})

    client = GeminiClient(api_key="AIzaSyOfflineTestKey12345")
    with pytest.raises(GeminiTransportError) as caught:
        _call_gemini_direct(
            client, model="gemini-4-neon", system="s", prompt="p",
            max_tokens=100, temperature=0.0, effort="high", http_client=FakeHttp(),
        )
    assert caught.value.code == "response_refused"
    assert validate_error_metadata(extract_error_metadata(caught.value)) == {"error_code": "response_refused"}


@pytest.mark.parametrize(
    "finish_reason,expected_code",
    [
        ("SAFETY", "response_refused"),
        ("RECITATION", "response_refused"),
        ("PROHIBITED_CONTENT", "response_refused"),
        ("BLOCKLIST", "response_refused"),
        ("SPII", "response_refused"),
        ("MAX_TOKENS", "stream_incomplete"),
        ("CONTINUATION", "stream_incomplete"),
        ("OTHER", "provider_error"),
        (None, "provider_error"),
    ],
)
def test_gemini_finish_reason_mapping_survives_validate_error_metadata(finish_reason, expected_code):
    class FakeHttp:
        def post(self, url, *, headers, json):
            return _make_httpx_response(
                200,
                {
                    "candidates": [
                        {
                            "finishReason": finish_reason,
                            "content": {"parts": [{"text": "partial"}]},
                        }
                    ]
                },
            )

    client = GeminiClient(api_key="AIzaSyOfflineTestKey12345")
    with pytest.raises(GeminiTransportError) as caught:
        _call_gemini_direct(
            client, model="gemini-4-neon", system="s", prompt="p",
            max_tokens=100, temperature=0.0, effort="high", http_client=FakeHttp(),
        )
    assert caught.value.code == expected_code
    assert validate_error_metadata(extract_error_metadata(caught.value)) == {"error_code": expected_code}


@pytest.mark.parametrize(
    "parts",
    [
        [{"thought": True, "text": "only thoughts, no answer"}],
        [{"thought": True, "text": "thinking"}, {"text": "   \n\t  "}],
        [],
    ],
)
def test_gemini_thought_only_or_empty_visible_text_fails_closed_response_empty(parts):
    class FakeHttp:
        def post(self, url, *, headers, json):
            return _make_httpx_response(
                200,
                {"candidates": [{"finishReason": "STOP", "content": {"parts": parts}}]},
            )

    client = GeminiClient(api_key="AIzaSyOfflineTestKey12345")
    with pytest.raises(GeminiTransportError) as caught:
        _call_gemini_direct(
            client, model="gemini-4-neon", system="s", prompt="p",
            max_tokens=100, temperature=0.0, effort="high", http_client=FakeHttp(),
        )
    assert caught.value.code == "response_empty"
    assert validate_error_metadata(extract_error_metadata(caught.value)) == {"error_code": "response_empty"}


@pytest.mark.parametrize(
    "status,raw_body,headers,expected_code",
    [
        (400, b'{"error":{"message":"bad"}}', {}, "invalid_request"),
        (401, b'{"error":{"message":"unauth"}}', {}, "auth_denied"),
        (403, b'{"error":{"message":"forbidden"}}', {}, "auth_denied"),
        (404, b'{"error":{"message":"missing"}}', {}, "model_unavailable"),
        (429, b'{"error":{"message":"quota"}}', {"Retry-After": "4.5"}, "rate_limit_exceeded"),
        (500, b"<html>500 Internal Server Error</html>", {}, "provider_error"),
        (503, b"<html>Service Unavailable</html>", {"Retry-After": "2"}, "provider_error"),
        (302, b"", {"Location": "https://evil.invalid"}, "provider_error"),
        (200, b"<html>not json</html>", {}, "stream_invalid"),
    ],
)
def test_gemini_http_errors_and_invalid_json_map_to_validated_error_codes(
    status, raw_body, headers, expected_code
):
    class FakeHttp:
        def post(self, url, *, headers: dict, json: dict):
            return _make_httpx_response(status, raw_body, headers=resp_headers)

    resp_headers = headers
    client = GeminiClient(api_key="AIzaSySensitiveKey987654321")
    with pytest.raises(GeminiTransportError) as caught:
        _call_gemini_direct(
            client, model="gemini-4-neon", system="s", prompt="p",
            max_tokens=100, temperature=0.0, effort="high", http_client=FakeHttp(),
        )
    assert caught.value.code == expected_code
    assert caught.value.status_code == status
    meta = extract_error_metadata(caught.value)
    assert validate_error_metadata(meta) == meta
    assert meta["error_code"] == expected_code
    if "Retry-After" in headers:
        assert meta["retry_after_seconds"] == float(headers["Retry-After"])
    err = ModelTransportError("provider_error", status, error_metadata=meta)
    assert err.error_metadata["error_code"] == expected_code
    assert "AIzaSySensitiveKey987654321" not in str(err)
    assert "Service Unavailable" not in str(err)


# ---------------------------------------------------------------------------
# 3. transport_worker roundtrip, pooled execution, timeout, and secret safety
# ---------------------------------------------------------------------------


def test_gemini_transport_worker_end_to_end_roundtrip_and_secret_isolation(monkeypatch):
    secret = "AIzaSyWorkerRoundtripSecret12345"
    observed: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            observed["path"] = self.path
            observed["headers"] = {k.lower(): v for k, v in self.headers.items()}
            observed["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            payload = json.dumps({
                "responseId": "resp_worker_42",
                "modelVersion": "gemini-4-neon-002",
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {"thought": True, "text": "private thought"},
                                {"text": '{"direction":"long","conviction":0.85}'},
                            ]
                        },
                    }
                ],
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server, thread = _start_local_server(Handler)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}/v1beta"
        # Direct worker execute()
        worker_out = transport_worker.execute({
            "kind": "model",
            "provider": "gemini",
            "client": {"api_key": secret, "base_url": base_url},
            "request": {
                "model": "gemini-4-neon",
                "system": "sys",
                "prompt": "prompt",
                "max_tokens": 512,
                "temperature": 0.0,
                "effort": "high",
            },
            "timeout": 5.0,
        })
        assert worker_out["text"] == '{"direction":"long","conviction":0.85}'
        assert worker_out["provenance"]["identity_status"] == "pinned"
        assert worker_out["provenance"]["returned_model"] == "gemini-4-neon-002"
        assert worker_out["provenance"]["billing_mode"] == "api"
        assert secret not in json.dumps(worker_out)

        # Full call_analysis_model through killable TransportWorkerPool
        client = GeminiClient(api_key=secret, base_url=base_url)
        provenance: dict = {}
        with model_budget(time.monotonic() + 10):
            text = call_analysis_model(
                client,
                model="gemini-4-neon",
                system="sys",
                prompt="prompt",
                max_tokens=512,
                temperature=0.0,
                effort="high",
                provenance=provenance,
            )
        assert text == '{"direction":"long","conviction":0.85}'
        assert observed["path"] == "/v1beta/models/gemini-4-neon:generateContent"
        assert observed["headers"]["x-goog-api-key"] == secret
        assert "temperature" not in observed["body"]["generationConfig"]
        assert observed["body"]["generationConfig"]["maxOutputTokens"] == GEMINI_DEFAULT_MAX_OUTPUT_TOKENS
        assert observed["body"]["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "high"}
        assert provenance["identity_status"] == "pinned"
        assert provenance["billing_mode"] == "api"
        assert secret not in json.dumps(provenance)

        # Verify run_evidence redacts GEMINI_API_KEY and x-goog-api-key header patterns
        monkeypatch.setenv("GEMINI_API_KEY", secret)
        assert secret not in sanitize_evidence_text(f"leaked x-goog-api-key: {secret}")
        assert secret not in sanitize_evidence_text(f"GEMINI_API_KEY={secret}")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert "AIzaSyInlineHeaderKey123" not in sanitize_evidence_text(
            "x-goog-api-key: AIzaSyInlineHeaderKey123"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_gemini_transport_worker_429_preserves_retry_after_and_no_secret():
    secret = "AIzaSyRateLimitSecret99999"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = b'{"error":{"message":"private quota detail"}}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "7")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server, thread = _start_local_server(Handler)
    try:
        client = GeminiClient(api_key=secret, base_url=f"http://127.0.0.1:{server.server_port}/v1beta")
        with model_budget(time.monotonic() + 10), pytest.raises(ModelTransportError) as caught:
            call_analysis_model(
                client,
                model="gemini-4-hydrogen",
                system="sys",
                prompt="prompt",
                max_tokens=256,
                temperature=0.0,
                effort="high",
            )
        assert caught.value.status_code == 429
        assert caught.value.error_metadata == {
            "error_code": "rate_limit_exceeded",
            "retry_after_seconds": 7.0,
        }
        assert secret not in str(caught.value)
        assert "private quota detail" not in str(caught.value)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_gemini_stalled_server_is_killed_at_model_budget():
    started = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            started.set()
            release.wait(timeout=15)

    server, thread = _start_local_server(Handler)
    try:
        client = GeminiClient(api_key="AIzaSyTimeoutSecret12345", base_url=f"http://127.0.0.1:{server.server_port}/v1beta")
        t0 = time.monotonic()
        with model_budget(t0 + 2.0), pytest.raises(ModelDeadlineExceeded):
            call_analysis_model(
                client,
                model="gemini-4-neon",
                system="sys",
                prompt="p",
                max_tokens=100,
                temperature=0.0,
                effort="high",
            )
        assert started.is_set()
        assert time.monotonic() - t0 < 3.5
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# ---------------------------------------------------------------------------
# 4. Candidate reuse, mixed-provider LLMAnalyzer, fork_for_parallel, 503 retry
# ---------------------------------------------------------------------------


def test_gemini_candidate_response_reuse_namespace_limit_and_secret_safety(monkeypatch):
    import tradingagents.strategies.candidate_response_reuse as reuse_mod

    secret = "AIzaSyReuseSecretKey123456"
    c1 = GeminiClient(api_key=secret, base_url="https://generativelanguage.googleapis.com/v1beta")
    c2 = GeminiClient(api_key="AIzaSyOtherKey98765432100", base_url="https://generativelanguage.googleapis.com/v1beta")
    c3 = GeminiClient(api_key=secret, base_url="https://other.invalid/v1beta")

    memo = _Memo()
    ns1 = _client_namespace(memo, c1)
    assert ns1 == 0
    assert _client_namespace(memo, GeminiClient(api_key=secret)) == 0
    assert _client_namespace(memo, c2) != ns1
    assert _client_namespace(memo, c3) != ns1

    serialized: list[str] = []
    orig_dumps = reuse_mod.json.dumps
    monkeypatch.setattr(reuse_mod.json, "dumps", lambda v, *a, **kw: (s := orig_dumps(v, *a, **kw), serialized.append(s))[0])

    kwargs = dict(
        model="gemini-4-neon",
        system="sys",
        prompt="prompt",
        max_tokens=1024,
        temperature=0.0,
        effort="high",
        role="thesis",
    )
    with candidate_response_memo():
        tok = begin_candidate_response("earnings_call", "earnings_call", "disc-1", False)
        try:
            assert reused_candidate_response(c1, **kwargs) is None
            retain_candidate_response('{"direction":"long"}', {"billing_mode": "api"})
            committed = commit_candidate_response()
        finally:
            end_candidate_response(tok)

        tok = begin_candidate_response("earnings_call", "earnings_call", "disc-1", False)
        try:
            hit = reused_candidate_response(c1, **kwargs)
            assert hit is not None
            assert hit[0] == '{"direction":"long"}'
            assert hit[1]["request_reuse"]["request_digest"] == committed["request_digest"]
            # Different credential misses
            assert reused_candidate_response(c2, **kwargs) is None
        finally:
            end_candidate_response(tok)

    # Effective limit 65535 was hashed into key, and api_key never appeared in serialized key
    assert any("65535" in item for item in serialized)
    assert all(secret not in item for item in serialized)


def test_analyzer_gemini_fork_and_mixed_provider_routing(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyAnalyzerKey12345678")
    parent = LLMAnalyzer({
        "autoresearch": {
            "llm_billing_mode": "api",
            "bounded_model": "gemini-4-hydrogen",
            "thesis_model": "claude-sonnet-4-6",
        }
    })
    client = parent._get_client()
    assert isinstance(client, GeminiClient)
    fork = parent.fork_for_parallel()
    assert fork._client is client
    assert fork.last_call_provenance is not parent.last_call_provenance

    seen_clients = []
    monkeypatch.setattr(
        "tradingagents.strategies.llm_utils.call_analysis_model",
        lambda c, **kw: (seen_clients.append((type(c).__name__, kw["model"])), "{}")[1],
    )
    fake_anthropic = SimpleNamespace(name="anthropic-client")
    with patch("anthropic.Anthropic", return_value=fake_anthropic):
        assert parent._call_llm("sys", "u", role="bounded") == "{}"
        assert parent._call_llm("sys", "u", role="thesis") == "{}"
    assert seen_clients == [("GeminiClient", "gemini-4-hydrogen"), ("SimpleNamespace", "claude-sonnet-4-6")]


def test_portfolio_committee_retries_on_503_for_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyCommitteeKey1234567")
    committee = PortfolioCommittee({
        "autoresearch": {
            "llm_billing_mode": "api",
            "paper_trade": {"portfolio_committee_model": "gemini-4-neon"},
        }
    })
    assert isinstance(committee._get_client(), GeminiClient)
    attempts = []

    def flaky(client, **kwargs):
        attempts.append(kwargs["model"])
        if len(attempts) == 1:
            raise ModelTransportError("provider_error", 503, error_metadata={"error_code": "provider_error"})
        return "[]"

    monkeypatch.setattr("tradingagents.strategies.llm_utils.call_analysis_model", flaky)
    monkeypatch.setattr("tradingagents.strategies.runtime_deadline.model_backoff", lambda delay: None)
    assert committee._call_llm(system="sys", prompt="prompt") == "[]"
    assert attempts == ["gemini-4-neon", "gemini-4-neon"]


# ---------------------------------------------------------------------------
# 5. Option-2 Cutover Fail-Closed Credential Guard (generation_config, CLI, controller)
# ---------------------------------------------------------------------------


def test_missing_gemini_credentials_matrix():
    profile = json.loads(GEMINI4_PROFILE.read_text(encoding="utf-8"))
    assert missing_gemini_credentials(profile, {}) is True
    assert missing_gemini_credentials(profile, {"GEMINI_API_KEY": "   "}) is True
    assert missing_gemini_credentials(profile, {"GEMINI_API_KEY": "AIzaSyValidKey"}) is False
    assert missing_gemini_credentials(profile, {"GOOGLE_API_KEY": "AIzaSyValidKey"}) is False
    assert missing_gemini_credentials(GenerationConfig(), {}) is True
    assert missing_gemini_credentials(GenerationConfig(), {"GEMINI_API_KEY": "k"}) is False
    # Legacy chatgpt_plan or empty dicts must not trip Gemini guard
    assert missing_gemini_credentials({}, {}) is False
    assert missing_gemini_credentials({"autoresearch": {"llm_billing_mode": "chatgpt_plan"}}, {}) is False
    assert missing_gemini_credentials({"autoresearch": {"thesis_model": "gemini-4-neon"}}, {}) is True
    assert missing_gemini_credentials(
        GenerationConfig(
            llm_billing_mode="api",
            bounded_model="gpt-6-luna",
            thesis_model="gpt-6.2-sol",
            portfolio_committee_model="gpt-6.2-sol",
        ),
        {},
    ) is False


def test_run_generations_start_fails_closed_without_gemini_key_and_succeeds_with_key(tmp_path, monkeypatch):
    from scripts import run_generations
    from tradingagents.strategies.orchestration import generation_manager as gm

    started = []

    class FakeManager:
        def list_generations(self):
            return []

        def start_generation(self, description, *, config_file=None):
            started.append((description, config_file))
            return SimpleNamespace(
                gen_id="gen_007",
                git_commit="a" * 40,
                git_branch="main",
                worktree_path=str(tmp_path / "wt"),
                state_dir=str(tmp_path / "state"),
                description=description,
            )

    monkeypatch.setattr(gm, "GenerationManager", lambda repo: FakeManager())
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    # Without --config-file (defaults to Gemini 4)
    monkeypatch.setattr("sys.argv", ["run_generations.py", "start", "default gemini4"])
    with pytest.raises(SystemExit) as caught:
        run_generations._main()
    assert caught.value.code == 2
    assert started == []

    # With --config-file gemini4-clean.json
    monkeypatch.setattr(
        "sys.argv",
        ["run_generations.py", "start", "gemini4 cutover", "--config-file", str(GEMINI4_PROFILE)],
    )
    with pytest.raises(SystemExit) as caught:
        run_generations._main()
    assert caught.value.code == 2
    assert started == []

    # With GOOGLE_API_KEY fallback -> succeeds
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSyFallbackKey12345")
    run_generations._main()
    assert len(started) == 1
    assert started[0] == ("gemini4 cutover", str(GEMINI4_PROFILE))


def test_run_generations_start_two_gen_circuit_breaker_and_force_override(tmp_path, monkeypatch, capsys):
    from scripts import run_generations
    from tradingagents.strategies.orchestration import generation_manager as gm

    started = []
    history_state = [
        SimpleNamespace(
            gen_id="gen_004",
            run_history=[
                {"date": "2026-11-20", "outcome": "clean", "success": True},
                {"date": "2026-11-21", "outcome": "clean", "success": True},
            ],
        ),
        SimpleNamespace(
            gen_id="gen_005",
            run_history=[{"date": "2026-11-25", "outcome": "degraded", "success": True}],
        ),
        SimpleNamespace(
            gen_id="gen_006",
            run_history=[{"date": "2026-11-27", "outcome": "failed", "success": False}],
        ),
    ]

    class FakeManager:
        def list_generations(self):
            return list(history_state)

        def start_generation(self, description, *, config_file=None):
            started.append((description, config_file))
            return SimpleNamespace(
                gen_id="gen_007",
                git_commit="a" * 40,
                git_branch="main",
                worktree_path=str(tmp_path / "wt"),
                state_dir=str(tmp_path / "state"),
                description=description,
            )

    monkeypatch.setattr(gm, "GenerationManager", lambda repo: FakeManager())
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyValidCircuitKey1234")

    # Last 2 gens (gen_005: 1 completed, gen_006: 0 completed) -> trips without --force-circuit-breaker
    monkeypatch.setattr(
        "sys.argv",
        ["run_generations.py", "start", "cutover blocked", "--config-file", str(GEMINI4_PROFILE)],
    )
    with pytest.raises(SystemExit) as caught:
        run_generations._main()
    assert caught.value.code == 2
    assert started == []
    stderr = capsys.readouterr().err
    assert "gen_005" in stderr and "gen_006" in stderr
    assert "--force-circuit-breaker" in stderr

    # Explicit --force-circuit-breaker overrides and starts cleanly
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_generations.py",
            "start",
            "forced cutover",
            "--config-file",
            str(GEMINI4_PROFILE),
            "--force-circuit-breaker",
        ],
    )
    run_generations._main()
    assert len(started) == 1
    assert started[0] == ("forced cutover", str(GEMINI4_PROFILE))

    # Once gen_006 completes >= 2 distinct sessions, circuit breaker does NOT trip
    history_state[-1] = SimpleNamespace(
        gen_id="gen_006",
        run_history=[
            {"date": "2026-11-27", "outcome": "clean", "success": True},
            {"date": "2026-11-30", "outcome": "clean", "success": True},
        ],
    )
    monkeypatch.setattr(
        "sys.argv",
        ["run_generations.py", "start", "normal cutover", "--config-file", str(GEMINI4_PROFILE)],
    )
    run_generations._main()
    assert len(started) == 2
    assert started[1] == ("normal cutover", str(GEMINI4_PROFILE))


def test_recover_runtime_tick_fails_closed_on_idle_tick_when_gemini_key_missing(tmp_path, monkeypatch):
    from scripts import recover_runtime as controller

    repo = tmp_path.resolve()
    manifest = repo / "data" / "generations" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    frozen = repo / "data" / "generations" / "gen_007" / "generation-config.json"
    frozen.parent.mkdir(parents=True)
    body = GEMINI4_PROFILE.read_bytes()
    frozen.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    row = {
        "gen_id": "gen_007",
        "git_commit": "a" * 40,
        "config_sha256": digest,
        "state_dir": str(frozen.parent),
        "worktree_path": str(repo / "worktree"),
        "config_file": str(frozen),
        "status": "active",
    }
    manifest.write_text(json.dumps({"generations": [row]}), encoding="utf-8")

    monkeypatch.setattr(controller, "canonical_runtime_lock_path", lambda r: r / "runtime.lock")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    idle_time = datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc)
    result = controller.tick(repo, idle_time, clock=lambda: idle_time)
    assert result["status"] == "operator_required"
    assert result["controller_reason_code"] == "auth_required"
    status_path = repo / "data" / "runtime_recovery" / "status.json"
    assert status_path.is_file()
    assert json.loads(status_path.read_text(encoding="utf-8"))["controller_reason_code"] == "auth_required"

    # main() exits 1 on operator_required
    monkeypatch.setattr("sys.argv", ["recover_runtime.py", "--repo-root", str(repo)])
    with pytest.raises(SystemExit) as caught:
        controller.main()
    assert caught.value.code == 1

    # Once GEMINI_API_KEY is present, healthy idle tick performs 0 git/sqlite/fsync
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyHealthyKey123456")
    status_path.unlink()

    def forbid(*_a, **_kw):
        raise AssertionError("healthy idle tick must not fork git, open sqlite, or write files")

    monkeypatch.setattr(controller, "identity_for", forbid)
    monkeypatch.setattr(controller, "_discover_sessions", forbid)
    monkeypatch.setattr(controller, "atomic_json", forbid)
    healthy = controller.tick(repo, idle_time, clock=lambda: idle_time)
    assert healthy["status"] == "healthy"
    assert healthy["controller_reason_code"] is None
    assert not status_path.exists()
