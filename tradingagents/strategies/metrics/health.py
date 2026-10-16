from __future__ import annotations

from datetime import date
from typing import Collection, Mapping

from .identity import _stable_id
from .models import StrategyHealthRecord


def classify_strategy_run(
    *,
    epoch_id: str,
    session: date,
    policy_id: str,
    strategy: str,
    data_sources: Collection[str],
    candidates: Collection[object],
    provider_errors: Mapping[str, str],
    exception: Exception | None,
    admission_manifest: dict | None = None,
) -> StrategyHealthRecord:
    """Classify one completed strategy screen with durable diagnostic evidence."""
    evidence: dict[str, object] = {
        "data_sources": sorted(data_sources),
        "candidate_count": len(candidates),
    }
    manifest = admission_manifest if admission_manifest is not None else getattr(candidates, "admission_manifest", None)
    if manifest is not None:
        from copy import deepcopy
        evidence["admission_manifest"] = deepcopy(manifest)
        evidence.update(discovered_count=len(manifest["discovered"]),
                        admitted_count=len(manifest["admitted"]),
                        excluded_count=len(manifest["excluded"]))
    if exception is not None:
        status = "strategy_defect"
        evidence.update(error_type=type(exception).__name__, error=str(exception))
    elif provider_errors:
        status = "data_failure"
        evidence["provider_errors"] = dict(sorted(provider_errors.items()))
    elif candidates:
        status = "signals"
    else:
        status = "legitimate_no_event"
        evidence["screen_completed"] = True

    return StrategyHealthRecord(
        health_id=_stable_id("health", epoch_id, session, policy_id, strategy),
        epoch_id=epoch_id,
        session=session,
        policy_id=policy_id,
        strategy=strategy,
        status=status,
        signal_count=len(candidates),
        evidence=evidence,
    )
