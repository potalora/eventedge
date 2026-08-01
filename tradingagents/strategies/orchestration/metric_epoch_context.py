"""Build the allowlisted semantic identity for one generation's metric epoch."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping

from tradingagents.strategies.execution.contracts import (
    COST_MODEL_VERSION,
    EXECUTION_CLOCK_VERSION,
    PRICING_VERSION,
)
from tradingagents.strategies.execution.ids import stable_id
from tradingagents.strategies.metrics.epochs import EpochContext


@dataclass(frozen=True)
class CohortSemanticPolicy:
    name: str
    horizon: str
    size_profile: str
    policy_id: str
    use_llm: bool
    learning_enabled: bool
    execution_policy: dict[str, object]


def _required_text(label: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _validate_execution_policy(value: object, path: str = "execution_policy") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} keys must be strings")
            _validate_execution_policy(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_execution_policy(item, f"{path}[{index}]")
        return
    if value is None or isinstance(value, (str, int, bool)):
        return
    raise TypeError(
        f"{path} contains unsupported {type(value).__name__}"
    )


def build_epoch_context(
    *,
    generation_id: str,
    generation_commit: str,
    models: Mapping[str, str | None],
    strategies: tuple[str, ...],
    cohort_policies: tuple[CohortSemanticPolicy, ...],
) -> EpochContext:
    """Return a deterministic context from explicit, secret-free semantics."""
    generation_id = _required_text("generation_id", generation_id)
    generation_commit = _required_text("generation_commit", generation_commit)
    model_document = {
        _required_text("model key", key): (
            _required_text(f"model {key}", value) if value is not None else None
        )
        for key, value in sorted(models.items())
    }
    strategy_document = tuple(
        _required_text("strategy", strategy) for strategy in strategies
    )
    if len(set(strategy_document)) != len(strategy_document):
        raise ValueError("duplicate strategy name")

    names: list[str] = []
    for policy in cohort_policies:
        names.append(_required_text("cohort name", policy.name))
        _required_text("cohort horizon", policy.horizon)
        _required_text("cohort size_profile", policy.size_profile)
        _required_text("cohort policy_id", policy.policy_id)
        if not isinstance(policy.use_llm, bool):
            raise ValueError("cohort use_llm must be boolean")
        if not isinstance(policy.learning_enabled, bool):
            raise ValueError("cohort learning_enabled must be boolean")
        _validate_execution_policy(policy.execution_policy)
    if len(set(names)) != len(names):
        raise ValueError("duplicate cohort name")

    sorted_policies = tuple(sorted(cohort_policies, key=lambda row: row.name))
    policy_document = [asdict(policy) for policy in sorted_policies]
    behavior_hash = stable_id(
        "metric_behavior",
        generation_commit,
        model_document,
        tuple(sorted(strategy_document)),
        tuple((row.name, row.use_llm) for row in sorted_policies),
    )
    config_hash = stable_id("metric_configuration", policy_document)
    return EpochContext(
        generation_id=generation_id,
        generation_commit=generation_commit,
        behavior_hash=behavior_hash,
        config_hash=config_hash,
        execution_clock_version=EXECUTION_CLOCK_VERSION,
        pricing_version=PRICING_VERSION,
        cost_model_version=COST_MODEL_VERSION,
    )
