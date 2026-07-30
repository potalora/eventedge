"""Deterministic tests for FinnhubSource reliability.

The free tier (60/min) can return 429 mid-burst during the full multi-source
fetch, which the source previously swallowed as [] — silencing supply_chain.
The earnings calendar and company-peers endpoints can also be slow. All tests
here use a fake requests session; no network or credentials are used.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from tradingagents.strategies.data_sources.finnhub_source import FinnhubSource


class _RateLimitError(Exception):
    """Mimics finnhub.FinnhubAPIException carrying a status_code."""

    def __init__(self, status_code: int = 429):
        super().__init__(f"API limit reached. status {status_code}")
        self.status_code = status_code


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class _FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.exceptions.HTTPError(f"status {self.status_code}")
            error.response = self
            raise error

    def json(self) -> object:
        return self.payload


class _FakeSession:
    """Deterministic requests.Session seam recording request start times."""

    def __init__(self, effects: list[object], clock: _FakeClock | None = None) -> None:
        self.effects = list(effects)
        self.clock = clock
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs):
        self.calls.append({
            "url": url,
            "params": kwargs["params"],
            "timeout": kwargs["timeout"],
            "started_at": self.clock.now if self.clock is not None else None,
        })
        effect = self.effects.pop(0)
        if callable(effect):
            effect = effect(url, kwargs)
        if isinstance(effect, BaseException):
            raise effect
        if isinstance(effect, _FakeResponse):
            return effect
        return _FakeResponse(effect)


def _reliability_config(**overrides) -> dict:
    config = {
        "rate_delay_s": 0.0,
        "workflow_budget_s": 20.0,
        "earnings_calendar": {
            "connect_timeout_s": 2.0,
            "read_timeout_s": 12.0,
            "max_attempts": 3,
            "base_backoff_s": 1.0,
            "max_backoff_s": 4.0,
            "jitter_s": 0.0,
        },
        "company_peers": {
            "connect_timeout_s": 2.0,
            "read_timeout_s": 8.0,
            "max_attempts": 2,
            "base_backoff_s": 1.0,
            "max_backoff_s": 4.0,
            "jitter_s": 0.0,
        },
        "company_news": {
            "connect_timeout_s": 2.0,
            "read_timeout_s": 8.0,
            "max_attempts": 3,
            "base_backoff_s": 1.0,
            "max_backoff_s": 4.0,
            "jitter_s": 0.0,
        },
    }
    config.update(overrides)
    return config


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
    session = _FakeSession([
        _FakeResponse({}, 429),
        [{"headline": "Memory shortage", "summary": "S", "source": "x",
          "datetime": 1, "url": "u", "category": "c"}],
    ])
    src = FinnhubSource(
        api_key="k",
        reliability_config=_reliability_config(),
        http_session=session,
    )

    with patch("time.sleep"):
        res = src.fetch_company_news("AAPL", "2026-05-01", "2026-05-08")
    assert len(res) == 1
    assert res[0]["headline"] == "Memory shortage"
    assert len(session.calls) == 2


def test_malformed_earnings_date_degrades_without_starting_request(caplog):
    session = _FakeSession([])
    src = FinnhubSource(
        api_key="not-real",
        reliability_config=_reliability_config(),
        http_session=session,
    )

    with caplog.at_level("INFO"):
        result = src.fetch_earnings_news("AAPL", "not-a-date")

    assert result == []
    assert session.calls == []
    assert "error_type=ValueError" in caplog.text
    assert "error_message=" in caplog.text


def test_documented_adapter_paths_token_and_timeouts_without_private_client():
    session = _FakeSession([
        {"earningsCalendar": [
            {"symbol": "AAPL", "date": "2026-07-30", "epsActual": 1.5},
        ]},
        ["MSFT", "GOOG"],
    ])
    src = FinnhubSource(
        api_key="not-real",
        reliability_config=_reliability_config(),
        http_session=session,
        http_base_url="https://example.test/api/v1",
    )
    src._client = MagicMock()

    assert src.fetch_recent_earnings("2026-07-23", "2026-07-30")
    assert src.fetch_supply_chain("AAPL")

    assert [call["url"] for call in session.calls] == [
        "https://example.test/api/v1/calendar/earnings",
        "https://example.test/api/v1/stock/peers",
    ]
    assert session.calls[0]["params"] == {
        "from": "2026-07-23",
        "to": "2026-07-30",
        "symbol": "",
        "international": "false",
        "token": "not-real",
    }
    assert session.calls[1]["params"] == {
        "symbol": "AAPL",
        "token": "not-real",
    }
    assert session.calls[0]["timeout"] == (2.0, 12.0)
    assert session.calls[1]["timeout"] == (2.0, 8.0)
    assert src._client.mock_calls == []


def test_earnings_calendar_timeout_then_success_uses_inactivity_timeouts(caplog):
    clock = _FakeClock()
    session = _FakeSession([
        requests.exceptions.ReadTimeout("slow earnings calendar"),
        {
            "earningsCalendar": [
                {"symbol": "AAPL", "date": "2026-07-30", "epsActual": 1.5},
                {"symbol": "FUTR", "date": "2026-07-31", "epsActual": None},
            ],
        },
    ])
    src = FinnhubSource(
        api_key="not-real",
        reliability_config=_reliability_config(),
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
        jitter_fn=lambda _maximum: 0.0,
        http_session=session,
    )

    with caplog.at_level("INFO"):
        result = src.fetch_recent_earnings("2026-07-23", "2026-07-30")

    assert [event["symbol"] for event in result] == ["AAPL"]
    assert len(session.calls) == 2
    assert all(call["url"].endswith("/calendar/earnings") for call in session.calls)
    assert session.calls[0]["timeout"] == (2.0, 12.0)
    assert "strategy=earnings_call endpoint=earnings_calendar" in caplog.text
    assert "recovered" in caplog.text
    assert "candidate_count=2 qualifying_count=1" in caplog.text


@pytest.mark.parametrize("status_code", [429, 503])
def test_company_peers_retryable_http_then_success(status_code, caplog):
    clock = _FakeClock()
    session = _FakeSession([
        _FakeResponse({}, status_code),
        ["MSFT", "GOOG"],
    ])
    src = FinnhubSource(
        api_key="not-real",
        reliability_config=_reliability_config(),
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
        jitter_fn=lambda _maximum: 0.0,
        http_session=session,
    )

    with caplog.at_level("INFO"):
        result = src.fetch_supply_chain("AAPL")

    assert result == [
        {"ticker": "MSFT", "relationship": "peer"},
        {"ticker": "GOOG", "relationship": "peer"},
    ]
    assert len(session.calls) == 2
    assert session.calls[0]["timeout"] == (2.0, 8.0)
    assert f"status={status_code}" in caplog.text
    assert "recovered" in caplog.text


def test_earnings_retry_exhaustion_returns_empty_and_logs_final_state(caplog):
    clock = _FakeClock()
    session = _FakeSession([
        requests.exceptions.ReadTimeout("one"),
        requests.exceptions.ReadTimeout("two"),
        requests.exceptions.ReadTimeout("three token=not-real"),
    ])
    src = FinnhubSource(
        api_key="not-real",
        reliability_config=_reliability_config(),
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
        jitter_fn=lambda _maximum: 0.0,
        http_session=session,
    )

    with caplog.at_level("INFO"):
        result = src.fetch_recent_earnings("2026-07-23", "2026-07-30")

    assert result == []
    assert len(session.calls) == 3
    assert sum(clock.sleeps) == 3.0
    assert "exhausted strategy=earnings_call endpoint=earnings_calendar" in caplog.text
    assert "attempt=3/3" in caplog.text
    assert "degraded" in caplog.text
    assert "error_type=ReadTimeout" in caplog.text
    assert "error_message=three token=<redacted>" in caplog.text
    assert "token=not-real" not in caplog.text


def test_permanent_4xx_is_not_retried_and_degrades_gracefully(caplog):
    clock = _FakeClock()
    session = _FakeSession([_FakeResponse({}, 400)])
    src = FinnhubSource(
        api_key="not-real",
        reliability_config=_reliability_config(),
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
        jitter_fn=lambda _maximum: 0.0,
        http_session=session,
    )

    with caplog.at_level("INFO"):
        result = src.fetch_supply_chain("AAPL")

    assert result == []
    assert len(session.calls) == 1
    assert clock.sleeps == []
    assert "non_retryable strategy=supply_chain endpoint=company_peers" in caplog.text
    assert "status=400" in caplog.text
    assert "error_type=HTTPError error_message=status 400" in caplog.text


def test_attempts_and_backoff_are_bounded_by_policy():
    clock = _FakeClock()
    session = _FakeSession([requests.exceptions.ConnectionError("down")] * 10)
    config = _reliability_config()
    config["earnings_calendar"].update({
        "max_attempts": 999,
        "base_backoff_s": 2.0,
        "max_backoff_s": 3.0,
        "jitter_s": 1.0,
    })
    src = FinnhubSource(
        api_key="not-real",
        reliability_config=config,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
        jitter_fn=lambda maximum: maximum,
        http_session=session,
    )

    assert src.fetch_recent_earnings("2026-07-23", "2026-07-30") == []
    assert src._policies["earnings_calendar"].max_attempts == 4
    assert len(session.calls) == 4
    assert clock.sleeps == [3.0, 3.0, 3.0]


def test_supply_chain_batch_success_deduplicates_in_stable_order():
    session = _FakeSession([
        ["MSFT"],
        ["WMT"],
        ["DE"],
    ])
    src = FinnhubSource(
        api_key="not-real",
        reliability_config=_reliability_config(),
        http_session=session,
    )

    result = src.fetch_supply_chains(
        ["AAPL", "", "AAPL", "AMZN", "BA", "AMZN"],
    )

    assert list(result) == ["AAPL", "AMZN", "BA"]
    assert result["AAPL"] == [{"ticker": "MSFT", "relationship": "peer"}]
    assert [call["params"]["symbol"] for call in session.calls] == [
        "AAPL",
        "AMZN",
        "BA",
    ]


def test_supply_chain_batch_continues_after_symbol_failure():
    session = _FakeSession([
        ["MSFT"],
        _FakeResponse({}, 400),
        ["DE"],
    ])
    src = FinnhubSource(
        api_key="not-real",
        reliability_config=_reliability_config(),
        http_session=session,
    )

    result = src.fetch_supply_chains(["AAPL", "AMZN", "BA"])

    assert list(result) == ["AAPL", "BA"]
    assert [call["params"]["symbol"] for call in session.calls] == [
        "AAPL",
        "AMZN",
        "BA",
    ]


def test_batch_retains_success_and_starts_no_retry_or_request_after_deadline(caplog):
    clock = _FakeClock()

    def first_success(_url, _kwargs):
        clock.now = 0.5
        return ["MSFT"]

    def timeout_at_deadline(_url, _kwargs):
        clock.now = 2.0
        raise requests.exceptions.ReadTimeout("slow peers")

    session = _FakeSession(
        [first_success, timeout_at_deadline, ["SHOULD_NOT_BE_REQUESTED"]],
        clock=clock,
    )
    config = _reliability_config(workflow_budget_s=2.0)
    config["company_peers"].update({
        "connect_timeout_s": 1.0,
        "read_timeout_s": 1.0,
        "max_attempts": 3,
        "base_backoff_s": 0.5,
    })
    src = FinnhubSource(
        api_key="not-real",
        reliability_config=config,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
        jitter_fn=lambda _maximum: 0.0,
        http_session=session,
    )

    with caplog.at_level("INFO"):
        result = src.fetch_supply_chains(["AAPL", "AMZN", "BA", "CAT"])

    assert result == {
        "AAPL": [{"ticker": "MSFT", "relationship": "peer"}],
    }
    assert [call["params"]["symbol"] for call in session.calls] == ["AAPL", "AMZN"]
    assert [call["started_at"] for call in session.calls] == [0.0, 0.5]
    assert all(call["started_at"] < 2.0 for call in session.calls)
    assert clock.sleeps == []
    assert "deadline_exhausted=true" in caplog.text
    assert "candidate_count=4" in caplog.text
    assert "qualifying_count=1" in caplog.text


def test_expired_shared_deadline_starts_no_request():
    clock = _FakeClock()
    session = _FakeSession([{"earningsCalendar": []}], clock=clock)
    src = FinnhubSource(
        api_key="not-real",
        reliability_config=_reliability_config(),
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
        http_session=session,
    )

    assert src.fetch_recent_earnings(
        "2026-07-23",
        "2026-07-30",
        deadline=clock.now,
    ) == []
    assert src.fetch_supply_chains(["AAPL"], deadline=clock.now) == {}
    assert session.calls == []
    assert clock.sleeps == []


def test_aggregate_finnhub_fetch_uses_one_deadline_and_preserves_results():
    from tradingagents.strategies.orchestration.multi_strategy_engine import (
        MultiStrategyEngine,
    )

    clock = _FakeClock()

    def calendar_result(_url, _kwargs):
        clock.now = 0.25
        return {
            "earningsCalendar": [
                {
                    "symbol": "AAPL",
                    "date": "2026-07-30",
                    "epsActual": 1.5,
                },
                {
                    "symbol": "MSFT",
                    "date": "2026-07-30",
                    "epsActual": 2.0,
                },
            ],
        }

    def news_finishes_at_deadline(_url, _kwargs):
        clock.now = 2.0
        return [{
            "headline": "Guidance raised",
            "summary": "Demand remains strong",
            "source": "wire",
        }]

    session = _FakeSession(
        [calendar_result, news_finishes_at_deadline],
        clock=clock,
    )
    src = FinnhubSource(
        api_key="not-real",
        reliability_config=_reliability_config(workflow_budget_s=2.0),
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
        jitter_fn=lambda _maximum: 0.0,
        http_session=session,
    )
    engine = object.__new__(MultiStrategyEngine)
    engine.registry = MagicMock()
    engine.registry.get.return_value = src

    result = engine._fetch_finnhub_data("2026-07-30")

    assert [item["symbol"] for item in result["transcripts"]] == ["AAPL"]
    assert [call["url"].rsplit("/", 1)[-1] for call in session.calls] == [
        "earnings",
        "company-news",
    ]
    assert [call["started_at"] for call in session.calls] == [0.0, 0.25]
    assert all(call["started_at"] < 2.0 for call in session.calls)
    assert "disruption_news" not in result
    assert "supply_chains" not in result
    assert "pqc_news" not in result
