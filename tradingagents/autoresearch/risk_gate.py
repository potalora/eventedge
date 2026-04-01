"""Risk gate: hard portfolio controls between signal generation and execution.

Every trade must pass ALL gates or it's rejected. This is the safety layer
that prevents unbounded losses on a $5K portfolio.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RiskGateConfig:
    """Portfolio risk parameters."""
    total_capital: float = 5000.0
    max_positions: int = 8
    max_position_pct: float = 0.15          # Max 15% in one position ($750)
    min_position_value: float = 100.0       # Don't open micro-positions
    daily_loss_limit_pct: float = 0.03      # 3% daily = $150
    max_drawdown_pct: float = 0.15          # 15% max DD = $750
    per_strategy_max: int = 3               # Max 3 positions per strategy
    global_stop_loss_pct: float = 0.08      # 8% stop per position
    long_only: bool = True                  # $5K accounts can't short easily

    @classmethod
    def from_dict(cls, config: dict) -> RiskGateConfig:
        """Build from nested config dict (reads autoresearch.risk_gate section)."""
        rg = config.get("autoresearch", {}).get("risk_gate", {})
        total_capital = config.get("autoresearch", {}).get("total_capital", 5000.0)
        return cls(
            total_capital=total_capital,
            max_positions=rg.get("max_positions", 8),
            max_position_pct=rg.get("max_position_pct", 0.15),
            min_position_value=rg.get("min_position_value", 100.0),
            daily_loss_limit_pct=rg.get("daily_loss_limit_pct", 0.03),
            max_drawdown_pct=rg.get("max_drawdown_pct", 0.15),
            per_strategy_max=rg.get("per_strategy_max", 3),
            global_stop_loss_pct=rg.get("global_stop_loss_pct", 0.08),
            long_only=rg.get("long_only", True),
        )


class RiskGate:
    """Enforces hard portfolio risk controls.

    Every trade must pass check() before execution. Position sizing
    is computed by compute_position_size() from portfolio committee
    recommendations. The committee is the sole sizing authority;
    this gate only enforces hard limits.
    """

    def __init__(self, config: RiskGateConfig, broker: Any) -> None:
        """
        Args:
            config: Risk parameters.
            broker: BaseBroker instance (PaperBroker or AlpacaBroker).
        """
        self.config = config
        self.broker = broker
        self._high_water_mark: float = config.total_capital
        self._daily_losses: float = 0.0
        self._daily_date: str = ""

    def check(
        self,
        ticker: str,
        direction: str,
        position_value: float,
        strategy: str,
        open_trades: list[dict] | None = None,
    ) -> tuple[bool, str]:
        """Run all gates. Returns (passed, rejection_reason).

        Args:
            ticker: Stock ticker.
            direction: "long" or "short".
            position_value: Dollar value of proposed position.
            strategy: Strategy name (for per-strategy limit).
            open_trades: List of open trade dicts (from PaperTrader state).
        """
        open_trades = open_trades or []

        # 1. Long-only filter
        if self.config.long_only and direction == "short":
            return False, "long_only: short trades disabled"

        # 2. Max positions (portfolio-wide)
        positions = self.broker.get_positions()
        if len(positions) >= self.config.max_positions:
            return False, f"max_positions: {len(positions)}/{self.config.max_positions}"

        # 3. Per-strategy limit
        strategy_count = sum(
            1 for t in open_trades if t.get("strategy") == strategy
        )
        if strategy_count >= self.config.per_strategy_max:
            return False, f"per_strategy_max: {strategy} has {strategy_count}/{self.config.per_strategy_max}"

        # 4. Position size bounds
        if position_value < self.config.min_position_value:
            return False, f"min_position_value: ${position_value:.0f} < ${self.config.min_position_value:.0f}"

        account = self.broker.get_account()
        max_value = account.portfolio_value * self.config.max_position_pct
        if position_value > max_value:
            return False, f"max_position_pct: ${position_value:.0f} > ${max_value:.0f} ({self.config.max_position_pct:.0%})"

        # 5. Daily loss limit
        if self._daily_losses >= account.portfolio_value * self.config.daily_loss_limit_pct:
            return False, f"daily_loss_limit: ${self._daily_losses:.0f} losses today"

        # 6. Max drawdown
        if self._high_water_mark > 0:
            drawdown = (self._high_water_mark - account.portfolio_value) / self._high_water_mark
            if drawdown >= self.config.max_drawdown_pct:
                return False, f"max_drawdown: {drawdown:.1%} >= {self.config.max_drawdown_pct:.0%}"

        # 7. Duplicate check (already holding this ticker?)
        held_tickers = {p.get("ticker", "") for p in positions}
        if ticker in held_tickers:
            return False, f"duplicate: already holding {ticker}"

        # 8. Buying power check
        if position_value > account.buying_power:
            return False, f"buying_power: need ${position_value:.0f}, have ${account.buying_power:.0f}"

        return True, ""

    def compute_position_size(
        self,
        position_size_pct: float,
        current_price: float,
    ) -> int:
        """Convert committee recommendation to whole shares.

        The portfolio committee is the sole sizing authority. This method
        only enforces hard limits (max position, min position, buying power).

        Args:
            position_size_pct: From TradeRecommendation (0.01-0.10).
            current_price: Current stock price.

        Returns:
            Number of whole shares (0 if position too small).
        """
        if current_price <= 0:
            return 0

        account = self.broker.get_account()
        capital = account.portfolio_value

        # Base allocation from committee recommendation
        base_allocation = capital * position_size_pct

        # Cap at max_position_pct
        max_value = capital * self.config.max_position_pct
        allocation = min(base_allocation, max_value)

        # Cap at available buying power
        allocation = min(allocation, account.buying_power)

        # Convert to whole shares
        shares = int(allocation / current_price)

        # Check minimum position value
        if shares * current_price < self.config.min_position_value:
            return 0

        return shares

    def enforce_stop_losses(
        self,
        open_trades: list[dict],
        price_cache: dict[str, Any],
    ) -> list[str]:
        """Check all open positions against global stop loss.

        Args:
            open_trades: List of open trade dicts with entry_price, ticker, direction.
            price_cache: {ticker: DataFrame with "Close" column}.

        Returns:
            List of trade_ids that should be force-closed.
        """
        force_close: list[str] = []

        for trade in open_trades:
            ticker = trade.get("ticker", "")
            entry_price = trade.get("entry_price", 0)
            direction = trade.get("direction", "long")

            if entry_price <= 0 or not ticker:
                continue

            ticker_prices = price_cache.get(ticker)
            if ticker_prices is None:
                continue

            try:
                if hasattr(ticker_prices, 'empty') and not ticker_prices.empty:
                    current_price = float(ticker_prices["Close"].iloc[-1])
                else:
                    continue
            except (KeyError, IndexError):
                continue

            # Compute P&L
            if direction == "long":
                pnl_pct = (current_price - entry_price) / entry_price
            else:
                pnl_pct = (entry_price - current_price) / entry_price

            if pnl_pct <= -self.config.global_stop_loss_pct:
                trade_id = trade.get("trade_id", "")
                if trade_id:
                    force_close.append(trade_id)
                    logger.warning(
                        "Stop loss triggered: %s %s pnl=%.1f%% (limit=%.1f%%)",
                        ticker, direction, pnl_pct * 100, -self.config.global_stop_loss_pct * 100,
                    )

        return force_close

    def record_daily_loss(self, loss_amount: float) -> None:
        """Record a realized loss for daily limit tracking."""
        self._daily_losses += abs(loss_amount)

    def reset_daily(self, date: str) -> None:
        """Reset daily loss counter for a new trading day."""
        if date != self._daily_date:
            self._daily_losses = 0.0
            self._daily_date = date

    def update_high_water_mark(self) -> None:
        """Update high-water mark from current portfolio value."""
        account = self.broker.get_account()
        if account.portfolio_value > self._high_water_mark:
            self._high_water_mark = account.portfolio_value
