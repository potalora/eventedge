"""Production learning is fail-closed and cannot touch state."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tradingagents.strategies.orchestration.learning_policy import LearningPolicy


def test_learning_policy_rejects_enabled_mode() -> None:
    with pytest.raises(ValueError, match="production learning is disabled"):
        LearningPolicy(mode="enabled")  # type: ignore[arg-type]


def test_cohort_config_defaults_to_disabled_learning_policy() -> None:
    from tradingagents.strategies.orchestration.cohort_orchestrator import CohortConfig

    config = CohortConfig("cohort", "state", "30d", "5k")

    assert config.learning_policy == LearningPolicy()


def test_run_cohorts_learning_refuses_before_state_write(tmp_path, monkeypatch) -> None:
    from scripts import run_cohorts

    monkeypatch.setenv("AUTORESEARCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["run_cohorts.py", "--learning"])

    with pytest.raises(SystemExit) as exc:
        run_cohorts.main()

    assert exc.value.code == 2
    assert list(tmp_path.iterdir()) == []


def test_run_generations_learning_refuses_before_manager_creation(monkeypatch) -> None:
    from scripts import run_generations

    monkeypatch.setattr("sys.argv", ["run_generations.py", "run-learning"])

    with pytest.raises(SystemExit) as exc:
        run_generations.main()

    assert exc.value.code == 2


def test_generation_manager_learning_refuses_without_subprocess(
    tmp_path, monkeypatch
) -> None:
    from tradingagents.strategies.orchestration.generation_manager import (
        GenerationManager,
    )

    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    manager = GenerationManager(tmp_path)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    monkeypatch.setattr(
        manager,
        "_run_cohorts_subprocess",
        lambda *_args: pytest.fail("learning must not start a subprocess"),
    )

    with pytest.raises(RuntimeError, match="production learning is disabled"):
        manager.run_learning()

    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_engine_rejects_adaptive_confidence_before_state_creation(tmp_path) -> None:
    from tradingagents.strategies.orchestration.multi_strategy_engine import (
        MultiStrategyEngine,
    )

    with pytest.raises(ValueError, match="production learning is disabled"):
        MultiStrategyEngine(
            config={"autoresearch": {"state_dir": str(tmp_path)}},
            adaptive_confidence=True,
        )

    assert list(tmp_path.iterdir()) == []


def test_engine_learning_loop_refuses_without_state_mutation(tmp_path) -> None:
    from tradingagents.strategies.orchestration.multi_strategy_engine import (
        MultiStrategyEngine,
    )

    engine = MultiStrategyEngine(
        config={"autoresearch": {"state_dir": str(tmp_path)}}, strategies=[]
    )
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    with pytest.raises(RuntimeError, match="production learning is disabled"):
        engine.run_learning_loop()

    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_subclassed_learning_policy_is_rejected_before_state_creation(tmp_path) -> None:
    from tradingagents.strategies.orchestration.cohort_orchestrator import (
        CohortConfig,
        CohortOrchestrator,
    )

    class BypassPolicy(LearningPolicy):
        pass

    config = CohortConfig(
        "cohort",
        str(tmp_path / "cohort"),
        "30d",
        "5k",
        use_llm=False,
        learning_policy=BypassPolicy(),
    )
    with pytest.raises(ValueError, match="exactly LearningPolicy"):
        CohortOrchestrator(
            [config],
            {
                "execution": {"mode": "paper"},
                "autoresearch": {"state_dir": str(tmp_path)},
            },
            generation_id="gen_test",
            generation_commit="commit",
        )

    assert list(tmp_path.iterdir()) == []


def test_cohort_orchestrator_has_no_production_learning_path() -> None:
    from tradingagents.strategies.orchestration.cohort_orchestrator import (
        CohortOrchestrator,
    )

    assert not hasattr(CohortOrchestrator, "run_learning")


def test_metrics_package_has_no_learning_import() -> None:
    for path in Path("tradingagents/strategies/metrics").glob("*.py"):
        assert "strategies.learning" not in path.read_text()
