import dataclasses

import pandas as pd
import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    SIZE_PROFILES,
)
from tradingagents.strategies.trading.portfolio_policy import (
    PortfolioPolicyConfig,
    PolicyPosition,
    PortfolioRiskContext,
    annualized_volatility,
    build_portfolio_risk_context,
)
from tradingagents.strategies.trading.portfolio_committee import TradeRecommendation


def _policy_config(size: str = "100k") -> PortfolioPolicyConfig:
    return PortfolioPolicyConfig.from_size_profile(
        SIZE_PROFILES[size],
        DEFAULT_CONFIG["autoresearch"]["portfolio_policy"],
    )


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


def test_annualized_volatility_uses_60_sessions_and_floor() -> None:
    flat = pd.DataFrame({"Close": [100.0] * 80})

    assert annualized_volatility(flat, lookback_sessions=60, floor=0.15) == 0.15


def test_context_includes_current_and_pending_positions_with_full_tags() -> None:
    prices = {
        "AAPL": pd.DataFrame({"Close": [100.0, 101.0, 100.5]}),
        "MSFT": pd.DataFrame({"Close": [200.0, 202.0, 204.0]}),
    }
    context = build_portfolio_risk_context(
        portfolio_value=100_000.0,
        cash=75_000.0,
        current_positions=[{
            "ticker": "AAPL",
            "direction": "long",
            "marked_value": 10_000.0,
            "sector": "Technology",
            "strategy_tags": ("earnings_call", "filing_analysis"),
            "risk_tags": ("event:aapl-q2",),
        }],
        pending_positions=[{
            "ticker": "MSFT",
            "direction": "long",
            "marked_value": 8_000.0,
            "sector": "Technology",
            "strategy_tags": ("congressional_trades",),
            "risk_tags": ("member:jane-doe",),
        }],
        price_cache=prices,
        earnings_dates={"MSFT": 12},
        short_interest={"MSFT": 2.0},
        borrow_available={"MSFT": True},
        margin_used=0.0,
        consumed_event_keys={"event-old"},
        config=_policy_config(),
    )

    assert context.positions[0].weight == 0.10
    assert context.pending_positions[0].weight == 0.08
    assert context.positions[0].strategy_tags == (
        "earnings_call",
        "filing_analysis",
    )
    assert context.consumed_event_keys == frozenset({"event-old"})


def test_context_mappings_resist_source_and_instance_mutation() -> None:
    earnings_dates = {"MSFT": 12}
    context = build_portfolio_risk_context(
        portfolio_value=10_000.0,
        cash=9_000.0,
        current_positions=[],
        pending_positions=[],
        price_cache={},
        earnings_dates=earnings_dates,
        short_interest={"MSFT": 2.0},
        borrow_available={"MSFT": True},
        margin_used=0.0,
        consumed_event_keys={"event-old"},
        config=_policy_config(),
    )

    earnings_dates["MSFT"] = 99

    assert context.earnings_dates["MSFT"] == 12
    with pytest.raises(TypeError):
        context.earnings_dates["AAPL"] = 3  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.cash = 0.0  # type: ignore[misc]


def test_direct_context_constructor_normalizes_mutable_collections() -> None:
    earnings_dates = {"MSFT": 12}
    context = PortfolioRiskContext(
        portfolio_value=10_000.0,
        cash=9_000.0,
        positions=[],  # type: ignore[arg-type]
        pending_positions=[],  # type: ignore[arg-type]
        sectors={"MSFT": "Technology"},
        annualized_volatility={"MSFT": 0.20},
        earnings_dates=earnings_dates,
        short_interest={"MSFT": 2.0},
        borrow_available={"MSFT": True},
        margin_used=0.0,
        consumed_event_keys={"event-old"},
        config=_policy_config(),
    )

    earnings_dates["MSFT"] = 99

    assert context.positions == ()
    assert context.earnings_dates["MSFT"] == 12
    with pytest.raises(TypeError):
        context.sectors["AAPL"] = "Technology"  # type: ignore[index]


def test_policy_position_normalizes_direct_constructor_tag_lists() -> None:
    strategy_tags = ["congressional_trades"]
    risk_tags = ["member:jane-doe"]
    position = PolicyPosition(
        ticker="MSFT",
        direction="long",
        weight=0.08,
        sector="Technology",
        strategy_tags=strategy_tags,
        risk_tags=risk_tags,
        annualized_volatility=0.20,
    )
    context = PortfolioRiskContext(
        portfolio_value=10_000.0,
        cash=9_000.0,
        positions=[position],
        pending_positions=[],
        sectors={"MSFT": "Technology"},
        annualized_volatility={"MSFT": 0.20},
        earnings_dates={},
        short_interest={},
        borrow_available={},
        margin_used=0.0,
        consumed_event_keys=set(),
        config=_policy_config(),
    )

    strategy_tags.append("filing_analysis")
    risk_tags.append("event:changed")

    assert position.strategy_tags == ("congressional_trades",)
    assert position.risk_tags == ("member:jane-doe",)
    assert context.positions[0].strategy_tags == ("congressional_trades",)
    assert context.positions[0].risk_tags == ("member:jane-doe",)


def test_policy_config_requires_size_profile_factory() -> None:
    with pytest.raises(TypeError):
        PortfolioPolicyConfig()


def test_policy_config_rejects_direct_full_value_construction() -> None:
    valid_config = _policy_config()

    with pytest.raises(
        TypeError,
        match=r"PortfolioPolicyConfig\.from_size_profile",
    ):
        PortfolioPolicyConfig(**dataclasses.asdict(valid_config))


@pytest.mark.parametrize("size", ["5k", "10k", "50k", "100k"])
def test_policy_config_factory_uses_every_profile_limit(size: str) -> None:
    settings = DEFAULT_CONFIG["autoresearch"]["portfolio_policy"]
    profile = SIZE_PROFILES[size]

    config = PortfolioPolicyConfig.from_size_profile(profile, settings)

    assert config.max_positions == profile.max_positions
    assert config.profile_name == size
    assert config.max_position_pct == profile.max_position_pct
    assert config.max_sector_exposure_pct == profile.sector_concentration_cap
    assert config.max_strategy_exposure_pct == profile.max_strategy_exposure_pct
    assert config.max_event_cluster_exposure_pct == profile.max_event_cluster_exposure_pct
    assert config.max_position_risk_contribution_pct == (
        profile.max_position_risk_contribution_pct
    )
    assert config.congressional_exposure_pct == settings[
        "congressional_exposure_by_size"
    ][size]
    assert config.version == settings["version"]
