"""Offline, reproducible input planning proxy; never model transport entitlement.

Only the bundled hash-pinned public vocabulary is read. No tokenizer registry,
model alias, network loader, user cache or special-token interpretation is used.
"""
from __future__ import annotations

import base64
from functools import lru_cache
import hashlib
import re
from importlib import metadata, resources

from .regulation_evidence import digest

POLICY = 'o200k_base_planning_v1'
PROOF_VERSION = 'regulation-prompt-planning-proof-v1'
TOKENIZER_VERSION = '0.12.0'
VOCABULARY_SHA256 = '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d'
PLANNING_WINDOW = 500000
RESERVED_TOKENS = 25000
TOKEN_LIMIT = PLANNING_WINDOW - RESERVED_TOKENS
MAX_PROMPT_BYTES = 16 * 1024**2
# Exact o200k_base pattern from OpenAI tiktoken 0.12.0 (MIT; bundled notice).
_PATTERN = '|'.join([
    r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
    r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
    r'\p{N}{1,3}', r' ?[^\s\p{L}\p{N}]+[\r\n/]*', r'\s*[\r\n]+', r'\s+(?!\S)', r'\s+',
])
_FAILURES = {'tokenizer_unavailable', 'tokenizer_version', 'vocabulary_unavailable', 'vocabulary_hash'}


def _fail(code):
    raise ValueError('invalid_regulation_prompt_' + code)


def configured(config):
    policy = config.get('regulation_prompt_budget_policy')
    if policy not in (None, POLICY) or (policy is not None and (
            config.get('regulation_evidence_policy') != 'complete_document_text_v1'
            or config.get('graphics_execution_policy') != 'single_target_compact_v1')):
        _fail('budget_policy')
    return policy


def _installed_version():
    return metadata.version('tiktoken')


def _vocabulary_bytes():
    return resources.files(__package__).joinpath('assets/o200k_base.tiktoken').read_bytes()


@lru_cache(maxsize=1)
def _build_encoding(vocabulary):
    import tiktoken
    # Parsing already hash-verified local bytes avoids tiktoken.load's remote
    # resource/cache machinery entirely. A cached encoding is only a pure speedup;
    # installed version and current vocabulary bytes/hash are checked each time.
    ranks = {}
    for line in vocabulary.splitlines():
        token, rank = line.split()
        ranks[base64.b64decode(token, validate=True)] = int(rank)
    return tiktoken.Encoding(name='eventedge_o200k_base_planning_v1',
        pat_str=_PATTERN, mergeable_ranks=ranks,
        special_tokens={'<|endoftext|>': 199999, '<|endofprompt|>': 200018})


def _counter():
    try:
        if _installed_version() != TOKENIZER_VERSION:
            return None, 'tokenizer_version'
    except metadata.PackageNotFoundError:
        return None, 'tokenizer_unavailable'
    try:
        vocabulary = _vocabulary_bytes()
    except (OSError, FileNotFoundError):
        return None, 'vocabulary_unavailable'
    if hashlib.sha256(vocabulary).hexdigest() != VOCABULARY_SHA256:
        return None, 'vocabulary_hash'
    try:
        return _build_encoding(vocabulary), None
    except (ImportError, ValueError, TypeError):
        return None, 'tokenizer_unavailable'


def _base(system, user):
    if type(system) is not str or type(user) is not str:
        _fail('input')
    try:
        system_raw, user_raw = system.encode('utf-8'), user.encode('utf-8')
    except UnicodeEncodeError:
        _fail('input')
    return {'version': PROOF_VERSION, 'policy': POLICY,
        'tokenizer': 'tiktoken', 'tokenizer_version': TOKENIZER_VERSION,
        'encoding': 'o200k_base', 'vocabulary_sha256': VOCABULARY_SHA256,
        'count_method': 'sum_encode_ordinary_system_user',
        'system_sha256': hashlib.sha256(system_raw).hexdigest(),
        'user_sha256': hashlib.sha256(user_raw).hexdigest(),
        'prompt_bytes': len(system_raw) + len(user_raw),
        'planning_window': PLANNING_WINDOW, 'reserved_tokens': RESERVED_TOKENS,
        'planning_limit': TOKEN_LIMIT, 'not_transport_entitlement': True,
        'limitation': 'Conservative input planning proxy; excludes transport framing and does not establish model context entitlement.'}


def prepare_proof(system, user):
    proof = _base(system, user)
    too_large = proof['prompt_bytes'] > MAX_PROMPT_BYTES
    counter, failure = (None, 'byte_limit_exceeded') if too_large else _counter()
    count = None if counter is None else len(counter.encode_ordinary(system)) + len(counter.encode_ordinary(user))
    proof.update(status='rejected_bytes' if too_large else 'unavailable' if failure else 'measured',
        input_tokens=count, within_planning_limit=count is not None and count <= TOKEN_LIMIT,
        failure_reason=failure)
    proof['proof_sha256'] = digest(proof)
    return proof


def _validate_shape(proof):
    base = _base('', '')
    dynamic = {'system_sha256', 'user_sha256', 'prompt_bytes'}
    expected_keys = set(base) | {'status', 'input_tokens', 'within_planning_limit', 'failure_reason', 'proof_sha256'}
    if (type(proof) is not dict or set(proof) != expected_keys
            or any(type(proof[k]) is not type(v) or proof[k] != v for k,v in base.items() if k not in dynamic)
            or any(type(proof[k]) is not str or not re.fullmatch('[a-f0-9]{64}', proof[k])
                for k in ('system_sha256', 'user_sha256', 'proof_sha256'))
            or type(proof['prompt_bytes']) is not int or proof['prompt_bytes'] < 0
            or type(proof['within_planning_limit']) is not bool
            or proof['proof_sha256'] != digest({k:v for k,v in proof.items() if k != 'proof_sha256'})):
        _fail('proof')
    if proof['status'] == 'rejected_bytes':
        if (proof['prompt_bytes'] <= MAX_PROMPT_BYTES or proof['input_tokens'] is not None
                or proof['within_planning_limit'] is not False or proof['failure_reason'] != 'byte_limit_exceeded'):
            _fail('proof')
    elif proof['status'] == 'unavailable':
        if (proof['prompt_bytes'] > MAX_PROMPT_BYTES or proof['input_tokens'] is not None
                or proof['within_planning_limit'] is not False or proof['failure_reason'] not in _FAILURES):
            _fail('proof')
    elif proof['status'] == 'measured':
        if (proof['prompt_bytes'] > MAX_PROMPT_BYTES or type(proof['input_tokens']) is not int
                or not 0 <= proof['input_tokens'] <= proof['prompt_bytes']
                or proof['within_planning_limit'] is not (proof['input_tokens'] <= TOKEN_LIMIT)
                or proof['failure_reason'] is not None):
            _fail('proof')
    else:
        _fail('proof')


def validate_proof(system, user, proof):
    """Recount measured proofs; unavailable historical attempts carry no authority.

    An unavailable counter's historic host condition cannot be reconstructed from
    a later repaired installation. Only its finite failed receipt and exact prompt
    binding are validated; it can never authorize dispatch or a valid assessment.
    """
    _validate_shape(proof)
    if any(proof[k] != v for k,v in _base(system, user).items()):
        _fail('proof')
    if proof['status'] == 'measured' and proof != prepare_proof(system, user):
        _fail('proof')
    return proof


def require_dispatchable(proof):
    _validate_shape(proof)
    if proof['status'] != 'measured':
        _fail(proof['failure_reason'])
    if proof['within_planning_limit'] is not True:
        _fail('planning_budget_exceeded')
