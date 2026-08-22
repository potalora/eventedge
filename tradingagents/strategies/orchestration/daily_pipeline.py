"""Pure, strict reporting helpers for daily cohort runs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from tradingagents.strategies.metrics.models import GOVERNED_BAR_RECOVERY_CONTRACT
from tradingagents.strategies.orchestration.run_outcome import RunOutcome
from tradingagents.strategies.orchestration.trading_calendar import is_session

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
class DailyFinalizationState:
    """Mutable per-run state required by the daily result finalizer."""

    metric_store: Any
    epoch_id: str | None
    session: date
    candidate_issue_references: list[dict[str, object]] = field(default_factory=list)
    candidate_issues_hydrated: bool = False
    candidate_bar_quarantine_suppressions: set[str] = field(default_factory=set)
    candidate_issue_reference_suppressions: set[str] = field(default_factory=set)
    governed_summaries_by_cohort: dict[str, list[dict[str, object]]] = field(
        default_factory=dict
    )


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
    state: DailyFinalizationState,
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
            issues = state.metric_store.read_candidate_input_issues(
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
            if epoch_match.group("session") != session_text:
                raise ValueError("candidate input issue reference id is invalid")
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
