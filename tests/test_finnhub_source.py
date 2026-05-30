"""Tests for FinnhubSource rate-limit retry.

The free tier (60/min) can return 429 mid-burst during the full multi-source
fetch, which the source previously swallowed as [] — silencing supply_chain.
These tests pin the retry-on-429 behavior (all mocked, no network).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tradingagents.strategies.data_sources.finnhub_source import FinnhubSource


class _RateLimitError(Exception):
    """Mimics finnhub.FinnhubAPIException carrying a status_code."""

    def __init__(self, status_code: int = 429):
        super().__init__(f"API limit reached. status {status_code}")
        self.status_code = status_code


def test_call_with_retry_retries_on_429_then_succeeds():
    src = FinnhubSource(api_key="k")
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _RateLimitError(429)
        return "ok"

    with patch("time.sleep"):
        assert src._call_with_retry(flaky) == "ok"
    assert calls["n"] == 3  # two retries, third succeeds


def test_call_with_retry_does_not_retry_non_rate_limit():
    src = FinnhubSource(api_key="k")
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        err = Exception("invalid symbol")
        err.status_code = 400
        raise err

    with patch("time.sleep"):
        with pytest.raises(Exception):
            src._call_with_retry(bad)
    assert calls["n"] == 1  # no retry on non-429


def test_fetch_company_news_recovers_after_transient_429():
    src = FinnhubSource(api_key="k")
    seq = [
        _RateLimitError(429),
        [{"headline": "Memory shortage", "summary": "S", "source": "x",
          "datetime": 1, "url": "u", "category": "c"}],
    ]

    def company_news(symbol, _from=None, to=None):
        v = seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    fake_client = MagicMock()
    fake_client.company_news.side_effect = company_news
    src._client = fake_client

    with patch("time.sleep"):
        res = src.fetch_company_news("AAPL", "2026-05-01", "2026-05-08")
    assert len(res) == 1
    assert res[0]["headline"] == "Memory shortage"
