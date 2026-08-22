# Canonical Run Outcome Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make generation daily-run completion authoritative as `clean`, `degraded`, or `failed`, with execution-valid degradation remaining alertable while the CLI and systemd job complete successfully.

**Architecture:** Add a small generation-level outcome module and make `GenerationManager` attach its value to every daily subprocess result and history entry. Keep legacy booleans for historical and external compatibility, but make the daily CLI and dashboard consume the canonical outcome with a bounded legacy fallback.

**Tech Stack:** Python 3.10+, `Enum`, pytest, Bash, Ruff, GitHub pull requests.

**Authoritative design:** `docs/superpowers/specs/2026-08-21-reliability-first-orchestration-refactor-design.md`

## Global Constraints

- Work only in branch `codex/run-outcome-contract` and its isolated worktree.
- Do not alter cohort execution, candidate quarantine, governed validation, preflight, ledger, metric, strategy, or portfolio-policy behavior.
- `clean` and execution-valid `degraded` daily runs exit process status 0; only `failed` exits nonzero.
- `success` retains its historical meaning of clean research observation, so a degraded result remains `success=false`.
- `outcome` is authoritative for new daily results and history; `success`, `degraded`, and `execution_valid` remain bounded compatibility fields.
- Missing or malformed authoritative worker output fails closed.
- Preflight keeps its independent success and exit contract.
- Historical manifests are not migrated; dashboard reads use a legacy fallback.
- Do not touch `.env`, `data/`, generation state, provider APIs, or the VPS in this PR.
- Subagents may inspect, test, review, or edit assigned files but must not commit; the root agent reviews and commits explicit paths.

---

### Task 1: Add the canonical outcome value and compatibility reader

**Files:**
- Create: `tradingagents/strategies/orchestration/run_outcome.py`
- Create: `tests/test_run_outcome.py`

**Interfaces:**
- Consumes: a generation daily result as `Mapping[str, object]`.
- Produces: `RunOutcome`, `run_outcome(result, *, allow_legacy=False)`, and `completed_run(result)`.

- [ ] **Step 1: Write the failing outcome tests**

```python
from __future__ import annotations

import pytest

from tradingagents.strategies.orchestration.run_outcome import (
    RunOutcome,
    completed_run,
    run_outcome,
)


@pytest.mark.parametrize("value", ("clean", "degraded", "failed"))
def test_run_outcome_accepts_exact_wire_values(value: str) -> None:
    assert run_outcome({"outcome": value}) is RunOutcome(value)


@pytest.mark.parametrize("value", (None, "ok", "DEGRADED", 1, True))
def test_run_outcome_rejects_missing_or_malformed_authoritative_value(value) -> None:
    payload = {} if value is None else {"outcome": value}
    with pytest.raises(ValueError, match="invalid run outcome"):
        run_outcome(payload)


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        ({"success": True}, RunOutcome.CLEAN),
        ({"success": False, "degraded": True, "execution_valid": True}, RunOutcome.FAILED),
        ({"success": False}, RunOutcome.FAILED),
    ),
)
def test_legacy_outcome_is_available_only_when_requested(payload, expected) -> None:
    assert run_outcome(payload, allow_legacy=True) is expected
    with pytest.raises(ValueError, match="invalid run outcome"):
        run_outcome(payload)


@pytest.mark.parametrize(
    "payload",
    (
        {"success": True, "degraded": True},
        {"success": True, "execution_valid": False},
        {"success": 1},
        {"success": True, "degraded": "false"},
        {"success": False, "execution_valid": 1},
    ),
)
def test_legacy_outcome_rejects_contradictory_or_non_boolean_fields(payload) -> None:
    with pytest.raises(ValueError, match="invalid run outcome"):
        run_outcome(payload, allow_legacy=True)


def test_only_clean_and_degraded_are_completed_processes() -> None:
    assert completed_run({"outcome": "clean"}) is True
    assert completed_run({"outcome": "degraded"}) is True
    assert completed_run({"outcome": "failed"}) is False
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest tests/test_run_outcome.py -q
```

Expected: collection fails with `ModuleNotFoundError` for `run_outcome`.

- [ ] **Step 3: Implement the minimal outcome module**

