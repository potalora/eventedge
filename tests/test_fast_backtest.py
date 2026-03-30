"""Tests for tradingagents.autoresearch.fast_backtest and fast mode integration."""

import json
import pytest
from unittest.mock import patch, MagicMock

from tradingagents.autoresearch.models import ScreenerResult
from tradingagents.autoresearch.fast_backtest import FastBacktestRunner
from tradingagents.autoresearch.cached_pipeline import CachedPipelineRunner


def _make_screener_result(**overrides) -> ScreenerResult:
    """Create a ScreenerResult with sensible defaults."""
    defaults = dict(
        ticker="AAPL", close=150.0, change_14d=0.05, change_30d=0.10,
        high_52w=180.0, low_52w=120.0, avg_volume_20d=50_000_000,
        volume_ratio=1.2, rsi_14=55.0, ema_10=148.0, ema_50=145.0,
        macd=0.5, boll_position=0.6, iv_rank=45.0, put_call_ratio=0.8,
        options_volume=100_000, market_cap=2.5e12, sector="Technology",
        revenue_growth_yoy=0.08, next_earnings_date="2026-04-15",
        regime="RISK_ON", trading_day_coverage=0.95,
    )
    defaults.update(overrides)
    return ScreenerResult(**defaults)


def _make_config(**ar_overrides) -> dict:
    config = {"autoresearch": {"fast_backtest": True, "cache_model": "claude-haiku-4-5-20251001"}}
    config["autoresearch"].update(ar_overrides)
    return config


class TestPromptConstruction:
    def test_prompt_contains_ticker_and_date(self):
        runner = FastBacktestRunner(MagicMock(), _make_config())
        sr = _make_screener_result()
        prompt = runner._build_prompt("AAPL", "2026-03-01", sr)
        assert "AAPL" in prompt
        assert "2026-03-01" in prompt

    def test_prompt_contains_price_data(self):
        runner = FastBacktestRunner(MagicMock(), _make_config())
        sr = _make_screener_result(close=155.50, change_14d=0.03, change_30d=-0.02)
        prompt = runner._build_prompt("MSFT", "2026-03-01", sr)
        assert "$155.50" in prompt
        assert "+3.0%" in prompt

    def test_prompt_contains_technicals(self):
        runner = FastBacktestRunner(MagicMock(), _make_config())
        sr = _make_screener_result(rsi_14=72.5, ema_10=150.0, ema_50=145.0, macd=1.2)
        prompt = runner._build_prompt("AAPL", "2026-03-01", sr)
        assert "72.5" in prompt
        assert "150.00" in prompt  # EMA(10)
        assert "bullish" in prompt  # ema_10 > ema_50

    def test_prompt_contains_fundamentals(self):
        runner = FastBacktestRunner(MagicMock(), _make_config())
        sr = _make_screener_result(sector="Healthcare", market_cap=5e10)
        prompt = runner._build_prompt("JNJ", "2026-03-01", sr)
        assert "Healthcare" in prompt
        assert "50,000,000,000" in prompt

    def test_prompt_contains_options_data(self):
        runner = FastBacktestRunner(MagicMock(), _make_config())
        sr = _make_screener_result(put_call_ratio=1.5, iv_rank=80.0)
        prompt = runner._build_prompt("AAPL", "2026-03-01", sr)
        assert "1.50" in prompt
        assert "80.0" in prompt

    def test_prompt_contains_regime(self):
        runner = FastBacktestRunner(MagicMock(), _make_config())
        sr = _make_screener_result(regime="CRISIS")
        prompt = runner._build_prompt("AAPL", "2026-03-01", sr)
        assert "CRISIS" in prompt

    def test_prompt_handles_none_options(self):
        runner = FastBacktestRunner(MagicMock(), _make_config())
        sr = _make_screener_result(put_call_ratio=None, iv_rank=None)
        prompt = runner._build_prompt("AAPL", "2026-03-01", sr)
        assert "N/A" in prompt


