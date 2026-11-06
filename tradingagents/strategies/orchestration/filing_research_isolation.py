"""Prospective, pure filing dependency isolation with no trading authority.

The original envelope remains the strict coverage and raw-capture obligation.
This projection grants access only to independently verified, closed inputs. A
captured rejected original is never treated as a successful parse, nor is its
recorded parser failure independently certified by this module.
"""
from __future__ import annotations

from copy import deepcopy

from .research_resilience import POLICY, configured
from .filing_material_validation import COLLECTIONS, _digest, _required, validate_filing_material_policy
from .filing_acquisition_validation import validate_filing_acquisition_policy

VERSION = 'filing-research-isolation-v1'


def _fail():
    raise ValueError('invalid_filing_research_isolation')


def _identity_checks(edgar, graph):
    """An occurrence cannot contradict an accepted or captured source identity."""
    from tradingagents.strategies.data_sources.filing_acquisition import complete_submission_url
    from tradingagents.strategies.data_sources.edgar_source import normalize_filing_form
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    originals = {r['identity']['accession']: r for r in graph['acquisition_scope']['originals']}
    identities = {}
    for category in COLLECTIONS:
        rows = edgar.get(category, [])
        if 'collections' in graph and graph['collections'].get(category, []) != rows:
            _fail()
        for row in rows:
            accession = row.get('adsh') or row.get('accession_number')
            if not isinstance(accession, str) or not accession:
                _fail()
            if row.get('adsh') is not None and row.get('accession_number') is not None and row['adsh'] != row['accession_number']:
                _fail()
            if row.get('filing_evidence_ref') not in (None, accession):
                _fail()
            try:
                identity = (normalize_filing_form(row['form_type']), row['file_date'],
                            complete_submission_url(row['file_url'], accession))
            except SourceFetchError:
                _fail()
            if accession in identities and identities[accession] != identity:
                _fail()
            identities[accession] = identity
            if accession in originals:
                original = originals[accession]['identity']
                if identity != (normalize_filing_form(original['form']), original['filing_date'], original['source_url']):
                    _fail()
    return originals


def _source_reasons(graph, ref):
    evidence = graph['corpus'].get(ref)
    if evidence is None:
        return ['missing_verified_evidence']
    if 'material_quarantine' in evidence:
        return ['material_quarantine']
    proof = graph['supported_source_proofs'][ref]
    reasons = []
    if evidence.get('structural_status') != 'complete':
        reasons.append('incomplete_selected_evidence')
    if proof['residual_failures']:
        reasons.append('source_proof_failure')
    if proof['reasons']:
        reasons.append('unverified_material_capability')
    disposition = graph['supported_dispositions'].get(ref)
    if disposition is not None and disposition['status'] != 'admitted':
        reasons.append('dependent_record_not_admitted')
    return reasons


def _edges(graph, edgar):
    edges = set()
    for category in COLLECTIONS:
        for row in edgar.get(category, []):
            current = row.get('filing_evidence_ref') or row.get('adsh') or row.get('accession_number')
            prior = row.get('prior_evidence_ref') or (row.get('prior_comparison') or {}).get('accession')
            if prior:
                edges.add((current, prior, 'prior_comparison'))
    for current, lineage in graph.get('ownership_lineages', {}).items():
        # Candidate bodies also prove negative membership in the chain; retain
        # those inputs rather than only the favourable selected predecessors.
        refs = set(lineage['source_accessions']) | set(lineage['candidate_accessions'])
        refs.update(lineage['incorporated_accessions'])
        refs.update(duty['accession'] for duty in lineage['obligations'] if duty.get('accession'))
        for ref in refs - {current}:
            edges.add((current, ref, 'ownership_source'))
    return [{'from': current, 'to': target, 'kind': kind} for current, target, kind in sorted(edges)]


def _closure(refs, edges):
    result = set(refs)
    while True:
        additions = {edge['to'] for edge in edges if edge['from'] in result}
        if additions <= result:
            return result
        result.update(additions)