```python
from __future__ import annotations

from enum import Enum
from typing import Mapping


class RunOutcome(str, Enum):
    CLEAN = "clean"
    DEGRADED = "degraded"
    FAILED = "failed"


def run_outcome(
    result: Mapping[str, object], *, allow_legacy: bool = False
) -> RunOutcome:
    value = result.get("outcome")
    if isinstance(value, str):
        try:
            return RunOutcome(value)
        except ValueError:
            pass
    if allow_legacy and "outcome" not in result:
        for key in ("success", "degraded", "execution_valid"):
            if key in result and not isinstance(result[key], bool):
                raise ValueError("invalid run outcome")
        if result.get("success") is True:
            if result.get("degraded") is True or result.get("execution_valid") is False:
                raise ValueError("invalid run outcome")
            return RunOutcome.CLEAN
        if result.get("success") is False:
            return RunOutcome.FAILED
    raise ValueError("invalid run outcome")


def completed_run(result: Mapping[str, object]) -> bool:
    return run_outcome(result) in {RunOutcome.CLEAN, RunOutcome.DEGRADED}
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the Step 2 command again. Expected: all tests pass.

- [ ] **Step 5: Commit explicit paths**

```bash
git diff --check
git add tradingagents/strategies/orchestration/run_outcome.py tests/test_run_outcome.py
git commit -m "feat: define canonical daily run outcome"
```

### Task 2: Canonicalize every generation daily result

**Files:**
- Modify: `tradingagents/strategies/orchestration/run_outcome.py`
- Modify: `tradingagents/strategies/orchestration/generation_manager.py`
- Modify: `scripts/run_cohorts.py`
- Modify: `tests/test_run_outcome.py`
- Modify: `tests/test_generation_manager.py`
- Test: `tests/test_cohort_failure_reporting.py`
- Modify: `tests/test_metrics_migration.py`
- Test: `tests/test_preflight.py`

**Interfaces:**
- Consumes: `RunOutcome` from Task 1 and parsed cohort results from `_extract_cohort_results()`.
- Produces: every non-preflight `_run_cohorts_subprocess()` result contains `outcome`; every daily `run_history` entry contains the same value.
- Produces: a validated worker-wire boundary that accepts only the exact 16
  cohort IDs built by `build_default_cohorts({})` and lifecycle-consistent
  cohort dictionaries.
- Produces: one exact daily envelope with keys `wire_version` and
  `cohort_results`, where `wire_version` is integer 1. Preflight remains
  unwrapped and independent. The compact envelope is carried on exactly one
  stdout line prefixed by `EVENTEDGE_DAILY_RESULT_V1=`.

- [ ] **Step 1: Write failing result and history assertions**

Add these assertions to existing clean, degraded, mixed-failure, nonzero-child,
timeout, and exception cases:

```python
assert clean_result["outcome"] == "clean"
assert degraded_result["outcome"] == "degraded"
assert failed_result["outcome"] == "failed"
```

Add a table-driven worker-wire matrix. Use all 16 names returned by
`build_default_cohorts({})` for valid fixtures. Prove:

```python
@pytest.mark.parametrize(
    ("returncode", "payload_kind", "expected"),
    (
        (0, "clean", "clean"),
        (0, "degraded", "degraded"),
        (2, "degraded", "degraded"),
        (1, "degraded", "failed"),
        (2, "clean", "failed"),
        (1, "clean", "failed"),
    ),
)
def test_worker_return_code_and_payload_must_agree(
    tmp_path, returncode, payload_kind, expected
):
    result = _run_with_daily_wire(tmp_path, returncode, payload_kind)
    assert result["outcome"] == expected
```

Also prove rc-zero fails for an empty object, unknown or missing cohort IDs,
non-mapping values, missing/non-boolean `error`, missing lifecycle booleans on a
non-error cohort, raw unwrapped cohort JSON, a wrong wire version, extra or
missing envelope keys, duplicate envelopes, and unrelated trailing JSON
including a valid-looking decoy. Update the environment/logging
fixture in `tests/test_generation_manager.py` and the rc-zero fixture in
`tests/test_metrics_migration.py` to emit a complete valid 16-cohort result
when the test intends success.

Update `test_run_daily_records_history` and
`test_run_daily_records_degraded_history` so mocked results contain the new
outcome and persisted history asserts it exactly. Add:

```python
def test_run_daily_persists_failed_outcome(git_repo, manager):
    manager.start_generation("failed history test")
    with patch.object(manager, "_run_cohorts_subprocess") as run:
        run.return_value = {
            "outcome": "failed",
            "success": False,
            "execution_valid": False,
            "elapsed_s": 0.5,
            "error": "governed input invalid",
        }
        manager.run_daily("2026-03-31")
    entry = manager.get_generation("gen_001").run_history[0]
    assert entry["outcome"] == "failed"
    assert entry["success"] is False
```

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest tests/test_generation_manager.py tests/test_cohort_failure_reporting.py -q
```

Expected: new assertions fail because current results and history omit
`outcome`.

- [ ] **Step 3: Attach an outcome in every daily subprocess branch**

Add `DAILY_RESULT_PREFIX = "EVENTEDGE_DAILY_RESULT_V1="`,
`DAILY_RESULT_WIRE_VERSION = 1`, and helpers that render and parse exactly one
line whose suffix is:

```python
{
    "wire_version": 1,
    "cohort_results": {"horizon_30d_size_5k": {}},
}
```

