"""Cache-first wrapper around TradingAgentsGraph.propagate()."""

import copy
import logging
from typing import Optional

from tradingagents.strategies.state.models import ScreenerResult

logger = logging.getLogger(__name__)


class CachedPipelineRunner:
    """Runs the full TradingAgents pipeline with a SQLite cache layer.

    On cache hit, returns cached results immediately.
    On cache miss, runs the full pipeline, caches results, and returns them.
    When fast_backtest is enabled and a ScreenerResult is provided, delegates
    to FastBacktestRunner for a single-LLM-call backtest instead.
    """

    def __init__(self, db, config: dict):
        self.db = db
        self.config = config
        self.ar_config = config.get("autoresearch", {})
        self._hits = 0
        self._misses = 0
        self._fast_runner = None

    def run(self, ticker: str, trade_date: str, model_tier: str = "haiku",
            screener_result: Optional[ScreenerResult] = None) -> dict:
        """Run pipeline for a ticker/date, using cache when available.

        Args:
            ticker: Stock ticker symbol.
            trade_date: Trade date string (YYYY-MM-DD).
            model_tier: "haiku" for cached backtests, "sonnet" for live/paper.
            screener_result: Pre-fetched screener data. When provided with
                fast_backtest enabled, uses single-call fast mode.

        Returns:
            Dict with keys: rating, market_report, sentiment_report,
            news_report, fundamentals_report, options_report,
            full_decision, debate_summary, analyst_scores.
        """
        # Fast mode: single LLM call when enabled and screener data available
        if self._should_use_fast_mode(model_tier, screener_result):
            return self._get_fast_runner().run(
                ticker, trade_date, screener_result, model_tier
            )

        # Check cache first
        cached = self.db.get_pipeline_cache(ticker, trade_date, model_tier)
        if cached is not None:
            self._hits += 1
            logger.debug("Cache hit: %s/%s/%s", ticker, trade_date, model_tier)
            return cached

        # Cache miss — run full pipeline
        self._misses += 1
        logger.info("Cache miss: %s/%s/%s — running pipeline", ticker, trade_date, model_tier)

        graph_config = self._build_graph_config(model_tier)

        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph(
            selected_analysts=["market", "social", "news", "fundamentals", "options"],
            config=graph_config,
        )

        final_state, rating = graph.propagate(ticker, trade_date)

        # Extract reports from final state
        result = {
            "ticker": ticker,
            "trade_date": trade_date,
            "model_tier": model_tier,
            "rating": rating,
            "market_report": final_state.get("market_report", ""),
            "sentiment_report": final_state.get("sentiment_report", ""),
            "news_report": final_state.get("news_report", ""),
            "fundamentals_report": final_state.get("fundamentals_report", ""),
            "options_report": final_state.get("options_report", ""),
            "full_decision": final_state.get("final_trade_decision", ""),
            "debate_summary": self._extract_debate_summary(final_state),
            "analyst_scores": None,  # populated by fitness module later
        }

        # Cache the result
        self.db.insert_pipeline_cache(
            ticker=ticker,
            trade_date=trade_date,
            model_tier=model_tier,
            rating=result["rating"],
            market_report=result["market_report"],
            sentiment_report=result["sentiment_report"],
            news_report=result["news_report"],
            fundamentals_report=result["fundamentals_report"],
            options_report=result["options_report"],
            full_decision=result["full_decision"],
            debate_summary=result["debate_summary"],
            analyst_scores=result["analyst_scores"],
        )

        return result

    def run_batch(
        self, ticker_date_pairs: list[tuple[str, str]], model_tier: str = "haiku"
    ) -> list[dict]:
        """Run pipeline for multiple ticker/date pairs.

        Args:
            ticker_date_pairs: List of (ticker, trade_date) tuples.
            model_tier: Model tier to use.

        Returns:
            List of result dicts.
        """
        return [self.run(ticker, date, model_tier) for ticker, date in ticker_date_pairs]

    def get_cache_stats(self) -> dict:
        """Return cache hit/miss statistics (includes fast runner stats)."""
        fast_stats = self._fast_runner.get_cache_stats() if self._fast_runner else {"hits": 0, "misses": 0}
        hits = self._hits + fast_stats["hits"]
        misses = self._misses + fast_stats["misses"]
        total = hits + misses
        return {
            "hits": hits,
            "misses": misses,
            "total": total,
            "hit_rate": hits / total if total > 0 else 0.0,
        }

    def _should_use_fast_mode(self, model_tier: str, screener_result: Optional[ScreenerResult]) -> bool:
        """Check if fast backtest mode should be used."""
        return (
            self.ar_config.get("fast_backtest", True)
            and model_tier == "haiku"
            and screener_result is not None
        )

    def _get_fast_runner(self):
        """Lazy-init the FastBacktestRunner."""
        if self._fast_runner is None:
            from tradingagents.strategies._dormant.fast_backtest import FastBacktestRunner
            self._fast_runner = FastBacktestRunner(self.db, self.config)
        return self._fast_runner

    def _build_graph_config(self, model_tier: str) -> dict:
        """Build a TradingAgentsGraph config for the given model tier.

        Maps "haiku" to cache_model, "sonnet" to live_model.
        Sets both deep_think_llm and quick_think_llm to the same model
        so the entire pipeline uses a single tier.
        """
        config = copy.deepcopy(self.config)

        if model_tier == "haiku":
            model = self.ar_config.get("cache_model", "claude-haiku-4-5-20251001")
        else:
            model = self.ar_config.get("live_model", "claude-sonnet-4-20250514")

        config["llm_provider"] = "anthropic"
        config["deep_think_llm"] = model
        config["quick_think_llm"] = model
        config["backend_url"] = None

        return config

    def _extract_debate_summary(self, final_state: dict) -> str:
        """Extract a debate summary from the investment and risk debate states."""
        parts = []

        invest_state = final_state.get("investment_debate_state")
        if invest_state and isinstance(invest_state, dict):
            judge = invest_state.get("judge_decision", "")
            if judge:
                parts.append(f"Investment debate: {judge}")

        risk_state = final_state.get("risk_debate_state")
        if risk_state and isinstance(risk_state, dict):
            judge = risk_state.get("judge_decision", "")
            if judge:
                parts.append(f"Risk debate: {judge}")

        return " | ".join(parts) if parts else ""
