"""Execution bridge: unified paper/live trading interface.

Wraps PaperBroker or AlpacaBroker behind a single API. One config switch
(execution.mode) controls which broker is used. The RiskGate is integrated
so every trade passes through safety checks.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from tradingagents.strategies.trading.risk_gate import RiskGate, RiskGateConfig
from tradingagents.execution.base_broker import AccountInfo, BaseBroker, OrderResult

logger = logging.getLogger(__name__)


class ExecutionBridge:
    """Unified interface for paper and live trade execution.

    Usage:
        bridge = ExecutionBridge(config)
        result = bridge.execute_recommendation(rec, price)
        if result and result.status == "filled":
            # record trade
    """

    def __init__(self, config: dict) -> None:
        self.config = config
        self.broker = self._build_broker(config)
        self.risk_gate = RiskGate(
            RiskGateConfig.from_dict(config),
            self.broker,
        )

    @staticmethod
    def _build_broker(config: dict) -> BaseBroker:
        """Instantiate the correct broker based on config."""
        mode = config.get("execution", {}).get("mode", "paper")

        if mode == "live":
            try:
                from tradingagents.execution.alpaca_broker import AlpacaBroker
            except ImportError:
                raise ImportError("alpaca-py required for live trading: pip install alpaca-py")

            api_key = (
                config.get("execution", {}).get("alpaca_api_key")
                or os.environ.get("ALPACA_API_KEY", "")
            )
            secret_key = (
                config.get("execution", {}).get("alpaca_secret_key")
                or os.environ.get("ALPACA_SECRET_KEY", "")
            )
            paper_trading = config.get("execution", {}).get("alpaca_paper", True)

            if not api_key or not secret_key:
                raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY required for live mode")

            logger.info("ExecutionBridge: LIVE mode via Alpaca (paper=%s)", paper_trading)
            return AlpacaBroker(
                api_key=api_key,
                secret_key=secret_key,
                paper=paper_trading,
            )

        # Default: paper mode
        from tradingagents.execution.paper_broker import PaperBroker

        capital = config.get("autoresearch", {}).get("total_capital", 5000.0)
        logger.info("ExecutionBridge: PAPER mode with $%.0f capital", capital)
        return PaperBroker(initial_capital=capital)

    def execute_recommendation(
        self,
        ticker: str,
        direction: str,
        position_size_pct: float,
        confidence: float,
        strategy: str,
        current_price: float,
        open_trades: list[dict] | None = None,
    ) -> OrderResult | None:
        """Full flow: size → gate check → submit order.

        Args:
            ticker: Stock ticker.
            direction: "long" or "short".
            position_size_pct: From TradeRecommendation (0.01-0.10).
            confidence: Signal confidence (0-1).
            strategy: Primary strategy name.
            current_price: Current stock price.
            open_trades: List of open trade dicts for per-strategy limit check.

        Returns:
            OrderResult if trade executed, None if rejected or sized to 0.
        """
        # 1. Compute position size
        shares = self.risk_gate.compute_position_size(
            position_size_pct, current_price,
        )
        if shares <= 0:
            logger.debug("Position sized to 0 shares for %s", ticker)
            return None

        # 2. Run risk gate checks
        position_value = shares * current_price
        passed, reason = self.risk_gate.check(
            ticker, direction, position_value, strategy, open_trades,
        )
        if not passed:
            logger.info("RiskGate rejected %s %s: %s", ticker, direction, reason)
            return None

        # 3. Submit order
        side = "buy" if direction == "long" else "sell"
        result = self.broker.submit_stock_order(
            symbol=ticker, side=side, qty=shares, price=current_price,
        )

        if result.status == "filled":
            logger.info(
                "Executed: %s %d shares of %s @ $%.2f ($%.0f)",
                side.upper(), shares, ticker, current_price, position_value,
            )
        else:
            logger.warning(
                "Order rejected by broker: %s %s — %s",
                ticker, side, result.message,
            )

        return result

    def close_position(
        self, ticker: str, shares: int, current_price: float,
    ) -> OrderResult:
        """Close a position (sell shares)."""
        return self.broker.submit_stock_order(
            symbol=ticker, side="sell", qty=shares, price=current_price,
        )

    def get_account(self) -> AccountInfo:
        """Get current account info."""
        return self.broker.get_account()

    def get_positions(self) -> list[dict]:
        """Get current open positions from broker."""
        return self.broker.get_positions()

    @property
    def is_live(self) -> bool:
        """Whether this bridge is connected to a live broker."""
        mode = self.config.get("execution", {}).get("mode", "paper")
        return mode == "live"
