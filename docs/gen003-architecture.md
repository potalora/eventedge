# EventEdge `gen_003` Architecture & Operator Runbook

This runbook documents the `gen_003` runtime topology, daily execution flow, `generation.sqlite3` inspection procedures, and the 60–90 session infrastructure freeze rules.

## Hermes container topology

In production, EventEdge runs inside the Hermes container environment under the `hermes` service account:

- **Application checkout:** `/opt/data/private/eventedge/app` (frozen generation worktrees live alongside the generation registry under the repo's `.generations/` / `data/` state layout).
- **Operator / runtime profile:** `/opt/data/profiles/eventedge`
- **Writer identity:** `EventEdge trading writer` (`hermes:hermes`, owning exclusive write access to `generation.sqlite3`, `data/runtime_recovery/`, and `data/logs/`).
- **OAuth credentials:** `/opt/data/private/eventedge/credentials/plan-auth.json` (`chatgpt_plan_auth_path` in `config/profiles/gen003-clean.json`).
- **Systemd units (`deploy/systemd/`):**
  - `trade.timer` / `trade.service`: Fires Monday–Friday at 18:00 `America/New_York` (plus recovery wakeups) and invokes `scripts/recover_runtime.py`.
  - `trade-preflight.timer` / `trade-preflight.service`: Runs `scripts/preflight.sh` (read-only live fetch and governed gate checks, no state writes).
  - `trade-rerun.path` / `trade-rerun.service`: Bounded rerun trigger.

## How `gen_003` runs

1. **Runtime controller (`scripts/recover_runtime.py`):**
   - Acquires the canonical exclusive runtime lock (`runtime_lock.py`) and passes the inherited file descriptor to child processes via `EVENTEDGE_RUNTIME_LOCK_FD` / `pass_fds` to prevent lock TOCTOU races.
   - Short-circuits idle timer ticks cleanly (0 git forks, 0 SQLite opens, 0 fsyncs).
   - When a valid `XNYS` session is due (or pending P0 recovery is safe), invokes `scripts/daily_trading.sh` or bounded recovery workers (max 3 attempts, `5m` / `30m` backoff).
2. **Daily wrapper (`scripts/daily_trading.sh`):**
   - Loads `.env`, runs the hard `governed` preflight gate (`scripts/run_generations.py preflight --date "$TODAY" --preflight-mode governed`), and then runs `scripts/run_generations.py run-daily --date "$TODAY"`.
   - In its `EXIT` trap, generates deterministic JSON and Markdown operational reports under `docs/reports/` via `scripts/generate_operational_report.py`.
3. **Generation & cohort execution (`scripts/run_generations.py`, `scripts/run_cohorts.py`):**
   - `run_cohorts.py` executes in the generation's frozen git worktree and reports structured JSON outcomes atomically via `--result-file` (`_io.atomic_write_json`), decoupled from stdout/stderr.
   - Worker attempt logs and outputs are archived under `data/logs/run_attempts/` (`run_evidence.py`, which redacts allowlisted secrets only in stdout/stderr logs).
4. **Session pipeline (`CohortOrchestrator`):**
   - **P0 Accounting (`SessionExecutor`):** Validates exact-session `alpaca_sip` raw bars and `yfinance_actions_benchmarks` corporate actions/benchmarks (Tier-1, fail-closed). Applies corporate actions, settles exits/open-gap stops, executes T+1 opening entries (trimming quantity deterministically if slippage/gap pushes notional over limit within `max_open_chase_pct = 0.05`), settles intraday stops, accrues borrow/financing, and snapshots all 16 books.
   - **Tier-2 Acquisition & Screening (`DataAcquisitionService`):** Instantiated **once per daily run**, fetches the 8 Tier-2 strategy sources + 3 optional enrichment feeds, freezes the Parquet/gzip bundle, and screens the 5 active strategies (`earnings_call`, `supply_chain`, `govt_contracts`, `weather_ag`, `commodity_macro`). Tier-2 failures trip per-strategy circuit breakers without invalidating Tier-1 accounting.
   - **LLM Synthesis & Sizing:** `TransportWorkerPool` (`chatgpt_plan` OAuth, `max_workers=32`, hard `SIGKILL` timeouts) runs `gpt-6-luna` (`high`) for bounded `commodity_macro`/`weather_ag` assessments and `gpt-6.2-sol` (`high`) for thesis analysis and 1-call-per-horizon `PortfolioCommittee.synthesize_horizon()` (4 committee calls/day). `PortfolioPolicy` (`portfolio_policy_v2`) deterministically sizes and stages intents across all 16 books.

## Inspecting `generation.sqlite3`

All authoritative state for a generation lives in a single WAL-mode SQLite database (`generation.sqlite3`, `busy_timeout = 10000 ms`) defined in `tradingagents/strategies/state/generation_db.py`:

- **14 shared tables:** `schema_metadata`, `metric_epochs`, `signals`, `signal_candidate_contexts`, `marks`, `corporate_actions`, `dividend_events`, `benchmark_observations`, `strategy_health`, `regime_snapshots`, `outcomes`, `bar_recoveries`, `candidate_input_issues`, `session_shared_seals`.
- **17 cohort-scoped tables (indexed by `cohort_id`):** `accounting_state`, `session_runs`, `session_phases`, `session_execution_contexts`, `committee_decisions`, `order_intents`, `intent_signals`, `fills`, `lots`, `lot_closures`, `cash_events`, `borrow_accruals`, `financing_accruals`, `dividend_receivables`, `account_snapshots`, `staging_runs`, `policy_audit_records`.

### Safe read-only inspection

Never open `generation.sqlite3` in read-write mode while `EventEdge trading writer` may be active, and never copy only the main `.sqlite3` file without its `-wal` journal. Use one of these safe read-only paths:

```bash
# 1. Python snapshot via sqlite3.Connection.backup() (safe with active WAL)
python -c '
from pathlib import Path
from tradingagents.strategies.orchestration._io import snapshot_sqlite
with snapshot_sqlite(Path("data/generations/gen_003/generation.sqlite3")) as conn:
    print("Latest snapshots:", conn.execute(
        "SELECT cohort_id, session, total_equity FROM account_snapshots ORDER BY session DESC LIMIT 5"
    ).fetchall())
'

# 2. Read-only URI mode in sqlite3 CLI (inspects current WAL state without writing)
sqlite3 "file:data/generations/gen_003/generation.sqlite3?mode=ro" \
  "SELECT session, strategy, status FROM strategy_health ORDER BY session DESC LIMIT 10;"

# 3. Check 5-session continuity readiness (read-only)
python scripts/check_generation_readiness.py \
  --repo /opt/data/private/eventedge/app \
  --generation gen_003 \
  --expected-commit FULL_40_CHARACTER_SHA \
  --through 2026-09-21
```

Operational reports (`docs/reports/operational_report_<date>.json` and `.md`) are the canonical daily monitor inputs.

## 60–90 session infrastructure freeze rules

Once `gen_003` is launched for forward evaluation:

1. **No in-place patches:** Do not modify Python code, strategy parameters, `GenerationConfig` (`config/profiles/gen003-clean.json`), model identifiers (`gpt-6.2-sol` / `gpt-6-luna`), or prompt contracts inside an active generation's worktree.
2. **New generation for material changes:** Any bug fix or behavior change that affects signal generation, committee decisions, sizing, or execution economics requires starting a **new generation** (`scripts/run_generations.py start "..."`) with a fresh metric epoch.
3. **Diagnostic readouts only at 30 / 60 / 90 sessions:** Review operational continuity, source/strategy health, drawdowns, turnover, and carrying costs at 30, 60, and 90 completed `XNYS` sessions. Do not tune thresholds, drop strategies, or promote capital based on short-window returns.
4. **252-session minimum for alpha claims:** Per [the forward-test protocol](research/2026-10-09-forward-test-protocol.md), strong alpha evaluation requires at least 252 valid out-of-sample daily returns on the primary `horizon_3m_size_100k` book against `SPY`.
