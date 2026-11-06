"""A missing SDK longName can use an attributed Yahoo shortName only."""
from types import SimpleNamespace as N

import pytest
from curl_cffi.requests.exceptions import HTTPError, Timeout
from yfinance.data import YfData

from test_openbb_native_enrichment import profile, source_with
from tradingagents.strategies.data_sources.request_policy import provider_budget


def missing_name(symbol="ASIC", **changes):
    return profile(symbol, **(dict(name=None, issue_type="EQUITY") | changes))


def quote(symbol="ASIC", **changes):
    return {"quoteResponse": {"error": None, "result": [
        {"symbol": symbol, "shortName": "Ategrity Specialty Insurance Co", "quoteType": "EQUITY"} | changes,
    ]}}


@pytest.fixture
def raw_lookup(monkeypatch):
    calls = []
    response = [quote()]
    def get_raw_json(self, url, **kwargs):
        calls.append((url, kwargs))
        value = response.pop(0) if len(response) > 1 else response[0]
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(YfData, "get_raw_json", get_raw_json)
    return calls, response


@pytest.mark.parametrize("scalar", [False, True])
def test_missing_long_name_uses_labeled_exact_provider_short_name(raw_lookup, scalar):
    calls, _ = raw_lookup
    source = source_with(lambda **_: N(results=[missing_name()]))
    result = (source.fetch({"method": "equity_profile", "ticker": "ASIC"}) if scalar
              else source.fetch_profiles(["ASIC"])["profiles"]["ASIC"])
    assert result["name"] == "Ategrity Specialty Insurance Co"
    assert result["name_source"] == "yfinance.quoteResponse.shortName"
    assert result["name_fallback"] == "native_long_name_missing"
    assert result["sector"] == "Technology"
    assert result["market_cap"] == 100
    assert len(calls) == 1
    assert calls[0][0] == "https://query1.finance.yahoo.com/v7/finance/quote"
    assert calls[0][1]["params"]["symbols"] == "ASIC"
    assert 0 < calls[0][1]["timeout"] <= 5
    assert source.fetch({"method": "equity_profile", "ticker": "ASIC"}) == result
    assert len(calls) == 1


@pytest.mark.parametrize("row", [profile("ASIC"), missing_name(sector=None),
    missing_name(market_cap=float("nan")), missing_name(name=" "),
    missing_name(issue_type="MUTUALFUND"), missing_name(issue_type=None),
    missing_name("OTHER"),
])
def test_complete_or_unqualified_native_rows_never_fetch_short_name(raw_lookup, row):
    calls, _ = raw_lookup
    source_with(lambda **_: N(results=[row])).fetch_profiles(["ASIC"])
    assert calls == []


@pytest.mark.parametrize("raw", [None, {}, quote("OTHER"), quote(None), quote(shortName=" "),
    quote(shortName=None), quote(shortName=123), quote(quoteType="ETF"), quote(quoteType=None),
    {"quoteResponse": {"error": None, "result": [{"shortName": "Name", "quoteType": "EQUITY"}]}},
    {"quoteResponse": {"error": {"code": "Bad"}, "result": quote()["quoteResponse"]["result"]}},
    {"quoteResponse": {"error": None, "result": []}},
    {"quoteResponse": {"error": None, "result": quote()["quoteResponse"]["result"] * 2}},
])
def test_invalid_raw_identity_or_name_preserves_successful_siblings(raw_lookup, raw):
    calls, responses = raw_lookup
    responses[:] = [raw]
    source = source_with(lambda **_: N(results=[missing_name(), profile("AAPL")]))
    result = source.fetch_profiles(["ASIC", "AAPL"])
    assert set(result["profiles"]) == {"AAPL"}
    assert result["errors"]["ASIC"]["reason_code"] == "invalid_response"
    assert "equity_profile|ASIC" not in source._cache
    assert len(calls) == 1


