"""Prospective retirement of one unsupported optional enrichment capability.

Declarations describe acquisition scope, never a provider result or permission
to trade. Historical enrichment without this policy retains its original rules.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

POLICY = 'optional_enrichment_retirement_v1'
DECLARATION_KEY = '_enrichment_capability_policy'
RETIRED = {'commodity_futures_curves': 'maturity_identified_curve_evidence_unavailable'}
UNDERLYING_SYMBOLS = frozenset({'GC', 'SI', 'CL', 'NG', 'HG'})
_HOLDS = frozenset({'model_deadline_exhausted', 'shared_regime_unavailable', 'required_universe_unavailable'})


def _invalid():
    raise ValueError('invalid_enrichment_capability_policy') from None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def configured(config):
    """Reject implicit or unknown retirements; absence preserves legacy scope."""
    if not isinstance(config, dict):
        _invalid()
    policy = config.get('enrichment_capability_policy')
    retired = config.get('retired_enrichment_capabilities')
    if policy is None:
        if retired is not None:
            _invalid()
        return False
    if type(policy) is not str or policy != POLICY or type(retired) is not dict or retired != RETIRED:
        _invalid()
    return True


def declaration(config, underlying_symbols):
    """Build a detached finite scope, including sessions with no affected symbol."""
    if not configured(config):
        return {}
    if (not isinstance(underlying_symbols, (list, tuple, set, frozenset))
            or any(type(symbol) is not str or symbol not in UNDERLYING_SYMBOLS
                for symbol in underlying_symbols)):
        _invalid()
    document = {'policy': POLICY, 'retired_capabilities': deepcopy(RETIRED),
        'underlying_symbols': sorted(set(underlying_symbols))}
    return {DECLARATION_KEY: {**document, 'sha256': _digest(document)}}


def declared_scope(original, config):
    """Bind frozen scope to configuration; retired results are integrity errors."""
    enabled = configured(config)
    if not isinstance(original, dict):
        _invalid()
    if not enabled:
        if DECLARATION_KEY in original:
            _invalid()
        return None
    value = original.get(DECLARATION_KEY)
    if (type(value) is not dict or set(value) != {'policy', 'retired_capabilities', 'underlying_symbols', 'sha256'}
            or type(value.get('underlying_symbols')) is not list):
        _invalid()
    expected = declaration(config, value['underlying_symbols'])[DECLARATION_KEY]
    if value != expected:
        _invalid()
    errors = original.get('errors', {})
    if not isinstance(errors, dict) or any(operation in original or operation in errors for operation in RETIRED):
        _invalid()
    return deepcopy(value)


def supported_complete(scope, config):
    """Validate the coverage derivation of a reconstructed projection receipt.

    Callers must reconstruct this scope from accepted enrichment and compare it
    with durable committee scope. This helper cannot authenticate a receipt or
    its hashes; it never treats its stored supported-success bit as evidence.
    """
    enabled = configured(config)
    if not isinstance(scope, dict):
        _invalid()
    expected_keys = {'version', 'policy', 'original_sha256', 'projection_sha256', 'excluded',
        'hold_reasons', 'research_coverage_complete', 'order_authority'}
    if enabled:
        expected_keys.update({'declared_exclusions', 'supported_enrichment_complete'})
    if (set(scope) != expected_keys
            or scope['version'] != ('research-enrichment-selection-v2' if enabled else 'research-enrichment-selection-v1')
            or scope['policy'] != 'dependency_isolation_v1'
            or scope['order_authority'] is not False
            or any(type(scope[key]) is not str or re.fullmatch('[a-f0-9]{64}', scope[key]) is None
                for key in ('original_sha256', 'projection_sha256'))
            or type(scope['excluded']) is not list or type(scope['hold_reasons']) is not list
            or any(type(reason) is not str or reason not in _HOLDS for reason in scope['hold_reasons'])):
        _invalid()
    if enabled:
        declared_scope({DECLARATION_KEY: scope['declared_exclusions']}, config)
    operations = {'openbb', 'profiles', 'short_interest', 'factors'}
    if not enabled:
        operations.add('commodity_futures_curves')
    for row in scope['excluded']:
        if (type(row) is not dict or set(row) != {'operation', 'symbol', 'reason'}
                or type(row['operation']) is not str or row['operation'] not in operations
                or row['reason'] != 'recorded_source_failure'
                or (row['symbol'] is not None and type(row['symbol']) is not str)):
            _invalid()
    complete = not bool(scope['excluded'] or scope['hold_reasons'])
    if (scope['research_coverage_complete'] is not (False if enabled else complete)
            or enabled and scope['supported_enrichment_complete'] is not complete):
        _invalid()
    return complete
