"""Offline controls for candidate-only versus provider/implementation failure."""
from types import SimpleNamespace

import pandas as pd
import pytest

from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
from tradingagents.strategies.orchestration.data_acquisition import DataAcquisitionService
from test_shared_policy_volatility_evidence import SESSION, _history, _run_staging_matrix


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import socket
    def blocked(*args, **kwargs):
        raise AssertionError("provider classification tests must remain offline")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


def _run_case(tmp_path, monkeypatch, *, failure=None, healthy=True, source_error=False):
    from tradingagents.strategies.orchestration import daily_pipeline
    original = daily_pipeline.run_candidate_volatility_validation
    def validate(state):
        if source_error:
            state.shared_data["yfinance"]["error"] = "provider outage"
        # The shared offline fixture owns the legacy engine mock. Bind its
        # synthetic responses explicitly to the new strict acquisition seam.
        def strict_fetch(tickers, start, end, *, strict=False):
            assert strict
            if failure is not None:
                raise failure
            return state.first_engine._fetch_missing_prices(tickers, start, end)
        monkeypatch.setattr(state.first_engine.data_acquisition, "fetch_missing_prices", strict_fetch)
        return original(state)
    monkeypatch.setattr(daily_pipeline, "run_candidate_volatility_validation", validate)
    staged = {}
    histories = {"UI": _history(.031).iloc[:0]}
    if healthy:
        histories["NCL"] = _history(.033)
    return (*_run_staging_matrix(
        tmp_path, monkeypatch, histories, signal_tickers=("UI",),
        open_tickers=(), pending_tickers=(), stage_observations=staged,
    ), staged)


@pytest.mark.parametrize("failure,healthy,source_error", [
    (RuntimeError("unexpected defect"), True, False),
    (SourceFetchError("outage", reason_code="provider_error"), True, False),
    (SourceFetchError("symbol absent", reason_code="invalid_response", failed_operations={"UI": "invalid_response"}), False, False),
    (None, False, False),
    (None, True, True),
])
def test_unproven_candidate_failure_holds_all_books(tmp_path, monkeypatch, failure, healthy, source_error):
    owner, results, calls, staged = _run_case(
        tmp_path, monkeypatch, failure=failure, healthy=healthy, source_error=source_error)
    try:
        assert not staged
        assert all(row["error"] and not row["staging_valid"] for row in results.values())
        assert not owner._metric_store.read_candidate_input_issues(owner._epoch_id, SESSION)
    finally:
        for cohort in owner.cohorts:
            cohort["ledger"].close()


@pytest.mark.parametrize("response", ["unavailable", "empty"])
def test_strict_acquisition_rejects_unknown_empty_response(monkeypatch, response):
    source = YFinanceSource() if response == "empty" else None
    if source is not None:
        monkeypatch.setattr(source, "fetch_prices", lambda *args: pd.DataFrame())
    service = object.__new__(DataAcquisitionService)
    service.price_cache = {}
    service.registry = SimpleNamespace(get=lambda name: source)
    assert service.fetch_missing_prices(["UI"], "2026-01-01", "2026-07-31") == {}
    with pytest.raises(SourceFetchError):
        service.fetch_missing_prices(["UI"], "2026-01-01", "2026-07-31", strict=True)


@pytest.mark.parametrize("failure", [None, SourceFetchError(
    "symbol absent", reason_code="invalid_response", failed_operations={"UI": "invalid_response"})])
def test_proven_local_miss_with_healthy_peer_remains_isolated(tmp_path, monkeypatch, failure):
    owner, results, calls, staged = _run_case(tmp_path, monkeypatch, failure=failure)
    try:
        issues = owner._metric_store.read_candidate_input_issues(owner._epoch_id, SESSION)
        assert len(issues) == 1 and issues[0].ticker == "UI"
        assert len(staged) == 16
        assert all(not row["error"] and row["staging_valid"] for row in results.values())
    finally:
        for cohort in owner.cohorts:
            cohort["ledger"].close()


