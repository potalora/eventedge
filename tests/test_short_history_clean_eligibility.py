from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import socket
import sqlite3

import pytest

from scripts import recover_runtime as controller
from test_candidate_issue_reporting_volume import COHORTS as VOLUME_COHORTS, SESSION as VOLUME_SESSION, reference
from test_operational_report import (
    COHORTS as REPORT_COHORTS,
    COMMIT,
    EPOCH,
    GENERATION,
    SESSION as REPORT_SESSION,
    _attempt,
    _insert,
    _report,
    native_evidence,  # noqa: F401
)
from test_shared_policy_volatility_evidence import SESSION as STAGE_SESSION, _history, _run_staging_matrix
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.orchestration import runtime_recovery as recovery
from tradingagents.strategies.orchestration.candidate_inputs import (
    CandidateInputIssue,
    CandidateInputReasonCode,
    candidate_acceptance_payload,
    containment_key,
    short_history_only,
)
from tradingagents.strategies.orchestration import daily_pipeline
from tradingagents.strategies.orchestration.daily_pipeline import summarize_cohort_results
from tradingagents.strategies.orchestration.generation_manager import (
    _normalize_daily_candidate_issues,
    _valid_daily_cohort_results,
)
from tradingagents.strategies.orchestration.operating_contract import (
    POLICY,
    OperatingProfile,
    evaluate_completion,
)
from tradingagents.strategies.orchestration.operational_report import (
    EXPECTED_COHORTS,
    render_operational_report,
)
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
from tradingagents.strategies.state.generation_db import GENERATION_DB_FILENAME


@pytest.fixture(autouse=True)
def _block_network_sockets(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("network socket access is forbidden in offline containment tests")

    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)


@pytest.mark.parametrize(
    ("case", "expect_clean"),
    [
        ("single_short_history", True),
        ("two_short_history", True),
        ("missing_data", False),
        ("provider_error", False),
        ("stale_data", False),
        ("invalid_data", False),
        ("reference_bar", False),
        ("mixed_short_and_provider", False),
        ("clean_marked_degraded", False),
    ],
)
def test_clean_summary_preserves_only_short_history(case, expect_clean):
    first = reference(0, VOLUME_COHORTS)
    first["reason_code"] = "insufficient_history"
    issues = [first]
    extra_fields: dict[str, object] = {}
    if case == "two_short_history":
        second = reference(1, VOLUME_COHORTS)
        second["reason_code"] = "insufficient_history"
        issues.append(second)
    elif case in {"missing_data", "provider_error", "stale_data", "invalid_data"}:
        first["reason_code"] = case
    elif case == "reference_bar":
        first["dependency_kind"] = "reference_bar"
        first["reason_code"] = "missing_data"
    elif case == "mixed_short_and_provider":
        second = reference(1, VOLUME_COHORTS)
        second["reason_code"] = "provider_error"
        issues.append(second)
    elif case == "clean_marked_degraded":
        extra_fields["degraded"] = True

    result = {
        "outcome": "clean",
        "success": True,
        "execution_valid": True,
        "input_coverage_valid": True,
        "candidate_input_issues": issues,
        "elapsed_s": 1,
        **extra_fields,
    }
    normalized = _normalize_daily_candidate_issues(deepcopy(result), VOLUME_SESSION)
    if expect_clean:
        assert normalized == result
    else:
        assert normalized["outcome"] == "failed"


