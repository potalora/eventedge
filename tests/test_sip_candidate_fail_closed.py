"""Offline SIP containment requires typed local failure and same-source health."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tradingagents.strategies.execution.alpaca_daily_bar import AlpacaBarFailure
from tradingagents.strategies.execution.models import MarketBar
from tradingagents.strategies.execution.price_source import CandidateBarAttempt, CandidateBarResolution
from test_shared_policy_volatility_evidence import SESSION, _history, _run_staging_matrix


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import socket
    def blocked(*args, **kwargs):
        raise AssertionError('offline only')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket.socket, 'connect_ex', blocked)


@pytest.mark.parametrize('case', ['no_peer', 'transport', 'auth', 'unknown', 'defect'])
def test_unproven_sip_failure_cannot_stage_or_accept(tmp_path, monkeypatch, case):
    def resolve(tickers, session, processed_at, max_age):
        if case == 'defect':
            raise RuntimeError('strategy defect')
        now = datetime(2026, 7, 31, 20, tzinfo=timezone.utc)
        bars, attempts = {}, []
        for ticker in tickers:
            values = (None,) * 4 if ticker == 'UI' else tuple(map(Decimal, ('100', '101', '99', '100')))
            attempts.append(CandidateBarAttempt(ticker, session, 1, 'alpaca_sip', now, *values,
                                               ('missing daily bar' if case == 'no_peer' else case) if ticker == 'UI' else None))
            if ticker != 'UI':
                bars[ticker, session] = MarketBar(ticker, session, *values, 'alpaca_sip', now, False)
        return CandidateBarResolution(bars, tuple(attempts), frozenset(), frozenset({'UI'}),
                                      local_failures={'UI': 'missing_data'} if case == 'no_peer' else None)
    staged = {}
    signals = ('UI',) if case == 'no_peer' else ('NCL', 'UI')
    owner, results, _ = _run_staging_matrix(
        tmp_path, monkeypatch, {'OPEN': _history(.021), 'PENDING': _history(.027),
                               'NCL': _history(.033), 'UI': _history(.031)},
        signal_tickers=signals, stage_observations=staged,
        price_source=SimpleNamespace(resolve_candidate_daily_bars=resolve))
    try:
        assert not staged
        assert all(row['error'] and not row['staging_valid'] for row in results.values())
        assert not owner._metric_store.read_candidate_input_issues(owner._epoch_id, SESSION)
    finally:
        for cohort in owner.cohorts:
            cohort['ledger'].close()


def test_sip_adapter_does_not_reclassify_implementation_defect():
    from datetime import timedelta
    from tradingagents.strategies.execution.price_source import AlpacaSIPPriceSource
    def fail(*args, **kwargs):
        raise RuntimeError('strategy defect')
    source = AlpacaSIPPriceSource(sip_source=SimpleNamespace(fetch_daily_bar=fail))
    with pytest.raises(RuntimeError, match='strategy defect'):
        source.resolve_candidate_daily_bars(['UI'], SESSION, datetime.now(timezone.utc), timedelta(hours=24))


@pytest.mark.parametrize('failure', [AlpacaBarFailure.TRANSPORT_ERROR,
                                   AlpacaBarFailure.HTTP_ERROR, AlpacaBarFailure.MISSING_CREDENTIALS])
def test_typed_provider_failure_is_never_local(failure):
    from datetime import timedelta
    from tradingagents.strategies.execution.alpaca_daily_bar import AlpacaDailyBarResult
    from tradingagents.strategies.execution.price_source import AlpacaSIPPriceSource
    source = AlpacaSIPPriceSource(sip_source=SimpleNamespace(
        fetch_daily_bar=lambda *args, **kwargs: AlpacaDailyBarResult(None, failure)))
    result = source.resolve_candidate_daily_bars(['UI'], SESSION, datetime.now(timezone.utc), timedelta(hours=24))
    assert result.quarantined_tickers == frozenset({'UI'})
    assert not result.local_failures


def test_exact_symbol_complete_missing_result_is_local():
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from tradingagents.strategies.execution.alpaca_daily_bar import AlpacaDailyBarResult
    from tradingagents.strategies.execution.price_source import AlpacaSIPPriceSource
    start = datetime.combine(SESSION, datetime.min.time(), ZoneInfo('America/New_York'))
    source = AlpacaSIPPriceSource(sip_source=SimpleNamespace(
        fetch_daily_bar=lambda *args, **kwargs: AlpacaDailyBarResult(
            None, AlpacaBarFailure.INVALID_RESPONSE, request_start=start,
            response_symbol='UI', row_count=0, pagination_complete=True, reason_code='missing_data')))
    result = source.resolve_candidate_daily_bars(['UI'], SESSION, datetime.now(timezone.utc), timedelta(hours=24))
    assert result.local_failures == {'UI': 'missing_data'}
