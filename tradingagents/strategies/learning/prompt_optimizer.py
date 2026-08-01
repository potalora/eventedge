"""Diagnostic prompt-trial scoring over explicit metrics-v2 outcomes.

LLM analyzer prompts are the trainable parameters. Persisted ``OutcomeRecord``
inputs are the diagnostic evidence. The optimizer:
1. Scores each strategy's prompt by realized signal outcomes.
2. Identifies the worst-performing prompt (by hit rate).
3. Proposes a targeted modification via meta-prompt.
4. Trials the new prompt for 5 trading days.
5. Keeps or reverts based on directional-accuracy comparison.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from tradingagents.strategies.metrics.models import OutcomeRecord
from tradingagents.strategies.metrics.outcomes import directional_accuracy

logger = logging.getLogger(__name__)

# Strategies that use LLM analysis (others are purely rule-based)
LLM_STRATEGIES = {
    "earnings_call",
    "insider_activity",
    "filing_analysis",
    "regulatory_pipeline",
    "supply_chain",
    "litigation",
}

TRIAL_DAYS = 5
MIN_SIGNALS_FOR_EVAL = 20
DIAGNOSTIC_HOLDING_SESSIONS = 5


class PromptOptimizer:
    """Evolves LLM analyzer prompts based on signal journal outcomes."""

    def __init__(self, state_dir: str, analyzer: Any) -> None:
        self._prompts_dir = Path(state_dir) / "prompts"
        self._history_dir = self._prompts_dir / "history"
        self._trials_path = Path(state_dir) / "prompt_trials.json"
        self._analyzer = analyzer

        self._prompts_dir.mkdir(parents=True, exist_ok=True)
        self._history_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Evaluate prompt performance
    # ------------------------------------------------------------------

    def evaluate_prompts(
        self, outcomes_by_strategy: Mapping[str, Iterable[OutcomeRecord]]
    ) -> dict[str, dict]:
        """Score each LLM-using strategy's prompt by realized outcomes.

        For each strategy:
        - Consume persisted v2 outcome records
        - Compute directional accuracy once through the governed metric helper
        - Surface unavailable return/conviction diagnostics honestly as ``None``

        Returns per-strategy accuracy and sample-size disclosures.
        """
        scores: dict[str, dict] = {}

        for strategy in LLM_STRATEGIES:
            outcomes = self._validated_outcomes(
                strategy, outcomes_by_strategy.get(strategy, ())
            )
            accuracy = directional_accuracy(outcomes)

            if accuracy.actionable_count < MIN_SIGNALS_FOR_EVAL:
                scores[strategy] = {
                    "hit_rate": accuracy.rate,
                    "avg_return": None,
                    "calibration": None,
                    "n_signals": accuracy.actionable_count,
                    "high_conviction_hits": None,
                    "high_conviction_total": None,
                }
                continue

            scores[strategy] = {
                "hit_rate": accuracy.rate,
                "avg_return": None,
                "calibration": None,
                "n_signals": accuracy.actionable_count,
                "high_conviction_hits": None,
                "high_conviction_total": None,
            }

        return scores

    @staticmethod
    def _validated_outcomes(
        strategy: str, outcomes: Iterable[OutcomeRecord]
    ) -> tuple[OutcomeRecord, ...]:
        rows = tuple(outcomes)
        if any(row.strategy != strategy for row in rows):
            raise ValueError(f"outcome strategy does not match {strategy!r}")
        rows = tuple(
            row for row in rows if row.holding_sessions == DIAGNOSTIC_HOLDING_SESSIONS
        )
        epoch_ids = {row.epoch_id for row in rows}
        if len(epoch_ids) > 1:
            raise ValueError("prompt diagnostics cannot mix metric epochs")
        return rows

    def identify_worst_prompt(self, scores: dict[str, dict]) -> str | None:
        """Return strategy name with lowest hit_rate (min signals required).

        Returns None if no strategy has enough data.
        """
        eligible = {
            name: s
            for name, s in scores.items()
            if s["n_signals"] >= MIN_SIGNALS_FOR_EVAL
        }

        if not eligible:
            return None

        return min(eligible, key=lambda n: eligible[n]["hit_rate"])

    # ------------------------------------------------------------------
    # Propose and trial modifications
    # ------------------------------------------------------------------

    def propose_modification(
        self,
        strategy_name: str,
        current_prompt: str,
        recent_failures: list[dict],
    ) -> str:
        """Use LLM meta-prompt to propose a targeted prompt modification.

        Args:
            strategy_name: Strategy whose prompt is being optimized.
            current_prompt: The current system prompt text.
            recent_failures: Recent high-conviction signals that were wrong.

        Returns:
            Modified prompt text.
        """
        failures_text = json.dumps(recent_failures[:10], indent=2, default=str)

        meta_system = """You are optimizing an analyst prompt for a trading signal system.
You will be given the current prompt and recent signals where the conviction was high
but the outcome was wrong (the predicted direction did not match the actual 5-day return).

Propose ONE specific, targeted change to the prompt that would improve accuracy.
Keep the change minimal — do not rewrite the entire prompt. Focus on:
- Adding a specific check or caveat that would have caught the failures
- Adjusting conviction calibration guidance
- Adding domain-specific knowledge that was missing

Return the COMPLETE modified prompt (not just the diff)."""

        meta_user = f"""Strategy: {strategy_name}

CURRENT PROMPT:
{current_prompt}

RECENT HIGH-CONVICTION FAILURES (predicted direction was wrong):
{failures_text}

