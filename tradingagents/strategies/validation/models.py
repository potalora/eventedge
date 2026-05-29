"""Dataclasses for the event study pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EventSpec:
    """An event to study: one ticker anchored at one date, tagged with a group."""

    ticker: str
    event_date: str          # YYYY-MM-DD, anchors day 0
    group: str               # aggregation key (e.g. strategy name)
    metadata: dict = field(default_factory=dict)


@dataclass
class MarketModelFit:
    """OLS fit R_stock = alpha + beta * R_market over the estimation window."""

    alpha: float
    beta: float
    r_squared: float
    n_obs: int


@dataclass
class EventCAR:
    """CAR result for a single event."""

    ticker: str
    event_date: str
    group: str
    market_model: MarketModelFit
    daily_ar: list[float] = field(default_factory=list)
    cars: dict[str, float | None] = field(default_factory=dict)  # window label -> CAR
    metadata: dict = field(default_factory=dict)


@dataclass
class BootstrapCI:
    """Percentile bootstrap confidence interval for a mean CAR."""

    lower: float
    upper: float
    confidence: float = 0.95
    n_bootstrap: int = 10_000


@dataclass
class WindowStats:
    """Aggregate stats for one CAR window across many events in a group."""

    window: str
    n_events: int
    mean_car: float
    std_car: float
    t_stat: float
    p_value: float
    ci: BootstrapCI


@dataclass
class AggregateResult:
    """Cross-sectional results for one group (e.g. all earnings_call events)."""

    group: str
    n_events: int
    windows: list[WindowStats] = field(default_factory=list)


@dataclass
class EventStudyResult:
    """Top-level result returned by compute_car()."""

    events: list[EventCAR] = field(default_factory=list)
    aggregates: list[AggregateResult] = field(default_factory=list)
    skipped_tickers: list[str] = field(default_factory=list)
