"""Tests for MultiStrategyEngine._resolve_signals.

Regression: supply_chain emits one candidate per news article, and LLM
enrichment can tag the same ticker with opposing directions (e.g. 1 short +
3 long for AAPL). The old conflict-resolution cancelled opposing same-ticker
directions, removing BOTH and silencing the strategy. We now collapse to the
single highest-conviction candidate per (strategy, ticker) instead.
"""
from __future__ import annotations

from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine


def _sig(strategy, ticker, direction, score):
    return {"strategy": strategy, "ticker": ticker, "direction": direction, "score": score}


def test_same_strategy_ticker_mixed_directions_not_cancelled():
    signals = [
        _sig("supply_chain", "AAPL", "short", 0.65),
        _sig("supply_chain", "AAPL", "long", 0.60),
        _sig("supply_chain", "AAPL", "long", 0.62),
        _sig("supply_chain", "AAPL", "long", 0.62),
    ]
    out = MultiStrategyEngine._resolve_signals(signals)
    # Exactly one AAPL aggregate survives, preserving bull/bear disagreement & net conviction.
    assert len(out) == 1
    assert out[0]["ticker"] == "AAPL"
    assert out[0]["direction"] == "long"
    assert out[0]["long_count"] == 3
    assert out[0]["short_count"] == 1
    assert out[0]["disagreement"] is True
    assert round(out[0]["net_conviction"], 4) == round((0.60 + 0.62 + 0.62 - 0.65) / 4, 4)
    assert len(out[0]["bull_evidence"]) == 3
    assert len(out[0]["bear_evidence"]) == 1


def test_cross_strategy_conflict_preserved():
    # Different strategies disagreeing on a ticker are both kept (unchanged behavior).
    signals = [
        _sig("congressional_trades", "T", "long", 8.0),
        _sig("insider_activity", "T", "short", 0.5),
    ]
    out = MultiStrategyEngine._resolve_signals(signals)
    assert len(out) == 2
    assert {s["direction"] for s in out} == {"long", "short"}


def test_distinct_tickers_all_kept():
    signals = [
        _sig("filing_analysis", "CGC", "long", 0.6),
        _sig("commodity_macro", "COPX", "short", 0.5),
        _sig("govt_contracts", "LMT", "long", 1.0),
    ]
    out = MultiStrategyEngine._resolve_signals(signals)
    assert len(out) == 3
    assert {s["ticker"] for s in out} == {"CGC", "COPX", "LMT"}


def test_empty_ticker_dropped():
    signals = [_sig("litigation", "", "short", 0.5), _sig("litigation", "LCID", "short", 0.5)]
    out = MultiStrategyEngine._resolve_signals(signals)
    assert [s["ticker"] for s in out] == ["LCID"]
