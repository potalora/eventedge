"""Regression tests: a daily run where cohorts errored must never be recorded
as a clean success.

2026-06-01 incident: FD exhaustion errored all 16 cohorts, but run_cohorts.py
exited 0 (per-cohort errors are caught and returned as {"error": true}), so the
manifest logged success:true and the failure was invisible until the missing
report was noticed.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from scripts import run_cohorts
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    CohortOrchestrator,
    aggregate_candidate_input_issues,
    aggregate_governed_reporting,
    build_default_cohorts,
    count_degraded_cohorts,
    count_failed_cohorts,
)
from tradingagents.strategies.orchestration.generation_manager import (
    GenerationManager,
    _extract_cohort_results,
)
from tradingagents.strategies.orchestration.candidate_inputs import CandidateInputIssue
from tradingagents.strategies.orchestration.daily_pipeline import DailyRunState


# --- count_failed_cohorts (pure) ---


def test_count_failed_cohorts_all_errored():
    results = {f"c{i}": {"error": True} for i in range(16)}
    n_failed, n_total, failed = count_failed_cohorts(results)
    assert n_failed == 16
    assert n_total == 16
    assert failed == sorted(results)


def test_count_failed_cohorts_partial():
    results = {
        "a": {"error": True},
        "b": {"signals": [], "trades_opened": []},
        "c": {"error": True},
    }
    assert count_failed_cohorts(results) == (2, 3, ["a", "c"])


def test_count_failed_cohorts_none():
    results = {"a": {"signals": []}, "b": {"trades_opened": [1]}}
    assert count_failed_cohorts(results) == (0, 2, [])


def test_count_failed_cohorts_ignores_non_dicts_and_falsey_error():
    results = {"a": {"error": False}, "b": "weird", "c": {"error": None}}
    assert count_failed_cohorts(results) == (0, 3, [])


def test_candidate_quarantine_is_reportable_degradation_not_execution_failure():
    results = {
        f"c{i}": {
            "error": False,
            "degraded": True,
            "execution_valid": True,
            "staging_valid": False,
            "candidate_bar_quarantines": ["ALX"],
            "signals": [{"ticker": "MSFT", "strategy": "earnings_call"}],
        }
        for i in range(16)
    }

    assert count_failed_cohorts(results) == (0, 16, [])
    assert all(result["degraded"] for result in results.values())
    assert all(
        result["candidate_bar_quarantines"] == ["ALX"] for result in results.values()
    )


def test_count_degraded_cohorts_is_distinct_from_execution_failures():
    results = {
        "candidate_quarantined": {
            "error": False,
            "degraded": True,
            "execution_valid": True,
            "candidate_bar_quarantines": ["ALX"],
        },
        "clean": {"error": False, "degraded": False, "execution_valid": True},
    }

    assert count_failed_cohorts(results) == (0, 2, [])
    assert count_degraded_cohorts(results) == (1, 2, ["candidate_quarantined"])


def _candidate_issue_reference(
    *,
    affected_cohorts,
    epoch_start_session="2026-08-10",
    session="2026-08-10",
):
    return {
        "issue_id": "candidate_input_issue_" + "a" * 32,
        "epoch_id": f"gen_001-{epoch_start_session}-" + "b" * 16,
        "session": session,
        "dependency_kind": "reference_bar",
        "reason_code": "provider_error",
        "ticker": "UI",
        "affected_cohorts": affected_cohorts,
    }


def _candidate_issue_carrier(*references):
    return {
        "degraded": True,
        "staging_valid": False,
        "candidate_input_issues": list(references),
    }


def test_candidate_issue_reporting_deduplicates_sixteen_identical_references():
    cohorts = [f"cohort-{index:02d}" for index in range(16)]
    reference = _candidate_issue_reference(affected_cohorts=cohorts)
    results = {
        cohort: _candidate_issue_carrier(dict(reference)) for cohort in cohorts
    }

    assert aggregate_candidate_input_issues(results) == [reference]


def test_candidate_issue_reporting_accepts_later_session_in_same_epoch():
    reference = _candidate_issue_reference(
        affected_cohorts=["cohort-a"],
        epoch_start_session="2026-08-24",
        session="2026-08-26",
    )

    assert aggregate_candidate_input_issues(
        {"cohort-a": _candidate_issue_carrier(reference)},
        trading_date="2026-08-26",
    ) == [reference]


@pytest.mark.parametrize("epoch_start_session", ("2026-08-23", "2026-08-27"))
def test_candidate_issue_reporting_rejects_invalid_epoch_start(
    epoch_start_session,
):
    reference = _candidate_issue_reference(
        affected_cohorts=["cohort-a"],
        epoch_start_session=epoch_start_session,
        session="2026-08-26",
    )

    with pytest.raises(
        ValueError, match="candidate input issue reference id is invalid"
    ):
        aggregate_candidate_input_issues(
            {"cohort-a": _candidate_issue_carrier(reference)},
            trading_date="2026-08-26",
        )


@pytest.mark.parametrize(
    ("issue_epoch_id", "issue_session"),
    (
        pytest.param(
            "gen_001-2026-08-10-" + "b" * 16,
            date(2026, 8, 11),
            id="session-mismatch",
        ),
        pytest.param(
            "gen_001-2026-08-11-" + "b" * 16,
            date(2026, 8, 10),
            id="epoch-mismatch",
        ),
    ),
)
def test_candidate_issue_hydration_rejects_mismatched_durable_scope(
    issue_epoch_id, issue_session
):
    state_session = date(2026, 8, 10)
    state_epoch_id = "gen_001-2026-08-10-" + "b" * 16
    issue = CandidateInputIssue.create(
        issue_id="candidate_input_issue_" + "a" * 32,
        epoch_id=issue_epoch_id,
        session=issue_session,
        dependency_kind="reference_bar",
        reason_code="provider_error",
        ticker="UI",
        source="yfinance",
        fetched_at=datetime(
            issue_session.year,
            issue_session.month,
            issue_session.day,
            21,
            tzinfo=timezone.utc,
        ),
        requested_history_digest="sha256:" + "c" * 64,
        returned_history_digest="sha256:" + "d" * 64,
        expected_sessions=(issue_session,),
        observed_sessions=(),
        retryable=False,
        affected_signal_identities=(),
        affected_cohorts=("cohort-a",),
    )

    def read_candidate_input_issues(exact_epoch, exact_session):
        assert (exact_epoch, exact_session) == (state_epoch_id, state_session)
        return [issue]

    store = SimpleNamespace(read_candidate_input_issues=read_candidate_input_issues)
    state = DailyRunState(
        owner=SimpleNamespace(_metric_store=store),
        trading_date=state_session.isoformat(),
        session=state_session,
        processed_at=datetime(2026, 8, 10, 21, tzinfo=timezone.utc),
        epoch_id=state_epoch_id,
    )

    with pytest.raises(ValueError, match="candidate input issue durable scope"):
        state.finalize({})


@pytest.mark.parametrize(
    "mutate",
    (
        lambda reference: {**reference, "dependency_kind": "secret_provider"},
        lambda reference: {**reference, "reason_code": "retry_exhausted"},
        lambda reference: {**reference, "ticker": "ui"},
        lambda reference: {**reference, "session": "08/10/2026"},
        lambda reference: {
            **reference,
            "issue_id": "candidate_input_issue_provider-secret",
        },
        lambda reference: {
            **reference,
            "epoch_id": "gen_001-2026-08-10-provider-secret",
        },
        lambda reference: {
            **reference,
            "epoch_id": "gen_001-2026-08-11-" + "b" * 16,
        },
        lambda reference: {**reference, "affected_cohorts": ["cohort-b", "cohort-a"]},
        lambda reference: {**reference, "provider_secret": "do-not-persist"},
    ),
)
def test_candidate_issue_reporting_rejects_malformed_untrusted_references(mutate):
    cohorts = ["cohort-a", "cohort-b"]
    reference = _candidate_issue_reference(affected_cohorts=cohorts)
    malformed = mutate(reference)
    results = {
        "cohort-a": _candidate_issue_carrier(malformed),
        "cohort-b": _candidate_issue_carrier(dict(reference)),
    }

    with pytest.raises(ValueError, match="candidate input issue reference"):
        aggregate_candidate_input_issues(results)


def test_candidate_issue_reporting_rejects_unequal_duplicate_ids():
    cohorts = ["cohort-a", "cohort-b"]
    reference = _candidate_issue_reference(affected_cohorts=cohorts)
    conflicting = {**reference, "ticker": "ZKH"}

    with pytest.raises(ValueError, match="candidate input issue reference"):
        aggregate_candidate_input_issues(
            {
                "cohort-a": _candidate_issue_carrier(reference),
                "cohort-b": _candidate_issue_carrier(conflicting),
            }
        )


@pytest.mark.parametrize(
    "results",
    (
        {object(): _candidate_issue_carrier()},
        {
            "cohort-a": _candidate_issue_carrier(
                    {
                        **_candidate_issue_reference(affected_cohorts=["cohort-a"]),
                        "dependency_kind": [],
                    }
                )
        },
        {
            "cohort-a": _candidate_issue_carrier(
                    {
                        **_candidate_issue_reference(affected_cohorts=["cohort-a"]),
                        "reason_code": [],
                    }
                )
        },
        {
            "cohort-a": _candidate_issue_carrier(
                    _candidate_issue_reference(affected_cohorts=[["cohort-a"]])
                )
        },
    ),
)
def test_candidate_issue_reporting_rejects_nonstring_or_unhashable_fields(results):
    with pytest.raises(ValueError, match="candidate input issue reference"):
        aggregate_candidate_input_issues(results)


@pytest.mark.parametrize("session", ("2026-08-09", "2026-08-11"))
def test_candidate_issue_reporting_rejects_weekend_or_wrong_run_session(session):
    reference = {
        **_candidate_issue_reference(affected_cohorts=["cohort-a"]),
        "session": session,
    }
    with pytest.raises(ValueError, match="candidate input issue reference session"):
        aggregate_candidate_input_issues(
            {"cohort-a": _candidate_issue_carrier(reference)},
            trading_date="2026-08-10",
        )


def test_candidate_issue_reporting_rejects_different_ids_for_one_durable_scope():
    first = _candidate_issue_reference(affected_cohorts=["cohort-a"])
    second = {**first, "issue_id": "candidate_input_issue_" + "c" * 32}
    with pytest.raises(ValueError, match="candidate input issue reference scope"):
        aggregate_candidate_input_issues(
            {"cohort-a": _candidate_issue_carrier(first, second)}
        )


def test_candidate_issue_reporting_uses_canonical_store_order():
    zkh = {
        **_candidate_issue_reference(affected_cohorts=["cohort-a"]),
        "issue_id": "candidate_input_issue_" + "1" * 32,
        "ticker": "ZKH",
    }
    ui = {
        **_candidate_issue_reference(affected_cohorts=["cohort-a"]),
        "issue_id": "candidate_input_issue_" + "f" * 32,
        "ticker": "UI",
    }
    assert aggregate_candidate_input_issues(
        {"cohort-a": _candidate_issue_carrier(zkh, ui)}
    ) == [ui, zkh]


def test_candidate_issue_reporting_rejects_oversized_global_collection():
    results = {
        f"cohort-{index:03d}": {
            "degraded": True,
            "staging_valid": False,
            "candidate_input_issues": [
                {
                    **_candidate_issue_reference(
                        affected_cohorts=[f"cohort-{index:03d}"]
                    ),
                    "issue_id": f"candidate_input_issue_{index:032x}",
                }
            ]
        }
        for index in range(257)
    }

    with pytest.raises(ValueError, match="candidate input issue reference"):
        aggregate_candidate_input_issues(results)


def test_candidate_issue_reporting_omits_clean_and_governed_only_runs():
    assert aggregate_candidate_input_issues(
        {
            "clean": {"degraded": False},
            "governed": {"governed_bar_recoveries": []},
        }
    ) == []


@pytest.mark.parametrize(
    "carrier",
    (
        {
            "error": True,
            "degraded": False,
            "staging_valid": False,
        },
        {
            "degraded": True,
            "staging_valid": True,
        },
        {
            "degraded": True,
            "staging_valid": False,
            "candidate_input_issues": [],
        },
    ),
)
def test_candidate_issue_reporting_rejects_invalid_carrier_lifecycle(carrier):
    if "candidate_input_issues" not in carrier:
        carrier = {
            **carrier,
            "candidate_input_issues": [
                _candidate_issue_reference(affected_cohorts=["cohort-a"])
            ],
        }
    with pytest.raises(ValueError, match="candidate input issue reference"):
        aggregate_candidate_input_issues({"cohort-a": carrier})


def test_invalid_metric_epoch_exit_lazily_hydrates_persisted_candidate_issue():
    session = date(2026, 8, 10)
    issue = CandidateInputIssue.create(
        issue_id="candidate_input_issue_" + "a" * 32,
        epoch_id="gen_001-2026-08-10-" + "b" * 16,
        session=session,
        dependency_kind="reference_bar",
        reason_code="provider_error",
        ticker="UI",
        source="yfinance",
        fetched_at=datetime(2026, 8, 10, 21, tzinfo=timezone.utc),
        requested_history_digest="sha256:" + "c" * 64,
        returned_history_digest="sha256:" + "d" * 64,
        expected_sessions=(session,),
        observed_sessions=(),
        retryable=False,
        affected_signal_identities=(),
        affected_cohorts=("cohort-a",),
    )

    class Store:
        def pending_critical_gap(self):
            return None

        def read_candidate_bar_recoveries(self, epoch_id, exact_session):
            assert (epoch_id, exact_session) == (issue.epoch_id, session)
            return []

        def read_candidate_input_issues(self, epoch_id, exact_session):
            assert (epoch_id, exact_session) == (issue.epoch_id, session)
            return [issue]

    class Executor:
        def ensure_metric_epoch(self, context, exact_session):
            assert exact_session == session
            return SimpleNamespace(
                epoch_id=issue.epoch_id,
                status="invalid",
                boundary_reason="metric epoch conflict",
            )

    class Ledger:
        def session_invalid_reason(self, exact_session):
            assert exact_session == session
            return "metric epoch conflict"

    orchestrator = CohortOrchestrator.__new__(CohortOrchestrator)
    orchestrator._metric_store = Store()
    orchestrator._metric_epoch_context = object()
    orchestrator._epoch_id = None
    orchestrator.cohorts = [
        {
            "config": SimpleNamespace(name="cohort-a"),
            "executor": Executor(),
            "ledger": Ledger(),
        }
    ]

    result = orchestrator.run_daily(session.isoformat())["cohort-a"]

    assert result["error"] is True
    assert result["invalid_reason"] == "metric epoch conflict"
    assert result["degraded"] is True
    assert result["execution_valid"] is False
    assert result["staging_valid"] is False
    assert result["candidate_bar_quarantines"] == ["UI"]
    assert result["candidate_input_issues"] == [issue.reference()]


def test_pending_gap_exact_session_exit_lazily_hydrates_persisted_candidate_issue():
    session = date(2026, 8, 10)
    issue = CandidateInputIssue.create(
        issue_id="candidate_input_issue_" + "a" * 32,
        epoch_id="gen_001-2026-08-10-" + "b" * 16,
        session=session,
        dependency_kind="reference_bar",
        reason_code="provider_error",
        ticker="UI",
        source="yfinance",
        fetched_at=datetime(2026, 8, 10, 21, tzinfo=timezone.utc),
        requested_history_digest="sha256:" + "c" * 64,
        returned_history_digest="sha256:" + "d" * 64,
        expected_sessions=(session,),
        observed_sessions=(),
        retryable=False,
        affected_signal_identities=(),
        affected_cohorts=("cohort-a",),
    )
    marker = SimpleNamespace(epoch_id=issue.epoch_id, gap_session=session)

    class Store:
        def pending_critical_gap(self):
            return marker

        def read_candidate_input_issues(self, epoch_id, exact_session):
            assert (epoch_id, exact_session) == (issue.epoch_id, session)
            return [issue]

    orchestrator = CohortOrchestrator.__new__(CohortOrchestrator)
    orchestrator._metric_store = Store()
    orchestrator._epoch_id = None
    orchestrator.cohorts = [object()]
    orchestrator._complete_pending_critical_gap = lambda marker, processed, results: {
        "cohort-a": {
            "error": True,
            "invalid_reason": "critical_market_data_gap",
            "degraded": False,
            "execution_valid": False,
            "staging_valid": False,
            "candidate_bar_quarantines": [],
        }
    }

    result = orchestrator.run_daily(session.isoformat())["cohort-a"]

    assert result["error"] is True
    assert result["degraded"] is True
    assert result["execution_valid"] is False
    assert result["staging_valid"] is False
    assert result["candidate_bar_quarantines"] == ["UI"]
    assert result["candidate_input_issues"] == [issue.reference()]


def test_reference_issue_tamper_emits_only_reconstructed_safe_scope_without_io(
    tmp_path,
):
    from test_30day_simulation import _authoritative_committee
    from test_cohort_lifecycle import (
        _CandidateRecoveryPriceSource,
        _candidate_lifecycle_orchestrator,
    )
    from tradingagents.strategies.orchestration.trading_calendar import next_session

    source = _CandidateRecoveryPriceSource("quarantined")
    orchestrator, first_session = _candidate_lifecycle_orchestrator(
        tmp_path, source
    )
    session = next_session(first_session)
    second = orchestrator.cohorts[1]
    original_stage = second["engine"].screen_and_stage
    fail_once = True

    def fail_first_stage(*args, **kwargs):
        nonlocal fail_once
        if kwargs["trading_date"] == session.isoformat() and fail_once:
            fail_once = False
            raise RuntimeError("post-resolution staging failure")
        return original_stage(*args, **kwargs)

    second["engine"].screen_and_stage = fail_first_stage
    with patch(
        "tradingagents.strategies.trading.portfolio_committee.PortfolioCommittee.synthesize",
        side_effect=_authoritative_committee,
    ):
        orchestrator.run_daily(first_session.isoformat())
        first = orchestrator.run_daily(session.isoformat())
        expected_reference = first["horizon_3m"]["candidate_input_issues"][0]
        with sqlite3.connect(orchestrator._metric_store.path) as connection:
            issue_id, payload_json = connection.execute(
                "SELECT issue_id, payload_json FROM candidate_input_issues "
                "WHERE dependency_kind = 'reference_bar'"
            ).fetchone()
            payload = json.loads(payload_json)
            payload["reason_code"] = "stale_data"
            payload["affected_cohorts"] = ["horizon_3m"]
            connection.execute(
                "UPDATE candidate_input_issues SET payload_json = ? "
                "WHERE issue_id = ?",
                (
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    issue_id,
                ),
            )
        candidate_calls = len(source.candidate_calls)
        source.resolve_candidate_daily_bars = (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("candidate resolver called after issue scope tamper")
            )
        )
        replay = orchestrator.run_daily(session.isoformat())

    assert len(source.candidate_calls) == candidate_calls
    assert replay["horizon_3m"]["error"] is True
    assert replay["horizon_3m"]["invalid_reason"] == (
        "candidate reference-bar validation failed"
    )
    assert all(
        result["candidate_input_issues"] == [expected_reference]
        for result in replay.values()
    )
    assert expected_reference["reason_code"] == "invalid_data"
    assert expected_reference["affected_cohorts"] == (
        "horizon_30d",
        "horizon_3m",
    )


def test_governed_reporting_is_deduplicated_sorted_and_bounded():
    summary = {
        "ticker": "ESS",
        "session": "2026-08-10",
        "recovery_id": "governed_bar_recovery:" + "b" * 64,
        "contract_version": "yfinance-60m-v1",
        "evidence_digest": "sha256:" + "a" * 64,
        "affected_cohort_ids": ["cohort-a", "cohort-b"],
    }
    results = {
        "cohort-b": {
            "governed_bar_recoveries": [summary, {**summary, "ticker": ""}],
            "governed_failure_map": {"SPY": "invalid_benchmark SPY/2026-08-10"},
        },
        "cohort-a": {
            "governed_bar_recoveries": [dict(summary)],
            "governed_failure_map": {
                "SPY": "invalid_benchmark SPY/2026-08-10",
                "TOKEN": "provider secret should not escape",
            },
        },
        "malformed": {
            "governed_bar_recoveries": "x" * 100_000,
            "governed_failure_map": ["not", "a", "map"],
        },
    }

    summaries, failures = aggregate_governed_reporting(results)

    assert summaries == [summary]
    assert failures == {"SPY": "invalid_benchmark SPY/2026-08-10"}
    assert "secret" not in json.dumps({"summaries": summaries, "failures": failures})


def test_governed_reporting_omits_secret_like_ticker_and_cohort_identifiers():
    baseline = {
        "ticker": "ESS",
        "session": "2026-08-10",
        "recovery_id": "governed_bar_recovery:" + "b" * 64,
        "contract_version": "yfinance-60m-v1",
        "evidence_digest": "sha256:" + "a" * 64,
        "affected_cohort_ids": ["cohort-a"],
    }
    results = {
        "cohort-a": {
            "governed_bar_recoveries": [
                baseline,
                {**baseline, "ticker": "PROVIDER SECRET"},
                {**baseline, "affected_cohort_ids": ["PASSWORD=SECRET"]},
            ],
            "governed_failure_map": {
                "PROVIDER SECRET": "invalid PROVIDER SECRET/2026-08-10"
            },
        }
    }

    summaries, failures = aggregate_governed_reporting(results)

    assert summaries == [baseline]
    assert failures == {}
    assert "SECRET" not in json.dumps({"summaries": summaries, "failures": failures})


def test_governed_reporting_accepts_canonical_ids_with_one_global_cap_and_unsafe_keys():
    results = {
        f"cohort-{index:04d}": {
            "governed_bar_recoveries": [
                {
                    "ticker": f"T{index:04d}",
                    "session": "2026-08-10",
                    "recovery_id": "governed_bar_recovery:" + f"{index:064x}",
                    "contract_version": "yfinance-60m-v1",
                    "evidence_digest": "sha256:" + f"{index:064x}",
                    "affected_cohort_ids": [f"cohort-{index:04d}"],
                }
            ]
        }
        for index in range(300)
    }
    results[object()] = {"governed_failure_map": {object(): "secret"}}

    summaries, failures = aggregate_governed_reporting(results)

    assert 0 < len(summaries) <= 256
    assert summaries == sorted(
        summaries, key=lambda row: (row["ticker"], row["session"], row["recovery_id"])
    )
    assert failures == {}


def test_worker_status_is_degraded_for_successful_governed_recovery():
    summary = {
        "ticker": "ESS",
        "session": "2026-08-10",
        "recovery_id": "governed_bar_recovery:" + "b" * 64,
        "contract_version": "yfinance-60m-v1",
        "evidence_digest": "sha256:" + "a" * 64,
        "affected_cohort_ids": ["cohort-a"],
    }
    result = {
        "cohort-a": {
            "error": False,
            "degraded": True,
            "execution_valid": True,
            "staging_valid": True,
            "candidate_bar_quarantines": [],
            "governed_bar_recoveries": [summary],
            "governed_failure_map": {},
        }
    }

    exit_code, message = run_cohorts._cohort_run_exit_status(result)

    assert exit_code == 0
    assert "recovered tickers: ESS" in message


def test_worker_status_is_degraded_for_candidate_quarantine():
    result = {
        "candidate_quarantined": {
            "error": False,
            "degraded": True,
            "execution_valid": True,
            "candidate_bar_quarantines": ["ALX"],
        }
    }

    exit_code, message = run_cohorts._cohort_run_exit_status(result)

    assert exit_code == 0
    assert "DEGRADED: 1/1 cohorts" in message


def test_worker_failure_status_retains_simultaneous_candidate_quarantine():
    result = {
        "execution_failed": {
            "error": True,
            "execution_valid": False,
            "invalid_reason": "missing governed mark",
        },
        "candidate_quarantined": {
            "error": False,
            "degraded": True,
            "execution_valid": True,
            "staging_valid": False,
            "candidate_bar_quarantines": ["ALX"],
        },
    }

    exit_code, message = run_cohorts._cohort_run_exit_status(result)

    assert exit_code == 1
    assert "ERROR: 1/2 cohorts failed: execution_failed" in message
    assert "DEGRADED: 1/2 cohorts degraded (execution valid)" in message
    assert "candidate_quarantined" in message
    assert "quarantined tickers: ALX" in message


def test_worker_failure_status_retains_degradation_on_same_cohort():
    result = {
        "candidate_replay_conflict": {
            "error": True,
            "degraded": True,
            "execution_valid": True,
            "staging_valid": False,
            "candidate_bar_quarantines": ["ALX"],
            "invalid_reason": "deterministic candidate replay identity conflict",
        }
    }

    assert count_failed_cohorts(result) == (1, 1, ["candidate_replay_conflict"])
    assert count_degraded_cohorts(result) == (
        1,
        1,
        ["candidate_replay_conflict"],
    )
    exit_code, message = run_cohorts._cohort_run_exit_status(result)
    assert exit_code == 1
    assert "ERROR: 1/1 cohorts failed" in message
    assert "DEGRADED: 1/1 cohorts degraded (execution valid)" in message
    assert "quarantined tickers: ALX" in message


def test_worker_main_exits_degraded_for_candidate_quarantine(monkeypatch, capsys):
    reference = _candidate_issue_reference(
        affected_cohorts=["candidate_quarantined"]
    )
    result = {
        "candidate_quarantined": {
            "error": False,
            "degraded": True,
            "execution_valid": True,
            "staging_valid": False,
            "candidate_bar_quarantines": ["ALX"],
            "candidate_input_issues": [reference],
        }
    }

    class FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            pass

        def run_daily(self, trading_date):
            return result

    monkeypatch.setattr(sys, "argv", ["run_cohorts.py", "--date", "2026-08-10"])
    monkeypatch.setenv("EVENTEDGE_GENERATION_ID", "gen_001")
    monkeypatch.setenv("EVENTEDGE_GENERATION_COMMIT", "synthetic-commit")
    monkeypatch.setattr(
        "tradingagents.strategies.orchestration.cohort_orchestrator.build_default_cohorts",
        lambda config: [],
    )
    monkeypatch.setattr(
        "tradingagents.strategies.orchestration.cohort_orchestrator.CohortOrchestrator",
        FakeOrchestrator,
    )

    run_cohorts.main()

    captured = capsys.readouterr()
    assert "DEGRADED: 1/1 cohorts" in captured.err
    wire_lines = [
        line for line in captured.out.splitlines() if line.startswith(_DAILY_RESULT_PREFIX)
    ]
    assert len(wire_lines) == 1
    assert json.loads(wire_lines[0].removeprefix(_DAILY_RESULT_PREFIX)) == {
        "wire_version": _DAILY_RESULT_WIRE_VERSION,
        "cohort_results": result,
    }


def test_worker_main_rejects_malformed_issue_before_printing_wire(
    monkeypatch, capsys
):
    result = {
        "cohort-a": {
            "error": False,
            "degraded": True,
            "execution_valid": True,
            "staging_valid": False,
            "candidate_input_issues": [
                    {
                        **_candidate_issue_reference(affected_cohorts=["cohort-a"]),
                        "issue_id": "candidate_input_issue_provider-secret-do-not-print",
                }
            ],
        }
    }

    class FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            pass

        def run_daily(self, trading_date):
            return result

    monkeypatch.setattr(sys, "argv", ["run_cohorts.py", "--date", "2026-08-10"])
    monkeypatch.setenv("EVENTEDGE_GENERATION_ID", "gen_001")
    monkeypatch.setenv("EVENTEDGE_GENERATION_COMMIT", "synthetic-commit")
    monkeypatch.setattr(
        "tradingagents.strategies.orchestration.cohort_orchestrator.build_default_cohorts",
        lambda config: [],
    )
    monkeypatch.setattr(
        "tradingagents.strategies.orchestration.cohort_orchestrator.CohortOrchestrator",
        FakeOrchestrator,
    )

    with pytest.raises(SystemExit) as raised:
        run_cohorts.main()

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert _DAILY_RESULT_PREFIX not in captured.out
    assert "provider-secret-do-not-print" not in captured.out + captured.err
    assert "invalid cohort reporting payload" in captured.err


# --- _extract_cohort_results (parse run_cohorts.py stdout) ---


def test_extract_cohort_results_from_mixed_stdout():
    results = {"h_30d_5k": {"error": True}, "h_30d_10k": {"signals": []}}
    stdout = (
        "2026-06-01 10:00 INFO Registered data source: yfinance\n"
        "Selected: disaggregated_fut\n"
        "\nDaily trading completed for 2026-06-01 in 560.8s\n"
        + _DAILY_RESULT_PREFIX
        + json.dumps(
            {
                "wire_version": _DAILY_RESULT_WIRE_VERSION,
                "cohort_results": results,
            }
        )
        + "\n"
    )
    extracted = _extract_cohort_results(stdout)
    assert extracted == results
    assert count_failed_cohorts(extracted)[0] == 1


def test_extract_cohort_results_garbage_returns_none():
    assert _extract_cohort_results("no json here\njust logs\n") is None
    assert _extract_cohort_results("") is None


def test_daily_worker_wire_constants_are_exact_and_shared():
    from tradingagents.strategies.orchestration import run_outcome

    assert getattr(run_outcome, "DAILY_RESULT_PREFIX", None) == _DAILY_RESULT_PREFIX
    assert (
        getattr(run_outcome, "DAILY_RESULT_WIRE_VERSION", None)
        == _DAILY_RESULT_WIRE_VERSION
    )
    assert getattr(run_outcome, "DAILY_RESULT_ENVELOPE_KEYS", None) == frozenset(
        {"wire_version", "cohort_results"}
    )


def test_reset_refuses_before_constructing_ledger_orchestrator(monkeypatch, capsys):
    """A reset cannot delete P0 ledgers or retain a mismatched metric store."""

    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("ledger-backed orchestrator must not be constructed")

    monkeypatch.setattr(sys, "argv", ["run_cohorts.py", "--reset"])
    monkeypatch.setenv("EVENTEDGE_GENERATION_ID", "gen_004")
    monkeypatch.setenv("EVENTEDGE_GENERATION_COMMIT", "abc123")
    monkeypatch.setattr(
        "tradingagents.strategies.orchestration.cohort_orchestrator.CohortOrchestrator",
        fail_if_constructed,
    )

    with pytest.raises(SystemExit) as error:
        run_cohorts.main()

    assert error.value.code == 2
    assert (
        "reset is disabled for ledger-backed generation state"
        in capsys.readouterr().err
    )


def test_orchestrator_reset_refuses_before_deleting_ledger_state():
    orchestrator = CohortOrchestrator.__new__(CohortOrchestrator)
    state = Mock()
    orchestrator.cohorts = [{"state": state}]

    with pytest.raises(
        RuntimeError, match="reset is disabled for ledger-backed generation state"
    ):
        orchestrator.reset()

    state.reset.assert_not_called()


# --- the core defect: rc==0 but cohorts failed => success False ---


class _FakeProc:
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_DAILY_COHORT_NAMES = tuple(cohort.name for cohort in build_default_cohorts({}))
_DAILY_RESULT_PREFIX = "EVENTEDGE_DAILY_RESULT_V1="
_DAILY_RESULT_WIRE_VERSION = 1


def _clean_daily_results():
    return {
        name: {
            "error": False,
            "degraded": False,
            "execution_valid": True,
            "staging_valid": True,
        }
        for name in _DAILY_COHORT_NAMES
    }


def _daily_results(payload_kind):
    results = _clean_daily_results()
    if payload_kind == "degraded":
        results[_DAILY_COHORT_NAMES[0]].update(
            {
                "degraded": True,
                "staging_valid": False,
                "candidate_bar_quarantines": ["ALX"],
            }
        )
    return results


def _worker_stdout(results, *, envelope=None):
    payload = envelope or {
        "wire_version": _DAILY_RESULT_WIRE_VERSION,
        "cohort_results": results,
    }
    return "done\n" + _DAILY_RESULT_PREFIX + json.dumps(payload) + "\n"


def _run_with_proc(tmp_path, proc):
    mgr = GenerationManager.__new__(GenerationManager)  # bypass __init__
    mgr._venv_python = "python"
    gen_data = {
        "gen_id": "gen_001",
        "git_commit": "synthetic-commit-gen-001",
        "state_dir": str(tmp_path / "state"),
        "worktree_path": str(tmp_path / "wt"),
    }
    with (
        patch(
            "tradingagents.strategies.orchestration.generation_manager.subprocess.run",
            return_value=proc,
        ),
        patch.object(GenerationManager, "_write_run_log", lambda self, *a, **k: None),
    ):
        return mgr._run_cohorts_subprocess(gen_data, ["--date", "2026-08-10"])


def test_rc0_but_all_cohorts_failed_is_marked_failed(tmp_path):
    results = {name: {"error": True} for name in _DAILY_COHORT_NAMES}
    stdout = _worker_stdout(results)
    result = _run_with_proc(tmp_path, _FakeProc(0, stdout))
    assert result["success"] is False
    assert result["outcome"] == "failed"
    assert "16/16 cohorts failed" in result["error"]


def test_rc0_partial_failure_is_marked_failed(tmp_path):
    results = _clean_daily_results()
    results[_DAILY_COHORT_NAMES[0]] = {"error": True}
    stdout = _worker_stdout(results)
    result = _run_with_proc(tmp_path, _FakeProc(0, stdout))
    assert result["success"] is False
    assert result["outcome"] == "failed"
    assert "1/16 cohorts failed" in result["error"]


def test_rc0_all_success_stays_success(tmp_path):
    results = _clean_daily_results()
    stdout = _worker_stdout(results)
    result = _run_with_proc(tmp_path, _FakeProc(0, stdout))
    assert result["success"] is True
    assert result["outcome"] == "clean"


def test_degraded_worker_result_preserves_execution_validity(tmp_path):
    results = _daily_results("degraded")
    stdout = _worker_stdout(results)

    result = _run_with_proc(tmp_path, _FakeProc(2, stdout, "DEGRADED: candidate data"))

    assert result["success"] is False
    assert result["outcome"] == "degraded"
    assert result["degraded"] is True
    assert result["execution_valid"] is True
    assert "1/16 cohorts degraded" in result["error"]


def test_generation_status_labels_governed_recovery_without_candidate_quarantine(
    tmp_path,
):
    summary = {
        "ticker": "ESS",
        "session": "2026-08-10",
        "recovery_id": "governed_bar_recovery:" + "b" * 64,
        "contract_version": "yfinance-60m-v1",
        "evidence_digest": "sha256:" + "a" * 64,
        "affected_cohort_ids": [_DAILY_COHORT_NAMES[0]],
    }
    results = _daily_results("degraded")
    results[_DAILY_COHORT_NAMES[0]].update(
        {"candidate_bar_quarantines": [], "governed_bar_recoveries": [summary]}
    )
    stdout = _worker_stdout(results)

    result = _run_with_proc(tmp_path, _FakeProc(2, stdout, "degraded"))

    assert "governed bar recovery" in result["error"]
    assert "candidate data quarantined" not in result["error"]


def test_generation_status_labels_mixed_candidate_and_governed_degradation(tmp_path):
    summary = {
        "ticker": "ESS",
        "session": "2026-08-10",
        "recovery_id": "governed_bar_recovery:" + "b" * 64,
        "contract_version": "yfinance-60m-v1",
        "evidence_digest": "sha256:" + "a" * 64,
        "affected_cohort_ids": [_DAILY_COHORT_NAMES[0]],
    }
    results = _daily_results("degraded")
    results[_DAILY_COHORT_NAMES[0]]["governed_bar_recoveries"] = [summary]
    stdout = _worker_stdout(results)

    result = _run_with_proc(tmp_path, _FakeProc(2, stdout, "degraded"))

    assert "candidate data quarantined; governed bar recovery" in result["error"]


def test_failed_worker_result_preserves_simultaneous_degradation(tmp_path):
    results = _clean_daily_results()
    results[_DAILY_COHORT_NAMES[0]] = {
        "error": True,
        "execution_valid": False,
        "invalid_reason": "missing governed mark",
    }
    results[_DAILY_COHORT_NAMES[1]].update(
        {
            "degraded": True,
            "staging_valid": False,
            "candidate_bar_quarantines": ["ALX"],
        }
    )
    stdout = _worker_stdout(results)

    result = _run_with_proc(tmp_path, _FakeProc(1, stdout, "ERROR and DEGRADED"))

    assert result["success"] is False
    assert result["outcome"] == "failed"
    assert result["degraded"] is True
    assert result["execution_valid"] is False
    assert result["candidate_bar_quarantines"] == ["ALX"]
    assert "1/16 cohorts failed" in result["error"]
    assert "1/16 cohorts degraded" in result["error"]


def test_failed_worker_result_preserves_same_cohort_degradation(tmp_path):
    results = _clean_daily_results()
    results[_DAILY_COHORT_NAMES[0]] = {
        "error": True,
        "degraded": True,
        "execution_valid": True,
        "staging_valid": False,
        "candidate_bar_quarantines": ["ALX"],
        "invalid_reason": "deterministic candidate replay identity conflict",
    }
    stdout = _worker_stdout(results)

    result = _run_with_proc(tmp_path, _FakeProc(1, stdout, "ERROR and DEGRADED"))

    assert result["success"] is False
    assert result["outcome"] == "failed"
    assert result["degraded"] is True
    assert result["execution_valid"] is True
    assert result["candidate_bar_quarantines"] == ["ALX"]
    assert "1/16 cohorts failed" in result["error"]
    assert "1/16 cohorts degraded" in result["error"]


def test_issue_only_worker_result_is_degraded_with_one_run_level_summary(tmp_path):
    results = _clean_daily_results()
    cohorts = sorted(results)
    reference = _candidate_issue_reference(affected_cohorts=cohorts)
    for cohort_result in results.values():
        cohort_result.update(
            {
                "degraded": True,
                "staging_valid": False,
                "candidate_input_issues": [dict(reference)],
            }
        )

    result = _run_with_proc(tmp_path, _FakeProc(2, _worker_stdout(results)))

    assert result["outcome"] == "degraded"
    assert result["execution_valid"] is True
    assert result["candidate_input_issues"] == [reference]


def test_candidate_issue_plus_real_failure_remains_failed(tmp_path):
    results = _clean_daily_results()
    cohorts = sorted(results)
    reference = _candidate_issue_reference(affected_cohorts=cohorts)
    for cohort_result in results.values():
        cohort_result.update(
            {
                "degraded": True,
                "staging_valid": False,
                "candidate_input_issues": [dict(reference)],
            }
        )
    results[cohorts[0]].update(
        {"error": True, "execution_valid": False, "invalid_reason": "real failure"}
    )

    result = _run_with_proc(tmp_path, _FakeProc(1, _worker_stdout(results)))

    assert result["outcome"] == "failed"
    assert result["candidate_input_issues"] == [reference]


def test_malformed_candidate_issue_wire_fails_closed(tmp_path):
    results = _clean_daily_results()
    cohorts = sorted(results)
    reference = _candidate_issue_reference(affected_cohorts=cohorts)
    for cohort_result in results.values():
        cohort_result.update(
            {
                "degraded": True,
                "staging_valid": False,
                "candidate_input_issues": [dict(reference)],
            }
        )
    results[cohorts[-1]]["candidate_input_issues"][0]["reason_code"] = "unknown"

    result = _run_with_proc(tmp_path, _FakeProc(2, _worker_stdout(results)))

    assert result["outcome"] == "failed"
    assert result["error"] == "invalid daily worker result"


def test_nonzero_rc_still_failed(tmp_path):
    result = _run_with_proc(tmp_path, _FakeProc(1, "boom\n", "traceback\n"))
    assert result["success"] is False
    assert result["outcome"] == "failed"


@pytest.mark.parametrize(
    ("returncode", "payload_kind", "expected"),
    (
        (0, "clean", "clean"),
        (0, "degraded", "degraded"),
        (2, "degraded", "degraded"),
        (1, "degraded", "failed"),
        (2, "clean", "failed"),
        (1, "clean", "failed"),
    ),
)
def test_worker_return_code_and_payload_must_agree(
    tmp_path, returncode, payload_kind, expected
):
    stdout = _worker_stdout(_daily_results(payload_kind))
    result = _run_with_proc(tmp_path, _FakeProc(returncode, stdout))
    assert result["outcome"] == expected


@pytest.mark.parametrize(
    "invalid_kind",
    (
        "empty",
        "unknown_cohort",
        "missing_cohort",
        "non_mapping",
        "missing_error",
        "non_boolean_error",
        "missing_degraded",
        "missing_execution_valid",
        "missing_staging_valid",
        "non_boolean_degraded",
        "non_boolean_optional_error_lifecycle",
    ),
)
def test_rc0_rejects_invalid_daily_worker_schema(tmp_path, invalid_kind):
    results = _clean_daily_results()
    first = _DAILY_COHORT_NAMES[0]
    if invalid_kind == "empty":
        results = {}
    elif invalid_kind == "unknown_cohort":
        results["unknown_cohort"] = results.pop(first)
    elif invalid_kind == "missing_cohort":
        results.pop(first)
    elif invalid_kind == "non_mapping":
        results[first] = "not a mapping"
    elif invalid_kind == "missing_error":
        results[first].pop("error")
    elif invalid_kind == "non_boolean_error":
        results[first]["error"] = 0
    elif invalid_kind == "missing_degraded":
        results[first].pop("degraded")
    elif invalid_kind == "missing_execution_valid":
        results[first].pop("execution_valid")
    elif invalid_kind == "missing_staging_valid":
        results[first].pop("staging_valid")
    elif invalid_kind == "non_boolean_degraded":
        results[first]["degraded"] = "false"
    elif invalid_kind == "non_boolean_optional_error_lifecycle":
        results[first] = {"error": True, "execution_valid": 1}

    stdout = _worker_stdout(results)
    result = _run_with_proc(tmp_path, _FakeProc(0, stdout))
    assert result["outcome"] == "failed"
    assert result["success"] is False
    assert result["error"] == "invalid daily worker result"


def test_rc0_rejects_unrelated_trailing_json(tmp_path):
    stdout = _worker_stdout(_clean_daily_results()) + json.dumps({"unrelated": True})
    result = _run_with_proc(tmp_path, _FakeProc(0, stdout))
    assert result["outcome"] == "failed"
    assert result["success"] is False


def test_rc0_rejects_trailing_valid_looking_decoy(tmp_path):
    decoy = {
        "wire_version": _DAILY_RESULT_WIRE_VERSION,
        "cohort_results": _clean_daily_results(),
    }
    stdout = _worker_stdout(_clean_daily_results()) + json.dumps(decoy) + "\n"
    result = _run_with_proc(tmp_path, _FakeProc(0, stdout))
    assert result["outcome"] == "failed"


@pytest.mark.parametrize(
    "stdout",
    (
        json.dumps(_clean_daily_results()),
        "done\n",
        _DAILY_RESULT_PREFIX + "not-json\n",
        _worker_stdout(
            _clean_daily_results(),
            envelope={
                "wire_version": 2,
                "cohort_results": _clean_daily_results(),
            },
        ),
        _worker_stdout(
            _clean_daily_results(),
            envelope={"cohort_results": _clean_daily_results()},
        ),
        _worker_stdout(
            _clean_daily_results(),
            envelope={
                "wire_version": _DAILY_RESULT_WIRE_VERSION,
                "cohort_results": _clean_daily_results(),
                "extra": True,
            },
        ),
        _worker_stdout(
            _clean_daily_results(),
            envelope={"wire_version": _DAILY_RESULT_WIRE_VERSION},
        ),
        _worker_stdout(_clean_daily_results())
        + _DAILY_RESULT_PREFIX
        + json.dumps(
            {
                "wire_version": _DAILY_RESULT_WIRE_VERSION,
                "cohort_results": _clean_daily_results(),
            }
        )
        + "\n",
    ),
)
def test_rc0_rejects_invalid_daily_worker_envelope(tmp_path, stdout):
    result = _run_with_proc(tmp_path, _FakeProc(0, stdout))
    assert result["outcome"] == "failed"
    assert result["success"] is False


@pytest.mark.parametrize(
    "failure_marker",
    (
        {"valid": False},
        {"invalid_reason": "governed lifecycle invalid"},
    ),
)
def test_legacy_failure_markers_force_failed_outcome(tmp_path, failure_marker):
    results = _clean_daily_results()
    results[_DAILY_COHORT_NAMES[0]].update(failure_marker)
    result = _run_with_proc(tmp_path, _FakeProc(0, _worker_stdout(results)))
    assert result["outcome"] == "failed"


def test_error_cohort_cannot_claim_valid_staging(tmp_path):
    results = _clean_daily_results()
    results[_DAILY_COHORT_NAMES[0]] = {"error": True, "staging_valid": True}
    result = _run_with_proc(tmp_path, _FakeProc(0, _worker_stdout(results)))
    assert result["outcome"] == "failed"
    assert result["error"] == "invalid daily worker result"


def test_worker_exception_is_failed(tmp_path):
    mgr = GenerationManager.__new__(GenerationManager)
    mgr._venv_python = "python"
    gen_data = {
        "gen_id": "gen_001",
        "git_commit": "synthetic-commit-gen-001",
        "state_dir": str(tmp_path / "state"),
        "worktree_path": str(tmp_path / "wt"),
    }
    with patch(
        "tradingagents.strategies.orchestration.generation_manager.subprocess.run",
        side_effect=RuntimeError("worker launch failed"),
    ):
        result = mgr._run_cohorts_subprocess(gen_data, ["--date", "2026-06-01"])

    assert result["outcome"] == "failed"
    assert result["success"] is False