Return the complete modified prompt."""

        result = self._analyzer._call_llm(meta_system, meta_user, max_tokens=4096)
        if not result:
            return current_prompt

        # Clean up: remove markdown code fences if present
        result = result.strip()
        if result.startswith("```"):
            lines = result.split("\n")
            lines = [line for line in lines if not line.strip().startswith("```")]
            result = "\n".join(lines).strip()

        return result

    def start_trial(self, strategy_name: str, new_prompt: str) -> str:
        """Save new prompt as a trial, record start date. Returns trial_id."""
        prompt_hash = hashlib.sha256(new_prompt.encode()).hexdigest()[:12]
        trial_id = f"{strategy_name}_{prompt_hash}"

        # Save the trial prompt
        prompt_path = self._prompts_dir / f"{strategy_name}_trial.txt"
        prompt_path.write_text(new_prompt)

        # Save baseline (current active) if not already saved
        baseline_path = self._prompts_dir / f"{strategy_name}_baseline.txt"
        if not baseline_path.exists():
            current = self._analyzer.get_prompt(strategy_name)
            baseline_path.write_text(current)

        # Record trial metadata
        trials = self._load_trials()
        trials[trial_id] = {
            "strategy": strategy_name,
            "start_date": datetime.now().isoformat(),
            "prompt_hash": prompt_hash,
            "status": "active",
        }
        self._save_trials(trials)

        # Activate the trial prompt
        self._analyzer.set_prompt_override(strategy_name, new_prompt)

        logger.info("Started prompt trial %s for %s", trial_id, strategy_name)
        return trial_id

    def check_trial(self, trial_id: str, outcomes: Iterable[OutcomeRecord]) -> str:
        """After TRIAL_DAYS trading days, compare trial vs baseline.

        Returns "keep" | "revert" | "ongoing".
        """
        trials = self._load_trials()
        trial = trials.get(trial_id)
        if not trial or trial["status"] != "active":
            return "ongoing"

        try:
            start_session = date.fromisoformat(trial["start_date"][:10])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                "trial start_date must contain an ISO session date"
            ) from error
        strategy = trial["strategy"]
        strategy_outcomes = self._validated_outcomes(
            strategy, (row for row in tuple(outcomes) if row.strategy == strategy)
        )

        trial_outcomes = tuple(
            row for row in strategy_outcomes if row.entry_session >= start_session
        )
        trial_accuracy = directional_accuracy(trial_outcomes)
        actionable_trial_sessions = {
            row.entry_session
            for row in trial_outcomes
            if row.status == "valid" and row.direction in {"long", "short"}
        }
        if len(actionable_trial_sessions) < TRIAL_DAYS or trial_accuracy.rate is None:
            return "ongoing"
        baseline_accuracy = directional_accuracy(
            tuple(row for row in strategy_outcomes if row.entry_session < start_session)
        )
        if baseline_accuracy.rate is None:
            return "keep" if trial_accuracy.rate > 0.5 else "revert"

        # Keep if trial improves hit rate by at least 2pp
        if trial_accuracy.rate >= baseline_accuracy.rate + 0.02:
            return "keep"
        return "revert"

    def commit_or_revert(self, trial_id: str, decision: str) -> None:
        """Commit or revert a trial prompt.

        If "keep": move trial prompt to active, archive baseline.
        If "revert": restore baseline, archive trial.
        """
        trials = self._load_trials()
        trial = trials.get(trial_id)
        if not trial:
            return

        strategy = trial["strategy"]
        trial_path = self._prompts_dir / f"{strategy}_trial.txt"
        baseline_path = self._prompts_dir / f"{strategy}_baseline.txt"
        active_path = self._prompts_dir / f"{strategy}.txt"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if decision == "keep":
            # Archive baseline
            if baseline_path.exists():
                history_path = (
                    self._history_dir / f"{strategy}_{timestamp}_baseline.txt"
                )
                history_path.write_text(baseline_path.read_text())

            # Promote trial to active
            if trial_path.exists():
                active_path.write_text(trial_path.read_text())
                self._analyzer.set_prompt_override(strategy, trial_path.read_text())
                trial_path.unlink()
            if baseline_path.exists():
                baseline_path.unlink()

            logger.info("Committed prompt trial %s: KEPT", trial_id)

        elif decision == "revert":
            # Archive failed trial
            if trial_path.exists():
                history_path = (
                    self._history_dir / f"{strategy}_{timestamp}_reverted.txt"
                )
                history_path.write_text(trial_path.read_text())
                trial_path.unlink()

            # Restore baseline
            if baseline_path.exists():
                active_path.write_text(baseline_path.read_text())
                self._analyzer.set_prompt_override(strategy, baseline_path.read_text())
                baseline_path.unlink()
            else:
                # No baseline saved = use built-in default
                self._analyzer.set_prompt_override(strategy, "")

            logger.info("Committed prompt trial %s: REVERTED", trial_id)

        trial["status"] = decision
        trial["completed_date"] = datetime.now().isoformat()
        self._save_trials(trials)

    # ------------------------------------------------------------------
    # Active trial management
    # ------------------------------------------------------------------

    def get_active_trial(self) -> tuple[str | None, dict | None]:
        """Return (trial_id, trial_dict) for any active trial, or (None, None)."""
        trials = self._load_trials()
        for tid, trial in trials.items():
            if trial.get("status") == "active":
                return tid, trial
        return None, None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_trials(self) -> dict:
        if self._trials_path.exists():
            return json.loads(self._trials_path.read_text())
        return {}

    def _save_trials(self, trials: dict) -> None:
        self._trials_path.write_text(json.dumps(trials, indent=2))

    def get_prompt_version(self, strategy_name: str) -> str:
        """Return a short hash identifying the active prompt for a strategy."""
        prompt = self._analyzer.get_prompt(strategy_name)
        return hashlib.sha256(prompt.encode()).hexdigest()[:12]
