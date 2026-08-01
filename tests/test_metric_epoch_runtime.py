from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tradingagents.strategies.execution.contracts import (
    COST_MODEL_VERSION,
    EXECUTION_CLOCK_VERSION,
    POLICY_DOCUMENT_VERSION,
    PRICING_VERSION,
)
from tradingagents.strategies.metrics.store import MetricStore
from tradingagents.strategies.orchestration import metric_epoch_context as context_module
from tradingagents.strategies.orchestration.metric_epoch_context import (
    CohortSemanticPolicy,
    build_epoch_context,
)
from tradingagents.strategies.orchestration.session_executor import SessionExecutor
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    CohortConfig,
    CohortOrchestrator,
)
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_COHORTS = REPO_ROOT / "scripts" / "run_cohorts.py"


def _execution_policy(**changes: object) -> dict[str, object]:
    policy: dict[str, object] = {
        "policy_document_version": POLICY_DOCUMENT_VERSION,
        "execution_clock_contract": EXECUTION_CLOCK_VERSION,
        "pricing_contract": PRICING_VERSION,
        "cost_model_contract": COST_MODEL_VERSION,
        "risk_gate": {"max_positions": 5},
        "cost_model": {"commission_per_share": "0.005"},
    }
    policy.update(changes)
    return policy


def _policy(
    name: str = "cohort_a",
    *,
    horizon: str = "30d",
    size_profile: str = "5k",
    policy_id: str = "foundation-30d",
    use_llm: bool = False,
    learning_enabled: bool = False,
    execution_policy: dict[str, object] | None = None,
) -> CohortSemanticPolicy:
    return CohortSemanticPolicy(
        name=name,
        horizon=horizon,
        size_profile=size_profile,
        policy_id=policy_id,
        use_llm=use_llm,
        learning_enabled=learning_enabled,
        execution_policy=execution_policy or _execution_policy(),
    )


def _context(**changes: object):
    values = {
        "generation_id": "gen_004",
        "generation_commit": "abc123",
        "models": {"llm_provider": "anthropic", "autoresearch_model": "sonnet"},
        "strategies": ("filing_analysis", "litigation"),
        "cohort_policies": (_policy(),),
    }
    values.update(changes)
    return build_epoch_context(**values)


def _executor(tmp_path: Path, name: str, config_changes: dict | None = None):
    state_dir = tmp_path / name
    config = {
        "execution": {"mode": "paper"},
        "autoresearch": {
            "state_dir": str(state_dir),
            "paper_ledger": {"benchmark_symbols": ["SPY", "BIL"]},
        },
    }
    if config_changes:
        config["autoresearch"].update(config_changes)
    ledger = PortfolioLedger(state_dir / "portfolio.db", name, Decimal("5000"))
    return SessionExecutor(ledger, config), ledger


def test_contract_versions_are_centralized_exact_values() -> None:
    assert POLICY_DOCUMENT_VERSION == "execution-policy-v2"
    assert EXECUTION_CLOCK_VERSION == "exact-next-xnys-open-v1"
    assert PRICING_VERSION == "raw-unadjusted-daily-ohlc-v1"
    assert COST_MODEL_VERSION == "adverse-equity-fill-v1"


def test_epoch_context_is_stable_across_order_and_state_paths(tmp_path) -> None:
    first_executor, first_ledger = _executor(tmp_path, "one")
    second_executor, second_ledger = _executor(tmp_path, "two")
    try:
        first = build_epoch_context(
            generation_id="gen_004",
            generation_commit="abc123",
            models={"llm_provider": "anthropic", "autoresearch_model": "sonnet"},
            strategies=("filing_analysis", "litigation"),
            cohort_policies=(
                _policy("b", execution_policy=second_executor.semantic_policy_document()),
                _policy("a", execution_policy=first_executor.semantic_policy_document()),
            ),
        )
        second = build_epoch_context(
            generation_id="gen_004",
            generation_commit="abc123",
            models={"autoresearch_model": "sonnet", "llm_provider": "anthropic"},
            strategies=("litigation", "filing_analysis"),
            cohort_policies=(
                _policy("a", execution_policy=first_executor.semantic_policy_document()),
                _policy("b", execution_policy=second_executor.semantic_policy_document()),
            ),
        )
        assert first == second
        serialized = json.dumps(asdict(first), sort_keys=True)
        assert str(tmp_path) not in serialized
    finally:
        first_ledger.close()
        second_ledger.close()


