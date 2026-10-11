"""Revalidate scoped sources while preserving proved original provider failures."""
from __future__ import annotations

from datetime import date, datetime

from .scoped_sources import (
    AWARD_POLICY, COURT_POLICY, source_scope_evidence, validate_portfolio_targets,
)


def _validate_congress_health(by_identity, policy_ids, expected_scope):
    from .congress_policy import POLICY, STRATEGY
    from .source_coverage import canonical_source_scope_limits
    from tradingagents.strategies.modules.congressional_trades import CongressionalTradesStrategy
    if not policy_ids:
        raise ValueError('completed replay Congress audit has no configured horizon')
    expected = {'congress': expected_scope} if expected_scope is not None else {}
    for policy_id in policy_ids:
        record = by_identity.get((policy_id, STRATEGY))
        evidence = record.evidence if record is not None else None
        if (record is None or record.status != 'disabled_by_policy'
                or type(record.signal_count) is not int or record.signal_count != 0
                or not isinstance(evidence, dict)
                or evidence.get('reason') != POLICY or evidence.get('disclosure_policy') != POLICY
                or type(evidence.get('candidate_count')) is not int or evidence['candidate_count'] != 0
                or evidence.get('data_sources') != sorted(CongressionalTradesStrategy.data_sources)):
            raise ValueError('completed replay Congress audit lacks matching disabled health')
        try:
            scoped = canonical_source_scope_limits(evidence.get('source_scope_limits', {}))
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError('completed replay Congress audit health scope invalid') from error
        if scoped != expected:
            raise ValueError('completed replay Congress audit health differs from frozen evidence')


def validate_replay_source_scopes(data: dict, owner, session: str, epoch: str, *, now: datetime) -> None:
    """Called only after the frozen source digest matches the decision binding.

Successful payloads must retain their full scope proof. A retained acquisition
failure remains a failure, backed by every dependent strategy's original health
record; it does not invalidate unrelated, already completed execution.
    """
    config = owner._base_config.get('autoresearch', {})
    from .filing_policy_validation import validate_filing_comparison_policy
    validate_filing_comparison_policy(data, config)
    from .filing_attribution_validation import (
        validate_filing_attribution_policy, validate_filing_attribution_health, filing_source_error_sha256,
    )
    from tradingagents.strategies.data_sources.filing_attribution_policy import configured as filing_attribution_configured
    filing_attribution_scope = validate_filing_attribution_policy(data, config)
    filing_attribution_enabled = filing_attribution_configured(config)
    from .filing_acquisition_validation import configured as acquisition_configured, validate_filing_acquisition_policy
    validate_filing_acquisition_policy(data, config)
    acquisition_enabled = acquisition_configured(config)
    from .congress_policy import audit_scope, configured
    congress_scope = audit_scope(data, config, session, now=now, replay=True)
    congress_enabled = configured(config)
    validate_portfolio_targets(data, owner, session)
    errors, _, _ = source_scope_evidence(data, config, session, now=now)
    if not errors and not congress_enabled and not filing_attribution_enabled and not acquisition_enabled:
        return
    policy_ids = {owner._policy_id_for_horizon(row['config'].horizon) for row in owner.cohorts}
    records = owner._metric_store.read_strategy_health(epoch, session=date.fromisoformat(session), limit=1000)
    by_identity = {}
    for record in records:
        key = (record.policy_id, record.strategy)
        if record.epoch_id != epoch or record.session.isoformat() != session or key in by_identity:
            raise ValueError('completed replay source health identity invalid')
        by_identity[key] = record
    if congress_enabled:
        _validate_congress_health(by_identity, policy_ids, congress_scope)
    if filing_attribution_enabled or acquisition_enabled:
        disabled = {**getattr(owner, '_disabled_strategies', {}), **config.get('disabled_strategies', {})}
        strategy_sources = {strategy.name: strategy.data_sources for strategy in owner.cohorts[0]['engine'].paper_trade_strategies
                          if 'edgar' in strategy.data_sources and strategy.name not in disabled
                          and not getattr(strategy, 'retirement_reason', None)}
        validate_filing_attribution_health(records, policy_ids, set(strategy_sources), filing_attribution_scope,
            expected_error_sha256=filing_source_error_sha256(data.get('edgar')),
            strategy_sources=strategy_sources)
    if not errors:
        return
    policies = {'courtlistener': ('courtlistener_scope_policy', COURT_POLICY),
                'usaspending': ('award_attribution_policy', AWARD_POLICY)}
    disabled = {**getattr(owner, '_disabled_strategies', {}), **config.get('disabled_strategies', {})}
    strategies = {strategy.name: strategy for strategy in owner.cohorts[0]['engine'].paper_trade_strategies
                  if strategy.name not in disabled and not getattr(strategy, 'retirement_reason', None)}
    for provider in errors:
        key, expected_policy = policies[provider]
        payload = data.get(provider)
        if (config.get(key) != expected_policy or not isinstance(payload, dict)
                or payload.get(key) not in (None, expected_policy)
                or not isinstance(payload.get('error'), str) or not payload['error'].strip()):
            raise ValueError('completed replay source scope evidence invalid')
        dependents = {name for name, strategy in strategies.items() if provider in strategy.data_sources}
        if not policy_ids:
            raise ValueError('completed replay source failure has no dependent health')
        for policy_id in policy_ids:
            for strategy in dependents:
                record = by_identity.get((policy_id, strategy))
                provider_errors = record.evidence.get('provider_errors', {}) if record is not None else {}
                if (record is None or record.status != 'data_failure'
                        or record.evidence.get('data_sources') != sorted(strategies[strategy].data_sources)
                        or not isinstance(provider_errors, dict)
                        or not isinstance(provider_errors.get(provider), str)
                        or not provider_errors[provider].strip()):
                    raise ValueError('completed replay source failure lacks durable matching health')