class TestResponseParsing:
    def test_valid_json(self):
        runner = FastBacktestRunner(MagicMock(), _make_config())
        response = '{"rating": "BUY", "confidence": 80, "reasoning": "Strong momentum"}'
        result = runner._parse_response(response)
        assert result["rating"] == "BUY"
        assert result["confidence"] == 80
        assert result["reasoning"] == "Strong momentum"

    def test_json_in_code_block(self):
        runner = FastBacktestRunner(MagicMock(), _make_config())
        response = '```json\n{"rating": "SELL", "confidence": 60, "reasoning": "Weak technicals"}\n```'
        result = runner._parse_response(response)
        assert result["rating"] == "SELL"
        assert result["confidence"] == 60

    def test_json_in_plain_code_block(self):
        runner = FastBacktestRunner(MagicMock(), _make_config())
        response = '```\n{"rating": "HOLD", "confidence": 50, "reasoning": "Mixed signals"}\n```'
        result = runner._parse_response(response)
        assert result["rating"] == "HOLD"

    def test_invalid_rating_defaults_to_hold(self):
        runner = FastBacktestRunner(MagicMock(), _make_config())
        response = '{"rating": "STRONG BUY", "confidence": 90, "reasoning": "test"}'
        result = runner._parse_response(response)
        assert result["rating"] == "HOLD"

    def test_malformed_json_falls_back_to_regex(self):
        runner = FastBacktestRunner(MagicMock(), _make_config())
        response = "Based on the analysis, I recommend BUY because momentum is strong."
        result = runner._parse_response(response)
        assert result["rating"] == "BUY"
        assert result["confidence"] == 50

    def test_no_signal_defaults_to_hold(self):
        runner = FastBacktestRunner(MagicMock(), _make_config())
        response = "I'm not sure what to recommend here."
        result = runner._parse_response(response)
        assert result["rating"] == "HOLD"


class TestCacheIntegration:
    def test_cache_hit_skips_llm(self):
        db = MagicMock()
        db.get_pipeline_cache.return_value = {"rating": "BUY", "ticker": "AAPL"}
        runner = FastBacktestRunner(db, _make_config())
        sr = _make_screener_result()

        result = runner.run("AAPL", "2026-03-01", sr)
        assert result["rating"] == "BUY"
        assert runner._hits == 1
        assert runner._misses == 0
        db.get_pipeline_cache.assert_called_once_with("AAPL", "2026-03-01", "haiku_fast")

    @patch("tradingagents.autoresearch.fast_backtest.FastBacktestRunner._call_llm")
    def test_cache_miss_calls_llm_and_caches(self, mock_llm):
        db = MagicMock()
        db.get_pipeline_cache.return_value = None
        mock_llm.return_value = '{"rating": "SELL", "confidence": 70, "reasoning": "Bearish"}'
        runner = FastBacktestRunner(db, _make_config())
        sr = _make_screener_result()

        result = runner.run("AAPL", "2026-03-01", sr)
        assert result["rating"] == "SELL"
        assert runner._misses == 1
        mock_llm.assert_called_once()
        db.insert_pipeline_cache.assert_called_once()

    def test_cache_stats(self):
        db = MagicMock()
        db.get_pipeline_cache.return_value = {"rating": "BUY"}
        runner = FastBacktestRunner(db, _make_config())
        sr = _make_screener_result()

        runner.run("AAPL", "2026-03-01", sr)
        runner.run("MSFT", "2026-03-01", sr)
        stats = runner.get_cache_stats()
        assert stats["hits"] == 2
        assert stats["hit_rate"] == 1.0


