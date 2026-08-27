# Multi-session candidate issue recovery implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the candidate-issue epoch validator, ship the reviewed fix, preserve and retire `gen_011`, and resume normal scheduling with an empty `gen_012`.

**Architecture:** Keep report aggregation pure and change only the meaning of the date embedded in `epoch_id`: it is an XNYS epoch start that may precede the issue session. Preserve the durable store's exact epoch/session checks. Production recovery is a separate root-owned phase with full quiescence, a verified state archive, two audited ledger cancellations, immutable generation creation, and fail-closed scheduler restoration.

**Tech Stack:** Python 3.11, pytest, `exchange_calendars` through the existing trading-calendar helpers, SQLite `PortfolioLedger`, git worktrees, GitHub pull requests, SSH, and systemd.

## Global constraints

- Base all work on private main revision `3e15f78f75a65cbe78043477ad94e3b6eac24bfc` plus the approved design commit.
- Do not relax governed market-data validation, durable candidate-issue scope checks, candidate quarantine, execution, staging, sizing, portfolio policy, broker, or fill behavior.
- The abandoned VPS Codex branch is evidence only and must not be merged.
- Do not replay the 2026-08-25 or 2026-08-26 sessions.
- Do not mutate the VPS until the fix is reviewed, merged, and its exact merge SHA is known.
- Live quiescence, backup, cancellation, generation lifecycle, and scheduler restoration are root-agent responsibilities.
- Subagents may investigate or review but must not commit, merge, deploy, or mutate production.

---

## File map

- Modify `tests/test_cohort_failure_reporting.py`: encode separate epoch-start and issue-session dates; add the regression and fail-closed boundary tests.
- Modify `tradingagents/strategies/orchestration/daily_pipeline.py`: validate the embedded epoch-start session and allow it to precede the issue session.
- Preserve `tradingagents/strategies/metrics/epochs.py`: its existing open-epoch reuse behavior is the contract the validator must match.
- Create `/tmp/eventedge-cancel-gen011.py` only during the live recovery: narrowly cancel the two prevalidated intent IDs through `PortfolioLedger.cancel_intent()`; do not commit this operator artifact.
- Do not modify any other production source file.

### Task 1: Add the report-contract regression

**Files:**
- Modify: `tests/test_cohort_failure_reporting.py:102-224`
- Test: `tests/test_cohort_failure_reporting.py`

**Interfaces:**
- Consumes: `aggregate_candidate_input_issues(results: dict, trading_date: str | None) -> list[dict[str, object]]`
- Produces: regression fixtures that distinguish `epoch_start_session` from the candidate issue's `session`.

- [ ] **Step 1: Make the reference helper express both dates**

Replace the helper with:

```python
def _candidate_issue_reference(
    *,
    affected_cohorts,
    epoch_start_session="2026-08-10",
    session="2026-08-10",
):
    return {
        "issue_id": "candidate_input_issue_" + "a" * 32,
        "epoch_id": f"gen_001-{epoch_start_session}-" + "b" * 16,
        "session": session,
        "dependency_kind": "reference_bar",
        "reason_code": "provider_error",
        "ticker": "UI",
        "affected_cohorts": affected_cohorts,
    }
```

- [ ] **Step 2: Add the incident regression**

```python
def test_candidate_issue_reporting_accepts_later_session_in_same_epoch():
    reference = _candidate_issue_reference(
        affected_cohorts=["cohort-a"],
        epoch_start_session="2026-08-24",
        session="2026-08-26",
    )

    assert aggregate_candidate_input_issues(
        {"cohort-a": _candidate_issue_carrier(reference)},
        trading_date="2026-08-26",
    ) == [reference]
```

- [ ] **Step 3: Run the incident regression and prove RED**

Run:

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest -q \
  tests/test_cohort_failure_reporting.py::test_candidate_issue_reporting_accepts_later_session_in_same_epoch