def test_late_name_lookup_is_rejected_under_inherited_budget(monkeypatch):
    clock = [0.0]
    observed = []
    def raw(self, url, **kwargs):
        observed.append(kwargs["timeout"])
        clock[0] = 3.0
        return quote()
    monkeypatch.setattr(YfData, "get_raw_json", raw)
    source = source_with(lambda **_: N(results=[missing_name(), profile("AAPL")]))
    with provider_budget("openbb", 2.0, clock=lambda: clock[0], sleep=lambda _: None) as diagnostics:
        result = source.fetch_profiles(["ASIC", "AAPL"])
    assert observed == [2.0]
    assert result["errors"]["ASIC"]["reason_code"] == "timeout"
    assert set(result["profiles"]) == {"AAPL"}
    assert diagnostics[-1]["operation"] == "equity_profile_name"
    assert diagnostics[-1]["reason_code"] == "timeout"


def test_transient_name_lookup_retries_are_accounted(raw_lookup):
    calls, responses = raw_lookup
    responses[:] = [Timeout("private"), quote()]
    clock = [0.0]
    with provider_budget("openbb", 10, clock=lambda: clock[0],
                         sleep=lambda delay: clock.__setitem__(0, clock[0]+delay),
                         random_fn=lambda: 0) as diagnostics:
        result = source_with(lambda **_: N(results=[missing_name()])).fetch_profiles(["ASIC"])
    assert result["errors"] == {}
    assert len(calls) == 2
    assert diagnostics[-1] == {"provider": "openbb", "operation": "equity_profile_name",
        "reason_code": None, "http_status": None, "attempts": 2, "recovered": True}


def test_raw_http_failure_is_classified_without_admission(raw_lookup):
    calls, responses = raw_lookup
    responses[:] = [HTTPError("private", response=N(status_code=404))]
    with provider_budget("openbb", 9999999999) as diagnostics:
        result = source_with(lambda **_: N(results=[missing_name()])).fetch_profiles(["ASIC"])
    assert result["profiles"] == {}
    assert result["errors"]["ASIC"]["reason_code"] == "http_error"
    assert len(calls) == 1
    assert diagnostics[-1]["http_status"] == 404


def test_expired_parent_budget_never_dispatches_name_lookup(monkeypatch, raw_lookup):
    from tradingagents.strategies.data_sources import openbb_source
    clock = [0.0]
    calls, _ = raw_lookup
    original = openbb_source._profile_short_name
    def complete(*args):
        clock[0] = 3.0
        return original(*args)
    monkeypatch.setattr(openbb_source, '_profile_short_name', complete)
    with provider_budget('openbb', 2, clock=lambda: clock[0], sleep=lambda _: None) as diagnostics:
        result = source_with(lambda **_: N(results=[missing_name()])).fetch_profiles(['ASIC'])
    assert calls == []
    assert result['errors']['ASIC']['reason_code'] == 'timeout'
    assert diagnostics[-1]['attempts'] == 0


def test_scalar_failure_does_not_retry_already_completed_native_profile(raw_lookup):
    calls, responses = raw_lookup
    responses[:] = [Timeout('private')]
    native_calls = []
    def native(**kwargs):
        native_calls.append(kwargs)
        return N(results=[missing_name()])
    clock = [0.0]
    with provider_budget('openbb', 10, clock=lambda: clock[0],
                         sleep=lambda delay: clock.__setitem__(0, clock[0]+delay),
                         random_fn=lambda: 0) as diagnostics:
        result = source_with(native).fetch({'method': 'equity_profile', 'ticker': 'ASIC'})
    assert result['reason_code'] == 'timeout'
    assert len(calls) == 3
    assert len(native_calls) == 1
    assert diagnostics[0]['operation'] == 'equity_profile_name'
    assert diagnostics[0]['attempts'] == 3


def test_raw_http_rate_limit_preserves_retry_after(raw_lookup):
    calls, responses = raw_lookup
    closed = []
    responses[:] = [HTTPError('private', response=N(status_code=429,
        headers={'Retry-After': '2'}, close=lambda: closed.append(True))), quote()]
    clock = [0.0]
    waits = []
    def sleep(delay):
        waits.append(delay)
        clock[0] += delay
    with provider_budget('openbb', 10, clock=lambda: clock[0], sleep=sleep,
                         random_fn=lambda: 0) as diagnostics:
        result = source_with(lambda **_: N(results=[missing_name()])).fetch_profiles(['ASIC'])
    assert result['errors'] == {}
    assert len(calls) == 2
    assert waits == [2.0]
    assert closed == [True]
    assert diagnostics[-1]['recovered'] is True
