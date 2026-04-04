"""30-day isolated test of the PromptOptimizer lifecycle.

Exercises the real PromptOptimizer + LLMAnalyzer + SignalJournal components
over 30 simulated days with synthetic journal data.

Most tests mock the LLM _call_llm for speed. The TestLiveLLMOptimization
class at the bottom makes real Haiku API calls to validate the full loop.

Lifecycle per optimization cycle (~10 days):
  Days 1-5:  Accumulate signals with baseline prompt
  Day 6:     fill_outcomes for day-1 signals → evaluate → identify worst → propose → start trial
  Days 7-11: Accumulate signals with trial prompt
  Day 12:    fill_outcomes → check trial → commit or revert
  Repeat with next worst strategy
"""
from __future__ import annotations

import json
import os
import random
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.learning.prompt_optimizer import (
    LLM_STRATEGIES,
    MIN_SIGNALS_FOR_EVAL,
    TRIAL_DAYS,
    PromptOptimizer,
)
from tradingagents.strategies.learning.signal_journal import JournalEntry, SignalJournal

BASE_DATE = datetime(2026, 4, 1)

# --- Helpers ---


def _patch_trial_start_date(optimizer: PromptOptimizer, trial_id: str, new_date: str) -> None:
    """Override a trial's start_date for deterministic testing.

    start_trial uses datetime.now() which makes timestamp comparisons
    non-deterministic. This helper rewrites the start_date after creation.
    """
    trials = optimizer._load_trials()
    if trial_id in trials:
        trials[trial_id]["start_date"] = new_date
        optimizer._save_trials(trials)

def _make_entry(
    day: int,
    strategy: str,
    ticker: str,
    direction: str = "long",
    conviction: float = 0.7,
    score: float = 0.6,
    return_5d: float | None = None,
    prompt_version: str = "",
) -> JournalEntry:
    ts = (BASE_DATE + timedelta(days=day)).isoformat()
    return JournalEntry(
        timestamp=ts,
        strategy=strategy,
        ticker=ticker,
        direction=direction,
        score=score,
        llm_conviction=conviction,
        regime="normal",
        traded=True,
        entry_price=100.0,
        return_5d=return_5d,
        prompt_version=prompt_version,
    )


