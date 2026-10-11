"""Bind model inputs to once-frozen SEC corpora and verified security evidence."""
from __future__ import annotations

import hashlib
import json

from tradingagents.strategies.data_sources.equity_universe import EquityUniverse
from tradingagents.strategies.data_sources.filing_assessment import evidence_digest, unit_id
from tradingagents.strategies.data_sources.filing_hydration import POLICY, _nearest
from tradingagents.strategies.data_sources.filing_comparison_policy import validate_current_only_binding
from tradingagents.strategies.data_sources.filing_news import prepare_filing_news as _news


def _fail(kind='source'):
    raise ValueError('invalid_filing_' + kind)


def _issuer(evidence):
    role = 'SUBJECT-COMPANY' if evidence['form'].startswith('SCHEDULE 13') else 'FILER'
    ciks = {item['cik'] for item in evidence['roles'] if item['role'] == role}
    if len(ciks) != 1:
        _fail('issuer')
    return next(iter(ciks))


def _comparison(graph, binding, current, prior):
    """Recheck the actual retained observations behind the source's prior proof."""
    try:
        ciks, archive_refs = binding['history_refs'], binding['archive_refs']
        if ciks != [_issuer(current)] or _issuer(prior) != ciks[0]:
            _fail('comparator')
        proof = {'recent': {cik: graph['history_corpus'][cik] for cik in ciks},
                 'archives': {ref: graph['archive_corpus'][ref] for ref in archive_refs}}
        encoded = json.dumps(proof, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
        if hashlib.sha256(encoded.encode()).hexdigest() != binding['history_snapshot_sha256']:
            _fail('comparator')
        for cik, recent in proof['recent'].items():
            if recent.get('coverage', {}).get('complete') is not True or recent['cik'] != cik:
                _fail('comparator')
            rows = list(recent['filings'])
            candidate, _ = _nearest(rows, current['form'], current['filing_date'])
            floor = candidate['filing_date'] if candidate else '0001-01-01'
            for descriptor in recent['archives']:
                if descriptor['filingFrom'] < current['filing_date'] and descriptor['filingTo'] >= floor:
                    archive = proof['archives'][cik + '/' + descriptor['name']]
                    if archive.get('coverage', {}).get('complete') is not True or archive['descriptor'] != descriptor:
                        _fail('comparator')
            for ref, archive in proof['archives'].items():
                if ref.startswith(cik + '/'):
                    if archive.get('coverage', {}).get('complete') is not True or archive['cik'] != cik:
                        _fail('comparator')
                    rows.extend(archive['filings'])
            selected, status = _nearest(rows, current['form'], current['filing_date'])
            if (status != 'selected' or selected['accession_number'] != prior['accession']
                    or selected['filing_date'] != prior['filing_date'] or selected['form'] != prior['form']):
                _fail('comparator')
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError('invalid_filing_comparator') from error


def filing_analysis_inputs(candidate, shared_data, universe):
    """Resolve compact candidate refs; never manufacture text, dates, or targets.

    An unresolved security can still have an attributable source document. Only
    a real universe/CIK binding creates a target attestation for the analyzer.
    The caller must retain unresolved execution as a readiness failure.
    """
    try:
        metadata = candidate.metadata
        edgar = shared_data['edgar']
        graph = edgar['filing_evidence']
        if graph['policy'] != POLICY or metadata['full_filing_evidence_policy'] != POLICY:
            _fail()
        corpus = graph['corpus']
        pqc = metadata['analysis_type'] == 'quantum_readiness'
        prior, comparison = None, None
        if pqc:
            rows = edgar.get('pqc_filings', [])
            refs = sorted({row.get('filing_evidence_ref') or row.get('accession_number') or row.get('adsh') for row in rows})
            if refs != metadata['filing_evidence_refs'] or any(not isinstance(ref, str) for ref in refs):
                _fail()
            current = [corpus[ref] for ref in refs]
            news = _news(shared_data.get('finnhub', {}).get('pqc_news', []), metadata['news_evidence_refs'])
        else:
            ref = metadata['filing_evidence_ref']
            if metadata.get('accession_number') != ref:
                _fail()
            rows = [row for key in ('filings', 'activist_13d', 'passive_13g') for row in edgar.get(key, [])
                    if row.get('filing_evidence_ref') == ref]
            if not rows:
                _fail()
            current, news = [corpus[ref]], []
            if metadata['analysis_type'] == 'filing_change':
                comparison = metadata['comparison_binding']
                prior_ref = metadata['prior_evidence_ref']
                if not any(row.get('comparison_binding') == comparison and row.get('prior_evidence_ref') == prior_ref
                           and row.get('requires_prior') is True for row in rows):
                    _fail('comparator')
                prior = corpus[prior_ref]
                _comparison(graph, comparison, current[0], prior)
            elif metadata['analysis_type'] == 'filing_current_only':
                comparison = metadata['comparison_binding']
                if metadata.get('prior_evidence_ref') is not None or not any(
                        row.get('comparison_binding') == comparison
                        and row.get('filing_assessment_scope') == 'current_only'
                        and row.get('prior_evidence_ref') is None
                        and row.get('requires_prior') is True for row in rows):
                    _fail('comparator')
                validate_current_only_binding(graph, comparison, current[0])
        for item in current:
            if item['accession'] not in corpus or corpus[item['accession']] != item:
                _fail()
        all_corpora = current + ([prior] if prior else [])
        if any('material_quarantine' in item for item in all_corpora):
            _fail('material_quarantine')
        issuer_map = {item['accession']: _issuer(item) for item in all_corpora}
        issuer_binding = {'status': 'verified', 'issuers': issuer_map,
                          'corpus_sha256': {item['accession']: evidence_digest(item) for item in all_corpora}}
        target = None
        ticker = candidate.ticker
        if ticker:
            if not isinstance(universe, EquityUniverse) or universe.decision(ticker) != 'eligible':
                _fail('target')
            membership = None
            if not pqc:
                membership = universe.filing_decision([_issuer(current[0])])
                eligible = sorted(symbol for symbol, decision in membership['symbols'].items() if decision == 'eligible')
                if eligible != [ticker] or any(decision not in {'eligible', 'inactive_asset',
                        'absent_from_asset_master', 'outside_sip_exchange_universe'}
                        for decision in membership['symbols'].values()):
                    _fail('target')
            target = {'status': 'verified', 'ticker': ticker, 'binding_sha256': evidence_digest({
                'ticker': ticker, 'universe': {key: universe.evidence[key] for key in
                    ('policy', 'assets_sha256', 'response_sha256', 'observed_at')}, 'issuer_membership': membership,
                'source_issuers': issuer_map, 'purpose': 'pqc_target' if pqc else 'filing_issuer'})}
        required = []
        for row in rows:
            item = corpus[row['filing_evidence_ref']]
            for reference in row.get('required_exhibits', ()):
                units = [unit for unit in item['units'] if reference in (unit['type'], unit['filename'])]
                if len(units) != 1:
                    _fail('material_dependency')
                declaration = {'accession': item['accession'], 'reference': reference,
                               'resolved_unit_id': unit_id(item, units[0])}
                if declaration not in required:
                    required.append(declaration)
        return {'current_evidence': current if pqc else current[0], 'prior_evidence': prior,
                'issuer_binding': issuer_binding, 'target_binding': target, 'news_evidence': news,
                'comparison_binding': comparison, 'required_material_dependencies': required}
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise ValueError('invalid_filing_source') from error
