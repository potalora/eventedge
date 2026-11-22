from __future__ import annotations

import json
import stat
from datetime import datetime, timezone

import pytest

from tradingagents.strategies.orchestration.run_evidence import (
    persist_run_evidence,
    sanitize_evidence_text,
)


def _evidence() -> dict:
    return {
        "schema_version": 1,
        "generation_id": "gen_001",
        "generation_commit": "a" * 40,
        "requested_session": "2026-09-06",
        "action": "daily",
        "preflight_mode": None,
        "started_at": "2026-09-06T12:00:00+00:00",
        "finished_at": "2026-09-06T12:00:01+00:00",
        "process_return_code": 0,
        "process_status": "completed",
        "stdout": "worker output",
        "stderr": "",
        "result": {"outcome": "clean", "success": True, "elapsed_s": 1.0},
    }


def test_persist_run_evidence_creates_unique_immutable_files(tmp_path):
    first_path = persist_run_evidence(tmp_path, _evidence())
    first_bytes = first_path.read_bytes()

    second_path = persist_run_evidence(tmp_path, _evidence())

    assert first_path.parent == tmp_path / "data" / "logs" / "run_attempts"
    assert second_path.parent == first_path.parent
    assert second_path != first_path
    assert first_path.read_bytes() == first_bytes
    assert json.loads(first_bytes) == _evidence()


def test_persist_run_evidence_redacts_log_text_only_and_restricts_mode(
    tmp_path, monkeypatch
):
    env_secret = "environment-secret-value"
    evidence = _evidence()
    evidence.update(
        {
            "stdout": (
                f"raw={env_secret}\n"
                "Authorization: Bearer header-secret\n"
                '{"Authorization": "Bearer quoted-header-secret"}\n'
                "https://login-user:url-password@provider.test/private\n"
                "https://provider.test/data?api_key=query-secret&symbol=SPY"
            ),
            "stderr": "password=stderr-secret",
            "result": {
                "outcome": "clean",
                "success": True,
                "flag": "true",
                "nested": {"count": 1234, "empty": None},
            },
        }
    )
    monkeypatch.setenv("OPENAI_API_KEY", env_secret)
    # Short or non-allowlisted env vars must never redact booleans/null/short words.
    monkeypatch.setenv("FINNHUB_API_KEY", "true")
    monkeypatch.setenv("RANDOM_TOKEN", "false")
    monkeypatch.setenv("UNLISTED_SECRET_VAR", "should-not-corrupt-text")

    path = persist_run_evidence(tmp_path, evidence)
    raw = path.read_text()
    payload = json.loads(raw)

    for secret in (
        env_secret,
        "header-secret",
        "quoted-header-secret",
        "login-user",
        "url-password",
        "query-secret",
        "stderr-secret",
    ):
        assert secret not in payload["stdout"]
        assert secret not in payload["stderr"]
    assert payload["stdout"].count("<redacted>") >= 5
    assert payload["stderr"] == "password=<redacted>"
    # Structured result JSON is preserved verbatim (never corrupted by log redaction).
    assert payload["result"] == evidence["result"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "environment" not in payload


def test_short_secret_values_and_literals_are_never_redacted(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "true")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "false")
    monkeypatch.setenv("FRED_API_KEY", "null")
    monkeypatch.setenv("FINNHUB_API_KEY", "1234567")  # < 8 chars
    text = '{"ok": true, "failed": false, "value": null, "id": 1234567}'
    assert sanitize_evidence_text(text) == text


def test_persist_run_evidence_accepts_timeout_byte_streams(tmp_path):
    evidence = _evidence()
    evidence.update(
        {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "process_return_code": None,
            "process_status": "timeout",
            "stdout": b"partial stdout \xff",
            "stderr": b"partial stderr \xfe",
        }
    )

    path = persist_run_evidence(tmp_path, evidence)
    payload = json.loads(path.read_text())

    assert payload["stdout"] == "partial stdout \ufffd"
    assert payload["stderr"] == "partial stderr \ufffd"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('password="multi word, secret value"', 'password="<redacted>"'),
        (
            'password="escaped \\" quote, secret value"',
            'password="<redacted>"',
        ),
        (
            '{"Authorization": "Bearer multi word, secret value"}',
            '{"Authorization": "<redacted>"}',
        ),
        (
            "Authorization='Basic multi word, secret value'",
            "Authorization='<redacted>'",
        ),
    ],
)
def test_sanitize_evidence_text_redacts_quoted_values(value, expected):
    assert sanitize_evidence_text(value) == expected
