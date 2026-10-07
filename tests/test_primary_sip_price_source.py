"""Primary SIP prices are normal governed inputs, never Yahoo recovery."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock

import pytest

from tradingagents.strategies.execution import price_source as prices
from tradingagents.strategies.execution.alpaca_daily_bar import (
    AlpacaBarFailure,
    AlpacaHistoricalSIPSource,
    SOURCE,
)
from tradingagents.strategies.orchestration.governed_market_data import (
    resolve_governed_bars,
)

SESSION = date(2026, 10, 2)
NOW = datetime(2026, 10, 6, 22, tzinfo=timezone.utc)
OBSERVED = {
    ("AYI", SESSION): ("303.9", "312.055", "303.9", "308.76"),
    ("BR", SESSION): ("161.15", "161.15", "156.86", "156.93"),
    ("AYI", date(2026, 10, 5)): ("308.87", "308.87", "301.085", "304.84"),
}


def _source(monkeypatch, *, mutation=None, now=NOW):
    monkeypatch.setenv("ALPACA_API_KEY", "offline-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "offline-secret")

    def get(url, **kwargs):
        ticker = url.split("/")[-2]
        session = date.fromisoformat(kwargs["params"]["start"][:10])
        body = {
            "symbol": ticker,
            "bars": [dict(t=f"{session}T04:00:00Z", **dict(zip("ohlc", OBSERVED[ticker, session])))],
            "next_page_token": None,
        }
        if mutation:
            mutation(body)
        response = Mock(status_code=200)
        response.json.return_value = body
        return response

    monkeypatch.setattr(prices.yf, "download", Mock(side_effect=AssertionError("Yahoo daily prices forbidden")))
    return prices.AlpacaSIPPriceSource(
        sip_source=AlpacaHistoricalSIPSource(get=get), now=lambda: now
    )


@pytest.mark.parametrize("key", OBSERVED)
def test_incident_coherent_sip_is_primary_without_yahoo(monkeypatch, key):
    source = _source(monkeypatch)
    ticker, session = key
    candidate = source.resolve_candidate_daily_bars([ticker], session, NOW, timedelta(hours=24))
    bar = candidate.bars[key]
    assert (bar.open, bar.high, bar.low, bar.close) == tuple(map(Decimal, OBSERVED[key]))
    assert bar.source == SOURCE
    assert candidate.recovered_tickers == candidate.quarantined_tickers == frozenset()
    assert candidate.attempts[0].validation_error is None


def test_factory_defaults_to_primary_sip_and_rejects_raw_yahoo_selection():
    assert isinstance(prices.build_price_source({}), prices.AlpacaSIPPriceSource)
    config = {"autoresearch": {"paper_ledger": {"pricing_version": "raw-alpaca-sip-v1"}}}
    assert isinstance(prices.build_price_source(config), prices.AlpacaSIPPriceSource)
    for version in ("raw-yfinance-v1", "raw-iex-v1"):
        with pytest.raises(ValueError, match="unsupported pricing_version"):
            prices.build_price_source({"autoresearch": {"paper_ledger": {"pricing_version": version}}})


def test_primary_governed_bar_needs_no_recovery_record(monkeypatch):
    source = _source(monkeypatch)
    result = resolve_governed_bars(
        price_source=source, metric_store=None, epoch_id="new-gen", session=SESSION,
        tickers=["AYI", "BR"], cohort_ids_by_ticker={"AYI": ("cohort",), "BR": ("cohort",)},
        processed_at=NOW, persist=False,
    )
    assert set(result.bars) == {"AYI", "BR"}
    assert result.failure_map == result.recovery_bindings == {}
    assert result.recovery_summaries == ()


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(symbol="OTHER"),
    lambda p: p.update(next_page_token="more"),
    lambda p: p["bars"].append(p["bars"][0].copy()),
    lambda p: p["bars"][0].update(t="2026-10-05T04:00:00Z"),
    lambda p: p["bars"][0].update(l="304.98"),
])
def test_primary_invalid_sip_stays_visible_and_governed_blocking(monkeypatch, mutation):
    source = _source(monkeypatch, mutation=mutation)
    candidate = source.resolve_candidate_daily_bars(["AYI"], SESSION, NOW, timedelta(hours=24))
    assert candidate.bars == {}
    assert candidate.quarantined_tickers == frozenset({"AYI"})
    assert candidate.attempts[0].validation_error == "invalid_response AYI/2026-10-02"
    governed = source.resolve_governed_daily_bars(["AYI"], SESSION, processed_at=NOW)
    assert governed.bars == {}
    assert governed.failure_map == {"AYI": "invalid AYI/2026-10-02"}
    assert governed.attempts["AYI"].validation_error == "invalid_response AYI/2026-10-02"


@pytest.mark.parametrize("field,value", [("feed", "iex"), ("adjustment", "all"), ("timeframe", "1Min"), ("row_count", 2), ("pagination_complete", False)])
def test_primary_rechecks_exact_adapter_evidence(monkeypatch, field, value):
    source = _source(monkeypatch)
    result = source._sip_source.fetch_daily_bar("AYI", SESSION, now=NOW)
    source._sip_source = Mock(fetch_daily_bar=Mock(return_value=replace(result, **{field: value})))
    candidate = source.resolve_candidate_daily_bars(["AYI"], SESSION, NOW, timedelta(hours=24))
    assert candidate.bars == {}
    assert candidate.attempts[0].validation_error == "invalid_response AYI/2026-10-02"


@pytest.mark.parametrize("reason", [AlpacaBarFailure.MISSING_CREDENTIALS, AlpacaBarFailure.SESSION_NOT_READY, AlpacaBarFailure.TRANSPORT_ERROR])
def test_primary_typed_failures_never_fallback(monkeypatch, reason):
    from tradingagents.strategies.execution.alpaca_daily_bar import AlpacaDailyBarResult
    source = _source(monkeypatch)
    source._sip_source = Mock(fetch_daily_bar=Mock(return_value=AlpacaDailyBarResult(None, reason)))
    with pytest.raises(prices.SIPPriceError) as error:
        source.get_daily_bars(["AYI"], SESSION, SESSION)
    assert error.value.failure_map == {"AYI": reason}
    assert "offline-secret" not in str(error.value)


def _executor_fixture(tmp_path):
    from test_session_executor import _config, _ledger
    from tradingagents.strategies.orchestration.session_executor import SessionExecutor, SessionInputBundle
    config = _config()
    config["autoresearch"]["paper_ledger"]["pricing_version"] = "raw-alpaca-sip-v1"
    ledger = _ledger(tmp_path)
    benchmark = prices.AdjustedClose("SPY", SESSION, Decimal("650"), "yfinance-adjusted", NOW)
    bundle = SessionInputBundle(SESSION, ("AYI",), {
        ("AYI", SESSION): prices.MarketBar("AYI", SESSION, *map(Decimal, OBSERVED["AYI", SESSION]), SOURCE, NOW, False)
    }, (), {("SPY", SESSION): benchmark, ("BIL", SESSION): replace(benchmark, symbol="BIL", close=Decimal("91"))})
    return ledger, config, bundle, SessionExecutor


def test_primary_executor_accepts_and_persists_normal_sip_input(tmp_path):
    ledger, config, bundle, executor_type = _executor_fixture(tmp_path)
    try:
        result = executor_type(ledger, config).execute_open_and_mark(SESSION, "epoch", bundle, {}, NOW)
        assert result.valid, result.invalid_reason
        restored = executor_type(ledger, config).persisted_input_bundle(SESSION)
        assert restored.bars == bundle.bars
        assert restored.governed_recoveries == {}
        context = ledger.session_execution_context(SESSION)
        document, _ = executor_type(ledger, config)._static_context_documents((), {})
        assert document["price_source_policy"] == "raw-alpaca-sip-v1"
        assert context["config_digest"] == prices.stable_id("session_execution_config", document)
    finally:
        ledger.close()


def test_primary_resume_reuses_frozen_inputs_without_provider(tmp_path):
    ledger, config, bundle, executor_type = _executor_fixture(tmp_path)
    try:
        def crash(phase):
            if phase == "validate_market_data":
                raise RuntimeError("interrupted after binding")
        with pytest.raises(RuntimeError, match="interrupted"):
            executor_type(ledger, config, after_phase_commit=crash).execute_open_and_mark(SESSION, "epoch", bundle, {}, NOW)
        context = ledger.session_execution_context(SESSION)
        provider = Mock(get_daily_bars=Mock(side_effect=AssertionError("never refetch frozen SIP")))
        later = NOW + timedelta(days=2)
        result = executor_type(ledger, config).execute_open_and_mark(SESSION, "epoch", provider, {}, later)
        assert result.valid, result.invalid_reason
        assert ledger.session_execution_context(SESSION)["input_digest"] == context["input_digest"]
        provider.get_daily_bars.assert_not_called()
    finally:
        ledger.close()


def test_primary_executor_rejects_yahoo_raw_even_when_coherent(tmp_path):
    ledger, config, bundle, executor_type = _executor_fixture(tmp_path)
    try:
        bad = replace(bundle, bars={key: replace(bar, source="yfinance") for key, bar in bundle.bars.items()})
        result = executor_type(ledger, config).execute_open_and_mark(SESSION, "epoch", bad, {}, NOW)
        assert not result.valid
        assert "primary SIP" in result.invalid_reason
        assert ledger.session_execution_context(SESSION) is None
    finally:
        ledger.close()


def test_bound_price_policy_cannot_change_same_session(tmp_path):
    ledger, config, bundle, executor_type = _executor_fixture(tmp_path)
    try:
        assert executor_type(ledger, config).execute_open_and_mark(SESSION, "epoch", bundle, {}, NOW).valid
        config["autoresearch"]["paper_ledger"].pop("pricing_version")
        result = executor_type(ledger, config).execute_open_and_mark(SESSION, "epoch", bundle, {}, NOW)
        assert not result.valid
        assert "effective config" in result.invalid_reason
    finally:
        ledger.close()


def test_total_return_benchmark_dependency_is_explicit_and_not_raw_sip(monkeypatch):
    source = _source(monkeypatch)
    close = prices.AdjustedClose("SPY", SESSION, Decimal("640"), "yfinance-adjusted", NOW)
    research = Mock(get_total_return_closes=Mock(return_value={("SPY", SESSION): close}))
    source._research_source = research
    assert source.get_total_return_closes(["SPY"], SESSION, SESSION)["SPY", SESSION] == close
    assert "total_return_adjusted_benchmarks" in source.yahoo_dependencies


def test_preflight_primary_sip_accepts_healthy_input_without_recovery_binding(monkeypatch):
    from types import SimpleNamespace
    from tradingagents.strategies.orchestration.preflight import _validate_governed_resolution
    from tradingagents.strategies.orchestration.governed_market_data import GovernedInputResolution
    source = _source(monkeypatch)
    resolution = GovernedInputResolution(
        {"AYI": source.get_daily_bars(["AYI"], SESSION, SESSION)["AYI", SESSION]}, {}, (), {},
    )
    snapshot = SimpleNamespace(governed_tickers=("AYI",), cohort_ids_by_ticker={"AYI": ("cohort",)})
    assert _validate_governed_resolution(
        resolution, snapshot=snapshot, session=SESSION, processed_at=NOW,
        pricing_version="raw-alpaca-sip-v1",
    ) == ([], {})
    bad = replace(resolution, bars={"AYI": replace(resolution.bars["AYI"], source="yfinance")})
    with pytest.raises(ValueError, match="source"):
        _validate_governed_resolution(
            bad, snapshot=snapshot, session=SESSION, processed_at=NOW,
            pricing_version="raw-alpaca-sip-v1",
        )


def test_epoch_context_accepts_current_primary_source_contract(tmp_path):
    from test_metric_epoch_runtime import _context, _policy
    ledger, config, _, executor_type = _executor_fixture(tmp_path)
    try:
        document = executor_type(ledger, config).semantic_policy_document()
        context = _context(cohort_policies=(_policy(execution_policy=document),))
        assert context.pricing_version == "raw-alpaca-sip-v1"
        changed = _context(cohort_policies=(_policy(execution_policy={**document, "benchmark_price_source_policy": "declared-adjusted-v2"}),))
        assert changed.config_hash != context.config_hash
    finally:
        ledger.close()


def test_runtime_preflight_uses_current_primary_factory_and_real_resolver(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from tradingagents.strategies.orchestration.preflight import run_preflight

    source = _source(monkeypatch)
    factory = Mock(return_value=source)
    monkeypatch.setattr(prices, "build_price_source", factory)
    snapshot = SimpleNamespace(
        state_status="uninitialized", epoch_id="current-gen",
        governed_tickers=("AYI", "BR"),
        cohort_ids_by_ticker={"AYI": ("cohort",), "BR": ("cohort",)},
    )

    @contextmanager
    def state_context(**kwargs):
        yield snapshot, None

    config = {"autoresearch": {"state_dir": str(tmp_path / "not-created")}}
    report = run_preflight(
        config, str(SESSION), mode="governed", processed_at=NOW,
        state_context_factory=state_context,
    )
    assert report["governed_probe_status"] == "ready"
    assert report["ok"] is True
    assert report["price_source_policy"] == "raw-alpaca-sip-v1"
    assert report["governed_bar_recoveries"] == []
    assert report["governed_failure_map"] == {}
    factory.assert_called_once_with(config)
    assert not (tmp_path / "not-created").exists()


def test_runtime_preflight_does_not_acquire_before_sip_readiness(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from tradingagents.strategies.orchestration.preflight import run_preflight

    factory = Mock(side_effect=AssertionError("must not instantiate before SIP close+15m"))
    monkeypatch.setattr(prices, "build_price_source", factory)

    @contextmanager
    def state_context(**kwargs):
        yield SimpleNamespace(state_status="uninitialized", governed_tickers=("AYI",)), None

    report = run_preflight(
        {"autoresearch": {"state_dir": str(tmp_path / "not-created")}},
        str(SESSION), mode="governed",
        processed_at=datetime(2026, 10, 2, 20, 14, 59, tzinfo=timezone.utc),
        state_context_factory=state_context,
    )
    assert report["governed_probe_status"] == "not_ready"
    factory.assert_not_called()


def test_primary_success_is_reused_but_failed_request_is_not_cached(monkeypatch):
    from tradingagents.strategies.execution.alpaca_daily_bar import AlpacaDailyBarResult
    source = _source(monkeypatch)
    response = source._sip_source.fetch_daily_bar("AYI", SESSION, now=NOW)
    get = Mock(side_effect=[AlpacaDailyBarResult(None, AlpacaBarFailure.TRANSPORT_ERROR), response])
    source._sip_source = Mock(fetch_daily_bar=get)
    assert source.resolve_candidate_daily_bars(["AYI"], SESSION, NOW, timedelta(hours=24)).bars == {}
    first = source.get_daily_bars(["AYI"], SESSION, SESSION)
    assert source.get_daily_bars(["AYI"], SESSION, SESSION) == first
    assert get.call_count == 2


@pytest.mark.parametrize("change", [
    {"fetched_at": NOW + timedelta(minutes=1)},
    {"fetched_at": datetime(2026, 10, 2, 20, 14, tzinfo=timezone.utc)},
    {"ticker": "BR"},
    {"session": date(2026, 10, 5)},
    {"source": "yfinance"},
    {"adjusted": True},
])
def test_primary_never_accepts_unready_or_wrong_bar_identity(monkeypatch, change):
    source = _source(monkeypatch)
    result = source._sip_source.fetch_daily_bar("AYI", SESSION, now=NOW)
    source._sip_source = Mock(fetch_daily_bar=Mock(return_value=replace(result, bar=replace(result.bar, **change))))
    bars = source.resolve_candidate_daily_bars(["AYI"], SESSION, NOW, timedelta(hours=24))
    assert bars.bars == {}
    assert bars.attempts[0].validation_error == "invalid_response AYI/2026-10-02"


def test_primary_cannot_reinterpret_old_recovery_record(monkeypatch):
    from test_governed_sip_recovery import _sip_record
    from tradingagents.strategies.orchestration.governed_market_data import GovernedMarketDataError
    source = _source(monkeypatch)
    store = Mock(load_governed_bar_recovery=Mock(return_value=_sip_record()))
    with pytest.raises(GovernedMarketDataError):
        resolve_governed_bars(
            price_source=source, metric_store=store, epoch_id="new-gen", session=SESSION,
            tickers=["AYI"], cohort_ids_by_ticker={"AYI": ("cohort",)}, processed_at=NOW, persist=False,
        )


def test_primary_batch_cannot_restart_budget_for_each_ticker(monkeypatch):
    import requests
    from tradingagents.strategies.data_sources import request_policy
    from test_request_policy import Clock
    clock = Clock()
    monkeypatch.setattr(request_policy.time, 'monotonic', clock)
    monkeypatch.setattr(request_policy.time, 'sleep', clock.sleep)
    monkeypatch.setenv('ALPACA_API_KEY', 'offline')
    monkeypatch.setenv('ALPACA_SECRET_KEY', 'offline')
    calls = []
    def exhausted_request(url, **kwargs):
        calls.append(url)
        clock.now += 301
        raise requests.Timeout('private message')
    source = prices.AlpacaSIPPriceSource(
        sip_source=AlpacaHistoricalSIPSource(get=exhausted_request), now=lambda: NOW,
    )
    result = source.resolve_candidate_daily_bars(['AYI', 'BR'], SESSION, NOW, timedelta(hours=24))
    assert result.quarantined_tickers == frozenset({'AYI', 'BR'})
    assert len(calls) == 1