@pytest.mark.parametrize(
    "change",
    (
        "generation_commit",
        "model",
        "active_strategy",
        "cohort_use_llm",
        "cohort_horizon",
        "size_profile",
        "policy_id",
        "learning_flag",
        "risk_gate",
        "cost_parameter",
    ),
)
def test_every_allowlisted_semantic_change_rotates_context_hash(change: str) -> None:
    if change == "generation_commit":
        changed = _context(generation_commit="def456")
    elif change == "model":
        changed = _context(
            models={"llm_provider": "anthropic", "autoresearch_model": "opus"}
        )
    elif change == "active_strategy":
        changed = _context(
            strategies=("filing_analysis", "litigation", "supply_chain")
        )
    elif change == "cohort_use_llm":
        changed = _context(cohort_policies=(_policy(use_llm=True),))
    elif change == "cohort_horizon":
        changed = _context(cohort_policies=(_policy(horizon="3m"),))
    elif change == "size_profile":
        changed = _context(cohort_policies=(_policy(size_profile="10k"),))
    elif change == "policy_id":
        changed = _context(cohort_policies=(_policy(policy_id="policy-v2"),))
    elif change == "learning_flag":
        changed = _context(cohort_policies=(_policy(learning_enabled=True),))
    elif change == "risk_gate":
        changed = _context(
            cohort_policies=(
                _policy(execution_policy=_execution_policy(risk_gate={"max_positions": 6})),
            )
        )
    else:
        changed = _context(
            cohort_policies=(
                _policy(
                    execution_policy=_execution_policy(
                        cost_model={"commission_per_share": "0.006"}
                    )
                ),
            )
        )
    assert changed != _context()


@pytest.mark.parametrize(
    ("constant", "changed"),
    (
        ("EXECUTION_CLOCK_VERSION", "clock-v2"),
        ("PRICING_VERSION", "pricing-v2"),
        ("COST_MODEL_VERSION", "cost-v2"),
    ),
)
def test_every_contract_change_rotates_context_hash(
    monkeypatch, constant: str, changed: str
) -> None:
    baseline = _context()
    monkeypatch.setattr(context_module, constant, changed)
    assert _context() != baseline


@pytest.mark.parametrize(
    "runtime_changes",
    (
        {"fmp_api_key": "secret-one", "courtlistener_token": "secret-two"},
        {"state_dir": "__alternate_state_path__"},
        {"live_borrow_rates": {"AAPL": "0.99"}},
        {"positions": [{"ticker": "AAPL", "quantity": 50}]},
        {"prices": {"AAPL": "999.99"}},
        {"session_timestamp": "2026-08-03T21:00:00Z"},
    ),
)
def test_session_varying_and_secret_values_do_not_enter_policy(
    tmp_path, runtime_changes: dict
) -> None:
    runtime_changes = dict(runtime_changes)
    if runtime_changes.get("state_dir") == "__alternate_state_path__":
        runtime_changes["state_dir"] = str(tmp_path / "alternate-state")
    baseline, baseline_ledger = _executor(tmp_path, "baseline")
    changed, changed_ledger = _executor(tmp_path, "changed", runtime_changes)
    try:
        assert changed.semantic_policy_document() == baseline.semantic_policy_document()
        serialized = json.dumps(changed.semantic_policy_document(), sort_keys=True)
        for value in runtime_changes.values():
            if isinstance(value, str):
                assert value not in serialized
    finally:
        baseline_ledger.close()
        changed_ledger.close()


def _changed_policy_leaves(value: object):
    if isinstance(value, dict):
        for key in sorted(value):
            for changed in _changed_policy_leaves(value[key]):
                copy = dict(value)
                copy[key] = changed
                yield copy
        return
    if isinstance(value, list):
        yield [*value, "semantic-change"]
    elif isinstance(value, bool):
        yield not value
    elif isinstance(value, int):
        yield value + 1
    elif isinstance(value, str):
        yield value + "-changed"
    elif value is None:
        yield "changed"
    else:  # pragma: no cover - semantic policy validation owns this invariant.
        raise AssertionError(f"unexpected policy leaf {type(value).__name__}")


def test_every_effective_executor_policy_leaf_rotates_config_hash(tmp_path) -> None:
    executor, ledger = _executor(tmp_path, "cohort")
    try:
        policy = executor.semantic_policy_document()
        baseline = _context(cohort_policies=(_policy(execution_policy=policy),))
        changed_contexts = [
            _context(cohort_policies=(_policy(execution_policy=changed),))
            for changed in _changed_policy_leaves(policy)
        ]
        assert changed_contexts
        assert all(row.config_hash != baseline.config_hash for row in changed_contexts)
    finally:
        ledger.close()


