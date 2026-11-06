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
    payload = data.get('courtlistener', {})
    configured = config.get('courtlistener_scope_policy')
    declared = payload.get('courtlistener_scope_policy') if isinstance(payload, dict) else None
    if configured is not None or declared is not None:
        try:
            from .litigation_targets import validate_litigation_targets
            from ..data_sources.courtlistener_scope import validate_focused_litigation
            if configured != COURT_POLICY or declared != configured:
                raise ValueError('Court scope policy mismatch')
            targets = data['_courtlistener_targets']
            evidence = validate_litigation_targets(
                {key: targets[key] for key in ('scope', 'manifest')},
                session=date.fromisoformat(session), settings=litigation_settings(config),
                company_map=targets['company_map'])
            validate_focused_litigation(payload, expected_scope=evidence['scope'],
                date_filed_after=(date.fromisoformat(session) - timedelta(days=14)).isoformat(),
                date_filed_before=session, now=now or datetime.now(timezone.utc),
                max_evidence_age_seconds=None)
            scope, manifest = evidence['scope'], evidence['manifest']
            proofs['courtlistener'] = evidence
            limits['courtlistener'] = {
                'policy': COURT_POLICY, 'scope_sha256': manifest['scope_sha256'],
                'target_manifest_sha256': manifest['manifest_sha256'],
                'coverage_basis': 'declared_issuer_and_case_queries',
                'content_kind': 'docket_metadata_only', 'issuer_count': len(scope['issuers']),
                'case_count': len(scope['case_ids']), 'docket_count': len(payload['dockets']),
                'omitted_issuer_count': len(manifest['omitted_issuers']) + len(manifest['omitted_shortlist']),
                'target_search_complete': manifest['target_search_complete'], 'marketwide_coverage': False}
        except (ValueError, TypeError, KeyError, SourceFetchError):
            errors['courtlistener'] = 'scoped_source_evidence_invalid'
    return errors, limits, proofs


def validate_portfolio_targets(data: dict, owner, session: str) -> None:
    """Bind the original target seed to this run, never to its later positions."""
    config = owner._base_config.get('autoresearch', {})
    if config.get('courtlistener_scope_policy') is None:
        return
    from .litigation_targets import validate_litigation_targets
    targets = data.get('_courtlistener_targets')
    if isinstance(targets, dict) and targets.get('error'):
        # A failed acquisition is retained for failed strategy-health evidence.
        if data.get('courtlistener', {}).get('error'):
            return
        raise ValueError('Court target failure lacks source failure')
    evidence = validate_litigation_targets(
        {key: targets[key] for key in ('scope', 'manifest')},
        session=date.fromisoformat(session), settings=litigation_settings(config),
        company_map=targets['company_map'], require_portfolio_scope=True)
    manifest = evidence['manifest']
    roster = sorted(({'name': row['config'].name,
                      'policy_id': owner._policy_id_for_horizon(row['config'].horizon)}
                     for row in owner.cohorts), key=lambda row: row['name'])
    if manifest['cohorts'] != roster or manifest['epoch_id'] != owner._epoch_id:
        raise ValueError('Court target portfolio scope mismatch')
    acquisition = data.get('_decision_acquisition')
    if acquisition is not None and manifest['cutoff'] != acquisition.get('started_at'):
        raise ValueError('Court target acquisition cutoff mismatch')


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
