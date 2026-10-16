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
