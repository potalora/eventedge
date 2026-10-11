"""Retain exact material gaps while proving the remaining analysis population.

Strict source completeness never changes. This separately bound permission
removes three explicit framing-only envelopes from downstream analysis.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json

from tradingagents.strategies.data_sources.filing_material_policy import (
    POLICY, configured, policy_manifest, policy_manifest_sha256, validate_quarantined_evidence,
)

COLLECTIONS = ('filings', 'activist_13d', 'passive_13g', 'pqc_filings')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _fail():
    raise ValueError('invalid_filing_material_policy')


def _required(category, row):
    from tradingagents.strategies.data_sources.edgar_source import normalize_filing_form, filing_form_family
    form = normalize_filing_form(row.get('form_type', ''))
    required = (category == 'pqc_filings' or form in {'10-K', '10-Q', '8-K', 'DEF 14A'}
                or filing_form_family(form) in {'SCHEDULE 13D', 'SCHEDULE 13G'})
    if category != 'pqc_filings' and row.get('universe_membership', {}).get('status') == 'excluded':
        required = False
    return required, required and category == 'filings' and form in {'10-K', '10-Q'}


def build_material_scope(graph, collections):
    """Recompute failure causes per occurrence, including quarantined occurrences."""
    from tradingagents.strategies.data_sources.edgar_source import normalize_filing_form, filing_form_family
    from tradingagents.strategies.data_sources.filing_acquisition import complete_submission_url
    from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence
    from tradingagents.strategies.data_sources.filing_comparison_policy import validate_current_only_binding
    try:
        if (graph.get('policy') != 'complete_submission_v1' or not isinstance(collections, dict)
                or set(collections) - set(COLLECTIONS) or not isinstance(graph.get('corpus'), dict)):
            _fail()
        coverage, corpus = graph['coverage'], graph['corpus']
        quarantines, hashes = {}, {}
        approved_accessions = {item['accession'] for item in policy_manifest()['identities']}
        for accession, value in corpus.items():
            if (value.get('accession') != accession
                    or (accession in approved_accessions and 'material_quarantine' not in value)):
                _fail()
            hashes[accession] = _digest(value)
            if 'material_quarantine' in value:
                approved = validate_quarantined_evidence(value)
                declaration = value['material_quarantine']
                quarantines[accession] = {key: approved[key] for key in
                    ('accession', 'form', 'filing_date', 'issuer_cik', 'document_count', 'gap_codes')}
                quarantines[accession].update(submission_size=declaration['submission_size'],
                    submission_sha256=value['submission_sha256'], header_sha256=value['header_sha256'],
                    source_url_sha256=hashlib.sha256(value['source_url'].encode()).hexdigest(),
                    evidence_sha256=hashes[accession], occurrences=[])
        entries, by_collection = [], {}
        required_rows = strict_failed = scoped_failed = 0
        from tradingagents.strategies.data_sources.filing_attribution_policy import POLICY as ATTRIBUTION_POLICY
        attribution = coverage.get('attribution_policy') == ATTRIBUTION_POLICY
        checked = set()
        for category in COLLECTIONS:
            rows = collections.get(category, [])
            if not isinstance(rows, list):
                _fail()
            counts = {'total_rows': len(rows), 'quarantined_rows': 0}
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    _fail()
                required, needs_prior = _required(category, row)
                if row.get('requires_prior') is not needs_prior:
                    _fail()
                required_rows += int(required)
                accession = row.get('adsh') or row.get('accession_number')
                ref, prior_ref = row.get('filing_evidence_ref'), row.get('prior_evidence_ref')
                value = corpus.get(ref) if isinstance(ref, str) else None
                quarantine = value is not None and ref in quarantines
                independent = bool(row.get('filing_failure')) or row.get('discovery_identity_conflict') is True
                if value is not None:
                    if (ref != accession or value['form'] != normalize_filing_form(row.get('form_type', ''))
                            or value['filing_date'] != row.get('file_date')):
                        _fail()
                if quarantine:
                    if (not required or complete_submission_url(row['file_url'], ref) != value['source_url']
                            or row.get('filing_material_disposition') != 'quarantined'
                            or row.get('material_gap_codes') != quarantines[ref]['gap_codes']
                            or row.get('filing_evidence_status') != 'insufficient'
                            or row.get('text_status') != 'insufficient'
                            or prior_ref is not None or row.get('comparison_binding') is not None
                            or row.get('filing_assessment_scope') is not None
                            or (needs_prior and row.get('prior_status') != 'not_assessed_material_quarantine')
                            or (not needs_prior and row.get('prior_status') is not None)):
                        _fail()
                    required_exhibits = row.get('required_exhibits', ())
                    if not isinstance(required_exhibits, (list, tuple)) or any(
                            name not in value['material_quarantine']['required_exhibits'] for name in required_exhibits):
                        _fail()
                    quarantines[ref]['occurrences'].append({'collection': category, 'index': index})
                    counts['quarantined_rows'] += 1
                    strict_failed += 1
                    scoped_failed += int(independent)
                elif required:
                    failed = (independent or value is None or value.get('structural_status') != 'complete'
                              or row.get('filing_evidence_status') != 'complete' or row.get('text_status') != 'available')
                    if value is not None and value.get('structural_status') == 'complete':
                        if ref not in checked:
                            _validate_evidence(value, allow_joint_issuers=attribution)
                            checked.add(ref)
                        binding = row.get('issuer_binding', {})
                        failed |= binding.get('status') != 'verified' and not attribution
                        if filing_form_family(value['form']) in {'SCHEDULE 13D', 'SCHEDULE 13G'}:
                            failed |= binding.get('execution_status') not in {'verified', 'outside_declared_equity_universe'} and not attribution
                    if needs_prior:
                        if row.get('filing_assessment_scope') == 'current_only':
                            validate_current_only_binding(graph, row['comparison_binding'], value)
                        elif row.get('prior_status') == 'available' and prior_ref in corpus and prior_ref not in quarantines:
                            from .filing_inputs import _comparison
                            _validate_evidence(corpus[prior_ref])
                            _comparison(graph, row['comparison_binding'], value, corpus[prior_ref])
                        else:
                            failed = True
                    # Quarantined prior evidence never becomes an exception for another current filing.
                    failed |= prior_ref in quarantines
                    strict_failed += int(failed)
                    scoped_failed += int(failed)
                elif row.get('filing_material_disposition') is not None or row.get('material_gap_codes') is not None:
                    _fail()
                entries.append({'collection': category, 'index': index, 'accession': accession,
                    'evidence_ref': ref, 'prior_evidence_ref': prior_ref, 'row_sha256': _digest(row),
                    'evidence_sha256': hashes.get(ref), 'disposition': 'quarantined' if quarantine else 'unchanged'})
            by_collection[category] = counts
        if (type(coverage.get('required_rows')) is not int or coverage['required_rows'] != required_rows
                or type(coverage.get('failed_rows')) is not int or coverage['failed_rows'] != strict_failed
                or type(coverage.get('complete')) is not bool
                or type(coverage.get('deadline_exhausted')) is not bool
                or type(coverage.get('discovery_complete')) is not bool):
            _fail()
        acquisition = graph.get('acquisition_scope', {})
        spool = acquisition.get('spool', {})
        closed = (not acquisition or (spool.get('closed') is True and not any(spool.get(key) for key in
            ('active_child_copies', 'active_parent_writers', 'active_parent_readers', 'parent_bytes',
             'child_bytes', 'transient_bytes', 'cleanup_failures', 'failed_objects'))))
        scope_failure = coverage.get('scope_failure')
        if scope_failure is not None and (not isinstance(scope_failure, dict)
                or set(scope_failure) != {'code', 'reason_code'}
                or scope_failure['code'] != 'filing_scope_closure_failure'
                or not isinstance(scope_failure['reason_code'], str) or not scope_failure['reason_code']):
            _fail()
        global_complete = (scope_failure is None and not coverage['deadline_exhausted'] and coverage['discovery_complete'] and closed
            and not coverage.get('rejected_evidence_objects', 0) and not coverage.get('rejected_evidence_bytes', 0))
        # Strict false may include resource failures even when all accepted rows are complete.
        if coverage['complete'] and (strict_failed or not global_complete):
            _fail()
        scope = {'policy': POLICY, 'policy_manifest_sha256': policy_manifest_sha256(),
            'approved_accessions': sorted(item['accession'] for item in policy_manifest()['identities']),
            'total_rows': len(entries), 'required_rows': required_rows,
            'quarantined_rows': sum(item['quarantined_rows'] for item in by_collection.values()),
            'quarantined_accessions': sorted(quarantines),
            'quarantines': [quarantines[key] for key in sorted(quarantines)],
            'strict_complete': coverage['complete'], 'strict_failed_rows': strict_failed,
            'scoped_complete': scoped_failed == 0 and global_complete,
            'scoped_failed_rows': scoped_failed, 'by_collection': by_collection, 'rows': entries}
        return {**scope, 'manifest_sha256': _digest(scope)}
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError('invalid_filing_material_policy') from error


def _summary(scope):
    return deepcopy({key: value for key, value in scope.items() if key != 'rows'})


def validate_filing_material_policy(data, config):
    try:
        enabled = configured(config)
        edgar = data.get('edgar', {})
        graph = edgar.get('filing_evidence')
        declared = graph.get('coverage', {}).get('material_policy') if isinstance(graph, dict) else None
        if not enabled:
            if isinstance(graph, dict) and (declared is not None or 'material_scope' in graph
                    or any('material_quarantine' in item for item in graph.get('corpus', {}).values())):
                _fail()
            if any(row.get('filing_material_disposition') is not None for key in COLLECTIONS for row in edgar.get(key, [])):
                _fail()
            return None
        error = isinstance(edgar.get('error'), str) and bool(edgar['error'].strip())
        if graph is None and error:
            return None
        if not isinstance(graph, dict) or declared != POLICY:
            _fail()
        expected = build_material_scope(graph, {key: edgar.get(key, []) for key in COLLECTIONS})
        if _digest(graph.get('material_scope')) != _digest(expected) or (not expected['scoped_complete'] and not error):
            _fail()
        for key in ('scoped_complete', 'scoped_failed_rows', 'quarantined_rows'):
            if _digest(graph['coverage'].get(key)) != _digest(expected[key]):
                _fail()
        return _summary(expected)
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError('invalid_filing_material_policy') from error


def signal_edgar(original_edgar, summary, *, projected_edgar=None):
    """Remove material envelopes only after rebinding the original row manifest."""
    try:
        graph = original_edgar['filing_evidence']
        scope = build_material_scope(graph, {key: original_edgar.get(key, []) for key in COLLECTIONS})
        if _digest(graph['material_scope']) != _digest(scope) or _digest(summary) != _digest(_summary(scope)):
            _fail()
        projected = original_edgar if projected_edgar is None else projected_edgar
        quarantines = set(scope['quarantined_accessions'])
        # Accept only unchanged subsets from the previous verified-target projection.
        result = {key: deepcopy(original_edgar[key]) for key in
                  ('company_tickers', 'form4', 'coverage', 'error') if key in original_edgar}
        for category in COLLECTIONS:
            remaining = Counter(_digest(row) for row in original_edgar.get(category, []))
            selected = []
            for row in projected.get(category, []):
                digest = _digest(row)
                if remaining[digest] <= 0:
                    _fail()
                remaining[digest] -= 1
                if row.get('filing_evidence_ref') not in quarantines:
                    if row.get('prior_evidence_ref') in quarantines:
                        _fail()
                    selected.append(deepcopy(row))
            result[category] = selected
        # Rebuild known graph fields, so no nested collections/scope/corpus alias
        # can reintroduce a disabled original. Comparator metadata remains proof.
        result['filing_evidence'] = {key: deepcopy(graph[key]) for key in
            ('policy', 'history_corpus', 'archive_corpus') if key in graph}
        if 'comparator_policy' in graph['coverage']:
            result['filing_evidence']['coverage'] = {'comparator_policy': graph['coverage']['comparator_policy']}
        result['filing_evidence']['corpus'] = {key: deepcopy(value) for key, value in graph['corpus'].items()
                                               if key not in quarantines}
        return result
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError('invalid_filing_material_policy') from error


def validate_filing_material_health(records, policy_ids, strategy_names, expected_scope):
    if not policy_ids:
        _fail()
    by_identity = {}
    for record in records:
        value = record if isinstance(record, dict) else vars(record)
        key = (value['policy_id'], value['strategy'])
        if key in by_identity:
            _fail()
        by_identity[key] = value
    for policy_id in policy_ids:
        for strategy in strategy_names:
            record = by_identity.get((policy_id, strategy))
            evidence = record.get('evidence') if isinstance(record, dict) else None
            if (not isinstance(evidence, dict) or _digest(evidence.get('filing_material_scope')) != _digest(expected_scope)
                    or (expected_scope is None and 'filing_material_scope' in evidence)):
                _fail()
