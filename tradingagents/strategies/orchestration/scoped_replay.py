"""Revalidate active scoped sources while preserving proved original provider failures."""

from __future__ import annotations

from datetime import date, datetime

from .scoped_sources import AWARD_POLICY, source_scope_evidence, validate_portfolio_targets


def validate_replay_source_scopes(
    data: dict, owner, session: str, epoch: str, *, now: datetime
) -> None:
    """Validate active scoped sources (USASpending) during replay without mutating history."""
    config = owner._base_config.get("autoresearch", {})
    validate_portfolio_targets(data, owner, session)
    errors, _, _ = source_scope_evidence(data, config, session, now=now)
    if not errors:
        return

    policy_ids = {
        owner._policy_id_for_horizon(row["config"].horizon) for row in owner.cohorts
    }
    records = owner._metric_store.read_strategy_health(
        epoch, session=date.fromisoformat(session), limit=1000
    )
    by_identity = {}
    for record in records:
        key = (record.policy_id, record.strategy)
        if (
            record.epoch_id != epoch
            or record.session.isoformat() != session
            or key in by_identity
        ):
            raise ValueError("completed replay source health identity invalid")
        by_identity[key] = record

    disabled = {
        **getattr(owner, "_disabled_strategies", {}),
        **config.get("disabled_strategies", {}),
    }
    strategies = {
        strategy.name: strategy
        for strategy in owner.cohorts[0]["engine"].paper_trade_strategies
        if strategy.name not in disabled
        and not getattr(strategy, "retirement_reason", None)
    }
    for provider in errors:
        if provider != "usaspending":
            continue
        payload = data.get(provider)
        if (
            config.get("award_attribution_policy") != AWARD_POLICY
            or not isinstance(payload, dict)
            or payload.get("award_attribution_policy") not in (None, AWARD_POLICY)
            or not isinstance(payload.get("error"), str)
            or not payload["error"].strip()
        ):
            raise ValueError("completed replay source scope evidence invalid")
        dependents = {
            name for name, strategy in strategies.items() if provider in strategy.data_sources
        }
        if not policy_ids:
            raise ValueError("completed replay source failure has no dependent health")
        for policy_id in policy_ids:
            for strategy in dependents:
                record = by_identity.get((policy_id, strategy))
                provider_errors = (
                    record.evidence.get("provider_errors", {})
                    if record is not None
                    else {}
                )
                if (
                    record is None
                    or record.status not in {"data_failure", "unavailable"}
                    or not isinstance(provider_errors, dict)
                    or not isinstance(provider_errors.get(provider), str)
                    or not provider_errors[provider].strip()
                ):
                    raise ValueError(
                        "completed replay source failure lacks durable matching health"
                    )
