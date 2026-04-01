"""Autoresearch evolution loop with LLM-based param proposals and mutation.

Evolves strategy parameters over multiple generations using the
Atlas-GIC keep/revert pattern:
1. Identify worst-performing strategy
2. Generate ONE targeted mutation (via Haiku)
3. Run for N generations
4. If improved → keep. If not → revert.
"""
from __future__ import annotations

import copy
import logging
from typing import Any

from tradingagents.autoresearch.darwinian import update_weights
from tradingagents.autoresearch.llm_analyzer import LLMAnalyzer
from tradingagents.autoresearch.multi_strategy_engine import MultiStrategyEngine
from tradingagents.autoresearch.state import StateManager

logger = logging.getLogger(__name__)


class AutoresearchLoop:
    """Evolves strategy parameters and prompts over generations."""

    def __init__(
        self,
        engine: MultiStrategyEngine,
        analyzer: LLMAnalyzer | None = None,
        evaluation_window: int = 3,
    ) -> None:
        """
        Args:
            engine: The multi-strategy engine to run generations with.
            analyzer: LLM analyzer for param proposals and reflection.
            evaluation_window: Generations to evaluate mutations before keep/revert.
        """
        self.engine = engine
        self.analyzer = analyzer or LLMAnalyzer(engine.config)
        self.evaluation_window = evaluation_window

        # Mutation tracking
        self._active_mutation: dict | None = None
        self._mutation_baseline: dict | None = None

    def run(
        self,
        num_generations: int = 5,
        start_date: str | None = None,
        end_date: str | None = None,
        use_llm_proposals: bool = False,
    ) -> list[dict]:
        """Run the full autoresearch evolution loop.

        Args:
            num_generations: How many generations to run.
            start_date: Backtest start date.
            end_date: Backtest end date.
            use_llm_proposals: If True, use LLM to propose params (costs ~$0.01/gen).
                              If False, use random perturbation (free).

        Returns:
            List of generation result dicts.
        """
        results = []
        start_gen = self.engine.state.get_latest_generation() + 1

        for i in range(num_generations):
            gen = start_gen + i
            logger.info("=== Autoresearch Generation %d ===", gen)

            # Check for mutation evaluation
            if self._active_mutation and i > 0 and i % self.evaluation_window == 0:
                self._evaluate_mutation(results)

            # Propose a mutation for the weakest strategy every N gens
            if not self._active_mutation and i > 0 and i % self.evaluation_window == 0:
                scores = results[-1].get("scores", {}) if results else {}
                if scores:
                    self._propose_mutation(scores)

            # Run generation
            gen_result = self.engine.run_generation(
                generation=gen,
                start_date=start_date,
                end_date=end_date,
            )
            results.append(gen_result)

            # Reflect (LLM call if available)
            if use_llm_proposals:
                self._reflect(gen, gen_result)

            # Log progress
            scores = gen_result.get("scores", {})
            if scores:
                best = max(scores, key=scores.get)
                worst = min(scores, key=scores.get)
                logger.info(
                    "Gen %d: best=%s (%.3f), worst=%s (%.3f)",
                    gen, best, scores[best], worst, scores[worst],
                )

        return results

    def run_two_phase(
        self,
        backtest_generations: int = 50,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        """Run the full two-phase pipeline: backtest evolution → paper trading.

        Phase 1: Run backtest_generations of backtest evolution (aggressive weights).
        Phase 2: Run one paper trading loop iteration (no weight changes).
        Optionally triggers learning loop if enough trades accumulated.

        Returns:
            Dict with keys: playbook, trading_result, learning_result.
        """
        # Phase 1
        logger.info("=== Phase 1: Backtest Evolution (%d generations) ===", backtest_generations)
        playbook = self.engine.run_backtest_phase(
            num_generations=backtest_generations,
            start_date=start_date,
            end_date=end_date,
        )

        # Phase 2: Trading loop
        logger.info("=== Phase 2: Paper Trading ===")
        trading_result = self.engine.run_paper_trade_phase(playbook=playbook)

        # Check learning loop
        learning_result = None
        if self.engine._should_trigger_learning_loop():
            logger.info("=== Learning Loop Triggered ===")
            learning_result = self.engine.run_learning_loop()

        return {
            "playbook": playbook,
            "trading_result": trading_result,
            "learning_result": learning_result,
        }

    # ------------------------------------------------------------------
    # Mutation: identify weakest → propose change → evaluate → keep/revert
    # ------------------------------------------------------------------

    def identify_weakest(self, scores: dict[str, float]) -> str | None:
        """Find the worst-performing strategy that has been scored."""
        scored = {k: v for k, v in scores.items() if v is not None}
        if not scored:
            return None
        return min(scored, key=scored.get)

    def _propose_mutation(self, scores: dict[str, float]) -> None:
        """Propose a targeted mutation for the weakest strategy."""
        weakest = self.identify_weakest(scores)
        if weakest is None:
            return

        # Find the strategy module
        strategy = None
        for s in self.engine.strategies:
            if s.name == weakest:
                strategy = s
                break

        if strategy is None:
            return

        logger.info("Proposing mutation for weakest strategy: %s (score=%.3f)",
                     weakest, scores[weakest])

        # Save baseline for comparison
        self._mutation_baseline = {
            "strategy": weakest,
            "score_before": scores[weakest],
            "params_before": copy.deepcopy(strategy.get_default_params()),
            "generation_started": self.engine.state.get_latest_generation(),
        }

        # Try LLM proposal
        context = {
            "current_params": strategy.get_default_params(),
            "recent_results": [],
        }
        prompt = strategy.build_propose_prompt(context)
        proposed = self.analyzer.propose_params(prompt)

        if proposed:
            self._active_mutation = {
                "strategy": weakest,
                "proposed_params": proposed[0] if proposed else {},
                "all_proposals": proposed,
            }
            logger.info("Mutation proposed for %s: %s", weakest, proposed[0] if proposed else "none")
        else:
            logger.info("No LLM mutation available — using random perturbation")
            self._active_mutation = None

    def _evaluate_mutation(self, results: list[dict]) -> None:
        """Evaluate whether a mutation improved the strategy. Keep or revert."""
        if not self._active_mutation or not self._mutation_baseline:
            return

        strategy_name = self._active_mutation["strategy"]
        baseline_score = self._mutation_baseline["score_before"]

        # Average score over evaluation window
        recent_scores = []
        for r in results[-self.evaluation_window:]:
            s = r.get("scores", {}).get(strategy_name)
            if s is not None:
                recent_scores.append(s)

        if not recent_scores:
            logger.info("No scores for %s during evaluation — reverting", strategy_name)
            self._revert_mutation()
            return

        avg_score = sum(recent_scores) / len(recent_scores)

        if avg_score > baseline_score:
            logger.info(
                "KEEP mutation for %s: %.3f → %.3f (improved)",
                strategy_name, baseline_score, avg_score,
            )
            self._active_mutation = None
            self._mutation_baseline = None
        else:
            logger.info(
                "REVERT mutation for %s: %.3f → %.3f (no improvement)",
                strategy_name, baseline_score, avg_score,
            )
            self._revert_mutation()

    def _revert_mutation(self) -> None:
        """Revert an unsuccessful mutation."""
        self._active_mutation = None
        self._mutation_baseline = None

    # ------------------------------------------------------------------
    # Reflection
    # ------------------------------------------------------------------

    def _reflect(self, generation: int, gen_result: dict) -> None:
        """Generate and save a reflection on the generation."""
        scores = gen_result.get("scores", {})
        weights = gen_result.get("weights", {})

        # Get top strategy details
        top_results = {}
        bt_results = gen_result.get("backtest_results", {})
        for name in sorted(scores, key=scores.get, reverse=True)[:3]:
            if name in bt_results:
                top_results[name] = {
                    "score": scores[name],
                    "num_param_sets": len(bt_results[name]),
                }

        reflection = self.analyzer.reflect_on_generation(
            generation, scores, weights, top_results
        )

        if reflection:
            self.engine.state.save_reflection(generation, reflection)
            logger.info("Reflection saved: %s", reflection.get("summary", ""))
