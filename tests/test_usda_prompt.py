"""USDA provider rows must retain their meaning in the analysis prompt."""
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.strategies.data_sources.usda_source import USDASource
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer


def prompt(rows):
    analyzer = LLMAnalyzer()
    with patch.object(analyzer, "_call_llm", return_value="{}") as call:
        analyzer.analyze_ag_weather("DBA", "Agriculture", {"usda_data": {"crop_progress": {"CORN": rows}}})
    return call.call_args.args[1].split("CROP CONDITIONS (USDA):\n", 1)[1].split("\nPRICE ACTION:", 1)[0]


@pytest.mark.parametrize("latest", ["missing", "conflict"])
def test_source_invalid_latest_prompt_does_not_impute_zero_or_old_decline(latest):
    records = []
    for week, good in [("2026-09-06", "80"), ("2026-09-13", "60"), ("2026-09-20", None)]:
        records.append({"week_ending": week, "state_alpha": "IA", "unit_desc": "PCT EXCELLENT", "Value": "10"})
        if good:
            records.append({"week_ending": week, "state_alpha": "IA", "unit_desc": "PCT GOOD", "Value": good})
    if latest == "conflict":
        records.extend([dict(records[-1], unit_desc="PCT GOOD", Value=value) for value in ["10", "20"]])
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": records}
    source = USDASource(api_key="test-only")
    with patch("requests.get", return_value=response):
        with pytest.raises(SourceFetchError) as failure:
            source.fetch_crop_progress("CORN", 2026, "IA")
    assert failure.value.reason_code == "invalid_response"
    rows = failure.value.partial_data["crop_progress"]["CORN"]
    assert source._cache == {}
    latest_row = next(row for row in rows if row["week_ending"] == "2026-09-20")
    assert latest_row["condition_valid"] is False
    assert "good_pct" not in latest_row and "excellent_pct" not in latest_row
    assert [row["good_pct"] for row in rows[:-1]] == [80, 60]
    text = prompt(rows)
    assert "2026-09-20" in text
    assert "IA" in text
    assert "unavailable" in text.lower()
    assert "0% Good/Excellent" not in text
    assert "change:" not in text
    assert "2026-09-13" not in text


def test_prompt_matches_regions_across_exact_week_not_row_order():
    rows = [
        {"week_ending": "2026-09-20", "state": "IA", "good_pct": 50, "excellent_pct": 10},
        {"week_ending": "2026-09-13", "state": "IL", "good_pct": 20, "excellent_pct": 10},
        {"week_ending": "2026-09-13", "state": "IA", "good_pct": 60, "excellent_pct": 10},
        {"week_ending": "2026-09-20", "state": "IL", "good_pct": 20, "excellent_pct": 10},
    ]
    text = prompt(rows)
    assert text == prompt(list(reversed(rows)))
    assert "IA" in text and "IL" in text and "2026-09-20" in text
    assert "change: -10pp" in text and "change: +0pp" in text
    assert "change: -40pp" not in text


def test_single_week_prompt_has_no_weekly_change():
    text = prompt([
        {"week_ending": "2026-09-20", "state": "IA", "good_pct": 60, "excellent_pct": 10},
        {"week_ending": "2026-09-20", "state": "IL", "good_pct": 20, "excellent_pct": 10},
    ])
    assert "change:" not in text
    assert "weekly comparison unavailable" in text.lower()
