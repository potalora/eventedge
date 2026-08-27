"""Pure, strict reporting helpers for daily cohort runs."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

from tradingagents.strategies.metrics.models import GOVERNED_BAR_RECOVERY_CONTRACT
from tradingagents.strategies.orchestration.run_outcome import RunOutcome
from tradingagents.strategies.orchestration.trading_calendar import (
    is_session,
    previous_session,
    session_close,
)

logger = logging.getLogger(__name__)

_MAX_GOVERNED_REPORT_ITEMS = 256
_MAX_GOVERNED_REPORT_COHORTS = 64
_MAX_GOVERNED_REPORT_TEXT = 4_096
_MAX_CANDIDATE_ISSUE_REFERENCES = 256
_MAX_CANDIDATE_ISSUE_COHORTS = 64
_MAX_CANDIDATE_ISSUE_TEXT = 256
_CANDIDATE_ISSUE_REFERENCE_KEYS = frozenset(
    {
        "issue_id",
        "epoch_id",
        "session",
        "dependency_kind",
        "reason_code",
        "ticker",
        "affected_cohorts",
    }
)
_CANDIDATE_DEPENDENCY_KINDS = frozenset({"reference_bar", "volatility_history"})
_CANDIDATE_REASON_CODES = frozenset(
    {"provider_error", "missing_data", "stale_data", "invalid_data"}
)
_GOVERNED_FAILURE_KINDS = frozenset(
    {"missing", "incoherent", "invalid", "invalid_benchmark"}
)
_MARKET_TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.^_-]{0,31}")
_COHORT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_CANDIDATE_ISSUE_ID_RE = re.compile(r"candidate_input_issue_[0-9a-f]{32}")
_EPOCH_ID_RE = re.compile(
    r"(?P<generation>[A-Za-z0-9][A-Za-z0-9_-]{0,127})-(?P<session>[0-9]{4}-[0-9]{2}-[0-9]{2})-[0-9a-f]{16}"
)


@dataclass
class DailyRunState:
    owner: Any
    trading_date: str
    session: date
    processed_at: datetime
    epoch_id: str | None = None
    results: dict[str, Any] = field(default_factory=dict)
    candidate_issue_references: list[dict[str, object]] = field(default_factory=list)
    candidate_issues_hydrated: bool = False
    candidate_bar_quarantine_suppressions: set[str] = field(default_factory=set)
    candidate_issue_reference_suppressions: set[str] = field(default_factory=set)
    governed_summaries_by_cohort: dict[str, list[dict[str, object]]] = field(
        default_factory=dict
    )
    completed: list[dict[str, Any]] = field(default_factory=list)
    stage_only: list[dict[str, Any]] = field(default_factory=list)
    execution_needed: list[dict[str, Any]] = field(default_factory=list)
    valid: list[dict[str, Any]] = field(default_factory=list)
    fresh: list[dict[str, Any]] = field(default_factory=list)
    stored: list[dict[str, Any]] = field(default_factory=list)
    persisted: dict[str, Any] = field(default_factory=dict)
    persisted_borrow: dict[str, dict[str, Decimal | None]] = field(
        default_factory=dict
    )
    cohort_scopes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    bundle: Any = None
    first_engine: Any = None
    shared_data: dict[str, Any] = field(default_factory=dict)
    horizon_signals: dict[str, tuple[list[dict], dict, list[Any]]] = field(
        default_factory=dict
    )
    governed_reference_bars: dict[str, Any] = field(default_factory=dict)
    candidate_reference_bars: dict[str, Any] = field(default_factory=dict)
    issue_identity_scope: tuple[dict[str, str], ...] = ()
    session_candidate_quarantines: list[str] = field(default_factory=list)
    existing_quarantines: list[str] = field(default_factory=list)
    quarantined_tickers: set[str] = field(default_factory=set)
    volatility_quarantines: set[str] = field(default_factory=set)
    candidate_bar_quarantines: list[str] = field(default_factory=list)
    shared_volatility_evidence: dict[str, Any] | None = None

    def finalize(self, finalized: dict[str, Any] | None = None) -> dict[str, Any]:
        return finalize_daily_results(
            self, self.results if finalized is None else finalized
        )

    def critical_gap(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.finalize(
            self.owner._stop_for_critical_market_data_gap(*args, **kwargs)
        )

    def fail_candidates(
        self,
        reason: str,
        *,
        degraded: bool = False,
        quarantines: object = None,
    ) -> dict[str, Any]:
        assign_failures(
            self.results,
            self.valid,
            reason,
            degraded=degraded,
            execution_valid=True,
            staging_valid=False,
            candidate_bar_quarantines=(
                self.existing_quarantines if quarantines is None else quarantines
            ),
        )
        return self.finalize()

    def fail_candidate_classification(
        self, conflicts: list[str]
    ) -> dict[str, Any]:
        governed = set(self.governed_reference_bars)
        self.candidate_bar_quarantine_suppressions.update(governed)
        safe = sorted(
            ticker
            for ticker in set(self.existing_quarantines) | self.quarantined_tickers
            if ticker not in governed
        )
        for result in self.results.values():
            if isinstance(result, dict):
                result["candidate_bar_quarantines"] = [
                    ticker
                    for ticker in result.get("candidate_bar_quarantines", ())
                    if ticker not in governed
                ]
        assign_failures(
            self.results,
            self.valid,
            _candidate_classification_conflict_reason(conflicts),
            degraded=bool(safe),
            execution_valid=True,
            staging_valid=False,
            candidate_bar_quarantines=safe,
        )
        return self.finalize()


def failure_result(reason: str, **optional: object) -> dict[str, Any]:
    """Build a failure while preserving omitted legacy fields."""
    return {"error": True, "invalid_reason": reason, **optional}


def assign_failures(
    results: dict[str, Any],
    cohorts_or_names: Iterable[object],
    reason: str,
    **optional: object,
) -> None:
    """Assign one canonical failure shape to cohorts or explicit names."""
    for item in cohorts_or_names:
        name = item if isinstance(item, str) else item["config"].name
        results[name] = failure_result(reason, **optional)


def filter_horizon_signals(
    horizon_signals: dict[str, tuple[list[dict], dict, list[Any]]],
    excluded_tickers: set[str],
) -> dict[str, tuple[list[dict], dict, list[Any]]]:
    """Remove excluded tickers while preserving each horizon's context."""
    return {
        horizon: (
            [
                signal
                for signal in signals
                if str(signal.get("ticker", "")).strip().upper()
                not in excluded_tickers
            ],
            regime,
            health,
        )
        for horizon, (signals, regime, health) in horizon_signals.items()
    }


def finalize_daily_results(
    state: DailyRunState,
    finalized: dict[str, Any],
) -> dict[str, Any]:
    """Apply the daily run's canonical result finalization semantics."""
    if not state.candidate_issues_hydrated:
        by_id = {
            str(reference["issue_id"]): reference
            for reference in state.candidate_issue_references
            if reference["issue_id"]
            not in state.candidate_issue_reference_suppressions
        }
        if state.epoch_id is not None:
            issues = state.owner._metric_store.read_candidate_input_issues(
                state.epoch_id, state.session
            )
            for issue in issues:
                if issue.issue_id in state.candidate_issue_reference_suppressions:
                    continue
                issue.validate_integrity()
                if issue.epoch_id != state.epoch_id or issue.session != state.session:
                    raise ValueError("candidate input issue durable scope is invalid")
                reference = issue.reference()
                issue_id = str(reference["issue_id"])
                if issue_id not in by_id:
                    by_id[issue_id] = reference
        state.candidate_issue_references[:] = sorted(
            by_id.values(),
            key=lambda reference: (
                str(reference["session"]),
                str(reference["dependency_kind"]),
                str(reference["ticker"]),
                str(reference["issue_id"]),
            ),
        )
        state.candidate_issues_hydrated = True
    for name, summaries in state.governed_summaries_by_cohort.items():
        result = finalized.get(name)
        if not summaries or not isinstance(result, dict):
            continue
        result["degraded"] = True
        result.setdefault("execution_valid", not bool(result.get("error")))
        result.setdefault("staging_valid", False)
        result.setdefault("candidate_bar_quarantines", [])
        result["governed_bar_recoveries"] = summaries
        result.setdefault("governed_failure_map", {})
    for name, result in finalized.items():
        if not isinstance(result, dict):
            continue
        result.setdefault("degraded", False)
        result.setdefault("execution_valid", not bool(result.get("error")))
        result.setdefault(
            "staging_valid",
            not bool(result.get("error")) and not bool(result["degraded"]),
        )
        result.setdefault("candidate_bar_quarantines", [])
        references = [
            reference
            for reference in state.candidate_issue_references
            if name in reference["affected_cohorts"]
        ]
        if not references:
            continue
        result["candidate_input_issues"] = references
        result["degraded"] = True
        result["staging_valid"] = False
        result["candidate_bar_quarantines"] = sorted(
            {
                *result["candidate_bar_quarantines"],
                *(
                    str(reference["ticker"])
                    for reference in references
                    if reference["dependency_kind"] == "reference_bar"
                    and reference["ticker"]
                    not in state.candidate_bar_quarantine_suppressions
                ),
            }
            - state.candidate_bar_quarantine_suppressions
        )
    return finalized