The parser requires exactly one prefixed line and exact envelope keys;
unwrapped daily JSON and multiple prefixed envelopes fail closed.
`scripts/run_cohorts.py` prints the versioned envelope for its final daily
result. Keep preflight on its existing independent unwrapped JSON parser.

Import `RunOutcome`. Every existing failure dictionary receives
`"outcome": RunOutcome.FAILED.value`; the execution-valid degradation
dictionary receives `"outcome": RunOutcome.DEGRADED.value`; and the clean
dictionary receives `"outcome": RunOutcome.CLEAN.value`.

Timeouts, exceptions, malformed or unparseable nonzero output, and any parsed
cohort failure use `failed`. Before classification, compare the result keys to
the exact names from `build_default_cohorts({})`. Require every value to be a
mapping with boolean `error`. A non-error cohort must have boolean `degraded`,
`execution_valid`, and `staging_valid`; clean cohorts require
`execution_valid=true`, `staging_valid=true`, and `degraded=false`; degraded
cohorts require `execution_valid=true`. Optional lifecycle fields on an error
cohort must still be booleans when present, and an error cohort cannot claim
`staging_valid=true`. Preserve `count_failed_cohorts()` semantics: `valid=false`
or a truthy `invalid_reason` makes the run failed even if `error=false`.

Apply this return-code matrix after schema validation:

- any invalid payload or any cohort failure is `failed` regardless of rc;
- pure degradation is `degraded` only for rc 0 or 2;
- a pure clean payload is `clean` only for rc 0; and
- every other payload/rc combination is `failed` as contradictory.

Persist `"outcome": result["outcome"]` in each daily history entry. Add a
preflight regression showing the preflight result and no-history behavior do
not gain `outcome`. Do not change `_preflight_subprocess_result()` or preflight
history.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the Step 2 command again. Expected: all focused tests pass.

- [ ] **Step 5: Commit explicit paths**

```bash
git diff --check
git add tradingagents/strategies/orchestration/run_outcome.py tradingagents/strategies/orchestration/generation_manager.py scripts/run_cohorts.py tests/test_run_outcome.py tests/test_generation_manager.py tests/test_cohort_failure_reporting.py tests/test_metrics_migration.py tests/test_preflight.py
git commit -m "feat: persist canonical generation outcomes"
```

### Task 3: Make the daily CLI fail only for failed outcomes

**Files:**
- Modify: `scripts/run_generations.py`
- Modify: `tests/test_generation_manager.py`
- Modify: `tests/test_shell_preflight_contract.py`

**Interfaces:**
- Consumes: authoritative `outcome` returned by `GenerationManager.run_daily()`.
- Produces: printed `OK`, `DEGRADED`, or `FAILED`; process status 0 for clean and degraded, and 1 when any generation failed.

- [ ] **Step 1: Change the CLI tests before production code**

Change the degraded CLI test so its fixture contains `"outcome": "degraded"`,
calls `run_generations.main()` without expecting `SystemExit`, and still asserts
`gen_001: DEGRADED`. Add:

```python
def test_run_daily_cli_completes_when_clean_and_degraded_are_mixed(
    monkeypatch, capsys
):
    results = {
        "gen_001": {"outcome": "clean", "success": True, "elapsed_s": 1.0},
        "gen_002": {
            "outcome": "degraded",
            "success": False,
            "degraded": True,
            "execution_valid": True,
            "elapsed_s": 2.0,
            "error": "candidate quarantined",
        },
    }
    monkeypatch.setattr(GenerationManager, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(GenerationManager, "run_daily", lambda self, date: results)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_generations.py", "run-daily", "--date", "2026-07-31"],
    )
    run_generations.main()
    output = capsys.readouterr().out
    assert "gen_001: OK" in output
    assert "gen_002: DEGRADED" in output
```

Give the mixed clean/failed fixture explicit outcomes and retain exit 1.
Parameterize the shell helper with `daily_rc`; add a test proving a failed daily
command still propagates 1.

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest tests/test_generation_manager.py tests/test_shell_preflight_contract.py -q
```

Expected: degraded CLI tests fail because current code raises `SystemExit(1)`.

- [ ] **Step 3: Consume the canonical outcome in the daily CLI**

Import `RunOutcome` and `run_outcome`. Replace boolean status selection with:

```python
outcome = run_outcome(result)
status = "OK" if outcome is RunOutcome.CLEAN else outcome.value.upper()
```

Replace the final condition with:

```python
if any(run_outcome(result) is RunOutcome.FAILED for result in results.values()):
    raise SystemExit(1)
```

Do not add a special degraded shell exit and do not alter preflight handling.

- [ ] **Step 4: Run focused tests and shell syntax verification**

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest tests/test_generation_manager.py tests/test_shell_preflight_contract.py -q
bash -n scripts/daily_trading.sh scripts/preflight.sh
```