@pytest.mark.parametrize("short_tickers", [("UI",), ("UI", "ZKH")])
def test_short_history_candidates_stay_clean_across_16_books_and_replay(
    tmp_path, monkeypatch, short_tickers
):
    histories = {
        "OPEN": _history(0.021),
        "PENDING": _history(0.027),
        "NCL": _history(0.033),
    }
    for index, ticker in enumerate(short_tickers):
        histories[ticker] = _history(0.031 + index * 0.002).tail(12 + index * 5)

    staged = {}
    orchestrator, results, calls = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        histories,
        signal_tickers=("NCL", *short_tickers),
        stage_observations=staged,
    )
    try:
        assert len(results) == len(staged) == 16
        assert orchestrator._epoch_id is not None
        issues = orchestrator._metric_store.read_candidate_input_issues(
            orchestrator._epoch_id, STAGE_SESSION
        )
        assert [(issue.ticker, issue.dependency_kind, issue.reason_code) for issue in issues] == [
            (ticker, "volatility_history", "insufficient_history")
            for ticker in sorted(short_tickers)
        ]
        expected_refs = [issue.reference() for issue in issues]
        assert short_history_only(expected_refs)
        assert all(
            not row["error"]
            and row["execution_valid"]
            and row["staging_valid"]
            and not row["degraded"]
            and row["candidate_input_issues"] == expected_refs
            for row in results.values()
        )
        summary = summarize_cohort_results(results, STAGE_SESSION.isoformat())
        assert summary.outcome == "clean"
        assert summary.degraded == ()
        assert summary.failed == ()
        assert summary.execution_valid is True
        assert summary.input_coverage_valid is True
        assert [row["ticker"] for row in summary.candidate_input_issues] == sorted(short_tickers)

        assert _valid_daily_cohort_results(results, STAGE_SESSION.isoformat())

        unproved = deepcopy(results)
        for row in unproved.values():
            row.pop("candidate_input_acceptance", None)
        assert not _valid_daily_cohort_results(unproved, STAGE_SESSION.isoformat())

        if len(short_tickers) > 1:
            duplicated = deepcopy(results)
            for row in duplicated.values():
                row["candidate_input_acceptance"] = [
                    row["candidate_input_acceptance"][0],
                    deepcopy(row["candidate_input_acceptance"][0]),
                ]
            assert not _valid_daily_cohort_results(duplicated, STAGE_SESSION.isoformat())

        assert all(
            row == {"signals": ("NCL",), "reference_bars": ("NCL",)}
            for row in staged.values()
        )
        assert orchestrator.cohorts[0]["state"].load_latest_regime()["staging_valid"] is True

        excluded = set(short_tickers)
        for cohort in orchestrator.cohorts:
            ledger = cohort["ledger"]
            binding = ledger.read_policy_session_context(STAGE_SESSION)
            assert set(binding["context"]["annualized_volatility"]) == {"OPEN", "PENDING", "NCL"}
            assert not any(
                row["ticker"] in excluded
                for row in ledger.read_policy_candidate_decisions(
                    STAGE_SESSION, STAGE_SESSION, epoch_id=orchestrator._epoch_id
                )
            )
            assert not any(
                signal.ticker in excluded
                for signal in ledger.read_signals(
                    STAGE_SESSION, STAGE_SESSION, epoch_id=orchestrator._epoch_id
                )
            )
            for ticker in excluded:
                assert (
                    ledger._connection.execute(
                        "SELECT COUNT(*) FROM marks WHERE ticker = ?", (ticker,)
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    ledger._connection.execute(
                        "SELECT COUNT(*) FROM order_intents i JOIN intent_signals link "
                        "ON link.intent_id=i.intent_id JOIN signals s ON s.signal_id=link.signal_id "
                        "WHERE s.ticker=?",
                        (ticker,),
                    ).fetchone()[0]
                    == 0
                )
            assert ledger._connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0

        # Replay remains clean without fresh provider calls even if cache later becomes full.
        for ticker in short_tickers:
            orchestrator.cohorts[0]["engine"]._price_cache[ticker] = _history(0.031)
        calls_before = list(calls)
        replay = orchestrator.run_daily(STAGE_SESSION.isoformat())
        assert calls == calls_before
        assert all(
            not row["error"]
            and row["staging_valid"]
            and not row["degraded"]
            and row["candidate_input_issues"] == expected_refs
            for row in replay.values()
        )
        assert _valid_daily_cohort_results(replay, STAGE_SESSION.isoformat())
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


@pytest.mark.parametrize("fault", ["missing_data", "mid_series_gap", "stale_data", "provider_error"])
def test_mixed_short_history_and_genuine_fault_fails_closed(tmp_path, monkeypatch, fault):
    base_zkh = _history(0.035)
    if fault == "missing_data":
        fault_frame = base_zkh.iloc[:0]
    elif fault == "mid_series_gap":
        fault_frame = base_zkh.tail(15).drop(base_zkh.tail(15).index[6])
    elif fault == "stale_data":
        fault_frame = base_zkh.tail(15).iloc[:-1]
    else:
        fault_frame = base_zkh.iloc[:0]

    if fault == "provider_error":
        original_validate = daily_pipeline.run_candidate_volatility_validation

        def validate_with_zkh_error(state):
            service = state.first_engine.data_acquisition
            original_fetch = service.fetch_missing_prices

            def fetch_with_error(tickers, start, end, *, strict=False):
                if tuple(tickers) == ("ZKH",):
                    raise SourceFetchError(
                        "fixture isolated provider error",
                        reason_code="invalid_response",
                        failed_operations={"ZKH": "invalid_response"},
                    )
                return original_fetch(tickers, start, end, strict=strict)

            monkeypatch.setattr(service, "fetch_missing_prices", fetch_with_error)
            return original_validate(state)

        monkeypatch.setattr(
            daily_pipeline, "run_candidate_volatility_validation", validate_with_zkh_error
        )

    staged = {}
    orchestrator, results, _ = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        {
            "OPEN": _history(0.021),
            "PENDING": _history(0.027),
            "NCL": _history(0.033),
            "UI": _history(0.031).tail(12),
            "ZKH": fault_frame,
        },
        signal_tickers=("NCL", "UI", "ZKH"),
        stage_observations=staged,
    )
    try:
        assert orchestrator._epoch_id is not None
        issues = orchestrator._metric_store.read_candidate_input_issues(
            orchestrator._epoch_id, STAGE_SESSION
        )
        by_ticker = {issue.ticker: issue.reason_code for issue in issues}
        expected_fault_reason = (
            "missing_data"
            if fault == "mid_series_gap"
            else "invalid_data"
            if fault == "provider_error"
            else fault
        )
        assert by_ticker == {"UI": "insufficient_history", "ZKH": expected_fault_reason}
        assert len(results) == 16
        assert all(
            not row["error"] and row["degraded"] is True and row["staging_valid"] is False
            for row in results.values()
        )
        summary = summarize_cohort_results(results, STAGE_SESSION.isoformat())
        assert summary.outcome == "degraded"
        assert len(summary.degraded) == 16
        assert orchestrator.cohorts[0]["state"].load_latest_regime()["staging_valid"] is False
        assert all(
            "UI" not in row["signals"] and "ZKH" not in row["signals"]
            for row in staged.values()
        )
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


