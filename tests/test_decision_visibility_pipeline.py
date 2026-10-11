"""Offline 16-book committee abstention/failure through real daily boundaries."""
import json
from unittest.mock import patch

import pytest

from test_source_reliability_pipeline import pipeline, run_campaign


@pytest.mark.parametrize("mode", ["abstain", "unavailable", "unsupported"])
def test_native_16_book_committee_holds_cash_and_retains_decision_status(pipeline, mode):
    fixture, orchestrator, config, repo = pipeline
    original_model = fixture.model
    committee_calls = []
    def model(client, *, system, prompt, **kwargs):
        if "portfolio manager" in system:
            committee_calls.append(mode)
            if mode == "abstain":
                return "[]"
            if mode == "unsupported":
                return json.dumps([dict(ticker="UNSUPPORTED", direction="long",
                    position_size_pct=.05, confidence=.9, rationale="Fabricated trade",
                    contributing_strategies=["earnings_call"])])
            raise RuntimeError("fixture committee unavailable")
        enriched = json.loads(original_model(client, system=system, prompt=prompt, **kwargs))
        enriched["conviction"] = 1.0
        return json.dumps(enriched)
    from tradingagents.strategies import llm_utils
    with patch.object(llm_utils, "call_analysis_model", model):
        result, books, report = run_campaign(pipeline)
    assert len(committee_calls) == 16
    assert sum(len(row.get("recommendations", [])) for row in books.values()) == 0
    assert sum(len(row.get("intents_staged", [])) for row in books.values()) == 0
    assert result["execution_valid"] is True and result["input_coverage_valid"] is True
    assert result["outcome"] == ("clean" if mode == "abstain" else "degraded")
    for row in books.values():
        status = row["committee_decision_status"]
        assert status["mode"] == "model"
        assert status["status"] == ("abstained" if mode == "abstain" else "failed")
        assert status["degraded"] is (mode != "abstain")
        if mode == "unsupported":
            assert status["reason"] == "invalid_model_attribution"
