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


ALLOWED_MODEL_KEYS = frozenset(
    {
        "llm_provider",
        "deep_think_llm",
        "quick_think_llm",
        "cache_model",
        "live_model",
        "strategist_model",
        "cro_model",
        "autoresearch_model",
    }
)

_EXECUTION_POLICY_KEYS = frozenset(
    {
        "policy_document_version",
        "execution",
        "schema_version",
        "pricing_contract",
        "execution_clock_contract",
        "cost_model_contract",
        "calendar",
        "bar_max_age_hours",
        "benchmark_symbols",
        "cost_model",
        "risk_gate",
        "short_selling",
    }
)
_NESTED_POLICY_KEYS = {
    "execution": frozenset({"mode", "price_rules"}),
    "calendar": frozenset({"name", "provider", "provider_version"}),
    "cost_model": frozenset(
        {
            "slippage_bps",
            "commission_per_fill",
            "other_fee_per_fill",
            "margin_requirement",
            "margin_financing_rate",
            "idle_cash_yield_rate",
            "existing_short_missing_borrow_rate",
        }
    ),
    "risk_gate": frozenset(
        {
            "total_capital",
            "max_positions",
            "max_position_pct",
            "min_position_value",
            "daily_loss_limit_pct",
            "max_drawdown_pct",
            "per_strategy_max",
            "global_stop_loss_pct",
            "long_only",
            "cash_reserve_pct",
            "reentry_cooldown_days",
            "earnings_blackout_days",
            "max_borrow_cost_pct",
            "max_margin_utilization_pct",
            "short_squeeze_stop_pct",
            "short_squeeze_window_days",
            "premium_decay_floor_pct",
        }
    ),
    "short_selling": frozenset({"borrow_cost_reject_above"}),
}


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


def _validate_execution_policy_schema(policy: object) -> None:
    if not isinstance(policy, dict):
        raise TypeError("execution_policy must be a dict")
    actual = set(policy)
    unexpected = actual - _EXECUTION_POLICY_KEYS
    if unexpected:
        raise ValueError(
            f"unexpected execution_policy key {sorted(unexpected)[0]!r}"
        )
    missing = _EXECUTION_POLICY_KEYS - actual
    if missing:
        raise ValueError(
            f"execution_policy key set is missing {sorted(missing)[0]!r}"
        )
    for container, allowed in _NESTED_POLICY_KEYS.items():
        nested = policy[container]
        if not isinstance(nested, dict):
            raise TypeError(f"execution_policy.{container} must be a dict")
        actual = set(nested)
        unexpected = actual - allowed
        if unexpected:
            raise ValueError(
                f"unexpected execution_policy.{container} key "
                f"{sorted(unexpected)[0]!r}"
            )
        missing = allowed - actual
        if missing:
            raise ValueError(
                f"execution_policy.{container} key set is missing "
                f"{sorted(missing)[0]!r}"
            )
        for key, value in nested.items():
            if container == "execution" and key == "price_rules":
                if not isinstance(value, list) or any(
                    not isinstance(item, str) for item in value
                ):
                    raise TypeError(
                        "execution_policy.execution.price_rules must be a string list"
                    )
                continue
            if not (value is None or isinstance(value, (str, int, bool))):
                raise TypeError(
                    f"execution_policy.{container}.{key} must be a canonical scalar"
                )
    benchmarks = policy.get("benchmark_symbols")
    if benchmarks is not None and (
        not isinstance(benchmarks, list)
        or any(not isinstance(item, str) for item in benchmarks)
    ):
        raise TypeError("execution_policy.benchmark_symbols must be a string list")
    nested_keys = {*_NESTED_POLICY_KEYS, "benchmark_symbols"}
    for key, value in policy.items():
        if key in nested_keys:
            continue
        if not (value is None or isinstance(value, (str, int, bool))):
            raise TypeError(f"execution_policy.{key} must be a canonical scalar")
    _validate_execution_policy(policy)


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
    actual_model_keys = set(models)
    unexpected_model_keys = actual_model_keys - ALLOWED_MODEL_KEYS
    if unexpected_model_keys:
        raise ValueError(
            f"unexpected model key {sorted(unexpected_model_keys)[0]!r}"
        )
    missing_model_keys = ALLOWED_MODEL_KEYS - actual_model_keys
    if missing_model_keys:
        raise ValueError(
            f"model key set is missing {sorted(missing_model_keys)[0]!r}"
        )
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
        _validate_execution_policy_schema(policy.execution_policy)
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
