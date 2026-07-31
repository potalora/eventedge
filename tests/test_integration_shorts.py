"""Integration tests for short trade and covered call overlay pipelines."""
from __future__ import annotations

import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from tradingagents.strategies.execution import Fill, MarketBar, OrderIntent, SignalRecord, stable_id
from tradingagents.strategies.execution.cost_model import PaperCostModel
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    SIZE_PROFILES,
)
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
from tradingagents.strategies.trading.execution_bridge import ExecutionBridge
from tradingagents.strategies.trading.paper_trader import PaperTrader
from tradingagents.strategies.state.state import StateManager


class TestIntegrationShortPipeline:
    """End-to-end: short signal → committee → risk gate → PaperBroker → state."""

    def test_short_trade_full_pipeline(self, tmp_path):
        state = StateManager(str(tmp_path / "state"))
        config = {
            "execution": {"mode": "paper"},
            "autoresearch": {
                "total_capital": 50_000,
                "risk_gate": {
                    "long_only": False,
                    "min_position_value": 1,
                    "max_position_pct": 0.20,
                },
                "short_selling": {"borrow_cost_reject_above": "0.05"},
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

        ledger = PortfolioLedger(
            state.portfolio_ledger_path, "cohort", Decimal("50000"),
            short_selling_config=config["autoresearch"]["short_selling"],
        )
        try:
            bridge = ExecutionBridge(config, ledger=ledger)
            decided = datetime(2026, 7, 31, 20, tzinfo=timezone.utc)
            signal = SignalRecord(
                "short-signal", "epoch", "policy", "event", "litigation",
                "AAPL", "short", decided, decided, date(2026, 7, 31),
                Decimal("150"), decided, "evidence",
            )
            second_signal = SignalRecord(
                "congress-short-signal", "epoch", "policy", "congress-event",
                "congressional_trades", "AAPL", "short", decided, decided,
                date(2026, 7, 31), Decimal("150"), decided, "congress-evidence",
            )
            ledger.record_signal(signal)
            ledger.record_signal(second_signal)
            intent = bridge.stage_intent(
                rec,
                (signal, second_signal),
                ledger.account_state(),
                decided,
                date(2026, 8, 3),
            )
            bar = MarketBar(
                "AAPL", date(2026, 8, 3), Decimal("150"), Decimal("152"),
                Decimal("149"), Decimal("151"), "fixture",
                datetime(2026, 8, 3, 22, tzinfo=timezone.utc), False,
            )
            result = bridge.execute_due_intent(
                intent, bar, ledger.account_state(),
                {
                    "strategy": "litigation",
                    "borrow_rate": Decimal("0.02"),
                    "processing_at": datetime(2026, 8, 3, 22, tzinfo=timezone.utc),
                },
                PaperCostModel(),
            )
            assert result.status == "filled"

            # 3. Cover through the authoritative ledger, then consume only its
            # compatibility projection downstream.
            cover = OrderIntent(
                "cover-short-aapl", intent.signal_ids, ledger.cohort_id, "cover",
                result.fill.quantity, datetime(2026, 8, 3, 23, tzinfo=timezone.utc),
                date(2026, 8, 4), "next_session_open", "pending", None, None,
            )
            lot_id = stable_id("lot", result.fill.fill_id)
            ledger.stage_exit_intent(cover, ((lot_id, result.fill.quantity),))
            cover_at = datetime(2026, 8, 4, 22, tzinfo=timezone.utc)
            ledger.apply_fill(
                cover,
                Fill(
                    "cover-fill-aapl", cover.intent_id, "cover", date(2026, 8, 4),
                    cover_at, cover_at, Decimal("140"), Decimal("140"),
                    result.fill.quantity, Decimal("0"), Decimal("0"), Decimal("0"),
                ),
            )

            closed = state.load_paper_trades(status="closed")
            assert len(closed) == 1
            trade = closed[0]
            assert trade["direction"] == "short"
            assert trade["pnl"] > 0
            assert trade["pnl_pct"] == pytest.approx(
                (trade["entry_price"] - trade["exit_price"]) / trade["entry_price"]
            )
        finally:
            ledger.close()

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

    def test_short_with_borrow_cost_rejection(self, tmp_path):
        """High SI% stock should be rejected by risk gate."""
        config = {
            "execution": {"mode": "paper"},
            "autoresearch": {
                "total_capital": 50_000,
                "risk_gate": {"long_only": False},
            },
        }
        ledger = PortfolioLedger(tmp_path / "ledger.db", "cohort", Decimal("50000"))
        try:
            bridge = ExecutionBridge(config, ledger=ledger)
            bridge.risk_gate.config.long_only = False
            bridge.risk_gate.config.max_borrow_cost_pct = 0.05
            passed, reason = bridge.risk_gate.check(
                "GME", "short", 5000, "litigation",
                short_interest={"GME": 35.0},
            )
            assert not passed
            assert "borrow_cost" in reason
        finally:
            ledger.close()

    def test_short_pnl_computation_in_state(self):
        """PaperTrader cannot create a short outside the ledger."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = StateManager(tmpdir)
            trader = PaperTrader(state)

            with pytest.raises(RuntimeError, match="read-only"):
                trader.open_trade(
                    strategy="supply_chain", ticker="TSLA", direction="short",
                    entry_price=200.0, entry_date="2026-04-01",
                    shares=10, position_value=2000.0,
                )

    def test_short_pnl_negative_when_loss(self):
        """PaperTrader rejects direct short closure regardless of outcome."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state = StateManager(tmpdir)
            trader = PaperTrader(state)

            with pytest.raises(RuntimeError, match="read-only"):
                trader.close_trade(
                    "missing", exit_price=220.0, exit_date="2026-04-10",
                    exit_reason="stop_loss",
                )

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

    def test_short_trade_rejected_when_position_too_small(self, tmp_path):
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
        ledger = PortfolioLedger(tmp_path / "ledger.db", "cohort", Decimal("50000"))
        try:
            bridge = ExecutionBridge(config, ledger=ledger)
            # Floor ($10k) exceeds max-position cap ($500), so sizing is zero.
            assert bridge.risk_gate.compute_position_size(0.001, 150.0) == 0
        finally:
            ledger.close()

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