class TestFastModeToggle:
    def test_fast_mode_enabled_delegates_to_fast_runner(self):
        db = MagicMock()
        db.get_pipeline_cache.return_value = {"rating": "BUY", "ticker": "AAPL"}
        config = _make_config(fast_backtest=True)
        pipeline = CachedPipelineRunner(db, config)
        sr = _make_screener_result()

        result = pipeline.run("AAPL", "2026-03-01", "haiku", screener_result=sr)
        assert result["rating"] == "BUY"
        assert pipeline._fast_runner is not None

    def test_fast_mode_disabled_uses_full_pipeline(self):
        db = MagicMock()
        db.get_pipeline_cache.return_value = {"rating": "HOLD"}
        config = _make_config(fast_backtest=False)
        pipeline = CachedPipelineRunner(db, config)
        sr = _make_screener_result()

        result = pipeline.run("AAPL", "2026-03-01", "haiku", screener_result=sr)
        assert result["rating"] == "HOLD"
        assert pipeline._fast_runner is None  # never initialized

    def test_sonnet_tier_uses_full_pipeline(self):
        db = MagicMock()
        db.get_pipeline_cache.return_value = {"rating": "SELL"}
        config = _make_config(fast_backtest=True)
        pipeline = CachedPipelineRunner(db, config)
        sr = _make_screener_result()

        result = pipeline.run("AAPL", "2026-03-01", "sonnet", screener_result=sr)
        assert result["rating"] == "SELL"
        assert pipeline._fast_runner is None

    def test_no_screener_result_uses_full_pipeline(self):
        db = MagicMock()
        db.get_pipeline_cache.return_value = {"rating": "BUY"}
        config = _make_config(fast_backtest=True)
        pipeline = CachedPipelineRunner(db, config)

        result = pipeline.run("AAPL", "2026-03-01", "haiku")
        assert result["rating"] == "BUY"
        assert pipeline._fast_runner is None

    def test_cache_stats_include_fast_runner(self):
        db = MagicMock()
        db.get_pipeline_cache.return_value = {"rating": "BUY"}
        config = _make_config(fast_backtest=True)
        pipeline = CachedPipelineRunner(db, config)
        sr = _make_screener_result()

        pipeline.run("AAPL", "2026-03-01", "haiku", screener_result=sr)
        stats = pipeline.get_cache_stats()
        assert stats["hits"] >= 1


class TestBatchConcurrency:
    @patch("tradingagents.autoresearch.fast_backtest.FastBacktestRunner._call_llm")
    def test_batch_sequential(self, mock_llm):
        db = MagicMock()
        db.get_pipeline_cache.return_value = None
        mock_llm.return_value = '{"rating": "BUY", "confidence": 75, "reasoning": "test"}'
        runner = FastBacktestRunner(db, _make_config())
        sr = _make_screener_result()

        items = [("AAPL", "2026-03-01", sr), ("MSFT", "2026-03-01", sr)]
        results = runner.run_batch(items, max_workers=1)
        assert len(results) == 2
        assert all(r["rating"] == "BUY" for r in results)

    @patch("tradingagents.autoresearch.fast_backtest.FastBacktestRunner._call_llm")
    def test_batch_concurrent(self, mock_llm):
        db = MagicMock()
        db.get_pipeline_cache.return_value = None
        mock_llm.return_value = '{"rating": "SELL", "confidence": 60, "reasoning": "test"}'
        runner = FastBacktestRunner(db, _make_config())
        sr = _make_screener_result()

        items = [("AAPL", "2026-03-01", sr), ("MSFT", "2026-03-01", sr), ("GOOGL", "2026-03-01", sr)]
        results = runner.run_batch(items, max_workers=3)
        assert len(results) == 3
        assert all(r["rating"] == "SELL" for r in results)

    @patch("tradingagents.autoresearch.fast_backtest.FastBacktestRunner._call_llm")
    def test_batch_preserves_order(self, mock_llm):
        db = MagicMock()
        db.get_pipeline_cache.return_value = None
        call_count = [0]

        def side_effect(prompt):
            call_count[0] += 1
            if "AAPL" in prompt:
                return '{"rating": "BUY", "confidence": 80, "reasoning": "apple"}'
            return '{"rating": "SELL", "confidence": 60, "reasoning": "other"}'

        mock_llm.side_effect = side_effect
        runner = FastBacktestRunner(db, _make_config())

        items = [
            ("AAPL", "2026-03-01", _make_screener_result(ticker="AAPL")),
            ("MSFT", "2026-03-01", _make_screener_result(ticker="MSFT")),
        ]
        results = runner.run_batch(items, max_workers=2)
        assert results[0]["rating"] == "BUY"
        assert results[1]["rating"] == "SELL"