def aggregate_candidate_input_issues(
    results: dict, trading_date: str | None = None
) -> list[dict[str, object]]:
    if not isinstance(results, dict) or len(results) > _MAX_CANDIDATE_ISSUE_COHORTS:
        raise ValueError("candidate input issue reference collection is invalid")
    if any(
        not isinstance(name, str)
        or _COHORT_ID_RE.fullmatch(name) is None
        or len(name) > _MAX_CANDIDATE_ISSUE_TEXT
        for name in results
    ):
        raise ValueError("candidate input issue reference cohort is invalid")
    cohort_names = sorted(results)
    valid_cohorts = set(cohort_names)
    normalized: dict[str, dict[str, object]] = {}
    observed_by_issue: dict[str, set[str]] = {}
    issue_id_by_scope: dict[tuple[str, str, str, str], str] = {}
    observed_epochs: set[str] = set()
    observed_sessions: set[str] = set()
    item_count = 0
    for cohort_name in cohort_names:
        result = results[cohort_name]
        if not isinstance(result, dict) or "candidate_input_issues" not in result:
            continue
        references = result["candidate_input_issues"]
        if (
            not isinstance(references, (list, tuple))
            or not references
            or result.get("degraded") is not True
            or result.get("staging_valid") is not False
        ):
            raise ValueError("candidate input issue reference collection is invalid")
        item_count += len(references)
        if item_count > _MAX_CANDIDATE_ISSUE_REFERENCES:
            raise ValueError("candidate input issue reference collection is invalid")
        for reference in references:
            if (
                not isinstance(reference, dict)
                or set(reference) != _CANDIDATE_ISSUE_REFERENCE_KEYS
            ):
                raise ValueError("candidate input issue reference shape is invalid")
            issue_id, epoch_id, session_text = (
                reference["issue_id"],
                reference["epoch_id"],
                reference["session"],
            )
            dependency_kind, reason_code, ticker, affected = (
                reference[k]
                for k in (
                    "dependency_kind",
                    "reason_code",
                    "ticker",
                    "affected_cohorts",
                )
            )
            if any(
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > _MAX_CANDIDATE_ISSUE_TEXT
                for value in (issue_id, epoch_id, session_text, ticker)
            ):
                raise ValueError("candidate input issue reference text is invalid")
            epoch_match = _EPOCH_ID_RE.fullmatch(epoch_id)
            if (
                _CANDIDATE_ISSUE_ID_RE.fullmatch(issue_id) is None
                or epoch_match is None
            ):
                raise ValueError("candidate input issue reference id is invalid")
            if (
                not isinstance(dependency_kind, str)
                or not isinstance(reason_code, str)
                or dependency_kind not in _CANDIDATE_DEPENDENCY_KINDS
                or reason_code not in _CANDIDATE_REASON_CODES
                or ticker != ticker.upper()
                or _MARKET_TICKER_RE.fullmatch(ticker) is None
                or not isinstance(affected, (list, tuple))
                or not affected
                or len(affected) > _MAX_CANDIDATE_ISSUE_COHORTS
            ):
                raise ValueError("candidate input issue reference value is invalid")
            try:
                parsed_session = date.fromisoformat(session_text)
                if (
                    parsed_session.isoformat() != session_text
                    or not is_session(parsed_session)
                    or (trading_date is not None and session_text != trading_date)
                ):
                    raise ValueError
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "candidate input issue reference session is invalid"
                ) from error
            try:
                epoch_start_text = epoch_match.group("session")
                epoch_start_session = date.fromisoformat(epoch_start_text)
                if (
                    epoch_start_session.isoformat() != epoch_start_text
                    or not is_session(epoch_start_session)
                    or epoch_start_session > parsed_session
                ):
                    raise ValueError
            except (TypeError, ValueError) as error:
                raise ValueError("candidate input issue reference id is invalid") from error
            affected_cohorts = list(affected)
            if (
                any(
                    not isinstance(name, str)
                    or len(name) > _MAX_CANDIDATE_ISSUE_TEXT
                    or _COHORT_ID_RE.fullmatch(name) is None
                    for name in affected_cohorts
                )
                or affected_cohorts != sorted(set(affected_cohorts))
                or not set(affected_cohorts) <= valid_cohorts
                or cohort_name not in affected_cohorts
            ):
                raise ValueError("candidate input issue reference cohorts are invalid")
            canonical = {
                "issue_id": issue_id,
                "epoch_id": epoch_id,
                "session": session_text,
                "dependency_kind": dependency_kind,
                "reason_code": reason_code,
                "ticker": ticker,
                "affected_cohorts": affected_cohorts,
            }
            existing = normalized.get(issue_id)
            if existing is not None and existing != canonical:
                raise ValueError("candidate input issue reference conflict")
            scope = (epoch_id, session_text, dependency_kind, ticker)
            existing_scope_id = issue_id_by_scope.get(scope)
            if existing_scope_id is not None and existing_scope_id != issue_id:
                raise ValueError("candidate input issue reference scope conflict")
            issue_id_by_scope[scope] = issue_id
            observed_epochs.add(epoch_id)
            observed_sessions.add(session_text)
            normalized[issue_id] = canonical
            observed_by_issue.setdefault(issue_id, set()).add(cohort_name)
    if any(
        observed_by_issue[issue_id] != set(reference["affected_cohorts"])
        for issue_id, reference in normalized.items()
    ):
        raise ValueError("candidate input issue reference coverage is invalid")
    if len(observed_epochs) > 1 or len(observed_sessions) > 1:
        raise ValueError("candidate input issue reference run scope is invalid")
    return sorted(
        normalized.values(),
        key=lambda reference: (
            str(reference["session"]),
            str(reference["dependency_kind"]),
            str(reference["ticker"]),
            str(reference["issue_id"]),
        ),
    )


def canonical_candidate_input_issue_summaries(
    value: object, trading_date: str
) -> list[dict[str, object]]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or len(value) > _MAX_CANDIDATE_ISSUE_REFERENCES
    ):
        raise ValueError("candidate input issue reference collection is invalid")
    cohort_names: set[str] = set()
    for reference in value:
        if not isinstance(reference, dict):
            raise ValueError("candidate input issue reference shape is invalid")
        affected = reference.get("affected_cohorts")
        if (
            not isinstance(affected, (list, tuple))
            or not affected
            or len(affected) > _MAX_CANDIDATE_ISSUE_COHORTS
        ):
            raise ValueError("candidate input issue reference cohorts are invalid")
        for cohort_name in affected:
            if not isinstance(cohort_name, str):
                raise ValueError("candidate input issue reference cohort is invalid")
            cohort_names.add(cohort_name)
            if len(cohort_names) > _MAX_CANDIDATE_ISSUE_COHORTS:
                raise ValueError(
                    "candidate input issue reference collection is invalid"
                )
    synthetic = {
        name: {"degraded": True, "staging_valid": False, "candidate_input_issues": []}
        for name in sorted(cohort_names)
    }
    for reference in value:
        for cohort_name in reference["affected_cohorts"]:
            synthetic[cohort_name]["candidate_input_issues"].append(reference)
    return aggregate_candidate_input_issues(synthetic, trading_date)


