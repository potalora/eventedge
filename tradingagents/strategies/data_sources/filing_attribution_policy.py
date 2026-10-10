"""Prospective filing attribution, independent of acquisition/model adequacy.

Every discovery occurrence is retained. This permission never selects a share
class, collapses joint source roles, or certifies sufficient model analysis.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from .equity_universe import EquityUniverse

POLICY = 'verified_filing_targets_v1'
COLLECTIONS = ('filings', 'activist_13d', 'passive_13g', 'pqc_filings')
_COUNTS = {'verified_target': 'verified_target_rows', 'outside': 'outside_rows',
           'unresolved': 'unresolved_rows'}
_REASONS = ('verified_execution_security', 'proved_equity_universe_exclusion',
            'proved_discovery_universe_exclusion', 'joint_source_issuers',
            'unresolved_execution_security', 'required_evidence_unavailable',
            'structural_evidence_incomplete', 'not_required_form')


def _fail():
    raise ValueError('invalid_filing_attribution')


def _digest(value):
    try:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
            ensure_ascii=True, allow_nan=False).encode('utf-8')).hexdigest()
    except (ValueError, TypeError, RecursionError):
        _fail()


def configured(config):
    """Only the explicit policy paired with complete source evidence opts in."""
    try:
        policy = config.get('filing_attribution_policy')
        if policy is None:
            return False
        if policy != POLICY or config.get('filing_evidence_policy') != 'complete_submission_v1':
            _fail()
    except (AttributeError, TypeError):
        _fail()
    return True


def _structural_roles(evidence):
    """Use the full native evidence contract, preserving all joint source roles."""
    from .filing_assessment import _validate_evidence
    try:
        _validate_evidence(evidence, allow_joint_issuers=True)
    except (ValueError, KeyError, TypeError, AttributeError):
        _fail()


def attribution_binding(evidence, universe, company_symbols):
    """Only well-formed complete source roles may receive the new permission."""
    from .filing_hydration import _issuer_binding
    _structural_roles(evidence)
    return _issuer_binding(evidence, universe, company_symbols)


def build_scope(graph, collections, universe, company_map):
    """Recompute the per-occurrence manifest from the original retained graph."""
    from .filing_hydration import _issuer_binding
    from .edgar_source import normalize_filing_form
    if (not isinstance(graph, dict) or graph.get('policy') != 'complete_submission_v1'
            or not isinstance(collections, dict) or set(collections) - set(COLLECTIONS)
            or not isinstance(graph.get('corpus'), dict)):
        _fail()
    symbols = EquityUniverse.company_symbols(company_map) if company_map is not None else {}
    entries, by_collection, bodies, bindings = [], {}, {}, {}
    reasons = dict.fromkeys(_REASONS, 0)
    for category in COLLECTIONS:
        rows = collections.get(category, [])
        if not isinstance(rows, list):
            _fail()
        counts = {'total_rows': len(rows), **{key: 0 for key in _COUNTS.values()}}
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                _fail()
            ref = row.get('filing_evidence_ref')
            evidence = graph['corpus'].get(ref) if isinstance(ref, str) else None
            disposition, reason, binding = 'unresolved', 'required_evidence_unavailable', None
            if evidence is not None:
                if (ref != evidence.get('accession') or ref != (row.get('adsh') or row.get('accession_number'))
                        or evidence.get('filing_date') != row.get('file_date')
                        or evidence.get('form') != normalize_filing_form(row.get('form_type', ''))):
                    _fail()
                if ref not in bindings:
                    if evidence.get('structural_status') == 'complete':
                        bindings[ref] = attribution_binding(evidence, universe, symbols)
                    else:
                        bindings[ref] = _issuer_binding(evidence, universe, symbols)
                    bodies[ref] = _digest(evidence)
                binding = bindings[ref]
                if row.get('issuer_binding') != binding:
                    _fail()
                reason = 'structural_evidence_incomplete'
                if evidence.get('structural_status') == 'complete':
                    status = binding['execution_status']
                    if status == 'verified':
                        disposition, reason = 'verified_target', 'verified_execution_security'
                    elif status == 'outside_declared_equity_universe':
                        disposition, reason = 'outside', 'proved_equity_universe_exclusion'
                    else:
                        reason = ('joint_source_issuers' if binding['status'] != 'verified'
                                  else 'unresolved_execution_security')
            elif row.get('text_status') == 'outside_declared_equity_universe':
                expected = universe.filing_decision(row.get('ciks', [])) if universe is not None else None
                if expected is None or expected.get('status') != 'excluded' or row.get('universe_membership') != expected:
                    _fail()
                disposition, reason = 'outside', 'proved_discovery_universe_exclusion'
            elif row.get('text_status') == 'not_required_form':
                reason = 'not_required_form'
            counts[_COUNTS[disposition]] += 1
            reasons[reason] += 1
            entries.append({'collection': category, 'index': index,
                'accession': row.get('adsh') or row.get('accession_number'),
                'evidence_ref': ref, 'prior_evidence_ref': row.get('prior_evidence_ref'),
                'row_sha256': _digest(row), 'evidence_sha256': bodies.get(ref),
                'binding': deepcopy(binding), 'disposition': disposition, 'reason': reason})
        by_collection[category] = counts
    scope = {'policy': POLICY, 'total_rows': len(entries),
        **{key: sum(item[key] for item in by_collection.values()) for key in _COUNTS.values()},
        'by_collection': by_collection, 'reason_counts': reasons, 'company_map_sha256': _digest(company_map),
        'universe_sha256': _digest(universe.evidence if universe is not None else None), 'rows': entries}
    return {**scope, 'manifest_sha256': _digest(scope)}


def _summary(scope):
    return {key: deepcopy(scope[key]) for key in ('policy', 'total_rows', 'verified_target_rows',
        'outside_rows', 'unresolved_rows', 'by_collection', 'reason_counts', 'manifest_sha256')}


def validate_attribution(edgar, universe):
    """Recompute source/map/universe-bound dispositions; never trust counts."""
    try:
        graph = edgar['filing_evidence']
        if graph['coverage'].get('attribution_policy') != POLICY:
            _fail()
        expected = build_scope(graph, {key: edgar.get(key, []) for key in COLLECTIONS},
                               universe, edgar.get('company_tickers'))
        if _digest(graph.get('attribution_scope')) != _digest(expected):
            _fail()
        return _summary(expected)
    except (KeyError, TypeError, AttributeError):
        _fail()


def signal_edgar(edgar, summary):
    """Project only already validated targets, retaining the entire source graph.

    Call validate_attribution against the original data before this projection.
    The digest and every occurrence are checked again to prevent rebinding the
    projection after validation. Returned row copies isolate downstream edits.
    """
    try:
        scope = edgar['filing_evidence']['attribution_scope']
        if (_digest(_summary(scope)) != _digest(summary) or scope['policy'] != POLICY
                or _digest({k: v for k, v in scope.items() if k != 'manifest_sha256'}) != scope['manifest_sha256']):
            _fail()
        result = dict(edgar)
        entries = {(entry['collection'], entry['index']): entry for entry in scope['rows']}
        if len(entries) != len(scope['rows']):
            _fail()
        seen = set()
        for category in COLLECTIONS:
            selected = []
            for index, row in enumerate(edgar.get(category, [])):
                key = (category, index)
                entry = entries[key]
                seen.add(key)
                if entry['row_sha256'] != _digest(row):
                    _fail()
                if entry['disposition'] == 'verified_target':
                    selected.append(deepcopy(row))
            result[category] = selected
        if seen != set(entries):
            _fail()
        return result
    except (KeyError, TypeError, AttributeError):
        _fail()
