"""A declared retirement cannot erase acquired failures or grant authority."""
from copy import deepcopy

import pytest


CONFIG = {
    'enrichment_capability_policy': 'optional_enrichment_retirement_v1',
    'retired_enrichment_capabilities': {
        'commodity_futures_curves': 'maturity_identified_curve_evidence_unavailable'},
}


def test_explicit_configuration_preserves_legacy_without_policy():
    from tradingagents.strategies.orchestration.enrichment_capabilities import configured
    assert configured({}) is False
    assert configured({'enrichment_capability_policy': None}) is False
    assert configured(CONFIG) is True


@pytest.mark.parametrize('config', [
    {'retired_enrichment_capabilities': CONFIG['retired_enrichment_capabilities']},
    {'enrichment_capability_policy': 'ignore_errors'},
    {'enrichment_capability_policy': True},
    {'enrichment_capability_policy': CONFIG['enrichment_capability_policy']},
    {**CONFIG, 'retired_enrichment_capabilities': {}},
    {**CONFIG, 'retired_enrichment_capabilities': {'profiles': 'unavailable'}},
    {**CONFIG, 'retired_enrichment_capabilities': {'commodity_futures_curves': 'timeout'}},
])
def test_configuration_cannot_retire_unknown_scope(config):
    from tradingagents.strategies.orchestration.enrichment_capabilities import configured
    with pytest.raises(ValueError, match='enrichment_capability'):
        configured(config)


def test_declaration_binds_finite_unique_symbols_without_mutating_config():
    from tradingagents.strategies.orchestration.enrichment_capabilities import declaration, declared_scope, DECLARATION_KEY
    before = deepcopy(CONFIG)
    original = declaration(CONFIG, ['SI', 'NG', 'SI'])
    scope = declared_scope(original, CONFIG)
    assert scope['underlying_symbols'] == ['NG', 'SI']
    assert scope['retired_capabilities'] == {
        'commodity_futures_curves': 'maturity_identified_curve_evidence_unavailable'}
    assert len(scope['sha256']) == 64
    scope['underlying_symbols'].append('GC')
    assert original[DECLARATION_KEY]['underlying_symbols'] == ['NG', 'SI']
    assert CONFIG == before
    assert declared_scope(declaration(CONFIG, []), CONFIG)['underlying_symbols'] == []
    assert declaration({}, []) == {}


@pytest.mark.parametrize('symbols', [['UNKNOWN'], ['NG', 3], 'NG', None])
def test_declaration_rejects_unknown_symbols(symbols):
    from tradingagents.strategies.orchestration.enrichment_capabilities import declaration
    with pytest.raises(ValueError, match='enrichment_capability'):
        declaration(CONFIG, symbols)


@pytest.mark.parametrize('change', [
    lambda d, key: d[key].update(policy='ignore_errors'),
    lambda d, key: d[key].update(sha256='0' * 64),
    lambda d, key: d[key].update(underlying_symbols=['SI', 'NG']),
    lambda d, key: d[key].update(underlying_symbols=['NG', 'NG']),
    lambda d, key: d[key].update(extra=True),
    lambda d, key: d.update(commodity_futures_curves={}),
    lambda d, key: d.update(errors={'commodity_futures_curves': {}}),
])
def test_projection_rejects_scope_tampering_and_retired_operation_injection(change):
    from tradingagents.strategies.orchestration.enrichment_capabilities import declaration, DECLARATION_KEY
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    original = declaration(CONFIG, ['NG', 'SI'])
    change(original, DECLARATION_KEY)
    with pytest.raises(ValueError, match='enrichment_capability'):
        project_enrichment(original, capability_config=CONFIG)


def test_projection_requires_exact_configured_declaration():
    from tradingagents.strategies.orchestration.enrichment_capabilities import declaration
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    for original, config in (({}, CONFIG), (declaration(CONFIG, []), {})):
        with pytest.raises(ValueError, match='enrichment_capability'):
            project_enrichment(original, capability_config=config)


def test_retirement_is_clean_supported_enrichment_but_excluded_full_coverage():
    from tradingagents.strategies.orchestration.enrichment_capabilities import declaration, supported_complete
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment, digest
    original = {**declaration(CONFIG, ['NG']), 'profiles': {'UNG': {'sector': 'Energy'}}}
    before = deepcopy(original)
    projected, scope = project_enrichment(original, capability_config=CONFIG)
    assert projected == {'profiles': {'UNG': {'sector': 'Energy'}}}
    assert scope['version'] == 'research-enrichment-selection-v2'
    assert scope['excluded'] == []
    assert scope['supported_enrichment_complete'] is True
    assert supported_complete(scope, CONFIG) is True
    assert scope['research_coverage_complete'] is False
    assert scope['order_authority'] is False
    assert scope['original_sha256'] == digest(original)
    assert original == before


@pytest.mark.parametrize('failure', [
    {'profiles': {'BAD': {'error': 'invalid facts'}}},
    {'errors': {'factors': {'reason_code': 'timeout'}}},
    {'errors': {'unknown_operation': {'reason_code': 'timeout'}}},
    {'error': 'whole-provider failure'},
    {'research_hold_reasons': ['model_deadline_exhausted']},
    {'research_hold_reasons': ['shared_regime_unavailable']},
    {'research_hold_reasons': ['required_universe_unavailable']},
])
def test_real_failures_and_holds_still_prevent_supported_completion(failure):
    from tradingagents.strategies.orchestration.enrichment_capabilities import declaration, supported_complete
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    original = {**declaration(CONFIG, []), **failure}
    _, scope = project_enrichment(original, capability_config=CONFIG)
    assert scope['supported_enrichment_complete'] is False
    assert supported_complete(scope, CONFIG) is False
    scope['supported_enrichment_complete'] = True
    with pytest.raises(ValueError, match='enrichment_capability'):
        supported_complete(scope, CONFIG)


def test_legacy_projection_keeps_recorded_curve_failures_and_receipt_schema():
    from tradingagents.strategies.orchestration.enrichment_capabilities import supported_complete
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    _, scope = project_enrichment({'errors': {'commodity_futures_curves': {'NG': {'error': 'retired'}}}})
    assert scope['version'] == 'research-enrichment-selection-v1'
    assert 'supported_enrichment_complete' not in scope
    assert scope['excluded'] == [{'operation': 'commodity_futures_curves', 'symbol': 'NG',
        'reason': 'recorded_source_failure'}]
    assert supported_complete(scope, {}) is False
    _, clean = project_enrichment({})
    assert supported_complete(clean, {}) is True


def test_legacy_supported_helper_rejects_forged_full_coverage():
    from tradingagents.strategies.orchestration.enrichment_capabilities import supported_complete
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    _, scope = project_enrichment({'errors': {'profiles': {'BAD': {'error': 'offline'}}}})
    scope['research_coverage_complete'] = True
    with pytest.raises(ValueError, match='enrichment_capability'):
        supported_complete(scope, {})


def test_supported_helper_rejects_malformed_exclusion_operation():
    from tradingagents.strategies.orchestration.enrichment_capabilities import declaration, supported_complete
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    _, scope = project_enrichment(declaration(CONFIG, []), capability_config=CONFIG)
    scope['excluded'] = [{'operation': [], 'symbol': None, 'reason': 'recorded_source_failure'}]
    scope['supported_enrichment_complete'] = False
    with pytest.raises(ValueError, match='enrichment_capability'):
        supported_complete(scope, CONFIG)