def aggregate_governed_reporting(
    results: dict,
) -> tuple[list[dict[str, object]], dict[str, str]]:
    expected_summary_keys = {
        "ticker",
        "session",
        "recovery_id",
        "contract_version",
        "evidence_digest",
        "affected_cohort_ids",
    }
    summaries: dict[tuple[str, str, str], dict[str, object]] = {}
    summary_conflicts: set[tuple[str, str, str]] = set()
    failures: dict[str, str] = {}
    failure_conflicts: set[str] = set()
    summary_budget = failure_budget = _MAX_GOVERNED_REPORT_ITEMS
    cohort_names = sorted(
        key for key in results if isinstance(key, str) and _COHORT_ID_RE.fullmatch(key)
    )
    valid_cohort_ids = set(cohort_names)
    for cohort_name in cohort_names[:_MAX_GOVERNED_REPORT_ITEMS]:
        result = results[cohort_name]
        if not isinstance(result, dict):
            continue
        raw_summaries = result.get("governed_bar_recoveries", ())
        if isinstance(raw_summaries, (list, tuple)):
            for raw in raw_summaries[:summary_budget]:
                summary_budget -= 1
                if not isinstance(raw, dict) or set(raw) != expected_summary_keys:
                    continue
                ticker, session_text, recovery_id, contract, digest, affected = (
                    raw.get(k)
                    for k in (
                        "ticker",
                        "session",
                        "recovery_id",
                        "contract_version",
                        "evidence_digest",
                        "affected_cohort_ids",
                    )
                )
                texts = (ticker, session_text, recovery_id, contract, digest)
                if (
                    any(
                        not isinstance(value, str)
                        or not value
                        or len(value) > _MAX_GOVERNED_REPORT_TEXT
                        for value in texts
                    )
                    or ticker != ticker.strip().upper()
                    or _MARKET_TICKER_RE.fullmatch(ticker) is None
                    or contract != GOVERNED_BAR_RECOVERY_CONTRACT
                    or not recovery_id.startswith("governed_bar_recovery:")
                    or len(recovery_id.removeprefix("governed_bar_recovery:")) != 64
                    or any(
                        c not in "0123456789abcdef"
                        for c in recovery_id.removeprefix("governed_bar_recovery:")
                    )
                    or not digest.startswith("sha256:")
                    or len(digest.removeprefix("sha256:")) != 64
                    or any(
                        c not in "0123456789abcdef"
                        for c in digest.removeprefix("sha256:")
                    )
                    or not isinstance(affected, (list, tuple))
                    or not affected
                    or len(affected) > _MAX_GOVERNED_REPORT_COHORTS
                ):
                    continue
                try:
                    parsed_session = date.fromisoformat(session_text)
                except ValueError:
                    continue
                affected_ids = list(affected)
                if (
                    parsed_session.isoformat() != session_text
                    or any(
                        not isinstance(v, str)
                        or not v.strip()
                        or len(v) > _MAX_GOVERNED_REPORT_TEXT
                        for v in affected_ids
                    )
                    or affected_ids != sorted(set(affected_ids))
                    or any(_COHORT_ID_RE.fullmatch(v) is None for v in affected_ids)
                    or not set(affected_ids) <= valid_cohort_ids
                ):
                    continue
                canonical = {
                    "ticker": ticker,
                    "session": session_text,
                    "recovery_id": recovery_id,
                    "contract_version": contract,
                    "evidence_digest": digest,
                    "affected_cohort_ids": affected_ids,
                }
                key = (ticker, session_text, recovery_id)
                existing = summaries.get(key)
                if existing is not None and existing != canonical:
                    summary_conflicts.add(key)
                else:
                    summaries[key] = canonical
        raw_failures = result.get("governed_failure_map", {})
        if not isinstance(raw_failures, dict):
            continue
        for ticker, failure in list(raw_failures.items())[:failure_budget]:
            failure_budget -= 1
            if (
                not isinstance(ticker, str)
                or not ticker
                or ticker != ticker.strip().upper()
                or _MARKET_TICKER_RE.fullmatch(ticker) is None
                or len(ticker) > _MAX_GOVERNED_REPORT_TEXT
                or not isinstance(failure, str)
                or len(failure) > _MAX_GOVERNED_REPORT_TEXT
            ):
                continue
            parts = failure.split(" ")
            scope = parts[1].split("/", 1) if len(parts) == 2 else ()
            if (
                len(parts) != 2
                or parts[0] not in _GOVERNED_FAILURE_KINDS
                or len(scope) != 2
                or scope[0] != ticker
            ):
                continue
            try:
                if date.fromisoformat(scope[1]).isoformat() != scope[1]:
                    continue
            except ValueError:
                continue
            existing = failures.get(ticker)
            if existing is not None and existing != failure:
                failure_conflicts.add(ticker)
            else:
                failures[ticker] = failure
    return (
        [summaries[key] for key in sorted(summaries) if key not in summary_conflicts],
        {
            ticker: failures[ticker]
            for ticker in sorted(failures)
            if ticker not in failure_conflicts
        },
    )


def count_failed_cohorts(results: dict) -> tuple[int, int, list[str]]:
    failed = sorted(
        name
        for name, result in results.items()
        if isinstance(result, dict)
        and (
            result.get("error")
            or result.get("valid") is False
            or bool(result.get("invalid_reason"))
        )
    )
    return len(failed), len(results), failed


def count_degraded_cohorts(results: dict) -> tuple[int, int, list[str]]:
    degraded = sorted(
        name
        for name, result in results.items()
        if isinstance(result, dict)
        and result.get("degraded")
        and result.get("execution_valid") is True
    )
    return len(degraded), len(results), degraded


def _candidate_signal_identity_pairs(
    signals: list[dict], ticker: str, session: date
) -> tuple[tuple[str, str], ...]:
    """Return the canonical, deterministic identities bound to candidate evidence."""
    from tradingagents.strategies.orchestration.event_identity import (
        ACTIVE_STRATEGY_NAMES,
        canonical_event_key,
    )

    pairs = sorted(
        {
            (
                (
                    str(signal.get("metadata", {}).get("event_key"))
                    if isinstance(signal.get("metadata"), dict)
                    and signal["metadata"].get("event_key")
                    and str(signal.get("strategy", "")).strip()
                    not in ACTIVE_STRATEGY_NAMES
                    else canonical_event_key(
                        str(signal.get("strategy", "")).strip(),
                        ticker,
                        (
                            signal["metadata"]
                            if isinstance(signal.get("metadata"), dict)
                            else {}
                        ),
                        session,
                    )
                ),
                str(signal.get("strategy", "")).strip(),
            )
            for signal in signals
            if str(signal.get("ticker", "")).strip().upper() == ticker
        }
    )
    if not pairs or any(not event_key or not strategy for event_key, strategy in pairs):
        raise ValueError(f"candidate recovery identity is incomplete for {ticker}")
    return tuple(pairs)


def _candidate_replay_conflict_reason(tickers: list[str]) -> str:
    """Build a clear bounded report reason for deterministic replay conflicts."""
    displayed = [ticker[:32] for ticker in sorted(tickers)[:10]]
    suffix = f" (+{len(tickers) - len(displayed)} more)" if len(tickers) > 10 else ""
    return (
        "deterministic candidate replay identity conflict: "
        + ", ".join(displayed)
        + suffix
    )


def _candidate_classification_conflict_reason(tickers: list[str]) -> str:
    """Build a bounded report reason for candidate/governed scope conflicts."""
    displayed = [ticker[:32] for ticker in sorted(tickers)[:10]]
    suffix = f" (+{len(tickers) - len(displayed)} more)" if len(tickers) > 10 else ""
    return "candidate/governed classification conflict: " + ", ".join(displayed) + suffix


def _candidate_signal_identity_scope(
    horizon_signals: dict[str, tuple[list[dict], dict, list[Any]]], session: date
) -> tuple[dict[str, str], ...]:
    """Return the canonical per-horizon identity universe for staging replay."""
    identities: list[dict[str, str]] = []
    for horizon, (signals, _regime, _health) in sorted(horizon_signals.items()):
        tickers = sorted(
            {
                str(signal.get("ticker", "")).strip().upper()
                for signal in signals
                if str(signal.get("ticker", "")).strip()
            }
        )
        for ticker in tickers:
            identities.extend(
                {
                    "horizon": horizon,
                    "ticker": ticker,
                    "event_key": event_key,
                    "strategy": strategy,
                }
                for event_key, strategy in _candidate_signal_identity_pairs(
                    signals, ticker, session
                )
            )
    return tuple(
        sorted(
            identities,
            key=lambda identity: (
                identity["horizon"],
                identity["ticker"],
                identity["event_key"],
                identity["strategy"],
            ),
        )
    )


