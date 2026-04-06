"""Fast backtest runner: single-LLM-call replacement for the full pipeline."""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from tradingagents.strategies.state.models import ScreenerResult

logger = logging.getLogger(__name__)


class FastBacktestRunner:
    """Runs a single-LLM-call backtest using pre-fetched screener data.

    Replaces the full 14-node TradingAgentsGraph pipeline for backtesting.
    Uses the same cache layer (pipeline_cache table) with model_tier="haiku_fast".
    """

    def __init__(self, db, config: dict):
        self.db = db
        self.config = config
        self.ar_config = config.get("autoresearch", {})
        self._hits = 0
        self._misses = 0

    def run(self, ticker: str, trade_date: str,
            screener_result: ScreenerResult,
            model_tier: str = "haiku") -> dict:
        """Run a fast single-call backtest.

        Args:
            ticker: Stock ticker symbol.
            trade_date: Trade date string (YYYY-MM-DD).
            screener_result: Pre-fetched screener data for this ticker.
            model_tier: Base model tier (mapped to "haiku_fast" for cache).

        Returns:
            Dict matching CachedPipelineRunner.run() shape.
        """
        cache_tier = "haiku_fast"

        cached = self.db.get_pipeline_cache(ticker, trade_date, cache_tier)
        if cached is not None:
            self._hits += 1
            return cached

        self._misses += 1

        prompt = self._build_prompt(ticker, trade_date, screener_result)
        response = self._call_llm(prompt)
        parsed = self._parse_response(response)

        rating = parsed.get("rating", "HOLD")
        reasoning = parsed.get("reasoning", "")
        confidence = parsed.get("confidence", 50)

        result = {
            "ticker": ticker,
            "trade_date": trade_date,
            "model_tier": cache_tier,
            "rating": rating,
            "market_report": f"[fast] {reasoning}",
            "sentiment_report": "",
            "news_report": "",
            "fundamentals_report": f"Sector: {screener_result.sector}, MCap: {screener_result.market_cap:.0f}",
            "options_report": f"P/C ratio: {screener_result.put_call_ratio}, IV rank: {screener_result.iv_rank}",
            "full_decision": f"{rating} (confidence: {confidence}/100) — {reasoning}",
            "debate_summary": "",
            "analyst_scores": None,
        }

        self.db.insert_pipeline_cache(
            ticker=ticker, trade_date=trade_date, model_tier=cache_tier,
            rating=rating,
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

    def run_batch(self, items: list[tuple[str, str, ScreenerResult]],
                  model_tier: str = "haiku",
                  max_workers: int = 1) -> list[dict]:
        """Run multiple fast backtests, optionally concurrent.

        Args:
            items: List of (ticker, trade_date, screener_result) tuples.
            model_tier: Model tier.
            max_workers: Number of concurrent workers (1 = sequential).

        Returns:
            List of result dicts in same order as items.
        """
        if max_workers <= 1:
            return [self.run(t, d, sr, model_tier) for t, d, sr in items]

        results = [None] * len(items)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {
                executor.submit(self.run, t, d, sr, model_tier): i
                for i, (t, d, sr) in enumerate(items)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                results[idx] = future.result()

        return results

    def get_cache_stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "total": total,
            "hit_rate": self._hits / total if total > 0 else 0.0,
        }

    def _call_llm(self, prompt: str) -> str:
        """Call Haiku with the given prompt. Retries on transient errors."""
        import time as _time
        from tradingagents.llm_clients import create_llm_client

        model = self.ar_config.get("cache_model", "claude-haiku-4-5-20251001")
        client = create_llm_client(provider="anthropic", model=model)
        llm = client.get_llm()

        for attempt in range(3):
            try:
                response = llm.invoke(prompt)
                return response.content if hasattr(response, "content") else str(response)
            except Exception as e:
                if attempt < 2 and ("500" in str(e) or "529" in str(e) or "overloaded" in str(e).lower()):
                    logger.warning("LLM call failed (attempt %d/3): %s — retrying", attempt + 1, e)
                    _time.sleep(2 ** attempt)
                else:
                    raise

    def _build_prompt(self, ticker: str, trade_date: str,
                      sr: ScreenerResult) -> str:
        """Build a comprehensive single prompt from screener data."""
        pct_of_range = ((sr.close - sr.low_52w) / max(sr.high_52w - sr.low_52w, 0.01))
        ema_cross = "bullish" if sr.ema_10 > sr.ema_50 else "bearish"
        price_vs_ema10 = "above" if sr.close > sr.ema_10 else "below"
        price_vs_ema50 = "above" if sr.close > sr.ema_50 else "below"

        pcr_str = f"{sr.put_call_ratio:.2f}" if sr.put_call_ratio else "N/A"
        iv_str = f"{sr.iv_rank:.1f}" if sr.iv_rank else "N/A"
        rev_str = f"{sr.revenue_growth_yoy:+.1%}" if sr.revenue_growth_yoy else "N/A"

        return f"""You are a trading analyst. Given the market data for {ticker} on {trade_date}, provide a trading signal.

PRICE:
- Close: ${sr.close:.2f} (14d: {sr.change_14d:+.1%}, 30d: {sr.change_30d:+.1%})
- 52w range: ${sr.low_52w:.2f} - ${sr.high_52w:.2f} ({pct_of_range:.0%} of range)
- Volume: {sr.volume_ratio:.1f}x 20d avg

TECHNICALS:
- RSI(14): {sr.rsi_14:.1f}
- EMA(10): ${sr.ema_10:.2f} (price {price_vs_ema10})
- EMA(50): ${sr.ema_50:.2f} (price {price_vs_ema50})
- EMA crossover: {ema_cross}
- MACD: {sr.macd:.4f}
- Bollinger: {sr.boll_position:.2f} (0=lower, 1=upper)

FUNDAMENTALS:
- Sector: {sr.sector}
- Market cap: ${sr.market_cap:,.0f}
- Revenue growth YoY: {rev_str}

OPTIONS:
- Put/Call ratio: {pcr_str}
- IV rank: {iv_str}

REGIME: {sr.regime}

Respond with ONLY a JSON object:
{{"rating": "BUY" or "HOLD" or "SELL", "confidence": 0-100, "reasoning": "one sentence"}}"""

    def _parse_response(self, response: str) -> dict:
        """Parse LLM response into {rating, confidence, reasoning}."""
        text = response.strip()

        # Try JSON extraction
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        try:
            data = json.loads(text)
            if isinstance(data, dict) and "rating" in data:
                rating = data["rating"].upper().strip()
                if rating not in ("BUY", "HOLD", "SELL"):
                    rating = "HOLD"
                return {
                    "rating": rating,
                    "confidence": int(data.get("confidence", 50)),
                    "reasoning": data.get("reasoning", ""),
                }
        except (json.JSONDecodeError, ValueError, AttributeError):
            pass

        # Regex fallback
        rating_match = re.search(r'\b(BUY|SELL|HOLD)\b', response.upper())
        rating = rating_match.group(1) if rating_match else "HOLD"
        logger.warning("Fast backtest: JSON parse failed, extracted '%s' via regex", rating)
        return {"rating": rating, "confidence": 50, "reasoning": response[:200]}
