"""Explicit prospective exclusion of SEC filing research acquisition."""

POLICY = 'disabled_v1'
FULL_FILING_POLICIES = (
    'filing_evidence_policy', 'filing_comparison_policy', 'filing_parser_policy',
    'filing_attribution_policy', 'filing_acquisition_policy', 'filing_material_policy',
)


def configured(config: dict) -> bool:
    value = config.get('filing_research_policy')
    if value is None:
        return False
    if type(value) is not str or value != POLICY:
        raise ValueError('invalid_filing_research_policy')
    conflicts = set(FULL_FILING_POLICIES) | {
        key for key in config if key.startswith('filing_') and 'manifest' in key}
    if (config.get('research_resilience_policy') != 'dependency_isolation_v1'
            or any(config.get(key) is not None for key in conflicts)):
        raise ValueError('invalid_filing_research_policy: disabled research requires '
                         'dependency isolation without full-filing policies or manifests')
    return True


STABILIZATION_POLICY = 'supported_sixteen_books_v1'
DECLARATION_KEY = '_filing_research_policy'
REASON = 'filing_research_disabled'
CONSUMERS = ('filing_analysis', 'insider_activity', 'quantum_readiness')


def sentinel():
    """Exact bulk-research non-admission; SEC issuer identity is separate."""
    return {'error': REASON, 'coverage': {'complete': False, 'reason': REASON,
            'filing_research_policy': POLICY}}


def declaration(config):
    """Only the explicit supported profile opts in to strategy exclusions."""
    if config.get('stabilization_profile_policy') != STABILIZATION_POLICY:
        return {}
    if not configured(config) or any(config.get('disabled_strategies', {}).get(name) != REASON
                                     for name in CONSUMERS):
        raise ValueError('invalid_filing_research_policy')
    from .source_lifecycle_policy import _digest
    from ..modules import get_paper_trade_strategies
    document = {'policy': POLICY, 'capability': 'bulk_filing_research',
        'disabled_strategies': dict.fromkeys(CONSUMERS, REASON),
        'strategy_sources': {strategy.name: sorted(strategy.data_sources)
            for strategy in get_paper_trade_strategies() if strategy.name in CONSUMERS}}
    return {DECLARATION_KEY: {**document, 'sha256': _digest(document)}}


def declared_scope(data, config=None):
    from copy import deepcopy
    value = data.get(DECLARATION_KEY)
    if config is not None and declaration(config).get(DECLARATION_KEY) != value:
        raise ValueError('invalid_filing_research_policy')
    if value is None:
        return None
    expected = declaration({'stabilization_profile_policy': STABILIZATION_POLICY,
        'filing_research_policy': POLICY, 'research_resilience_policy': 'dependency_isolation_v1',
        'disabled_strategies': dict.fromkeys(CONSUMERS, REASON)})[DECLARATION_KEY]
    if value != expected or data.get('edgar') != sentinel():
        raise ValueError('invalid_filing_research_policy')
    return deepcopy(value)


def health_evidence(scope, strategy):
    return ({'filing_research_policy': POLICY, 'filing_research_sha256': scope['sha256'],
             'data_sources': scope['strategy_sources'][strategy]}
            if scope is not None and strategy in scope['disabled_strategies'] else {})


def validate_health(scope, records, policy_ids):
    rows = [row if isinstance(row, dict) else vars(row) for row in records]
    if scope is None:
        if any('filing_research_policy' in row.get('evidence', {}) for row in rows):
            raise ValueError('invalid_filing_research_policy')
        return
    keys = [(row.get('policy_id'), row.get('strategy')) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError('invalid_filing_research_policy')
    indexed = dict(zip(keys, rows))
    for policy_id in policy_ids:
        for name, reason in scope['disabled_strategies'].items():
            row = indexed.get((policy_id, name), {})
            evidence = row.get('evidence', {})
            if (row.get('status') != 'disabled_by_policy' or type(row.get('signal_count')) is not int
                    or row['signal_count'] != 0 or type(evidence) is not dict
                    or evidence.get('reason') != reason or type(evidence.get('candidate_count')) is not int
                    or evidence['candidate_count'] != 0
                    or any(evidence.get(key) != value for key, value in health_evidence(scope, name).items())):
                raise ValueError('invalid_filing_research_policy')
