"""Realized-P&L compounding: closed-trade gains/losses must flow into both
the reconstructed broker cash (buying power for the next run) and the daily
equity snapshot's portfolio_value (reported equity).

Regression for the gen_001 bug where a take-profit winner (IBM +10.6%) showed
the cohort's portfolio_value *dropping*: realized P&L was recorded on the trade
(`pnl` field) but never banked into reconstructed cash, and the snapshot derived
portfolio_value from that stale cash.
"""
import pytest

from tradingagents.execution.paper_broker import PaperBroker
from tradingagents.strategies.state.equity_snapshot import write_snapshot, load_snapshots


class TestReconstructBanksRealizedPnL:
    def test_realized_gain_added_to_cash(self):
        fresh = PaperBroker(initial_capital=5000.0)
        fresh.reconstruct_from_trades(
            [{"ticker": "AAPL", "shares": 10, "entry_price": 150.0}],
            realized_pnl=300.0,
        )
        # 5000 - 1500 (open cost) + 300 (banked realized gain)
        assert fresh.cash == pytest.approx(3800.0)

    def test_realized_loss_subtracted_from_cash(self):
        fresh = PaperBroker(initial_capital=5000.0)
        fresh.reconstruct_from_trades(
            [{"ticker": "AAPL", "shares": 10, "entry_price": 150.0}],
            realized_pnl=-200.0,
        )
        assert fresh.cash == pytest.approx(3300.0)

    def test_default_realized_pnl_is_backward_compatible(self):
        fresh = PaperBroker(initial_capital=5000.0)
        fresh.reconstruct_from_trades(
            [{"ticker": "AAPL", "shares": 10, "entry_price": 150.0}]
        )
        assert fresh.cash == pytest.approx(3500.0)

    def test_realized_pnl_with_no_open_positions(self):
        fresh = PaperBroker(initial_capital=5000.0)
        fresh.reconstruct_from_trades([], realized_pnl=250.0)
        assert fresh.cash == pytest.approx(5250.0)


class TestSnapshotIncludesRealizedPnL:
    def _prices(self, **kv):
        import pandas as pd
        return {t: pd.DataFrame({"Close": [p]}) for t, p in kv.items()}

    def test_portfolio_value_uses_equity_identity(self, tmp_path):
        # One closed winner (+300) and one open position (unrealized +50).
        closed = [{
            "ticker": "IBM", "direction": "long", "shares": 10,
            "entry_price": 100.0, "exit_price": 130.0, "status": "closed",
        }]
        open_trades = [{
            "ticker": "T", "direction": "long", "shares": 10, "entry_price": 50.0,
        }]
        snap = write_snapshot(
            state_dir=str(tmp_path),
            trading_date="2026-06-03",
            cash=999_999.0,  # deliberately wrong: portfolio_value must NOT depend on it
            open_trades=open_trades,
            closed_trades=closed,
            price_cache=self._prices(T=55.0),
            total_capital=10_000.0,
        )
        assert snap["realized_pnl"] == pytest.approx(300.0)
        assert snap["unrealized_pnl"] == pytest.approx(50.0)
        # total_capital + realized + unrealized
        assert snap["portfolio_value"] == pytest.approx(10_350.0)
        assert snap["total_return_pct"] == pytest.approx(3.5)
        # row is internally consistent: cash + long - short == portfolio_value
        assert snap["cash"] + snap["long_value"] - snap["short_liability"] == pytest.approx(
            snap["portfolio_value"]
        )

    def test_take_profit_winner_raises_portfolio_value(self, tmp_path):
        # Mirrors the IBM case: a winner closing should never lower equity.
        # Day 1: open IBM 3 @ 297.80
        write_snapshot(
            state_dir=str(tmp_path), trading_date="2026-06-01", cash=9106.6,
            open_trades=[{"ticker": "IBM", "direction": "long", "shares": 3, "entry_price": 297.80}],
            closed_trades=[], price_cache=self._prices(IBM=297.80), total_capital=10_000.0,
        )
        # Day 2: IBM closed at 329.23 (take_profit, +94.29), no open positions
        snap = write_snapshot(
            state_dir=str(tmp_path), trading_date="2026-06-02", cash=9106.6,
            open_trades=[],
            closed_trades=[{"ticker": "IBM", "direction": "long", "shares": 3,
                            "entry_price": 297.80, "exit_price": 329.23, "status": "closed"}],
            price_cache=self._prices(), total_capital=10_000.0,
        )
        assert snap["realized_pnl"] == pytest.approx(94.29, abs=0.01)
        assert snap["portfolio_value"] == pytest.approx(10_094.29, abs=0.01)
        assert snap["total_return_pct"] > 0  # a winner must show positive return