@pytest.mark.parametrize("response", ["empty", "transport", "defect"])
def test_real_yahoo_path_does_not_invent_symbol_evidence(monkeypatch, response):
    import yfinance as yf
    monkeypatch.setattr(
        "tradingagents.strategies.data_sources.yfinance_source.ensure_yfinance_tz_cache", lambda: None)
    def download(*args, **kwargs):
        if response == "transport":
            raise TimeoutError("fixture transport failure")
        if response == "defect":
            raise RuntimeError("fixture implementation defect")
        return pd.DataFrame()
    monkeypatch.setattr(yf, "download", download)
    source = YFinanceSource()
    service = object.__new__(DataAcquisitionService)
    service.price_cache = {}
    service.registry = SimpleNamespace(get=lambda name: source)
    with pytest.raises(SourceFetchError) as caught:
        service.fetch_missing_prices(["UI"], "2026-01-01", "2026-07-31", strict=True)
    assert not caught.value.failed_operations
    assert not service.price_cache


def test_strict_acquisition_preserves_failure_while_legacy_stays_best_effort(monkeypatch):
    source = YFinanceSource()
    def fail(*args):
        raise RuntimeError("unexpected strategy defect")
    monkeypatch.setattr(source, "fetch_prices", fail)
    service = object.__new__(DataAcquisitionService)
    service.price_cache = {}
    service.registry = SimpleNamespace(get=lambda name: source)
    assert service.fetch_missing_prices(["UI"], "2026-01-01", "2026-07-31") == {}
    with pytest.raises(RuntimeError, match="unexpected strategy defect"):
        service.fetch_missing_prices(["UI"], "2026-01-01", "2026-07-31", strict=True)


@pytest.mark.parametrize("response", ["full", "short", "unknown_empty"])
def test_invalid_cached_candidate_history_reaches_strict_yahoo_acquisition(
    tmp_path, monkeypatch, response
):
    from tradingagents.strategies.orchestration import daily_pipeline

    full = _history(.031)
    short = full.tail(12)
    source = YFinanceSource()
    requests = []

    def fetch_prices(tickers, start, end):
        requests.append((tickers, start, end))
        return {"full": full, "short": short, "unknown_empty": full.iloc[:0]}[response]

    monkeypatch.setattr(source, "fetch_prices", fetch_prices)
    original = daily_pipeline.run_candidate_volatility_validation

    def validate(state):
        service = state.first_engine.data_acquisition
        monkeypatch.setattr(service, "registry", SimpleNamespace(get=lambda name: source))
        monkeypatch.setattr(service, "fetch_missing_prices", DataAcquisitionService.fetch_missing_prices.__get__(service))
        return original(state)

    monkeypatch.setattr(daily_pipeline, "run_candidate_volatility_validation", validate)
    staged = {}
    owner, results, _ = _run_staging_matrix(
        tmp_path, monkeypatch, {"UI": short, "NCL": _history(.033)},
        signal_tickers=("UI", "NCL"), open_tickers=(), pending_tickers=(),
        stage_observations=staged,
    )
    try:
        assert len(requests) == 1, "invalid cache must not satisfy the strict history refresh"
        tickers, start, end = requests[0]
        assert tickers == ["UI"] and start <= full.index[-61].date().isoformat()
        assert end == SESSION.isoformat()
        assert len(results) == 16
        issues = owner._metric_store.read_candidate_input_issues(owner._epoch_id, SESSION)
        if response == "full":
            assert len(staged) == 16 and all("UI" in row["signals"] for row in staged.values())
            assert all(row["staging_valid"] and not row["degraded"] for row in results.values())
            assert not issues
        elif response == "short":
            assert len(staged) == 16 and all("UI" not in row["signals"] for row in staged.values())
            assert all(row["staging_valid"] and row["degraded"] for row in results.values())
            assert [(issue.ticker, issue.reason_code) for issue in issues] == [("UI", "insufficient_history")]
        else:
            assert not staged and not issues
            assert all(row["error"] and not row["staging_valid"] for row in results.values())
    finally:
        for cohort in owner.cohorts:
            cohort["ledger"].close()
