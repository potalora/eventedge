"""Opt-in retained local document intake; no discovery, rendering, or network.

Complete coverage refers only to the explicitly configured finite registry.
Selected-page evidence never certifies a complete filing or current holdings.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat

POLICY = 'bounded_document_research_v1'
DECISION_POLICY = 'verified_scoped_claims_v1'
MAX_DOCUMENTS = 4
MAX_ORIGINAL_BYTES = 16 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 4 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_ENCODED_EVIDENCE_BYTES = 12 * 1024 * 1024
_ENTRY_KEYS = {'ticker', 'document_id', 'as_of', 'original', 'original_pages',
               'selected_pages', 'attachments', 'probes', 'residual_failures'}
_FILE_KEYS = {'media_type', 'filename', 'sha256', 'path'}
_ATTACHMENT_KEYS = _FILE_KEYS | {'source_sha256', 'pages', 'render_identity'}


def configured(ar_config):
    from ..document_review_clock import POLICY as NEXT_OPEN_POLICY
    if ar_config.get('bounded_document_decision_policy') == NEXT_OPEN_POLICY:
        # Reject unsupported clock/checkpoint combinations before source/model IO.
        from .decision_clock import policy, PROSPECTIVE
        from .research_resilience import configured as research_configured
        if policy({'autoresearch':ar_config}) != PROSPECTIVE or not research_configured(ar_config):
            raise ValueError('invalid_document_decision_clock_policy')
    if ar_config.get('bounded_document_decision_policy') not in (None, DECISION_POLICY, NEXT_OPEN_POLICY):
        raise ValueError('invalid_bounded_document_decision_policy')
    value = ar_config.get('bounded_document_research_policy')
    if value is None:
        return False
    if value != POLICY:
        raise ValueError('invalid_bounded_document_research_policy')
    return True


def configure_strategies(strategies, ar_config):
    result = list(strategies)
    if configured(ar_config) and not any(item.name == 'bounded_document_research' for item in result):
        from tradingagents.strategies.modules.bounded_document_research import BoundedDocumentResearchStrategy
        result.append(BoundedDocumentResearchStrategy())
    return result


def compact_source(evidence):
    """Identity carried per candidate; exact bytes stay in daily source storage."""
    original = evidence.get('original', {})
    return {**{key: deepcopy(evidence.get(key)) for key in
               ('policy', 'document_id', 'as_of', 'selected_pages', 'residual_failures')},
            'original_sha256': original.get('sha256') if isinstance(original, dict) else None,
            'attachment_sha256s': [item.get('sha256') for item in evidence.get('attachments', [])
                                  if isinstance(item, dict)]}


def release_candidate_bytes(candidate):
    if candidate.metadata.get('analysis_type') == 'bounded_document_research':
        evidence = candidate.metadata.pop('bounded_document_evidence', None)
        if isinstance(evidence, dict):
            candidate.metadata['bounded_document_source'] = compact_source(evidence)


def _require(value):
    if not value:
        raise ValueError('bounded_document_intake_invalid')


def _local_wire(spec, limit, *, attachment=False):
    _require(type(spec) is dict and set(spec) == (_ATTACHMENT_KEYS if attachment else _FILE_KEYS))
    _require(type(spec['path']) is str and type(spec['sha256']) is str
             and re.fullmatch('[0-9a-f]{64}', spec['sha256']))
    path = Path(spec['path'])
    _require(path.is_absolute() and '..' not in path.parts
             and not any(item.is_symlink() for item in (path, *path.parents)))
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        _require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= limit)
        data = handle.read(limit + 1)
    _require(0 < len(data) <= limit and hashlib.sha256(data).hexdigest() == spec['sha256'])
    wire = {key: spec[key] for key in ('media_type', 'filename', 'sha256')}
    wire['data_base64'] = base64.b64encode(data).decode('ascii')
    if attachment:
        wire.update({key: deepcopy(spec[key]) for key in ('source_sha256', 'pages', 'render_identity')})
    return wire, len(data)


def load_retained_documents(ar_config, trading_date):
    """Return all configured reviewed originals, or an explicit empty failure.

    Registry records mirror evidence, replacing data_base64 with absolute local
    path in original/attachments. No path is copied into accepted evidence.
    """
    if not configured(ar_config):
        return {}
    registry = ar_config.get('bounded_document_registry')
    residuals = ['complete_document_unassessed']
    if type(registry) is list:
        for item in registry[:MAX_DOCUMENTS]:
            if type(item) is dict and type(item.get('residual_failures')) is list:
                for reason in item['residual_failures'][:32]:
                    if type(reason) is str and 0 < len(reason) <= 512 and reason not in residuals:
                        residuals.append(reason)
    scope = {'policy': POLICY, 'population': 'configured_retained_document_registry',
             'configured_count': len(registry) if type(registry) is list else None,
             'accepted_count': 0, 'complete': False, 'residual_failures': residuals}
    if ar_config.get('bounded_document_decision_policy') is not None:
        scope['decision_policy'] = ar_config['bounded_document_decision_policy']
        from ..document_review_clock import POLICY as NEXT_OPEN_POLICY
        if scope['decision_policy'] == NEXT_OPEN_POLICY:
            from ..bounded_document_research import REQUEST_VERSION
            scope['review_clock_config'] = {key:ar_config[key] for key in
                ('bounded_document_decision_policy','decision_clock_policy','research_resilience_policy')}
            scope['review_clock_config'].update(bounded_document_recognition_request_version=
                ar_config.get('bounded_document_recognition_request_version',REQUEST_VERSION),
                llm_temperature=ar_config.get('llm_temperature',0.0))
    failure = {'error': 'bounded_document_intake_invalid', 'reason_code': 'invalid_response',
               'bounded_documents': [], 'bounded_document_scope': scope, 'coverage': {'complete': False}}
    try:
        _require(type(registry) is list and 1 <= len(registry) <= MAX_DOCUMENTS)
        from tradingagents.strategies.bounded_document_research import validate_evidence
        records, identities = [], set()
        for item in registry:
            _require(type(item) is dict and set(item) in (_ENTRY_KEYS, _ENTRY_KEYS | {'decision_scope'}))
            _require(type(item['ticker']) is str and re.fullmatch(r'[A-Z][A-Z0-9.-]{0,15}', item['ticker']))
            _require(type(item['document_id']) is str and item['document_id'] not in identities)
            identities.add(item['document_id'])
            original, _ = _local_wire(item['original'], MAX_ORIGINAL_BYTES)
            _require(type(item['attachments']) is list and 1 <= len(item['attachments']) <= 8)
            attachments, total = [], 0
            for attachment in item['attachments']:
                encoded, size = _local_wire(attachment, MAX_ATTACHMENT_BYTES, attachment=True)
                total += size
                _require(total <= MAX_TOTAL_ATTACHMENT_BYTES)
                attachments.append(encoded)
            _require(type(item['residual_failures']) is list)
            per_document_residuals = deepcopy(item['residual_failures'])
            if 'complete_document_unassessed' not in per_document_residuals:
                per_document_residuals.append('complete_document_unassessed')
            evidence = {key: deepcopy(item[key]) for key in
                        ('document_id', 'as_of', 'original_pages', 'selected_pages', 'probes')}
            evidence.update(policy=POLICY, original=original, attachments=attachments,
                            residual_failures=per_document_residuals)
            validate_evidence(evidence, session_as_of=trading_date)
            record = {'ticker': item['ticker'], 'evidence': evidence}
            if 'decision_scope' in item:
                _require(type(item['decision_scope']) is dict
                    and len(json.dumps(item['decision_scope'], allow_nan=False).encode()) <= 256 * 1024)
                record['decision_scope'] = deepcopy(item['decision_scope'])
            records.append(record)
        accepted_scope = dict(scope, accepted_count=len(records), complete=True,
            registry_sha256=hashlib.sha256(json.dumps(registry, sort_keys=True, separators=(',', ':'),
                                                     allow_nan=False).encode()).hexdigest())
        result = {'bounded_documents': records, 'bounded_document_scope': accepted_scope, 'coverage': {'complete': True}}
        # Leave room beneath SourceInputStore's existing 16 MiB codec bound.
        if len(json.dumps(result, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()) > MAX_ENCODED_EVIDENCE_BYTES:
            scope['residual_failures'].append('bounded_document_envelope_limit')
            return failure
        return result
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return failure
