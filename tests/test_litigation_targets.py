"""Focused Court targets use actual persisted ledger authority; no acquisition."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
from test_portfolio_policy_ledger import (
    _signal, _intent, _fill, _record_signal_policy, _record_intent_policy, _bind_policy_context,
)

SESSION = date(2026, 8, 3)
CUTOFF = datetime(2026, 8, 3, 21, tzinfo=timezone.utc)
SETTINGS = {'policy': 'focused_litigation_v1', 'lookback_sessions': 5,
            'shortlist_limit': 5, 'issuer_query_limit': 5, 'watchlist': [], 'case_ids': []}
COMPANIES = {str(i): {'cik_str': i + 1, 'ticker': ticker, 'title': name}
             for i, (ticker, name) in enumerate((('AAPL', 'Apple Inc.'), ('MSFT', 'Microsoft Corp.'),
                 ('GOOG', 'Alphabet Inc.'), ('NVDA', 'NVIDIA Corp.'), ('IBM', 'IBM Corp.'),
                 ('CAT', 'Caterpillar Inc.'), ('ALL', 'Allstate Corp.')))}


@pytest.fixture
def owner(tmp_path):
    cohorts = []
    for i in range(16):
        name = f'book-{i:02d}'
        ledger = PortfolioLedger(tmp_path / name / 'portfolio.db', name, Decimal('5000'))
        cohorts.append({'config': SimpleNamespace(name=name, horizon='30d'), 'ledger': ledger})
    value = SimpleNamespace(cohorts=cohorts, _epoch_id='epoch-1',
                            _policy_id_for_horizon=lambda horizon: 'param-policy-1')
    yield value
    for row in cohorts:
        row['ledger'].close()


def retained(ledger, ticker='AAPL', *, identity='one', pending=False, held=False,
             observed_at=None, journal_only=False):
    signal = replace(_signal(identity, ticker=ticker, event_key='event-' + identity),
                     observed_at=observed_at or datetime(2026, 7, 31, 20, tzinfo=timezone.utc))
    ledger.record_signal_with_journal(signal, {'signal_id': identity, 'ticker': ticker, 'status': 'timely'},
        signal.decision_at, {'signal': {'ticker': ticker, 'strategy': signal.strategy,
            'direction': signal.direction, 'journal_only': journal_only, 'metadata': {'event_key': signal.event_key}}})
    if journal_only:
        binding = _bind_policy_context(ledger, session=signal.reference_session)
        ledger.record_signal_policy_provenance(signal.signal_id, policy_version='portfolio_policy_v1',
            event_key=signal.event_key, source_event_keys=('source:'+signal.event_key,),
            strategy_tags=(signal.strategy,), risk_tags=('event:'+signal.event_key,), sector='Technology',
            journal_only=True, order_eligible=False, decision='rejected', reason_codes=('journal_only',),
            bound_context_digest=binding['context_digest'], captured_at=signal.decision_at)
    else:
        _record_signal_policy(ledger, signal)
    if pending or held:
        intent = replace(_intent('intent-' + identity, signal_ids=(identity,)), cohort_id=ledger.cohort_id)
        ledger.stage_intent(intent)
        _record_intent_policy(ledger, intent, signal)
        if held:
            ledger.apply_fill(intent, _fill(intent.intent_id))
    return signal


def build(owner, *, settings=None, company_map=None):
    from tradingagents.strategies.orchestration.litigation_targets import build_litigation_targets
    return build_litigation_targets(owner, SESSION, cutoff=CUTOFF,
        company_map=COMPANIES if company_map is None else company_map,
        settings=SETTINGS if settings is None else settings)


def test_all_books_and_exact_native_issuer_bindings_are_retained(owner):
    retained(owner.cohorts[0]['ledger'], 'AAPL', identity='held', held=True)
    retained(owner.cohorts[15]['ledger'], 'MSFT', identity='pending', pending=True)
    result = build(owner, settings={**SETTINGS, 'watchlist': ['GOOG'], 'case_ids': [55]})
    issuers = {row['ticker']: row for row in result['scope']['issuers']}
    assert set(issuers) == {'AAPL', 'MSFT', 'GOOG'}
    assert 'held' in issuers['AAPL']['roles'] and 'pending' in issuers['MSFT']['roles']
    assert issuers['GOOG']['roles'] == ['watchlist']
    assert issuers['AAPL']['legal_name'] == 'Apple Inc.' and issuers['AAPL']['issuer_cik'] == '0000000001'
    assert result['scope']['case_ids'] == [55]
    assert len(result['manifest']['cohorts']) == 16
    assert result['manifest']['target_population_complete'] is True
    assert result['manifest']['target_search_complete'] is True


def test_context_query_budget_keeps_every_omitted_issuer_visible(owner):
    for i, ticker in enumerate(COMPANIES[str(x)]['ticker'] for x in range(6)):
        retained(owner.cohorts[i]['ledger'], ticker, identity=f'held-{i}', held=True)
    result = build(owner)
    assert len(result['scope']['issuers']) == 5
    manifest = result['manifest']
    assert len(manifest['issuer_population']) == 6
    assert manifest['target_population_complete'] is True and manifest['target_search_complete'] is False
    assert len(manifest['omitted_issuers']) == 1
    assert manifest['omitted_issuers'][0]['reason'] == 'focused_query_budget'
    assert {x['ticker'] for x in manifest['issuer_population']} == {'AAPL','MSFT','GOOG','NVDA','IBM','CAT'}


def test_prior_shortlist_limit_is_deterministic_and_does_not_hide_discovery(owner):
    for ticker in ('NVDA', 'MSFT', 'AAPL'):
        retained(owner.cohorts[3]['ledger'], ticker, identity=ticker)
    result = build(owner, settings={**SETTINGS, 'shortlist_limit': 1})
    assert [row['ticker'] for row in result['scope']['issuers']] == ['AAPL']
    assert {row['ticker'] for row in result['manifest']['omitted_shortlist']} == {'MSFT', 'NVDA'}
    assert result['manifest']['target_search_complete'] is False


def test_future_and_journal_only_observations_do_not_enter_prior_shortlist(owner):
    retained(owner.cohorts[0]['ledger'], 'AAPL', identity='future', observed_at=CUTOFF+timedelta(seconds=1))
    retained(owner.cohorts[0]['ledger'], 'MSFT', identity='journal', journal_only=True)
    result = build(owner)
    assert result['scope']['issuers'] == []
    assert {row['reason'] for row in result['manifest']['excluded_signals']} == {'after_acquisition_cutoff','journal_only'}


def test_missing_native_projection_or_prior_policy_provenance_fails(owner):
    signal = retained(owner.cohorts[-1]['ledger'], pending=True)
    owner.cohorts[-1]['ledger'].connection.execute('DELETE FROM intent_policy_provenance')
    with pytest.raises(ValueError):
        build(owner)
    owner.cohorts[-1]['ledger'].connection.execute("UPDATE order_intents SET status = 'cancelled'")
    owner.cohorts[-1]['ledger'].connection.execute('DELETE FROM signal_policy_provenance')
    with pytest.raises(ValueError):
        build(owner)


def test_missing_or_ambiguous_company_mapping_never_uses_fuzzy_names(owner):
    settings = {**SETTINGS, 'watchlist': ['CAT']}
    bad = deepcopy(COMPANIES); del bad['5']
    with pytest.raises(ValueError):
        build(owner, settings=settings, company_map=bad)
    bad = deepcopy(COMPANIES); bad['duplicate'] = {'ticker':'CAT','cik_str':999,'title':'Other Cat Co.'}
    with pytest.raises(ValueError):
        build(owner, settings=settings, company_map=bad)


def test_frozen_validation_uses_original_seed_not_new_ledger_state(owner):
    from tradingagents.strategies.orchestration.litigation_targets import validate_litigation_targets
    settings = {**SETTINGS, 'watchlist': ['AAPL']}
    saved = build(owner, settings=settings)
    retained(owner.cohorts[0]['ledger'], 'MSFT', identity='later', pending=True)
    assert validate_litigation_targets(saved, session=SESSION, settings=settings, company_map=COMPANIES) == saved
    for mutate in (
        lambda v: v['scope']['issuers'][0].update(legal_name='Invented Company'),
        lambda v: v['manifest'].update(target_search_complete=False),
        lambda v: v['manifest']['cohorts'].pop(),
    ):
        bad = deepcopy(saved); mutate(bad)
        with pytest.raises(ValueError):
            validate_litigation_targets(bad, session=SESSION, settings=settings, company_map=COMPANIES)
    with pytest.raises(ValueError):
        validate_litigation_targets(saved, session=SESSION, settings={**settings,'watchlist':['MSFT']}, company_map=COMPANIES)


@pytest.mark.parametrize('settings', [
    {**SETTINGS, 'issuer_query_limit': 6}, {**SETTINGS, 'lookback_sessions': 0},
    {**SETTINGS, 'case_ids': list(range(1, 7))}, {**SETTINGS, 'watchlist': ['aapl']},
])
def test_invalid_scope_configuration_fails(owner, settings):
    with pytest.raises(ValueError):
        build(owner, settings=settings)


def test_configuration_only_preflight_cannot_claim_portfolio_scope():
    from tradingagents.strategies.orchestration.litigation_targets import validate_litigation_targets
    settings = {**SETTINGS, 'watchlist': ['AAPL'], 'case_ids': [55]}
    result = build(None, settings=settings)
    assert result['manifest']['acquisition_context'] == 'configuration_only'
    assert result['manifest']['cohorts'] == [] and result['manifest']['portfolio_scope_complete'] is False
    assert validate_litigation_targets(result, session=SESSION, settings=settings, company_map=COMPANIES) == result
    with pytest.raises(ValueError):
        validate_litigation_targets(result, session=SESSION, settings=settings, company_map=COMPANIES,
                                    require_portfolio_scope=True)


def test_admission_priority_is_held_pending_watchlist_then_prior(owner):
    retained(owner.cohorts[0]['ledger'], 'NVDA', identity='held', held=True)
    retained(owner.cohorts[1]['ledger'], 'MSFT', identity='pending', pending=True)
    retained(owner.cohorts[2]['ledger'], 'AAPL', identity='prior')
    result = build(owner, settings={**SETTINGS, 'watchlist': ['GOOG'], 'issuer_query_limit': 2})
    assert {r['ticker'] for r in result['scope']['issuers']} == {'NVDA', 'MSFT'}
    assert [r['ticker'] for r in result['manifest']['omitted_issuers']] == ['GOOG', 'AAPL']
    assert {r['ticker'] for r in result['manifest']['issuer_population']} == {'NVDA', 'MSFT', 'GOOG', 'AAPL'}


def test_shortlist_cap_does_not_remove_a_held_issuer(owner):
    retained(owner.cohorts[0]['ledger'], 'NVDA', identity='held', held=True)
    retained(owner.cohorts[1]['ledger'], 'AAPL', identity='prior')
    result = build(owner, settings={**SETTINGS, 'shortlist_limit': 1})
    assert {r['ticker'] for r in result['scope']['issuers']} == {'NVDA', 'AAPL'}
    assert result['manifest']['omitted_shortlist'] == []


def test_replay_rejects_mutated_retained_source_or_company_map(owner):
    from tradingagents.strategies.orchestration.litigation_targets import validate_litigation_targets
    from tradingagents.strategies.data_sources.courtlistener_scope import digest
    retained(owner.cohorts[0]['ledger'])
    result = build(owner)
    bad = deepcopy(result)
    seed = bad['manifest']['seed_refs'][0]
    seed['source']['candidate']['signal']['ticker'] = 'MSFT'
    seed['source_sha256'] = digest(seed['source'])
    with pytest.raises(ValueError):
        validate_litigation_targets(bad, session=SESSION, settings=SETTINGS, company_map=COMPANIES)
    changed = deepcopy(COMPANIES)
    changed['0']['title'] = 'Another Company'
    with pytest.raises(ValueError):
        validate_litigation_targets(result, session=SESSION, settings=SETTINGS, company_map=changed)


def test_newest_eligible_prior_evidence_wins_before_ticker_tiebreak(owner):
    retained(owner.cohorts[0]['ledger'], 'AAPL', identity='old')
    retained(owner.cohorts[1]['ledger'], 'MSFT', identity='new', observed_at=CUTOFF)
    result = build(owner, settings={**SETTINGS, 'shortlist_limit': 1})
    assert [r['ticker'] for r in result['scope']['issuers']] == ['MSFT']


def test_bound_cohort_identity_and_aware_cutoff_are_required(owner):
    from tradingagents.strategies.orchestration.litigation_targets import build_litigation_targets
    with pytest.raises(ValueError):
        build_litigation_targets(owner, SESSION, cutoff=CUTOFF.replace(tzinfo=None),
                                 company_map=COMPANIES, settings=SETTINGS)
    owner.cohorts[0]['config'].name = 'wrong-book'
    with pytest.raises(ValueError):
        build(owner)
