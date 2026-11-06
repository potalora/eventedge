"""Finite inline document integrity for the opt-in Luna plan pilot.

Magic and independently supplied hashes verify transport identity only. They
never certify a PDF's correctness, page completeness, or extraction quality.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import re

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_TOTAL_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_INPUTS = 8
_FORMATS = {'image/png': ('.png', b'\x89PNG\r\n\x1a\n'),
            'application/pdf': ('.pdf', b'%PDF-')}
_WIRE_KEYS = frozenset({'media_type', 'filename', 'sha256', 'data_base64'})
ORIGINALS_LABEL_POLICY = 'originals_label_v1'


def _invalid():
    raise ValueError('invalid_document_inputs') from None


def _validate(media_type, data, filename, sha256):
    if type(media_type) is not str or media_type not in _FORMATS:
        _invalid()
    extension, magic = _FORMATS[media_type]
    if (type(filename) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', filename)
            or '..' in filename or not filename.endswith(extension)):
        _invalid()
    if type(data) is not bytes or not 0 < len(data) <= MAX_DOCUMENT_BYTES or not data.startswith(magic):
        _invalid()
    if (type(sha256) is not str or not re.fullmatch(r'[0-9a-f]{64}', sha256)
            or hashlib.sha256(data).hexdigest() != sha256):
        _invalid()


@dataclass(frozen=True)
class PlanDocumentInput:
    media_type: str
    data: bytes = field(repr=False)
    filename: str
    sha256: str
    presentation_policy: str | None = None

    def __post_init__(self):
        _validate(self.media_type, self.data, self.filename, self.sha256)
        if self.presentation_policy not in (None, ORIGINALS_LABEL_POLICY):
            _invalid()


def encode_document_inputs(inputs):
    """Encode explicitly supplied local bytes; never fetch, infer, or truncate."""
    if type(inputs) not in (list, tuple) or not 1 <= len(inputs) <= MAX_DOCUMENT_INPUTS:
        _invalid()
    result, total, policies = [], 0, set()
    for item in inputs:
        if type(item) is not PlanDocumentInput:
            _invalid()
        _validate(item.media_type, item.data, item.filename, item.sha256)
        if item.presentation_policy not in (None, ORIGINALS_LABEL_POLICY):
            _invalid()
        policies.add(item.presentation_policy)
        total += len(item.data)
        if total > MAX_TOTAL_DOCUMENT_BYTES:
            _invalid()
        result.append({'media_type': item.media_type, 'filename': item.filename,
                       'sha256': item.sha256,
                       'data_base64': base64.b64encode(item.data).decode('ascii')})
        if item.presentation_policy is not None:
            result[-1]['presentation_policy'] = item.presentation_policy
    if len(policies) != 1:
        _invalid()
    return result


def _validated_wire(wire_inputs):
    if type(wire_inputs) is not list or not 1 <= len(wire_inputs) <= MAX_DOCUMENT_INPUTS:
        _invalid()
    result, total, policies = [], 0, set()
    for item in wire_inputs:
        if type(item) is not dict or item.keys() not in (_WIRE_KEYS, _WIRE_KEYS | {'presentation_policy'}):
            _invalid()
        policy = item.get('presentation_policy')
        if 'presentation_policy' in item and policy != ORIGINALS_LABEL_POLICY:
            _invalid()
        policies.add(policy)
        encoded = item['data_base64']
        if type(encoded) is not str or not 0 < len(encoded) <= 4 * ((MAX_DOCUMENT_BYTES + 2) // 3):
            _invalid()
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            _invalid()
        if base64.b64encode(data).decode('ascii') != encoded:
            _invalid()
        _validate(item['media_type'], data, item['filename'], item['sha256'])
        total += len(data)
        if total > MAX_TOTAL_DOCUMENT_BYTES:
            _invalid()
        result.append((dict(item), len(data)))
    if len(policies) != 1:
        _invalid()
    return result


def document_input_content(wire_inputs):
    """Revalidate exact JSON wire records before building official content blocks."""
    result = []
    for index, (item, _) in enumerate(_validated_wire(wire_inputs), 1):
        if item.get('presentation_policy') == ORIGINALS_LABEL_POLICY:
            result.append({'type': 'input_text',
                           'text': f"Attached image {index} (one-based): {item['filename']}"})
        data_url = f"data:{item['media_type']};base64,{item['data_base64']}"
        if item['media_type'] == 'application/pdf':
            result.append({'type': 'input_file', 'filename': item['filename'],
                           'file_data': data_url, 'detail': 'high'})
        else:
            result.append({'type': 'input_image', 'image_url': data_url, 'detail': 'high'})
    return result


def document_input_metadata(wire_inputs):
    """Detached ordered metadata only; never include document content."""
    return [{'media_type': item['media_type'], 'filename': item['filename'],
             'sha256': item['sha256'], 'size_bytes': size}
            | ({'presentation_policy': item['presentation_policy']} if 'presentation_policy' in item else {})
            for item, size in _validated_wire(wire_inputs)]