def _make_short_history_issue(
    ticker: str,
    suffix_char: str,
    *,
    reason_code: CandidateInputReasonCode = "insufficient_history",
):
    return CandidateInputIssue.create(
        issue_id="candidate_input_issue_" + suffix_char * 32,
        epoch_id=EPOCH,
        session=date.fromisoformat(REPORT_SESSION),
        dependency_kind="volatility_history",
        reason_code=reason_code,
        ticker=ticker,
        source="yfinance",
        fetched_at=datetime(2026, 10, 6, 20, tzinfo=timezone.utc),
        requested_history_digest="sha256:" + "a" * 64,
        returned_history_digest="sha256:" + suffix_char * 64,
        expected_sessions=(date(2026, 10, 2), date(2026, 10, 5)),
        observed_sessions=(date(2026, 10, 5),) if reason_code == "insufficient_history" else (),
        retryable=False,
        affected_signal_identities=({"event_key": f"{ticker.lower()}-event", "strategy": "earnings_call"},),
        affected_cohorts=REPORT_COHORTS,
    )


@pytest.mark.parametrize(
    "mode",
    ["single_short", "two_short", "two_short_unproved", "mixed_short_and_missing"],
)
def test_operational_report_and_controller_handle_short_history_cleanly(native_evidence, mode):
    repo, state, wire = native_evidence
    if mode == "single_short":
        issues = [_make_short_history_issue("UI", "d")]
    elif mode in {"two_short", "two_short_unproved"}:
        issues = [_make_short_history_issue("UI", "d"), _make_short_history_issue("ZKH", "e")]
    else:
        issues = [
            _make_short_history_issue("UI", "d"),
            _make_short_history_issue("ZKH", "e", reason_code="missing_data"),
        ]

    refs = []
    for issue in issues:
        ref = dict(issue.reference())
        ref["affected_cohorts"] = list(issue.affected_cohorts)
        refs.append(ref)

    db_path = state / GENERATION_DB_FILENAME
    acceptance_by_cohort: dict[str, list[dict]] = {name: [] for name in REPORT_COHORTS}
    with sqlite3.connect(db_path) as connection:
        for issue in issues:
            _insert(
                connection,
                "candidate_input_issues",
                issue_id=issue.issue_id,
                epoch_id=EPOCH,
                session=REPORT_SESSION,
                dependency_kind=issue.dependency_kind,
                ticker=issue.ticker,
                payload_json=issue.canonical_payload(),
            )
            if mode in {"single_short", "two_short"}:
                for name in REPORT_COHORTS:
                    payload = candidate_acceptance_payload(connection, issue, name)
                    connection.execute(
                        "INSERT INTO schema_metadata(metadata_key,metadata_value) VALUES (?,?)",
                        (containment_key(issue, name), payload),
                    )
                    acceptance_by_cohort[name].append(json.loads(payload))
        connection.commit()

    vol_path = next((state / "source_inputs/staging_volatility").glob("*.json"))
    envelope = SourceInputStore.decode(vol_path.read_text())
    envelope["payload"]["quarantined_tickers"] = tuple(sorted(issue.ticker for issue in issues))
    envelope["digest"] = hashlib.sha256(
        SourceInputStore.encode(envelope["payload"]).encode()
    ).hexdigest()
    vol_path.write_text(SourceInputStore.encode(envelope))

    for name, row in wire.items():
        if mode in {"single_short", "two_short"}:
            proofs = acceptance_by_cohort[name]
            row.update(
                degraded=False,
                staging_valid=True,
                candidate_input_issues=refs,
                candidate_input_acceptance=proofs[0] if len(proofs) == 1 else proofs,
            )
        elif mode == "two_short_unproved":
            row.update(degraded=True, staging_valid=True, candidate_input_issues=refs)
        else:
            row.update(degraded=True, staging_valid=False, candidate_input_issues=refs)

    attempt_outcome = "clean" if mode in {"single_short", "two_short"} else "degraded"
    _attempt(repo, wire, outcome=attempt_outcome)
    report = _report(repo)
    markdown = render_operational_report(report)

    profile = OperatingProfile(
        version=POLICY,
        roster=tuple(sorted(EXPECTED_COHORTS)),
        profile_sha256="c" * 64,
        horizon_policies={},
        strategies=(),
        supported_strategies=(),
        declared_exclusions={},
        exclusion_scope_sha256="",
        config={},
        ledger_policy_id="",
    )

    if mode in {"single_short", "two_short"}:
        assert report["evidence_complete"] is True, report["diagnostics"]
        assert report["accounting_valid"] is True
        assert report["staging_valid"] is True
        assert report["outcome"] == "clean"
        assert report["performance_claims_withheld"] is False
        assert report["candidate_input_issues"] == refs
        assert "Outcome: **clean**" in markdown
        for issue in issues:
            assert issue.ticker in markdown
            assert "insufficient_history" in markdown
        assert controller._terminal_report(report) == recovery._result("recovered", "complete")
        completion = evaluate_completion(profile, {"report": report})
        assert completion["workflow_complete"] is True
        assert completion["continuity_eligible"] is True
        assert completion["reason_codes"] == []
        report["completion"] = completion
        assert controller._terminal_report(report) == recovery._result("recovered", "complete")
    elif mode == "two_short_unproved":
        assert report["evidence_complete"] is False
        assert report["staging_valid"] is False
        assert report["performance_claims_withheld"] is True
    else:
        assert report["evidence_complete"] is True, report["diagnostics"]
        assert report["outcome"] == "degraded"
        assert report["staging_valid"] is False
        assert controller._terminal_report(report) == recovery._result(
            "wait_next_session", "accepted_outcome"
        )
        completion = evaluate_completion(profile, {"report": report})
        assert completion["workflow_complete"] is False
        assert completion["continuity_eligible"] is False
        assert "staging_invalid" in completion["reason_codes"]
        report["completion"] = completion
        assert controller._terminal_report(report) == recovery._result(
            "requires_user", "checkpoint_invalid"
        )