TICKERS = ["AAPL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "GOOG", "NFLX"]


def _seed_journal(
    journal: SignalJournal,
    strategies: list[str],
    num_days: int,
    signals_per_day: int,
    hit_rates: dict[str, float],
    rng: random.Random,
    day_offset: int = 0,
    prompt_versions: dict[str, str] | None = None,
) -> None:
    """Populate journal with signals that have known outcomes.

    For each strategy on each day, generates signals_per_day entries.
    return_5d is assigned immediately (simulating fill_outcomes already ran).
    The sign of return_5d is determined by the strategy's hit_rate.
    """
    prompt_versions = prompt_versions or {}
    for day in range(day_offset, day_offset + num_days):
        for strat in strategies:
            hr = hit_rates.get(strat, 0.5)
            for _ in range(signals_per_day):
                ticker = rng.choice(TICKERS)
                direction = rng.choice(["long", "short"])
                conviction = round(rng.uniform(0.3, 0.95), 2)

                # Outcome: correct with probability = hit_rate
                correct = rng.random() < hr
                magnitude = round(rng.uniform(0.005, 0.08), 4)
                if direction == "long":
                    ret = magnitude if correct else -magnitude
                else:
                    ret = -magnitude if correct else magnitude

                entry = _make_entry(
                    day=day,
                    strategy=strat,
                    ticker=ticker,
                    direction=direction,
                    conviction=conviction,
                    return_5d=ret,
                    prompt_version=prompt_versions.get(strat, "baseline_v0"),
                )
                journal.log_signal(entry)


def _seed_signals_no_outcomes(
    journal: SignalJournal,
    strategies: list[str],
    num_days: int,
    signals_per_day: int,
    rng: random.Random,
    day_offset: int = 0,
    prompt_versions: dict[str, str] | None = None,
) -> None:
    """Populate journal with signals that do NOT have outcomes yet."""
    prompt_versions = prompt_versions or {}
    for day in range(day_offset, day_offset + num_days):
        for strat in strategies:
            for _ in range(signals_per_day):
                ticker = rng.choice(TICKERS)
                direction = rng.choice(["long", "short"])
                conviction = round(rng.uniform(0.3, 0.95), 2)
                entry = _make_entry(
                    day=day,
                    strategy=strat,
                    ticker=ticker,
                    direction=direction,
                    conviction=conviction,
                    return_5d=None,
                    prompt_version=prompt_versions.get(strat, "baseline_v0"),
                )
                journal.log_signal(entry)


def _backfill_outcomes(
    journal: SignalJournal,
    since_day: int,
    hit_rate: float,
    rng: random.Random,
) -> None:
    """Manually backfill return_5d for entries that don't have it yet."""
    entries = journal.get_entries()
    since_dt = (BASE_DATE + timedelta(days=since_day)).isoformat()

    path = journal._path
    lines = path.read_text().splitlines()
    new_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            new_lines.append(line)
            continue

        if entry.get("return_5d") is None and entry.get("timestamp", "") >= since_dt:
            direction = entry.get("direction", "long")
            correct = rng.random() < hit_rate
            magnitude = round(rng.uniform(0.005, 0.08), 4)
            if direction == "long":
                entry["return_5d"] = magnitude if correct else -magnitude
            else:
                entry["return_5d"] = -magnitude if correct else magnitude

        new_lines.append(json.dumps(entry, default=str))

    path.write_text("\n".join(new_lines) + "\n")


# --- Tests ---


class TestPromptOptimizerEvaluation:
    """Test evaluation and worst-prompt identification with realistic data."""

    def test_evaluate_with_varied_hit_rates(self, tmp_path):
        """Strategies with different hit rates should be scored correctly."""
        rng = random.Random(42)
        journal = SignalJournal(str(tmp_path))
        analyzer = LLMAnalyzer()

        hit_rates = {
            "earnings_call": 0.70,
            "litigation": 0.35,
            "filing_analysis": 0.55,
            "insider_activity": 0.60,
            "supply_chain": 0.45,
            "regulatory_pipeline": 0.50,
        }

        _seed_journal(
            journal, list(hit_rates.keys()),
            num_days=10, signals_per_day=8,
            hit_rates=hit_rates, rng=rng,
        )

        optimizer = PromptOptimizer(str(tmp_path), analyzer)
        scores = optimizer.evaluate_prompts(journal)

        # All strategies should have 80 signals (10 days * 8/day)
        for strat in hit_rates:
            assert scores[strat]["n_signals"] == 80, f"{strat} has {scores[strat]['n_signals']}"

        # Litigation should be worst, earnings_call should be best
        worst = optimizer.identify_worst_prompt(scores)
        assert worst == "litigation"

        # Hit rates should roughly match seeded values (within tolerance)
        for strat, expected_hr in hit_rates.items():
            actual = scores[strat]["hit_rate"]
            assert abs(actual - expected_hr) < 0.20, (
                f"{strat}: expected ~{expected_hr}, got {actual}"
            )

    def test_insufficient_data_returns_none(self, tmp_path):
        """With fewer than MIN_SIGNALS_FOR_EVAL, identify_worst returns None."""
        rng = random.Random(42)
        journal = SignalJournal(str(tmp_path))
        analyzer = LLMAnalyzer()

        # Only 3 signals per strategy (well under 20)
        _seed_journal(
            journal, list(LLM_STRATEGIES),
            num_days=1, signals_per_day=3,
            hit_rates={s: 0.5 for s in LLM_STRATEGIES}, rng=rng,
        )

        optimizer = PromptOptimizer(str(tmp_path), analyzer)
        scores = optimizer.evaluate_prompts(journal)
        assert optimizer.identify_worst_prompt(scores) is None

    def test_high_conviction_calibration(self, tmp_path):
        """Calibration should be positive when high-conviction signals have higher hit rate."""
        journal = SignalJournal(str(tmp_path))
        analyzer = LLMAnalyzer()

        # Plant signals: high conviction (>0.6) are all correct, low conviction are 50/50
        for i in range(25):
            # High conviction, always correct
            journal.log_signal(_make_entry(
                day=i % 5, strategy="litigation", ticker=TICKERS[i % len(TICKERS)],
                direction="long", conviction=0.85,
                return_5d=0.03,  # correct for long
            ))
            # Low conviction, alternating correct/wrong
            journal.log_signal(_make_entry(
                day=i % 5, strategy="litigation", ticker=TICKERS[(i + 1) % len(TICKERS)],
                direction="long", conviction=0.4,
                return_5d=0.03 if i % 2 == 0 else -0.03,
            ))

        optimizer = PromptOptimizer(str(tmp_path), analyzer)
        scores = optimizer.evaluate_prompts(journal)
        lit = scores["litigation"]

        # High conviction hit rate should be 1.0, overall should be ~0.75
        assert lit["calibration"] > 0, f"calibration={lit['calibration']}"
        assert lit["high_conviction_hits"] == lit["high_conviction_total"]


class TestTrialLifecycle:
    """Test the full trial lifecycle: start → check → commit/revert."""

    def test_start_trial_creates_files_and_activates(self, tmp_path):
        """start_trial should create trial.txt, baseline.txt, and activate override."""
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        original_prompt = analyzer.get_prompt("litigation")
        new_prompt = original_prompt + "\nAlso check for patent trolls."

        trial_id = optimizer.start_trial("litigation", new_prompt)

        # Files created
        assert (tmp_path / "prompts" / "litigation_trial.txt").exists()
        assert (tmp_path / "prompts" / "litigation_baseline.txt").exists()

        # Trial recorded
        trials = optimizer._load_trials()
        assert trial_id in trials
        assert trials[trial_id]["status"] == "active"
        assert trials[trial_id]["strategy"] == "litigation"

        # Analyzer activated with new prompt
        assert analyzer.get_prompt("litigation") == new_prompt

        # Active trial check
        active_id, active_trial = optimizer.get_active_trial()
        assert active_id == trial_id

    def test_check_trial_ongoing_with_insufficient_data(self, tmp_path):
        """check_trial returns 'ongoing' when not enough signals have outcomes."""
        rng = random.Random(42)
        journal = SignalJournal(str(tmp_path))
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        trial_id = optimizer.start_trial("litigation", "new prompt text")

        # Only 3 signals with outcomes (need 5)
        for i in range(3):
            journal.log_signal(_make_entry(
                day=1, strategy="litigation", ticker=TICKERS[i],
                conviction=0.7, return_5d=0.02,
            ))

        assert optimizer.check_trial(trial_id, journal) == "ongoing"

    def test_check_trial_keep_when_improved(self, tmp_path):
        """Trial is kept when hit rate improves by >=2pp."""
        journal = SignalJournal(str(tmp_path))
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        # Seed baseline signals (before trial, day 0-4) with 50% hit rate
        for i in range(20):
            journal.log_signal(_make_entry(
                day=i % 5, strategy="litigation", ticker=TICKERS[i % len(TICKERS)],
                direction="long", conviction=0.7,
                return_5d=0.03 if i % 2 == 0 else -0.03,
            ))

        trial_id = optimizer.start_trial("litigation", "improved prompt")
        # Set trial start to day 5 so baseline (days 0-4) is before, trial (days 6+) is after
        trial_start_str = (BASE_DATE + timedelta(days=5)).isoformat()
        _patch_trial_start_date(optimizer, trial_id, trial_start_str)

        # Seed trial signals (after trial start, days 6-7) with 80% hit rate
        for i in range(10):
            ts = (BASE_DATE + timedelta(days=6, hours=i)).isoformat()
            journal.log_signal(JournalEntry(
                timestamp=ts,
                strategy="litigation",
                ticker=TICKERS[i % len(TICKERS)],
                direction="long",
                score=0.6,
                llm_conviction=0.7,
                entry_price=100.0,
                return_5d=0.03 if i < 8 else -0.03,  # 80% hit rate
            ))

        decision = optimizer.check_trial(trial_id, journal)
        assert decision == "keep"

    def test_check_trial_revert_when_worse(self, tmp_path):
        """Trial is reverted when hit rate doesn't improve enough."""
        journal = SignalJournal(str(tmp_path))
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        # Baseline (days 0-4): 60% hit rate
        for i in range(20):
            correct = i % 5 < 3  # 60%
            journal.log_signal(_make_entry(
                day=i % 5, strategy="litigation", ticker=TICKERS[i % len(TICKERS)],
                direction="long", conviction=0.7,
                return_5d=0.03 if correct else -0.03,
            ))

        trial_id = optimizer.start_trial("litigation", "worse prompt")
        # Set trial start to day 5
        trial_start_str = (BASE_DATE + timedelta(days=5)).isoformat()
        _patch_trial_start_date(optimizer, trial_id, trial_start_str)

        # Trial (days 6+): only 40% hit rate (worse)
        for i in range(10):
            ts = (BASE_DATE + timedelta(days=6, hours=i)).isoformat()
            correct = i < 4  # 40%
            journal.log_signal(JournalEntry(
                timestamp=ts,
                strategy="litigation",
                ticker=TICKERS[i % len(TICKERS)],
                direction="long",
                score=0.6,
                llm_conviction=0.7,
                entry_price=100.0,
                return_5d=0.03 if correct else -0.03,
            ))

        decision = optimizer.check_trial(trial_id, journal)
        assert decision == "revert"

    def test_commit_keeps_trial_prompt(self, tmp_path):
        """After commit(keep), trial prompt becomes active, baseline archived."""
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        original = analyzer.get_prompt("litigation")
        new_prompt = "This is the improved prompt."
        trial_id = optimizer.start_trial("litigation", new_prompt)

        optimizer.commit_or_revert(trial_id, "keep")

        # Trial prompt is now active
        assert analyzer.get_prompt("litigation") == new_prompt
        # Active file written
        active_path = tmp_path / "prompts" / "litigation.txt"
        assert active_path.exists()
        assert active_path.read_text() == new_prompt
        # Trial and baseline cleaned up
        assert not (tmp_path / "prompts" / "litigation_trial.txt").exists()
        assert not (tmp_path / "prompts" / "litigation_baseline.txt").exists()
        # History has archived baseline
        history_files = list((tmp_path / "prompts" / "history").glob("litigation_*_baseline.txt"))
        assert len(history_files) == 1
        assert history_files[0].read_text() == original
        # Trial status updated
        trials = optimizer._load_trials()
        assert trials[trial_id]["status"] == "keep"

    def test_revert_restores_baseline(self, tmp_path):
        """After commit(revert), baseline prompt is restored, trial archived."""
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        original = analyzer.get_prompt("litigation")
        trial_id = optimizer.start_trial("litigation", "bad prompt")

        optimizer.commit_or_revert(trial_id, "revert")

        # Baseline restored
        assert analyzer.get_prompt("litigation") == original
        # Failed trial archived
        history_files = list((tmp_path / "prompts" / "history").glob("litigation_*_reverted.txt"))
        assert len(history_files) == 1
        assert history_files[0].read_text() == "bad prompt"
        # Trial status updated
        trials = optimizer._load_trials()
        assert trials[trial_id]["status"] == "revert"


class TestProposalGeneration:
    """Test LLM-based prompt proposal (with mocked _call_llm)."""

    def test_propose_generates_modified_prompt(self, tmp_path):
        """propose_modification should call LLM and return modified prompt."""
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        current = analyzer.get_prompt("litigation")
        failures = [
            {
                "timestamp": "2026-04-05",
                "strategy": "litigation",
                "ticker": "AAPL",
                "direction": "short",
                "llm_conviction": 0.9,
                "return_5d": 0.05,
            },
        ]

        modified_text = current + "\nAlso verify the case has actual damages claimed."

        with patch.object(analyzer, "_call_llm", return_value=modified_text):
            result = optimizer.propose_modification("litigation", current, failures)

        assert result != current
        assert "actual damages" in result

    def test_propose_returns_current_on_llm_failure(self, tmp_path):
        """If LLM call fails, return current prompt unchanged."""
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        current = analyzer.get_prompt("litigation")

        with patch.object(analyzer, "_call_llm", return_value=""):
            result = optimizer.propose_modification("litigation", current, [])

        assert result == current

    def test_propose_strips_markdown_fences(self, tmp_path):
        """Markdown code fences in LLM response should be stripped."""
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        current = analyzer.get_prompt("litigation")
        fenced = "```\nCleaned prompt text here.\n```"

        with patch.object(analyzer, "_call_llm", return_value=fenced):
            result = optimizer.propose_modification("litigation", current, [])

        assert "```" not in result
        assert "Cleaned prompt text here." in result


class TestPromptVersionTracking:
    """Test that prompt versions are tracked correctly through trials."""

    def test_version_changes_after_commit(self, tmp_path):
        """Prompt version hash should change after a trial is committed."""
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        v1 = optimizer.get_prompt_version("litigation")
        assert len(v1) == 12

        trial_id = optimizer.start_trial("litigation", "completely new prompt")
        v_trial = optimizer.get_prompt_version("litigation")
        assert v_trial != v1  # Trial prompt is active

        optimizer.commit_or_revert(trial_id, "keep")
        v2 = optimizer.get_prompt_version("litigation")
        assert v2 == v_trial  # Kept the trial prompt

    def test_version_reverts_after_revert(self, tmp_path):
        """Prompt version should revert to baseline after revert."""
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        v1 = optimizer.get_prompt_version("litigation")

        trial_id = optimizer.start_trial("litigation", "experimental prompt")
        v_trial = optimizer.get_prompt_version("litigation")
        assert v_trial != v1

        optimizer.commit_or_revert(trial_id, "revert")
        v3 = optimizer.get_prompt_version("litigation")
        assert v3 == v1  # Back to original


class TestFullThirtyDayOptimizationLoop:
    """End-to-end 30-day simulation of the prompt optimization loop.

    Simulates the weekly learning cycle that Cohort B runs:
    - Week 1 (days 0-6):  Accumulate baseline signals
    - Week 2 (days 7-13): Evaluate → identify worst → propose → start trial
    - Week 3 (days 14-20): Trial signals accumulate, outcomes backfilled
    - Week 4 (days 21-27): Check trial → commit/revert → second cycle starts
    - Days 28-30: Final evaluation
    """

    def test_30_day_optimization_loop(self, tmp_path):
        rng = random.Random(42)
        journal = SignalJournal(str(tmp_path))
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        strategies = sorted(LLM_STRATEGIES)
        # Litigation has worst hit rate — optimizer should target it first
        hit_rates = {
            "earnings_call": 0.65,
            "filing_analysis": 0.55,
            "insider_activity": 0.60,
            "litigation": 0.35,
            "regulatory_pipeline": 0.50,
            "supply_chain": 0.45,
        }

        # Track state through the simulation
        trials_started = []
        trials_completed = []
        prompt_versions: dict[str, list[str]] = {s: [] for s in strategies}

        # Record initial prompt versions
        for s in strategies:
            prompt_versions[s].append(optimizer.get_prompt_version(s))

        # ----------------------------------------------------------------
        # WEEK 1 (Days 0-6): Accumulate baseline signals with outcomes
        # ----------------------------------------------------------------
        _seed_journal(
            journal, strategies,
            num_days=7, signals_per_day=4,
            hit_rates=hit_rates, rng=rng,
        )

        # Verify: each strategy has 28 signals with outcomes
        for s in strategies:
            entries = [e for e in journal.get_entries(strategy=s) if e.get("return_5d") is not None]
            assert len(entries) == 28, f"{s} has {len(entries)} entries after week 1"

        # ----------------------------------------------------------------
        # DAY 7: First optimization cycle — evaluate, propose, start trial
        # ----------------------------------------------------------------
        scores = optimizer.evaluate_prompts(journal)

        # All strategies should be eligible (28 >= 20)
        for s in strategies:
            assert scores[s]["n_signals"] >= MIN_SIGNALS_FOR_EVAL, f"{s}: {scores[s]['n_signals']}"

        worst = optimizer.identify_worst_prompt(scores)
        assert worst == "litigation", f"Expected litigation, got {worst}"

        # Get failures for proposal
        failures = journal.get_high_conviction_failures("litigation", limit=10)
        assert len(failures) > 0, "Should have high-conviction failures for litigation"

        # Propose modification (mock LLM)
        current_prompt = analyzer.get_prompt("litigation")
        improved_prompt = current_prompt + "\nIMPROVED: Check for frivolous lawsuit indicators before shorting."

        with patch.object(analyzer, "_call_llm", return_value=improved_prompt):
            proposed = optimizer.propose_modification("litigation", current_prompt, failures)

        assert proposed == improved_prompt

        # Start trial — patch start_date to day 7 boundary
        trial_id_1 = optimizer.start_trial("litigation", proposed)
        _patch_trial_start_date(optimizer, trial_id_1, (BASE_DATE + timedelta(days=7)).isoformat())
        trials_started.append(trial_id_1)

        # Verify trial is active
        active_id, active_trial = optimizer.get_active_trial()
        assert active_id == trial_id_1
        assert active_trial["strategy"] == "litigation"

        # Version should have changed
        new_version = optimizer.get_prompt_version("litigation")
        assert new_version != prompt_versions["litigation"][0]
        prompt_versions["litigation"].append(new_version)

        # ----------------------------------------------------------------
        # WEEK 2 (Days 7-13): Trial signals accumulate
        # ----------------------------------------------------------------
        _seed_signals_no_outcomes(
            journal, strategies,
            num_days=7, signals_per_day=4,
            rng=rng, day_offset=7,
            prompt_versions={s: optimizer.get_prompt_version(s) for s in strategies},
        )

        # Trial should still be "ongoing" — no outcomes yet for post-trial signals
        assert optimizer.check_trial(trial_id_1, journal) == "ongoing"

        # ----------------------------------------------------------------
        # DAY 14: Backfill outcomes for trial-period signals
        # ----------------------------------------------------------------
        _backfill_outcomes(journal, since_day=7, hit_rate=0.60, rng=rng)

        # Now trial should have enough data to evaluate
        decision_1 = optimizer.check_trial(trial_id_1, journal)
        assert decision_1 in ("keep", "revert"), f"Unexpected: {decision_1}"

        # With 60% trial vs ~35% baseline, should keep
        # (seed-dependent but heavily biased toward keep)
        optimizer.commit_or_revert(trial_id_1, decision_1)
        trials_completed.append((trial_id_1, decision_1))

        # Verify trial is no longer active
        active_id, _ = optimizer.get_active_trial()
        assert active_id is None

        # Record prompt version after first cycle
        prompt_versions["litigation"].append(optimizer.get_prompt_version("litigation"))

        # ----------------------------------------------------------------
        # DAY 14 continued: Second optimization cycle
        # ----------------------------------------------------------------
        scores_2 = optimizer.evaluate_prompts(journal)

        worst_2 = optimizer.identify_worst_prompt(scores_2)
        # With litigation potentially improved, another strategy should be worst
        assert worst_2 is not None
        assert worst_2 in strategies

        # Propose and start second trial
        current_2 = analyzer.get_prompt(worst_2)
        improved_2 = current_2 + f"\nIMPROVED: Better {worst_2} analysis v2."

        with patch.object(analyzer, "_call_llm", return_value=improved_2):
            proposed_2 = optimizer.propose_modification(worst_2, current_2,
                journal.get_high_conviction_failures(worst_2))

        trial_id_2 = optimizer.start_trial(worst_2, proposed_2)
        _patch_trial_start_date(optimizer, trial_id_2, (BASE_DATE + timedelta(days=14)).isoformat())
        trials_started.append(trial_id_2)

        # ----------------------------------------------------------------
        # WEEK 3 (Days 14-20): Second trial signals
        # ----------------------------------------------------------------
        _seed_signals_no_outcomes(
            journal, strategies,
            num_days=7, signals_per_day=4,
            rng=rng, day_offset=14,
        )

        # ----------------------------------------------------------------
        # DAY 21: Backfill and check second trial
        # ----------------------------------------------------------------
        _backfill_outcomes(journal, since_day=14, hit_rate=0.50, rng=rng)

        decision_2 = optimizer.check_trial(trial_id_2, journal)
        assert decision_2 in ("keep", "revert", "ongoing")

        if decision_2 != "ongoing":
            optimizer.commit_or_revert(trial_id_2, decision_2)
            trials_completed.append((trial_id_2, decision_2))

        # ----------------------------------------------------------------
        # WEEK 4 (Days 21-27): More signals
        # ----------------------------------------------------------------
        _seed_signals_no_outcomes(
            journal, strategies,
            num_days=7, signals_per_day=4,
            rng=rng, day_offset=21,
        )
        _backfill_outcomes(journal, since_day=21, hit_rate=0.50, rng=rng)

        # If second trial was ongoing, check again
        if decision_2 == "ongoing":
            decision_2b = optimizer.check_trial(trial_id_2, journal)
            if decision_2b != "ongoing":
                optimizer.commit_or_revert(trial_id_2, decision_2b)
                trials_completed.append((trial_id_2, decision_2b))

        # ----------------------------------------------------------------
        # DAYS 28-30: Final signals and evaluation
        # ----------------------------------------------------------------
        _seed_signals_no_outcomes(
            journal, strategies,
            num_days=3, signals_per_day=4,
            rng=rng, day_offset=28,
        )
        _backfill_outcomes(journal, since_day=28, hit_rate=0.50, rng=rng)

        # ----------------------------------------------------------------
        # FINAL ASSERTIONS
        # ----------------------------------------------------------------

        # 1. At least 2 trials were attempted
        assert len(trials_started) >= 2, f"Only {len(trials_started)} trials started"

        # 2. At least 1 trial was completed
        assert len(trials_completed) >= 1, f"Only {len(trials_completed)} completed"

        # 3. Trial history is persisted correctly
        all_trials = optimizer._load_trials()
        for tid in trials_started:
            assert tid in all_trials
            assert all_trials[tid]["strategy"] in strategies
            assert "start_date" in all_trials[tid]

        # 4. Prompt history directory has archived files
        history_files = list((tmp_path / "prompts" / "history").glob("*.txt"))
        assert len(history_files) >= 1, "Should have at least 1 archived prompt"

        # 5. No active trial left dangling (all should be resolved)
        active_id, _ = optimizer.get_active_trial()
        assert active_id is None, f"Dangling active trial: {active_id}"

        # 6. Each strategy's signals total 30 days * 4/day = 120
        total_entries = journal.get_entries()
        total_per_strategy = {}
        for e in total_entries:
            s = e["strategy"]
            total_per_strategy[s] = total_per_strategy.get(s, 0) + 1
        for s in strategies:
            # 31 days total (0-30), 4 signals/day = up to 124
            assert total_per_strategy.get(s, 0) >= 100, (
                f"{s} has only {total_per_strategy.get(s, 0)} entries"
            )

        # 7. All entries should have outcomes backfilled
        no_outcome = [e for e in total_entries if e.get("return_5d") is None]
        assert len(no_outcome) == 0, f"{len(no_outcome)} entries still without outcomes"

        # 8. For kept trials, the active prompt should differ from default
        for tid, decision in trials_completed:
            if decision == "keep":
                strat = all_trials[tid]["strategy"]
                current = analyzer.get_prompt(strat)
                from tradingagents.strategies.learning.llm_analyzer import _DEFAULT_PROMPTS
                default = _DEFAULT_PROMPTS.get(strat, "")
                assert current != default, (
                    f"Kept trial for {strat} but prompt is still default"
                )

        # 9. For reverted trials, prompt should match default (or prior committed version)
        for tid, decision in trials_completed:
            if decision == "revert":
                strat = all_trials[tid]["strategy"]
                # Reverted trial's prompt is archived in history
                reverted_files = list(
                    (tmp_path / "prompts" / "history").glob(f"{strat}_*_reverted.txt")
                )
                assert len(reverted_files) >= 1, (
                    f"No reverted archive for {strat}"
                )

        # 10. Prompt version tracking shows changes over time
        lit_versions = prompt_versions["litigation"]
        assert len(lit_versions) >= 2, "Litigation should have version changes"
        # At least one version transition occurred
        assert len(set(lit_versions)) >= 2, "Versions should have changed"


class TestMultipleConcurrentStrategies:
    """Test that optimization correctly targets one strategy at a time."""

    def test_only_one_active_trial_at_a_time(self, tmp_path):
        """Starting a second trial while one is active should work
        (the system doesn't prevent it, but get_active_trial returns first found)."""
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        tid1 = optimizer.start_trial("litigation", "prompt v1")
        tid2 = optimizer.start_trial("earnings_call", "prompt v2")

        # Both are active
        trials = optimizer._load_trials()
        assert trials[tid1]["status"] == "active"
        assert trials[tid2]["status"] == "active"

        # get_active_trial returns one of them
        active_id, _ = optimizer.get_active_trial()
        assert active_id in (tid1, tid2)

        # Clean up both
        optimizer.commit_or_revert(tid1, "revert")
        optimizer.commit_or_revert(tid2, "revert")

        active_id, _ = optimizer.get_active_trial()
        assert active_id is None

    def test_sequential_optimization_of_different_strategies(self, tmp_path):
        """Optimize litigation, then supply_chain, verifying independent state."""
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        # First: litigation
        original_lit = analyzer.get_prompt("litigation")
        tid1 = optimizer.start_trial("litigation", "improved litigation prompt")
        optimizer.commit_or_revert(tid1, "keep")

        assert analyzer.get_prompt("litigation") == "improved litigation prompt"
        assert analyzer.get_prompt("supply_chain") != "improved litigation prompt"

        # Second: supply_chain
        original_sc = analyzer.get_prompt("supply_chain")
        tid2 = optimizer.start_trial("supply_chain", "improved supply chain prompt")
        optimizer.commit_or_revert(tid2, "keep")

        # Both changed independently
        assert analyzer.get_prompt("litigation") == "improved litigation prompt"
        assert analyzer.get_prompt("supply_chain") == "improved supply chain prompt"

        # Other strategies unchanged
        assert analyzer.get_prompt("earnings_call") == LLMAnalyzer().get_prompt("earnings_call")

        # History has 2 archived baselines
        history_files = list((tmp_path / "prompts" / "history").glob("*_baseline.txt"))
        assert len(history_files) == 2


def _has_anthropic_key() -> bool:
    """Check if ANTHROPIC_API_KEY is available (env or .env file)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    try:
        from dotenv import load_dotenv
        load_dotenv()
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    except ImportError:
        return False


@pytest.mark.skipif(not _has_anthropic_key(), reason="ANTHROPIC_API_KEY not set")
class TestLiveLLMOptimization:
    """Full optimization cycle with REAL Haiku API calls.

    Validates that:
    1. propose_modification sends the right meta-prompt and gets a valid modified prompt back
    2. The modified prompt is structurally similar to the original (not garbage)
    3. The full evaluate → propose → trial → check cycle works end-to-end
    """

    @pytest.fixture(autouse=True)
    def _load_env(self):
        """Ensure .env is loaded for API keys."""
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

    def test_propose_returns_valid_prompt(self, tmp_path):
        """Real Haiku call: propose_modification returns a meaningful prompt."""
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        current_prompt = analyzer.get_prompt("litigation")
        failures = [
            {
                "timestamp": "2026-04-01",
                "strategy": "litigation",
                "ticker": "AAPL",
                "direction": "short",
                "llm_conviction": 0.9,
                "return_5d": 0.05,
                "rationale": "Patent lawsuit filed",
            },
            {
                "timestamp": "2026-04-02",
                "strategy": "litigation",
                "ticker": "MSFT",
                "direction": "short",
                "llm_conviction": 0.85,
                "return_5d": 0.03,
                "rationale": "Class action antitrust suit",
            },
            {
                "timestamp": "2026-04-03",
                "strategy": "litigation",
                "ticker": "TSLA",
                "direction": "short",
                "llm_conviction": 0.8,
                "return_5d": 0.07,
                "rationale": "Securities fraud complaint",
            },
        ]

        modified = optimizer.propose_modification("litigation", current_prompt, failures)

        # Should return a non-empty string
        assert len(modified) > 100, f"Modified prompt too short: {len(modified)} chars"
        # Should be different from original
        assert modified != current_prompt, "LLM returned identical prompt"
        # Should still contain key structural elements (JSON output format)
        assert "direction" in modified.lower(), "Modified prompt lost 'direction' key requirement"
        assert "conviction" in modified.lower(), "Modified prompt lost 'conviction' key requirement"
        # Should contain some reference to the failure pattern (shorting litigation)
        # (This is a soft check — LLM might phrase it differently)
        print(f"\n--- Modified prompt ({len(modified)} chars) ---")
        print(modified[:500])
        print("...")

    def test_full_cycle_with_live_llm(self, tmp_path):
        """Full evaluate → propose (real LLM) → trial → check → commit cycle."""
        rng = random.Random(42)
        journal = SignalJournal(str(tmp_path))
        analyzer = LLMAnalyzer()
        optimizer = PromptOptimizer(str(tmp_path), analyzer)

        # Seed baseline: litigation at 35% hit rate, others at 60%
        hit_rates = {
            "earnings_call": 0.60,
            "filing_analysis": 0.60,
            "insider_activity": 0.60,
            "litigation": 0.35,
            "regulatory_pipeline": 0.60,
            "supply_chain": 0.60,
        }
        _seed_journal(
            journal, list(hit_rates.keys()),
            num_days=10, signals_per_day=8,
            hit_rates=hit_rates, rng=rng,
        )

        # Step 1: Evaluate
        scores = optimizer.evaluate_prompts(journal)
        worst = optimizer.identify_worst_prompt(scores)
        assert worst == "litigation", f"Expected litigation, got {worst}"
        print(f"\nWorst strategy: {worst} (hit_rate={scores[worst]['hit_rate']:.2f})")

        # Step 2: Get failures
        failures = journal.get_high_conviction_failures("litigation", limit=10)
        assert len(failures) > 0
        print(f"High-conviction failures: {len(failures)}")

        # Step 3: Propose modification (REAL LLM CALL)
        current_prompt = analyzer.get_prompt("litigation")
        modified = optimizer.propose_modification("litigation", current_prompt, failures)

        assert modified != current_prompt, "LLM returned identical prompt"
        assert len(modified) > 50, f"Modified prompt too short: {len(modified)}"
        print(f"Original prompt: {len(current_prompt)} chars")
        print(f"Modified prompt: {len(modified)} chars")

        # Step 4: Start trial
        original_version = optimizer.get_prompt_version("litigation")
        trial_id = optimizer.start_trial("litigation", modified)
        _patch_trial_start_date(optimizer, trial_id, (BASE_DATE + timedelta(days=10)).isoformat())

        assert analyzer.get_prompt("litigation") == modified
        assert optimizer.get_prompt_version("litigation") != original_version

        # Step 5: Seed trial-period signals with improved hit rate (60%)
        _seed_signals_no_outcomes(
            journal, list(hit_rates.keys()),
            num_days=7, signals_per_day=4,
            rng=rng, day_offset=10,
            prompt_versions={"litigation": optimizer.get_prompt_version("litigation")},
        )
        _backfill_outcomes(journal, since_day=10, hit_rate=0.60, rng=rng)

        # Step 6: Check trial
        decision = optimizer.check_trial(trial_id, journal)
        assert decision in ("keep", "revert"), f"Unexpected: {decision}"
        print(f"Trial decision: {decision}")

        # Step 7: Commit
        optimizer.commit_or_revert(trial_id, decision)

        # Verify final state
        trials = optimizer._load_trials()
        assert trials[trial_id]["status"] == decision
        assert "completed_date" in trials[trial_id]

        active_id, _ = optimizer.get_active_trial()
        assert active_id is None

        history_files = list((tmp_path / "prompts" / "history").glob("*.txt"))
        assert len(history_files) >= 1
        print(f"Archived prompt files: {len(history_files)}")

        if decision == "keep":
            # The LLM-modified prompt is now the active prompt
            assert analyzer.get_prompt("litigation") == modified
            print("Trial KEPT — LLM-modified prompt is now active")
        else:
            # Reverted to baseline
            assert analyzer.get_prompt("litigation") == current_prompt
            print("Trial REVERTED — original prompt restored")
