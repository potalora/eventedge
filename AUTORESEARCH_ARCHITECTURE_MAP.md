# EventEdge Autoresearch Architecture (`gen_003`)

## Daily session lifecycle

The systemd timer in `deploy/systemd/trade.timer` wakes at 18:00 `America/New_York` Monday–Friday via `scripts/recover_runtime.py`. Both the recovery controller and daily CLIs validate the exact requested `XNYS` trading session (never rolling holidays or weekends).

For each valid session, `CohortOrchestrator` (`tradingagents/strategies/orchestration/cohort_orchestrator.py`) executes in strict execution-first order:

1. **Tier-1 market data validation:** Validate the complete exact-session `alpaca_sip` raw bars and `yfinance_actions_benchmarks` corporate actions and benchmark closes before any economic mutation.
2. **P0 accounting & execution (`SessionExecutor`):** Apply corporate actions (splits before post-split distributions), settle prior next-open exits and open-gap stops, then execute due T+1 opening entries (with deterministic open-gap quantity trimming up to `max_open_chase_pct = 0.05`, rejecting if `fill_qty < 1` or gap > 5%). Intraday stops settle afterward.
3. **Carry, marks & snapshots:** Accrue calendar-day borrow and financing charges on prior accepted closing exposure, mark positions, record `SPY`/`BIL`/`VTI`/`VT` total-return benchmarks, and snapshot each cohort's account.
4. **Shared Tier-2 acquisition & screening:** Instantiate `DataAcquisitionService` (`tradingagents/strategies/orchestration/data_acquisition.py`) **once per daily run** (not per cohort), fetch Tier-2 strategy and optional enrichment data, freeze the shared source bundle (Parquet via `pyarrow` with gzipped JSON fallback), and screen the 5 active strategies across the 4 horizons (`30d`, `3m`, `6m`, `1y`).
5. **1-call-per-horizon committee & deterministic sizing:** Call `PortfolioCommittee.synthesize_horizon()` once per horizon (4 calls/day total using `gpt-6.2-sol`), then apply deterministic per-tier `PortfolioPolicy` sizing (continuous inverse-volatility cap from position #1) and `RiskGate` limits across all 16 books to stage next-open intents.
6. **Worker IPC & reporting:** `scripts/run_cohorts.py` writes structured JSON outcomes atomically via `--result-file` (`_io.atomic_write_json`), decoupled from stdout/stderr. `scripts/generate_operational_report.py` emits deterministic JSON and Markdown reports under `docs/reports/`.

## Core modules and state authority

- **Typed configuration (`tradingagents/strategies/config.py`):** `GenerationConfig` (loaded via `load_generation_config()`, canonical profile in `config/profiles/gen003-clean.json`) replaces untyped dictionaries with frozen, fail-closed dataclasses (`PaperLedgerConfig`, `PortfolioPolicyConfig`, `RiskDisciplineConfig`, `ShortSellingConfig`, `RiskGateSettings`, `FinnhubReliabilityConfig`) and deterministic SHA-256 identity.
- **Shared stdlib I/O (`tradingagents/strategies/orchestration/_io.py`):** Stdlib-only primitives for atomic writes (`atomic_write_json`), strict JSON loading (`load_json_strict`), SQLite connections (`open_sqlite`), `Connection.backup()` snapshotting (`snapshot_sqlite`), git worktree inspection, and `SOURCE_TAXONOMY`.
- **Unified `generation.sqlite3` (`tradingagents/strategies/state/generation_db.py`):** Replaces the legacy 17-SQLite-file layout (16 `portfolio.db` + `metrics_v2.sqlite3`) with one WAL-mode `generation.sqlite3` per generation (`DEFAULT_BUSY_TIMEOUT_MS = 10000`), separating 14 shared once-per-session tables from 17 cohort-scoped tables indexed by `cohort_id`.
- **Modular ledger (`tradingagents/strategies/state/ledger/`):** `PortfolioLedger` (`tradingagents/strategies/state/portfolio_ledger.py`) delegates to `connection.py`, `book.py`, `corporate_actions.py`, `session_lifecycle.py`, and `policy_audit.py`. Preserves `Decimal` FIFO lot accounting, 10-phase digest invariants, T+1 open trimming (`FillResult.trimmed_at_open`, `requested_qty`), and cohort-scoped `policy_audit_records`.
- **Recovery & metrics (`tradingagents/strategies/execution/recovery_evidence.py`, `tradingagents/strategies/metrics/`):** `RecoveryEvidenceStore` persists `bar_recoveries`, `candidate_input_issues`, and normalized `candidate_signal_identity_bindings` / `candidate_signal_identity_rows`. `MetricStore` and `MetricsService` read `generation.sqlite3` with batched SQL queries.
- **Direct sources & enrichment (`tradingagents/strategies/data_sources/`):** `enrichment_source.py` (`EnrichmentSource`) provides direct Yahoo profiles, FINRA Query API short interest (dynamic `settlementDate` freshness), and optional Ken French factors without OpenBB SDK. `noaa_yearly_source.py` fetches NCEI Access v1 daily summaries for 22 Corn Belt ASOS stations (Apr–Sep; `off_season_skipped` Oct–Mar). `finnhub_source.py`, `cftc_source.py`, `usaspending_source.py`, `usda_source.py`, `drought_monitor_source.py`, and `fred_source.py` are registered in `registry.py`.
- **LLM transport (`tradingagents/strategies/chatgpt_plan_transport.py`, `transport_worker.py`, `runtime_deadline.py`):** `chatgpt_plan` OAuth transport with `TransportWorkerPool` (`--serve` NDJSON loop, `max_workers=32`, hard `SIGKILL` timeouts) routing `gpt-6.2-sol` (`high`) for thesis/committee synthesis and `gpt-6-luna` (`high`) for bounded `commodity_macro` and `weather_ag` assessments.

## Tier-1 vs. Tier-2 fault isolation

- **Tier-1 (fail-closed):** `alpaca_sip`, `yfinance_actions_benchmarks`, `SessionExecutor`, and `generation.sqlite3` accounting invariants. `DataAcquisitionService.fetch_all_data()` rejects Tier-1 sources upfront. Missing held/due/benchmark prices or corporate actions halt execution for that book/session rather than guessing or falling back.
- **Tier-2 (per-strategy circuit breakers):** Strategy sources (`finnhub`, `yfinance_research`, `usaspending`, `noaa`, `usda`, `drought_monitor`, `cftc`, `fred`) and optional enrichment (`yfinance_profile`, `finra_short_interest`, `ken_french`) use `_io.SOURCE_TAXONOMY` and `evaluate_strategy_circuit_breaker()`. Failures mark only affected strategies `unavailable`, never aborting sibling strategies, invalidating Tier-1 accounting, or wedging runtime recovery.

## Quiet runtime recovery and preflight

- **Preflight (`tradingagents/strategies/orchestration/preflight_state.py`):** Uses `_io.snapshot_sqlite()` (`Connection.backup()`) for read-only inspection, eliminating non-empty `-wal` false positives while enforcing fail-closed post-yield topology checks. `scripts/daily_trading.sh` runs the `governed` preflight gate directly before `run-daily`.
- **Recovery (`scripts/recover_runtime.py`):** Short-circuits idle timer ticks with zero git forks, zero SQLite opens, and zero fsyncs. When launching workers, it passes the inherited exclusive runtime lock file descriptor via `EVENTEDGE_RUNTIME_LOCK_FD` / `pass_fds` to eliminate TOCTOU races.

## Portfolio scenarios

EventEdge runs 16 dependent scenario books across 4 horizons (`30d`, `3m`, `6m`, `1y`) × 4 sizes (`5k`, `10k`, `50k`, `100k`). The primary forward test is `horizon_3m_size_100k`; the remaining 15 books are dependent sensitivity scenarios sharing the same source fetches and horizon committee decisions. Production automated learning is disabled. Generations are frozen git worktrees with isolated `generation.sqlite3` state; a branch, PR, or readiness check is not a live deployment.
