"""Tests for CycleTracker — 30-day portfolio evaluation cycles."""
from __future__ import annotations

import json
import os
import tempfile

import pytest


class TestCycleTracker:
    """Test cycle math, boundary detection, and snapshots."""

    @pytest.fixture
    def tracker(self):
        from tradingagents.strategies.state.cycle_tracker import CycleTracker
        tmpdir = tempfile.mkdtemp()
        return CycleTracker(gen_start_date="2026-04-01", state_dir=tmpdir)

    def test_current_cycle_day_one(self, tracker):
        assert tracker.current_cycle("2026-04-01") == 1

    def test_current_cycle_day_fifteen(self, tracker):
        assert tracker.current_cycle("2026-04-15") == 1

    def test_current_cycle_day_thirty(self, tracker):
        assert tracker.current_cycle("2026-04-30") == 1

    def test_current_cycle_day_thirty_one(self, tracker):
        assert tracker.current_cycle("2026-05-01") == 2

    def test_current_cycle_day_sixty(self, tracker):
        assert tracker.current_cycle("2026-05-30") == 2

    def test_current_cycle_day_sixty_one(self, tracker):
        assert tracker.current_cycle("2026-05-31") == 3

    def test_days_remaining_day_one(self, tracker):
        assert tracker.days_remaining("2026-04-01") == 30

    def test_days_remaining_day_fifteen(self, tracker):
        assert tracker.days_remaining("2026-04-15") == 16

    def test_days_remaining_day_thirty(self, tracker):
        assert tracker.days_remaining("2026-04-30") == 1

    def test_days_remaining_day_thirty_one(self, tracker):
        assert tracker.days_remaining("2026-05-01") == 30

    def test_is_boundary_day_twenty_nine(self, tracker):
        assert not tracker.is_boundary("2026-04-29")

    def test_is_boundary_day_thirty(self, tracker):
        assert tracker.is_boundary("2026-04-30")

    def test_is_boundary_day_sixty(self, tracker):
        assert tracker.is_boundary("2026-05-30")

    def test_update_daily(self, tracker):
        tracker.update_daily("2026-04-01", positions=[], portfolio_value=5000.0)
        tracker.update_daily("2026-04-02", positions=[], portfolio_value=5010.0)
        # Should not raise

    def test_snapshot_cycle(self, tracker):
        tracker.update_daily("2026-04-01", positions=[], portfolio_value=5000.0)
        snap = tracker.snapshot_cycle(
            cycle_number=1,
            positions=[],
            closed_trades=[{"pnl": 50.0, "strategy": "earnings_call"}],
            portfolio_value=5050.0,
        )
        assert snap["cycle"] == 1
        assert snap["portfolio_value_end"] == 5050.0
        assert snap["realized_pnl"] == 50.0

    def test_snapshot_persisted_to_disk(self, tracker):
        tracker.update_daily("2026-04-01", positions=[], portfolio_value=5000.0)
        tracker.snapshot_cycle(1, [], [], 5000.0)
        cycle_path = os.path.join(tracker._state_dir, "cycles", "cycle_001.json")
        assert os.path.exists(cycle_path)
        data = json.loads(open(cycle_path).read())
        assert data["cycle"] == 1

    def test_strategy_breakdown_in_snapshot(self, tracker):
        tracker.update_daily("2026-04-01", positions=[], portfolio_value=5000.0)
        closed = [
            {"pnl": 50.0, "strategy": "earnings_call", "holding_days": 18},
            {"pnl": -20.0, "strategy": "earnings_call", "holding_days": 15},
            {"pnl": 30.0, "strategy": "weather_ag", "holding_days": 25},
        ]
        snap = tracker.snapshot_cycle(1, [], closed, 5060.0)
        breakdown = snap["strategy_breakdown"]
        assert "earnings_call" in breakdown
        assert breakdown["earnings_call"]["traded"] == 2
        assert breakdown["earnings_call"]["pnl"] == 30.0
