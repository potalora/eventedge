"""Pure, deterministic portfolio-policy risk context primitives.

This module deliberately has no ledger, API, or LLM dependencies.  Ledger
adapters provide authoritative ``marked_value`` inputs; cached prices are used
only to calculate historical volatility.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from math import sqrt
from types import MappingProxyType
from typing import Any, Iterable, Mapping

import pandas as pd


_PORTFOLIO_POLICY_FACTORY_TOKEN = object()


def _immutable_mapping(values: Mapping[str, Any]) -> Mapping[str, Any]:
    """Copy scalar mapping data into an immutable mapping with normal get access."""
    return MappingProxyType(dict(values))


@dataclass(frozen=True)
class PolicyPosition:
    ticker: str
    direction: str
    weight: float
    sector: str
    strategy_tags: tuple[str, ...]
    risk_tags: tuple[str, ...]
    annualized_volatility: float

    def __post_init__(self) -> None:
        """Prevent mutable tag collections from entering a frozen position."""
        object.__setattr__(self, "strategy_tags", tuple(self.strategy_tags))
        object.__setattr__(self, "risk_tags", tuple(self.risk_tags))


@dataclass(frozen=True, init=False)
class PortfolioPolicyConfig:
    """Versioned policy settings for one explicitly selected cohort profile."""

    profile_name: str
    version: str
    max_positions: int
    max_position_pct: float
    max_sector_exposure_pct: float
    max_strategy_exposure_pct: float
    max_event_cluster_exposure_pct: float
    max_position_risk_contribution_pct: float
    risk_contribution_min_positions: int
    max_short_exposure_pct: float
    max_single_short_pct: float
    cash_reserve_pct: float
    margin_cash_buffer_pct: float
    volatility_lookback_sessions: int
    annualized_volatility_floor: float
    congressional_exposure_pct: float

    def __init__(
        self,
        *,
        _factory_token: object | None = None,
        **values: Any,
    ) -> None:
        """Reject arbitrary configuration outside the profile-bound factory."""
        if _factory_token is not _PORTFOLIO_POLICY_FACTORY_TOKEN:
            raise TypeError(
                "PortfolioPolicyConfig must be created with "
                "PortfolioPolicyConfig.from_size_profile()"
            )

        expected_fields = {field.name for field in fields(type(self))}
        supplied_fields = set(values)
        if supplied_fields != expected_fields:
            missing = expected_fields - supplied_fields
            unexpected = supplied_fields - expected_fields
            raise TypeError(
                "PortfolioPolicyConfig factory received invalid fields: "
                f"missing={sorted(missing)}, unexpected={sorted(unexpected)}"
            )
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @classmethod
    def from_size_profile(
        cls,
        profile: Any,
        policy_settings: Mapping[str, Any],
    ) -> "PortfolioPolicyConfig":
        """Build a config from a cohort profile without cross-size fallbacks.

        ``congressional_exposure_by_size`` must explicitly name the selected
        profile.  A missing entry is a configuration error, rather than an
        opportunity to inherit a larger cohort's policy.
        """
        try:
            congressional_exposure = policy_settings[
                "congressional_exposure_by_size"
            ][profile.name]
            return cls(
                _factory_token=_PORTFOLIO_POLICY_FACTORY_TOKEN,
                profile_name=str(profile.name),
                version=str(policy_settings["version"]),
                max_positions=int(profile.max_positions),
                max_position_pct=float(profile.max_position_pct),
                max_sector_exposure_pct=float(profile.sector_concentration_cap),
                max_strategy_exposure_pct=float(profile.max_strategy_exposure_pct),
                max_event_cluster_exposure_pct=float(
                    profile.max_event_cluster_exposure_pct
                ),
                max_position_risk_contribution_pct=float(
                    profile.max_position_risk_contribution_pct
                ),
                risk_contribution_min_positions=int(
                    profile.risk_contribution_min_positions
                ),
                max_short_exposure_pct=float(profile.max_short_exposure_pct),
                max_single_short_pct=float(profile.max_single_short_pct),
                cash_reserve_pct=float(profile.cash_reserve_pct),
                margin_cash_buffer_pct=float(profile.margin_cash_buffer_pct),
                volatility_lookback_sessions=int(
                    policy_settings["volatility_lookback_sessions"]
                ),
                annualized_volatility_floor=float(
                    policy_settings["annualized_volatility_floor"]
                ),
                congressional_exposure_pct=float(congressional_exposure),
            )
        except (AttributeError, KeyError, TypeError) as exc:
            raise ValueError(
                "portfolio policy settings must explicitly configure the "
                "selected size profile"
            ) from exc


@dataclass(frozen=True)
class PortfolioRiskContext:
    portfolio_value: float
    cash: float
    positions: tuple[PolicyPosition, ...]
    pending_positions: tuple[PolicyPosition, ...]
    sectors: Mapping[str, str]
    annualized_volatility: Mapping[str, float]
    earnings_dates: Mapping[str, int]
    short_interest: Mapping[str, float]
    borrow_available: Mapping[str, bool]
    margin_used: float
    consumed_event_keys: frozenset[str]
    config: PortfolioPolicyConfig

    def __post_init__(self) -> None:
        """Normalize direct construction to the same deep immutability contract."""
        object.__setattr__(self, "positions", tuple(self.positions))
        object.__setattr__(self, "pending_positions", tuple(self.pending_positions))
        object.__setattr__(self, "sectors", _immutable_mapping(self.sectors))
        object.__setattr__(
            self,
            "annualized_volatility",
            _immutable_mapping(self.annualized_volatility),
        )
        object.__setattr__(
            self,
            "earnings_dates",
            _immutable_mapping(self.earnings_dates),
        )
        object.__setattr__(
            self,
            "short_interest",
            _immutable_mapping(self.short_interest),
        )
        object.__setattr__(
            self,
            "borrow_available",
            _immutable_mapping(self.borrow_available),
        )
        object.__setattr__(
            self,
            "consumed_event_keys",
            frozenset(self.consumed_event_keys),
        )


def annualized_volatility(
    prices: pd.DataFrame,
    *,
    lookback_sessions: int,
    floor: float,
) -> float:
    """Calculate bounded historical volatility, respecting the policy floor."""
    if lookback_sessions <= 0:
        raise ValueError("lookback_sessions must be positive")
    closes = prices["Close"].astype(float).dropna().tail(lookback_sessions + 1)
    returns = closes.pct_change().dropna()
    value = float(returns.std(ddof=1) * sqrt(252)) if len(returns) > 1 else 0.0
    return max(float(floor), value)


def _position(
    row: Mapping[str, Any],
    portfolio_value: float,
    price_cache: Mapping[str, pd.DataFrame],
    config: PortfolioPolicyConfig,
) -> PolicyPosition:
    ticker = str(row["ticker"])
    marked_value = float(row["marked_value"])
    volatility = (
        annualized_volatility(
            price_cache[ticker],
            lookback_sessions=config.volatility_lookback_sessions,
            floor=config.annualized_volatility_floor,
        )
        if ticker in price_cache
        else config.annualized_volatility_floor
    )
    return PolicyPosition(
        ticker=ticker,
        direction=str(row.get("direction", "long")),
        weight=abs(marked_value) / portfolio_value,
        sector=str(row.get("sector", "Unknown")),
        strategy_tags=tuple(str(tag) for tag in row.get("strategy_tags", ())),
        risk_tags=tuple(str(tag) for tag in row.get("risk_tags", ())),
        annualized_volatility=volatility,
    )


def build_portfolio_risk_context(
    *,
    portfolio_value: float,
    cash: float,
    current_positions: Iterable[Mapping[str, Any]],
    pending_positions: Iterable[Mapping[str, Any]],
    price_cache: Mapping[str, pd.DataFrame],
    earnings_dates: Mapping[str, int],
    short_interest: Mapping[str, float],
    borrow_available: Mapping[str, bool],
    margin_used: float,
    consumed_event_keys: Iterable[str],
    config: PortfolioPolicyConfig,
) -> PortfolioRiskContext:
    """Build an immutable prospective-book context from authoritative marks."""
    if portfolio_value <= 0:
        raise ValueError("portfolio_value must be positive")

    current = tuple(
        _position(row, portfolio_value, price_cache, config)
        for row in current_positions
    )
    pending = tuple(
        _position(row, portfolio_value, price_cache, config)
        for row in pending_positions
    )
    all_positions = current + pending
    return PortfolioRiskContext(
        portfolio_value=float(portfolio_value),
        cash=float(cash),
        positions=current,
        pending_positions=pending,
        sectors=_immutable_mapping({p.ticker: p.sector for p in all_positions}),
        annualized_volatility=_immutable_mapping(
            {p.ticker: p.annualized_volatility for p in all_positions}
        ),
        earnings_dates=_immutable_mapping(
            {str(ticker): int(days) for ticker, days in earnings_dates.items()}
        ),
        short_interest=_immutable_mapping(
            {str(ticker): float(value) for ticker, value in short_interest.items()}
        ),
        borrow_available=_immutable_mapping(
            {str(ticker): bool(value) for ticker, value in borrow_available.items()}
        ),
        margin_used=float(margin_used),
        consumed_event_keys=frozenset(str(key) for key in consumed_event_keys),
        config=config,
    )
