from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
from tradingagents.strategies.trading.portfolio_committee import TradeRecommendation


def test_candidate_and_recommendation_preserve_policy_attribution() -> None:
    candidate = Candidate(
        ticker="MSFT",
        date="2026-07-31",
        event_key="event-1",
        source_event_keys=("disclosure-1",),
        strategy_tags=("congressional_trades",),
        risk_tags=("member:jane-doe", "disclosure_week:2026-W31"),
        journal_only=True,
    )
    recommendation = TradeRecommendation(
        ticker="MSFT",
        direction="long",
        position_size_pct=0.08,
        confidence=0.8,
        rationale="two members purchased",
        event_key=candidate.event_key,
        source_event_keys=candidate.source_event_keys,
        strategy_tags=candidate.strategy_tags,
        risk_tags=candidate.risk_tags,
        journal_only=candidate.journal_only,
    )

    assert recommendation.event_key == "event-1"
    assert recommendation.source_event_keys == ("disclosure-1",)
    assert recommendation.strategy_tags == ("congressional_trades",)
    assert recommendation.risk_tags == (
        "member:jane-doe",
        "disclosure_week:2026-W31",
    )
    assert recommendation.journal_only is True


def test_size_profile_policy_limits_match_approved_table() -> None:
    expected = {
        "5k": (0.50, 0.25, 0.40, 4),
        "10k": (0.40, 0.20, 0.35, 4),
        "50k": (0.25, 0.15, 0.30, 4),
        "100k": (0.20, 0.10, 0.25, 4),
    }

    for size, limits in expected.items():
        profile = SIZE_PROFILES[size]
        assert (
            profile.max_strategy_exposure_pct,
            profile.max_event_cluster_exposure_pct,
            profile.max_position_risk_contribution_pct,
            profile.risk_contribution_min_positions,
        ) == limits


def test_policy_config_is_versioned_and_options_are_inactive() -> None:
    policy = DEFAULT_CONFIG["autoresearch"]["portfolio_policy"]

    assert policy["version"] == "portfolio_policy_v1"
    assert policy["volatility_lookback_sessions"] == 60
    assert policy["annualized_volatility_floor"] == 0.15
    assert policy["congressional_exposure_by_size"] == {
        "5k": 0.25,
        "10k": 0.20,
        "50k": 0.15,
        "100k": 0.12,
    }
    assert policy["options_overlays_enabled"] is False
