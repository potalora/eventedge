"""Clean generation operating contract backed by ``GenerationConfig``.

Replaces legacy hardcoded 20-literal stabilization dict validation and manifest
amendment DSLs with direct ``GenerationConfig`` validation.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from tradingagents.strategies.config import (
    ACTIVE_STRATEGIES,
    ConfigValidationError,
    GenerationConfig,
)
from tradingagents.strategies.orchestration._io import (
    CANONICAL_COHORTS,
    COHORT_HORIZONS,
    SOURCE_TAXONOMY,
)
from tradingagents.strategies.orchestration.generation_config import (
    _merge,
    is_typed_generation_payload,
)

POLICY = "gen003_clean_v1"
HORIZONS = COHORT_HORIZONS
ROSTER = tuple(sorted(CANONICAL_COHORTS))
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class OperatingProfile:
    version: str
    profile_sha256: str
    roster: tuple[str, ...]
    horizon_policies: dict[str, str]
    strategies: tuple[str, ...]
    supported_strategies: tuple[str, ...]
    declared_exclusions: dict[str, str]
    exclusion_scope_sha256: str
    config: dict[str, Any]
    ledger_policy_id: str
    generation_config: GenerationConfig | None = None


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def configured(config: Mapping[str, Any] | GenerationConfig) -> bool:
    """Return True if ``config`` declares a validated generation operating profile."""
    if isinstance(config, GenerationConfig):
        return True
    if not isinstance(config, Mapping):
        return False
    if isinstance(config.get("_generation_config"), GenerationConfig):
        return True
    if is_typed_generation_payload(config):
        return True
    policy = config.get("autoresearch", {}).get("stabilization_profile_policy")
    return policy in {POLICY, "supported_sixteen_books_v1"}


def profile_contract(
    frozen_config: Mapping[str, Any] | GenerationConfig, *, profile_sha256: str
) -> OperatingProfile:
    """Validate a generation profile via ``GenerationConfig`` and bind its digest."""
    from tradingagents.default_config import DEFAULT_CONFIG

    if type(profile_sha256) is not str or not _SHA256_RE.fullmatch(profile_sha256):
        raise ValueError("invalid_generation_profile")

    try:
        if isinstance(frozen_config, GenerationConfig):
            gen_cfg = frozen_config
            runtime_cfg = gen_cfg.to_runtime_dict()
        elif isinstance(frozen_config, Mapping) and is_typed_generation_payload(frozen_config):
            gen_cfg = GenerationConfig.from_dict(frozen_config)
            runtime_cfg = gen_cfg.to_runtime_dict()
        elif isinstance(frozen_config, Mapping):
            gen_cfg = (
                frozen_config["_generation_config"]
                if isinstance(frozen_config.get("_generation_config"), GenerationConfig)
                else GenerationConfig()
            )
            runtime_cfg = deepcopy(DEFAULT_CONFIG)
            _merge(runtime_cfg, frozen_config)
            if runtime_cfg.get("execution", {}).get("mode") != "paper":
                raise ConfigValidationError("execution.mode must be 'paper'")
        else:
            raise ValueError("invalid_generation_profile")
    except (ConfigValidationError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"invalid_generation_profile: {exc}") from exc

    ar = runtime_cfg.setdefault("autoresearch", {})
    ar.setdefault("decision_clock_policy", "prospective_next_open_v1")
    ar.setdefault("research_resilience_policy", "dependency_isolation_v1")
    ar.setdefault("stabilization_profile_policy", POLICY)
    if "govt_contracts" in gen_cfg.enabled_strategies and not (
        ar.get("disabled_strategies") or {}
    ).get("govt_contracts"):
        ar.setdefault("award_attribution_policy", "verified_listed_targets_v1")
    ledger_cfg = ar.setdefault("paper_ledger", {})
    base_policy = ledger_cfg.get("policy_id")
    if isinstance(base_policy, str) and base_policy.endswith(f"generation-config-{profile_sha256}"):
        policy = base_policy
    else:
        policy = ((base_policy + "-") if base_policy else "") + f"generation-config-{profile_sha256}"
    ledger_cfg["policy_id"] = policy

    enabled = tuple(gen_cfg.enabled_strategies)
    return OperatingProfile(
        version=POLICY,
        profile_sha256=profile_sha256,
        roster=ROSTER,
        horizon_policies={h: f"{policy}:health:{h}" for h in HORIZONS},
        strategies=enabled,
        supported_strategies=enabled,
        declared_exclusions={},
        exclusion_scope_sha256=_digest({"enabled_strategies": list(enabled)}),
        config=runtime_cfg,
        ledger_policy_id=policy,
        generation_config=gen_cfg,
    )


def configure_strategies(
    strategies: Sequence[Any],
    config: Mapping[str, Any],
    *,
    full_config: Mapping[str, Any] | None = None,
) -> list[Any]:
    """Filter strategies to those enabled by ``GenerationConfig`` / config."""
    _ = full_config
    enabled = config.get("enabled_strategies")
    disabled = config.get("disabled_strategies") or {}
    allowed: set[str] | None = None
    if isinstance(enabled, (list, tuple, set, frozenset)) and enabled:
        allowed = {str(s) for s in enabled}
    elif isinstance(disabled, Mapping) and disabled:
        allowed = set(ACTIVE_STRATEGIES) - set(disabled.keys())
    result = []
    for strategy in strategies:
        name = getattr(strategy, "name", None)
        if allowed is not None and name is not None and name not in allowed:
            continue
        result.append(strategy)
    return result


def optional_source_failure(item: Mapping[str, Any], payload: Any = None, failed_required: Any = None) -> bool:
    """Return True if a source diagnostic belongs to Tier-2 research/enrichment."""
    _ = (payload, failed_required)
    source = str(item.get("source") or item.get("provider") or "").strip().lower()
    if not source:
        return False
    return SOURCE_TAXONOMY.is_tier2(source) or source in {"openbb", "yahoo_research", "yfinance"}


def source_support(
    config: Mapping[str, Any], payload: Mapping[str, Any]
) -> tuple[dict[str, Any], set[str]]:
    """Reconstruct admitted capabilities and required supported sources."""
    from ..modules import get_paper_trade_strategies
    from .research_resilience import project_sources, strategy_capability_holds
    from .source_inputs import source_codec_limits

    ar = config.get("autoresearch", {})
    capability_policy = ar.get("source_capability_policy")
    strategies = configure_strategies(get_paper_trade_strategies(), ar, full_config=config)
    names = set(payload) | {"fred", "yfinance"}
    if ar.get("equity_universe_policy"):
        names.add("equity_universe")
    names.update(source for strategy in strategies for source in strategy.data_sources)
    _, scope = project_sources(
        payload,
        names,
        codec_limits=source_codec_limits(config),
        capability_policy=capability_policy,
    )
    failed: set[str] = set()
    if capability_policy is not None:
        capabilities = scope.get("capabilities", {})
        usable = lambda name: capabilities.get(name, {}).get("status") == "usable"
        if not usable("fred.hy_spread") or not usable("fred.yield_curve"):
            failed.add("fred")
        if not usable("fred.vix") and not usable("yfinance.vix"):
            failed.update(("fred", "yfinance"))
    disabled = ar.get("disabled_strategies") or {}
    for strategy in strategies:
        if strategy.name in disabled:
            continue
        if capability_policy is not None:
            failed.update(
                name.split(".")[0]
                for name in strategy_capability_holds(scope, strategy.name, strategy.data_sources)
            )
        failed.update(
            source
            for source in strategy.data_sources
            if source not in {"fred", "yfinance", "openbb", "enrichment"}
            and scope.get("sources", {}).get(source, {}).get("status") != "usable"
        )
    if (
        ar.get("equity_universe_policy")
        and scope.get("sources", {}).get("equity_universe", {}).get("status") != "usable"
    ):
        failed.add("equity_universe")
    return scope, failed


def healthy_supported_record(row: Mapping[str, Any], data_sources: Sequence[str]) -> bool:
    """Keep healthy classifications consistent with the native health producer."""
    evidence = row.get("evidence", {})
    count = row.get("signal_count")
    return (
        type(evidence) is dict
        and evidence.get("data_sources") == sorted(data_sources)
        and type(count) is int
        and count >= 0
        and type(evidence.get("candidate_count")) is int
        and evidence["candidate_count"] == count
        and not evidence.get("provider_errors")
        and not evidence.get("error_type")
        and not evidence.get("error")
        and (
            row.get("status") == "signals"
            and count > 0
            or row.get("status") == "legitimate_no_event"
            and count == 0
            and evidence.get("screen_completed") is True
        )
    )


def evaluate_completion(profile: OperatingProfile, facts: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate canonical session completion against Tier-1 accounting invariants.

    Tier-2 strategy source unavailability never invalidates Tier-1 accounting.
    """
    report = facts.get("report", {}) if isinstance(facts, Mapping) else {}
    reasons: set[str] = set()

    def require(cond: bool, reason: str) -> None:
        if not cond:
            reasons.add(reason)

    generation = report.get("generation_id")
    session = report.get("session")
    commit = report.get("generation_commit")
    epoch = report.get("epoch_id")
    require(
        all(type(v) is str and bool(v) for v in (generation, session, commit, epoch)),
        "identity_invalid",
    )
    if report.get("profile_sha256") is not None:
        require(report.get("profile_sha256") == profile.profile_sha256, "profile_identity_invalid")
    require(report.get("outcome") in {"clean", "degraded"}, "run_incomplete")
    require(report.get("evidence_complete") is True and not report.get("diagnostics"), "evidence_incomplete")

    books = report.get("cohorts", {})
    require(type(books) is dict and tuple(sorted(books)) == profile.roster, "book_roster_invalid")
    require(
        bool(books)
        and all(type(row) is dict and row.get("accounting_valid") is True for row in books.values()),
        "accounting_invalid",
    )
    require(
        bool(books)
        and all(
            type(row) is dict
            and row.get("staging_complete") is True
            and row.get("staging_valid") is True
            for row in books.values()
        ),
        "staging_invalid",
    )
    vol = report.get("volatility")
    if isinstance(vol, dict) and "valid" in vol:
        require(vol.get("valid") is True, "volatility_invalid")

    complete = not reasons
    unresolved = report.get("sources", {}).get("unresolved", [])
    tier1_unresolved = [
        item for item in unresolved if isinstance(item, Mapping) and not optional_source_failure(item)
    ]
    return {
        "version": POLICY,
        "generation_id": generation,
        "generation_commit": commit,
        "profile_sha256": profile.profile_sha256,
        "session": session,
        "epoch_id": epoch,
        "supported_profile_complete": complete,
        "workflow_complete": complete,
        "continuity_eligible": complete and not tier1_unresolved,
        "reason_codes": sorted(reasons),
    }
