from __future__ import annotations

from dataclasses import asdict

import pytest

from tradingagents.strategies.orchestration.daily_pipeline import (
    summarize_cohort_results,
)


TRADING_DATE = "2026-08-10"


def _candidate_issue(
    *,
    affected_cohorts: list[str],
    dependency_kind: str = "reference_bar",
    ticker: str = "UI",
) -> dict[str, object]:
    suffix = "a" if dependency_kind == "reference_bar" else "c"
    return {
        "issue_id": "candidate_input_issue_" + suffix * 32,
        "epoch_id": "gen_001-2026-08-10-" + "b" * 16,
        "session": TRADING_DATE,
        "dependency_kind": dependency_kind,
        "reason_code": "provider_error",
        "ticker": ticker,
        "affected_cohorts": affected_cohorts,
    }


def _governed_recovery(affected_cohorts: list[str]) -> dict[str, object]:
    return {
        "ticker": "ESS",
        "session": TRADING_DATE,
        "recovery_id": "governed_bar_recovery:" + "b" * 64,
        "contract_version": "yfinance-60m-v1",
        "evidence_digest": "sha256:" + "a" * 64,
        "affected_cohort_ids": affected_cohorts,
    }


def _result(
    *,
    error: bool = False,
    degraded: bool = False,
    execution_valid: bool = True,
    staging_valid: bool = True,
    quarantines: list[str] | None = None,
    issues: list[dict[str, object]] | None = None,
    recoveries: list[dict[str, object]] | None = None,
    failures: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "error": error,
        "degraded": degraded,
        "execution_valid": execution_valid,
        "staging_valid": staging_valid,
        "candidate_bar_quarantines": quarantines or [],
        "candidate_input_issues": issues or [],
        "governed_bar_recoveries": recoveries or [],
        "governed_failure_map": failures or {},
    }


@pytest.mark.parametrize(
    ("results", "expected"),
    (
        (
            {
                "cohort-a": _result(),
                "cohort-b": _result(),
            },
            {
                "outcome": "clean",
                "total": 2,
                "failed": (),
                "degraded": (),
                "execution_valid": True,
                "candidate_bar_quarantines": (),
                "governed_bar_recoveries": (),
                "governed_failure_map": {},
                "candidate_input_issues": (),
                "degradation_label": None,
            },
        ),
        (
            (
                lambda issue: {
                    name: _result(
                        degraded=True,
                        staging_valid=False,
                        quarantines=["UI"],
                        issues=[dict(issue)],
                    )
                    for name in ("cohort-a", "cohort-b")
                }
            )(_candidate_issue(affected_cohorts=["cohort-a", "cohort-b"])),
            {
                "outcome": "degraded",
                "total": 2,
                "failed": (),
                "degraded": ("cohort-a", "cohort-b"),
                "execution_valid": True,
                "candidate_bar_quarantines": ("UI",),
                "governed_bar_recoveries": (),
                "governed_failure_map": {},
                "candidate_input_issues": (
                    _candidate_issue(affected_cohorts=["cohort-a", "cohort-b"]),
                ),
                "degradation_label": "candidate input issue",
            },
        ),
        (
            (
                lambda issue: {
                    "cohort-a": _result(
                        degraded=True,
                        staging_valid=False,
                        issues=[issue],
                    )
                }
            )(
                _candidate_issue(
                    affected_cohorts=["cohort-a"],
                    dependency_kind="volatility_history",
                    ticker="ZKH",
                )
            ),
            {
                "outcome": "degraded",
                "total": 1,
                "failed": (),
                "degraded": ("cohort-a",),
                "execution_valid": True,
                "candidate_bar_quarantines": (),
                "governed_bar_recoveries": (),
                "governed_failure_map": {},
                "candidate_input_issues": (
                    _candidate_issue(
                        affected_cohorts=["cohort-a"],
                        dependency_kind="volatility_history",
                        ticker="ZKH",
                    ),
                ),
                "degradation_label": "candidate input issue",
            },
        ),
        (
            (
                lambda issue: {
                    "cohort-a": _result(
                        degraded=True,
                        staging_valid=False,
                        quarantines=["UI"],
                        issues=[issue],
                    ),
                    "cohort-b": _result(
                        error=True,
                        execution_valid=False,
                        staging_valid=False,
                    ),
                }
            )(_candidate_issue(affected_cohorts=["cohort-a"])),
            {
                "outcome": "failed",
                "total": 2,
                "failed": ("cohort-b",),
                "degraded": ("cohort-a",),
                "execution_valid": False,
                "candidate_bar_quarantines": ("UI",),
                "governed_bar_recoveries": (),
                "governed_failure_map": {},
                "candidate_input_issues": (
                    _candidate_issue(affected_cohorts=["cohort-a"]),
                ),
                "degradation_label": "candidate input issue",
            },
        ),
        (
            (
                lambda recovery: {
                    name: _result(
                        degraded=True,
                        recoveries=[dict(recovery)],
                    )
                    for name in ("cohort-a", "cohort-b")
                }
            )(_governed_recovery(["cohort-a", "cohort-b"])),
            {
                "outcome": "degraded",
                "total": 2,
                "failed": (),
                "degraded": ("cohort-a", "cohort-b"),
                "execution_valid": True,
                "candidate_bar_quarantines": (),
                "governed_bar_recoveries": (
                    _governed_recovery(["cohort-a", "cohort-b"]),
                ),
                "governed_failure_map": {},
                "candidate_input_issues": (),
                "degradation_label": "governed bar recovery",
            },
        ),
        (
            {
                "cohort-a": _result(
                    error=True,
                    execution_valid=False,
                    staging_valid=False,
                    failures={"SPY": "invalid_benchmark SPY/2026-08-10"},
                )
            },
            {
                "outcome": "failed",
                "total": 1,
                "failed": ("cohort-a",),
                "degraded": (),
                "execution_valid": False,
                "candidate_bar_quarantines": (),
                "governed_bar_recoveries": (),
                "governed_failure_map": {
                    "SPY": "invalid_benchmark SPY/2026-08-10"
                },
                "candidate_input_issues": (),
                "degradation_label": None,
            },
        ),
    ),
    ids=(
        "clean",
        "candidate-reference-degraded",
        "candidate-volatility-degraded",
        "candidate-plus-real-failure",
        "governed-recovery-degraded",
        "governed-failure",
    ),
)
def test_daily_result_summary_incident_corpus(results, expected):
    summary = summarize_cohort_results(results, trading_date=TRADING_DATE)

    assert asdict(summary) == expected
