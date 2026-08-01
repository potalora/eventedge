"""Pure, deterministic portfolio-policy risk context primitives.

This module deliberately has no ledger, API, or LLM dependencies.  Ledger
adapters provide authoritative ``marked_value`` inputs; cached prices are used
only to calculate historical volatility.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, fields, replace
from math import sqrt
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Iterable, Mapping

import pandas as pd

if TYPE_CHECKING:
    from tradingagents.strategies.trading.portfolio_committee import (
        TradeRecommendation,
    )


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
    max_correlated_shorts: int
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
                max_correlated_shorts=int(profile.max_correlated_shorts),
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


class PortfolioPolicy:
    """Apply deterministic portfolio constraints to a prospective book."""

    _EPSILON = 1e-9
    _HARD_REJECTION_REASONS = frozenset(
        {
            "journal_only",
            "duplicate_ticker",
            "consumed_event",
            "max_positions",
            "borrow_unavailable",
            "max_correlated_shorts",
        }
    )

    def apply(
        self,
        recommendations: list["TradeRecommendation"],
        context: PortfolioRiskContext,
    ) -> list["TradeRecommendation"]:
        """Scale or reject recommendations sequentially in their ranked order."""
        accepted: list["TradeRecommendation"] = []
        working = context
        for recommendation in recommendations:
            allowed, _ = self._max_allowed_weight(recommendation, working)
            weight = min(float(recommendation.position_size_pct), allowed)
            if weight <= self._EPSILON:
                continue
            constrained = replace(
                recommendation,
                position_size_pct=round(weight, 8),
            )
            accepted.append(constrained)
            working = self._with_pending(working, constrained)
        return accepted

    def validate(
        self,
        recommendation: "TradeRecommendation",
        context: PortfolioRiskContext,
    ) -> tuple[bool, str]:
        """Return whether a recommendation fits without policy scaling."""
        allowed, reason = self._max_allowed_weight(recommendation, context)
        if recommendation.position_size_pct <= 0.0:
            if (
                allowed <= self._EPSILON
                and reason in self._HARD_REJECTION_REASONS
            ):
                return False, reason
            return False, "nonpositive_weight"
        if recommendation.position_size_pct <= allowed + self._EPSILON:
            return True, ""
        return False, reason

    def _max_allowed_weight(
        self,
        recommendation: "TradeRecommendation",
        context: PortfolioRiskContext,
    ) -> tuple[float, str]:
        cfg = context.config
        book = context.positions + context.pending_positions

        if recommendation.journal_only:
            return 0.0, "journal_only"
        if any(position.ticker == recommendation.ticker for position in book):
            return 0.0, "duplicate_ticker"
        if recommendation.event_key in context.consumed_event_keys:
            return 0.0, "consumed_event"
        if len(book) >= cfg.max_positions:
            return 0.0, "max_positions"

        sector = self._sector(context.sectors.get(recommendation.ticker))
        strategy_tags = recommendation.strategy_tags or tuple(
            recommendation.contributing_strategies
        )
        risk_tags = recommendation.risk_tags

        strategy_exposure: defaultdict[str, float] = defaultdict(float)
        risk_exposure: defaultdict[str, float] = defaultdict(float)
        sector_exposure: defaultdict[str, float] = defaultdict(float)
        short_exposure = 0.0
        for position in book:
            weight = abs(position.weight)
            sector_exposure[self._sector(position.sector)] += weight
            if position.direction == "short":
                short_exposure += weight
            for tag in position.strategy_tags:
                strategy_exposure[tag] += weight
            for tag in position.risk_tags:
                risk_exposure[tag] += weight

        caps: list[tuple[float, str]] = [
            (cfg.max_position_pct, "max_position"),
            (
                cfg.max_sector_exposure_pct - sector_exposure[sector],
                "max_sector_exposure",
            ),
        ]
        for tag in strategy_tags:
            caps.append(
                (
                    cfg.max_strategy_exposure_pct - strategy_exposure[tag],
                    f"strategy:{tag}",
                )
            )
            if tag == "congressional_trades":
                caps.append(
                    (
                        cfg.congressional_exposure_pct - strategy_exposure[tag],
                        "congressional_exposure",
                    )
                )
        for tag in risk_tags:
            caps.append(
                (
                    cfg.max_event_cluster_exposure_pct - risk_exposure[tag],
                    f"risk_tag:{tag}",
                )
            )

        if recommendation.direction == "long":
            pending_long_notional = sum(
                abs(position.weight) * context.portfolio_value
                for position in context.pending_positions
                if position.direction == "long"
            )
            reserved_cash = cfg.cash_reserve_pct * context.portfolio_value
            caps.append(
                (
                    (
                        context.cash
                        - pending_long_notional
                        - reserved_cash
                    )
                    / context.portfolio_value,
                    "cash_reserve",
                )
            )

        if recommendation.direction == "short":
            if context.borrow_available.get(recommendation.ticker) is not True:
                return 0.0, "borrow_unavailable"

            correlated_shorts = sum(
                1
                for position in book
                if position.direction == "short"
                and self._sector(position.sector) == sector
            )
            if correlated_shorts >= cfg.max_correlated_shorts:
                return 0.0, "max_correlated_shorts"

            pending_short_notional = sum(
                abs(position.weight) * context.portfolio_value
                for position in context.pending_positions
                if position.direction == "short"
            )
            margin_buffer = (
                cfg.margin_cash_buffer_pct * context.portfolio_value
            )
            caps.extend(
                [
                    (cfg.max_single_short_pct, "max_single_short"),
                    (
                        cfg.max_short_exposure_pct - short_exposure,
                        "max_short_exposure",
                    ),
                    (
                        (
                            context.cash
                            - context.margin_used
                            - pending_short_notional
                            - margin_buffer
                        )
                        / context.portfolio_value,
                        "margin_cash_buffer",
                    ),
                ]
            )

        prospective_count = len(book) + 1
        if prospective_count >= cfg.risk_contribution_min_positions:
            candidate_volatility = max(
                context.annualized_volatility.get(
                    recommendation.ticker,
                    cfg.annualized_volatility_floor,
                ),
                cfg.annualized_volatility_floor,
            )
            base_risk = sum(
                abs(position.weight)
                * max(
                    position.annualized_volatility,
                    cfg.annualized_volatility_floor,
                )
                for position in book
            )
            contribution_cap = cfg.max_position_risk_contribution_pct
            risk_weight_cap = (
                contribution_cap
                * base_risk
                / (candidate_volatility * (1.0 - contribution_cap))
                if candidate_volatility > 0 and contribution_cap < 1.0
                else cfg.max_position_pct
            )
            caps.append((risk_weight_cap, "max_risk_contribution"))

        allowed = min(cap for cap, _ in caps)
        reason = next(
            reason
            for cap, reason in caps
            if cap <= allowed + self._EPSILON
        )
        return max(0.0, allowed), reason

    def _with_pending(
        self,
        context: PortfolioRiskContext,
        recommendation: "TradeRecommendation",
    ) -> PortfolioRiskContext:
        cfg = context.config
        position = PolicyPosition(
            ticker=recommendation.ticker,
            direction=recommendation.direction,
            weight=abs(recommendation.position_size_pct),
            sector=self._sector(context.sectors.get(recommendation.ticker)),
            strategy_tags=recommendation.strategy_tags
            or tuple(recommendation.contributing_strategies),
            risk_tags=recommendation.risk_tags,
            annualized_volatility=max(
                context.annualized_volatility.get(
                    recommendation.ticker,
                    cfg.annualized_volatility_floor,
                ),
                cfg.annualized_volatility_floor,
            ),
        )
        return replace(
            context,
            pending_positions=context.pending_positions + (position,),
        )

    @staticmethod
    def _sector(value: object) -> str:
        """Normalize all missing/unknown sector values into one group."""
        sector = str(value).strip() if value is not None else ""
        if not sector or sector.casefold() == "unknown":
            return "Unknown"
        return sector.casefold()
