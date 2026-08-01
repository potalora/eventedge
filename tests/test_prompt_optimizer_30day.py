"""Metrics-v2 coverage for the PromptOptimizer lifecycle.

These tests deliberately use persisted-style ``OutcomeRecord`` rows rather
than the retired SignalJournal API.  The analyzer is a local fake: proposal,
trial, persistence, and version coverage must never make a provider call.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tradingagents.strategies.learning.prompt_optimizer import (
    LLM_STRATEGIES,
    MIN_SIGNALS_FOR_EVAL,
    PromptOptimizer,
)
from tradingagents.strategies.metrics.models import OutcomeRecord


class FakeAnalyzer:
    """Minimal prompt store that prevents tests from reaching an LLM provider."""

    def __init__(self) -> None:
        self.prompts = {
            strategy: f"default {strategy} prompt" for strategy in LLM_STRATEGIES
        }
        self.calls: list[tuple[str, str, int]] = []
        self.llm_result = ""

    def get_prompt(self, strategy: str) -> str:
        return self.prompts.get(strategy, "")

    def set_prompt_override(self, strategy: str, prompt: str) -> None:
        self.prompts[strategy] = prompt or f"default {strategy} prompt"

    def _call_llm(self, system: str, user: str, max_tokens: int = 4096) -> str:
        self.calls.append((system, user, max_tokens))
        return self.llm_result


def _outcome(
    index: int,
    *,
    strategy: str = "litigation",
    epoch_id: str = "epoch-1",
    entry_session: date = date(2026, 4, 1),
    hit: bool = True,
) -> OutcomeRecord:
    signed = Decimal("0.01" if hit else "-0.01")
    return OutcomeRecord(
        outcome_id=f"outcome-{index}",
        signal_id=f"signal-{index}",
        event_key=f"event-{index}",
        epoch_id=epoch_id,
        strategy=strategy,
        policy_id="policy-1",
        ticker="AAPL",
        direction="long",
        holding_sessions=5,
        entry_session=entry_session,
        exit_session=date(2026, 4, 30),
        entry_price=Decimal("100"),
        exit_price=Decimal("101" if hit else "99"),
        raw_return=signed,
        signed_return=signed,
        status="valid",
        invalid_reason="",
    )


def _set_trial_start(optimizer: PromptOptimizer, trial_id: str, start: date) -> None:
    trials = optimizer._load_trials()
    trials[trial_id]["start_date"] = start.isoformat()
    optimizer._save_trials(trials)


@pytest.fixture
def optimizer(tmp_path):
    return PromptOptimizer(str(tmp_path), FakeAnalyzer())


def test_evaluate_uses_directional_accuracy_and_honest_unavailable_fields(
    optimizer, monkeypatch
) -> None:
    calls = []

    def governed(rows):
        materialized = tuple(rows)
        calls.append(materialized)
        from tradingagents.strategies.metrics.outcomes import directional_accuracy

        return directional_accuracy(materialized)

    monkeypatch.setattr(
        "tradingagents.strategies.learning.prompt_optimizer.directional_accuracy",
        governed,
    )
    rows = tuple(_outcome(index, hit=index < 15) for index in range(20))
    score = optimizer.evaluate_prompts({"litigation": (row for row in rows)})[
        "litigation"
    ]

    assert score == {
        "hit_rate": pytest.approx(0.75),
        "avg_return": None,
        "calibration": None,
        "n_signals": 20,
        "high_conviction_hits": None,
        "high_conviction_total": None,
    }
    assert calls.count(rows) == 1


def test_evaluation_identifies_worst_eligible_strategy(optimizer) -> None:
    scores = optimizer.evaluate_prompts(
        {
            "litigation": tuple(
                _outcome(i, hit=i < 7) for i in range(MIN_SIGNALS_FOR_EVAL)
            ),
            "earnings_call": tuple(
                _outcome(i, strategy="earnings_call", hit=i < 15)
                for i in range(MIN_SIGNALS_FOR_EVAL)
            ),
        }
    )
    assert optimizer.identify_worst_prompt(scores) == "litigation"


def test_evaluate_insufficient_data_is_not_a_false_zero(optimizer) -> None:
    score = optimizer.evaluate_prompts({})["litigation"]
    assert score["n_signals"] == 0
    assert score["hit_rate"] is None
    assert score["avg_return"] is None
    assert score["high_conviction_total"] is None
    assert optimizer.identify_worst_prompt(optimizer.evaluate_prompts({})) is None


def test_evaluate_rejects_strategy_and_epoch_mismatch(optimizer) -> None:
    with pytest.raises(ValueError, match="strategy"):
        optimizer.evaluate_prompts(
            {"litigation": [_outcome(1, strategy="supply_chain")]}
        )
    with pytest.raises(ValueError, match="mix metric epochs"):
        optimizer.evaluate_prompts(
            {"litigation": [_outcome(1), _outcome(2, epoch_id="epoch-2")]}
        )


def test_start_trial_creates_baseline_and_activates_override(
    optimizer, tmp_path
) -> None:
    analyzer = optimizer._analyzer
    original = analyzer.get_prompt("litigation")
    trial_id = optimizer.start_trial("litigation", "improved prompt")

    assert (
        tmp_path / "prompts" / "litigation_trial.txt"
    ).read_text() == "improved prompt"
    assert (tmp_path / "prompts" / "litigation_baseline.txt").read_text() == original
    assert optimizer._load_trials()[trial_id]["status"] == "active"
    assert analyzer.get_prompt("litigation") == "improved prompt"
    assert optimizer.get_active_trial()[0] == trial_id


def test_trial_split_uses_exact_entry_session_and_directional_accuracy(
    optimizer,
) -> None:
    trial_id = optimizer.start_trial("litigation", "trial")
    _set_trial_start(optimizer, trial_id, date(2026, 4, 10))
    baseline = tuple(
        _outcome(i, entry_session=date(2026, 4, 9), hit=i < 3) for i in range(10)
    )
    trial = tuple(
        _outcome(i + 10, entry_session=date(2026, 4, 10), hit=i < 4) for i in range(5)
    )
    assert optimizer.check_trial(trial_id, baseline + trial) == "keep"


def test_trial_reverts_when_not_improved(optimizer) -> None:
    trial_id = optimizer.start_trial("litigation", "trial")
    _set_trial_start(optimizer, trial_id, date(2026, 4, 10))
    baseline = tuple(
        _outcome(i, entry_session=date(2026, 4, 9), hit=i < 8) for i in range(10)
    )
    trial = tuple(
        _outcome(i + 10, entry_session=date(2026, 4, 10), hit=i < 2) for i in range(5)
    )
    assert optimizer.check_trial(trial_id, baseline + trial) == "revert"


def test_trial_with_too_few_exact_v2_outcomes_is_ongoing(optimizer) -> None:
    trial_id = optimizer.start_trial("litigation", "trial")
    assert optimizer.check_trial(trial_id, (_outcome(1), _outcome(2))) == "ongoing"


def test_commit_keeps_trial_prompt_and_archives_baseline(optimizer, tmp_path) -> None:
    analyzer = optimizer._analyzer
    original = analyzer.get_prompt("litigation")
    trial_id = optimizer.start_trial("litigation", "improved prompt")
    optimizer.commit_or_revert(trial_id, "keep")

    assert analyzer.get_prompt("litigation") == "improved prompt"
    assert (tmp_path / "prompts" / "litigation.txt").read_text() == "improved prompt"
    assert not (tmp_path / "prompts" / "litigation_trial.txt").exists()
    assert (
        list((tmp_path / "prompts" / "history").glob("litigation_*_baseline.txt"))[
            0
        ].read_text()
        == original
    )
    assert optimizer._load_trials()[trial_id]["status"] == "keep"


def test_revert_restores_baseline_and_archives_trial(optimizer, tmp_path) -> None:
    analyzer = optimizer._analyzer
    original = analyzer.get_prompt("litigation")
    trial_id = optimizer.start_trial("litigation", "bad prompt")
    optimizer.commit_or_revert(trial_id, "revert")

    assert analyzer.get_prompt("litigation") == original
    assert (
        list((tmp_path / "prompts" / "history").glob("litigation_*_reverted.txt"))[
            0
        ].read_text()
        == "bad prompt"
    )
    assert optimizer._load_trials()[trial_id]["status"] == "revert"


def test_proposal_uses_local_analyzer_and_returns_modified_prompt(optimizer) -> None:
    analyzer = optimizer._analyzer
    analyzer.llm_result = "modified prompt"
    result = optimizer.propose_modification(
        "litigation", "current prompt", [{"ticker": "AAPL"}]
    )
    assert result == "modified prompt"
    assert analyzer.calls and "litigation" in analyzer.calls[0][1]


def test_proposal_preserves_current_prompt_on_empty_response(optimizer) -> None:
    assert (
        optimizer.propose_modification("litigation", "current prompt", [])
        == "current prompt"
    )


def test_proposal_strips_markdown_fences(optimizer) -> None:
    optimizer._analyzer.llm_result = "```\nclean prompt\n```"
    assert optimizer.propose_modification("litigation", "current", []) == "clean prompt"


def test_prompt_version_changes_on_keep_and_reverts_on_revert(optimizer) -> None:
    initial = optimizer.get_prompt_version("litigation")
    keep = optimizer.start_trial("litigation", "kept prompt")
    trial_version = optimizer.get_prompt_version("litigation")
    optimizer.commit_or_revert(keep, "keep")
    assert trial_version != initial
    assert optimizer.get_prompt_version("litigation") == trial_version

    revert = optimizer.start_trial("litigation", "discarded prompt")
    optimizer.commit_or_revert(revert, "revert")
    assert optimizer.get_prompt_version("litigation") == trial_version


def test_concurrent_trials_are_persisted_and_can_be_independently_resolved(
    optimizer,
) -> None:
    first = optimizer.start_trial("litigation", "litigation trial")
    second = optimizer.start_trial("earnings_call", "earnings trial")
    assert {
        optimizer._load_trials()[first]["status"],
        optimizer._load_trials()[second]["status"],
    } == {"active"}
    assert optimizer.get_active_trial()[0] in {first, second}

    optimizer.commit_or_revert(first, "keep")
    optimizer.commit_or_revert(second, "revert")
    assert optimizer.get_active_trial() == (None, None)
    assert optimizer._analyzer.get_prompt("litigation") == "litigation trial"
    assert (
        optimizer._analyzer.get_prompt("earnings_call")
        == "default earnings_call prompt"
    )
