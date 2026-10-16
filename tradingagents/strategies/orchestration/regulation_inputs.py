"""Bind complete regulatory inputs and named applicability to frozen sources."""
from __future__ import annotations

from tradingagents.strategies.data_sources.regulation_evidence import POLICY, validate_regulation_source
from tradingagents.strategies.data_sources.regulation_assessment import (
    validate_target_bindings,
)


def validate_regulation_policy(data, config):
    """Recheck the declared source contract without fetching or exempting failures."""
    try:
        policy = config.get('regulation_evidence_policy')
        retrieval = config.get('regulation_retrieval_policy')
        from tradingagents.strategies.data_sources.regulation_government_copy import POLICIES as RETRIEVAL_POLICIES
        if retrieval is not None and (
                type(retrieval) is not str or retrieval not in RETRIEVAL_POLICIES or policy != POLICY):
            raise ValueError('invalid regulation retrieval policy')
        source = data.get('regulations', {})
        graph = source.get('regulation_evidence')
        if graph is not None and graph.get('retrieval_policy') != retrieval:
            raise ValueError('regulation retrieval policy differs from configured scope')
        labeled = any(any(key in row for key in ('regulation_evidence_ref',
            'regulation_evidence_status', 'regulation_failure')) for row in source.get('proposed_rules', []))
        if policy is None:
            if graph is not None or labeled:
                raise ValueError('undeclared regulation evidence')
            return None
        from .research_resilience import configured as resilience_configured
        if (resilience_configured(config) and policy == POLICY and graph is None
                and isinstance(source.get('error'), str) and source['error'].strip()):
            # An outage before graph publication is a retained research gap.
            # A declared accepted record without its graph is still corruption.
            if any(row.get('regulation_evidence_status') == 'complete'
                   for row in source.get('proposed_rules', [])):
                raise ValueError('accepted regulation evidence graph missing')
            return None
        if policy != POLICY or graph is None:
            raise ValueError('missing regulation evidence graph')
        summary = validate_regulation_source(source)
        error = source.get('error')
        has_error = isinstance(error, str) and bool(error.strip())
        if (not summary['content_complete']) != has_error or (error is not None and not has_error):
            raise ValueError('contradictory regulation acquisition status')
        return summary
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError('invalid_regulation_evidence_policy') from error


def regulation_analysis_inputs(candidate, shared_data, universe):
    """Recompute every proof from the retained document and frozen universe.

    Free prose cannot certify issuer applicability; directional relationships
    remain unresolved until an independent authority contract supports them.
    No network lookups, title fallback, or model-suggested target attestation.
    """
    try:
        metadata = candidate.metadata
        source = shared_data['regulations']
        if validate_regulation_source(source)['content_complete'] is not True:
            raise ValueError('invalid_regulation_source')
        ref = metadata['regulation_evidence_ref']
        if (metadata['regulation_evidence_policy'] != POLICY
                or metadata['analysis_type'] != 'regulation'
                or ref != metadata['document_id']
                or metadata['regulation_evidence_status'] != 'complete'
                or metadata.get('regulation_failure') is not None):
            raise ValueError('invalid_regulation_source')
        rows = [row for row in source['proposed_rules'] if row['document_id'] == ref]
        if not rows or not any(all(metadata.get(key) == row.get(key) for key in
                ('document_id', 'title', 'agency_id', 'posted_date', 'regulation_evidence_ref',
                 'regulation_evidence_status', 'regulation_failure')) for row in rows):
            raise ValueError('invalid_regulation_source')
        evidence = source['regulation_evidence']['corpus'][ref]
        company_map = shared_data.get('edgar', {}).get('company_tickers', {})
        # SEC identity and universe membership do not prove rule applicability.
        # No automatic prose-derived issuer bridge is supported by this policy.
        bindings = validate_target_bindings(evidence, [], universe, company_map)
        return {'evidence': evidence, 'target_bindings': bindings}
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise ValueError('invalid_regulation_source') from error
