from dataclasses import dataclass, field
from datetime import datetime
import json


@dataclass
class Filter:
    """A single quantitative filter for screening."""
    field: str       # e.g. "rsi_14"
    op: str          # "<", ">", "<=", ">=", "==", "between"
    value: float | list[float]  # single value or [low, high] for "between"

    def evaluate(self, actual_value: float) -> bool:
        """Evaluate this filter against an actual value."""
        if self.op == "<":
            return actual_value < self.value
        elif self.op == ">":
            return actual_value > self.value
        elif self.op == "<=":
            return actual_value <= self.value
        elif self.op == ">=":
            return actual_value >= self.value
        elif self.op == "==":
            return actual_value == self.value
        elif self.op == "between":
            return self.value[0] <= actual_value <= self.value[1]
        return False


@dataclass
class ScreenerCriteria:
    """Criteria for filtering the ticker universe."""
    market_cap_range: list[float] = field(default_factory=lambda: [0, float("inf")])
    min_avg_volume: int = 100_000
    sector: str | None = None
    min_options_volume: int | None = None
    custom_filters: list[Filter] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize to dict for JSON storage."""
        return {
            "market_cap_range": self.market_cap_range,
            "min_avg_volume": self.min_avg_volume,
            "sector": self.sector,
            "min_options_volume": self.min_options_volume,
            "custom_filters": [
                {"field": f.field, "op": f.op, "value": f.value}
                for f in self.custom_filters
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScreenerCriteria":
        """Deserialize from dict."""
        raw_filters = d.get("custom_filters", [])
        filters = []
        for f in raw_filters:
            if isinstance(f, dict) and "field" in f and "op" in f:
                filters.append(Filter(**f))
            # Skip strings or malformed items — LLMs sometimes return
            # filters as plain strings like "RSI > 30"
        return cls(
            market_cap_range=d.get("market_cap_range", [0, float("inf")]),
            min_avg_volume=d.get("min_avg_volume", 100_000),
            sector=d.get("sector"),
            min_options_volume=d.get("min_options_volume"),
            custom_filters=filters,
        )


@dataclass
class BacktestResults:
    """Results from backtesting a strategy."""
    sharpe: float = 0.0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    num_trades: int = 0
    tickers_tested: list[str] = field(default_factory=list)
    backtest_period: str = ""
    walk_forward_scores: list[float] = field(default_factory=list)
    holdout_sharpe: float | None = None


@dataclass
class ScreenerResult:
    """Data for a single ticker from a screener run."""
    ticker: str
    close: float
    change_14d: float
    change_30d: float
    high_52w: float
    low_52w: float
    avg_volume_20d: int
    volume_ratio: float
    rsi_14: float
    ema_10: float
    ema_50: float
    macd: float
    boll_position: float  # 0-1, where in the Bollinger band
    iv_rank: float | None
    put_call_ratio: float | None
    options_volume: int | None
    market_cap: float
    sector: str
    revenue_growth_yoy: float | None
    next_earnings_date: str | None
    regime: str  # RISK_ON, RISK_OFF, CRISIS, TRANSITION
    trading_day_coverage: float  # for survivorship filter


@dataclass
class Strategy:
    """A trading strategy with its rules and results."""
    id: int = 0
    generation: int = 0
    parent_ids: list[int] = field(default_factory=list)
    name: str = ""
    screener: ScreenerCriteria = field(default_factory=ScreenerCriteria)
    instrument: str = "stock_long"
    entry_rules: list[str] = field(default_factory=list)
    exit_rules: list[str] = field(default_factory=list)
    position_size_pct: float = 0.05
    max_risk_pct: float = 0.05
    time_horizon_days: int = 30
    hypothesis: str = ""
    conviction: int = 50
    backtest_results: BacktestResults | None = None
    status: str = "proposed"
    regime_born: str = "TRANSITION"
    fitness_score: float = 0.0
    created_at: datetime = field(default_factory=datetime.now)

    def to_db_dict(self) -> dict:
        """Serialize for database insertion. JSON-encodes complex fields."""
        return {
            "generation": self.generation,
            "parent_ids": json.dumps(self.parent_ids),
            "name": self.name,
            "hypothesis": self.hypothesis,
            "conviction": self.conviction,
            "screener_criteria": json.dumps(self.screener.to_dict()),
            "instrument": self.instrument,
            "entry_rules": json.dumps(self.entry_rules),
            "exit_rules": json.dumps(self.exit_rules),
            "position_size_pct": self.position_size_pct,
            "max_risk_pct": self.max_risk_pct,
            "time_horizon_days": self.time_horizon_days,
            "regime_born": self.regime_born,
            "status": self.status,
        }

    @classmethod
    def from_db_dict(cls, row: dict) -> "Strategy":
        """Deserialize from database row dict."""

        def _ensure_list(val):
            if isinstance(val, list):
                return val
            if isinstance(val, str):
                return json.loads(val)
            return []

        def _ensure_dict(val):
            if isinstance(val, dict):
                return val
            if isinstance(val, str):
                return json.loads(val)
            return {}

        return cls(
            id=row["id"],
            generation=row["generation"],
            parent_ids=_ensure_list(row.get("parent_ids")),
            name=row["name"],
            hypothesis=row["hypothesis"],
            conviction=row.get("conviction", 50),
            screener=ScreenerCriteria.from_dict(
                _ensure_dict(row.get("screener_criteria"))
            ),
            instrument=row["instrument"],
            entry_rules=_ensure_list(row.get("entry_rules")),
            exit_rules=_ensure_list(row.get("exit_rules")),
            position_size_pct=row.get("position_size_pct", 0.05),
            max_risk_pct=row.get("max_risk_pct", 0.05),
            time_horizon_days=row.get("time_horizon_days", 30),
            status=row.get("status", "proposed"),
            regime_born=row.get("regime_born", "TRANSITION"),
            fitness_score=row.get("fitness_score", 0.0),
        )

    def to_prompt_str(self) -> str:
        """Human-readable string for LLM prompts."""
        lines = [
            f"Strategy: {self.name} (gen {self.generation}, {self.status})",
            f"Instrument: {self.instrument}",
            f"Hypothesis: {self.hypothesis}",
            f"Entry rules: {', '.join(self.entry_rules)}",
            f"Exit rules: {', '.join(self.exit_rules)}",
            f"Position size: {self.position_size_pct:.0%}, Max risk: {self.max_risk_pct:.0%}",
            f"Time horizon: {self.time_horizon_days} days",
            f"Conviction: {self.conviction}/100",
        ]
        if self.backtest_results:
            br = self.backtest_results
            lines.append(
                f"Backtest: Sharpe={br.sharpe:.2f}, Return={br.total_return:.1%}, "
                f"Win={br.win_rate:.0%}, Trades={br.num_trades}, "
                f"MaxDD={br.max_drawdown:.1%}, PF={br.profit_factor:.2f}"
            )
        if self.fitness_score:
            lines.append(f"Fitness: {self.fitness_score:.4f}")
        return "\n".join(lines)
