"""Portfolio committee: synthesizes signals from multiple strategies.

Takes all paper-trade signals, regime context, and strategy confidence.
Returns ranked trade recommendations with position sizes.

Uses LLM (Haiku) when available, falls back to rule-based synthesis.
Cost: ~$0.001 per synthesis call.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from tradingagents.strategies.modules.base import OptionSpec

logger = logging.getLogger(__name__)


@dataclass
class TradeRecommendation:
    """A sized trade recommendation from the portfolio committee."""
    ticker: str
    direction: str                          # "long" or "short"
    position_size_pct: float                # % of total capital
    confidence: float                       # 0.0-1.0
    rationale: str
    contributing_strategies: list[str] = field(default_factory=list)
    regime_alignment: str = "neutral"       # "aligned"/"neutral"/"misaligned"
    vehicle: str = "equity"                 # "equity" or "option"
    option_spec: OptionSpec | None = None   # Populated when vehicle == "option"


class PortfolioCommittee:
    """Synthesizes signals across strategies into trade recommendations."""

    def __init__(self, config: dict | None = None, size_profile: Any = None) -> None:
        self.config = config or {}
        pt_config = self.config.get("autoresearch", {}).get("paper_trade", {})
        self._model_name = pt_config.get(
            "portfolio_committee_model",
            self.config.get("autoresearch", {}).get("autoresearch_model", "claude-haiku-4-5-20251001"),
        )
        self._enabled = pt_config.get("portfolio_committee_enabled", True)

        # Use size profile if provided, otherwise fall back to config defaults
        if size_profile is not None:
            self._max_sector = size_profile.sector_concentration_cap
            self._max_position = size_profile.max_position_pct
        else:
            self._max_sector = pt_config.get("max_sector_concentration_pct", 0.30)
            self._max_position = pt_config.get("max_single_position_pct", 0.10)

        self._size_profile = size_profile
        self._client = None

    def synthesize(
        self,
        signals: list[dict],
        regime_context: dict | None = None,
        strategy_confidence: dict[str, float] | None = None,
        current_positions: list[dict] | None = None,
        total_capital: float = 5000.0,
        enrichment: dict | None = None,
    ) -> list[TradeRecommendation]:
        """Synthesize all signals into ranked trade recommendations.

        Args:
            signals: List of signal dicts with keys: ticker, direction, score,
                     strategy (name), metadata.
            regime_context: RegimeContext as dict (vix_regime, credit_regime, etc.).
            strategy_confidence: Map of strategy name -> confidence [0,1].
            current_positions: List of current open position dicts.
            total_capital: Total portfolio capital for sizing.

        Returns:
            List of TradeRecommendation sorted by confidence descending.
        """
        if not signals:
            return []

        regime_context = regime_context or {}
        strategy_confidence = strategy_confidence or {}
        current_positions = current_positions or []

        enrichment = enrichment or {}

        # Try LLM synthesis if enabled and available
        if self._enabled:
            try:
                llm_result = self._llm_synthesize(
                    signals, regime_context, strategy_confidence,
                    current_positions, total_capital, enrichment,
                )
                if llm_result:
                    return llm_result
            except Exception:
                logger.warning("LLM synthesis failed, falling back to rule-based", exc_info=True)

        return self._rule_based_synthesize(
            signals, regime_context, strategy_confidence,
            current_positions, total_capital, enrichment,
        )

    def _rule_based_synthesize(
        self,
        signals: list[dict],
        regime_context: dict,
        strategy_confidence: dict[str, float],
        current_positions: list[dict],
        total_capital: float,
        enrichment: dict | None = None,
    ) -> list[TradeRecommendation]:
        """Rule-based signal synthesis fallback.

        Logic:
        1. Group signals by ticker
        2. Compute consensus score = sum(signal.score * strategy_confidence)
        3. Determine direction consensus (majority vote weighted by score)
        4. Filter: require 2+ strategies or single with confidence > 0.8
        5. Size proportional to consensus, capped by max_single_position
        6. Enforce sector concentration limits
        7. Return sorted by confidence
        """
        # Group by ticker
        ticker_signals: dict[str, list[dict]] = defaultdict(list)
        for sig in signals:
            ticker = sig.get("ticker", "")
            if ticker:
                ticker_signals[ticker].append(sig)

        # Filter short signals for ineligible cohorts
        if self._size_profile and not getattr(self._size_profile, 'short_eligible', True):
            ticker_signals = {
                t: [s for s in sigs if s.get("direction") != "short"]
                for t, sigs in ticker_signals.items()
            }
            ticker_signals = {t: sigs for t, sigs in ticker_signals.items() if sigs}

        recommendations: list[TradeRecommendation] = []

        for ticker, sigs in ticker_signals.items():
            # Compute weighted direction consensus
            long_score = 0.0
            short_score = 0.0
            strategies: list[str] = []

            for s in sigs:
                strat = s.get("strategy", "unknown")
                conf = strategy_confidence.get(strat, 0.5)
                score = s.get("score", 0.0) * conf

                if s.get("direction") == "long":
                    long_score += score
                elif s.get("direction") == "short":
                    short_score += score

                if strat not in strategies:
                    strategies.append(strat)

            # Direction = majority weighted vote
            if long_score > short_score:
                direction = "long"
                consensus_score = long_score
            elif short_score > long_score:
                direction = "short"
                consensus_score = short_score
            else:
                continue  # Skip ties

            # Filter: require 2+ strategies or single with high confidence
            num_strategies = len(strategies)
            if num_strategies < 2:
                max_conf = max(
                    (strategy_confidence.get(s.get("strategy", ""), 0.5) for s in sigs),
                    default=0.0,
                )
                if max_conf < 0.4:
                    continue

            # Regime alignment
            regime_alignment = self._assess_regime_alignment(
                direction, regime_context,
            )

            # Confidence: based on strategy count, consensus, and regime
            confidence = min(1.0, consensus_score * (1.0 if regime_alignment == "aligned"
                                                      else 0.7 if regime_alignment == "neutral"
                                                      else 0.6))

            # Size: proportional to confidence, capped
            position_size = min(confidence * self._max_position, self._max_position)

            rationale_parts = [f"{len(strategies)} strategies agree"]
            if regime_alignment != "neutral":
                rationale_parts.append(f"regime {regime_alignment}")

            recommendations.append(TradeRecommendation(
                ticker=ticker,
                direction=direction,
                position_size_pct=position_size,
                confidence=confidence,
                rationale="; ".join(rationale_parts),
                contributing_strategies=strategies,
                regime_alignment=regime_alignment,
            ))

        # Enforce sector concentration (use enrichment profiles if available)
        enrichment = enrichment or {}
        profiles = enrichment.get("profiles", {})
        if profiles:
            sector_alloc: dict[str, float] = defaultdict(float)
            for rec in recommendations:
                sector = profiles.get(rec.ticker, {}).get("sector", "Unknown")
                sector_alloc[sector] += rec.position_size_pct

            for rec in recommendations:
                sector = profiles.get(rec.ticker, {}).get("sector", "Unknown")
                if sector_alloc[sector] > self._max_sector:
                    scale = self._max_sector / sector_alloc[sector]
                    rec.position_size_pct *= scale

        recommendations = self._enforce_sector_limits(recommendations)

        # Cap short exposure
        if self._size_profile and getattr(self._size_profile, 'max_short_exposure_pct', 0) > 0:
            short_recs = [r for r in recommendations if r.direction == "short"]
            total_short = sum(r.position_size_pct for r in short_recs)
            if total_short > self._size_profile.max_short_exposure_pct:
                scale = self._size_profile.max_short_exposure_pct / total_short
                for r in short_recs:
                    r.position_size_pct *= scale

        # Sort by confidence descending
        recommendations.sort(key=lambda r: r.confidence, reverse=True)

        return recommendations

    def _assess_regime_alignment(self, direction: str, regime_context: dict) -> str:
        """Assess whether a trade direction aligns with current regime.

        - Crisis regime + long equity = misaligned
        - Crisis regime + short/defensive = aligned
        - Normal regime = neutral for all
        - Benign regime + long equity = aligned
        """
        overall = regime_context.get("overall_regime", "normal")

        if overall in ("crisis", "stressed"):
            return "aligned" if direction == "short" else "misaligned"
        elif overall == "benign":
            return "aligned" if direction == "long" else "misaligned"
        return "neutral"

    def _enforce_sector_limits(
        self, recommendations: list[TradeRecommendation],
    ) -> list[TradeRecommendation]:
        """Cap total position size per sector."""
        # Without reliable sector data on all tickers, just cap total allocation
        total_alloc = sum(r.position_size_pct for r in recommendations)
        if total_alloc > 1.0:
            # Scale down proportionally
            scale = 1.0 / total_alloc
            for r in recommendations:
                r.position_size_pct *= scale
        return recommendations

    def _llm_synthesize(
        self,
        signals: list[dict],
        regime_context: dict,
        strategy_confidence: dict[str, float],
        current_positions: list[dict],
        total_capital: float,
        enrichment: dict | None = None,
    ) -> list[TradeRecommendation] | None:
        """Single LLM call for signal synthesis. Returns None on failure."""
        if self._get_client() is None:
            return None

        prompt = self._build_prompt(
            signals, regime_context, strategy_confidence,
            current_positions, total_capital, enrichment,
        )

        try:
            system_parts = [
                "You are a portfolio manager synthesizing trading signals from multiple strategies.",
            ]
            if self._size_profile:
                system_parts.append(
                    f"Portfolio: ${self._size_profile.total_capital:,.0f} capital, "
                    f"max {self._size_profile.max_positions} positions, "
                    f"max {self._size_profile.max_position_pct:.0%} per position."
                )
            else:
                system_parts.append("Investment horizon: 30 days.")
            system_parts.append(
                "Given signals, regime context, and strategy confidence scores, output a ranked list of trades. "
                "Return ONLY a JSON array of objects with keys: ticker, direction, position_size_pct, confidence, "
                "rationale, contributing_strategies, regime_alignment. "
                f"Keep position_size_pct between 0.01 and {self._max_position:.2f}. Keep rationale under 80 chars."
            )
            system_prompt = "\n".join(system_parts)

            text = self._call_llm(system=system_prompt, prompt=prompt, max_tokens=1024)
            return self._parse_llm_response(text)
        except Exception:
            logger.warning("LLM synthesis call failed", exc_info=True)
            return None

    def _build_prompt(
        self,
        signals: list[dict],
        regime_context: dict,
        strategy_confidence: dict[str, float],
        current_positions: list[dict],
        total_capital: float,
        enrichment: dict | None = None,
    ) -> str:
        """Build synthesis prompt for LLM."""
        # Compact signal summary
        sig_lines = []
        for s in signals[:20]:  # Cap at 20 signals
            sig_lines.append(
                f"  {s.get('ticker','?')} {s.get('direction','?')} "
                f"score={s.get('score',0):.2f} strategy={s.get('strategy','?')}"
            )

        regime_str = json.dumps(regime_context, default=str) if regime_context else "normal"
        conf_str = json.dumps(strategy_confidence, default=str) if strategy_confidence else "{}"

        pos_lines = []
        for p in current_positions[:10]:
            pos_lines.append(f"  {p.get('ticker','?')} {p.get('direction','?')}")

        # Add enrichment context if available
        enrichment_str = ""
        enrichment = enrichment or {}
        profiles = enrichment.get("profiles", {})
        short_interest = enrichment.get("short_interest", {})
        factors = enrichment.get("factors", {})

        if profiles:
            sector_lines = [f"  {t}: {p.get('sector', '?')}" for t, p in list(profiles.items())[:10]]
            enrichment_str += "\nSector classification:\n" + "\n".join(sector_lines)
        if short_interest:
            si_lines = [f"  {t}: {s.get('short_pct_of_float', 0):.1f}% short"
                        for t, s in list(short_interest.items())[:10]]
            enrichment_str += "\nShort interest:\n" + "\n".join(si_lines)
        if factors:
            enrichment_str += f"\nFama-French factors: {json.dumps(factors, default=str)}"

        return f"""Capital: ${total_capital:,.0f}
