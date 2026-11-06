"""Recheck prospective current-only permissions in frozen source observations."""
from __future__ import annotations

from tradingagents.strategies.data_sources.edgar_source import normalize_filing_form
from tradingagents.strategies.data_sources.filing_comparison_policy import (
    CURRENT_ONLY_POLICY, validate_current_only_binding,
)


def validate_filing_comparison_policy(data: dict, config: dict) -> dict | None:
    """Validate every labeled row and its counts without acquiring new evidence.

    An acquisition failure grants no permission. Valid retained rows still need
    validation when another EDGAR operation failed in the same acquisition.
    """
    try:
        policy = config.get('filing_comparison_policy')
        if policy is not None and (policy != CURRENT_ONLY_POLICY
                or config.get('filing_evidence_policy') != 'complete_submission_v1'):
            raise ValueError('unsupported policy')
        edgar = data.get('edgar', {})
        graph = edgar.get('filing_evidence')
        rows = [row for name in ('filings', 'activist_13d', 'passive_13g', 'pqc_filings')
                for row in edgar.get(name, [])]
        declared = graph.get('coverage', {}).get('comparator_policy') if isinstance(graph, dict) else None
        labeled = [row for row in rows if row.get('filing_assessment_scope') == 'current_only'
                   or (row.get('comparison_binding') or {}).get('policy') == CURRENT_ONLY_POLICY]
        if policy is None:
            if declared is not None or labeled:
                raise ValueError('undeclared current-only policy')
            return None
        if not labeled and graph is None and isinstance(edgar.get('error'), str) and edgar['error'].strip():
            return None
        if declared != policy or graph['policy'] != 'complete_submission_v1':
            raise ValueError('missing policy graph')
        counts = {'current_only_rows': len(labeled), 'current_only_absent_rows': 0,
                  'current_only_ambiguous_rows': 0}
        for row in labeled:
            binding = row['comparison_binding']
            ref = row['filing_evidence_ref']
            current = graph['corpus'][ref]
            if (row.get('filing_assessment_scope') != 'current_only'
                    or row.get('requires_prior') is not True
                    or row.get('prior_evidence_ref') is not None
                    or row.get('prior_status') != binding['reason']
                    or row.get('filing_evidence_status') != 'complete'
                    or ref != current['accession']
                    or (row.get('adsh') or row.get('accession_number')) != ref
                    or normalize_filing_form(row['form_type']) != current['form']
                    or row['file_date'] != current['filing_date']):
                raise ValueError('contradictory current-only row')
            validate_current_only_binding(graph, binding, current)
            counts['current_only_absent_rows' if binding['reason'] == 'missing_prior'
                   else 'current_only_ambiguous_rows'] += 1
        if any(type(graph['coverage'].get(key)) is not int or graph['coverage'][key] != value
               for key, value in counts.items()):
            raise ValueError('current-only count mismatch')
        return {'policy': policy, **counts, 'comparative_claims_allowed': False}
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError('invalid_filing_comparison_policy') from error
