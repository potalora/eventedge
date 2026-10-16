"""Bind disclosure-only acquisition to a disabled trading strategy."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta

from tradingagents.strategies.data_sources.congress_disclosure_audit import POLICY

STRATEGY = 'congressional_trades'
DECLARATION_KEY = '_congress_disclosure_policy'
DECLARATION = {'policy': POLICY, 'strategy': STRATEGY, 'signals_enabled': False,
               'model_context_enabled': False, 'stock_disclosure_complete': False}


def configured(config):
    policy = config.get('congress_disclosure_policy')
    reason = config.get('disabled_strategies', {}).get(STRATEGY)
    if (policy not in (None, POLICY) or (policy == POLICY and reason != POLICY)
            or (reason == POLICY and policy != POLICY)):
        raise ValueError('invalid_congress_disclosure_policy')
    return policy == POLICY


def declared_exclusions(data, config=None):
    """Read a frozen declaration; live/replay callers also check configuration."""
    declared = data.get(DECLARATION_KEY)
    if config is not None:
        enabled = configured(config)
        if enabled != (declared is not None):
            raise ValueError('congress disclosure declaration mismatch')
    if declared is None:
        return {}
    if declared != DECLARATION or any(type(declared[key]) is not bool
                                     for key in ('signals_enabled', 'model_context_enabled', 'stock_disclosure_complete')):
        raise ValueError('invalid_congress_disclosure_declaration')
    return {STRATEGY: POLICY}


def declaration(config):
    return {DECLARATION_KEY: deepcopy(DECLARATION)} if configured(config) else {}


def audit_scope(data, config, session, *, now=None, replay=False):
    """Return independently checked display limits, without model context."""
    enabled = bool(declared_exclusions(data, config))
    payload = data.get('congress', {})
    if not enabled:
        if isinstance(payload, dict) and 'audit_snapshot' in payload:
            raise ValueError('undeclared_congress_audit_snapshot')
        return None
    if not isinstance(payload, dict) or any(key in payload for key in ('trades', 'recent_trades', 'all_trades')):
        raise ValueError('invalid_congress_audit_payload')
    if 'audit_snapshot' not in payload:
        if isinstance(payload.get('error'), str) and payload['error'].strip():
            return None
        raise ValueError('missing_congress_audit_snapshot')
    from tradingagents.strategies.data_sources.congress_disclosure_audit import validate_audit_snapshot, audit_summary
    bound = {key: payload[key] for key in ('audit_snapshot', 'coverage')}
    end = date.fromisoformat(session)
    validate_audit_snapshot(bound, date_filed_after=(end - timedelta(days=30)).isoformat(),
        date_filed_before=session, now=now, **({'max_evidence_age_seconds': None} if replay else {}))
    return audit_summary(bound)
