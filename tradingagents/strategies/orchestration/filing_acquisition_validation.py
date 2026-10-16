"""Bind bounded original acquisition to frozen selected filing evidence."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import re

POLICY = 'bounded_original_submission_v1'
MAX_SUBMISSION_BYTES = 512 * 1024**2
MAX_DOCUMENT_BYTES = 16 * 1024**2
PHYSICAL_LIMIT_BYTES = 4 * 1024**3


def configured(config):
    policy = config.get('filing_acquisition_policy')
    if policy is None:
        return False
    if (policy != POLICY or config.get('filing_evidence_policy') != 'complete_submission_v1'
            or config.get('filing_parser_policy') != 'two_processes_v1'):
        raise ValueError('invalid_filing_acquisition_policy')
    return True


def acquisition_scope(owner):
    """Freeze metadata after closing the shared owner, including rejected parses."""
    return {'policy': POLICY, 'max_submission_bytes': MAX_SUBMISSION_BYTES,
            'max_document_bytes': MAX_DOCUMENT_BYTES, 'physical_limit_bytes': PHYSICAL_LIMIT_BYTES,
            'original_deadline': owner.original_deadline,
            'originals': sorted(owner.completed_metadata(),
                                key=lambda row: (row['identity']['accession'], row['sha256'])),
            'inventory_reconciliations':owner.reconciliation_manifest(),
            'spool': owner.stats()}



def _ownership_dependency_failures(graph, collections, *, prospective, failed):
    """Only exact linked dependency diagnostics can explain unmatched originals.

    Material validation has already recomputed each retained lineage receipt.
    This check independently derives links; a supplied required_by list cannot
    grant itself authority to hide another completed original.
    """
    if not prospective:
        if 'ownership_acquisition_failures' in graph:
            raise ValueError('undeclared ownership acquisition failures')
        return set()
    entries = graph.get('ownership_acquisition_failures', {})
    if not isinstance(entries, dict) or len(entries) > (128 + 256) * max(1, len(graph.get('ownership_lineages', {}))):
        raise ValueError('invalid ownership acquisition failures')
    currents = {row.get('adsh') or row.get('accession_number') for name in
        ('filings', 'activist_13d', 'passive_13g', 'pqc_filings') for row in collections.get(name, [])}
    valid, checked_lineages = set(), set()
    for target, entry in entries.items():
        if (not isinstance(target, str) or not re.fullmatch(r'[0-9]{10}-[0-9]{2}-[0-9]{6}', target)
                or target in currents or target in graph['corpus']
                or not isinstance(entry, dict) or set(entry) != {'failure', 'required_by'}):
            raise ValueError('invalid ownership dependency target')
        failure = entry['failure']
        if (not isinstance(failure, dict) or set(failure) not in
                ({'code', 'reason_code'}, {'code', 'reason_code', 'http_status'})
                or failure['code'] not in {'source_failure', 'deadline_exhausted', 'evidence_byte_limit',
                    'mismatched_submission_evidence', 'invalid_evidence_serialization',
                    'conflicting_ownership_target_metadata'}
                or failure['reason_code'] not in {'timeout', 'transport_error', 'http_error',
                    'invalid_response', 'provider_error', 'batch_failure'}
                or ('http_status' in failure and (type(failure['http_status']) is not int
                    or not 100 <= failure['http_status'] <= 599))):
            raise ValueError('invalid ownership dependency failure')
        expected = []
        for current, receipt in sorted(graph.get('ownership_lineages', {}).items()):
            if current not in currents or current not in graph['corpus'] or receipt['current_accession'] != current:
                raise ValueError('unbound ownership current')
            duties = [duty for duty in receipt['obligations'] if duty.get('accession') == target]
            candidate = target in receipt['candidate_accessions']
            if candidate or duties:
                if current not in checked_lineages:
                    from .filing_supported_scope import ownership_bundle
                    from tradingagents.strategies.data_sources.filing_ownership_lineage import validate_ownership_lineage
                    bundle = ownership_bundle(graph, current)
                    validate_ownership_lineage(bundle['receipt'], graph['corpus'][current],
                        **{key: value for key, value in bundle.items() if key != 'receipt'})
                    checked_lineages.add(current)
                expected.append({'current_accession': current, 'candidate': candidate, 'obligations': duties})
        if (not expected or entry['required_by'] != expected
                or any(type(link.get('candidate')) is not bool for link in entry['required_by'])
                or not failed or graph['coverage']['complete'] is not False):
            raise ValueError('unbound ownership dependency failure')
        valid.add(target)
    return valid


def _supported_original_bindings(graph, originals, *, prospective):
    if not prospective:
        if 'supported_originals' in graph or 'supported_source_proofs' in graph:
            raise ValueError('undeclared supported original receipts')
        return
    expected = {accession for accession, evidence in graph['corpus'].items()
                if 'material_quarantine' not in evidence}
    receipts, proofs = graph.get('supported_originals'), graph.get('supported_source_proofs')
    if (not isinstance(receipts, dict) or not isinstance(proofs, dict)
            or set(receipts) != expected or set(proofs) != expected):
        raise ValueError('missing or orphan supported original')
    from tradingagents.strategies.data_sources.filing_supported_policy import _checked_local_proof
    for accession in sorted(expected):
        if receipts[accession] != originals[accession]:
            raise ValueError('supported original differs from acquisition owner')
        _checked_local_proof(proofs[accession], graph['corpus'][accession], originals[accession])

def validate_filing_acquisition_policy(data, config):
    try:
        enabled = configured(config)
        edgar = data.get('edgar', {})
        graph = edgar.get('filing_evidence')
        declared = graph.get('coverage', {}).get('acquisition_policy') if isinstance(graph, dict) else None
        if not enabled:
            if declared is not None or (isinstance(graph, dict) and 'acquisition_scope' in graph):
                raise ValueError('undeclared acquisition policy')
            return None
        failed = isinstance(edgar.get('error'), str) and bool(edgar['error'].strip())
        if graph is None and failed:
            return None
        if (not isinstance(graph, dict) or declared != POLICY or graph.get('policy') != 'complete_submission_v1'
                or graph['coverage'].get('parser_policy') != 'two_processes_v1'):
            raise ValueError('missing acquisition graph')
        from .filing_material_validation import validate_filing_material_policy
        material_scope = validate_filing_material_policy(data, config)
        material_complete = isinstance(material_scope, dict) and material_scope['scoped_complete'] is True
        scope = graph['acquisition_scope']
        if (set(scope) != {'policy', 'max_submission_bytes', 'max_document_bytes', 'physical_limit_bytes', 'original_deadline', 'originals', 'spool','inventory_reconciliations'}
                or scope['policy'] != POLICY):
            raise ValueError('invalid acquisition scope')
        if (type(scope['original_deadline']) not in (int, float) or not math.isfinite(scope['original_deadline'])
                or type(graph['coverage'].get('complete')) is not bool
                or (graph['coverage']['complete'] is False and not failed and not material_complete)):
            raise ValueError('unfinished acquisition lacks original failure')
        for key, value in (('max_submission_bytes', MAX_SUBMISSION_BYTES),
                           ('max_document_bytes', MAX_DOCUMENT_BYTES), ('physical_limit_bytes', PHYSICAL_LIMIT_BYTES)):
            if type(scope[key]) is not int or scope[key] != value:
                raise ValueError('invalid acquisition resource limit')
        counts = {'retained_bytes', 'parent_bytes', 'child_bytes', 'transient_bytes', 'total_bytes',
                  'completed_objects', 'failed_objects', 'active_child_copies', 'active_parent_writers',
                  'active_parent_readers', 'cleanup_failures'}
        peaks = {'retained_bytes', 'parent_bytes', 'child_bytes', 'transient_bytes', 'total_bytes', 'active_child_copies'}
        stats = scope['spool']
        if set(stats) != counts | {'peak_' + key for key in peaks} | {'closing', 'closed'}:
            raise ValueError('invalid spool counters')
        for key in counts | {'peak_' + key for key in peaks}:
            if type(stats[key]) is not int or stats[key] < 0:
                raise ValueError('invalid spool counter')
            if key.endswith('_bytes') and stats[key] > PHYSICAL_LIMIT_BYTES:
                raise ValueError('spool overflow')
        if type(stats['closing']) is not bool or type(stats['closed']) is not bool or not stats['closing']:
            raise ValueError('spool never closed')
        if (stats['transient_bytes'] != stats['parent_bytes'] + stats['child_bytes']
                or stats['total_bytes'] != stats['retained_bytes'] + stats['transient_bytes']
                or any(stats[key] > stats['peak_' + key] for key in peaks)):
            raise ValueError('inconsistent spool counters')
        if stats['active_child_copies'] > 2 or stats['peak_active_child_copies'] > 2:
            raise ValueError('too many child copies')
        cleanup_bad = (not stats['closed'] or any(stats[key] for key in
                       ('active_child_copies', 'active_parent_writers', 'active_parent_readers',
                        'parent_bytes', 'child_bytes', 'transient_bytes', 'cleanup_failures')))
        if cleanup_bad and (not failed or graph['coverage'].get('complete') is not False):
            raise ValueError('unfinished original acquisition')
        if not isinstance(scope['originals'], list) or stats['completed_objects'] != len(scope['originals']):
            raise ValueError('missing completed original')
        from tradingagents.strategies.data_sources.filing_acquisition import complete_submission_url
        from tradingagents.strategies.data_sources.edgar_source import normalize_filing_form
        originals = {}
        for original in scope['originals']:
            if (not isinstance(original, dict) or set(original) != {'identity', 'observed_at', 'size', 'sha256', 'complete'}
                    or original['complete'] is not True or type(original['size']) is not int
                    or not 0 < original['size'] <= MAX_SUBMISSION_BYTES
                    or not isinstance(original['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', original['sha256'])
                    or not isinstance(original['observed_at'], str)
                    or datetime.fromisoformat(original['observed_at']).utcoffset() is None):
                raise ValueError('invalid completed original')
            identity = original['identity']
            if (not isinstance(identity, dict) or set(identity) != {'accession', 'form', 'filing_date', 'source_url'}
                    or any(not isinstance(value, str) or not value for value in identity.values())):
                raise ValueError('invalid original identity')
            if complete_submission_url(identity['source_url'], identity['accession']) != identity['source_url']:
                raise ValueError('unbound original source')
            if identity['accession'] in originals:
                raise ValueError('duplicate completed original')
            originals[identity['accession']] = original
        if not isinstance(graph['corpus'], dict):
            raise ValueError('invalid filing corpus')
        collections = graph.get('collections', edgar)
        failed_accessions = set()
        for name in ('filings', 'activist_13d', 'passive_13g', 'pqc_filings'):
            for row in collections.get(name, []):
                if isinstance(row.get('filing_failure'), dict) and row.get('filing_evidence_status') == 'unavailable':
                    failed_accessions.add(row.get('adsh') or row.get('accession_number'))
                prior = row.get('prior_comparison', {})
                if row.get('prior_status') == 'prior_evidence_unavailable':
                    failed_accessions.add(prior.get('accession'))
        from tradingagents.strategies.data_sources.filing_material_policy import SUPPORTED_DOCUMENT_POLICIES
        prospective = config.get('filing_material_policy') in SUPPORTED_DOCUMENT_POLICIES
        failed_accessions.update(_ownership_dependency_failures(graph, collections,
            prospective=prospective, failed=failed))
        _supported_original_bindings(graph, originals, prospective=prospective)
        unmatched = set(originals) - set(graph['corpus'])
        if unmatched and (not failed or graph['coverage']['complete'] is not False or not unmatched <= failed_accessions):
            raise ValueError('completed original has no evidence or recorded parse failure')
        from tradingagents.strategies.data_sources.filing_inventory_reconciliation import validate_reconciled_evidence
        reconciliations=scope['inventory_reconciliations']
        if not isinstance(reconciliations,dict) or not set(reconciliations)<=set(originals):
            raise ValueError('invalid reconciliation manifest')
        for accession,entry in reconciliations.items():
            original=originals[accession]
            if (not isinstance(entry,dict) or set(entry)!={'submission_sha256','submission_size','proof_sha256','proof','framing'}
                    or entry['submission_sha256']!=original['sha256'] or type(entry['submission_size']) is not int
                    or entry['submission_size']!=original['size'] or not isinstance(entry['framing'],dict)
                    or set(entry['framing'])!={'header_sha256','roles','document_inventory'}):
                raise ValueError('invalid reconciliation attestation')
            digest=hashlib.sha256(json.dumps(entry['proof'],sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')).hexdigest()
            if entry['proof_sha256']!=digest:raise ValueError('reconciliation digest mismatch')
            minimal={**original['identity'],'observed_at':original['observed_at'],'submission_sha256':original['sha256'],
                **entry['framing'],'inventory_reconciliation':entry['proof']}
            validate_reconciled_evidence(minimal,original={k:original[k] for k in ('identity','sha256','size','observed_at')})
            if accession not in graph['corpus'] and (not failed or graph['coverage']['complete'] is not False or accession not in failed_accessions):
                raise ValueError('reconciled acquisition has no corpus or failure')
        for accession, evidence in graph['corpus'].items():
            original = originals[accession]
            identity = original['identity']
            inventory = evidence['document_inventory']
            if (not isinstance(inventory, list) or not inventory
                    or any(type(doc.get('body_start')) is not int or type(doc.get('body_end')) is not int
                           or not 0 <= doc['body_start'] <= doc['body_end'] <= original['size'] for doc in inventory)):
                raise ValueError('original size does not contain document inventory')
            if (evidence['accession'] != accession or normalize_filing_form(evidence['form']) != normalize_filing_form(identity['form'])
                    or evidence['filing_date'] != identity['filing_date']
                    or evidence['observed_at'] != original['observed_at']
                    or evidence['submission_sha256'] != original['sha256']
                    or evidence['source_url'] != identity['source_url']
                    or ('material_quarantine' in evidence and
                        evidence['material_quarantine']['submission_size'] != original['size'])):
                raise ValueError('selected evidence differs from original')
            if ('inventory_reconciliation' in evidence)!=(accession in reconciliations):
                raise ValueError('missing or unsolicited reconciliation')
            if accession in reconciliations:
                entry=reconciliations[accession]
                if (evidence['inventory_reconciliation']!=entry['proof']
                        or any(evidence[key]!=entry['framing'][key] for key in ('header_sha256','roles','document_inventory'))):
                    raise ValueError('corpus differs from acquisition reconciliation')
                validate_reconciled_evidence(evidence,original={k:original[k] for k in ('identity','sha256','size','observed_at')})
        return {'policy': POLICY, 'completed_originals': len(originals),
                'reconciled_originals':len(reconciliations),
                'oversized_originals': sum(row['size'] > 64 * 1024**2 for row in originals.values()),
                'completed_original_bytes': sum(row['size'] for row in originals.values()),
                'spool_closed': stats['closed'], 'peak_spool_bytes': stats['peak_total_bytes']}
    except Exception as error:
        raise ValueError('invalid_filing_acquisition_policy') from error
