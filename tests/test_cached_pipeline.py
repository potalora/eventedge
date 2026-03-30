"""Tests for the CachedPipelineRunner."""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from copy import deepcopy

from tradingagents.autoresearch.cached_pipeline import CachedPipelineRunner
from tradingagents.default_config import DEFAULT_CONFIG


def _make_config():
    config = deepcopy(DEFAULT_CONFIG)
    config["llm_provider"] = "anthropic"
    config["autoresearch"] = {
        "cache_model": "claude-haiku-4-5-20251001",
        "live_model": "claude-sonnet-4-20250514",
    }
    return config


def _make_cached_result():
    return {
        "id": 1,
        "ticker": "AAPL",
        "trade_date": "2024-03-01",
        "model_tier": "haiku",
        "rating": "BUY",
        "market_report": "Market looks good",
        "sentiment_report": "Positive sentiment",
        "news_report": "Good news",
        "fundamentals_report": "Strong fundamentals",
        "options_report": "Normal IV",
        "full_decision": "BUY AAPL",
        "debate_summary": "Investment debate: BUY",
        "analyst_scores": None,
    }


def _make_final_state():
    return {
        "market_report": "Market analysis for AAPL",
        "sentiment_report": "Sentiment is bullish",
        "news_report": "AAPL beats earnings",
        "fundamentals_report": "Strong balance sheet",
        "options_report": "IV rank moderate",
        "final_trade_decision": "BUY AAPL with conviction",
        "investment_debate_state": {
            "bull_history": "...",
            "bear_history": "...",
            "history": "...",
            "current_response": "...",
            "judge_decision": "Bull wins — BUY",
        },
        "risk_debate_state": {
            "aggressive_history": "...",
            "conservative_history": "...",
            "neutral_history": "...",
            "history": "...",
            "latest_speaker": "...",
            "current_aggressive_response": "...",
            "current_conservative_response": "...",
            "current_neutral_response": "...",
            "judge_decision": "Moderate risk acceptable",
        },
    }


class TestCacheHit:
    """Tests for cache hit behavior."""

    def test_cache_hit_returns_cached_result(self):
        db = MagicMock()
        db.get_pipeline_cache.return_value = _make_cached_result()

        runner = CachedPipelineRunner(db, _make_config())
        result = runner.run("AAPL", "2024-03-01", "haiku")

        assert result["rating"] == "BUY"
        db.get_pipeline_cache.assert_called_once_with("AAPL", "2024-03-01", "haiku")

    def test_cache_hit_does_not_call_propagate(self):
        db = MagicMock()
        db.get_pipeline_cache.return_value = _make_cached_result()

        runner = CachedPipelineRunner(db, _make_config())
        with patch("tradingagents.graph.trading_graph.TradingAgentsGraph") as mock_graph:
            runner.run("AAPL", "2024-03-01", "haiku")
            mock_graph.assert_not_called()

    def test_cache_hit_increments_hits(self):
        db = MagicMock()
        db.get_pipeline_cache.return_value = _make_cached_result()

        runner = CachedPipelineRunner(db, _make_config())
        runner.run("AAPL", "2024-03-01", "haiku")

        stats = runner.get_cache_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 0


class TestCacheMiss:
    """Tests for cache miss behavior."""

    @patch("tradingagents.graph.trading_graph.TradingAgentsGraph")
    def test_cache_miss_runs_pipeline(self, MockGraph):
        db = MagicMock()
        db.get_pipeline_cache.return_value = None

        mock_instance = MagicMock()
        mock_instance.propagate.return_value = (_make_final_state(), "BUY")
        MockGraph.return_value = mock_instance

        runner = CachedPipelineRunner(db, _make_config())
        result = runner.run("AAPL", "2024-03-01", "haiku")

        assert result["rating"] == "BUY"
        assert result["market_report"] == "Market analysis for AAPL"
        mock_instance.propagate.assert_called_once_with("AAPL", "2024-03-01")

    @patch("tradingagents.graph.trading_graph.TradingAgentsGraph")
    def test_cache_miss_inserts_to_db(self, MockGraph):
        db = MagicMock()
        db.get_pipeline_cache.return_value = None

        mock_instance = MagicMock()
        mock_instance.propagate.return_value = (_make_final_state(), "BUY")
        MockGraph.return_value = mock_instance

        runner = CachedPipelineRunner(db, _make_config())
        runner.run("AAPL", "2024-03-01", "haiku")

        db.insert_pipeline_cache.assert_called_once()
        call_kwargs = db.insert_pipeline_cache.call_args
        # Verify key fields passed
        assert call_kwargs[1]["ticker"] == "AAPL"
        assert call_kwargs[1]["trade_date"] == "2024-03-01"
        assert call_kwargs[1]["model_tier"] == "haiku"

    @patch("tradingagents.graph.trading_graph.TradingAgentsGraph")
    def test_cache_miss_increments_misses(self, MockGraph):
        db = MagicMock()
        db.get_pipeline_cache.return_value = None

        mock_instance = MagicMock()
        mock_instance.propagate.return_value = (_make_final_state(), "SELL")
        MockGraph.return_value = mock_instance

        runner = CachedPipelineRunner(db, _make_config())
        runner.run("AAPL", "2024-03-01", "haiku")

        stats = runner.get_cache_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 1


