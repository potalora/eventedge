"""Strategist agent: proposes new strategies and reviews them via CRO."""

import json
import logging
from typing import Optional

from tradingagents.autoresearch.models import (
    Strategy,
    ScreenerCriteria,
    ScreenerResult,
)

logger = logging.getLogger(__name__)


class Strategist:
    """Proposes trading strategies via LLM and reviews them with a CRO agent."""

    def __init__(self, db, config: dict):
        self.db = db
        self.config = config
        self.ar_config = config.get("autoresearch", {})

    def propose(
        self,
        screener_results: list[ScreenerResult],
        regime: str,
        generation: int,
    ) -> list[Strategy]:
        """Propose new strategies based on screener data and market regime.

        1. Fetches top strategies, reflections, analyst weights from DB
        2. Builds prompt, calls strategist LLM (Sonnet)
        3. Parses JSON response into Strategy objects
        4. For each, runs CRO review (Haiku)
        5. Persists survivors to DB

        Returns list of strategies that passed CRO review.
        """
        # Gather context from DB
        top_strategies = self.db.get_top_strategies(limit=5)
        reflections = self.db.get_reflections()
        analyst_weights = self.db.get_analyst_weights()

        # Build strategist prompt
        prompt = self._build_propose_prompt(
            screener_results, regime, generation,
            top_strategies, reflections, analyst_weights,
        )

        # Call strategist LLM
        response = self._call_llm(
            prompt,
            model=self.ar_config.get("strategist_model", "claude-sonnet-4-20250514"),
        )

        # Parse strategies from response
        strategies = self._parse_strategies(response, generation, regime)
        if not strategies:
            logger.warning("Strategist produced no valid strategies for gen %d", generation)
            return []

        # CRO review each strategy
        survivors = []
        for strategy in strategies:
            approved, reason = self.cro_review(strategy)
            if approved:
                # Persist to DB
                sid = self.db.insert_strategy(
                    generation=strategy.generation,
                    parent_ids=strategy.parent_ids,
                    name=strategy.name,
                    hypothesis=strategy.hypothesis,
                    conviction=strategy.conviction,
                    screener_criteria=strategy.screener.to_dict(),
                    instrument=strategy.instrument,
                    entry_rules=strategy.entry_rules,
                    exit_rules=strategy.exit_rules,
                    position_size_pct=strategy.position_size_pct,
                    max_risk_pct=strategy.max_risk_pct,
                    time_horizon_days=strategy.time_horizon_days,
                    regime_born=strategy.regime_born,
                    status="proposed",
                )
                strategy.id = sid
                survivors.append(strategy)
                logger.info("Strategy '%s' approved by CRO (id=%d)", strategy.name, sid)
            else:
                logger.info("Strategy '%s' rejected by CRO: %s", strategy.name, reason)

        return survivors

    def cro_review(self, strategy: Strategy) -> tuple[bool, str]:
        """Adversarial Chief Risk Officer review of a strategy.

        Returns (approved: bool, reason: str).
        """
        prompt = self._build_cro_prompt(strategy)
        response = self._call_llm(
            prompt,
            model=self.ar_config.get("cro_model", "claude-haiku-4-5-20251001"),
        )
        return self._parse_cro_response(response)

    def reflect(
        self,
        generation: int,
        strategies: list[Strategy],
        top_all_time: list[dict],
    ) -> dict:
        """Post-generation reflection: analyze what worked and what didn't.

        Writes reflection to DB and returns the reflection dict.
        """
        prompt = self._build_reflect_prompt(generation, strategies, top_all_time)
        response = self._call_llm(
            prompt,
            model=self.ar_config.get("strategist_model", "claude-sonnet-4-20250514"),
        )
        reflection = self._parse_reflection(response)

        self.db.insert_reflection(
            generation=generation,
            patterns_that_work=reflection.get("patterns_that_work", []),
            patterns_that_fail=reflection.get("patterns_that_fail", []),
            next_generation_guidance=reflection.get("next_generation_guidance", []),
            regime_notes=reflection.get("regime_notes", ""),
        )

        return reflection

    def _call_llm(self, prompt: str, model: str) -> str:
        """Call an LLM with the given prompt. Uses Anthropic provider."""
        from tradingagents.llm_clients import create_llm_client

        client = create_llm_client(
            provider="anthropic",
            model=model,
        )
        llm = client.get_llm()
        response = llm.invoke(prompt)
        return response.content if hasattr(response, "content") else str(response)

    def _build_propose_prompt(
        self,
        screener_results: list[ScreenerResult],
        regime: str,
        generation: int,
        top_strategies: list[dict],
        reflections: list[dict],
        analyst_weights: dict[str, float],
    ) -> str:
        """Build the strategy proposal prompt."""
        num_strategies = self.ar_config.get("strategies_per_generation", 4)

        # Format screener results
        screener_summary = "\n".join(
            f"- {r.ticker}: close={r.close:.2f}, RSI={r.rsi_14:.1f}, "
            f"EMA10={r.ema_10:.2f}, EMA50={r.ema_50:.2f}, "
            f"sector={r.sector}, regime={r.regime}"
            for r in screener_results[:20]
        )

        # Format top strategies
        if top_strategies:
            top_str = "\n".join(
                f"- {s['name']} (fitness={s.get('fitness_score', 0):.4f}, "
                f"instrument={s['instrument']})"
                for s in top_strategies
            )
        else:
            top_str = "No previous strategies."

        # Format reflections
        if reflections:
            latest = reflections[-1]
            reflection_str = (
                f"Patterns that work: {json.dumps(latest.get('patterns_that_work', []))}\n"
                f"Patterns that fail: {json.dumps(latest.get('patterns_that_fail', []))}\n"
                f"Guidance: {json.dumps(latest.get('next_generation_guidance', []))}"
            )
        else:
            reflection_str = "No prior reflections."

        # Format analyst weights
        if analyst_weights:
            weights_str = ", ".join(f"{k}={v:.2f}" for k, v in analyst_weights.items())
        else:
            weights_str = "Default weights (1.0 each)."

        return f"""You are a quantitative trading strategist. Propose {num_strategies} new trading strategies.

## Current Market Regime: {regime}

## Screener Results (top tickers):
{screener_summary}

## Top Historical Strategies:
{top_str}

## Lessons from Prior Generations:
{reflection_str}

## Analyst Weights:
{weights_str}

## Generation: {generation}

## Requirements:
- Each strategy must have a clear hypothesis
- Include specific entry and exit rules
- Specify instrument type: stock_long, stock_short, call_option, put_option, spread
- Set position_size_pct (0.01-0.10) and max_risk_pct (0.01-0.10)
- Set time_horizon_days (1-90)
- Set conviction (0-100)
- Build on what worked, avoid what failed
- Diversify across instruments and sectors

## Response Format (JSON array):
```json
[
  {{
    "name": "strategy_name",
    "hypothesis": "why this should work",
    "instrument": "stock_long",
    "entry_rules": ["RSI_14 crosses above 30", "price > EMA_10"],
    "exit_rules": ["50% profit target", "25% stop loss", "time_horizon exceeded"],
    "position_size_pct": 0.05,
    "max_risk_pct": 0.05,
    "time_horizon_days": 30,
    "conviction": 70,
    "parent_ids": [],
    "screener_criteria": {{
      "market_cap_range": [1e9, 1e12],
      "min_avg_volume": 500000,
      "sector": null,
      "custom_filters": []
    }}
  }}
]
```

Return ONLY the JSON array, no other text."""

    def _build_cro_prompt(self, strategy: Strategy) -> str:
        """Build the CRO adversarial review prompt."""
        return f"""You are a Chief Risk Officer reviewing a proposed trading strategy.
Be adversarial — look for flaws, unrealistic assumptions, and hidden risks.

## Strategy Under Review:
{strategy.to_prompt_str()}

## Entry Rules: {json.dumps(strategy.entry_rules)}
## Exit Rules: {json.dumps(strategy.exit_rules)}
## Position Size: {strategy.position_size_pct:.0%}
## Max Risk: {strategy.max_risk_pct:.0%}

## Review Criteria:
1. Are the entry/exit rules specific enough to be testable?
2. Is the position sizing appropriate for the risk level?
3. Does the hypothesis make economic sense?
4. Are there obvious risks not addressed by the exit rules?
5. Is the time horizon realistic for the instrument type?

## Response Format (JSON):
```json
{{
  "approved": true/false,
  "reason": "explanation of decision",
  "risk_score": 1-10,
  "concerns": ["list of concerns"]
}}
```

Return ONLY the JSON, no other text."""

    def _build_reflect_prompt(
        self,
        generation: int,
        strategies: list[Strategy],
        top_all_time: list[dict],
    ) -> str:
        """Build the post-generation reflection prompt."""
        strat_summaries = "\n".join(
            f"- {s.name}: fitness={s.fitness_score:.4f}, "
            f"instrument={s.instrument}, status={s.status}"
            for s in strategies
        )

        top_str = "\n".join(
            f"- {s['name']}: fitness={s.get('fitness_score', 0):.4f}"
            for s in top_all_time[:5]
        ) if top_all_time else "None yet."

        return f"""You are reflecting on generation {generation} of strategy evolution.

## This Generation's Strategies:
{strat_summaries}

## All-Time Top Strategies:
{top_str}

## Your Task:
Analyze patterns in what worked and what failed. Provide guidance for the next generation.

## Response Format (JSON):
```json
{{
  "patterns_that_work": ["pattern1", "pattern2"],
  "patterns_that_fail": ["pattern1", "pattern2"],
  "next_generation_guidance": ["guidance1", "guidance2"],
  "regime_notes": "observations about current market regime"
}}
```

Return ONLY the JSON, no other text."""

    def _parse_strategies(
        self, response: str, generation: int, regime: str
    ) -> list[Strategy]:
        """Parse LLM response into Strategy objects."""
        # Try to extract JSON from response
        text = response.strip()
        # Handle markdown code blocks
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.error("Failed to parse strategist response as JSON")
            return []

        if not isinstance(data, list):
            data = [data]

        strategies = []
        for item in data:
            try:
                screener = ScreenerCriteria.from_dict(item.get("screener_criteria", {}))
                strategy = Strategy(
                    generation=generation,
                    parent_ids=item.get("parent_ids", []),
                    name=item.get("name", "unnamed"),
                    hypothesis=item.get("hypothesis", ""),
                    conviction=item.get("conviction", 50),
                    screener=screener,
                    instrument=item.get("instrument", "stock_long"),
                    entry_rules=item.get("entry_rules", []),
                    exit_rules=item.get("exit_rules", []),
                    position_size_pct=item.get("position_size_pct", 0.05),
                    max_risk_pct=item.get("max_risk_pct", 0.05),
                    time_horizon_days=item.get("time_horizon_days", 30),
                    regime_born=regime,
                    status="proposed",
                )
                strategies.append(strategy)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("Failed to parse strategy item: %s", e)
                continue

        return strategies

    def _parse_cro_response(self, response: str) -> tuple[bool, str]:
        """Parse CRO review response."""
        text = response.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        try:
            data = json.loads(text)
            approved = data.get("approved", False)
            reason = data.get("reason", "No reason given")
            return approved, reason
        except json.JSONDecodeError:
            # If we can't parse, reject as a safety measure
            logger.warning("Failed to parse CRO response, rejecting strategy")
            return False, "CRO response unparseable"

    def _parse_reflection(self, response: str) -> dict:
        """Parse reflection response."""
        text = response.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse reflection response")
            return {
                "patterns_that_work": [],
                "patterns_that_fail": [],
                "next_generation_guidance": [],
                "regime_notes": "",
            }
