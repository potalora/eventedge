"""Explicit generation-bound source retirement, without automatic scope changes.

The profile names every dependent strategy and disables it with the same review
reason. Accepted source bundles retain the dependency declaration so historical
reports do not reinterpret old retirements through later strategy changes.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

POLICY = 'explicit_retirement_v1'
DECLARATION_KEY = '_source_lifecycle_policy'
_SHARED = frozenset({'fred', 'yfinance', 'openbb'})
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}')
# A retirement profile must remove policies that still require that source's
# evidence. Those validators retain their existing fail-closed semantics.
_CONFLICTS = {
    'edgar': ('filing_evidence_policy', 'filing_comparison_policy', 'filing_parser_policy',
              'filing_attribution_policy', 'filing_acquisition_policy', 'filing_material_policy'),
    'congress': ('congress_disclosure_policy',),
    'courtlistener': ('courtlistener_scope_policy',),
    'usaspending': ('award_attribution_policy',),
    'regulations': ('regulation_evidence_policy', 'regulation_retrieval_policy',
                    'regulation_publication_policy'),
}


def _invalid():
    raise ValueError('invalid_source_lifecycle_policy') from None


def _safe(value):
    return type(value) is str and _ID.fullmatch(value) is not None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _dependencies(strategies):
    result = {}
    for strategy in strategies:
        name = getattr(strategy, 'name', None)
        sources = getattr(strategy, 'data_sources', None)
        identities = getattr(strategy, 'source_identity_dependencies', [])
        if (not _safe(name) or name in result or not isinstance(sources, (list, tuple))
                or not 0 < len(sources) <= 32 or any(not _safe(source) for source in sources)
                or not isinstance(identities, (list, tuple)) or len(identities) > 32
                or any(not _safe(source) for source in identities)):
            _invalid()
        result[name] = sorted(set(sources) | set(identities))
    if not result or len(result) > 64:
        _invalid()
    return dict(sorted(result.items()))


def _retirements(value, dependencies, disabled):
    if not isinstance(value, dict) or not value or len(value) > 32:
        _invalid()
    result, reasons = {}, {}
    for source, row in value.items():
        if (not _safe(source) or source in _SHARED or not isinstance(row, dict)
                or set(row) != {'reason', 'strategies'} or not _safe(row['reason'])):
            _invalid()
        names = row['strategies']
        expected = sorted(name for name, sources in dependencies.items() if source in sources)
        if (not isinstance(names, list) or not names or any(not _safe(name) for name in names)
                or names != sorted(set(names)) or names != expected):
            _invalid()
        for name in names:
            if disabled.get(name) != row['reason'] or (name in reasons and reasons[name] != row['reason']):
                _invalid()
            reasons[name] = row['reason']
        result[source] = {'reason': row['reason'], 'strategies': list(names)}
    return dict(sorted(result.items()))


def configured(config, strategies):
    """Validate an explicit prospective profile; absence preserves old behavior."""
    policy = config.get('source_lifecycle_policy')
    if policy is None:
        if config.get('retired_sources') is not None:
            _invalid()
        return False
    if (policy != POLICY or config.get('decision_clock_policy') != 'prospective_next_open_v1'
            or config.get('research_resilience_policy') != 'dependency_isolation_v1'
            or not isinstance(config.get('disabled_strategies'), dict)):
        _invalid()
    dependencies = _dependencies(strategies)
    retired = _retirements(config.get('retired_sources'), dependencies, config['disabled_strategies'])
    for source in retired:
        if (any(config.get(key) is not None for key in _CONFLICTS.get(source, ()))
                or (source == 'edgar' and any(key.startswith('filing_') and 'manifest' in key
                    and value is not None for key, value in config.items()))):
            _invalid()
    # Focused litigation verifies issuer identities through EDGAR outside its
    # historical data_sources declaration. Do not hide that acquisition.
    if 'edgar' in retired and config.get('courtlistener_scope_policy') is not None:
        _invalid()
    return True


def declaration(config, strategies):
    if not configured(config, strategies):
        return {}
    document = {'policy': POLICY, 'retired_sources': deepcopy(config['retired_sources']),
                'strategy_sources': _dependencies(strategies)}
    return {DECLARATION_KEY: {**document, 'sha256': _digest(document)}}


def declared_scope(data, config=None, strategies=None):
    """Read immutable acquisition scope; live/replay callers also bind config."""
    value = data.get(DECLARATION_KEY)
    if config is not None:
        expected = declaration(config, strategies or [])
        if expected.get(DECLARATION_KEY) != value:
            _invalid()
    if value is None:
        return None
    if (not isinstance(value, dict)
            or set(value) != {'policy', 'retired_sources', 'strategy_sources', 'sha256'}
            or value['policy'] != POLICY or not isinstance(value['strategy_sources'], dict)):
        _invalid()
    from types import SimpleNamespace
    dependencies = _dependencies([SimpleNamespace(name=name, data_sources=sources)
        for name, sources in value['strategy_sources'].items()])
    if dependencies != value['strategy_sources']:
        _invalid()
    retired = value['retired_sources']
    if not isinstance(retired, dict):
        _invalid()
    reasons = {}
    for row in retired.values():
        if not isinstance(row, dict) or not isinstance(row.get('strategies'), list):
            _invalid()
        for name in row['strategies']:
            if not _safe(name):
                _invalid()
            reasons[name] = row.get('reason')
    canonical = _retirements(retired, dependencies, reasons)
    document = {'policy': POLICY, 'retired_sources': canonical, 'strategy_sources': dependencies}
    if value['sha256'] != _digest(document):
        _invalid()
    if any(source in data for source in retired):
        _invalid()
    return deepcopy(value)


def exclusions(scope):
    return ({name: row['reason'] for row in scope['retired_sources'].values() for name in row['strategies']}
            if scope is not None else {})


def health_evidence(scope, strategy):
    return ({'source_lifecycle_policy': POLICY, 'source_lifecycle_sha256': scope['sha256'],
             'data_sources': scope['strategy_sources'][strategy]}
            if strategy in exclusions(scope) else {})


def validate_health(scope, records, policy_ids):
    """Every configured horizon must retain its explicit zero-signal exclusion."""
    if scope is None:
        if any('source_lifecycle_policy' in (row.get('evidence', {}) if isinstance(row, dict) else row.evidence)
               for row in records):
            _invalid()
        return
    disabled = exclusions(scope)
    by_scope = {}
    for row in records:
        row = row if isinstance(row, dict) else vars(row)
        key = (row.get('policy_id'), row.get('strategy'))
        if key in by_scope:
            _invalid()
        by_scope[key] = row
    for policy_id in policy_ids:
        for name, reason in disabled.items():
            row = by_scope.get((policy_id, name), {})
            evidence = row.get('evidence', {})
            if (row.get('status') != 'disabled_by_policy' or type(row.get('signal_count')) is not int
                    or row['signal_count'] != 0 or not isinstance(evidence, dict)
                    or evidence.get('reason') != reason or type(evidence.get('candidate_count')) is not int
                    or evidence['candidate_count'] != 0
                    or evidence.get('data_sources') != scope['strategy_sources'][name]
                    or any(evidence.get(key) != value for key, value in health_evidence(scope, name).items())):
                _invalid()
