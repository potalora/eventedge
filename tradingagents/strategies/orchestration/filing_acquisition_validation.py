"""Bind bounded original acquisition to frozen selected filing evidence."""
from __future__ import annotations

from datetime import datetime
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
            'spool': owner.stats()}


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
        scope = graph['acquisition_scope']
        if (set(scope) != {'policy', 'max_submission_bytes', 'max_document_bytes', 'physical_limit_bytes', 'original_deadline', 'originals', 'spool'}
                or scope['policy'] != POLICY):
            raise ValueError('invalid acquisition scope')
        if (type(scope['original_deadline']) not in (int, float) or not math.isfinite(scope['original_deadline'])
                or type(graph['coverage'].get('complete')) is not bool
                or (graph['coverage']['complete'] is False and not failed)):
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
        unmatched = set(originals) - set(graph['corpus'])
        if unmatched and (not failed or graph['coverage']['complete'] is not False or not unmatched <= failed_accessions):
            raise ValueError('completed original has no evidence or recorded parse failure')
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
                    or evidence['source_url'] != identity['source_url']):
                raise ValueError('selected evidence differs from original')
        return {'policy': POLICY, 'completed_originals': len(originals),
                'oversized_originals': sum(row['size'] > 64 * 1024**2 for row in originals.values()),
                'completed_original_bytes': sum(row['size'] for row in originals.values()),
                'spool_closed': stats['closed'], 'peak_spool_bytes': stats['peak_total_bytes']}
    except Exception as error:
        raise ValueError('invalid_filing_acquisition_policy') from error
