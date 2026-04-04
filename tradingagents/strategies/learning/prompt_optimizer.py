"""Atlas-GIC-inspired prompt optimization loop.

LLM analyzer prompts are the trainable parameters. Market outcomes
(from the signal journal) are the loss function. The optimizer:
1. Scores each strategy's prompt by realized signal outcomes.
2. Identifies the worst-performing prompt (by hit rate).
3. Proposes a targeted modification via meta-prompt.
4. Trials the new prompt for 5 trading days.
5. Keeps or reverts based on Sharpe comparison.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

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

    def evaluate_prompts(self, journal: Any) -> dict[str, dict]:
        """Score each LLM-using strategy's prompt by realized outcomes.

        For each strategy:
        - Get all signals with return_5d filled
        - Split by high-conviction (llm_conviction > 0.6) vs low
        - Compute hit_rate (direction matched actual return sign),
          avg_return, conviction_calibration

        Returns {strategy_name: {hit_rate, avg_return, calibration, n_signals}}.
        """
        scores: dict[str, dict] = {}

        for strategy in LLM_STRATEGIES:
            entries = journal.get_entries(strategy=strategy)
            with_outcomes = [
                e for e in entries
                if e.get("return_5d") is not None and e.get("llm_conviction", 0) > 0
            ]

            if len(with_outcomes) < MIN_SIGNALS_FOR_EVAL:
                scores[strategy] = {
                    "hit_rate": 0.0,
                    "avg_return": 0.0,
                    "calibration": 0.0,
                    "n_signals": len(with_outcomes),
                    "high_conviction_hits": 0,
                    "high_conviction_total": 0,
                }
                continue

            hits = 0
            returns = []
            high_conv_hits = 0
            high_conv_total = 0

            for e in with_outcomes:
                ret_5d = e["return_5d"]
                direction = e.get("direction", "long")
                conviction = e.get("llm_conviction", 0.5)

                # Did direction match the actual return?
                correct = (direction == "long" and ret_5d > 0) or (
                    direction == "short" and ret_5d < 0
                )
                if correct:
                    hits += 1

                returns.append(ret_5d)

                if conviction > 0.6:
                    high_conv_total += 1
                    if correct:
                        high_conv_hits += 1

            hit_rate = hits / len(with_outcomes) if with_outcomes else 0.0
            avg_return = sum(returns) / len(returns) if returns else 0.0

            # Calibration: high-conviction signals should have higher hit rate
            hc_hit_rate = high_conv_hits / high_conv_total if high_conv_total > 0 else 0.0
            calibration = hc_hit_rate - hit_rate  # positive = well-calibrated

            scores[strategy] = {
                "hit_rate": hit_rate,
                "avg_return": avg_return,
                "calibration": calibration,
                "n_signals": len(with_outcomes),
                "high_conviction_hits": high_conv_hits,
                "high_conviction_total": high_conv_total,
            }

        return scores

    def identify_worst_prompt(self, scores: dict[str, dict]) -> str | None:
        """Return strategy name with lowest hit_rate (min signals required).

        Returns None if no strategy has enough data.
        """
        eligible = {
            name: s for name, s in scores.items()
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
            lines = [l for l in lines if not l.strip().startswith("```")]
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

    def check_trial(self, trial_id: str, journal: Any) -> str:
        """After TRIAL_DAYS trading days, compare trial vs baseline.

        Returns "keep" | "revert" | "ongoing".
        """
        trials = self._load_trials()
        trial = trials.get(trial_id)
        if not trial or trial["status"] != "active":
            return "ongoing"

        start_date = trial["start_date"][:10]  # YYYY-MM-DD
        strategy = trial["strategy"]

        # Get signals since trial started
        entries = journal.get_entries(strategy=strategy, since=start_date)
        with_outcomes = [e for e in entries if e.get("return_5d") is not None]

        if len(with_outcomes) < 5:
            return "ongoing"

        # Compute trial hit rate
        hits = sum(
            1 for e in with_outcomes
            if (e["direction"] == "long" and e["return_5d"] > 0)
            or (e["direction"] == "short" and e["return_5d"] < 0)
        )
        trial_hit_rate = hits / len(with_outcomes)

        # Compare against baseline (pre-trial signals)
        all_entries = journal.get_entries(strategy=strategy)
        pre_trial = [
            e for e in all_entries
            if e.get("return_5d") is not None
            and e.get("timestamp", "") < start_date
            and e.get("llm_conviction", 0) > 0
        ]

        if not pre_trial:
            return "keep" if trial_hit_rate > 0.5 else "revert"

        baseline_hits = sum(
            1 for e in pre_trial
            if (e["direction"] == "long" and e["return_5d"] > 0)
            or (e["direction"] == "short" and e["return_5d"] < 0)
        )
        baseline_hit_rate = baseline_hits / len(pre_trial)

        # Keep if trial improves hit rate by at least 2pp
        if trial_hit_rate >= baseline_hit_rate + 0.02:
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
                history_path = self._history_dir / f"{strategy}_{timestamp}_baseline.txt"
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
                history_path = self._history_dir / f"{strategy}_{timestamp}_reverted.txt"
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
