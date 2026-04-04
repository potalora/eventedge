from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Candidate:
    """A screened candidate for potential trade entry."""

    ticker: str
    date: str
    direction: str  # "long" or "short"
    score: float = 0.0  # Higher = stronger signal
    confidence: float = 0.0
    metadata: dict = field(default_factory=dict)  # Strategy-specific data


@dataclass
class StrategyParams:
    """A set of evolvable parameters for a strategy."""

    id: str  # Unique identifier for this param set
    strategy_name: str
    params: dict[str, Any]  # The actual parameters
    generation: int = 0
    parent_id: str | None = None  # For evolution tracking
    fitness: float = 0.0
    weight: float = 1.0


@dataclass
class BacktestTrade:
    """A single trade from backtesting."""

    ticker: str
    direction: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    pnl: float
    pnl_pct: float
    holding_days: int
    exit_reason: str
    metadata: dict = field(default_factory=dict)


@dataclass
class BacktestResult:
    """Results from backtesting a strategy with specific params."""

    strategy_name: str
    params: StrategyParams
    trades: list[BacktestTrade]
    sharpe: float = 0.0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    num_trades: int = 0
    profit_factor: float = 0.0


@dataclass
class RegimeContext:
    """Market regime indicators for paper-trade decisions."""

    vix_level: float = 0.0
    vix_regime: str = "normal"  # low/normal/elevated/crisis
    credit_spread_bps: float = 0.0
    credit_regime: str = "normal"
    yield_curve_slope: float = 0.0
    yield_regime: str = "normal"  # inverted/flat/normal/steep
    overall_regime: str = "normal"
    timestamp: str = ""


@dataclass
class VintageInfo:
    """Tracks lineage of a parameter set through evolution."""

    vintage_id: str  # UUID
    created_at: str  # ISO timestamp
    source_phase: str  # "backtest" or "paper_learning"
    backtest_generation: int = 0
    completed_trade_count: int = 0
    is_exploration: bool = False


@dataclass
class PlaybookEntry:
    """Per-strategy entry in the backtest->paper-trade playbook."""

    strategy_name: str
    optimized_params: dict
    sharpe: float
    sharpe_ci_lower: float
    sharpe_ci_upper: float
    win_rate: float
    num_backtest_trades: int
    profit_factor: float
    best_regime: str
    worst_regime: str
    vintage_id: str


@runtime_checkable
class StrategyModule(Protocol):
    name: str
    track: str  # "backtest" or "paper_trade"
    data_sources: list[str]

    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        """Return evolvable parameters and their (min, max) ranges.
        For bool params: (True, False). For categorical: tuple of options.

        Args:
            horizon: Investment horizon ("30d", "3m", "6m", "1y").
        """
        ...

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        """Return sensible default parameters.

        Args:
            horizon: Investment horizon ("30d", "3m", "6m", "1y").
        """
        ...

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for entry candidates on a given date."""
        ...

    def check_exit(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        holding_days: int,
        params: dict,
        data: dict,
    ) -> tuple[bool, str]:
        """Check if exit conditions are met. Returns (should_exit, reason)."""
        ...

    def build_propose_prompt(self, context: dict) -> str:
        """Build LLM prompt for proposing new parameter sets."""
        ...
