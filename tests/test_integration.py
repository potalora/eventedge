"""
End-to-end integration test that verifies all modules work together.
Uses mocks for LLM and external APIs to avoid real API calls.
"""
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock

import pandas as pd
import numpy as np
from langchain_core.messages import AIMessage

from tradingagents.storage.db import Database
from tradingagents.backtesting.portfolio import Portfolio, Order
from tradingagents.backtesting.metrics import compute_metrics
from tradingagents.execution.paper_broker import PaperBroker
from tradingagents.execution.position_manager import PositionManager
from tradingagents.scheduler.alerts import AlertManager


class TestIntegrationPipeline:
    @pytest.fixture
    def tmp_db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = Database(path)
        yield db
        db.close()
        os.unlink(path)

    def test_full_pipeline_stock_trade(self, tmp_db):
        """Simulate: analysis -> decision -> execution -> storage -> metrics"""
        # 1. Simulate a BUY decision
        decision_id = tmp_db.insert_decision(
            ticker="SOFI", trade_date="2026-03-01", rating="BUY",
            full_decision="Rating: BUY\nBuy SOFI based on strong momentum",
            options_report="IV is low, consider long calls",
        )

        # 2. Store analyst reports
        for report_type in ["fundamentals", "technical", "news", "sentiment", "options"]:
            tmp_db.insert_report(
                decision_id=decision_id, ticker="SOFI",
                trade_date="2026-03-01", report_type=report_type,
                content=f"Mock {report_type} report for SOFI",
            )

        # 3. Execute via paper broker
        broker = PaperBroker(initial_capital=5000.0)
        config = {
            "execution": {"execution_enabled": True, "confirm_before_trade": False},
            "backtest": {"max_position_pct": 0.35, "max_options_risk_pct": 0.05},
        }
        pm = PositionManager(broker=broker, config=config)
        results = pm.execute_decision(
            decision_text="Rating: BUY\nBuy SOFI",
            rating="BUY", ticker="SOFI", current_price=10.0,
        )

        assert len(results) == 1
        assert results[0].status == "filled"

        # 4. Record trade in DB
        tmp_db.insert_trade(
            decision_id=decision_id, ticker="SOFI",
            instrument_type="stock", action="buy",
            quantity=results[0].filled_qty, price=10.0,
            option_details=None, status="filled", pnl=None,
        )

        # 5. Verify storage
        trades = tmp_db.get_trades_for_ticker("SOFI")
        assert len(trades) == 1
        reports = tmp_db.get_reports_for_decision(decision_id)
        assert len(reports) == 5

        # 6. Verify broker state
        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0]["ticker"] == "SOFI"

    def test_portfolio_metrics_pipeline(self):
        """Simulate backtest portfolio -> metrics computation"""
        portfolio = Portfolio(initial_capital=5000.0)

        # Simulate trades
        buy = Order(ticker="SOFI", action="buy", quantity=100,
                    instrument_type="stock", price=10.0)
        portfolio.execute_order(buy, fill_price=10.0, date="2025-09-01", slippage_bps=0)

        # Record daily snapshots
        for i, price in enumerate([10.0, 10.5, 10.2, 11.0, 11.5]):
            date = f"2025-09-{i+1:02d}"
            portfolio.record_snapshot(date, {"SOFI": price})

        # Sell
        sell = Order(ticker="SOFI", action="sell", quantity=100,
                     instrument_type="stock", price=11.5)
        portfolio.execute_order(sell, fill_price=11.5, date="2025-09-05", slippage_bps=0)
        portfolio.trade_log[-1]["pnl"] = 150.0

        # Compute metrics
        equity_df = pd.DataFrame(portfolio.get_equity_curve())
        metrics = compute_metrics(equity_df, portfolio.trade_log)

        assert metrics["total_trades"] == 1
        assert metrics["win_rate"] == 1.0
        assert metrics["total_return"] > 0

    def test_alerts_integration(self):
        """Verify alert manager handles all alert types gracefully"""
        config = {
            "alerts": {
                "enabled": True,
                "channels": [],  # no real channels
                "notify_on": ["new_signal", "stop_loss"],
            }
        }
        am = AlertManager(config)
        # Should not raise even without channels
        am.send("new_signal", "SOFI rated BUY")
        am.send("stop_loss", "PLTR hit stop")
        am.send("daily_summary", "Should be skipped")