def test_errored_cohort_with_short_history_skips_still_aggregates_and_validates():
    ref = reference(0, VOLUME_COHORTS)
    ref["reason_code"] = "insufficient_history"
    results = {
        name: {
            "error": True,
            "invalid_reason": "decision_inputs_invalid",
            "degraded": False,
            "execution_valid": True,
            "staging_valid": False,
            "input_coverage_valid": True,
            "source_health_failures": [],
            "candidate_bar_quarantines": [],
            "candidate_input_issues": [deepcopy(ref)],
        }
        for name in VOLUME_COHORTS
    }
    summary = summarize_cohort_results(results, VOLUME_SESSION)
    assert summary.outcome == "failed"
    assert len(summary.failed) == 16
    assert summary.degraded == ()
    assert list(summary.candidate_input_issues) == [ref]
    assert _valid_daily_cohort_results(results, VOLUME_SESSION)
    normalized = _normalize_daily_candidate_issues(
        {
            "outcome": "failed",
            "success": False,
            "execution_valid": True,
            "input_coverage_valid": True,
            "candidate_input_issues": [deepcopy(ref)],
            "elapsed_s": 1.0,
            "error": "16/16 cohorts failed",
        },
        VOLUME_SESSION,
    )
    assert normalized["outcome"] == "failed"
    assert normalized["candidate_input_issues"] == [ref]


