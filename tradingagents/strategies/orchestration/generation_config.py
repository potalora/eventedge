"""Bounded, secret-free configuration frozen with a generation.

The exact profile byte digest is appended to paper_ledger.policy_id. Existing
epoch semantics therefore distinguish profiles without changing the epoch schema.
State belongs to GenerationManager; credentials remain environment-owned.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

MAX_CONFIG_BYTES = 1024 * 1024
CONFIG_FILE_ENV = 'EVENTEDGE_GENERATION_CONFIG_FILE'
CONFIG_SHA_ENV = 'EVENTEDGE_GENERATION_CONFIG_SHA256'
FROZEN_CONFIG_NAME = 'generation-config.json'
_SECRET_KEY = re.compile(r'api.?key|token|password|secret|credential|authorization|cookie|private.?key', re.I)
_SECRET_VALUE = re.compile(r'^(?:sk-|Bearer\s)|-----BEGIN .*PRIVATE KEY-----', re.I)


def _invalid():
    raise ValueError('generation_config_invalid') from None


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _validate(value, depth=0):
    if depth > 64:
        _invalid()
    if type(value) is dict:
        for key, item in value.items():
            if key != 'chatgpt_plan_auth_path' and _SECRET_KEY.search(key) and item not in (None, ''):
                _invalid()
            _validate(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _validate(item, depth + 1)
    elif type(value) is float and not math.isfinite(value):
        _invalid()
    elif type(value) is str and _SECRET_VALUE.search(value):
        _invalid()


def read_generation_config(path, expected_sha256=None):
    """Read exact regular-file bytes once, rejecting links and secret material."""
    try:
        path = Path(path).absolute()
        if any(part.is_symlink() for part in (path, *path.parents)):
            _invalid()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_CONFIG_BYTES:
                _invalid()
            body = handle.read(MAX_CONFIG_BYTES + 1)
        if not 0 < len(body) <= MAX_CONFIG_BYTES:
            _invalid()
        digest = hashlib.sha256(body).hexdigest()
        if expected_sha256 is not None and (type(expected_sha256) is not str
                or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256) or digest != expected_sha256):
            _invalid()
        overlay = json.loads(body.decode('utf-8'), object_pairs_hook=_pairs,
                             parse_constant=lambda value: _invalid())
        if type(overlay) is not dict:
            _invalid()
        _validate(overlay)
        ar = overlay.get('autoresearch', {})
        if (type(ar) is not dict or 'state_dir' in ar
                or type(ar.get('paper_ledger', {})) is not dict):
            _invalid()
        policy_id = ar.get('paper_ledger', {}).get('policy_id')
        if policy_id is not None and (type(policy_id) is not str or not policy_id.strip()):
            _invalid()
        return body, overlay, digest
    except (OSError, ValueError, TypeError, RecursionError):
        _invalid()


def _merge(target, overlay):
    for key, value in overlay.items():
        if type(value) is dict and type(target.get(key)) is dict:
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def apply_generation_config(defaults, path, expected_sha256):
    """Deep-merge a verified profile and bind its exact bytes to epoch policy."""
    _, overlay, digest = read_generation_config(path, expected_sha256)
    result = copy.deepcopy(defaults)
    _merge(result, overlay)
    ar = result.setdefault('autoresearch', {})
    ledger = ar.setdefault('paper_ledger', {})
    configured = ledger.get('policy_id')
    ledger['policy_id'] = ((configured + '-') if configured else '') + 'generation-config-' + digest
    return result
