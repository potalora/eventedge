"""Independent focused Court request/evidence boundary controls; no network."""
import pytest
from test_focused_courtlistener import Clock, Response, collect, docket, install, scope, no_network
from tradingagents.strategies.data_sources import courtlistener_source as court
from tradingagents.strategies.data_sources import request_policy as policy
from test_litigation_targets import owner


def test_focused_queries_request_dockets_without_nested_filing_documents(monkeypatch):
    calls = install(monkeypatch, [Response({'count': 1, 'results': [docket()], 'next': None})])
    clock = Clock()
    with policy.provider_budget('courtlistener', 700., clock=clock, sleep=clock.sleep):
        result = collect(scope(), clock)
    assert result['coverage']['complete'] is True
    assert calls[0][1]['params']['type'] == 'd'


def test_legacy_search_retains_recap_type(monkeypatch):
    calls = install(monkeypatch, [Response({'count': 0, 'results': [], 'next': None})])
    clock = Clock()
    with policy.provider_budget('courtlistener', 700., clock=clock, sleep=clock.sleep):
        court.CourtListenerSource('synthetic-token').search_dockets('old query')
    assert calls[0][1]['params']['type'] == 'r'


def test_capped_retries_do_not_reserve_a_phantom_rate_slot(monkeypatch):
    pages = [Response({}), Response({})]
    for response in pages:
        response.status_code = 503
    calls = install(monkeypatch, pages)
    clock = Clock()
    limits = ((50, 60),)
    with policy.provider_budget('courtlistener', 700., clock=clock, sleep=clock.sleep,
                                limits=limits, random_fn=lambda: 0):
        result = collect(scope(), clock, request_cap=2)
    history = policy._HISTORY[('courtlistener', clock, 50, 60)]
    assert len(calls) == result['coverage']['requests'] == 2
    assert len(history) == len(calls)


@pytest.mark.parametrize('role', ['held', 'pending'])
def test_unmapped_protected_portfolio_target_fails_instead_of_omission(owner, role):
    from test_litigation_targets import build, retained, COMPANIES
    retained(owner.cohorts[-1]['ledger'], 'AAPL', identity='protected', **{role: True})
    companies = {key: row for key, row in COMPANIES.items() if row['ticker'] != 'AAPL'}
    with pytest.raises(ValueError, match='missing_or_ambiguous_litigation_issuer:AAPL'):
        build(owner, company_map=companies)


def test_malformed_retained_prior_candidate_is_not_silently_skipped(owner):
    from test_litigation_targets import build, retained
    ledger = owner.cohorts[-1]['ledger']
    signal = retained(ledger, 'AAPL', identity='malformed')
    original = ledger.signal_observation
    def malformed(identity):
        result = original(identity)
        if identity != signal.signal_id:
            return result
        return result[0], {'signal': {'ticker': 'AAPL'}}, result[2]
    ledger.signal_observation = malformed
    with pytest.raises((KeyError, ValueError)):
        build(owner)
