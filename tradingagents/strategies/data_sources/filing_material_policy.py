"""Exact, opt-in framing-only evidence for three unassessed SEC originals.

Pure standard-library module: worker children load it directly without importing
providers. This permission establishes neither selected-evidence completeness nor
material adequacy. Original bytes and their framing remain acquisition duties.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re

POLICY = 'retained_three_material_gaps_v1'
_VERSION = 'sec-filing-evidence-v1'
_MAX_ORIGINAL = 512 * 1024 * 1024
_METADATA = ('version', 'format', 'accession', 'form', 'filing_date', 'observed_at',
             'accepted_at', 'submission_sha256', 'header_sha256', 'roles', 'issuer_candidates')
_DOCUMENT_KEYS = {'type', 'sequence', 'filename', 'body_start', 'body_end', 'body_sha256'}
_ROLE_KEYS = {'role', 'cik', 'name', 'start', 'end', 'sha256'}
_ROWS = (
    ('0001193125-26-402806', '10-K', '2026-09-25', '0001512228',
     'NIOCORP DEVELOPMENTS LTD', '20260925160238', 310, '1512228',
     ((1, 'nb-20260630.htm'),), 'unassessed_technical_diagrams'),
    ('0000016732-26-000031', 'DEF 14A', '2026-10-07', '0000016732',
     "CAMPBELL'S Co", '20261007085538', 91, '16732',
     ((1, 'cpb-20261007.htm'), (81, 'cpb2026_courtesy-pdfa.pdf')),
     'unverified_proxy_pdf_image_material'),
    ('0000818479-26-000278', '8-K', '2026-10-09', '0000818479',
     'DENTSPLY SIRONA Inc.', '20261009161618', 505, '818479',
     ((1, 'xray-20261008.htm'),), 'unverified_visual_redline_semantics'),
)


def _digest(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'),
            ensure_ascii=True, allow_nan=False).encode('ascii')
    except (TypeError, ValueError, OverflowError):
        _invalid()
    return hashlib.sha256(encoded).hexdigest()


def _invalid():
    raise ValueError('Invalid SEC material quarantine evidence')


def configured(config: dict) -> bool:
    value = config.get('filing_material_policy')
    if value is None:
        return False
    if (value != POLICY or config.get('filing_evidence_policy') != 'complete_submission_v1'
            or config.get('filing_acquisition_policy') != 'bounded_original_submission_v1'
            or config.get('filing_parser_policy') != 'two_processes_v1'):
        raise ValueError('SEC material quarantine requires complete bounded two-process evidence')
    return True


def policy_manifest() -> dict:
    identities = []
    for accession, form, date, cik, name, accepted, count, url_cik, primaries, gap in _ROWS:
        identities.append(dict(accession=accession, form=form, filing_date=date,
            source_url=f'https://www.sec.gov/Archives/edgar/data/{url_cik}/{accession.replace("-", "")}/{accession}.txt',
            issuer_cik=cik, issuer_name=name, accepted_at=accepted, document_count=count,
            primary_candidates=[dict(sequence=sequence, type=form, filename=filename)
                                for sequence, filename in primaries], gap_codes=[gap] + (['ambiguous_primary_document'] if form == 'DEF 14A' else [])))
    return {'policy': POLICY, 'identities': identities}


def policy_manifest_sha256() -> str:
    return _digest(policy_manifest())


def approved_identity(identity: dict) -> dict | None:
    if not isinstance(identity, dict):
        _invalid()
    for row in policy_manifest()['identities']:
        if identity.get('accession') == row['accession']:
            if any(identity.get(key) != row[key] for key in ('form', 'filing_date', 'source_url')):
                _invalid()
            return row
    return None


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _validate_inventory(documents, size):
    if not isinstance(documents, list):
        _invalid()
    sequences, filenames, previous_end, previous_sequence = set(), set(), 0, 0
    for document in documents:
        if not isinstance(document, dict) or set(document) != _DOCUMENT_KEYS:
            _invalid()
        start, end, sequence = (document[k] for k in ('body_start', 'body_end', 'sequence'))
        filename, kind = document['filename'], document['type']
        if (type(start) is not int or type(end) is not int or not previous_end < start < end < size
                or type(sequence) is not int or sequence <= previous_sequence or sequence in sequences
                or not isinstance(filename, str)
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}', filename) is None
                or '..' in filename or filename in filenames or not isinstance(kind, str)
                or not kind or kind.strip() != kind or not _hash(document['body_sha256'])):
            _invalid()
        sequences.add(sequence)
        filenames.add(filename)
        previous_end, previous_sequence = end, sequence


def _validate_frame(framed, size, row, documents):
    if (type(size) is not int or not 0 < size <= _MAX_ORIGINAL
            or framed.get('version') != _VERSION
            or framed.get('format') not in {'nc_submission', 'sec_complete_submission'}
            or framed.get('accepted_at') != row['accepted_at']
            or not _hash(framed.get('submission_sha256')) or not _hash(framed.get('header_sha256'))
            or len(documents) != row['document_count']):
        _invalid()
    try:
        observed = datetime.fromisoformat(framed['observed_at'])
        if observed.tzinfo is None or observed.utcoffset() is None:
            _invalid()
    except (TypeError, ValueError, KeyError):
        _invalid()
    _validate_inventory(documents, size)
    roles = framed.get('roles')
    if not isinstance(roles, list) or len(roles) != 1:
        _invalid()
    role = roles[0]
    if (not isinstance(role, dict) or set(role) != _ROLE_KEYS
            or role.get('role') != 'FILER' or role.get('cik') != row['issuer_cik']
            or role.get('name') != row['issuer_name'] or not _hash(role.get('sha256'))
            or type(role.get('start')) is not int or type(role.get('end')) is not int
            or not 0 <= role['start'] < role['end'] < documents[0]['body_start']
            or framed.get('issuer_candidates') != roles):
        _invalid()
    primaries = [document for document in documents if document['type'] == row['form']]
    if [dict(sequence=d['sequence'], type=d['type'], filename=d['filename']) for d in primaries] != row['primary_candidates']:
        _invalid()
    return primaries


def _required(documents, required_exhibits):
    if not isinstance(required_exhibits, (tuple, list)):
        _invalid()
    for required in required_exhibits:
        if (not isinstance(required, str) or not required or len(required) > 255
                or len([d for d in documents if d['filename'] == required
                        or d['type'].upper() == required.upper()]) != 1):
            _invalid()
    return list(required_exhibits)


def quarantine_evidence(framed: dict, *, submission_size: int, required_exhibits=()) -> dict:
    # A successful native frame has no source URL: derive only this policy's
    # exact canonical URL, then bind the observed receipt independently upstream.
    if not isinstance(framed, dict):
        _invalid()
    candidates = [r for r in policy_manifest()['identities'] if r['accession'] == framed.get('accession')]
    if len(candidates) != 1:
        _invalid()
    row = candidates[0]
    approved_identity(dict(framed, source_url=framed.get('source_url', row['source_url'])))
    documents = framed.get('documents')
    if not isinstance(documents, list):
        _invalid()
    primaries = _validate_frame(framed, submission_size, row, documents)
    if framed.get('primary_candidates') != [i for i, d in enumerate(documents) if d['type'] == row['form']]:
        _invalid()
    required = _required(documents, required_exhibits)
    result = {key: deepcopy(framed[key]) for key in _METADATA}
    result.update(source_url=row['source_url'], structural_status='insufficient',
        structural_scope='complete_original_inventory_unselected', analysis_adequacy='insufficient',
        dependency_assessment='not_assessed', units=[], dependencies=[],
        issues=[{'code': code} for code in row['gap_codes']], document_inventory=deepcopy(documents),
        material_quarantine=dict(policy=POLICY, policy_manifest_sha256=policy_manifest_sha256(),
            submission_size=submission_size, document_count=len(documents),
            primary_candidates=deepcopy(primaries), gap_codes=list(row['gap_codes']),
            required_exhibits=required, document_inventory_sha256=_digest(documents)))
    return result


def validate_quarantined_evidence(evidence: dict) -> dict:
    if not isinstance(evidence, dict):
        _invalid()
    row = approved_identity(evidence)
    if row is None or not isinstance(evidence.get('material_quarantine'), dict):
        _invalid()
    declaration = evidence['material_quarantine']
    documents = evidence.get('document_inventory')
    if not isinstance(documents, list):
        _invalid()
    framed = {key: evidence.get(key) for key in _METADATA}
    framed.update(source_url=evidence.get('source_url'), documents=documents,
        primary_candidates=[i for i, d in enumerate(documents)
                            if isinstance(d, dict) and d.get('type') == row['form']])
    expected = quarantine_evidence(framed, submission_size=declaration.get('submission_size'),
        required_exhibits=declaration.get('required_exhibits'))
    if evidence != expected or _digest(evidence) != _digest(expected):
        _invalid()
    return row
