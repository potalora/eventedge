import dataclasses

import pandas as pd
import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    SIZE_PROFILES,
)
from tradingagents.strategies.trading.portfolio_policy import (
    PortfolioPolicy,
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


def test_policy_config_rejects_altered_real_factory_config() -> None:
    altered_values = dataclasses.asdict(_policy_config("100k"))
    altered_values["max_strategy_exposure_pct"] = 1.0

    with pytest.raises(
        TypeError,
        match=r"PortfolioPolicyConfig\.from_size_profile",
    ):
        PortfolioPolicyConfig(**altered_values)


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
    assert config.max_correlated_shorts == profile.max_correlated_shorts
    assert config.congressional_exposure_pct == settings[
        "congressional_exposure_by_size"
    ][size]
    assert config.version == settings["version"]


def _position(
    ticker: str,
    weight: float,
    *,
    direction: str = "long",
    sector: str = "Diversified",
    strategies: tuple[str, ...] = ("existing",),
    risks: tuple[str, ...] = ("existing-risk",),
    volatility: float = 0.15,
) -> PolicyPosition:
    return PolicyPosition(
        ticker=ticker,
        direction=direction,
        weight=weight,
        sector=sector,
        strategy_tags=strategies,
        risk_tags=risks,
        annualized_volatility=volatility,
    )


def _context(
    *,
    size: str = "100k",
    positions: tuple[PolicyPosition, ...] = (),
    pending: tuple[PolicyPosition, ...] = (),
    cash: float = 100_000.0,
    sectors: dict[str, str] | None = None,
    volatility: dict[str, float] | None = None,
    borrow_available: dict[str, bool] | None = None,
    margin_used: float = 0.0,
    consumed_event_keys: frozenset[str] = frozenset(),
) -> PortfolioRiskContext:
    return PortfolioRiskContext(
        portfolio_value=100_000.0,
        cash=cash,
        positions=positions,
        pending_positions=pending,
        sectors=sectors or {},
        annualized_volatility=volatility or {},
        earnings_dates={},
        short_interest={},
        borrow_available=borrow_available or {},
        margin_used=margin_used,
        consumed_event_keys=consumed_event_keys,
        config=_policy_config(size),
    )


def _recommendation(
    ticker: str,
    weight: float,
    *,
    direction: str = "long",
    strategies: tuple[str, ...] = ("new-strategy",),
    risks: tuple[str, ...] = ("new-risk",),
    event_key: str | None = None,
    journal_only: bool = False,
) -> TradeRecommendation:
    return TradeRecommendation(
        ticker=ticker,
        direction=direction,
        position_size_pct=weight,
        confidence=0.8,
        rationale="test",
        event_key=event_key or f"event:{ticker}",
        strategy_tags=strategies,
        risk_tags=risks,
        journal_only=journal_only,
    )


def test_policy_counts_full_weight_against_every_strategy_tag() -> None:
    existing = _position(
        "AAPL",
        0.18,
        sector="Consumer",
        strategies=("earnings_call", "filing_analysis"),
        risks=("old-risk",),
    )
    recommendation = _recommendation(
        "MSFT",
        0.08,
        strategies=("earnings_call", "filing_analysis"),
        risks=("new-risk",),
    )

    accepted = PortfolioPolicy().apply(
        [recommendation],
        _context(positions=(existing,), sectors={"MSFT": "Technology"}),
    )

    assert [item.position_size_pct for item in accepted] == pytest.approx([0.02])


def test_policy_applies_event_cluster_cap_independently() -> None:
    existing = _position(
        "AAPL",
        0.09,
        sector="Consumer",
        strategies=("old-strategy",),
        risks=("cluster:q2",),
    )
    recommendation = _recommendation(
        "MSFT",
        0.08,
        strategies=("new-strategy",),
        risks=("cluster:q2",),
    )

    accepted = PortfolioPolicy().apply(
        [recommendation],
        _context(positions=(existing,), sectors={"MSFT": "Technology"}),
    )

    assert [item.position_size_pct for item in accepted] == pytest.approx([0.01])


def test_policy_applies_congressional_cap_independently() -> None:
    existing = _position(
        "AAPL",
        0.10,
        sector="Consumer",
        strategies=("congressional_trades",),
        risks=("member:old",),
    )
    recommendation = _recommendation(
        "MSFT",
        0.08,
        strategies=("congressional_trades",),
        risks=("member:new",),
    )

    accepted = PortfolioPolicy().apply(
        [recommendation],
        _context(positions=(existing,), sectors={"MSFT": "Technology"}),
    )

    assert [item.position_size_pct for item in accepted] == pytest.approx([0.02])


def test_policy_enforces_generic_and_congressional_caps_from_factory_profile() -> None:
    config = _policy_config("100k")
    assert config.max_strategy_exposure_pct == pytest.approx(0.20)
    assert config.congressional_exposure_pct == pytest.approx(0.12)

    generic_accepted = PortfolioPolicy().apply(
        [_recommendation("MSFT", 0.08, strategies=("earnings_call",))],
        _context(
            positions=(
                _position(
                    "AAPL",
                    0.18,
                    strategies=("earnings_call",),
                    risks=("generic:old",),
                ),
            ),
            sectors={"MSFT": "Technology"},
        ),
    )
    congressional_accepted = PortfolioPolicy().apply(
        [_recommendation("NVDA", 0.08, strategies=("congressional_trades",))],
        _context(
            positions=(
                _position(
                    "AAPL",
                    0.08,
                    strategies=("congressional_trades",),
                    risks=("congressional:old",),
                ),
            ),
            sectors={"NVDA": "Technology"},
        ),
    )

    assert [item.position_size_pct for item in generic_accepted] == pytest.approx(
        [0.02]
    )
    assert [item.position_size_pct for item in congressional_accepted] == pytest.approx(
        [0.04]
    )


def test_policy_counts_current_pending_and_prior_acceptances_in_order() -> None:
    current = _position(
        "AAPL",
        0.04,
        sector="Consumer",
        strategies=("shared",),
        risks=("risk:a",),
    )
    pending = _position(
        "MSFT",
        0.04,
        sector="Technology",
        strategies=("shared",),
        risks=("risk:m",),
    )
    recommendations = [
        _recommendation(
            "JNJ", 0.08, strategies=("shared",), risks=("risk:j",)
        ),
        _recommendation(
            "XOM", 0.08, strategies=("shared",), risks=("risk:x",)
        ),
    ]

    accepted = PortfolioPolicy().apply(
        recommendations,
        _context(
            positions=(current,),
            pending=(pending,),
            sectors={"JNJ": "Healthcare", "XOM": "Energy"},
        ),
    )

    assert [item.ticker for item in accepted] == ["JNJ", "XOM"]
    assert [item.position_size_pct for item in accepted] == pytest.approx(
        [0.08, 0.04]
    )


def test_position_risk_contribution_waits_for_four_positions_then_caps() -> None:
    positions = tuple(
        _position(
            f"T{i}",
            0.05,
            sector=f"Sector-{i}",
            strategies=(f"strategy-{i}",),
            risks=(f"risk-{i}",),
            volatility=0.05,
        )
        for i in range(3)
    )
    recommendation = _recommendation("MSFT", 0.08)
    before_activation = PortfolioPolicy().apply(
        [recommendation],
        _context(
            positions=positions[:2],
            sectors={"MSFT": "Technology"},
            volatility={"MSFT": 0.60},
        ),
    )
    after_activation = PortfolioPolicy().apply(
        [recommendation],
        _context(
            positions=positions,
            sectors={"MSFT": "Technology"},
            volatility={"MSFT": 0.60},
        ),
    )

    assert before_activation[0].position_size_pct == pytest.approx(0.08)
    accepted_weight = after_activation[0].position_size_pct
    base_risk = 3 * 0.05 * 0.15
    candidate_risk = accepted_weight * 0.60
    assert candidate_risk / (candidate_risk + base_risk) <= 0.25 + 1e-9
    assert accepted_weight == pytest.approx(0.0125)


@pytest.mark.parametrize("location", ["current", "pending"])
def test_policy_rejects_duplicate_ticker_from_entire_book(location: str) -> None:
    duplicate = _position("MSFT", 0.02)
    positions = (duplicate,) if location == "current" else ()
    pending = (duplicate,) if location == "pending" else ()

    valid, reason = PortfolioPolicy().validate(
        _recommendation("MSFT", 0.01),
        _context(positions=positions, pending=pending),
    )

    assert (valid, reason) == (False, "duplicate_ticker")


def test_policy_rejects_journal_only_recommendation() -> None:
    recommendation = _recommendation("MSFT", 0.01, journal_only=True)
    policy = PortfolioPolicy()

    assert policy.apply([recommendation], _context()) == []
    assert policy.validate(recommendation, _context()) == (False, "journal_only")


def test_policy_rejects_consumed_event() -> None:
    recommendation = _recommendation("MSFT", 0.01, event_key="event:used")

    assert PortfolioPolicy().validate(
        recommendation,
        _context(consumed_event_keys=frozenset({"event:used"})),
    ) == (False, "consumed_event")


@pytest.mark.parametrize(
    ("recommendation", "context", "reason"),
    [
        (_recommendation("MSFT", 0.0, journal_only=True), _context(), "journal_only"),
        (
            _recommendation("MSFT", 0.0),
            _context(positions=(_position("MSFT", 0.01),)),
            "duplicate_ticker",
        ),
        (
            _recommendation("MSFT", 0.0, event_key="event:used"),
            _context(consumed_event_keys=frozenset({"event:used"})),
            "consumed_event",
        ),
    ],
)
def test_policy_validate_preserves_hard_rejection_for_zero_weight(
    recommendation: TradeRecommendation,
    context: PortfolioRiskContext,
    reason: str,
) -> None:
    assert PortfolioPolicy().validate(recommendation, context) == (False, reason)


@pytest.mark.parametrize("weight", [0.0, -0.01])
def test_policy_validate_rejects_ordinary_nonpositive_weight(weight: float) -> None:
    assert PortfolioPolicy().validate(
        _recommendation("MSFT", weight),
        _context(),
    ) == (False, "nonpositive_weight")


def test_policy_rejects_when_profile_max_positions_is_reached() -> None:
    positions = tuple(
        _position(
            f"T{i}",
            0.01,
            sector=f"Sector-{i}",
            strategies=(f"strategy-{i}",),
            risks=(f"risk-{i}",),
        )
        for i in range(SIZE_PROFILES["5k"].max_positions)
    )

    assert PortfolioPolicy().validate(
        _recommendation("MSFT", 0.01),
        _context(size="5k", positions=positions),
    ) == (False, "max_positions")


def test_policy_scales_to_position_cap_and_validate_returns_stable_reason() -> None:
    recommendation = _recommendation("MSFT", 0.12)
    policy = PortfolioPolicy()

    accepted = policy.apply([recommendation], _context())

    assert accepted[0].position_size_pct == pytest.approx(0.08)
    assert policy.validate(recommendation, _context()) == (False, "max_position")


def test_policy_scales_to_sector_cap() -> None:
    existing = _position("AAPL", 0.23, sector="Technology")

    accepted = PortfolioPolicy().apply(
        [_recommendation("MSFT", 0.08)],
        _context(positions=(existing,), sectors={"MSFT": "Technology"}),
    )

    assert accepted[0].position_size_pct == pytest.approx(0.02)


def test_policy_normalizes_case_and_whitespace_for_sector_exposure() -> None:
    existing = _position("AAPL", 0.23, sector="Technology")

    accepted = PortfolioPolicy().apply(
        [_recommendation("MSFT", 0.08)],
        _context(positions=(existing,), sectors={"MSFT": "  technology  "}),
    )

    assert [item.position_size_pct for item in accepted] == pytest.approx([0.02])


def test_policy_scales_single_short_to_profile_cap() -> None:
    recommendation = _recommendation("MSFT", 0.08, direction="short")

    accepted = PortfolioPolicy().apply(
        [recommendation],
        _context(
            sectors={"MSFT": "Technology"},
            borrow_available={"MSFT": True},
        ),
    )

    assert accepted[0].position_size_pct == pytest.approx(0.05)


def test_policy_scales_to_total_short_cap() -> None:
    positions = tuple(
        _position(
            f"S{i}",
            0.045,
            direction="short",
            sector=f"Sector-{i}",
            strategies=(f"strategy-{i}",),
            risks=(f"risk-{i}",),
        )
        for i in range(4)
    )
    recommendation = _recommendation("MSFT", 0.05, direction="short")

    accepted = PortfolioPolicy().apply(
        [recommendation],
        _context(
            positions=positions,
            sectors={"MSFT": "Technology"},
            borrow_available={"MSFT": True},
        ),
    )

    assert accepted[0].position_size_pct == pytest.approx(0.02)


@pytest.mark.parametrize("sector", ["Technology", "Unknown"])
def test_policy_rejects_correlated_short_count_for_known_and_unknown_sectors(
    sector: str,
) -> None:
    current = _position("AAPL", 0.02, direction="short", sector=sector)
    pending = _position("NVDA", 0.02, direction="short", sector=sector)
    sectors = {"MSFT": sector} if sector != "Unknown" else {}

    assert PortfolioPolicy().validate(
        _recommendation("MSFT", 0.02, direction="short"),
        _context(
            size="50k",
            positions=(current,),
            pending=(pending,),
            sectors=sectors,
            borrow_available={"MSFT": True},
        ),
    ) == (False, "max_correlated_shorts")


def test_policy_normalizes_case_and_whitespace_for_correlated_shorts() -> None:
    current = _position("AAPL", 0.02, direction="short", sector="Technology")
    pending = _position("NVDA", 0.02, direction="short", sector="Technology")

    assert PortfolioPolicy().validate(
        _recommendation("MSFT", 0.02, direction="short"),
        _context(
            size="50k",
            positions=(current,),
            pending=(pending,),
            sectors={"MSFT": " technology "},
            borrow_available={"MSFT": True},
        ),
    ) == (False, "max_correlated_shorts")


@pytest.mark.parametrize("borrow_available", [{}, {"MSFT": False}])
def test_policy_rejects_short_when_borrow_is_unknown_or_unavailable(
    borrow_available: dict[str, bool],
) -> None:
    assert PortfolioPolicy().validate(
        _recommendation("MSFT", 0.02, direction="short"),
        _context(
            sectors={"MSFT": "Technology"},
            borrow_available=borrow_available,
        ),
    ) == (False, "borrow_unavailable")


def test_policy_reserves_pending_long_notional_and_cash_floor() -> None:
    pending = _position("AAPL", 0.10, sector="Consumer")

    accepted = PortfolioPolicy().apply(
        [_recommendation("MSFT", 0.08)],
        _context(
            pending=(pending,),
            cash=30_000.0,
            sectors={"MSFT": "Technology"},
        ),
    )

    assert accepted[0].position_size_pct == pytest.approx(0.05)


def test_policy_reserves_pending_short_margin_and_cash_buffer() -> None:
    pending = _position(
        "AAPL", 0.04, direction="short", sector="Consumer"
    )

    accepted = PortfolioPolicy().apply(
        [_recommendation("MSFT", 0.05, direction="short")],
        _context(
            pending=(pending,),
            cash=27_000.0,
            sectors={"MSFT": "Technology"},
            borrow_available={"MSFT": True},
            margin_used=5_000.0,
        ),
    )

    assert accepted[0].position_size_pct == pytest.approx(0.03)


def test_policy_uses_deterministic_reason_priority_when_caps_tie() -> None:
    existing = _position("AAPL", 0.17, sector="Technology")
    recommendation = _recommendation("MSFT", 0.09)

    assert PortfolioPolicy().validate(
        recommendation,
        _context(positions=(existing,), sectors={"MSFT": "Technology"}),
    ) == (False, "max_position")