```

Expected: one failure ending in `ValueError: candidate input issue reference id is invalid` at the current equality check in `daily_pipeline.py`.

- [ ] **Step 4: Add explicit epoch-start rejection cases**

```python
@pytest.mark.parametrize("epoch_start_session", ("2026-08-23", "2026-08-27"))
def test_candidate_issue_reporting_rejects_invalid_epoch_start(
    epoch_start_session,
):
    reference = _candidate_issue_reference(
        affected_cohorts=["cohort-a"],
        epoch_start_session=epoch_start_session,
        session="2026-08-26",
    )

    with pytest.raises(
        ValueError, match="candidate input issue reference id is invalid"
    ):
        aggregate_candidate_input_issues(
            {"cohort-a": _candidate_issue_carrier(reference)},
            trading_date="2026-08-26",
        )
```

`2026-08-23` is a Sunday; `2026-08-27` is a valid session after the issue session.

- [ ] **Step 5: Preserve exact durable hydration scope with a direct test**

Import `DailyRunState` from `tradingagents.strategies.orchestration.daily_pipeline`, then add:

```python
def test_candidate_issue_hydration_rejects_a_different_issue_session():
    state_session = date(2026, 8, 10)
    issue_session = date(2026, 8, 11)
    epoch_id = "gen_001-2026-08-10-" + "b" * 16
    issue = CandidateInputIssue.create(
        issue_id="candidate_input_issue_" + "a" * 32,
        epoch_id=epoch_id,
        session=issue_session,
        dependency_kind="reference_bar",
        reason_code="provider_error",
        ticker="UI",
        source="yfinance",
        fetched_at=datetime(2026, 8, 11, 21, tzinfo=timezone.utc),
        requested_history_digest="sha256:" + "c" * 64,
        returned_history_digest="sha256:" + "d" * 64,
        expected_sessions=(issue_session,),
        observed_sessions=(),
        retryable=False,
        affected_signal_identities=(),
        affected_cohorts=("cohort-a",),
    )

    store = SimpleNamespace(
        read_candidate_input_issues=lambda exact_epoch, exact_session: [issue]
    )
    state = DailyRunState(
        owner=SimpleNamespace(_metric_store=store),
        trading_date=state_session.isoformat(),
        session=state_session,
        processed_at=datetime(2026, 8, 10, 21, tzinfo=timezone.utc),
        epoch_id=epoch_id,
    )

    with pytest.raises(ValueError, match="candidate input issue durable scope"):
        state.finalize({})
```

- [ ] **Step 6: Run the new boundary tests**

Run:

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest -q \
  tests/test_cohort_failure_reporting.py::test_candidate_issue_reporting_rejects_invalid_epoch_start \
  tests/test_cohort_failure_reporting.py::test_candidate_issue_hydration_rejects_a_different_issue_session
```

Expected: three passing parameterized cases in total.

### Task 2: Implement the minimal epoch-start validator

**Files:**
- Modify: `tradingagents/strategies/orchestration/daily_pipeline.py:358-371`
- Test: `tests/test_cohort_failure_reporting.py`

**Interfaces:**
- Consumes: the existing `_EPOCH_ID_RE` named group `session`, the parsed issue session, and `is_session(date) -> bool`.
- Produces: the same aggregation return type and error vocabulary; only an epoch start on or before the issue session is accepted.

- [ ] **Step 1: Replace equality with canonical epoch-start validation**

Replace:

```python
if epoch_match.group("session") != session_text:
    raise ValueError("candidate input issue reference id is invalid")
```

with:

```python
try:
    epoch_start_text = epoch_match.group("session")
    epoch_start_session = date.fromisoformat(epoch_start_text)
    if (
        epoch_start_session.isoformat() != epoch_start_text
        or not is_session(epoch_start_session)
        or epoch_start_session > parsed_session
    ):
        raise ValueError
except (TypeError, ValueError) as error:
    raise ValueError("candidate input issue reference id is invalid") from error
```

Do not alter any other reference, coverage, run-scope, or durable-scope check.

