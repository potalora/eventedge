import math
import pytest
import pandas as pd
from tradingagents.strategies.state import equity_snapshot as es


def test_mark_to_market_long_nan_falls_back_to_entry():
    trade = {"entry_price": 100.0, "shares": 10, "direction": "long"}
    pv, upnl = es._mark_to_market(trade, float("nan"))
    assert pv == 1000.0   # entry*shares, NOT nan
    assert upnl == 0.0


def test_mark_to_market_short_nan_falls_back_to_entry():
    trade = {"entry_price": 50.0, "shares": 4, "direction": "short"}
    pv, upnl = es._mark_to_market(trade, float("nan"))
    assert pv == -200.0   # -entry*shares liability
    assert upnl == 0.0


def test_current_price_for_returns_none_on_nan_last_close():
    df = pd.DataFrame({"Close": [101.0, float("nan")]})
    assert es._current_price_for("X", {"X": df}) is None


def test_write_snapshot_atomic_preserves_prior_on_failure(tmp_path, monkeypatch):
    sd = str(tmp_path)
    es.write_snapshot(sd, "2026-06-12", cash=5000, open_trades=[],
                      closed_trades=[], price_cache=None, total_capital=5000)

    def boom(*a, **k):
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(es.os, "replace", boom)
    with pytest.raises(RuntimeError):
        es.write_snapshot(sd, "2026-06-18", cash=4000, open_trades=[],
                          closed_trades=[], price_cache=None, total_capital=5000)

    rows = es.load_snapshots(sd)
    assert [r["date"] for r in rows] == ["2026-06-12"]   # prior intact, not truncated
    assert list(tmp_path.glob("*.tmp")) == []            # no leftover temp files