Max position: {self._max_position:.0%}
Max sector: {self._max_sector:.0%}

Regime: {regime_str}

Strategy confidence: {conf_str}

Signals:
{chr(10).join(sig_lines) or '  (none)'}

Current positions:
{chr(10).join(pos_lines) or '  (none)'}
{enrichment_str}

Synthesize into ranked trade list. Return JSON array."""

    def _parse_llm_response(self, text: str) -> list[TradeRecommendation] | None:
        """Parse LLM JSON response into TradeRecommendation list."""
        # Strip markdown fences
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("[")
            if start >= 0:
                try:
                    data = json.loads(text[start:])
                except json.JSONDecodeError:
                    return None
            else:
                return None

        if not isinstance(data, list):
            return None

        recommendations = []
        for item in data:
            if not isinstance(item, dict):
                continue
            recommendations.append(TradeRecommendation(
                ticker=item.get("ticker", ""),
                direction=item.get("direction", ""),
                position_size_pct=float(item.get("position_size_pct", 0.05)),
                confidence=float(item.get("confidence", 0.5)),
                rationale=item.get("rationale", ""),
                contributing_strategies=item.get("contributing_strategies", []),
                regime_alignment=item.get("regime_alignment", "neutral"),
            ))

        return recommendations if recommendations else None

    def generate_covered_call_overlays(
        self,
        current_positions: list[dict],
        iv_data: dict[str, dict],
        earnings_dates: dict[str, int],
        trading_date: str,
    ) -> list[TradeRecommendation]:
        """Identify existing long positions suitable for covered call overlay."""
        if not self._size_profile or "covered_call" not in getattr(self._size_profile, 'options_eligible', []):
            return []

        longs = [p for p in current_positions if p.get("direction") == "long"]
        if not longs:
            return []

        candidates = self._llm_covered_call_overlay(longs, iv_data, earnings_dates, trading_date)
        if not candidates:
            return []

        overlays = []
        for c in candidates:
            ticker = c.get("ticker", "")
            if not ticker:
                continue
            spec = OptionSpec(
                strategy="covered_call",
                expiry_target_days=c.get("expiry_days", 30),
                strike_offset_pct=c.get("strike_offset_pct", 0.05),
                max_premium_pct=getattr(self._size_profile, 'max_options_premium_pct', 0.05),
            )
            overlays.append(TradeRecommendation(
                ticker=ticker,
                direction="long",
                position_size_pct=0.0,
                confidence=0.7,
                rationale=c.get("rationale", "covered call overlay"),
                vehicle="option",
                option_spec=spec,
            ))
        return overlays

    def _llm_covered_call_overlay(
        self,
        positions: list[dict],
        iv_data: dict[str, dict],
        earnings_dates: dict[str, int],
        trading_date: str,
    ) -> list[dict]:
        """Ask LLM which positions to overlay with covered calls."""
        if self._get_client() is None:
            return []

        pos_lines = []
        for p in positions[:10]:
            ticker = p.get("ticker", "?")
            iv = iv_data.get(ticker, {})
            earnings_days = earnings_dates.get(ticker, "unknown")
            pos_lines.append(
                f"  {ticker}: entry=${p.get('entry_price', 0):.0f}, "
                f"shares={p.get('shares', 0)}, "
                f"IV={iv.get('iv', 'N/A')}, IV_rank={iv.get('iv_rank', 'N/A')}, "
                f"earnings_in={earnings_days} days"
            )

        prompt = f"""Date: {trading_date}