def _projection(edgar, graph, occurrences, included, *, provider_hold):
    result = {key: deepcopy(edgar[key]) for key in ('company_tickers', 'form4', 'coverage', 'error') if key in edgar}
    if edgar.get('error') or provider_hold:
        result.pop('form4', None)
    for category in COLLECTIONS:
        result[category] = [deepcopy(edgar[category][item['index']]) for item in occurrences
                            if item['collection'] == category and item['outcome'] == 'included']
    projected = {'policy': graph['policy'], 'corpus': {ref: deepcopy(graph['corpus'][ref]) for ref in sorted(included)}}
    # Keep only snapshots used by admitted comparator/lineage proofs. Snapshot
    # metadata must remain complete (including negative candidate membership),
    # but unrelated failed issuers cannot become free-form context aliases.
    history_refs, archive_refs = set(), set()
    for category in COLLECTIONS:
        for row in result[category]:
            binding = row.get('comparison_binding') or {}
            history_refs.update(binding.get('history_refs', []))
            archive_refs.update(binding.get('archive_refs', []))
            lineage = graph.get('ownership_lineages', {}).get(row['filing_evidence_ref'])
            if lineage and lineage['candidate_accessions'] != [lineage['current_accession']]:
                cik = lineage['subject_cik']
                history_refs.add(cik)
                archive_refs.update(ref for ref in graph.get('archive_corpus', {}) if ref.startswith(cik + '/'))
    for key, refs in (('history_corpus', history_refs), ('archive_corpus', archive_refs)):
        if key in graph:
            projected[key] = {ref: deepcopy(value) for ref, value in graph[key].items() if ref in refs}
    if 'comparator_policy' in graph['coverage']:
        projected['coverage'] = {'comparator_policy': graph['coverage']['comparator_policy']}
    for key in ('ownership_lineages', 'ownership_facts'):
        if key in graph:
            projected[key] = {ref: deepcopy(value) for ref, value in graph[key].items() if ref in included}
    result['filing_evidence'] = projected
    return result


