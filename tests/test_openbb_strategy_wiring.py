from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tradingagents.strategies.orchestration.multi_strategy_engine import (
    MultiStrategyEngine,
)


def _bare_engine() -> MultiStrategyEngine:
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine._emit = MagicMock()
    return engine


def test_fetch_all_data_wires_openbb_for_strategy_screening():
    engine = _bare_engine()
    engine.paper_trade_strategies = [SimpleNamespace(data_sources=["openbb"])]
    engine.registry = MagicMock()
    engine.registry.available_sources.return_value = ["openbb"]
    engine._fetch_openbb_strategy_data = MagicMock()

    with patch(
        "tradingagents.strategies.orchestration.multi_strategy_engine._gather_with_timeout",
        return_value={"openbb": {"government_trades": {"trades": []}}},
    ) as gather:
        data = engine._fetch_all_data("2026-06-01", "2026-07-01")

    fetches = gather.call_args.args[0]
    assert fetches["openbb"] == (engine._fetch_openbb_strategy_data, ())
    assert "openbb" in data


def test_openbb_strategy_data_preserves_government_trade_shape():
    engine = _bare_engine()
    source = MagicMock()
    source.fetch.return_value = {
        "trades": [
            {
                "ticker": "AAPL",
                "transaction_type": "Purchase",
                "amount": "$15,001 - $50,000",
            }
        ]
    }
    engine.registry = MagicMock()
    engine.registry.get.return_value = source

    result = engine._fetch_openbb_strategy_data()

    source.fetch.assert_called_once_with({"method": "equity_government_trades"})
    assert result["government_trades"]["trades"][0]["ticker"] == "AAPL"


def test_openbb_strategy_data_surfaces_missing_fmp_error(caplog):
    engine = _bare_engine()
    source = MagicMock()
    source.fetch.return_value = {"error": "equity_government_trades fetch failed"}
    engine.registry = MagicMock()
    engine.registry.get.return_value = source

    result = engine._fetch_openbb_strategy_data()

    assert result["government_trades"]["error"]
    assert "Set FMP_API_KEY" in caplog.text