- [ ] **Step 2: Run the incident regression and prove GREEN**

Run the single test from Task 1 Step 3.

Expected: one passing test.

- [ ] **Step 3: Run the focused contract suites**

Run:

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest -q \
  tests/test_cohort_failure_reporting.py tests/test_metric_epochs.py
```

Expected: all tests pass; no failure may be converted to an xfail or skip.

- [ ] **Step 4: Inspect the diff for scope**

Run:

```bash
git diff --check
git diff -- tests/test_cohort_failure_reporting.py \
  tradingagents/strategies/orchestration/daily_pipeline.py
```

Expected: only the helper/tests and the one validator block changed, with no whitespace errors.

- [ ] **Step 5: Commit the tested code change**

```bash
git add tests/test_cohort_failure_reporting.py \
  tradingagents/strategies/orchestration/daily_pipeline.py
git commit -m "fix: validate candidate issue epoch start"
```

Expected: one commit containing exactly those two files.

### Task 3: Verify, review, and merge the branch

**Files:**
- Verify: the entire repository
- Review: `private/main...codex/fix-multisession-candidate-issues`

**Interfaces:**
- Consumes: the committed validator fix and tests.
- Produces: one reviewed merge SHA on private main; no production mutation.

- [ ] **Step 1: Run all non-live tests from a clean branch**

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest -q -m "not live"
```

Expected: exit status 0 with no failures or errors. Warnings are acceptable only if they pre-existed and do not indicate skipped validation.

- [ ] **Step 2: Request independent code review**

Invoke the `requesting-code-review` skill. The reviewer must check the approved design, the RED/GREEN evidence, date ordering, XNYS validation, error categories, durable scope, and the exact diff from private main. Root resolves every substantive finding and reruns affected tests.

- [ ] **Step 3: Rebase only if private main moved**

```bash
git fetch private main
git merge-base --is-ancestor private/main HEAD
```

Expected: success. If it fails, run `git rebase private/main`, resolve without dropping either design evidence or tests, then repeat Tasks 2 Step 3 and 3 Step 1.

- [ ] **Step 4: Ship through a pull request**

Invoke the `ship` skill. Push `codex/fix-multisession-candidate-issues` to `private`, create a PR against `main`, wait for required checks, review the final PR diff, and merge only when checks and review are green.

- [ ] **Step 5: Record exact merge identity**

```bash
git fetch private main
git rev-parse private/main
git log -1 --oneline private/main
```

Expected: a full merge SHA whose history contains both the approved design commit and the tested fix commit. This SHA is the only deployable revision.

### Task 4: Quiesce production and retire `gen_011` safely

**Files and state:**
- Inspect/mutate: `/home/hermes/trading_agents/data/generations/manifest.json`
- Inspect/mutate: `/home/hermes/trading_agents/data/generations/gen_011/*/portfolio.db`
- Create: `/home/hermes/eventedge-recovery/<utc-timestamp>-gen011/` (timestamp-unique and collision-refusing)
- Create temporarily: `/tmp/eventedge-cancel-gen011.py`

**Interfaces:**
- Consumes: exactly two reviewed pending intent IDs and the merged deploy SHA.
- Produces: verified pre-recovery archive, two cancellation transitions, preserved `gen_011` worktree/state, and a retired manifest entry.

- [ ] **Step 1: Capture unit state and reconfirm the live window before mutation**

Over SSH to `hermes@100.112.88.99`, first create the unique, collision-refusing recovery destination below. Then record UTC/local time, `systemctl list-timers`, root `git status --short`, root HEAD, the manifest-validated `gen_011` entry, and its worktree HEAD. Before any stop, disable, or mask operation, persist the exact `systemctl is-enabled` and `is-active` outputs for all six units below. Require each automatic entry point's captured enablement to be exactly `enabled` or `disabled`; otherwise stop rather than guessing how to restore it. Require enough time to complete and restore service before the captured next `trade.timer` trigger. Refuse to continue if a worker is running, an unexpected active generation exists, the safe window is too short, or the live pending set differs from the approved two-intent shape.

