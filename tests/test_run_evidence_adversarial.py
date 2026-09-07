"""Deterministic sanitizer probes; every credential in this file is synthetic.

The tests cover the documented known-environment/common-auth contract. They do
not claim detection of arbitrary unknown secrets or reversible encodings.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from tradingagents.strategies.orchestration import run_evidence


@pytest.fixture(autouse=True)
def synthetic_environment_only(monkeypatch):
    # Keep the tests independent of developer credentials and incidental values.
    monkeypatch.setattr(run_evidence.os, "environ", {})


_NAMES = [
    "api_key",
    "api-key",
    "apikey",
    "API_KEY",
    "ApiKey",
    "x-api-key",
    "access_token",
    "access-token",
    "auth_token",
    "auth-token",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
]
_VALUES = [
    "synthetic-a9!+/=",
    "synthetic space, comma",
    'synthetic"quote',
    "synthetic\\backslash",
    "synthetic雪🔐",
]


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("value", _VALUES)
def test_common_named_credentials_in_json_and_repr(name, value):
    for original in [
        json.dumps({name: value, "symbol": "SPY"}),
        repr({name: value, "symbol": "SPY"}),
    ]:
        sanitized = run_evidence.sanitize_evidence_text(original)
        assert "synthetic" not in sanitized
        assert "SPY" in sanitized
        assert "<redacted>" in sanitized


@pytest.mark.parametrize("scheme", ["Bearer", "Basic", "Token", "bEaReR"])
@pytest.mark.parametrize("header", ["Authorization", "Proxy-Authorization"])
def test_plain_common_auth_headers(scheme, header):
    sanitized = run_evidence.sanitize_evidence_text(
        f"{header}: {scheme} synthetic-a9+/=\r\nstatus=401 symbol=SPY"
    )
    assert "synthetic" not in sanitized
    assert "status=401 symbol=SPY" in sanitized


@pytest.mark.parametrize("scheme", ["http", "https", "postgresql", "redis", "socks5"])
def test_url_userinfo_preserves_host_and_query(scheme):
    value = f"{scheme}://synthetic-user:p%40ss%2Fsynthetic@[::1]:1234/data?symbol=SPY"
    sanitized = run_evidence.sanitize_evidence_text(value)
    assert "synthetic" not in sanitized
    assert sanitized == f"{scheme}://<redacted>@[::1]:1234/data?symbol=SPY"


@pytest.mark.parametrize("name", ["api_key", "access_token", "token", "password"])
@pytest.mark.parametrize("value", ["synthetic-value", quote("synthetic space/雪,+&=")])
def test_url_query_credentials_preserve_adjacent_parameters(name, value):
    original = f"https://provider.test/data?symbol=SPY&{name}={value}&limit=42"
    assert run_evidence.sanitize_evidence_text(original) == (
        f"https://provider.test/data?symbol=SPY&{name}=<redacted>&limit=42"
    )


@pytest.mark.parametrize(
    "secret",
    [
        'synthetic"quote',
        "synthetic'quote",
        "synthetic\\backslash",
        "synthetic\nnewline",
        "synthetic\ttab",
        "synthetic雪🔐",
    ],
)
@pytest.mark.parametrize(
    "serialize", [str, repr, json.dumps], ids=["plain", "repr", "json"]
)
def test_known_environment_values_survive_serialization_redaction(
    monkeypatch, secret, serialize
):
    monkeypatch.setenv("PROVIDER_API_KEY", secret)
    original = f"error: rejected value {serialize(secret)}; status=401 symbol=SPY"
    sanitized = run_evidence.sanitize_evidence_text(original)
    assert "synthetic" not in sanitized
    assert "status=401 symbol=SPY" in sanitized


@pytest.mark.parametrize("prefix", ["password=", "api_key=", '"access_token": '])
@pytest.mark.parametrize(
    "value",
    ['"synthetic incomplete', "'synthetic incomplete", '"synthetic\\"incomplete'],
)
def test_truncated_quoted_credentials_are_redacted(prefix, value):
    sanitized = run_evidence.sanitize_evidence_text(prefix + value)
    assert "synthetic" not in sanitized
    assert "incomplete" not in sanitized


@pytest.mark.parametrize(
    "auth",
    [
        'Digest username="synthetic-user", nonce="synthetic-nonce", response="synthetic-response"',
        "AWS4-HMAC-SHA256 Credential=synthetic-id/date/region/service/aws4_request, SignedHeaders=host, Signature=synthetic-signature",
    ],
)
def test_full_unquoted_authorization_header_is_sensitive(auth):
    sanitized = run_evidence.sanitize_evidence_text(
        f"Authorization: {auth}\nstatus=401 symbol=SPY"
    )
    assert "synthetic" not in sanitized
    assert "status=401 symbol=SPY" in sanitized


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_API_KEY",
        "ALPACA_SECRET_KEY",
        "client_secret",
        "refresh_token",
        "X-Auth-Token",
    ],
)
def test_common_provider_credential_names_in_text(name):
    sanitized = run_evidence.sanitize_evidence_text(
        json.dumps({name: "synthetic-provider-value", "status": 401})
    )
    assert "synthetic" not in sanitized
    assert "401" in sanitized


@pytest.mark.parametrize("depth", [1, 25, 100])
def test_nested_payloads_redact_at_every_depth_and_preserve_outcomes(depth):
    payload = {"password": "synthetic-deep", "outcome": "no_signal", "success": True}
    for _ in range(depth):
        payload = {"layers": [payload, {"symbol": "SPY", "api_key": "synthetic-key"}]}
    sanitized = run_evidence._sanitize(payload, ())
    serialized = json.dumps(sanitized)
    assert "synthetic" not in serialized
    assert '"outcome": "no_signal"' in serialized
    assert '"success": true' in serialized
    assert serialized.count('"SPY"') == depth


def test_known_environment_values_in_dynamic_result_keys_are_redacted(monkeypatch):
    monkeypatch.setenv("PROVIDER_API_KEY", "synthetic-dynamic-key")
    sanitized = run_evidence._sanitize(
        {"by_request": {"synthetic-dynamic-key": {"status": 401}}},
        run_evidence._credential_values(),
    )
    assert "synthetic" not in json.dumps(sanitized)
    assert "401" in json.dumps(sanitized)


@pytest.mark.parametrize(
    "diagnostic",
    [
        "status=401 symbol=SPY retry_after=30",
        "TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'",
        'File "worker.py", line 42, in execute',
        "Unicode provider error: 日本語 café 🔎",
        "prompt_tokens=128 completion_tokens=16 token_count=144",
    ],
)
def test_plain_diagnostic_text_is_preserved(diagnostic):
    assert run_evidence.sanitize_evidence_text(diagnostic) == diagnostic


def test_structured_token_usage_diagnostics_are_preserved():
    diagnostics = {"prompt_tokens": 128, "completion_tokens": 16, "token_count": 144}
    assert run_evidence._sanitize(diagnostics, ()) == diagnostics


def test_bytes_invalid_utf8_and_known_credentials_are_safely_handled(monkeypatch):
    monkeypatch.setenv("PROVIDER_API_KEY", "synthetic-byte-value")
    assert (
        run_evidence.sanitize_evidence_text(
            b"prefix\xff synthetic-byte-value suffix\xfe"
        )
        == "prefix\ufffd <redacted> suffix\ufffd"
    )


@pytest.mark.parametrize(
    "name", ["AWS_SECRET_ACCESS_KEY", "APCA_API_KEY_ID", "API_KEY_PRIMARY"]
)
def test_credential_names_with_suffixes_remain_sensitive(monkeypatch, name):
    monkeypatch.setenv(name, "synthetic-suffix-secret")
    assert run_evidence.sanitize_evidence_text("rejected synthetic-suffix-secret") == (
        "rejected <redacted>"
    )
    assert "synthetic" not in run_evidence.sanitize_evidence_text(
        f"{name}=synthetic-unknown-secret"
    )
    assert run_evidence._sanitize({name: "synthetic-unknown-secret"}, ()) == {
        name: "<redacted>"
    }


def test_overlapping_environment_secrets_are_fully_redacted(monkeypatch):
    monkeypatch.setenv("PROVIDER_API_KEY", "synthetic-overlap")
    monkeypatch.setenv("PROVIDER_SECRET", "synthetic-overlap-extended")
    assert (
        run_evidence.sanitize_evidence_text(
            "synthetic-overlap-extended synthetic-overlap"
        )
        == "<redacted> <redacted>"
    )
