"""Native collection must honor an explicit retirement without hiding failures."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tradingagents.strategies.orchestration.cohort_orchestrator import CohortOrchestrator


POLICY = {
    'enrichment_capability_policy': 'optional_enrichment_retirement_v1',
    'retired_enrichment_capabilities': {
        'commodity_futures_curves': 'maturity_identified_curve_evidence_unavailable',
    },
}


class EnrichmentSource:
    def __init__(self):
        self.calls = []

    def is_available(self):
        return True

    def fetch_profiles(self, symbols):
        self.calls.append(('profiles', tuple(symbols)))
        return {'profiles': {symbol: {'sector': 'Technology'} for symbol in symbols}, 'errors': {}}

    def fetch_short_interest(self, symbols):
        self.calls.append(('short_interest', tuple(symbols)))
        return {'short_interest': {}, 'errors': {}, 'acquisition': {'observed': True}}

    def fetch(self, params):
        self.calls.append((params['method'], params.get('symbol')))
        if params['method'] == 'commodity_futures_curve':
            return {'error': 'unsupported futures curve: maturity-identified curve evidence required',
                    'reason_code': 'provider_error'}
        return {'factors': {'mkt_rf': 0.01}}


def owner(config=None, source=None):
    result = object.__new__(CohortOrchestrator)
    result._base_config = {'autoresearch': deepcopy(config or {})}
    result.cohorts = [{'engine': SimpleNamespace(registry={'openbb': source or EnrichmentSource()})}]
    return result


def test_retired_curves_are_not_dispatched_and_other_enrichment_is_preserved():
    source = EnrichmentSource()
    result = owner(POLICY, source)._fetch_openbb_enrichment(
        [{'ticker': 'UNG'}, {'ticker': 'SLV'}, {'ticker': 'AAPL'}, {'ticker': 'UNG'}])
    assert source.calls == [('profiles', ('AAPL', 'SLV', 'UNG')),
                            ('short_interest', ('AAPL', 'SLV', 'UNG')),
                            ('factors_fama_french', None)]
    assert set(result['profiles']) == {'AAPL', 'SLV', 'UNG'}
    assert result['factors'] == {'mkt_rf': 0.01}
    assert result['_enrichment_capability_policy']['underlying_symbols'] == ['NG', 'SI']
    assert 'errors' not in result


def test_absent_retirement_policy_keeps_legacy_curve_errors():
    source = EnrichmentSource()
    result = owner({}, source)._fetch_openbb_enrichment([{'ticker': 'UNG'}])
    assert ('commodity_futures_curve', 'NG') in source.calls
    assert result['errors']['commodity_futures_curves']['NG']['reason_code'] == 'provider_error'
    assert '_enrichment_capability_policy' not in result


@pytest.mark.parametrize('change', [
    {'enrichment_capability_policy': 'unknown'},
    {'retired_enrichment_capabilities': {'profiles': 'not_needed'}},
    {'enrichment_capability_policy': None},
])
def test_invalid_retirement_scope_fails_before_optional_provider_calls(change):
    source = EnrichmentSource()
    config = {**deepcopy(POLICY), **change}
    with pytest.raises(ValueError):
        owner(config, source)._fetch_openbb_enrichment([{'ticker': 'UNG'}])
    assert source.calls == []


def test_no_candidates_still_records_configured_retirement_scope():
    source = EnrichmentSource()
    result = owner(POLICY, source)._fetch_openbb_enrichment([])
    assert result['_enrichment_capability_policy']['underlying_symbols'] == []
    assert source.calls == []


def test_broad_commodity_fund_has_no_single_retired_underlying():
    result = owner(POLICY)._fetch_openbb_enrichment([{'ticker': 'PDBC'}])
    assert result['_enrichment_capability_policy']['underlying_symbols'] == []


def test_unavailable_admitted_enrichment_cannot_become_successful_retirement():
    value = owner(POLICY)
    value.cohorts[0]['engine'].registry = {}
    result = value._fetch_openbb_enrichment([{'ticker': 'UNG'}])
    assert result['_enrichment_capability_policy']['underlying_symbols'] == ['NG']
    assert result['errors']['openbb']['reason_code'] == 'provider_error'


def test_retirement_requires_the_research_projection_boundary():
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    with pytest.raises(ValueError, match='enrichment_capability'):
        MultiStrategyEngine({'autoresearch': deepcopy(POLICY)})


def test_research_input_replay_reconstructs_retirement_scope(owner):
    from test_research_decision_inputs import source, forbid
    from test_research_screening import SESSION
    from tradingagents.strategies.orchestration import decision_clock
    from tradingagents.strategies.orchestration.enrichment_capabilities import declaration
    ar = owner._base_config['autoresearch']
    ar.update(deepcopy(POLICY))
    data, digest = source(owner)
    original = {**declaration(ar, ['NG', 'SI']), 'profiles': {'AAA': {'sector': 'Technology'}}}
    first = decision_clock.prepare_decision_inputs(owner, SESSION, data, digest, lambda: deepcopy(original))
    assert first[0] == original
    assert decision_clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid) == first


def test_held_research_retains_retirement_declaration_without_acquisition(owner):
    import time
    from test_research_decision_inputs import source, checkpoint, forbid
    from test_research_screening import SESSION
    from tradingagents.strategies.orchestration import decision_clock
    from tradingagents.strategies.orchestration.enrichment_capabilities import declared_scope
    ar = owner._base_config['autoresearch']
    ar.update(deepcopy(POLICY))
    data, digest = source(owner)
    checkpoint(owner, digest, ('shared_regime_unavailable',))
    enrichment, _ = decision_clock.prepare_decision_inputs(owner, SESSION, data, digest, forbid,
        hold_reasons=('shared_regime_unavailable',), deadline=time.monotonic()-1)
    assert declared_scope(enrichment, ar)['underlying_symbols'] == []
    assert enrichment['research_hold_reasons'] == ['shared_regime_unavailable']


# The native accepted-input fixture uses real immutable source stores and ledgers.
from test_research_screening import owner as owner_fixture

@pytest.fixture(name='owner')
def accepted_input_owner(owner_fixture):
    return owner_fixture