```bash
umask 077
recovery_stamp="$(date -u +%Y%m%dT%H%M%S%N)"
recovery_dir="/home/hermes/eventedge-recovery/${recovery_stamp}-gen011"
mkdir "$recovery_dir"
```

```bash
units=(
  trade.timer trade-rerun.path trade-preflight.timer
  trade.service trade-rerun.service trade-preflight.service
)
entry_points=(trade.timer trade-rerun.path trade-preflight.timer)
for unit in "${units[@]}"; do
  printf '%s\t%s\t%s\n' "$unit" \
    "$(systemctl is-enabled "$unit" || true)" \
    "$(systemctl is-active "$unit" || true)"
done | tee "$recovery_dir/unit-state-before.tsv"
```

`mkdir` must fail if the timestamped name already exists; do not retry with an overwrite or a non-unique name.

- [ ] **Step 2: Persistently disable, then runtime-mask every execution entry point**

```bash
units=(
  trade.timer trade-rerun.path trade-preflight.timer
  trade.service trade-rerun.service trade-preflight.service
)
sudo systemctl disable --now trade.timer trade-rerun.path trade-preflight.timer
for unit in trade.timer trade-rerun.path trade-preflight.timer; do
  test "$(systemctl is-enabled "$unit" || true)" = disabled
  test "$(systemctl is-active "$unit" || true)" = inactive
done
sudo systemctl mask --runtime trade.timer trade-rerun.path trade-preflight.timer \
  trade.service trade-rerun.service trade-preflight.service
for unit in "${units[@]}"; do
  test "$(systemctl is-enabled "$unit" || true)" = masked-runtime
  test "$(systemctl is-active "$unit" || true)" = inactive
done
```

The persistent disable happens before the runtime masks, so a reboot cannot clear the mask and reactivate a timer or path unit. Then verify all six units are inactive/runtime-masked, `.triggers/run-now` is absent, the runtime lock is not held, and no `daily_trading.sh`, `run_generations.py`, `run_cohorts.py`, or `preflight.sh` process exists.

- [ ] **Step 3: Capture exact intent and broker provenance**

For every `gen_011/*/portfolio.db`, run a read-only query joining `order_intents`, `intent_signals`, `signals`, and `external_orders`. Require exactly:

```text
horizon_30d_size_100k | COIN | short | 27 | 2026-08-27 | pending
horizon_30d_size_50k  | COIN | short | 13 | 2026-08-27 | pending
```

Record exact intent IDs and price rules. Both `external_order_id` fields and joined external-order rows must be absent. Any mismatch stops recovery.

- [ ] **Step 4: Create and verify the rollback archive**

Use the unique, collision-refusing destination created in Step 1. Capture manifest/unit/git/process/intent evidence there, hash every `gen_011` file, archive the entire manifest-derived state directory and manifest, hash the archive, extract it into a `mktemp -d` directory, and prove the extracted file hashes equal the originals. Do not proceed on any mismatch.

- [ ] **Step 5: Prepare the narrow cancellation program**

Create `/tmp/eventedge-cancel-gen011.py` with this behavior:

