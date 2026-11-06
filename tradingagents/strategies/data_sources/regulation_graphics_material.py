"""Lossless readable Sol serialization of already image-validated compact material.

This codec supplies no source completeness authority. The original image-backed
recognition and independent review must be replayed before encoding a material.
"""
from __future__ import annotations

import json
import re

VERSION = 'regulation-graphic-compact-material-v1'
# Content retains the 256KiB response bound; ordered original source bindings
# and JSON escaping may add bytes. The final complete prompt is budgeted separately.
MAX_BYTES = 512 * 1024
_KEYS = {'id', 'sha256', 'content', 'sources'}
_CONTENT_KEYS = {'text', 'tables', 'notes', 'continuations', 'visuals', 'reading_order'}
_SOURCE_KEYS = {'image_position', 'id', 'filename', 'sha256'}
_BASIS = 'image_backed_machine_review_not_accuracy_guarantee'


def _fail():
    raise ValueError('invalid_regulation_graphic_material_serialization')


def encode_material(material):
    if type(material) is not dict or set(material) != _KEYS:
        _fail()
    content, sources = material['content'], material['sources']
    if (type(material['id']) is not str or not material['id']
            or type(material['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', material['sha256'])
            or type(content) is not dict or set(content) != _CONTENT_KEYS
            or any(type(content[key]) is not list for key in _CONTENT_KEYS)
            or any(type(value) is not str for value in content['text'])
            or any(type(value) is not dict for key in _CONTENT_KEYS - {'text'} for value in content[key])
            or type(sources) is not list or not 1 <= len(sources) <= 7):
        _fail()
    for index, source in enumerate(sources, 1):
        if (type(source) is not dict or set(source) != _SOURCE_KEYS
                or type(source['image_position']) is not int or source['image_position'] != index
                or any(type(source[key]) is not str or not source[key] for key in ('id', 'filename', 'sha256'))
                or not re.fullmatch('[a-f0-9]{64}', source['sha256'])):
            _fail()
    if sources[0]['id'] != material['id'] or sources[0]['sha256'] != material['sha256']:
        _fail()
    try:
        text = json.dumps({'version': VERSION, 'basis': _BASIS, 'graphic': material},
            ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
        if len(text.encode('utf-8')) > MAX_BYTES:
            _fail()
        # JSON preserves this canonical schema's types. Reject alternative Python
        # objects (such as tuples or integer keys) that a JSON projection changes.
        if json.loads(text)['graphic'] != material:
            _fail()
        return text
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        _fail()


def decode_material(text):
    if type(text) is not str:
        _fail()
    try:
        if len(text.encode('utf-8')) > MAX_BYTES:
            _fail()
        value = json.loads(text)
        if (type(value) is not dict or set(value) != {'version', 'basis', 'graphic'}
                or value['version'] != VERSION or value['basis'] != _BASIS
                or encode_material(value['graphic']) != text):
            _fail()
        return value['graphic']
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        _fail()