def _compute(edgar, config, material, acquisition):
    graph = edgar.get('filing_evidence')
    if graph is None:
        if material is not None or acquisition is not None or not edgar.get('error'):
            _fail()
        occurrences, refs = [], set()
        for category in COLLECTIONS:
            rows = edgar.get(category, [])
            if not isinstance(rows, list):
                _fail()
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    _fail()
                reported = row.get('adsh') or row.get('accession_number')
                ref = reported if isinstance(reported, str) and reported else None
                if ref is not None:
                    refs.add(ref)
                # These are reported discovery identities, not parsed source
                # identities. No missing graph can invent an acquired original
                # or successful proof; capture must audit physical acquisitions
                # independently of this non-authoritative hold receipt.
                occurrences.append({'collection': category, 'index': index, 'accession': ref,
                    'row_sha256': _digest(row), 'evidence_sha256': None, 'original_sha256': None,
                    'dependencies': [], 'outcome': 'excluded', 'reasons': ['missing_complete_discovery'],
                    'recorded_failure': deepcopy(row.get('filing_failure')),
                    'prior_outcome': deepcopy(row.get('prior_comparison')),
                    'parser_failure_independently_certified': False})
        projected = {category: [] for category in COLLECTIONS}
        projected.update({key: deepcopy(edgar[key]) for key in ('coverage', 'error') if key in edgar})
        source_outcomes = [{'accession': ref, 'original': None, 'evidence_sha256': None,
            'outcome': 'excluded', 'parser_failure_independently_certified': False,
            'recorded_dependency_failure': None} for ref in sorted(refs)]
        pqc = sorted({item['accession'] for item in occurrences
                      if item['collection'] == 'pqc_filings' and item['accession'] is not None})
        receipt = {'version': VERSION, 'policy': POLICY, 'original_edgar_sha256': _digest(edgar),
            'strict_material_scope_sha256': _digest(None), 'acquisition_scope_sha256': _digest(None),
            'original_acquisition_manifest_sha256': _digest(None), 'occurrences': occurrences, 'dependency_edges': [],
            'included_refs': [], 'excluded_refs': sorted(refs), 'source_outcomes': source_outcomes,
            'aggregate_scopes': {'pqc_filings': {'outcome': 'held', 'original_refs': pqc}},
            'discovery_scope': {'outcome': 'provider_hold', 'reasons': ['missing_complete_discovery']},
            'original_full_coverage': False, 'order_authority': False,
            'projection_sha256': _digest(projected)}
        return projected, {**receipt, 'manifest_sha256': _digest(receipt)}
    if material is None or acquisition is None:
        _fail()
    originals = _identity_checks(edgar, graph)
    coverage = graph['coverage']
    spool = graph['acquisition_scope']['spool']
    global_reasons = []
    if coverage['discovery_complete'] is not True:
        global_reasons.append('incomplete_discovery')
    discovery = graph.get('discovery_coverage', {})
    if any(not isinstance(discovery.get(category), dict) or discovery[category].get('complete') is not True
           for category in COLLECTIONS):
        global_reasons.append('missing_complete_discovery_declaration')
    if coverage['deadline_exhausted']:
        global_reasons.append('filing_deadline_exhausted')
    if coverage.get('scope_failure'):
        global_reasons.append('unclosed_filing_scope')
    if coverage.get('rejected_evidence_objects') or coverage.get('rejected_evidence_bytes'):
        global_reasons.append('incomplete_retained_scope')
    if not spool['closed'] or any(spool[key] for key in ('active_child_copies', 'active_parent_writers',
            'active_parent_readers', 'parent_bytes', 'child_bytes', 'transient_bytes', 'cleanup_failures', 'failed_objects')):
        global_reasons.append('unclosed_original_acquisition')
    edges = _edges(graph, edgar)
    occurrences = []
    for category in COLLECTIONS:
        for index, row in enumerate(edgar.get(category, [])):
            ref = row.get('filing_evidence_ref') or row.get('adsh') or row.get('accession_number')
            required, needs_prior = _required(category, row)
            reasons = list(global_reasons)
            if not required:
                reasons.append('outside_required_filing_scope')
            reasons.extend(_source_reasons(graph, ref))
            if row.get('filing_failure'):
                reasons.append('recorded_filing_failure')
            if row.get('discovery_identity_conflict'):
                reasons.append('discovery_identity_conflict')
            if row.get('filing_evidence_status') != 'complete' or row.get('text_status') != 'available':
                reasons.append('unavailable_analysis_input')
            if row.get('issuer_binding', {}).get('status') != 'verified':
                reasons.append('unverified_issuer_binding')
            if row.get('filing_material_disposition') != 'admitted':
                reasons.append('source_not_admitted')
            if needs_prior and row.get('filing_assessment_scope') != 'current_only' and row.get('prior_status') != 'available':
                reasons.append('unclosed_prior_comparison')
            dependencies = _closure({ref}, [edge for edge in edges
                if edge['kind'] != 'prior_comparison' or (needs_prior and edge['from'] == ref)])
            if any(_source_reasons(graph, dependency) for dependency in dependencies):
                reasons.append('unclosed_source_dependency')
            lineage = graph.get('ownership_lineages', {}).get(ref)
            if lineage is not None and (lineage['status'] != 'complete' or lineage['obligations']):
                reasons.append('unclosed_ownership_lineage')
            original = originals.get(ref)
            occurrences.append({'collection': category, 'index': index, 'accession': ref,
                'row_sha256': _digest(row), 'evidence_sha256': _digest(graph['corpus'][ref]) if ref in graph['corpus'] else None,
                'original_sha256': original['sha256'] if original else None,
                'dependencies': sorted(dependencies), 'outcome': 'excluded' if reasons else 'included',
                'reasons': sorted(set(reasons)), 'recorded_failure': deepcopy(row.get('filing_failure')),
                'prior_outcome': deepcopy(row.get('prior_comparison')),
                'parser_failure_independently_certified': False})
    pqc = [item for item in occurrences if item['collection'] == 'pqc_filings']
    pqc_hold = any(item['outcome'] != 'included' for item in pqc)
    if pqc_hold:
        for item in pqc:
            item['outcome'] = 'excluded'
            item['reasons'] = sorted(set(item['reasons']) | {'aggregate_dependency_gap'})
    included = {ref for item in occurrences if item['outcome'] == 'included' for ref in item['dependencies']}
    all_refs = set(originals) | set(graph['corpus']) | {item['accession'] for item in occurrences}
    all_refs.update(edge['to'] for edge in edges)
    projected = _projection(edgar, graph, occurrences, included, provider_hold=bool(global_reasons))
    source_outcomes = [{'accession': ref, 'original': deepcopy(originals.get(ref)),
        'evidence_sha256': _digest(graph['corpus'][ref]) if ref in graph['corpus'] else None,
        'outcome': 'included' if ref in included else 'excluded',
        'parser_failure_independently_certified': False,
        'recorded_dependency_failure': deepcopy(graph.get('ownership_acquisition_failures', {}).get(ref))}
        for ref in sorted(all_refs)]
    receipt = {'version': VERSION, 'policy': POLICY, 'original_edgar_sha256': _digest(edgar),
        'strict_material_scope_sha256': _digest(material), 'acquisition_scope_sha256': _digest(acquisition),
        'original_acquisition_manifest_sha256': _digest(graph['acquisition_scope']),
        'occurrences': occurrences, 'dependency_edges': edges, 'included_refs': sorted(included),
        'excluded_refs': sorted(all_refs-included), 'source_outcomes': source_outcomes,
        'aggregate_scopes': {'pqc_filings': {'outcome': 'held' if pqc_hold else 'complete',
            'original_refs': sorted({item['accession'] for item in pqc})}},
        'discovery_scope': {'outcome': 'provider_hold' if global_reasons else 'complete', 'reasons': global_reasons,
            'declarations_sha256': _digest(discovery)},
        'original_full_coverage': material['strict_complete'], 'order_authority': False,
        'projection_sha256': _digest(projected)}
    return projected, {**receipt, 'manifest_sha256': _digest(receipt)}