```python
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

if len(sys.argv) != 4:
    raise SystemExit("usage: eventedge-cancel-gen011.py TIMESTAMP INTENT_100K INTENT_50K")

repo = Path("/home/hermes/trading_agents").resolve()
manifest_path = repo / "data" / "generations" / "manifest.json"
try:
    manifest = json.loads(manifest_path.read_text())
    entries = manifest["generations"]
except (OSError, TypeError, ValueError, KeyError) as error:
    raise SystemExit("manifest is unreadable") from error
matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("gen_id") == "gen_011"]
if len(matches) != 1:
    raise SystemExit("gen_011 manifest entry is missing or ambiguous")
entry = matches[0]
required = ("status", "git_commit", "worktree_path", "state_dir")
if any(not isinstance(entry.get(key), str) or not entry[key] for key in required):
    raise SystemExit("gen_011 manifest entry is incomplete")
if entry["status"] != "active":
    raise SystemExit(f"gen_011 is not active: {entry['status']}")

worktree_path = Path(entry["worktree_path"]).resolve()
state_dir = Path(entry["state_dir"]).resolve()
if not worktree_path.is_dir() or not state_dir.is_dir():
    raise SystemExit("manifest-owned worktree or state directory is missing")
worktree_commit = subprocess.run(
    ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
    check=False,
    capture_output=True,
    text=True,
).stdout.strip()
if worktree_commit != entry["git_commit"]:
    raise SystemExit("gen_011 worktree commit does not match its manifest")

sys.path.insert(0, str(worktree_path))
from tradingagents.strategies.state import portfolio_ledger

module_path = Path(portfolio_ledger.__file__).resolve()
if not module_path.is_relative_to(worktree_path):
    raise SystemExit("PortfolioLedger was not imported from the gen_011 worktree")
PortfolioLedger = portfolio_ledger.PortfolioLedger

operation_at = datetime.fromisoformat(sys.argv[1])
if operation_at.tzinfo is None or operation_at.utcoffset() is None:
    raise SystemExit("TIMESTAMP must be timezone-aware")
reason = "operator incident recovery: retire gen_011 before fresh generation"
targets = {
    "horizon_30d_size_100k": sys.argv[2],
    "horizon_30d_size_50k": sys.argv[3],
}

for cohort_id, intent_id in targets.items():
    path = state_dir / cohort_id / "portfolio.db"
    if not path.is_file():
        raise SystemExit(f"missing manifest-owned ledger for {cohort_id}")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        opening = connection.execute(
            "SELECT amount FROM cash_events WHERE cohort_id = ? "
            "AND event_type = 'opening'",
            (cohort_id,),
        ).fetchone()
        pending = connection.execute(
            "SELECT status, external_order_id FROM order_intents "
            "WHERE cohort_id = ? AND intent_id = ?",
            (cohort_id, intent_id),
        ).fetchone()
    if opening is None or pending != ("pending", None):
        raise SystemExit(f"precondition failed for {cohort_id}/{intent_id}")
    ledger = PortfolioLedger(path, cohort_id, Decimal(str(opening[0])))
    try:
        ledger.cancel_intent(intent_id, operation_at, reason)
    finally:
        ledger.close()
```

Root must substitute only the two IDs captured in Step 3 and pass one recorded timezone-aware timestamp. The program itself re-reads the manifest, requires active `gen_011`, derives both paths from its unique manifest entry, verifies the detached worktree commit against `git_commit`, and rejects an ambient `PortfolioLedger` import before it opens a ledger. Review the file and its SHA-256 before execution.

- [ ] **Step 6: Execute once and verify logical changes**

Run the program once. Re-open every ledger read-only and require zero pending intents, exactly two new `cancelled` transitions with the recorded timestamp/reason, no external orders, and no changes to unrelated logical table content. If execution or verification fails, keep units masked, preserve the partial state, restore the whole `gen_011` directory from the verified extraction by same-filesystem replacement, and recheck original hashes.

- [ ] **Step 7: Retire while preserving the worktree**

```bash
cd /home/hermes/trading_agents
.venv/bin/python scripts/run_generations.py retire gen_011 --keep-worktree
```

Expected: the manifest reports `gen_011` retired, its worktree still exists at its original commit, and its state plus rollback archive remain readable.

### Task 5: Deploy the merge and start clean `gen_012`

**Files and state:**
- Update: `/home/hermes/trading_agents` root checkout to the reviewed merge SHA.
- Create: `/home/hermes/trading_agents/.worktrees/gen_012`
- Create: `/home/hermes/trading_agents/data/generations/gen_012`

**Interfaces:**
- Consumes: the exact reviewed merge SHA and retired `gen_011`.
- Produces: one active, empty `gen_012` and restored normal systemd scheduling.

