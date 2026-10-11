"""Offline full-population SIP acquisition and completeness regressions."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest
import requests

from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.execution.alpaca_daily_bar import AlpacaHistoricalSIPSource
from tradingagents.strategies.execution.price_source import AlpacaSIPPriceSource
from test_request_policy import Clock

SESSION = date(2026, 10, 9)
NOW = datetime(2026, 10, 10, 1, tzinfo=timezone.utc)


def row(**changes):
    # Synthetic valid positive activity; no minimum liquidity threshold.
    return {"t": "2026-10-09T04:00:00Z", "o": "1", "h": "3", "l": "1", "c": "2", "v": 1, "n": 1, **changes}


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status
        self.headers = {}
        self.content = json.dumps(body).encode()
        self.closed = 0

    def json(self, **kwargs):
        return deepcopy(self.body)

    def iter_content(self, chunk_size=65536):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset:offset + chunk_size]

    def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "offline-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "offline-secret")


def resolve(source, tickers):
    return source.resolve_candidate_daily_bars(tickers, SESSION, NOW, timedelta(hours=24))


def test_all_1466_symbols_fit_original_budget_in_fifteen_requests():
    """A return to scalar acquisition exhausts 200/minute slots after 1,000."""
    clock, calls, responses, diagnostics = Clock(), [], [], []
    symbols = [f"S{i:04}" for i in range(1466)]

    def get(url, **kwargs):
        calls.append((url, kwargs))
        requested = kwargs["params"].get("symbols", "").split(",")
        body = {"bars": {symbol: [row()] for symbol in reversed(requested)}, "next_page_token": None}
        response = Response(body)
        responses.append(response)
        clock.now += .06
        return response

    source = AlpacaSIPPriceSource(sip_source=AlpacaHistoricalSIPSource(get=get), now=lambda: NOW)
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep,
                         random_fn=lambda: 0, diagnostics=diagnostics):
        result = resolve(source, symbols + [symbols[0]])
    assert len(result.bars) == 1466
    assert result.quarantined_tickers == frozenset()
    assert [attempt.ticker for attempt in result.attempts] == symbols
    assert len(calls) == 15
    assert all(url == "https://data.alpaca.markets/v2/stocks/bars" for url, _ in calls)
    assert [symbol for _, options in calls for symbol in options["params"]["symbols"].split(",")] == symbols
    assert [len(options["params"]["symbols"].split(",")) for _, options in calls] == [100] * 14 + [66]
    assert all(response.closed == 1 for response in responses)
    assert all(item["attempts"] == 1 for item in diagnostics)
    assert clock.now < 1 and clock.waits == []
    assert result.bars[symbols[-1], SESSION].close == Decimal("2")
    # Successful exact-session responses are reused, with no second HTTP call.
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, random_fn=lambda: 0):
        again = resolve(source, symbols)
    assert again.bars == result.bars and len(calls) == 15


def test_batch_pagination_maps_exact_symbols_and_preserves_request_contract():
    responses = [Response({"bars": {"ICE": [row(c="3")]}, "next_page_token": "second"}),
                 Response({"bars": {"BRC": [row()]}, "next_page_token": None})]
    calls = []

    def get(url, **kwargs):
        calls.append((url, deepcopy(kwargs)))
        return responses[len(calls) - 1]

    clock = Clock()
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, random_fn=lambda: 0):
        result = AlpacaHistoricalSIPSource(get=get).fetch_daily_bars(["BRC", "ICE"], SESSION, now=NOW)
    assert result["BRC"].bar.close == Decimal("2")
    assert result["ICE"].bar.close == Decimal("3")
    assert all(item.pagination_complete for item in result.values())
    expected = {"symbols": "BRC,ICE", "feed": "sip", "adjustment": "raw", "timeframe": "1Day",
                "start": "2026-10-09T00:00:00-04:00", "end": "2026-10-10T00:45:00+00:00",
                "asof": "-", "currency": "USD", "sort": "asc", "limit": 201}
    assert calls[0][1]["params"] == expected
    assert calls[1][1]["params"] == {**expected, "page_token": "second"}
    assert all(call[1]["allow_redirects"] is False for call in calls)
    assert all(response.closed == 1 for response in responses)


def test_short_pages_can_cover_each_of_one_hundred_requested_symbols():
    symbols = [f"S{i:04}" for i in range(100)]
    responses = []
    clock = Clock()

    def get(*args, **kwargs):
        index = len(responses)
        response = Response({"bars": {symbols[index]: [row()]},
                             "next_page_token": str(index + 1) if index < 99 else None})
        responses.append(response)
        return response

    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, random_fn=lambda: 0):
        results = AlpacaHistoricalSIPSource(get=get).fetch_daily_bars(symbols, SESSION, now=NOW)
    assert len(results) == 100 and all(item.bar is not None for item in results.values())
    assert len(responses) == 100 and all(response.closed == 1 for response in responses)


@pytest.mark.parametrize("bad", [
    {"bars": {"OTHER": [row()]}, "next_page_token": None},
    {"bars": {"ICE": [row()]}},
    {"bars": {"ICE": [row()]}, "next_page_token": "second"},
    {"bars": {"ICE": [row()]}, "next_page_token": ""},
    {"bars": [], "next_page_token": None},
])
def test_incomplete_or_ambiguous_batch_does_not_publish_good_prefix(bad):
    pages = [Response({"bars": {"BRC": [row()]}, "next_page_token": "second"}), Response(bad)]
    calls = []

    def get(*args, **kwargs):
        calls.append(kwargs)
        return pages[min(len(calls) - 1, 1)]

    result = AlpacaHistoricalSIPSource(get=get).fetch_daily_bars(["BRC", "ICE"], SESSION, now=NOW)
    assert set(result) == {"BRC", "ICE"}
    assert all(item.bar is None for item in result.values())
    assert len(calls) == 2
    assert all(response.closed == 1 for response in pages)


@pytest.mark.parametrize("bad_rows", [
    [row(), row()], [row(t="2026-10-08T04:00:00Z")],
    [row(t="2026-10-09T13:30:00Z")], [row(t="2026-10-09T00:00:00")],
    [row(h="1")], [row(l="3")], [row(c="NaN")], [row(o=0)], [row(c=True)],
    [None], "not-a-list",
])
def test_bad_known_symbol_is_quarantined_without_discarding_valid_sibling(bad_rows):
    response = Response({"bars": {"ICE": bad_rows, "BRC": [row()]}, "next_page_token": None})
    source = AlpacaSIPPriceSource(sip_source=AlpacaHistoricalSIPSource(get=lambda *a, **kw: response), now=lambda: NOW)
    result = resolve(source, ["BRC", "ICE"])
    assert set(result.bars) == {("BRC", SESSION)}
    assert result.quarantined_tickers == frozenset({"ICE"})
    assert result.attempts[1].validation_error.startswith("invalid_response ICE/2026-10-09")
    assert response.closed == 1


@pytest.mark.parametrize("bars", [{"BRC": [row()]}, {"BRC": [row()], "ICE": []}])
def test_complete_batch_missing_symbol_is_explicit_missing_and_not_cached(bars):
    calls = []

    def get(*args, **kwargs):
        calls.append(kwargs)
        return Response({"bars": bars if len(calls) == 1 else {"ICE": [row()]},
                         "next_page_token": None})

    source = AlpacaSIPPriceSource(sip_source=AlpacaHistoricalSIPSource(get=get), now=lambda: NOW)
    first = resolve(source, ["BRC", "ICE"])
    assert set(first.bars) == {("BRC", SESSION)}
    assert first.attempts[1].validation_error.startswith("missing ICE/2026-10-09")
    second = resolve(source, ["BRC", "ICE"])
    assert set(second.bars) == {("BRC", SESSION), ("ICE", SESSION)}
    assert len(calls) == 2 and calls[1]["params"]["symbols"] == "ICE"


def test_batch_deadline_does_not_reset_or_publish_partial_pagination():
    clock, calls = Clock(), []

    def get(*args, **kwargs):
        calls.append(kwargs)
        clock.now = 300
        return Response({"bars": {"BRC": [row()]}, "next_page_token": "second"})

    source = AlpacaSIPPriceSource(sip_source=AlpacaHistoricalSIPSource(get=get), now=lambda: NOW)
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, random_fn=lambda: 0):
        result = resolve(source, ["BRC", "ICE"])
    assert result.bars == {} and len(calls) == 1
    assert all(attempt.validation_error.startswith("provider_error ") for attempt in result.attempts)
    assert all("reason=timeout" in attempt.validation_error and "attempts=0" in attempt.validation_error for attempt in result.attempts)


def test_batch_provider_error_preserves_only_safe_fixed_diagnostics():
    clock, diagnostics = Clock(), []
    source = AlpacaSIPPriceSource(sip_source=AlpacaHistoricalSIPSource(
        get=lambda *a, **kw: (_ for _ in ()).throw(requests.Timeout("PRIVATE_SECRET"))), now=lambda: NOW)
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep,
                         random_fn=lambda: 0, diagnostics=diagnostics):
        result = resolve(source, ["BRC", "ICE"])
    assert result.bars == {}
    assert "PRIVATE_SECRET" not in repr(result)
    assert all("reason=timeout" in attempt.validation_error and "attempts=3" in attempt.validation_error for attempt in result.attempts)


def test_legacy_scalar_failure_string_is_unchanged():
    from tradingagents.strategies.execution.alpaca_daily_bar import AlpacaDailyBarResult, AlpacaBarFailure

    class LegacySource:
        def fetch_daily_bar(self, *args, **kwargs):
            return AlpacaDailyBarResult(None, AlpacaBarFailure.TRANSPORT_ERROR)

    result = resolve(AlpacaSIPPriceSource(sip_source=LegacySource(), now=lambda: NOW), ["BRC"])
    assert result.attempts[0].validation_error == "transport_error BRC/2026-10-09"


@pytest.mark.parametrize("limit", ["pages", "rows", "bytes"])
def test_batch_limits_fail_closed_and_close_every_received_page(limit):
    from tradingagents.strategies.execution.alpaca_daily_bar import MAX_BATCH_BYTES

    responses = []

    def get(*args, **kwargs):
        response = Response({"bars": {"BRC": [row()]}, "next_page_token": None})
        if limit == "pages":
            response = Response({"bars": {}, "next_page_token": f"page-{len(responses) + 1}"})
        elif limit == "rows":
            response = Response({"bars": {"BRC": [row()] * 10001}, "next_page_token": None})
        else:
            response.content = b" " * (MAX_BATCH_BYTES + 1)
        responses.append(response)
        return response

    clock = Clock()
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, random_fn=lambda: 0):
        result = AlpacaHistoricalSIPSource(get=get).fetch_daily_bars(["BRC", "ICE"], SESSION, now=NOW)
    assert all(item.bar is None and item.reason_code == "invalid_response" for item in result.values())
    assert len(responses) == (128 if limit == "pages" else 1)
    assert all(response.closed == 1 for response in responses)


def test_repeated_symbol_across_pages_is_not_truncated_to_one_row():
    pages = [Response({"bars": {"BRC": [row()]}, "next_page_token": "next"}),
             Response({"bars": {"BRC": [row()], "ICE": [row()]}, "next_page_token": None})]
    source = AlpacaHistoricalSIPSource(get=lambda *a, **kw: pages.pop(0))
    result = source.fetch_daily_bars(["BRC", "ICE"], SESSION, now=NOW)
    assert result["BRC"].bar is None
    assert result["ICE"].bar.close == Decimal("2")


@pytest.mark.parametrize("status", [401, 403, 422])
def test_batch_http_failure_retains_status_without_parsing_body(status):
    response = Response({"private": "PRIVATE_SECRET"}, status)
    source = AlpacaSIPPriceSource(sip_source=AlpacaHistoricalSIPSource(get=lambda *a, **kw: response), now=lambda: NOW)
    result = resolve(source, ["BRC", "ICE"])
    assert result.bars == {} and response.closed == 1
    assert all(f"http_status={status}" in attempt.validation_error for attempt in result.attempts)
    assert all("attempts=1" in attempt.validation_error for attempt in result.attempts)
    assert "PRIVATE_SECRET" not in repr(result)


@pytest.mark.parametrize("scope", ["invalid_symbol", "early", "credentials"])
def test_invalid_batch_scope_never_touches_transport(scope, monkeypatch):
    symbols, now = ["BRC", "ICE"], NOW
    if scope == "invalid_symbol":
        symbols = ["BRC", "../ICE"]
    elif scope == "early":
        now = datetime(2026, 10, 9, 20, 14, tzinfo=timezone.utc)
    else:
        monkeypatch.delenv("ALPACA_API_KEY")

    def forbidden(*args, **kwargs):
        pytest.fail("invalid scope must not acquire market data")

    results = AlpacaHistoricalSIPSource(get=forbidden).fetch_daily_bars(symbols, SESSION, now=now)
    assert set(results) == set(symbols)
    assert all(item.bar is None for item in results.values())


def test_success_cache_expires_and_batch_refreshes_full_original_symbols():
    calls = []
    clock = [NOW]

    def get(*args, **kwargs):
        calls.append(kwargs)
        return Response({"bars": {"BRC": [row()], "ICE": [row()]}, "next_page_token": None})

    source = AlpacaSIPPriceSource(sip_source=AlpacaHistoricalSIPSource(get=get), now=lambda: clock[0])
    first = resolve(source, ["BRC", "ICE"])
    clock[0] += timedelta(hours=25)
    second = source.resolve_candidate_daily_bars(["BRC", "ICE"], SESSION, clock[0], timedelta(hours=24))
    assert len(calls) == 2 and calls[1]["params"]["symbols"] == "BRC,ICE"
    assert second.bars["BRC", SESSION].fetched_at == clock[0]
    assert second.bars["BRC", SESSION].fetched_at != first.bars["BRC", SESSION].fetched_at


@pytest.mark.parametrize("mutation", ["symbol", "stale", "future", "feed"])
def test_price_source_revalidates_batch_provider_evidence(mutation):
    from dataclasses import replace

    native = AlpacaHistoricalSIPSource(get=lambda *a, **kw: Response({
        "bars": {"BRC": [row()], "ICE": [row()]}, "next_page_token": None,
    }))
    observations = native.fetch_daily_bars(["BRC", "ICE"], SESSION, now=NOW)
    bad = observations["ICE"]
    if mutation == "symbol":
        bad = replace(bad, bar=replace(bad.bar, ticker="BRC"))
    elif mutation == "stale":
        bad = replace(bad, bar=replace(bad.bar, fetched_at=NOW - timedelta(days=2)))
    elif mutation == "future":
        bad = replace(bad, bar=replace(bad.bar, fetched_at=NOW + timedelta(days=2)))
    else:
        bad = replace(bad, feed="iex")

    class Provider:
        def fetch_daily_bars(self, *args, **kwargs):
            return {**observations, "ICE": bad}

        def fetch_daily_bar(self, *args, **kwargs):
            pytest.fail("batch failure must not trigger scalar fallback")

    result = resolve(AlpacaSIPPriceSource(sip_source=Provider(), now=lambda: NOW), ["BRC", "ICE"])
    assert set(result.bars) == {("BRC", SESSION)}
    assert result.quarantined_tickers == frozenset({"ICE"})


def test_json_duplicate_identity_is_rejected_and_response_closed():
    class DuplicateResponse(Response):
        def __init__(self, body):
            super().__init__(body)
            self.content = b'{"bars":{"BRC":[],"BRC":[]},"next_page_token":null}'

    response = DuplicateResponse({})
    results = AlpacaHistoricalSIPSource(get=lambda *a, **kw: response).fetch_daily_bars(["BRC", "ICE"], SESSION, now=NOW)
    assert all(item.bar is None for item in results.values())
    assert response.closed == 1


def test_response_cleanup_exception_cannot_leak_private_provider_text():
    class BrokenClose(Response):
        def close(self):
            super().close()
            raise RuntimeError("PRIVATE_SECRET")

    response = BrokenClose({"bars": {"BRC": [row()], "ICE": [row()]}, "next_page_token": None})
    results = AlpacaHistoricalSIPSource(get=lambda *a, **kw: response).fetch_daily_bars(["BRC", "ICE"], SESSION, now=NOW)
    assert all(item.bar is not None for item in results.values())
    assert response.closed == 1 and "PRIVATE_SECRET" not in repr(results)


@pytest.mark.parametrize("kind,category", [("legacy", "invalid_data"), ("timeout", "provider_error"), ("missing", "missing_data")])
def test_old_and_new_failure_evidence_replays_without_schema_or_payload_change(tmp_path, kind, category):
    from dataclasses import asdict
    from types import SimpleNamespace
    from tradingagents.strategies.metrics.models import CandidateBarRecoveryRecord
    from tradingagents.strategies.metrics.store import MetricStore
    from tradingagents.strategies.orchestration.daily_pipeline import (
        _candidate_reference_issue, _replay_candidate_reference_issues,
    )

    if kind == "legacy":
        from tradingagents.strategies.execution.alpaca_daily_bar import AlpacaDailyBarResult, AlpacaBarFailure

        class Legacy:
            def fetch_daily_bar(self, *args, **kwargs):
                return AlpacaDailyBarResult(None, AlpacaBarFailure.TRANSPORT_ERROR)

        provider = Legacy()
    else:
        def get(*args, **kwargs):
            if kind == "timeout":
                raise requests.Timeout("PRIVATE_SECRET")
            return Response({"bars": {}, "next_page_token": None})
        provider = AlpacaHistoricalSIPSource(get=get)
    source = AlpacaSIPPriceSource(sip_source=provider, now=lambda: NOW)
    clock = Clock()
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, random_fn=lambda: 0):
        resolution = resolve(source, ["BRC", "ICE"])
    store = MetricStore(tmp_path / "metrics.sqlite3")
    cohorts = [{"config": SimpleNamespace(name="book", horizon="30d")}]
    identities = tuple({"ticker": symbol, "horizon": "30d", "event_key": f"event-{symbol}",
                        "strategy": "filing_analysis"} for symbol in ["BRC", "ICE"])
    recoveries = {}
    for attempt in resolution.attempts:
        record = CandidateBarRecoveryRecord(
            recovery_id=f"recovery-{attempt.ticker}", epoch_id="epoch", session=SESSION,
            ticker=attempt.ticker, outcome="quarantined", attempts=(asdict(attempt),),
            signal_identities=({"event_key": f"event-{attempt.ticker}", "strategy": "filing_analysis"},),
        )
        store.save_candidate_bar_recovery(record)
        issue = _candidate_reference_issue(record, signal_identity_scope=identities, cohorts=cohorts)
        assert issue.reason_code == category
        store.save_candidate_input_issue(issue)
        recoveries[attempt.ticker] = record
    before = (store.read_session_candidate_bar_recoveries("epoch", SESSION),
              store.read_session_candidate_input_issues("epoch", SESSION))
    state = SimpleNamespace(owner=SimpleNamespace(_metric_store=store, cohorts=cohorts),
                            epoch_id="epoch", session=SESSION, issue_identity_scope=identities,
                            candidate_issue_references=[])
    _replay_candidate_reference_issues(state, recoveries, immutable_conflicts=True)
    assert len(state.candidate_issue_references) == 2
    assert (store.read_session_candidate_bar_recoveries("epoch", SESSION),
            store.read_session_candidate_input_issues("epoch", SESSION)) == before