def isolate_filing_research(original_edgar, config, *, strict_material_scope, acquisition_scope):
    """Return a new projection and an exhaustive non-authoritative receipt.

    Supplied scopes are the summaries returned by existing strict validators,
    not replacement success verdicts. Raw auditing remains a separate mandatory
    capture/replay boundary for every completed original, including exclusions.
    """
    if not configured(config):
        return deepcopy(original_edgar), None
    try:
        material = validate_filing_material_policy({'edgar': original_edgar}, config)
        acquisition = validate_filing_acquisition_policy({'edgar': original_edgar}, config)
        if _digest(material) != _digest(strict_material_scope) or _digest(acquisition) != _digest(acquisition_scope):
            _fail()
        return _compute(original_edgar, config, material, acquisition)
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError('invalid_filing_research_isolation') from error


def validate_filing_research_isolation(original_edgar, receipt, config, *, projected_edgar=None):
    """Recompute rather than trust a stored list of favourable source records."""
    try:
        material = validate_filing_material_policy({'edgar': original_edgar}, config) if configured(config) else None
        acquisition = validate_filing_acquisition_policy({'edgar': original_edgar}, config) if configured(config) else None
        projected, expected = (_compute(original_edgar, config, material, acquisition) if configured(config)
                               else (deepcopy(original_edgar), None))
        if _digest(receipt) != _digest(expected) or (projected_edgar is not None and _digest(projected_edgar) != _digest(projected)):
            _fail()
        return expected
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError('invalid_filing_research_isolation') from error