Current long positions:
{chr(10).join(pos_lines)}

Which positions are good candidates for covered call overlays?
Consider: IV rank (higher = better for selling calls), upcoming earnings (avoid if <10 days),
days held (prefer >14 days), and overall conditions.

Return ONLY a JSON array of objects with keys: ticker, strike_offset_pct (e.g. 0.05 for 5% OTM),
expiry_days (target DTE), rationale (under 60 chars). Return empty array [] if no good candidates."""

        try:
            text = self._call_llm(
                system="You are a portfolio manager specializing in options overlays. Be conservative.",
                prompt=prompt,
                max_tokens=512,
            )
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                text = "\n".join(lines)
            data = json.loads(text)
            return data if isinstance(data, list) else []
        except Exception:
            logger.warning("Covered call overlay LLM call failed", exc_info=True)
            return []

    def _get_client(self):
        """Lazy-init Anthropic client."""
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic()
            except (ImportError, Exception):
                return None
        return self._client

    def _call_llm(self, *, system: str, prompt: str, max_tokens: int = 1024):
        """Call LLM with retry + exponential backoff for rate limits.

        Returns the response text, or raises on non-retryable failure.
        """
        client = self._get_client()
        if client is None:
            raise RuntimeError("No LLM client")

        max_retries = 4
        base_delay = 2.0
        for attempt in range(max_retries + 1):
            try:
                response = client.messages.create(
                    model=self._model_name,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                )
                return response.content[0].text.strip()
            except Exception as exc:
                is_rate_limit = "rate" in str(exc).lower() or "429" in str(exc)
                is_overloaded = "overloaded" in str(exc).lower() or "529" in str(exc)
                if (is_rate_limit or is_overloaded) and attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.info(
                        "LLM rate limited (attempt %d/%d), retrying in %.1fs",
                        attempt + 1, max_retries, delay,
                    )
                    time.sleep(delay)
                    continue
                raise