def _candidate_issue_digest(value: object) -> str:
    """Return a deterministic SHA-256 boundary for candidate issue evidence."""

    def canonical(item: object) -> object:
        if isinstance(item, datetime):
            return item.isoformat()
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, dict):
            return {
                str(key): canonical(child)
                for key, child in sorted(item.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(item, (list, tuple)):
            return [canonical(child) for child in item]
        return item

    payload = json.dumps(
        canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _candidate_bar_recovery_id(
    *,
    epoch_id: str,
    session: date,
    ticker: str,
    outcome: str,
    attempts: tuple[dict[str, object], ...],
    signal_identities: tuple[dict[str, str], ...],
) -> str:
    """Bind compatibility recovery identity to its complete canonical evidence."""
    from tradingagents.strategies.execution.ids import stable_id

    return stable_id(
        "candidate_bar_recovery",
        {
            "epoch_id": epoch_id,
            "session": session,
            "ticker": ticker,
            "outcome": outcome,
            "attempts": attempts,
            "signal_identities": signal_identities,
        },
    )


def _candidate_reference_issue(
    record: Any,
    *,
    signal_identity_scope: tuple[dict[str, str], ...],
    cohorts: list[dict[str, Any]],
) -> Any:
    """Rebuild typed reference-bar issue evidence from compatibility recovery."""
    from tradingagents.strategies.execution.ids import stable_id
    from tradingagents.strategies.orchestration.candidate_inputs import (
        CandidateInputIssue,
    )

    identities = tuple(
        {
            "event_key": identity["event_key"],
            "strategy": identity["strategy"],
        }
        for identity in signal_identity_scope
        if identity["ticker"] == record.ticker
    )
    horizons = {
        identity["horizon"]
        for identity in signal_identity_scope
        if identity["ticker"] == record.ticker
    }
    affected_cohorts = tuple(
        cohort["config"].name
        for cohort in cohorts
        if cohort["config"].horizon in horizons
    )
    if not identities or not affected_cohorts:
        raise ValueError(
            f"candidate issue scope is incomplete for {record.ticker}"
        )
    final_attempt = record.attempts[-1]
    validation_error = str(final_attempt["validation_error"] or "")
    if validation_error.startswith("provider_error "):
        reason_code = "provider_error"
    elif validation_error.startswith("missing "):
        reason_code = "missing_data"
    elif validation_error.startswith(("stale ", "pre-close ")):
        reason_code = "stale_data"
    else:
        reason_code = "invalid_data"
    observed_sessions = tuple(
        record.session
        for attempt in record.attempts
        if any(attempt[field] is not None for field in ("open", "high", "low", "close"))
    )
    return CandidateInputIssue.create(
        issue_id=stable_id(
            "candidate_input_issue",
            record.epoch_id,
            record.session,
            "reference_bar",
            record.ticker,
        ),
        epoch_id=record.epoch_id,
        session=record.session,
        dependency_kind="reference_bar",
        reason_code=reason_code,
        ticker=record.ticker,
        source=str(final_attempt["source"]),
        fetched_at=final_attempt["fetched_at"],
        requested_history_digest=_candidate_issue_digest(
            {
                "dependency_kind": "reference_bar",
                "ticker": record.ticker,
                "expected_sessions": (record.session,),
            }
        ),
        returned_history_digest=_candidate_issue_digest(record.attempts),
        expected_sessions=(record.session,),
        observed_sessions=observed_sessions,
        retryable=False,
        affected_signal_identities=identities,
        affected_cohorts=affected_cohorts,
    )


def _candidate_volatility_scope(
    ticker: str,
    *,
    signal_identity_scope: tuple[dict[str, str], ...],
    cohorts: list[dict[str, Any]],
) -> tuple[tuple[dict[str, str], ...], tuple[str, ...]]:
    """Return the exact signal and cohort scope for one candidate ticker."""
    identity_pairs = {
        (identity["event_key"], identity["strategy"])
        for identity in signal_identity_scope
        if identity["ticker"] == ticker
    }
    identities = tuple(
        {"event_key": event_key, "strategy": strategy}
        for event_key, strategy in sorted(identity_pairs)
    )
    horizons = {
        identity["horizon"]
        for identity in signal_identity_scope
        if identity["ticker"] == ticker
    }
    affected_cohorts = tuple(
        sorted(
            {
                cohort["config"].name
                for cohort in cohorts
                if cohort["config"].horizon in horizons
            }
        )
    )
    if not identities or not affected_cohorts:
        raise ValueError(f"candidate issue scope is incomplete for {ticker}")
    return identities, affected_cohorts


def _volatility_request_digest(
    ticker: str, expected_sessions: tuple[date, ...]
) -> str:
    return _candidate_issue_digest(
        {
            "dependency_kind": "volatility_history",
            "ticker": ticker,
            "expected_sessions": expected_sessions,
        }
    )


def _candidate_volatility_issue_id(issue: Any) -> str:
    """Bind an issue identifier to its complete durable evidence payload."""
    from tradingagents.strategies.execution.ids import stable_id

    return stable_id("candidate_input_issue", issue.evidence_fields())


def _candidate_volatility_history_evidence(
    price_cache: dict[str, Any],
    ticker: str,
    expected_sessions: tuple[date, ...],
) -> tuple[str, tuple[date, ...], str]:
    """Describe invalid cached history without retaining provider text."""
    import pandas as pd

    from tradingagents.strategies.orchestration.trading_calendar import is_session

    matches = [
        frame
        for raw_ticker, frame in price_cache.items()
        if str(raw_ticker).strip().upper() == ticker
    ]
    if len(matches) != 1:
        status = "missing" if not matches else "conflicting"
        reason_code = "missing_data" if not matches else "invalid_data"
        return reason_code, (), _candidate_issue_digest({"status": status})
    frame = matches[0]
    if not isinstance(frame, pd.DataFrame):
        return "invalid_data", (), _candidate_issue_digest(
            {"status": "invalid_frame"}
        )
    if "Close" not in frame.columns or frame.empty:
        status = "empty" if frame.empty else "missing_close"
        return "missing_data", (), _candidate_issue_digest({"status": status})

    observed: list[date] = []
    invalid_session = False
    digest_sessions: list[str] = []
    for value in frame.index:
        try:
            if isinstance(value, datetime):
                normalized = value.date()
            elif isinstance(value, date):
                normalized = value
            elif isinstance(value, pd.Timestamp) and not pd.isna(value):
                normalized = value.date()
            else:
                raise ValueError("invalid session")
            digest_sessions.append(normalized.isoformat())
            if is_session(normalized):
                observed.append(normalized)
            else:
                invalid_session = True
        except (OverflowError, TypeError, ValueError):
            invalid_session = True
            digest_sessions.append(
                "invalid_session:"
                + hashlib.sha256(repr(value).encode("utf-8")).hexdigest()
            )

    digest_closes: list[str] = []
    invalid_close = False
    for value in frame["Close"]:
        try:
            normalized_value = float(value)
        except (TypeError, ValueError):
            invalid_close = True
            digest_closes.append(
                "invalid_value:"
                + hashlib.sha256(repr(value).encode("utf-8")).hexdigest()
            )
            continue
        if not math.isfinite(normalized_value) or normalized_value <= 0:
            invalid_close = True
            digest_closes.append(
                "invalid_value:"
                + hashlib.sha256(repr(value).encode("utf-8")).hexdigest()
            )
        else:
            digest_closes.append(format(normalized_value, ".17g"))
    returned_digest = _candidate_issue_digest(
        {
            "status": "history",
            "sessions": tuple(digest_sessions),
            "closes": tuple(digest_closes),
        }
    )
    observed_sessions = tuple(observed)
    if (
        invalid_session
        or invalid_close
        or len(set(observed_sessions)) != len(observed_sessions)
        or any(
            later <= earlier
            for earlier, later in zip(observed_sessions, observed_sessions[1:])
        )
    ):
        return "invalid_data", observed_sessions, returned_digest
    if len(observed_sessions) < len(expected_sessions):
        if observed_sessions and observed_sessions[-1] < expected_sessions[-1]:
            return "stale_data", observed_sessions, returned_digest
        return "missing_data", observed_sessions, returned_digest
    if observed_sessions[-len(expected_sessions) :] != expected_sessions:
        if observed_sessions[-1] < expected_sessions[-1]:
            return "stale_data", observed_sessions, returned_digest
        return "missing_data", observed_sessions, returned_digest
    return "invalid_data", observed_sessions, returned_digest


def _candidate_volatility_issue(
    *,
    epoch_id: str,
    session: date,
    ticker: str,
    fetched_at: datetime,
    expected_sessions: tuple[date, ...],
    price_cache: dict[str, Any],
    signal_identity_scope: tuple[dict[str, str], ...],
    cohorts: list[dict[str, Any]],
    reason_code: str | None = None,
) -> Any:
    """Create bounded generic evidence for an invalid candidate history."""
    from tradingagents.strategies.orchestration.candidate_inputs import (
        CandidateInputIssue,
    )

    identities, affected_cohorts = _candidate_volatility_scope(
        ticker,
        signal_identity_scope=signal_identity_scope,
        cohorts=cohorts,
    )
    observed_reason_code, observed_sessions, returned_digest = (
        _candidate_volatility_history_evidence(
            price_cache, ticker, expected_sessions
        )
    )
    provisional = CandidateInputIssue.create(
        issue_id="candidate-volatility-provisional",
        epoch_id=epoch_id,
        session=session,
        dependency_kind="volatility_history",
        reason_code=reason_code or observed_reason_code,
        ticker=ticker,
        source="yfinance",
        fetched_at=fetched_at,
        requested_history_digest=_volatility_request_digest(
            ticker, expected_sessions
        ),
        returned_history_digest=returned_digest,
        expected_sessions=expected_sessions,
        observed_sessions=observed_sessions,
        retryable=False,
        affected_signal_identities=identities,
        affected_cohorts=affected_cohorts,
    )
    issue = replace(
        provisional,
        issue_id=_candidate_volatility_issue_id(provisional),
    )
    issue.validate_integrity()
    return issue


def _candidate_identity_conflict_tickers(
    current: tuple[dict[str, str], ...], stored: tuple[dict[str, str], ...]
) -> list[str]:
    """Return tickers participating in an immutable identity-scope difference."""

    def canonical(
        identities: tuple[dict[str, str], ...],
    ) -> set[tuple[str, str, str, str]]:
        return {
            (
                identity["horizon"],
                identity["ticker"],
                identity["event_key"],
                identity["strategy"],
            )
            for identity in identities
        }

    current_only = canonical(current) - canonical(stored)
    differences = current_only or (canonical(stored) - canonical(current))
    return sorted({identity[1] for identity in differences})


def partition_daily_replay(state: DailyRunState) -> dict[str, Any] | None:
    """Partition complete replay, stage-only, stored-resume, and fresh work."""
    from tradingagents.strategies.orchestration.session_executor import PHASES

    owner, session = state.owner, state.session
    state.session_candidate_quarantines = sorted(
        record.ticker
        for record in owner._metric_store.read_candidate_bar_recoveries(
            state.epoch_id, session
        )
        if record.outcome == "quarantined"
    )
    complete_replays: dict[str, Any] = {}
    for cohort in owner.cohorts:
        name, ledger = cohort["config"].name, cohort["ledger"]
        invalid_reason = ledger.session_invalid_reason(session)
        if invalid_reason:
            state.results[name] = failure_result(invalid_reason)
            continue
        snapshots = ledger.read_snapshots(
            session, session, epoch_id=state.epoch_id, valid_only=True
        )
        horizon = cohort["config"].horizon
        policy_id = str(
            cohort["engine"].ar_config.get("paper_ledger", {}).get(
                "policy_id", f"foundation-{horizon}"
            )
        )
        phases_complete = all(
            ledger.phase_completed(session, phase) for phase in PHASES
        )
        staging_complete = ledger.staging_completed(
            session, state.epoch_id, policy_id
        )
        if len(snapshots) == 1 and phases_complete:
            try:
                cohort["executor"].validate_bound_context(session, state.epoch_id)
                persisted = cohort["executor"].persisted_input_bundle(session)
                state.governed_summaries_by_cohort[name] = [
                    dict(summary) for summary in persisted.governed_recovery_summaries
                ]
            except Exception as error:
                state.results[name] = failure_result(str(error))
                return state.critical_gap(
                    session,
                    state.processed_at,
                    state.results,
                    {},
                    str(error),
                    original_error=error,
                )
        if len(snapshots) == 1 and phases_complete and staging_complete:
            state.completed.append(cohort)
            summaries = state.governed_summaries_by_cohort.get(name, [])
            fills = ledger.read_fills(session, session)
            complete_replays[name] = {
                "signals": [
                    signal.__dict__
                    for signal in ledger.read_signals(
                        session,
                        session,
                        epoch_id=state.epoch_id,
                        policy_id=policy_id,
                    )
                ],
                "recommendations": [],
                "intents_staged": [],
                "cutoff_late": [],
                "regime": {},
                "account": snapshots[0].__dict__,
                "trades_opened": [
                    fill.fill_id for fill in fills if fill.side in {"buy", "short"}
                ],
                "trades_closed": [
                    fill.fill_id for fill in fills if fill.side in {"sell", "cover"}
                ],
                "replayed": True,
                "error": False,
                "degraded": bool(state.session_candidate_quarantines or summaries),
                "execution_valid": True,
                "staging_valid": not state.session_candidate_quarantines,
                "candidate_bar_quarantines": state.session_candidate_quarantines,
                "governed_bar_recoveries": summaries,
                "governed_failure_map": {},
            }
        elif len(snapshots) == 1 and phases_complete:
            cohort["marked_account"] = snapshots[0]
            state.stage_only.append(cohort)
        else:
            state.execution_needed.append(cohort)
    state.results.update(complete_replays)
    state.valid.extend(state.stage_only)
    for cohort in state.execution_needed:
        name = cohort["config"].name
        try:
            context = cohort["ledger"].session_execution_context(session)
            if context is None:
                state.fresh.append(cohort)
                continue
            persisted = cohort["executor"].persisted_input_bundle(session)
            state.governed_summaries_by_cohort[name] = [
                dict(summary) for summary in persisted.governed_recovery_summaries
            ]
            state.persisted[name] = persisted
            state.persisted_borrow[name] = cohort["executor"].persisted_borrow_rates(
                session
            )
            state.stored.append(cohort)
        except Exception as error:
            logger.error("Cohort %s stored resume preparation failed", name)
            state.results[name] = failure_result(str(error))
            return state.critical_gap(
                session,
                state.processed_at,
                state.results,
                {},
                str(error),
                original_error=error,
            )
    return None


def _record_due_outcomes(
    state: DailyRunState, cohorts: Iterable[dict[str, Any]]
) -> dict[str, Any] | None:
    for cohort in cohorts:
        name, executor = cohort["config"].name, cohort["executor"]
        if not executor.due_outcome_signals(state.session, state.epoch_id):
            continue
        try:
            executor.record_due_outcomes(
                state.session,
                state.epoch_id,
                executor.persisted_input_bundle(state.session).bars,
            )
        except Exception as error:
            state.results[name] = failure_result(str(error))
            return state.critical_gap(
                state.session,
                state.processed_at,
                state.results,
                {},
                str(error),
                original_error=error,
            )
    return None


def run_governed_execution(state: DailyRunState) -> dict[str, Any] | None:
    """Fetch, validate, and execute the governed session boundary."""
    from tradingagents.strategies.orchestration.governed_market_data import (
        GovernedMarketDataError,
    )
    from tradingagents.strategies.orchestration.session_executor import (
        CorporateActionBatchError,
        SessionExecutor,
    )

    owner, session = state.owner, state.session
    if state.fresh:
        benchmarks: set[str] = set()
        for cohort in state.fresh:
            name, executor = cohort["config"].name, cohort["executor"]
            required = set(executor.required_tickers(session, state.epoch_id))
            benchmarks.update(executor.benchmark_symbols)
            state.cohort_scopes[name] = tuple(
                sorted(required | set(executor.benchmark_symbols))
            )
        governed_tickers = tuple(
            sorted({ticker for scope in state.cohort_scopes.values() for ticker in scope})
        )
        memberships: dict[str, set[str]] = {}
        for cohort in owner.cohorts:
            name, executor = cohort["config"].name, cohort["executor"]
            context = cohort["ledger"].session_execution_context(session)
            required = set(
                context["required_tickers"]
                if context is not None
                else executor.required_tickers(session, state.epoch_id)
            )
            for ticker in sorted(
                (required | set(executor.benchmark_symbols)) & set(governed_tickers)
            ):
                memberships.setdefault(ticker, set()).add(name)
        cohort_ids = {
            ticker: tuple(sorted(memberships[ticker])) for ticker in governed_tickers
        }
        try:
            state.bundle = SessionExecutor.fetch_input_bundle(
                session,
                governed_tickers,
                owner._price_source,
                tuple(sorted(benchmarks)),
                metric_store=owner._metric_store,
                epoch_id=state.epoch_id,
                cohort_ids_by_ticker=cohort_ids,
                processed_at=state.processed_at,
                persist=True,
            )
            if state.bundle.governed_failure_map:
                raise GovernedMarketDataError(state.bundle.governed_failure_map)
            SessionExecutor.validate_shared_action_response(
                state.bundle.actions, state.bundle.tickers, session
            )
            state.processed_at = datetime.now(timezone.utc)
        except GovernedMarketDataError as error:
            assign_failures(state.results, state.fresh, "critical_market_data_gap")
            return state.critical_gap(
                session,
                state.processed_at,
                state.results,
                {},
                "critical_market_data_gap",
                governed_failure_map=dict(error.failure_map),
            )
        except CorporateActionBatchError as error:
            corporate_errors = {}
            reason = "invalid corporate action batch: " + "; ".join(
                sorted(set(error.errors))
            )
            for cohort in state.fresh:
                name = cohort["config"].name
                state.results[name] = failure_result(reason)
                corporate_errors[name] = error
            return state.critical_gap(
                session,
                state.processed_at,
                state.results,
                state.bundle.bars if state.bundle is not None else {},
                "critical_market_data_gap",
                corporate_action_errors=corporate_errors,
            )
        except Exception:
            reason = "shared session input fetch failed"
            assign_failures(state.results, state.fresh, reason)
            return state.critical_gap(
                session, state.processed_at, state.results, {}, reason
            )

        for name, scope in state.cohort_scopes.items():
            scoped = state.bundle.for_tickers(scope)
            state.governed_summaries_by_cohort[name] = [
                dict(summary) for summary in scoped.governed_recovery_summaries
            ]
        critical_gap = False
        for cohort in state.fresh:
            executor = cohort["executor"]
            if not executor.due_outcome_signals(session, state.epoch_id):
                continue
            _, invalid = executor.validated_outcome_bars(
                session, state.epoch_id, state.bundle.bars, state.processed_at
            )
            if invalid:
                reason = "market data validation failed: " + "; ".join(
                    f"{ticker} {invalid[ticker]}" for ticker in sorted(invalid)
                )
                state.results[cohort["config"].name] = failure_result(reason)
                critical_gap = True
        if critical_gap:
            return state.critical_gap(
                session,
                state.processed_at,
                state.results,
                state.bundle.bars,
                "critical_market_data_gap",
            )
        preflight_gap, corporate_errors = False, {}
        for cohort in state.fresh:
            name, executor = cohort["config"].name, cohort["executor"]
            governed = tuple(
                sorted(
                    set(executor.required_tickers(session, state.epoch_id))
                    | set(executor.benchmark_symbols)
                )
            )
            try:
                executor.validate_execution_input_bundle(
                    session,
                    state.epoch_id,
                    state.bundle.for_tickers(governed),
                    state.processed_at,
                )
            except CorporateActionBatchError as error:
                reason = "invalid corporate action batch: " + "; ".join(
                    sorted(set(error.errors))
                )
                state.results[name], corporate_errors[name] = failure_result(reason), error
                preflight_gap = True
            except Exception as error:
                state.results[name] = failure_result(
                    f"market data validation failed: {error}"
                )
                preflight_gap = True
        if preflight_gap:
            return state.critical_gap(
                session,
                state.processed_at,
                state.results,
                state.bundle.bars,
                "critical_market_data_gap",
                corporate_action_errors=corporate_errors,
            )

    early = _record_due_outcomes(state, state.completed)
    if early is not None:
        return early
    if not state.execution_needed and not state.stage_only:
        return state.finalize()
    for cohort in state.stored:
        name = cohort["config"].name
        try:
            lifecycle = cohort["executor"].execute_open_and_mark(
                session,
                state.epoch_id,
                state.persisted[name],
                state.persisted_borrow[name],
                state.processed_at,
            )
        except Exception as error:
            logger.error("Cohort %s stored resume failed", name, exc_info=True)
            state.results[name] = failure_result(str(error))
            return state.critical_gap(
                session,
                state.processed_at,
                state.results,
                {},
                str(error),
                original_error=error,
            )
        if not lifecycle.valid or lifecycle.snapshot is None:
            state.results[name] = {
                "error": True,
                "invalid_reason": lifecycle.invalid_reason,
            }
        else:
            cohort["marked_account"] = lifecycle.snapshot
            state.valid.append(cohort)
    execution_bundle_gap = False
    if state.bundle is not None:
        for cohort in state.fresh:
            name = cohort["config"].name
            try:
                lifecycle = cohort["executor"].execute_open_and_mark(
                    session,
                    state.epoch_id,
                    state.bundle.for_tickers(state.cohort_scopes[name]),
                    {},
                    state.processed_at,
                )
            except Exception as error:
                logger.error("Cohort %s execution failed", name, exc_info=True)
                state.results[name] = failure_result(str(error))
                continue
            if not lifecycle.valid or lifecycle.snapshot is None:
                state.results[name] = failure_result(lifecycle.invalid_reason)
                execution_bundle_gap |= lifecycle.invalid_reason.startswith(
                    "market data validation failed:"
                )
            else:
                cohort["marked_account"] = lifecycle.snapshot
                state.valid.append(cohort)
    if execution_bundle_gap:
        return state.critical_gap(
            session,
            state.processed_at,
            state.results,
            state.bundle.bars if state.bundle is not None else {},
            "critical_market_data_gap",
        )
    if not state.valid:
        return state.finalize()
    return _record_due_outcomes(state, state.valid)


def run_horizon_screening(state: DailyRunState) -> dict[str, Any] | None:
    """Run each required horizon once and merge governed reference bars."""
    owner, session = state.owner, state.session
    state.first_engine = owner.cohorts[0]["engine"]
    lookback_start = (
        datetime.strptime(state.trading_date, "%Y-%m-%d") - timedelta(days=90)
    ).strftime("%Y-%m-%d")
    state.shared_data = state.first_engine._fetch_all_data(
        lookback_start, state.trading_date
    )
    logger.info("Shared data fetched: %s", list(state.shared_data.keys()))
    for horizon in sorted({cohort["config"].horizon for cohort in state.valid}):
        signals, regime, health = owner._screen_for_horizon(
            state.shared_data, state.trading_date, horizon
        )
        if not owner._persist_horizon_health(
            health, session, owner._policy_id_for_horizon(horizon)
        ):
            assign_failures(state.results, state.valid, "unclassified_strategy_silence")
            return state.finalize()
        state.horizon_signals[horizon] = (signals, regime, health)
        logger.info("Horizon %s: %d signals", horizon, len(signals))
    try:
        for cohort in [*state.valid, *state.completed]:
            bars = cohort["executor"].validated_execution_reference_bars(
                session, state.epoch_id
            )
            for ticker, bar in bars.items():
                existing = state.governed_reference_bars.get(ticker)
                if existing is not None and existing != bar:
                    raise ValueError(
                        f"conflicting governed execution bar for {ticker}/{session}"
                    )
                state.governed_reference_bars[ticker] = bar
    except Exception as error:
        reason = f"governed execution reference-bar validation failed: {error}"
        governed_cohorts = [*state.completed, *state.valid]
        assign_failures(
            state.results,
            governed_cohorts,
            reason,
            degraded=False,
            execution_valid=False,
            staging_valid=False,
            candidate_bar_quarantines=[],
        )
        return state.critical_gap(
            session,
            state.processed_at,
            state.results,
            state.bundle.bars if state.bundle is not None else {},
            "critical_market_data_gap",
            original_error=error,
        )
    return None


def _all_signals(state: DailyRunState) -> list[dict]:
    return [
        signal
        for signals, _, _ in state.horizon_signals.values()
        for signal in signals
    ]


def _candidate_identity_scope_for_run(
    state: DailyRunState,
    signals: list[dict],
    stored_recoveries: dict[str, Any],
) -> set[str]:
    from tradingagents.strategies.execution.ids import stable_id
    from tradingagents.strategies.metrics.models import CandidateSignalIdentityBinding

    current = _candidate_signal_identity_scope(state.horizon_signals, state.session)
    binding = state.owner._metric_store.read_candidate_signal_identity_binding(
        state.epoch_id, state.session
    )
    conflicts: set[str] = set()
    if binding is None:
        state.issue_identity_scope = current
        if state.completed or stored_recoveries:
            conflicts.update(signal["ticker"] for signal in current)
            conflicts.update(stored_recoveries)
            if not conflicts:
                conflicts.add("identity-scope")
        else:
            state.owner._metric_store.save_candidate_signal_identity_binding(
                CandidateSignalIdentityBinding(
                    binding_id=stable_id(
                        "candidate_signal_identity_binding",
                        state.epoch_id,
                        state.session,
                    ),
                    epoch_id=state.epoch_id,
                    session=state.session,
                    identities=current,
                )
            )
    else:
        state.issue_identity_scope = binding.identities
        unfinished = set(state.horizon_signals)
        stored = tuple(
            identity
            for identity in binding.identities
            if identity.get("horizon") in unfinished
        )
        conflicts.update(_candidate_identity_conflict_tickers(current, stored))
    return conflicts


def _replay_candidate_reference_issues(
    state: DailyRunState,
    stored_recoveries: dict[str, Any],
    immutable_conflicts: bool,
) -> None:
    stored_issues = {
        issue.ticker: issue
        for issue in state.owner._metric_store.read_candidate_input_issues(
            state.epoch_id, state.session
        )
        if issue.dependency_kind == "reference_bar"
    }
    for ticker, record in stored_recoveries.items():
        stored_issue = stored_issues.pop(ticker, None)
        if record.outcome != "quarantined":
            if stored_issue is not None:
                raise ValueError(
                    f"candidate reference-bar issue conflicts with resolved {ticker}"
                )
            continue
        expected = _candidate_reference_issue(
            record,
            signal_identity_scope=state.issue_identity_scope,
            cohorts=state.owner.cohorts,
        )
        if stored_issue is None:
            if not immutable_conflicts:
                state.owner._metric_store.save_candidate_input_issue(expected)
                state.candidate_issue_references.append(expected.reference())
        elif stored_issue.canonical_payload() != expected.canonical_payload():
            raise ValueError(
                f"candidate reference-bar issue has unequal replay evidence for {ticker}"
            )
        else:
            state.candidate_issue_references.append(expected.reference())
    if stored_issues:
        raise ValueError(
            "candidate reference-bar issue is missing compatibility recovery evidence"
        )


def _restore_candidate_reference_issue_references(
    state: DailyRunState, stored_recoveries: dict[str, Any]
) -> None:
    state.candidate_issue_references[:] = [
        reference
        for reference in state.candidate_issue_references
        if reference["dependency_kind"] != "reference_bar"
    ]
    for record in stored_recoveries.values():
        if record.outcome != "quarantined":
            continue
        try:
            issue = _candidate_reference_issue(
                record,
                signal_identity_scope=state.issue_identity_scope,
                cohorts=state.owner.cohorts,
            )
        except Exception:
            continue
        state.candidate_issue_references.append(issue.reference())


def _accepted_candidate_bar(state: DailyRunState, ticker: str, record: Any) -> Any:
    from tradingagents.strategies.execution.models import MarketBar
    from tradingagents.strategies.execution.price_source import validate_required_bars

    evidence = record.attempts[-1]
    if evidence["validation_error"] is not None or any(
        evidence[field] is None for field in ("open", "high", "low", "close")
    ):
        raise ValueError(f"resolved candidate evidence is incomplete for {ticker}")
    bar = MarketBar(
        ticker,
        state.session,
        evidence["open"],
        evidence["high"],
        evidence["low"],
        evidence["close"],
        str(evidence["source"]),
        evidence["fetched_at"],
        False,
    )
    validate_required_bars(
        {(ticker, state.session): bar},
        {ticker},
        state.session,
        bar.fetched_at,
        timedelta.max,
    )
    if bar.fetched_at < session_close(state.session):
        raise ValueError(f"resolved candidate evidence is pre-close for {ticker}")
    return bar


def _resolve_candidate_bars(
    state: DailyRunState,
    unresolved: set[str],
    signals: list[dict],
) -> list[str] | None:
    from tradingagents.strategies.execution.price_source import (
        CandidateBarAttempt,
        CandidateBarResolution,
    )
    from tradingagents.strategies.metrics.models import CandidateBarRecoveryRecord
    from tradingagents.strategies.orchestration.session_executor import (
        ensure_reference_bars,
    )

    if not unresolved:
        return None
    max_age = timedelta(
        hours=float(
            state.owner._base_config.get("autoresearch", {})
            .get("paper_ledger", {})
            .get("bar_max_age_hours", 24)
        )
    )
    resolver = getattr(state.owner._price_source, "resolve_candidate_daily_bars", None)
    if callable(resolver):
        resolution = resolver(
            sorted(unresolved), state.session, state.processed_at, max_age
        )
    else:
        strict = ensure_reference_bars(
            state.owner._price_source,
            unresolved,
            state.session,
            state.processed_at,
            max_age,
        )
        resolution = CandidateBarResolution(
            bars={(ticker, state.session): bar for ticker, bar in strict.items()},
            attempts=tuple(
                CandidateBarAttempt(
                    ticker=ticker,
                    session=state.session,
                    attempt=1,
                    source=bar.source,
                    fetched_at=bar.fetched_at,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    validation_error=None,
                )
                for ticker, bar in sorted(strict.items())
            ),
            recovered_tickers=frozenset(),
            quarantined_tickers=frozenset(),
        )
    if any(bar_session != state.session for _, bar_session in resolution.bars):
        raise ValueError("candidate resolution session mismatch")
    bars = {ticker for ticker, _ in resolution.bars}
    recovered, quarantined = (
        set(resolution.recovered_tickers),
        set(resolution.quarantined_tickers),
    )
    attempt_tickers = {attempt.ticker for attempt in resolution.attempts}
    scope = bars | recovered | quarantined | attempt_tickers
    governed_conflicts = sorted(scope & set(state.governed_reference_bars))
    if governed_conflicts:
        return governed_conflicts
    if (
        scope != unresolved
        or attempt_tickers != unresolved
        or bars | quarantined != unresolved
        or bars & quarantined
        or recovered & quarantined
        or not recovered <= bars
    ):
        raise ValueError("candidate resolution state is contradictory")
    attempts_by_ticker = {
        ticker: tuple(
            attempt for attempt in resolution.attempts if attempt.ticker == ticker
        )
        for ticker in sorted(unresolved)
    }
    for ticker, attempts in attempts_by_ticker.items():
        final = attempts[-1]
        if ticker in quarantined:
            if final.validation_error is None:
                raise ValueError("candidate quarantine has successful final evidence")
        else:
            bar = resolution.bars[(ticker, state.session)]
            if final.validation_error is not None or (
                bar.ticker,
                bar.session,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.source,
                bar.fetched_at,
            ) != (
                ticker,
                state.session,
                final.open,
                final.high,
                final.low,
                final.close,
                final.source,
                final.fetched_at,
            ):
                raise ValueError("candidate bar differs from successful final evidence")
    state.candidate_reference_bars.update(
        {
            ticker: bar
            for (ticker, bar_session), bar in resolution.bars.items()
            if bar_session == state.session
        }
    )
    state.quarantined_tickers.update(quarantined)
    for ticker, attempts in attempts_by_ticker.items():
        serialized_attempts = tuple(asdict(attempt) for attempt in attempts)
        outcome = (
            "quarantined"
            if ticker in quarantined
            else "recovered" if ticker in recovered else "accepted"
        )
        identities = tuple(
            {"event_key": event_key, "strategy": strategy}
            for event_key, strategy in _candidate_signal_identity_pairs(
                signals, ticker, state.session
            )
        )
        recovery = CandidateBarRecoveryRecord(
            recovery_id=_candidate_bar_recovery_id(
                epoch_id=state.epoch_id,
                session=state.session,
                ticker=ticker,
                outcome=outcome,
                attempts=serialized_attempts,
                signal_identities=identities,
            ),
            epoch_id=state.epoch_id,
            session=state.session,
            ticker=ticker,
            outcome=outcome,
            attempts=serialized_attempts,
            signal_identities=identities,
        )
        state.owner._metric_store.save_candidate_bar_recovery(recovery)
        if outcome == "quarantined":
            issue = _candidate_reference_issue(
                recovery,
                signal_identity_scope=state.issue_identity_scope,
                cohorts=state.owner.cohorts,
            )
            state.owner._metric_store.save_candidate_input_issue(issue)
            state.candidate_issue_references.append(issue.reference())
    return None


def run_candidate_reference_validation(
    state: DailyRunState,
) -> dict[str, Any] | None:
    """Replay or resolve candidate reference bars without crossing governed scope."""
    signals = _all_signals(state)
    signal_tickers = {
        str(signal.get("ticker", "")).strip().upper()
        for signal in signals
        if str(signal.get("ticker", "")).strip()
    }
    candidate_only = signal_tickers - set(state.governed_reference_bars)
    stored = {
        record.ticker: record
        for record in state.owner._metric_store.read_candidate_bar_recoveries(
            state.epoch_id, state.session
        )
    }
    classification_conflicts = sorted(set(stored) & set(state.governed_reference_bars))
    recovery_conflicts = sorted(
        ticker
        for ticker, record in stored.items()
        if record.recovery_id
        != _candidate_bar_recovery_id(
            epoch_id=record.epoch_id,
            session=record.session,
            ticker=record.ticker,
            outcome=record.outcome,
            attempts=record.attempts,
            signal_identities=record.signal_identities,
        )
    )
    state.existing_quarantines = sorted(
        record.ticker
        for record in stored.values()
        if record.outcome == "quarantined"
        and record.ticker not in state.governed_reference_bars
    )
    try:
        replay_conflicts = _candidate_identity_scope_for_run(state, signals, stored)
    except Exception as error:
        return state.fail_candidates(
            f"candidate identity validation failed: {error}",
            degraded=bool(state.existing_quarantines),
        )
    try:
        _replay_candidate_reference_issues(
            state,
            stored,
            bool(replay_conflicts or recovery_conflicts or classification_conflicts),
        )
    except Exception:
        _restore_candidate_reference_issue_references(state, stored)
        return state.fail_candidates(
            "candidate reference-bar validation failed",
            degraded=bool(state.existing_quarantines),
        )
    if replay_conflicts:
        return state.fail_candidates(
            _candidate_replay_conflict_reason(sorted(replay_conflicts)),
            degraded=bool(state.existing_quarantines),
        )
    if recovery_conflicts:
        return state.fail_candidates(
            "candidate reference-bar validation failed",
            degraded=bool(state.existing_quarantines),
        )
    if classification_conflicts:
        return state.fail_candidate_classification(classification_conflicts)
    existing = {ticker: record for ticker, record in stored.items() if ticker in candidate_only}
    try:
        for ticker, record in existing.items():
            if record.outcome == "quarantined":
                state.quarantined_tickers.add(ticker)
            else:
                state.candidate_reference_bars[ticker] = _accepted_candidate_bar(
                    state, ticker, record
                )
        conflicts = _resolve_candidate_bars(
            state, candidate_only - set(existing), signals
        )
        if conflicts:
            return state.fail_candidate_classification(conflicts)
    except Exception:
        quarantines = sorted(
            set(state.existing_quarantines) | state.quarantined_tickers
        )
        return state.fail_candidates(
            "candidate reference-bar validation failed",
            degraded=True,
            quarantines=quarantines,
        )
    if state.quarantined_tickers:
        state.horizon_signals = filter_horizon_signals(
            state.horizon_signals, state.quarantined_tickers
        )
    state.candidate_bar_quarantines = sorted(
        set(state.existing_quarantines) | state.quarantined_tickers
    )
    return None


def _invalid_volatility_histories(
    engine: Any,
    tickers: set[str],
    *,
    lookback: int,
    floor: float,
    expected_sessions: tuple[date, ...],
) -> list[str]:
    from tradingagents.strategies.trading.portfolio_policy import (
        build_annualized_volatility_evidence,
    )

    invalid = []
    for ticker in sorted(tickers):
        try:
            build_annualized_volatility_evidence(
                engine._price_cache,
                (ticker,),
                lookback_sessions=lookback,
                floor=floor,
                expected_sessions=expected_sessions,
            )
        except (TypeError, ValueError, OverflowError):
            invalid.append(ticker)
    return invalid


def run_candidate_volatility_validation(
    state: DailyRunState,
) -> dict[str, Any] | None:
    """Replay or resolve portfolio-policy volatility evidence."""
    from tradingagents.strategies.trading.portfolio_policy import (
        build_annualized_volatility_evidence,
    )

    settings = state.owner._base_config.get("autoresearch", {}).get(
        "portfolio_policy"
    )
    if not isinstance(settings, dict):
        return None
    signals = _all_signals(state)
    candidate_tickers = {
        str(signal.get("ticker", "")).strip().upper()
        for signal in signals
        if str(signal.get("ticker", "")).strip()
    }
    governed_tickers = set(state.governed_reference_bars)
    lookback = int(settings.get("volatility_lookback_sessions", 60))
    floor = float(settings.get("annualized_volatility_floor", 0.15))
    descending = [previous_session(state.session)]
    for _ in range(lookback):
        descending.append(previous_session(descending[-1]))
    expected_sessions = tuple(reversed(descending))
    buffer_days = max(120, 2 * lookback)
    volatility_start = (
        datetime.strptime(state.trading_date, "%Y-%m-%d")
        - timedelta(days=buffer_days)
    ).date()
    volatility_start = min(volatility_start, expected_sessions[0]).isoformat()
    candidate_boundary = False
    try:
        for cohort in state.owner.cohorts:
            governed_tickers.update(
                str(row["ticker"]).strip().upper()
                for row in cohort["ledger"].policy_open_lot_projection(state.session)
            )
            governed_tickers.update(
                str(row["ticker"]).strip().upper()
                for row in cohort["ledger"].policy_pending_entry_projection()
            )
        candidate_tickers -= governed_tickers
        candidate_boundary = True
        stored_issues = {
            issue.ticker: issue
            for issue in state.owner._metric_store.read_candidate_input_issues(
                state.epoch_id, state.session
            )
            if issue.dependency_kind == "volatility_history"
        }
        for ticker, stored_issue in sorted(stored_issues.items()):
            try:
                identities, cohorts = _candidate_volatility_scope(
                    ticker,
                    signal_identity_scope=state.issue_identity_scope,
                    cohorts=state.owner.cohorts,
                )
                valid_replay = (
                    stored_issue.issue_id == _candidate_volatility_issue_id(stored_issue)
                    and stored_issue.requested_history_digest
                    == _volatility_request_digest(ticker, expected_sessions)
                    and stored_issue.expected_sessions == expected_sessions
                    and tuple(
                        dict(identity)
                        for identity in stored_issue.affected_signal_identities
                    )
                    == identities
                    and stored_issue.affected_cohorts == cohorts
                    and stored_issue.source == "yfinance"
                    and stored_issue.retryable is False
                )
                if not valid_replay:
                    raise ValueError(
                        f"candidate volatility issue has unequal replay evidence for {ticker}"
                    )
            except Exception:
                state.candidate_issue_reference_suppressions.add(
                    stored_issue.issue_id
                )
                raise
            reference = stored_issue.reference()
            state.candidate_issue_references.append(reference)
            if ticker in governed_tickers or ticker not in candidate_tickers:
                raise ValueError(
                    f"stored candidate volatility scope conflicts for {ticker}"
                )
            state.volatility_quarantines.add(ticker)
        candidate_boundary = False
        governed_refetch = _invalid_volatility_histories(
            state.first_engine,
            governed_tickers,
            lookback=lookback,
            floor=floor,
            expected_sessions=expected_sessions,
        )
        if governed_refetch:
            try:
                state.first_engine._fetch_missing_prices(
                    governed_refetch, volatility_start, state.trading_date
                )
            except Exception:
                raise ValueError("provider_error") from None
        governed_evidence = build_annualized_volatility_evidence(
            state.first_engine._price_cache,
            governed_tickers,
            lookback_sessions=lookback,
            floor=floor,
            expected_sessions=expected_sessions,
        )
        candidate_boundary = True
        unresolved = candidate_tickers - set(stored_issues)
        candidate_refetch = _invalid_volatility_histories(
            state.first_engine,
            unresolved,
            lookback=lookback,
            floor=floor,
            expected_sessions=expected_sessions,
        )
        provider_errors: set[str] = set()
        retry_times: dict[str, datetime] = {}
        for ticker in candidate_refetch:
            try:
                state.first_engine._fetch_missing_prices(
                    [ticker], volatility_start, state.trading_date
                )
            except Exception:
                provider_errors.add(ticker)
            retry_times[ticker] = datetime.now(timezone.utc)
        remaining_invalid = set(
            _invalid_volatility_histories(
                state.first_engine,
                set(candidate_refetch) - provider_errors,
                lookback=lookback,
                floor=floor,
                expected_sessions=expected_sessions,
            )
        )
        for ticker in candidate_refetch:
            if ticker not in provider_errors and ticker not in remaining_invalid:
                continue
            issue = _candidate_volatility_issue(
                epoch_id=state.epoch_id,
                session=state.session,
                ticker=ticker,
                fetched_at=retry_times[ticker],
                expected_sessions=expected_sessions,
                price_cache=state.first_engine._price_cache,
                signal_identity_scope=state.issue_identity_scope,
                cohorts=state.owner.cohorts,
                reason_code="provider_error" if ticker in provider_errors else None,
            )
            state.owner._metric_store.save_candidate_input_issue(issue)
            state.candidate_issue_references.append(issue.reference())
            state.volatility_quarantines.add(ticker)
        candidate_evidence = build_annualized_volatility_evidence(
            state.first_engine._price_cache,
            candidate_tickers - state.volatility_quarantines,
            lookback_sessions=lookback,
            floor=floor,
            expected_sessions=expected_sessions,
        )
        state.shared_volatility_evidence = dict(governed_evidence)
        state.shared_volatility_evidence.update(candidate_evidence)
    except Exception as error:
        reason = (
            "candidate volatility-history validation failed"
            if candidate_boundary
            else f"shared staging volatility evidence failed: {error}"
        )
        assign_failures(
            state.results,
            state.valid,
            reason,
            degraded=bool(state.candidate_bar_quarantines),
            execution_valid=True,
            staging_valid=False,
            candidate_bar_quarantines=state.candidate_bar_quarantines,
        )
        return state.finalize()
    if state.volatility_quarantines:
        state.horizon_signals = filter_horizon_signals(
            state.horizon_signals, state.volatility_quarantines
        )
        for ticker in state.volatility_quarantines:
            state.candidate_reference_bars.pop(ticker, None)
    return None


def stage_daily_results(state: DailyRunState) -> dict[str, Any]:
    """Persist regime snapshots, enrich once, and stage eligible cohorts."""
    all_signals = _all_signals(state)
    staging_valid = not (
        state.candidate_bar_quarantines or state.volatility_quarantines
    )
    for horizon, (_, regime, _) in state.horizon_signals.items():
        state.first_engine.state.save_regime_snapshot(
            regime,
            session=state.session,
            epoch_id=state.epoch_id,
            horizon=horizon,
            execution_valid=True,
            staging_valid=staging_valid,
            candidate_bar_quarantines=tuple(state.candidate_bar_quarantines),
        )
    conflicts = sorted(
        set(state.governed_reference_bars) & set(state.candidate_reference_bars)
    )
    if conflicts:
        return state.fail_candidate_classification(conflicts)
    enrichment = state.owner._fetch_openbb_enrichment(all_signals)
    reference_bars = dict(state.governed_reference_bars)
    reference_bars.update(state.candidate_reference_bars)
    state.shared_data["_execution_reference_bars"] = reference_bars
    for cohort in state.valid:
        cfg, engine = cohort["config"], cohort["engine"]
        signals, regime, _ = state.horizon_signals[cfg.horizon]
        summaries = state.governed_summaries_by_cohort.get(cfg.name, [])
        try:
            staged = engine.screen_and_stage(
                trading_date=state.trading_date,
                data=state.shared_data,
                shared_signals=signals,
                shared_regime=regime,
                enrichment=enrichment,
                size_profile=cohort.get("size_profile"),
                marked_account=cohort["marked_account"],
                annualized_volatility_evidence=state.shared_volatility_evidence,
            )
            fills = cohort["ledger"].read_fills(state.session, state.session)
            staged.update(
                trades_opened=[
                    fill.fill_id for fill in fills if fill.side in {"buy", "short"}
                ],
                trades_closed=[
                    fill.fill_id for fill in fills if fill.side in {"sell", "cover"}
                ],
                error=False,
                degraded=bool(state.candidate_bar_quarantines or summaries),
                execution_valid=True,
                staging_valid=staging_valid,
                candidate_bar_quarantines=state.candidate_bar_quarantines,
                governed_bar_recoveries=summaries,
                governed_failure_map={},
            )
            state.results[cfg.name] = staged
        except Exception as error:
            logger.error("Cohort %s staging failed", cfg.name, exc_info=True)
            state.results[cfg.name] = failure_result(
                str(error),
                degraded=bool(state.candidate_bar_quarantines),
                execution_valid=True,
                staging_valid=False,
                candidate_bar_quarantines=state.candidate_bar_quarantines,
                governed_bar_recoveries=summaries,
                governed_failure_map={},
            )
    return state.finalize()


@dataclass(frozen=True)
class DailyRunSummary:
    outcome: str
    total: int
    failed: tuple[str, ...]
    degraded: tuple[str, ...]
    execution_valid: bool
    candidate_bar_quarantines: tuple[str, ...]
    governed_bar_recoveries: tuple[dict[str, object], ...]
    governed_failure_map: dict[str, str]
    candidate_input_issues: tuple[dict[str, object], ...]
    degradation_label: str | None


def summarize_cohort_results(
    results: dict, trading_date: str | None = None
) -> DailyRunSummary:
    issue_results = {
        name: result
        for name, result in results.items()
        if isinstance(result, dict) and result.get("candidate_input_issues")
    }
    candidate_issues = (
        tuple(aggregate_candidate_input_issues(issue_results, trading_date))
        if issue_results
        else ()
    )
    recoveries, failures = aggregate_governed_reporting(results)
    n_failed, total, failed = count_failed_cohorts(results)
    n_degraded, _, degraded = count_degraded_cohorts(results)
    quarantines = tuple(
        sorted(
            {
                str(ticker)
                for name in degraded
                for ticker in results[name].get("candidate_bar_quarantines", [])
            }
        )
    )
    execution_valid = bool(results) and all(
        isinstance(result, dict) and result.get("execution_valid") is True
        for result in results.values()
    )
    label = None
    if n_degraded:
        if recoveries and candidate_issues:
            label = "candidate input issue; governed bar recovery"
        elif recoveries and quarantines:
            label = "candidate data quarantined; governed bar recovery"
        elif recoveries:
            label = "governed bar recovery"
        elif candidate_issues:
            label = "candidate input issue"
        else:
            label = "candidate data quarantined"
    outcome = (
        RunOutcome.FAILED.value
        if n_failed
        else RunOutcome.DEGRADED.value
        if n_degraded
        else RunOutcome.CLEAN.value
    )
    return DailyRunSummary(
        outcome,
        total,
        tuple(failed),
        tuple(degraded),
        execution_valid,
        quarantines,
        tuple(recoveries),
        failures,
        candidate_issues,
        label,
    )
