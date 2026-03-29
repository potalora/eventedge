"""Tests for the Strategist agent."""

import json
import pytest
from unittest.mock import MagicMock, patch

from tradingagents.autoresearch.strategist import Strategist
from tradingagents.autoresearch.models import (
    Strategy,
    ScreenerResult,
    ScreenerCriteria,
    BacktestResults,
)


def _make_screener_result(**overrides) -> ScreenerResult:
    """Helper to create a ScreenerResult with defaults."""
    defaults = dict(
        ticker="AAPL", close=150.0, change_14d=0.05, change_30d=0.10,
        high_52w=180.0, low_52w=120.0, avg_volume_20d=50_000_000,
        volume_ratio=1.2, rsi_14=55.0, ema_10=148.0, ema_50=145.0,
        macd=2.5, boll_position=0.6, iv_rank=0.4, put_call_ratio=0.8,
        options_volume=100_000, market_cap=2.5e12, sector="Technology",
        revenue_growth_yoy=0.15, next_earnings_date="2024-04-25",
        regime="RISK_ON", trading_day_coverage=0.95,
    )
    defaults.update(overrides)
    return ScreenerResult(**defaults)


def _make_config():
    return {
        "llm_provider": "anthropic",
        "autoresearch": {
            "strategies_per_generation": 2,
            "strategist_model": "claude-sonnet-4-20250514",
            "cro_model": "claude-haiku-4-5-20251001",
        },
    }


def _make_strategy_json(name="test_strat", approved=True):
    """Return a JSON string that _parse_strategies can handle."""
    return json.dumps([{
        "name": name,
        "hypothesis": "Mean reversion after oversold conditions",
        "instrument": "stock_long",
        "entry_rules": ["RSI_14 crosses above 30", "price > EMA_10"],
        "exit_rules": ["50% profit target", "25% stop loss"],
        "position_size_pct": 0.05,
        "max_risk_pct": 0.05,
        "time_horizon_days": 30,
        "conviction": 75,
        "parent_ids": [],
        "screener_criteria": {
            "market_cap_range": [1e9, 1e12],
            "min_avg_volume": 500000,
        },
    }])


def _make_cro_response(approved=True):
    return json.dumps({
        "approved": approved,
        "reason": "Strategy meets risk criteria" if approved else "Too risky",
        "risk_score": 3 if approved else 8,
        "concerns": [],
    })


def _make_reflection_response():
    return json.dumps({
        "patterns_that_work": ["Mean reversion in oversold conditions"],
        "patterns_that_fail": ["Momentum in low volume stocks"],
        "next_generation_guidance": ["Focus on high-cap tech stocks"],
        "regime_notes": "RISK_ON environment favors long positions",
    })


class TestParseStrategies:
    """Tests for _parse_strategies()."""

    def test_valid_json(self):
        s = Strategist(MagicMock(), _make_config())
        result = s._parse_strategies(_make_strategy_json(), generation=1, regime="RISK_ON")
        assert len(result) == 1
        assert result[0].name == "test_strat"
        assert result[0].generation == 1
        assert result[0].regime_born == "RISK_ON"

    def test_json_in_code_block(self):
        s = Strategist(MagicMock(), _make_config())
        response = f"```json\n{_make_strategy_json()}\n```"
        result = s._parse_strategies(response, generation=0, regime="RISK_OFF")
        assert len(result) == 1

    def test_invalid_json_returns_empty(self):
        s = Strategist(MagicMock(), _make_config())
        result = s._parse_strategies("not json at all", generation=0, regime="RISK_ON")
        assert result == []

    def test_single_object_wrapped(self):
        s = Strategist(MagicMock(), _make_config())
        single = json.dumps({
            "name": "single",
            "hypothesis": "test",
            "instrument": "stock_long",
            "entry_rules": [],
            "exit_rules": [],
        })
        result = s._parse_strategies(single, generation=0, regime="RISK_ON")
        assert len(result) == 1

    def test_partial_data_uses_defaults(self):
        s = Strategist(MagicMock(), _make_config())
        minimal = json.dumps([{"name": "minimal"}])
        result = s._parse_strategies(minimal, generation=2, regime="CRISIS")
        assert len(result) == 1
        assert result[0].instrument == "stock_long"
        assert result[0].position_size_pct == 0.05


class TestParseCroResponse:
    """Tests for _parse_cro_response()."""

    def test_approved(self):
        s = Strategist(MagicMock(), _make_config())
        approved, reason = s._parse_cro_response(_make_cro_response(True))
        assert approved is True

    def test_rejected(self):
        s = Strategist(MagicMock(), _make_config())
        approved, reason = s._parse_cro_response(_make_cro_response(False))
        assert approved is False
        assert "Too risky" in reason

    def test_invalid_json_rejects(self):
        s = Strategist(MagicMock(), _make_config())
        approved, reason = s._parse_cro_response("garbled response")
        assert approved is False
        assert "unparseable" in reason.lower()

    def test_json_in_code_block(self):
        s = Strategist(MagicMock(), _make_config())
        response = f"```json\n{_make_cro_response(True)}\n```"
        approved, _ = s._parse_cro_response(response)
        assert approved is True