class TestBuildGraphConfig:
    """Tests for _build_graph_config()."""

    def test_haiku_config(self):
        runner = CachedPipelineRunner(MagicMock(), _make_config())
        config = runner._build_graph_config("haiku")
        assert config["deep_think_llm"] == "claude-haiku-4-5-20251001"
        assert config["quick_think_llm"] == "claude-haiku-4-5-20251001"
        assert config["llm_provider"] == "anthropic"

    def test_sonnet_config(self):
        runner = CachedPipelineRunner(MagicMock(), _make_config())
        config = runner._build_graph_config("sonnet")
        assert config["deep_think_llm"] == "claude-sonnet-4-20250514"
        assert config["quick_think_llm"] == "claude-sonnet-4-20250514"

    def test_config_is_deep_copy(self):
        config = _make_config()
        runner = CachedPipelineRunner(MagicMock(), config)
        graph_config = runner._build_graph_config("haiku")
        # Modifying graph_config shouldn't affect original
        graph_config["llm_provider"] = "openai"
        assert config["llm_provider"] == "anthropic"


class TestRunBatch:
    """Tests for run_batch()."""

    @patch("tradingagents.graph.trading_graph.TradingAgentsGraph")
    def test_batch_with_mixed_hits(self, MockGraph):
        db = MagicMock()
        # First call: cache hit, second call: cache miss
        db.get_pipeline_cache.side_effect = [_make_cached_result(), None]

        mock_instance = MagicMock()
        mock_instance.propagate.return_value = (_make_final_state(), "SELL")
        MockGraph.return_value = mock_instance

        runner = CachedPipelineRunner(db, _make_config())
        results = runner.run_batch([("AAPL", "2024-03-01"), ("MSFT", "2024-03-01")])

        assert len(results) == 2
        stats = runner.get_cache_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 0.5


class TestCacheStats:
    """Tests for get_cache_stats()."""

    def test_initial_stats(self):
        runner = CachedPipelineRunner(MagicMock(), _make_config())
        stats = runner.get_cache_stats()
        assert stats == {"hits": 0, "misses": 0, "total": 0, "hit_rate": 0.0}

    def test_hit_rate_calculation(self):
        runner = CachedPipelineRunner(MagicMock(), _make_config())
        runner._hits = 3
        runner._misses = 1
        stats = runner.get_cache_stats()
        assert stats["hit_rate"] == 0.75


class TestDebateSummary:
    """Tests for _extract_debate_summary()."""

    def test_extracts_both_debates(self):
        runner = CachedPipelineRunner(MagicMock(), _make_config())
        state = _make_final_state()
        summary = runner._extract_debate_summary(state)
        assert "Bull wins" in summary
        assert "Moderate risk" in summary

    def test_missing_debate_states(self):
        runner = CachedPipelineRunner(MagicMock(), _make_config())
        summary = runner._extract_debate_summary({})
        assert summary == ""

    def test_empty_judge_decisions(self):
        runner = CachedPipelineRunner(MagicMock(), _make_config())
        state = {
            "investment_debate_state": {"judge_decision": ""},
            "risk_debate_state": {"judge_decision": ""},
        }
        summary = runner._extract_debate_summary(state)
        assert summary == ""
