"""Integration tests for short trade and covered call overlay pipelines."""
from __future__ import annotations

import tempfile
from unittest.mock import patch

from tradingagents.strategies.orchestration.cohort_orchestrator import (
    PortfolioSizeProfile,
    SIZE_PROFILES,
)
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
from tradingagents.strategies.trading.execution_bridge import ExecutionBridge
from tradingagents.strategies.trading.paper_trader import PaperTrader
from tradingagents.strategies.state.state import StateManager
from tradingagents.strategies.modules.base import OptionSpec


class TestIntegrationShortPipeline:
    """End-to-end: short signal → committee → risk gate → PaperBroker → state."""

    def test_short_trade_full_pipeline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = StateManager(tmpdir)
            config = {
                "execution": {"mode": "paper"},
                "autoresearch": {
                    "total_capital": 50_000,
                    "risk_gate": {"long_only": False},
                    "paper_trade": {"portfolio_committee_enabled": False},
                },
            }
            profile = SIZE_PROFILES["50k"]

            # 1. Signals — two strategies agree on short AAPL
            signals = [
                {"ticker": "AAPL", "direction": "short", "score": 0.85, "strategy": "litigation", "metadata": {}},
                {"ticker": "AAPL", "direction": "short", "score": 0.75, "strategy": "congressional_trades", "metadata": {}},
            ]

            # 2. Committee synthesis (rule-based, LLM disabled)
            committee = PortfolioCommittee(config, size_profile=profile)
            recs = committee.synthesize(signals, total_capital=50_000)
            assert len(recs) >= 1
            rec = recs[0]
            assert rec.direction == "short"
            assert rec.ticker == "AAPL"

            # 3. Execution bridge
            bridge = ExecutionBridge(config)
            bridge.risk_gate.config.long_only = False
            bridge.risk_gate.config.total_capital = 50_000

            result = bridge.execute_recommendation(
                ticker=rec.ticker, direction=rec.direction,
                position_size_pct=rec.position_size_pct,
                confidence=rec.confidence, strategy="litigation",
                current_price=150.0,
            )
            assert result is not None
            assert result.status == "filled"

            # 4. Record in state
            trader = PaperTrader(state)
            trade_id = trader.open_trade(
                strategy="litigation", ticker="AAPL", direction="short",
                entry_price=150.0, entry_date="2026-04-04",
                shares=result.filled_qty,
                position_value=result.filled_qty * 150.0,
            )
            assert trade_id

            # 5. Verify state
            open_trades = state.load_paper_trades(status="open")
            assert len(open_trades) == 1
            assert open_trades[0]["direction"] == "short"

            # 6. Close trade with profit (short at 150, cover at 140 = profit)
            trader.close_trade(trade_id, exit_price=140.0, exit_date="2026-04-10", exit_reason="take_profit")
            closed = state.load_paper_trades(status="closed")
            assert len(closed) == 1
            assert closed[0]["pnl"] > 0  # short at 150, cover at 140 = profit

    def test_ineligible_cohort_blocks_shorts(self):
        """5k cohort should not produce any short trades."""
        config = {
            "execution": {"mode": "paper"},
            "autoresearch": {
                "total_capital": 5_000,
                "risk_gate": {"long_only": True},
                "paper_trade": {"portfolio_committee_enabled": False},
            },
        }
        profile = SIZE_PROFILES["5k"]
        signals = [
            {"ticker": "AAPL", "direction": "short", "score": 0.9, "strategy": "litigation", "metadata": {}},
            {"ticker": "AAPL", "direction": "short", "score": 0.8, "strategy": "congressional_trades", "metadata": {}},
        ]
        committee = PortfolioCommittee(config, size_profile=profile)
        recs = committee.synthesize(signals, total_capital=5_000)
        short_recs = [r for r in recs if r.direction == "short"]
        assert len(short_recs) == 0

    def test_covered_call_overlay_pipeline(self):
        profile = SIZE_PROFILES["50k"]
        config = {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": True}}}
        committee = PortfolioCommittee(config, size_profile=profile)

        positions = [
            {"ticker": "AAPL", "direction": "long", "entry_price": 150.0,
             "entry_date": "2026-03-01", "shares": 10},
        ]

        mock_llm_result = [
            {"ticker": "AAPL", "strike_offset_pct": 0.05, "expiry_days": 30,
             "rationale": "Sideways, IV elevated"}
        ]

        with patch.object(committee, "_llm_covered_call_overlay", return_value=mock_llm_result):
            overlays = committee.generate_covered_call_overlays(
                current_positions=positions,
                iv_data={"AAPL": {"iv_rank": 55, "iv": 0.30}},
                earnings_dates={"AAPL": 40},
                trading_date="2026-04-04",
            )

        assert len(overlays) == 1
        assert overlays[0].vehicle == "option"
        assert overlays[0].option_spec.strategy == "covered_call"
        assert overlays[0].option_spec.expiry_target_days == 30

    def test_short_with_borrow_cost_rejection(self):
        """High SI% stock should be rejected by risk gate."""
        config = {
            "execution": {"mode": "paper"},
            "autoresearch": {
                "total_capital": 50_000,
                "risk_gate": {"long_only": False},
            },
        }
        bridge = ExecutionBridge(config)
        bridge.risk_gate.config.long_only = False
        bridge.risk_gate.config.max_borrow_cost_pct = 0.05

        # GME with 35% SI should be rejected
        passed, reason = bridge.risk_gate.check(
            "GME", "short", 5000, "litigation",
            short_interest={"GME": 35.0},
        )
        assert not passed
        assert "borrow_cost" in reason

    def test_short_pipeline_broker_tracks_position(self):
        """Short execution should register in PaperBroker.short_positions."""
        config = {
            "execution": {"mode": "paper"},
            "autoresearch": {
                "total_capital": 50_000,
                "risk_gate": {"long_only": False},
                "paper_trade": {"portfolio_committee_enabled": False},
            },
        }
        bridge = ExecutionBridge(config)

        result = bridge.execute_recommendation(
            ticker="TSLA", direction="short", position_size_pct=0.05,
            confidence=0.80, strategy="litigation", current_price=200.0,
        )
        assert result is not None
        assert result.status == "filled"
        # Verify PaperBroker short_positions tracking
        assert "TSLA" in bridge.broker.short_positions

    def test_cover_removes_short_from_broker(self):
        """Covering a short removes it from PaperBroker.short_positions."""
        config = {
            "execution": {"mode": "paper"},
            "autoresearch": {
                "total_capital": 50_000,
                "risk_gate": {"long_only": False},
            },
        }
        bridge = ExecutionBridge(config)

        # Open short
        open_result = bridge.execute_recommendation(
            ticker="TSLA", direction="short", position_size_pct=0.05,
            confidence=0.80, strategy="litigation", current_price=200.0,
        )
        assert open_result is not None
        assert "TSLA" in bridge.broker.short_positions

        qty = bridge.broker.short_positions["TSLA"]["quantity"]
        cover_result = bridge.close_position("TSLA", shares=qty, current_price=180.0, direction="short")
        assert cover_result.status == "filled"
        assert "TSLA" not in bridge.broker.short_positions

    def test_short_pnl_computation_in_state(self):
        """PaperTrader.close_trade should compute positive PnL for profitable short."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = StateManager(tmpdir)
            trader = PaperTrader(state)

            # Open a short at 200
            trade_id = trader.open_trade(
                strategy="supply_chain", ticker="TSLA", direction="short",
                entry_price=200.0, entry_date="2026-04-01",
                shares=10, position_value=2000.0,
            )
            assert trade_id

            # Close at 180 — profitable (sold high, bought low)
            trader.close_trade(trade_id, exit_price=180.0, exit_date="2026-04-10", exit_reason="take_profit")

            closed = state.load_paper_trades(status="closed")
            assert len(closed) == 1
            trade = closed[0]
            assert trade["pnl"] > 0
            assert trade["pnl_pct"] > 0

    def test_short_pnl_negative_when_loss(self):
        """PaperTrader should record negative PnL when short price moves against."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = StateManager(tmpdir)
            trader = PaperTrader(state)

            trade_id = trader.open_trade(
                strategy="supply_chain", ticker="TSLA", direction="short",
                entry_price=200.0, entry_date="2026-04-01",
                shares=10, position_value=2000.0,
            )

            # Close at 220 — loss (price went up against our short)
            trader.close_trade(trade_id, exit_price=220.0, exit_date="2026-04-10", exit_reason="stop_loss")

            closed = state.load_paper_trades(status="closed")
            assert len(closed) == 1
            trade = closed[0]
            assert trade["pnl"] < 0
            assert trade["pnl_pct"] < 0

    def test_10k_cohort_allows_covered_calls_not_shorts(self):
        """10k profile: covered calls eligible, short selling not eligible."""
        profile = SIZE_PROFILES["10k"]
        assert profile.options_eligible == ["covered_call"]
        assert not profile.short_eligible
        assert profile.max_short_exposure_pct == 0.0

        config = {
            "autoresearch": {
                "total_capital": 10_000,
                "risk_gate": {"long_only": True},
                "paper_trade": {"portfolio_committee_enabled": False},
            },
        }
        signals = [
            {"ticker": "AAPL", "direction": "short", "score": 0.9, "strategy": "litigation", "metadata": {}},
            {"ticker": "AAPL", "direction": "short", "score": 0.8, "strategy": "supply_chain", "metadata": {}},
        ]
        committee = PortfolioCommittee(config, size_profile=profile)
        recs = committee.synthesize(signals, total_capital=10_000)
        short_recs = [r for r in recs if r.direction == "short"]
        assert len(short_recs) == 0

    def test_covered_call_overlay_blocked_for_ineligible_profile(self):
        """5k profile has no options_eligible — overlay should return empty."""
        profile = SIZE_PROFILES["5k"]
        assert "covered_call" not in profile.options_eligible

        config = {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}}
        committee = PortfolioCommittee(config, size_profile=profile)

        overlays = committee.generate_covered_call_overlays(
            current_positions=[
                {"ticker": "AAPL", "direction": "long", "entry_price": 150.0,
                 "entry_date": "2026-03-01", "shares": 10},
            ],
            iv_data={"AAPL": {"iv_rank": 70, "iv": 0.40}},
            earnings_dates={"AAPL": 60},
            trading_date="2026-04-04",
        )
        assert len(overlays) == 0

    def test_100k_profile_has_higher_short_limits(self):
        """100k profile should have higher short exposure limits than 50k."""
        profile_50k = SIZE_PROFILES["50k"]
        profile_100k = SIZE_PROFILES["100k"]

        assert profile_100k.short_eligible
        assert profile_100k.max_short_exposure_pct >= profile_50k.max_short_exposure_pct
        assert profile_100k.max_correlated_shorts > profile_50k.max_correlated_shorts

    def test_short_trade_rejected_when_position_too_small(self):
        """Short position rejected when floor exceeds max_position_pct cap."""
        config = {
            "execution": {"mode": "paper"},
            "autoresearch": {
                "total_capital": 50_000,
                "risk_gate": {
                    "long_only": False,
                    "min_position_value": 10_000,
                    "max_position_pct": 0.01,  # $500 cap, below $10k floor
                },
            },
        }
        bridge = ExecutionBridge(config)

        # Floor ($10k) exceeds max_position cap ($500), so trade is rejected
        result = bridge.execute_recommendation(
            ticker="AAPL", direction="short", position_size_pct=0.001,
            confidence=0.80, strategy="litigation", current_price=150.0,
        )
        assert result is None

    def test_multi_signal_short_aggregation(self):
        """Multiple strategies signaling short on same ticker should aggregate correctly."""
        config = {
            "autoresearch": {
                "total_capital": 100_000,
                "risk_gate": {"long_only": False},
                "paper_trade": {"portfolio_committee_enabled": False},
            },
        }
        profile = SIZE_PROFILES["100k"]

        signals = [
            {"ticker": "GME", "direction": "short", "score": 0.9, "strategy": "supply_chain", "metadata": {}},
            {"ticker": "GME", "direction": "short", "score": 0.85, "strategy": "litigation", "metadata": {}},
            {"ticker": "GME", "direction": "short", "score": 0.80, "strategy": "congressional_trades", "metadata": {}},
        ]

        committee = PortfolioCommittee(config, size_profile=profile)
        recs = committee.synthesize(signals, total_capital=100_000)

        short_recs = [r for r in recs if r.direction == "short" and r.ticker == "GME"]
        assert len(short_recs) == 1
        rec = short_recs[0]
        # Three strategies contributing
        assert len(rec.contributing_strategies) == 3
        assert "supply_chain" in rec.contributing_strategies
        assert "litigation" in rec.contributing_strategies
        assert "congressional_trades" in rec.contributing_strategies