class TestParseReflection:
    """Tests for _parse_reflection()."""

    def test_valid_reflection(self):
        s = Strategist(MagicMock(), _make_config())
        result = s._parse_reflection(_make_reflection_response())
        assert "patterns_that_work" in result
        assert len(result["patterns_that_work"]) == 1

    def test_invalid_returns_defaults(self):
        s = Strategist(MagicMock(), _make_config())
        result = s._parse_reflection("not json")
        assert result["patterns_that_work"] == []
        assert result["regime_notes"] == ""


class TestBuildPrompts:
    """Tests for prompt building methods."""

    def test_propose_prompt_includes_all_sections(self):
        s = Strategist(MagicMock(), _make_config())
        screener_results = [_make_screener_result()]
        prompt = s._build_propose_prompt(
            screener_results, "RISK_ON", 1,
            top_strategies=[], reflections=[], analyst_weights={},
        )
        assert "RISK_ON" in prompt
        assert "AAPL" in prompt
        assert "JSON array" in prompt

    def test_propose_prompt_with_top_strategies(self):
        s = Strategist(MagicMock(), _make_config())
        tops = [{"name": "top1", "fitness_score": 1.5, "instrument": "stock_long"}]
        prompt = s._build_propose_prompt(
            [_make_screener_result()], "RISK_ON", 1,
            top_strategies=tops, reflections=[], analyst_weights={},
        )
        assert "top1" in prompt

    def test_propose_prompt_with_reflections(self):
        s = Strategist(MagicMock(), _make_config())
        refs = [{"patterns_that_work": ["momentum"], "patterns_that_fail": ["mean_rev"],
                 "next_generation_guidance": ["try breakouts"]}]
        prompt = s._build_propose_prompt(
            [_make_screener_result()], "RISK_ON", 1,
            top_strategies=[], reflections=refs, analyst_weights={},
        )
        assert "momentum" in prompt

    def test_cro_prompt_includes_strategy_details(self):
        s = Strategist(MagicMock(), _make_config())
        strat = Strategy(name="test", hypothesis="test hyp", instrument="stock_long",
                         entry_rules=["RSI > 30"], exit_rules=["25% stop loss"])
        prompt = s._build_cro_prompt(strat)
        assert "test hyp" in prompt
        assert "RSI > 30" in prompt
        assert "Chief Risk Officer" in prompt

    def test_reflect_prompt_includes_strategies(self):
        s = Strategist(MagicMock(), _make_config())
        strats = [Strategy(name="s1", fitness_score=1.2, instrument="stock_long", status="backtested")]
        prompt = s._build_reflect_prompt(1, strats, [])
        assert "s1" in prompt
        assert "generation 1" in prompt


class TestPropose:
    """Tests for propose() end-to-end with mocked LLM."""

    @patch.object(Strategist, "_call_llm")
    def test_propose_end_to_end(self, mock_llm):
        db = MagicMock()
        db.get_top_strategies.return_value = []
        db.get_reflections.return_value = []
        db.get_analyst_weights.return_value = {}
        db.insert_strategy.return_value = 42

        # First call: strategist, second call: CRO review
        mock_llm.side_effect = [
            _make_strategy_json("momentum_play"),
            _make_cro_response(True),
        ]

        s = Strategist(db, _make_config())
        result = s.propose([_make_screener_result()], "RISK_ON", generation=1)

        assert len(result) == 1
        assert result[0].name == "momentum_play"
        assert result[0].id == 42
        db.insert_strategy.assert_called_once()

    @patch.object(Strategist, "_call_llm")
    def test_propose_cro_rejects(self, mock_llm):
        db = MagicMock()
        db.get_top_strategies.return_value = []
        db.get_reflections.return_value = []
        db.get_analyst_weights.return_value = {}

        mock_llm.side_effect = [
            _make_strategy_json("risky_play"),
            _make_cro_response(False),
        ]

        s = Strategist(db, _make_config())
        result = s.propose([_make_screener_result()], "RISK_ON", generation=1)

        assert len(result) == 0
        db.insert_strategy.assert_not_called()

    @patch.object(Strategist, "_call_llm")
    def test_propose_invalid_strategist_response(self, mock_llm):
        db = MagicMock()
        db.get_top_strategies.return_value = []
        db.get_reflections.return_value = []
        db.get_analyst_weights.return_value = {}

        mock_llm.return_value = "I can't generate strategies right now."

        s = Strategist(db, _make_config())
        result = s.propose([_make_screener_result()], "RISK_ON", generation=1)
        assert result == []


class TestReflect:
    """Tests for reflect()."""

    @patch.object(Strategist, "_call_llm")
    def test_reflect_writes_to_db(self, mock_llm):
        db = MagicMock()
        mock_llm.return_value = _make_reflection_response()

        s = Strategist(db, _make_config())
        strats = [Strategy(name="s1", fitness_score=1.0, instrument="stock_long", status="backtested")]
        result = s.reflect(generation=1, strategies=strats, top_all_time=[])

        db.insert_reflection.assert_called_once()
        call_kwargs = db.insert_reflection.call_args
        assert call_kwargs[1]["generation"] == 1 or call_kwargs[0][0] == 1
        assert "patterns_that_work" in result

    @patch.object(Strategist, "_call_llm")
    def test_reflect_invalid_response_uses_defaults(self, mock_llm):
        db = MagicMock()
        mock_llm.return_value = "thinking about it..."

        s = Strategist(db, _make_config())
        result = s.reflect(generation=1, strategies=[], top_all_time=[])

        db.insert_reflection.assert_called_once()
        assert result["patterns_that_work"] == []