@pytest.mark.parametrize("field", ("generation_id", "generation_commit"))
@pytest.mark.parametrize("value", ("", "   ", None, 7))
def test_generation_identity_must_be_nonempty_text(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        _context(**{field: value})


def test_model_values_must_be_text_or_none() -> None:
    with pytest.raises(ValueError, match="model autoresearch_model"):
        _context(models={"autoresearch_model": 7})


@pytest.mark.parametrize(
    "bad_value",
    (1.25, Decimal("1.25"), date(2026, 8, 3), Path("/tmp/x"), {"x"}, object()),
)
def test_execution_policy_rejects_noncanonical_values(bad_value: object) -> None:
    with pytest.raises((TypeError, ValueError), match="execution_policy"):
        _context(
            cohort_policies=(
                _policy(execution_policy={"policy_document_version": bad_value}),
            )
        )


def test_duplicate_strategies_and_cohort_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate strategy"):
        _context(strategies=("litigation", "litigation"))
    with pytest.raises(ValueError, match="duplicate cohort"):
        _context(cohort_policies=(_policy("same"), _policy("same")))


def test_session_executor_registers_and_reuses_metric_epoch(tmp_path) -> None:
    executor, ledger = _executor(tmp_path, "cohort")
    try:
        context = _context()
        first = executor.ensure_metric_epoch(context, date(2026, 8, 3))
        repeated = executor.ensure_metric_epoch(context, date(2026, 8, 4))
        assert first == repeated == executor.metric_store.current_epoch()
    finally:
        ledger.close()


def test_invalidated_session_replay_does_not_open_replacement_epoch(tmp_path) -> None:
    executor, ledger = _executor(tmp_path, "cohort")
    try:
        context = _context()
        opened = executor.ensure_metric_epoch(context, date(2026, 8, 3))
        invalid = executor.invalidate_metric_epoch(
            date(2026, 8, 4), "critical_market_data_gap"
        )
        repeated = executor.ensure_metric_epoch(context, date(2026, 8, 4))
        assert repeated == invalid
        assert executor.metric_store.current_epoch() == invalid
        assert executor.metric_store.load_epoch(opened.epoch_id) == invalid
        with pytest.raises(ValueError, match="invalidated session context conflict"):
            executor.ensure_metric_epoch(
                replace(context, config_hash="changed"), date(2026, 8, 4)
            )
    finally:
        ledger.close()


def test_run_cohorts_requires_generation_metadata_before_state_write(tmp_path) -> None:
    env = os.environ.copy()
    env.pop("EVENTEDGE_GENERATION_ID", None)
    env.pop("EVENTEDGE_GENERATION_COMMIT", None)
    state = tmp_path / "state"
    env["AUTORESEARCH_STATE_DIR"] = str(state)
    result = subprocess.run(
        [sys.executable, str(RUN_COHORTS), "--date", "2026-08-03", "--no-llm"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 2
    assert (
        "EVENTEDGE_GENERATION_ID and EVENTEDGE_GENERATION_COMMIT are required"
        in combined
    )
    assert not state.exists()


def test_metric_store_is_shared_for_executor_epoch_manager(tmp_path) -> None:
    path = tmp_path / "metrics_v2.sqlite3"
    store = MetricStore(path)
    ledger = PortfolioLedger(tmp_path / "portfolio.db", "cohort", Decimal("5000"))
    try:
        executor = SessionExecutor(
            ledger,
            {"execution": {"mode": "paper"}, "autoresearch": {}},
            metric_store=store,
        )
        epoch = executor.ensure_metric_epoch(_context(), date(2026, 8, 3))
        assert executor.metric_store is store
        assert store.current_epoch() == epoch
    finally:
        ledger.close()


def test_malformed_strategy_context_fails_before_any_state_creation(tmp_path) -> None:
    state = tmp_path / "state"
    cohorts = [
        CohortConfig("cohort", str(state / "cohort"), "30d", "5k", False)
    ]
    duplicate = SimpleNamespace(name="duplicate")
    with patch(
        "tradingagents.strategies.modules.get_paper_trade_strategies",
        return_value=[duplicate, duplicate],
    ):
        with pytest.raises(ValueError, match="duplicate strategy"):
            CohortOrchestrator(
                cohorts,
                {"execution": {"mode": "paper"}, "autoresearch": {"state_dir": str(state)}},
                generation_id="gen_test",
                generation_commit="test-commit",
            )
    assert not state.exists()


def test_nontext_policy_id_fails_before_any_state_creation(tmp_path) -> None:
    state = tmp_path / "state"
    cohorts = [
        CohortConfig("cohort", str(state / "cohort"), "30d", "5k", False)
    ]
    with pytest.raises(ValueError, match="policy_id"):
        CohortOrchestrator(
            cohorts,
            {
                "execution": {"mode": "paper"},
                "autoresearch": {
                    "state_dir": str(state),
                    "paper_ledger": {"policy_id": 7},
                },
            },
            generation_id="gen_test",
            generation_commit="test-commit",
        )
    assert not state.exists()
