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


def test_finra_acquisition_is_validated_frozen_and_replayed(tmp_path):
    from tradingagents.strategies.data_sources.finra_bulk import new_attempt, VERSIONS
    ledger, engine, call = _policy_enabled_staging_fixture(tmp_path)
    call['annualized_volatility_evidence'] = {'AAPL': .31}
    attempt = new_attempt() | {'status': 'success', 'reason_code': None,
        'versions': dict(VERSIONS), 'cache_path': '/native/cache/finra.db',
        'row_count': 2, 'rowset_sha256': 'a'*64,
        'cached_archive_dates': ['2026-09-15'], 'missing_archive_dates': ['2026-09-30'],
        'required_archive_dates': ['2026-09-15', '2026-09-30'], 'inventory_after_observed': True,
        'inventory_before_observed': True, 'missing_archive_dates_before': ['2026-09-15', '2026-09-30'],
        'query_population_sha256': 'c'*64, 'prepare_started_at': 1.0,
        'prepare_finished_at': 2.0, 'prepare_elapsed_seconds': 1.0}
    acquisition = {'schema_version': 1, 'requested_count': 2, 'cached_count': 0, 'population_sha256': 'c'*64,
                   'attempts': [attempt]}
    call['enrichment']['short_interest_acquisition'] = deepcopy(acquisition)
    try:
        first = engine.screen_and_stage(**call)
        assert first['committee_decision_status']['short_interest_acquisition'] == acquisition
        accepted = deepcopy(ledger.committee_decision(FRIDAY, 'epoch', 'foundation-30d'))
        assert accepted['status']['short_interest_acquisition'] == acquisition
        call['enrichment']['short_interest_acquisition']['attempts'][0]['rowset_sha256'] = 'b'*64
        repeated = engine.screen_and_stage(**call)
        assert repeated['replayed'] is True
        assert repeated['committee_decision_status']['short_interest_acquisition'] == acquisition
        assert ledger.committee_decision(FRIDAY, 'epoch', 'foundation-30d') == accepted
    finally:
        ledger.close()


def test_finra_acquisition_unrecognized_metadata_cannot_enter_frozen_decision(tmp_path):
    ledger, engine, call = _policy_enabled_staging_fixture(tmp_path)
    call['annualized_volatility_evidence'] = {'AAPL': .31}
    call['enrichment']['short_interest_acquisition'] = {'credentials': 'PRIVATE'}
    try:
        with pytest.raises(ValueError, match='Invalid FINRA acquisition metadata'):
            engine.screen_and_stage(**call)
        assert ledger.committee_decision(FRIDAY, 'epoch', 'foundation-30d') is None
    finally:
        ledger.close()
