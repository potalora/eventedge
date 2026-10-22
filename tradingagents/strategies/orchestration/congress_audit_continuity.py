"""A recent, verified display-only House gap may remain a visible warning.

This prospective policy changes continuity classification only. It never repairs
raw coverage, supplies filing data, grants trading authority, or uses report time
to reinterpret an accepted acquisition.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib

POLICY = 'recent_snapshot_warning_v1'
KEY = 'congress_audit_continuity_policy'
_DISPLAY = 'display_audit_only_v1'
_ERROR = 'Congress audit House filing reconciliation gap [invalid_response]'


def _invalid():
    raise ValueError('invalid_congress_audit_continuity_policy') from None


def configured(config):
    """Opt in only within the existing supported, nontrading Congress scope."""
    if not isinstance(config, dict):
        _invalid()
    policy = config.get(KEY)
    if policy is None:
        return False
    disabled = config.get('disabled_strategies')
    if (type(policy) is not str or policy != POLICY
            or config.get('congress_disclosure_policy') != _DISPLAY
            or config.get('stabilization_profile_policy') != 'supported_sixteen_books_v1'
            or not isinstance(disabled, dict)
            or disabled.get('congressional_trades') != _DISPLAY):
        _invalid()
    return True


def _instant(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00')) if type(value) is str else value
        if (not isinstance(result, datetime) or result.tzinfo is None
                or result.utcoffset() != timedelta(0)):
            _invalid()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        _invalid()


def warning_for_source(payload, config, session, acquired_at):
    """Reconstruct a Congress provider wrapper at its frozen envelope clock.

    Ordinary unavailable sources and other gap types return no warning and keep
    their existing blocking classification. Malformed or contradictory accepted
    evidence raises an integrity error. The caller must retain the raw payload,
    false coverage, unresolved diagnostic, and returned warning together.
    """
    if not configured(config):
        return None
    if not isinstance(payload, dict):
        _invalid()
    if 'audit_snapshot' not in payload:
        return None
    if set(payload) - {'audit_snapshot', 'coverage', '_request_diagnostics', 'error', '_coverage', 'house_originals'}:
        _invalid()
    try:
        reference = date.fromisoformat(session) if type(session) is str else session
        if type(reference) is not date:
            _invalid()
        frozen_at = _instant(acquired_at)
        from tradingagents.strategies.data_sources import congress_disclosure_audit as audit
        bound = {key: payload[key] for key in ('audit_snapshot', 'coverage')}
        if 'house_originals' in payload:
            bound['house_originals'] = payload['house_originals']
        snapshot = audit.validate_audit_snapshot(bound,
            date_filed_after=(reference - timedelta(days=30)).isoformat(),
            date_filed_before=reference.isoformat(), now=frozen_at)
        if payload['coverage']['complete'] is True:
            return None
        reconciliation = snapshot['scope_evidence']['house_reconciliation']
        missing, additional = reconciliation['missing_ids'], reconciliation['additional_ids']
        if not missing or additional:
            return None
        if (set(payload) - {'house_originals'} != {'audit_snapshot', 'coverage', '_request_diagnostics', 'error', '_coverage'}
                or payload['error'] != _ERROR
                or payload['_coverage'] != {'status': 'partial', 'reason_code': 'invalid_response',
                    'http_status': None, 'attempts': 0}
                or type(payload['_coverage'].get('attempts')) is not int):
            _invalid()
        coverage = payload['coverage']
        if (any(coverage[key] is not True for key in ('acquisition_complete', 'integrity_valid'))
                or any(coverage[key] is not False for key in
                    ('complete', 'house_ids_reconciled', 'stock_disclosure_complete'))
                or snapshot['signals_enabled'] is not False or snapshot['model_context_enabled'] is not False):
            _invalid()
        diagnostics = payload['_request_diagnostics']
        count = len(snapshot['raw_artifacts'])
        if type(diagnostics) is not list or len(diagnostics) < count:
            _invalid()
        if 'house_originals' in payload:
            from tradingagents.strategies.data_sources.congress_house_originals import validate_original_diagnostics
            validate_original_diagnostics(diagnostics[count:], payload['house_originals'])
        elif len(diagnostics) != count:
            _invalid()
        for row in diagnostics[:count]:
            # Native request_policy records the final successful operation once;
            # earlier transient attempts are represented by its recovered bit.
            # Five is that policy's hard retry bound, not a new retry allowance.
            if (type(row) is not dict or set(row) !=
                    {'provider', 'operation', 'reason_code', 'http_status', 'attempts', 'recovered'}
                    or row['provider'] != 'congress' or row['operation'] != 'disclosure_audit'
                    or row['reason_code'] is not None or row['http_status'] is not None
                    or type(row['attempts']) is not int or not 1 <= row['attempts'] <= 5
                    or row['recovered'] is not (row['attempts'] > 1)):
                _invalid()
        publisher = snapshot['publisher']
        from .source_inputs import SourceInputStore, source_codec_limits
        encoded = SourceInputStore.encode(payload,
            **source_codec_limits({'autoresearch': config}, source='congress'))
        return {'policy': POLICY, 'source': 'congress', 'reason_code': 'house_filing_audit_gap',
            'source_payload_sha256': hashlib.sha256(encoded.encode()).hexdigest(),
            'content_sha256': snapshot['content_sha256'],
            'house_missing_filings': len(missing), 'house_missing_ids': list(missing),
            'house_additional_filings': len(additional), 'house_matched_filings': reconciliation['matched_count'],
            'publisher_revision': publisher['revision'], 'publisher_generated_at': publisher['generated_at'],
            'publisher_last_source_check_at': publisher['last_source_check_at'],
            'publisher_age_seconds': (frozen_at - _instant(publisher['generated_at'])).total_seconds(),
            'publisher_last_check_age_seconds': (frozen_at - _instant(publisher['last_source_check_at'])).total_seconds(),
            'snapshot_acquired_at': snapshot['acquired_at'],
            'snapshot_age_seconds': (frozen_at - _instant(snapshot['acquired_at'])).total_seconds(),
            'envelope_acquired_at': frozen_at.isoformat(), 'freshness_limit_seconds': audit.MAX_AGE_SECONDS,
            'research_coverage_complete': False, 'signals_enabled': False,
            'model_context_enabled': False, 'order_authority': False}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        _invalid()
