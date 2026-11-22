"""Validate declared source scopes without weakening required-source coverage."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError

from tradingagents.strategies.data_sources.award_attribution_policy import (
    POLICY as AWARD_POLICY, validate_attribution_scope,
)

COURT_POLICY = 'focused_litigation_v1'


def litigation_settings(config: dict) -> dict:
    settings = config.get('courtlistener_focused', {})
    if (config.get('courtlistener_scope_policy') != COURT_POLICY
            or not isinstance(settings, dict) or 'policy' in settings):
        raise ValueError('invalid focused Court configuration')
    return dict(settings, policy=COURT_POLICY)


def source_scope_evidence(data: dict, config: dict, session: str, *,
                          now: datetime | None = None) -> tuple[dict, dict, dict]:
    """Return source failures, report limitations, and recomputed scope proofs."""
    errors, limits, proofs = {}, {}, {}
    payload = data.get('usaspending', {})
    configured = config.get('award_attribution_policy')
    declared = payload.get('award_attribution_policy') if isinstance(payload, dict) else None
    if configured is not None or declared is not None:
        try:
            if configured != AWARD_POLICY or declared != configured:
                raise ValueError('award attribution policy mismatch')
            scope = validate_attribution_scope(payload, session=session)
            proofs['usaspending'] = scope
            limits['usaspending'] = {key: deepcopy(scope[key]) for key in (
                'policy', 'scope_sha256', 'counts', 'attribution_complete')}
            limits['usaspending']['coverage_basis'] = 'verified_listed_targets'
        except (ValueError, TypeError, KeyError, SourceFetchError):
            errors['usaspending'] = 'scoped_source_evidence_invalid'
    return errors, limits, proofs


def validate_portfolio_targets(data: dict, owner, session: str) -> None:
    """Retained no-op after retirement of focused litigation targets."""
    _ = (data, owner, session)


def litigation_context(data: dict, proof: dict) -> dict:
    """Expose query-associated metadata as context with no legal merits claim."""
    payload = data['courtlistener']
    return {'policy': COURT_POLICY, 'content_kind': 'docket_metadata_only',
        'scope_sha256': proof['manifest']['scope_sha256'],
        'target_manifest_sha256': proof['manifest']['manifest_sha256'],
        'marketwide_coverage': False, 'actionable_from_docket_metadata': False,
        'interpretation': 'Query matches only. Party roles, legal merits, materiality and price impact are unverified. Missing coverage is not evidence of no litigation.',
        'queries': [{key: deepcopy(row[key]) for key in
                     ('kind', 'query', 'tickers', 'issuer_ciks', 'docket_id', 'docket_ids') if key in row}
                    for row in payload['coverage']['queries']],
        'dockets': [{key: deepcopy(row[key]) for key in
                     ('docket_id', 'case_name', 'court', 'date_filed', 'nature_of_suit', 'cause')}
                    for row in payload['dockets']]}


def accepted_attribution_limitation(candidate, strategy: str, proofs: dict) -> bool:
    """Only exact non-actionable award dispositions avoid an analysis failure."""
    scope = proofs.get('usaspending')
    meta = candidate.metadata
    if (strategy != 'govt_contracts' or scope is None or not candidate.journal_only
            or candidate.ticker != '' or meta.get('source') != 'usaspending'
            or meta.get('award_attribution_policy') != AWARD_POLICY
            or meta.get('award_attribution_scope_sha256') != scope['scope_sha256']
            or meta.get('analysis_failure_reason')):
        return False
    rows = [row for row in scope['awards'] if row['award_key'] == meta.get('award_key')]
    return (len(rows) == 1 and rows[0]['status'] in {'unresolved', 'verified_no_listed_target'}
            and rows[0]['award_id'] == meta.get('award_id')
            and rows[0]['attribution'] == meta.get('issuer_attribution')
            and rows[0]['attribution']['reason'] == meta.get('non_actionable_reason'))
