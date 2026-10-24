"""Offline comparison rejects a false baseline and measures observable coverage."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tradingagents.strategies.research.comparison import compare_fixture


def row(identifier, ticker, score, reason, *, event=None, version="a" * 64):
    return {"discovery_id": identifier, "ticker": ticker, "score": score,
            "direction": "long", "journal_only": False, "reason": reason,
            "event_key": event or identifier, "source_event_key": event or identifier,
            "evidence_sha256": version,
            "universe": {"decision": "eligible"},
            "availability": {"candidate_at": "2026-10-22T20:00:00+00:00",
                             "source_observed_at": None,
                             "envelope_acquired_at": "2026-10-22T22:00:00+00:00"}}


def fixture():
    return {"schema_version": 1, "generation": "test", "code_commit": "b" * 40,
            "cases": [{"session": "2026-10-22", "horizon": "30d",
                       "strategy": "supply_chain", "budget": 2,
                       "cutoff_at": "2026-10-22T22:05:00+00:00",
                       "expected_admitted_ids": ["a1", "a2"],
                       "rows": [row("a1", "AAA", .9, "admitted"),
                                row("a2", "AAA", .8, "admitted"),
                                row("b1", "BBB", .7, "analysis_budget")]}]}


def test_comparison_reports_score_tradeoff_without_changing_inputs_or_budget():
    data = fixture()
    before = deepcopy(data)
    result = compare_fixture(data)
    case = result["cases"][0]
    assert data == before
    assert case["legacy"]["selected_ids"] == ["a1", "a2"]
    assert case["alternative"]["selected_ids"] == ["a1", "b1"]
    assert case["legacy"]["metrics"]["distinct_eligible_symbols"] == 1
    assert case["alternative"]["metrics"]["distinct_eligible_symbols"] == 2
    assert case["legacy"]["metrics"]["screening_score_sum"] == 1.7
    assert case["alternative"]["metrics"]["screening_score_sum"] == 1.6
    assert case["budget"] == 2
    assert case["selected_added"] == ["b1"]
    assert case["selected_removed"] == ["a2"]
    assert len(case["dispositions"]) == 3
    assert case["dispositions"][1]["original_reason"] == "admitted"
    assert result["model_calls"] == result["provider_calls"] == 0


def test_comparison_fails_when_baseline_disagrees_with_retained_admission():
    data = fixture()
    data["cases"][0]["expected_admitted_ids"] = ["a1", "b1"]
    with pytest.raises(ValueError, match="baseline"):
        compare_fixture(data)


@pytest.mark.parametrize("bad_time", ["2026-10-22T22:06:00+00:00", "2026-10-22T20:00:00"])
def test_future_or_naive_evidence_cannot_support_comparison(bad_time):
    data = fixture()
    data["cases"][0]["rows"][2]["availability"]["envelope_acquired_at"] = bad_time
    with pytest.raises(ValueError, match="availability|timezone"):
        compare_fixture(data)


def test_missing_clocks_and_portfolio_context_are_explicitly_unmeasured():
    data = fixture()
    for candidate in data["cases"][0]["rows"]:
        candidate.pop("availability")
    result = compare_fixture(data)
    case = result["cases"][0]
    assert case["availability"]["status"] == "unmeasured"
    assert case["availability"]["missing_rows"] == 3
    assert case["portfolio_relevance"] == "unmeasured"


def test_repeat_metrics_separate_identical_content_from_changed_content():
    data = fixture()
    later = deepcopy(data["cases"][0])
    later["session"] = "2026-10-23"
    later["cutoff_at"] = "2026-10-23T22:05:00+00:00"
    later["rows"][1]["evidence_sha256"] = "c" * 64
    data["cases"] = [later, data["cases"][0]]
    result = compare_fixture(data)
    metrics = result["cases"][1]["legacy"]["metrics"]
    assert metrics["repeated_source_hypotheses"] == 2
    assert metrics["repeated_identical_content"] == 1
    assert metrics["changed_retained_content"] == 1
    assert metrics["new_source_hypotheses"] == 0
    assert result == compare_fixture(dict(data, cases=list(reversed(data["cases"]))))


def test_same_session_horizons_do_not_count_as_prior_sessions():
    data = fixture()
    second = deepcopy(data["cases"][0])
    second["horizon"] = "1y"
    data["cases"].append(second)
    result = compare_fixture(data)
    assert result["session_count"] == 1
    for case in result["cases"]:
        assert case["legacy"]["metrics"]["history_status"] == "no_prior_session"


def test_duplicate_case_is_rejected_instead_of_inflating_sample_size():
    data = fixture()
    data["cases"].append(deepcopy(data["cases"][0]))
    with pytest.raises(ValueError, match="duplicate case"):
        compare_fixture(data)


def test_cli_writes_reproducible_results_and_does_not_modify_fixture(tmp_path):
    source = tmp_path / "inputs.json"
    source.write_text(json.dumps(fixture()))
    before = source.read_bytes()
    target = tmp_path / "comparison.json"
    command = [sys.executable, "scripts/compare_candidate_selection.py",
               "--fixture", str(source), "--output", str(target)]
    process = subprocess.run(command, capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert source.read_bytes() == before
    saved = json.loads(target.read_text())
    assert saved["cases"][0]["alternative"]["selected_ids"] == ["a1", "b1"]
    assert len(saved["fixture_sha256"]) == 64
    assert Path(saved["fixture_path"]).name == "inputs.json"


def test_retained_gen025_comparison_matches_observed_counts_and_tradeoffs():
    path = Path(__file__).resolve().parents[1] / "docs/experiments/2026-10-24-selection/selection-fixture.json"
    data = json.loads(path.read_text())
    result = compare_fixture(data)
    assert result["session_count"] == 2
    assert len(result["cases"]) == 24
    for case in result["cases"]:
        old = case["legacy"]["metrics"]
        new = case["alternative"]["metrics"]
        assert old["selected_count"] == new["selected_count"] == case["budget"]
        assert case["availability"]["missing_rows"] == 0
        for disposition in case["dispositions"]:
            if disposition["original_reason"].startswith("equity_universe:"):
                assert disposition["alternative_reason"] == disposition["original_reason"]
        if case["strategy"] == "supply_chain":
            assert old["eligible_symbols"] == ["AAPL"]
            assert new["eligible_symbols"] == ["AAPL", "AMZN", "BA", "CAT"]
            assert old["screening_score_sum"] == new["screening_score_sum"] == 2.4
        elif case["strategy"] == "govt_contracts":
            assert old["eligible_symbols"] == ["UNH"]
            assert new["eligible_symbols"] == ["IBM", "UNH", "VVX"]
            assert old["screening_score_sum"] == 2.50069921606
            assert new["screening_score_sum"] == 1.29677506005
        else:
            assert old["distinct_eligible_symbols"] == new["distinct_eligible_symbols"] == 4
            assert case["selected_added"] == case["selected_removed"] == []