- [ ] **Step 1: Preserve and classify root checkout drift**

Require root HEAD to be the prior reviewed commit and the only local changes to be the already inventoried `deploy/systemd/install.sh` executable-bit difference plus untracked runtime `data/`. Capture them in the recovery evidence. Stop if any content diff or additional path appears.

- [ ] **Step 2: Fast-forward to the reviewed merge SHA**

Fetch private main and fast-forward without checkout reset or destructive cleanup. Require:

```bash
git rev-parse HEAD
git rev-parse private/main
```

to equal the exact merge SHA from Task 3. Rerun the focused reporting/epoch suite on the VPS venv before creating a generation.

- [ ] **Step 3: Start the fresh generation**

```bash
.venv/bin/python scripts/run_generations.py start \
  "gen_012: multi-session candidate issue reporting recovery"
```

Expected: `Started gen_012` at the reviewed merge SHA with a detached worktree and a new state directory.

- [ ] **Step 4: Prove immutable code and empty state**

Require root HEAD, `gen_012.git_commit` in the manifest, and `.worktrees/gen_012` HEAD to equal the merge SHA. Require `gen_012` to be the sole active generation. Before and after no-write preflight, require its manifest-owned state directory to contain no inherited database, journal, signal, intent, transition, fill, lot, mark, metric, candidate-issue, snapshot, or external-order state.

- [ ] **Step 5: Run read-only smoke checks**

Run generation listing/status and the governed/all preflight for the current eligible session. A preflight provider failure is not permission to bypass a gate: leave scheduling disabled, record the evidence, and investigate. Do not run `run-daily` for a historical date.

- [ ] **Step 6: Restore normal entry points**

Require the captured next daily trigger still to be in the future; otherwise leave the three entry points persistently disabled and all six units runtime-masked so no reboot can launch an unapproved catch-up run. If it is still future, use the recorded recovery directory below to remove all six runtime masks and restore every automatic entry point to its exact Task 4 captured enablement. The recovery only accepts captured `enabled` or `disabled`; do not change the captured persistent enablement of the oneshot services.

```bash
: "${recovery_dir:?set recovery_dir to the Task 4 archive directory}"
test -f "$recovery_dir/unit-state-before.tsv"
sudo systemctl unmask --runtime \
  trade.timer trade-rerun.path trade-preflight.timer \
  trade.service trade-rerun.service trade-preflight.service
while IFS=$'\t' read -r unit enabled _active; do
  case "$unit" in
    trade.timer|trade-rerun.path|trade-preflight.timer)
      case "$enabled" in
        enabled) sudo systemctl enable --now "$unit" ;;
        disabled) sudo systemctl disable --now "$unit" ;;
        *) echo "unexpected captured enablement for $unit: $enabled" >&2; exit 1 ;;
      esac
      ;;
  esac
done < "$recovery_dir/unit-state-before.tsv"
for unit in trade.timer trade-rerun.path trade-preflight.timer; do
  captured="$(awk -F $'\t' -v unit="$unit" '$1 == unit { print $2 }' "$recovery_dir/unit-state-before.tsv")"
  test "$(systemctl is-enabled "$unit" || true)" = "$captured"
done
for unit in trade.timer trade-rerun.path trade-preflight.timer \
  trade.service trade-rerun.service trade-preflight.service; do
  test "$(systemctl is-enabled "$unit" || true)" != masked-runtime
done
```

Re-read and compare the three entry-point enablement values to `unit-state-before.tsv`, verify the six units are no longer runtime-masked, remove no trigger unless its absence was already verified, and confirm next trigger times.

- [ ] **Step 7: Final production verification**

Verify no worker is unexpectedly active, `gen_011` is retired/preserved, `gen_012` is active/empty at the merge SHA, focused VPS tests passed, smoke checks passed, all three entry points have their expected schedule, and no replay was invoked. Record archive paths, hashes, merge SHA, cancellation timestamp, intent IDs, and final unit state in the handoff.
