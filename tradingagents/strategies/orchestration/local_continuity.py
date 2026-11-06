"""Fold independently collected local evidence; never alter production readiness.

Callers must derive checks from the ordinary runner's retained artifacts, native
receipts, read-only ledger validation and anchored replay. This pure projection
does not certify supplied attestations or establish deployed scheduler health.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date

from .trading_calendar import is_session, previous_session

REQUIRED_CHECKS = ('accounting', 'staging', 'sources', 'screening', 'model_outcomes',
                   'committee', 'timeliness', 'report', 'replay', 'cleanup')
EXPECTED_MODELS = frozenset(('gpt-6-luna', 'gpt-6.1-sol'))
_SUCCESS = frozenset(('success', 'success_empty', 'complete'))
_FAILURE = frozenset(('failed', 'partial', 'invalid'))


def _identity(row):
    return tuple(row.get(key) for key in ('generation_id', 'generation_commit',
                                         'profile_sha256', 'epoch_id'))


def classify_session(observation, *, expected_commit, expected_profile):
    """Keep reduced scope, degraded holds and missing proof explicitly separate."""
    row = deepcopy(observation)
    checks = row.get('checks', {})
    reasons = [name for name in REQUIRED_CHECKS if checks.get(name) is not True]
    try:
        session = date.fromisoformat(row['session'])
        if session.isoformat() != row['session'] or not is_session(session):
            reasons.append('invalid_session')
    except (KeyError, TypeError, ValueError):
        reasons.append('invalid_session')
    if row.get('generation_commit') != expected_commit or row.get('profile_sha256') != expected_profile:
        reasons.append('candidate_identity')
    if not all(type(value) is str and value for value in _identity(row)):
        reasons.append('candidate_identity')
    if type(row.get('attempts')) is not int or row['attempts'] != 1:
        reasons.append('live_attempt_count')
    models = row.get('native_models', [])
    if (type(models) is not list or any(model not in EXPECTED_MODELS for model in models)
            or not models and checks.get('no_model_work_required') is not True):
        reasons.append('native_model_evidence')
    degraded = bool(row.get('unexpected_failures') or row.get('research_holds'))
    if degraded:
        reasons.append('unexpected_degradation')
    row['reasons'] = sorted(set(reasons))
    row['qualifies'] = not reasons
    row['status'] = ('qualifying_reduced_scope' if row['qualifies'] and row.get('approved_exclusions')
                     else 'qualifying' if row['qualifies']
                     else 'degraded' if degraded else 'failed')
    return row


def summarize_continuity(records, *, through: date, required_sessions=5):
    """Require distinct consecutive calendar sessions and unchanged identities.

    Recovery means a failed live source becomes usable on a later qualifying
    normal attempt under the same generation/profile/epoch. Same-day retries,
    a replacement epoch and code/config edits cannot supply that observation.
    """
    if type(through) is not date or not is_session(through) or required_sessions != 5:
        raise ValueError('local continuity requires five sessions through an XNYS session')
    if type(records) is not list or len(records) > 100:
        raise ValueError('local continuity record bound exceeded')
    by_date = {}
    for row in records:
        if type(row) is not dict or type(row.get('session')) is not str:
            raise ValueError('invalid local continuity record')
        session = date.fromisoformat(row['session'])
        if session.isoformat() != row['session'] or not is_session(session) or session > through:
            raise ValueError('invalid local continuity session')
        by_date.setdefault(session, []).append(row)
    cursor, window, identity = through, [], None
    while len(window) < required_sessions:
        matches = by_date.get(cursor, [])
        if len(matches) != 1 or matches[0].get('qualifies') is not True:
            break
        row = matches[0]
        if identity is None:
            identity = _identity(row)
        if _identity(row) != identity:
            break
        window.append(row)
        cursor = previous_session(cursor)
    models = sorted({model for row in window for model in row.get('native_models', [])})
    recovered = []
    failed = {}
    for session, matches in sorted(by_date.items()):
        if len(matches) != 1:
            continue
        row = matches[0]
        if _identity(row) != identity or row.get('attempts') != 1:
            continue
        for provider, status in row.get('source_observations', {}).items():
            if provider == 'edgar':
                continue  # Approved unavailable scope can never become recovery credit.
            if status in _FAILURE:
                failed[provider] = session
            elif (status in _SUCCESS and provider in failed and failed[provider] < session
                    and row.get('qualifies') is True):
                recovered.append({'provider': provider, 'failed_session': failed[provider].isoformat(),
                                  'recovered_session': session.isoformat()})
                del failed[provider]
    continuity = len(window) == required_sessions
    return {'required_consecutive_sessions': required_sessions, 'through': through.isoformat(),
        'consecutive_qualifying_sessions': len(window),
        'qualifying_sessions': [row['session'] for row in reversed(window)],
        'five_session_continuity_accepted': continuity,
        'native_models_observed': models,
        'automatic_recovery_observed': bool(recovered), 'recoveries': recovered,
        'local_operational_acceptance': continuity and set(models) == EXPECTED_MODELS and bool(recovered),
        'fill_lifecycle_observed': any(row.get('fill_lifecycle_observed') is True for row in window),
        'carried_positions_observed': any(row.get('carried_positions_observed') is True for row in window),
        'deployed_runtime_reliability_verified': False}
