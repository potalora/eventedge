import pandas as pd
import pytest

from tradingagents.strategies.state import equity_snapshot as es


def test_mark_to_market_long_missing_mark_fails_closed():
    trade = {"entry_price": 100.0, "shares": 10, "direction": "long"}
    with pytest.raises(ValueError, match="missing valid mark"):
        es._mark_to_market(trade, float("nan"))


def test_mark_to_market_short_missing_mark_fails_closed():
    trade = {"entry_price": 50.0, "shares": 4, "direction": "short"}
    with pytest.raises(ValueError, match="missing valid mark"):
        es._mark_to_market(trade, None)


def test_current_price_for_returns_none_on_nan_last_close():
    df = pd.DataFrame({"Close": [101.0, float("nan")]})
    assert es._current_price_for("X", {"X": df}) is None


def test_load_snapshots_uses_legacy_jsonl_only_without_ledger(tmp_path):
    path = tmp_path / es.SNAPSHOT_FILENAME
    path.write_text('{"date":"2026-06-12","portfolio_value":5000}\n')
    assert es.load_snapshots(str(tmp_path)) == [
        {"date": "2026-06-12", "portfolio_value": 5000}
    ]
