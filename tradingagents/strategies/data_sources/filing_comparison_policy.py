"""Opt-in current-only permission derived from complete retained SEC history."""
from __future__ import annotations

import hashlib
import json

from .edgar_source import _history_archive, _history_rows
from .fetch_errors import SourceFetchError

CURRENT_ONLY_POLICY = 'complete_history_current_only_v1'
NO_UNIQUE_PRIOR = frozenset({'missing_prior', 'ambiguous_prior_date'})


def _rows(rows):
    if not isinstance(rows, list):
        raise ValueError('invalid history rows')
    native = {name: [row[key] for row in rows] for name, key in (
        ('form', 'form'), ('filingDate', 'filing_date'),
        ('accessionNumber', 'accession_number'), ('primaryDocument', 'primary_document'))}
    if _history_rows(native) != rows:
        raise ValueError('invalid normalized history rows')
    return rows


def checked_history(history, cik):
    """Validate the retained recent rows and entire declared archive inventory."""
    if ('failure' in history or history['cik'] != cik
            or history['coverage']['complete'] is not True):
        raise ValueError('incomplete history')
    _rows(history['filings'])
    descriptors = history['archives']
    if not isinstance(descriptors, list) or len(descriptors) > 1000:
        raise ValueError('invalid archive inventory')
    if [_history_archive(cik, item) for item in descriptors] != descriptors:
        raise ValueError('invalid archive descriptor')
    if len({item['name'] for item in descriptors}) != len(descriptors):
        raise ValueError('duplicate archive descriptor')
    return history


def history_comparison(current, histories, archives, *, require_full_history=True):
    """Return every FILER outcome and exact proof; no partial-history permission.

    All declared archives that can contain earlier filings are required, including
    older archives when the recent rows already have a candidate or date tie.
    `require_full_history=False` preserves nearest-date archive selection for
    ordinary comparisons and identifying candidates needing expanded history.
    Archives use the durable graph's `CIK/name` keys. No source calls occur here.
    """
    from .filing_hydration import _nearest, _role_ciks
    ciks = _role_ciks(current, 'FILER')
    proof = {'recent': {}, 'archives': {}}
    outcomes, selected = {}, []
    if not ciks:
        return {'status': 'missing_issuer_roles', 'issuer_ciks': ciks}, proof
    for cik in ciks:
        try:
            recent = checked_history(histories[cik], cik)
            proof['recent'][cik] = recent
            rows = list(recent['filings'])
            candidate, _ = _nearest(rows, current['form'], current['filing_date'])
            floor = candidate['filing_date'] if candidate and not require_full_history else '0001-01-01'
            for descriptor in recent['archives']:
                if descriptor['filingFrom'] >= current['filing_date'] or descriptor['filingTo'] < floor:
                    continue
                ref = cik + '/' + descriptor['name']
                archive = archives[ref]
                if ('failure' in archive or archive['cik'] != cik
                        or archive['coverage']['complete'] is not True
                        or archive['descriptor'] != descriptor):
                    raise ValueError('incomplete archive')
                archive_rows = _rows(archive['filings'])
                if len(archive_rows) != descriptor['filingCount'] or any(
                        not descriptor['filingFrom'] <= row['filing_date'] <= descriptor['filingTo']
                        for row in archive_rows):
                    raise ValueError('archive count or range mismatch')
                proof['archives'][ref] = archive
                rows.extend(archive_rows)
            candidate, status = _nearest(rows, current['form'], current['filing_date'])
            outcomes[cik] = {'status': status}
            if candidate is not None:
                outcomes[cik]['selected'] = candidate
                selected.append(candidate)
        except (KeyError, TypeError, AttributeError, ValueError, SourceFetchError):
            outcomes[cik] = {'status': 'unproven_prior'}
    statuses = {value['status'] for value in outcomes.values()}
    if 'unproven_prior' in statuses:
        status = 'unproven_prior'
    elif 'conflicting_history_metadata' in statuses:
        status = 'conflicting_history_metadata'
    elif statuses <= NO_UNIQUE_PRIOR:
        status = 'ambiguous_prior_date' if 'ambiguous_prior_date' in statuses else 'missing_prior'
    elif statuses == {'selected'} and all(value == selected[0] for value in selected):
        status = 'selected'
    else:
        status = 'unresolved_joint_prior'
    result = {'status': status, 'issuer_ciks': ciks, 'issuer_outcomes': outcomes}
    if status == 'selected':
        result['candidate'] = selected[0]
    return result, proof


def current_only_binding(current, comparison, proof):
    if comparison['status'] not in NO_UNIQUE_PRIOR:
        raise ValueError('invalid_filing_comparator')
    return {'policy': CURRENT_ONLY_POLICY, 'assessment_scope': 'current_only',
        'comparative_claims_allowed': False, 'reason': comparison['status'],
        'current_accession': current['accession'], 'form_type': current['form'],
        'current_filing_date': current['filing_date'], 'issuer_ciks': comparison['issuer_ciks'],
        'history_refs': sorted(proof['recent']), 'archive_refs': sorted(proof['archives']),
        'issuer_outcomes': comparison['issuer_outcomes'],
        'history_snapshot_sha256': hashlib.sha256(json.dumps(proof, sort_keys=True,
            separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest()}


def validate_current_only_binding(graph, binding, current):
    """Revalidate durable permission for model input, replay, or report acceptance."""
    try:
        if (current['structural_status'] != 'complete' or current['issues'] != []
                or current['form'] not in ('10-K', '10-Q')):
            raise ValueError('incomplete current filing')
        if graph['coverage']['comparator_policy'] != CURRENT_ONLY_POLICY:
            raise ValueError('policy not enabled')
        comparison, proof = history_comparison(current, graph['history_corpus'], graph['archive_corpus'])
        if binding != current_only_binding(current, comparison, proof):
            raise ValueError('proof mismatch')
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError('invalid_filing_comparator') from error
    return comparison