def test_degraded_multi_short_history_wire_requires_valid_acceptance_proofs():
    refs = []
    for idx in range(2):
        item = reference(idx, VOLUME_COHORTS)
        item["reason_code"] = "insufficient_history"
        refs.append(item)
    base = {
        name: {
            "error": False,
            "degraded": True,
            "execution_valid": True,
            "staging_valid": True,
            "input_coverage_valid": True,
            "source_health_failures": [],
            "candidate_bar_quarantines": [],
            "candidate_input_issues": deepcopy(refs),
        }
        for name in VOLUME_COHORTS
    }
    assert not _valid_daily_cohort_results(base, VOLUME_SESSION)

    proved = deepcopy(base)
    for name, row in proved.items():
        row["candidate_input_acceptance"] = [
            {
                "version": 1,
                "issue_sha256": f"{index:064x}",
                "staging": [
                    "stage-1",
                    name,
                    VOLUME_SESSION,
                    item["epoch_id"],
                    "foundation-30d",
                    "2026-08-10T20:00:00+00:00",
                    "sha256:abc",
                ],
            }
            for index, item in enumerate(refs, start=1)
        ]
    assert _valid_daily_cohort_results(proved, VOLUME_SESSION)

    corrupt = deepcopy(proved)
    corrupt[VOLUME_COHORTS[0]]["candidate_input_acceptance"][1]["issue_sha256"] = (
        corrupt[VOLUME_COHORTS[0]]["candidate_input_acceptance"][0]["issue_sha256"]
    )
    assert not _valid_daily_cohort_results(corrupt, VOLUME_SESSION)


@pytest.mark.parametrize("wire_kind", ["failed_attempt", "degraded_uncontained"])
def test_unproved_single_short_history_wire_fails_closed_in_operational_report(
    native_evidence, wire_kind
):
    repo, state, wire = native_evidence
    issue = _make_short_history_issue("UI", "d")
    ref = dict(issue.reference())
    ref["affected_cohorts"] = list(issue.affected_cohorts)
    with sqlite3.connect(state / GENERATION_DB_FILENAME) as connection:
        _insert(
            connection,
            "candidate_input_issues",
            issue_id=issue.issue_id,
            epoch_id=EPOCH,
            session=REPORT_SESSION,
            dependency_kind=issue.dependency_kind,
            ticker=issue.ticker,
            payload_json=issue.canonical_payload(),
        )
        connection.commit()

    vol_path = next((state / "source_inputs/staging_volatility").glob("*.json"))
    envelope = SourceInputStore.decode(vol_path.read_text())
    envelope["payload"]["quarantined_tickers"] = ("UI",)
    envelope["digest"] = hashlib.sha256(
        SourceInputStore.encode(envelope["payload"]).encode()
    ).hexdigest()
    vol_path.write_text(SourceInputStore.encode(envelope))

    for row in wire.values():
        if wire_kind == "failed_attempt":
            row.update(error=True, degraded=True, staging_valid=False, candidate_input_issues=[ref])
        else:
            row.update(error=False, degraded=True, staging_valid=False, candidate_input_issues=[ref])

    _attempt(
        repo,
        wire,
        outcome="failed" if wire_kind == "failed_attempt" else "degraded",
        return_code=1 if wire_kind == "failed_attempt" else 0,
    )
    report = _report(repo)
    assert report["staging_valid"] is False
    assert report["evidence_complete"] is False
    assert report["performance_claims_withheld"] is True
    assert any(
        diag["code"] == "candidate_containment_acceptance_invalid"
        for diag in report["diagnostics"]
    )