Expected: all commands exit 0.

- [ ] **Step 5: Commit explicit paths**

```bash
git diff --check
git add scripts/run_generations.py tests/test_generation_manager.py tests/test_shell_preflight_contract.py
git commit -m "fix: complete execution-valid degraded runs"
```

### Task 4: Prefer canonical outcomes in the dashboard

**Files:**
- Modify: `tradingagents/dashboard/pages/overview.py`
- Modify: `tests/test_metrics_reporting.py`

**Interfaces:**
- Consumes: new `outcome` history values and historical legacy boolean entries.
- Produces: a trading-day count that includes clean and degraded runs and excludes failed runs.

- [ ] **Step 1: Write failing canonical and legacy dashboard cases**

Allow `_render_overview_card()` to receive a test history and parameterize:

```python
@pytest.mark.parametrize(
    ("history", "expected_days"),
    (
        ([{"date": "2026-08-31", "outcome": "clean"}], 1),
        ([{"date": "2026-08-31", "outcome": "degraded"}], 1),
        ([{"date": "2026-08-31", "outcome": "failed"}], 0),
        ([{"date": "2026-08-31", "success": True}], 1),
        ([{"date": "2026-08-31", "success": False, "degraded": True, "execution_valid": True}], 0),
        ([{"date": "2026-08-31", "outcome": "DEGRADED", "success": True}], 0),
    ),
)
def test_overview_counts_canonical_and_legacy_completed_runs(
    monkeypatch, history, expected_days
):
    rendered = _render_overview_card(
        monkeypatch,
        {"metric_schema_version": 2, "headline_books": {}},
        run_history=history,
    )
    assert ("Trading Days", expected_days) in rendered.metrics
```

- [ ] **Step 2: Run the dashboard tests and verify RED**

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest tests/test_metrics_reporting.py -q
```

Expected: canonical cases fail because the dashboard reads only legacy fields.

- [ ] **Step 3: Add the bounded compatibility read**

Use `run_outcome(record, allow_legacy=True)`. Ignore `ValueError` for malformed
history. Count dates only when the result is `RunOutcome.CLEAN` or
`RunOutcome.DEGRADED`.

- [ ] **Step 4: Run the dashboard tests and verify GREEN**

Run the Step 2 command again. Expected: all reporting tests pass.

- [ ] **Step 5: Commit explicit paths**

```bash
git diff --check
git add tradingagents/dashboard/pages/overview.py tests/test_metrics_reporting.py
git commit -m "refactor: consume canonical run outcomes"
```

### Task 5: Verify, review, document, and publish PR 1

**Files:**
- Modify only if final behavior makes current text inaccurate: `README.md`
- Review: all files changed since `private/main`

**Interfaces:**
- Consumes: completed Tasks 1-4.
- Produces: reviewed PR 1 merged into `private/main`, with local `main` synchronized.

- [ ] **Step 1: Run focused verification**

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest tests/test_run_outcome.py tests/test_generation_manager.py tests/test_cohort_failure_reporting.py tests/test_metrics_migration.py tests/test_preflight.py tests/test_shell_preflight_contract.py tests/test_metrics_reporting.py -q
```

- [ ] **Step 2: Run the full non-live suite**

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest -m "not live" -q
```

- [ ] **Step 3: Run static and repository checks**

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m ruff check scripts tradingagents tests
bash -n scripts/daily_trading.sh scripts/preflight.sh deploy/systemd/install.sh
git diff --check private/main...HEAD
git status --short --branch
```

Expected: every command exits 0 and only intended paths are changed.

- [ ] **Step 4: Review documentation accuracy**

If README behavior is stale, update only the affected passage, run the humanizer
workflow on changed prose, then rerun focused tests and `git diff --check`.

- [ ] **Step 5: Request whole-branch review**

Create a review package from `private/main...HEAD`. Resolve every Critical and
Important finding through a focused failing test and re-review before pushing.

- [ ] **Step 6: Push and create PR 1**

```bash
git push -u private codex/run-outcome-contract
gh pr create --repo potalora/eventedge --base main --head codex/run-outcome-contract --title "fix: make degraded runs operationally successful" --body $'## Summary\n- add a canonical clean, degraded, or failed generation outcome\n- keep degraded runs alertable without failing the systemd job\n- preserve legacy manifest and dashboard compatibility\n\n## Test plan\n- focused outcome, generation, shell, and dashboard suites\n- full non-live pytest suite\n- Ruff, Bash syntax, and git diff checks'
```

- [ ] **Step 7: Merge only after exact-head verification**

Confirm the PR head equals the verified local commit and all available checks
and reviews are green. Merge without force, fetch `private/main`, fast-forward
local `main`, and rerun the focused outcome contract on the merged tree.
