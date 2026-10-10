"""Optional enrichment failures remain inspectable in frozen decisions."""
from copy import deepcopy
import json
import pytest

from test_session_executor import _policy_enabled_staging_fixture, FRIDAY


def test_enrichment_failures_are_sanitized_frozen_and_replayed(tmp_path):
    ledger, engine, call = _policy_enabled_staging_fixture(tmp_path)
    call["annualized_volatility_evidence"] = {"AAPL": .31}
    call["enrichment"]["errors"] = {
        "profiles": {"MSFT": {"reason_code": "invalid_response", "error": "PRIVATE"}},
        "short_interest": {"AAPL": {"reason_code": "timeout", "error": "PRIVATE"}},
        "factors": {"reason_code": "provider_error", "error": "PRIVATE"},
    }
    expected = [
        {"operation": "factors", "symbol": None, "reason_code": "provider_error"},
        {"operation": "profiles", "symbol": "MSFT", "reason_code": "invalid_response"},
        {"operation": "short_interest", "symbol": "AAPL", "reason_code": "timeout"},
    ]
    try:
        first = engine.screen_and_stage(**call)
        status = first["committee_decision_status"]
        assert status["enrichment_failures"] == expected
        accepted = deepcopy(ledger.committee_decision(FRIDAY, "epoch", "foundation-30d"))
        assert accepted["status"]["enrichment_failures"] == expected
        assert "PRIVATE" not in json.dumps(accepted)
        call["enrichment"]["errors"] = {}
        repeated = engine.screen_and_stage(**call)
        assert repeated["replayed"] is True
        assert repeated["committee_decision_status"]["enrichment_failures"] == expected
        assert ledger.committee_decision(FRIDAY, "epoch", "foundation-30d") == accepted
    finally:
        ledger.close()


def test_enrichment_diagnostics_bound_untrusted_fields():
    from tradingagents.strategies.orchestration.multi_strategy_engine import _enrichment_failures
    result = _enrichment_failures({"errors": {
        "profiles": {"https://PRIVATE": {"reason_code": "PRIVATE", "error": "PRIVATE"}},
        "factors": {"reason_code": ["PRIVATE"]},
        "PRIVATE": {"PRIVATE": "PRIVATE"},
    }})
    assert "PRIVATE" not in json.dumps(result)
    assert result == [
        {"operation": "factors", "symbol": None, "reason_code": "provider_error"},
        {"operation": "profiles", "symbol": "unknown", "reason_code": "provider_error"},
    ]


@pytest.mark.parametrize("percentage,expected", [(None, "float percentage unavailable"),
                                                 (0, "0.0% short"), (125.5, "125.5% short")])
def test_committee_distinguishes_unknown_float_percentage_from_zero(percentage, expected):
    from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
    prompt = PortfolioCommittee({})._build_prompt([], {}, {}, [], 5000, {
        "short_interest": {"AAPL": {"short_pct_of_float": percentage,
                                      "short_interest": 400, "date": "2026-09-30"}}})
    assert expected in prompt
    if percentage is None:
        assert "0.0% short" not in prompt
        assert "400 shares short" in prompt
        assert "2026-09-30" in prompt
