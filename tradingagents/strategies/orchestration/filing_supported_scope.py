"""One occurrence/bundle contract shared by hydration and scope recomputation."""
from __future__ import annotations

import base64
from collections import defaultdict

from tradingagents.strategies.data_sources.filing_supported_policy import evaluate_supported_disposition

COLLECTIONS = ('filings', 'activist_13d', 'passive_13g', 'pqc_filings')


def ownership_bundle(graph, accession, *, check=None):
    """Retain the full lookup graph; the lineage verifier selects its used inputs."""
    if isinstance(accession, dict):
        accession = accession['accession']
    receipt = graph.get('ownership_lineages', {}).get(accession)
    if receipt is None:
        return None
    used = set(receipt['candidate_accessions']) | {accession}
    bodies = {}
    for key in sorted(used):
        if check is not None:
            check()
        encoded = graph['corpus'].get(key, {}).get('ownership_primary_body')
        if encoded is not None:
            bodies[key] = base64.b64decode(encoded, validate=True)
    return {'receipt': receipt, 'facts_by_accession': graph.get('ownership_facts', {}),
        'submissions': graph['corpus'], 'histories': graph.get('history_corpus', {}),
        'archives': graph.get('archive_corpus', {}), 'primary_bodies': bodies}


def build_supported_dispositions(graph, collections, *, on_receipt=None, check=None):
    """Bind every required occurrence; never subtract independent row failures.

    Missing current acquisitions remain ordinary row failures and have no source
    disposition. All current corpus objects outside the retained exact quarantine
    require a parser-bound source proof. Source admission does not validate model
    outputs, annual comparisons, discovery/spool closure or overall acquisition.
    Those remain independent checks in material-scope and execution validation.
    """
    from .filing_material_validation import _required
    from tradingagents.strategies.data_sources.filing_acquisition import complete_submission_url
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    grouped = defaultdict(list)
    for category in COLLECTIONS:
        for index, row in enumerate(collections.get(category, [])):
            if not _required(category, row)[0]:
                continue
            ref = row.get('filing_evidence_ref')
            evidence = graph['corpus'].get(ref)
            if evidence is None or 'material_quarantine' in evidence:
                continue
            grouped[ref].append(({'collection': category, 'index': index}, row))
    omissions = graph.get('supported_disposition_failures', {})
    if not isinstance(omissions, dict) or not set(omissions) <= set(grouped) or any(
            value != {'code': 'evidence_byte_limit'} for value in omissions.values()):
        raise ValueError('invalid_supported_disposition_failure')
    results = {}
    for accession, occurrences in sorted(grouped.items()):
        if check is not None:
            check()
        if accession in omissions:
            if any(not row.get('filing_failure') or row.get('filing_material_disposition') != 'failed'
                    for _, row in occurrences):
                raise ValueError('unrecorded_supported_disposition_failure')
            continue
        current = graph['corpus'][accession]
        residual = []
        local = graph.get('supported_source_proofs', {}).get(accession, {})
        for occurrence, row in occurrences:
            if row.get('filing_failure'):
                residual.append({'code': 'filing_failure', 'occurrence': occurrence, 'failure': row['filing_failure']})
            if row.get('discovery_identity_conflict') is True:
                residual.append({'code': 'discovery_identity_conflict', 'occurrence': occurrence})
            required = row.get('required_exhibits', ())
            if (not isinstance(required, (list, tuple)) or any(not isinstance(name, str)
                    or name not in local.get('required_exhibits', []) for name in required)):
                residual.append({'code': 'unclosed_required_exhibit_obligation', 'occurrence': occurrence})
            try:
                if complete_submission_url(row['file_url'], accession) != current['source_url']:
                    raise ValueError('source identity differs')
            except (KeyError, TypeError, ValueError, SourceFetchError):
                residual.append({'code': 'source_identity_mismatch', 'occurrence': occurrence})
        bundle = None
        if current['form'].startswith('SCHEDULE 13'):
            try:
                bundle = ownership_bundle(graph, accession, check=check)
            except (KeyError, TypeError, ValueError, SourceFetchError):
                residual.append({'code': 'invalid_ownership_raw_bundle'})
        receipt = evaluate_supported_disposition(current,
            source_proofs=graph.get('supported_source_proofs', {}),
            originals=graph.get('supported_originals', {}), ownership=bundle,
            residual_failures=residual, occurrences=[item[0] for item in occurrences], check=check)
        if on_receipt is None or on_receipt(accession, receipt):
            results[accession] = receipt
    return results
