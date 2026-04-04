# Autoresearch Multi-Strategy Architecture

## Overview

The autoresearch system autonomously discovers, evolves, and paper-trades strategies. Phase 1 infrastructure exists for offline backtest evolution but is currently dormant (no active backtest strategies). Phase 2 is a **2-cohort paper trading trial**: Cohort A (Control) uses fixed confidence and no learning; Cohort B (Adaptive) uses journal-derived confidence, prompt optimization, and weekly learning. The system runs **10 active strategy modules** across **12 data sources**. The portfolio committee is the sole sizing authority.

**Performance:** ~48 seconds/generation, ~$0.03 LLM cost, <1GB peak memory on M4 MacBook Air.

### Two Phases

| Phase | Mode | Weight Adjustment | Output |
|-------|------|-------------------|--------|
| **Phase 1: Backtest Evolution** | Offline, many generations | Aggressive (x1.05/x0.95) | Playbook: optimized params, regime model, reliability scores |
| **Phase 2: 2-Cohort Trial** | Live, ongoing | Conservative (x1.02/x0.98, >=20 trades, Adaptive only) | Validated signals, round-trip P&L, cohort comparison |

### Two Execution Tracks

| Track | Strategies | Signal Source | Evaluation |
|-------|-----------|--------------|------------|
| **Backtest** | 3 archived (not active) | Price data + indicators | Walk-forward simulation |
| **Paper-Trade** | 7 active LLM-signal strategies | Events + LLM analysis | Live signal recording |

---

## Architecture

```
                    +-------------------------------------+
                    |      Phase 1: Backtest Evolution     |
                    |      (infrastructure exists,         |
                    |       no active strategies)          |
                    |                                     |
                    |  MultiStrategyEngine                |
                    |    +-> DataSourceRegistry (8 src)   |
                    |    +-> RegimeModel                  |
                    |          VIX + credit + yield curve  |
                    +----------------+--------------------+
                                     |
                                Playbook
                         (params, regime, scores)
                                     |
                    +----------------v---------------------+
                    |      Phase 2: 2-Cohort Trial          |
                    |                                       |
                    |  CohortOrchestrator                    |
                    |    +-> Shared Data Fetch               |
                    |                                       |
                    |  +-- Cohort A (Control) ----------+   |
                    |  |  Fixed confidence 0.5           |   |
                    |  |  No learning loop               |   |
                    |  |  State: data/state/control/     |   |
                    |  +--------------------------------+   |
                    |                                       |
                    |  +-- Cohort B (Adaptive) ----------+  |
                    |  |  Journal-derived confidence     |   |
                    |  |  Prompt optimization            |   |
                    |  |  Weekly learning loop           |   |
                    |  |  State: data/state/adaptive/    |   |
                    |  +--------------------------------+   |
                    |                                       |
                    |  SignalJournal (signal_journal.jsonl)  |
                    |  PromptOptimizer (Cohort B only)       |
                    |  PortfolioCommittee (sole sizing auth) |
                    |  CohortComparison (compare + report)  |
                    |  Future: debate validation (TBD)      |
                    |                                       |
                    +----------------+---------------------+
                                     |
                           StateManager (JSON)
                      paper_trades / vintages
```

---

## Generation Lifecycle

### Two-Phase Flow

The system operates in two distinct phases.

### Phase 1: Backtest Evolution (`--phase backtest`)

Phase 1 infrastructure is fully built but currently dormant. The 2 backtest strategies (govt_contracts, state_economics) are archived in `_dormant/` and not registered in `get_all_strategies()`. To activate Phase 1, re-register backtest strategies in `modules/__init__.py`.

When active, Phase 1 runs N generations offline (typically 50+) to produce an optimized playbook:

#### 1. Fetch Data (batched by source)

The engine collects `data_sources` from all active strategies, then fetches from each available source:

| Source | What's Fetched | Used By |
|--------|---------------|---------|
| yfinance | OHLCV prices (batch download), VIX, earnings dates | All strategies (price data for exit checks) |
| finnhub | Earnings transcripts, company news, peer relationships | earnings_call, supply_chain |
| edgar | SEC filings (10-K, 10-Q, Form 4, SC 13D) polled via EventMonitor | filing_analysis, insider_activity |
| regulations | Recently proposed federal rules | regulatory_pipeline |
| courtlistener | Federal court docket search (securities, antitrust) | litigation |
| congress | Congressional stock transaction disclosures (S3 bulk data) | congressional_trades |
| usaspending | Large government contract awards | (archived: govt_contracts) |
| fred | Federal Reserve economic data (rates, spreads) | Regime model |
| noaa | Temperature/precipitation anomalies (Corn Belt GHCND) | weather_ag |
| usda | Weekly crop condition ratings (NASS QuickStats) | weather_ag |
| drought_monitor | Drought severity by state (USDM) | weather_ag |

#### 2. Run Backtest Strategies

For each backtest strategy:
1. Generate parameter sets (default + random perturbations from `get_param_space()`)
2. Walk through trading days: `screen()` for entries, `check_exit()` for exits
3. Compute stats: Sharpe ratio, total return, max drawdown, win rate, profit factor

#### 3. Regime Model Classification

Classifies current market regime using VIX level, credit spreads, and yield curve:
- **Crisis** -- VIX > 30, inverted yield curve
- **Stressed** -- elevated VIX, widening spreads
- **Normal** -- baseline conditions
- **Benign** -- low VIX, tight spreads

#### 4. Produce Playbook

After N generations, outputs: optimized params per strategy, regime model, strategy reliability scores. This playbook feeds Phase 2.

### Phase 2: 2-Cohort Paper Trading Trial

Uses the playbook for live trading via `CohortOrchestrator` (`orchestration/cohort_orchestrator.py`), which runs a shared data fetch then dispatches to two parallel cohorts with separate state directories.

#### Cohort A -- Control (`data/state/control/`)

- **Fixed `strategy_confidence=0.5`** (`adaptive_confidence=False`)
- No learning loop -- weights remain frozen from the playbook
- Serves as the baseline for comparison

#### Cohort B -- Adaptive (`data/state/adaptive/`)

- **Journal-derived `strategy_confidence`** from hit rates (`adaptive_confidence=True`)
- Prompt optimization for LLM calls (via `PromptOptimizer`)
- **Weekly learning loop**: evaluates completed round-trips, conservative weight adjustment (x1.02/x0.98), only for strategies with >= 20 completed trades

#### Shared Across Cohorts

1. Data fetch is shared (single call, results dispatched to both engines)
2. **Portfolio committee is the sole sizing authority** -- `compute_position_size()` and `execute_recommendation()` no longer accept `strategy_weight`. The risk gate only enforces hard limits.
3. Vintage tracking (param sets tagged with vintage ID, trade count, creation date)
4. 15% exploration budget for unproven param sets

#### Cohort Comparison (`orchestration/cohort_comparison.py`)

`CohortComparison.compare()` produces side-by-side metrics; `format_report()` renders a human-readable summary.

#### Future: Debate Validation

Placeholder for a debate-based signal validator. Not built yet.

### Save State

Generation results (scores, trades, weights, signals) saved to `data/state/gen_NNN.json`.

---

## Component Reference

### MultiStrategyEngine (`orchestration/multi_strategy_engine.py`)

Core orchestrator. Key methods:

| Method | Purpose |
|--------|---------|
| `run_generation()` | Run one full generation across all strategies |
| `run()` | Run multiple generations sequentially |
| `_fetch_all_data()` | Batch-fetch from all needed data sources |
| `_run_backtest_strategy()` | Generate params, simulate trades, compute stats |
| `_run_paper_trade_strategy()` | Screen events, record paper trades |
| `_compute_strategy_scores()` | Best Sharpe per strategy |

Constructor accepts: `config`, `strategies` (list), `registry` (DataSourceRegistry), `state_manager`, `on_event` (callback for progress reporting).

### StateManager (`state/state.py`)

JSON-file-based persistence with **atomic writes** (write to temp file, then `os.rename`).

| Method | File(s) |
|--------|---------|
| `save/load_generation()` | `gen_001.json`, `gen_002.json`, ... |
| `save_paper_trade()` | `paper_trades/*.json` |
| `save_leaderboard()` | `leaderboard.json` |
| `save_reflection()` | `reflections.json` |
| `save/load_playbook()` | `playbook.json` |
| `save/load_vintages()` | `vintages.json` |
| `reset()` | Clears all state files |

### PaperTrader (`trading/paper_trader.py`)

Records paper trades from paper-trade strategies and tracks P&L against real prices.

| Method | Purpose |
|--------|---------|
| `open_trade()` | Record new paper trade with entry price and rationale |
| `check_exits()` | Check all open trades against strategy exit rules |
| `close_trade()` | Record trade exit with P&L |
| `get_performance()` | Win rate, avg PnL, Sharpe for completed trades |

### PortfolioCommittee (`trading/portfolio_committee.py`)

Synthesizes signals across multiple strategies into unified position recommendations during Phase 2's trading loop.

| Method | Purpose |
|--------|---------|
| `synthesize_signals(signals, regime)` | Combine signals from multiple strategies using LLM analysis + rule-based fallback |
| `rule_based_fallback(signals)` | Deterministic signal aggregation when LLM is unavailable |

The committee is the **sole sizing authority** -- weight scaling has been removed from `compute_position_size()` and `execute_recommendation()`. The risk gate only enforces hard limits. Falls back to rule-based synthesis if LLM call fails.

### CohortOrchestrator (`orchestration/cohort_orchestrator.py`)

Top-level entry point for the 2-cohort paper trading trial.

| Method | Purpose |
|--------|---------|
| `run_trading(date)` | Shared data fetch, then dispatch to each cohort's engine |
| `run_learning()` | Run learning loop for cohorts that have it enabled (Adaptive only) |

`CohortConfig` defines per-cohort settings (`adaptive_confidence`, `state_dir`, `enable_learning`). `build_default_cohorts()` returns the standard A/B setup.

### CohortComparison (`orchestration/cohort_comparison.py`)

Side-by-side comparison of cohort performance.

| Method | Purpose |
|--------|---------|
| `compare()` | Compute comparative metrics across cohorts |
| `format_report()` | Render human-readable comparison report |

### SignalJournal (`learning/signal_journal.py`)

Append-only JSONL signal log at `{state_dir}/signal_journal.jsonl`. Records every signal (traded or not) and back-fills outcome data (5d/10d/30d returns) on subsequent runs.

| Method | Purpose |
|--------|---------|
| `log_signal(entry)` | Append one `JournalEntry` (strategy, ticker, direction, score, conviction, regime, etc.) |
| `log_signals(entries)` | Append multiple entries |
| `get_entries(strategy, ticker, since)` | Read entries with optional filters |
| `get_convergence_signals(date, min_strategies)` | Find tickers where multiple strategies agree on direction |
| `get_knowledge_gaps(regime)` | Rank strategies by observation count (fewest first) for exploration budget |
| `fill_outcomes(price_cache, today)` | Back-fill return_5d/10d/30d fields once enough calendar days have elapsed |
| `get_high_conviction_failures(strategy, limit, min_conviction)` | Recent high-conviction signals where direction was wrong (feeds prompt optimizer) |

Used by Cohort B for journal-derived `strategy_confidence` (hit rates per strategy).

### PromptOptimizer (`learning/prompt_optimizer.py`)

Atlas-GIC-inspired prompt evolution loop. LLM analyzer prompts are the trainable parameters; signal journal outcomes are the loss function. Active for Cohort B only.

Optimizes prompts for 6 LLM-using strategies: `earnings_call`, `insider_activity`, `filing_analysis`, `regulatory_pipeline`, `supply_chain`, `litigation`.

| Method | Purpose |
|--------|---------|
| `evaluate_prompts(journal)` | Score each strategy's prompt by hit rate, avg return, conviction calibration (min 20 signals) |
| `identify_worst_prompt(scores)` | Return strategy with lowest hit rate (enough data required) |
| `propose_modification(strategy, current_prompt, failures)` | LLM meta-prompt suggests one targeted change based on high-conviction failures |
| `start_trial(strategy, new_prompt)` | Save new prompt as a trial, activate it, record start date |
| `check_trial(trial_id, journal)` | After 5 trading days, compare trial vs baseline hit rate. Returns "keep"/"revert"/"ongoing" |
| `commit_or_revert(trial_id, decision)` | Promote trial prompt to active (keep) or restore baseline (revert). Archives both. |
| `get_active_trial()` | Return current active trial if any |
| `get_prompt_version(strategy)` | Short hash of active prompt (for journal tagging) |

Trial flow: evaluate -> identify worst -> propose modification -> trial for 5 days -> keep if hit rate improves by >=2pp, else revert.

### LLMAnalyzer (`learning/llm_analyzer.py`)

All calls use Haiku (`claude-haiku-4-5-20251001`, ~$0.001/call). Returns structured JSON. Prompts can be overridden at runtime by the PromptOptimizer.

| Method | Strategy | Analysis |
|--------|----------|----------|
| `analyze_filing_change()` | filing_analysis | Score material changes between 10-K/10-Q filings |
| `analyze_insider_context()` | insider_activity | Assess insider buy conviction from Form 4 context |
| `analyze_10b5_1_plan()` | insider_activity | Detect 10b5-1 plan red flags from Form 4 data |
| `analyze_exec_comp()` | (retained, no active strategy) | DEF 14A compensation structure shift signals |
| `analyze_earnings_call()` | earnings_call | Detect tone shifts, deception, guidance revisions |
| `analyze_regulation()` | regulatory_pipeline | Map proposed regulation to affected tickers |
| `analyze_supply_chain()` | supply_chain | Multi-hop supply chain impact assessment |
| `analyze_litigation()` | litigation | Court docket risk assessment |
| `propose_params()` | Evolution | Suggest new parameter combinations |
| `reflect_on_generation()` | Evolution | Cross-strategy performance reflection |

Default prompts are defined in `_DEFAULT_PROMPTS` for: `earnings_call`, `insider_activity`, `filing_analysis`, `regulatory_pipeline`, `supply_chain`, `litigation`. The `get_prompt()` / `set_prompt_override()` interface allows the PromptOptimizer to swap prompts at runtime.

### Vintage Tracking

Each parameter set is tagged with a vintage record:
- **vintage_id** -- unique identifier for the param set
- **trade_count** -- number of trades executed with this param set
- **creation_date** -- when the param set was created/evolved
- **source** -- `"backtest_evolution"` or `"exploration_budget"`

Vintages enable tracking which param sets perform well in live trading vs. backtesting, and gate weight adjustments (>= 20 trades required).

### Regime Model

Classifies market conditions using three inputs: VIX level, credit spreads (HY-IG), and yield curve (10Y-2Y).

| Regime | Conditions | Implications |
|--------|-----------|-------------|
| **Crisis** | VIX > 30, inverted yield | Reduce exposure, favor defensive strategies |
| **Stressed** | Elevated VIX, widening spreads | Cautious allocation |
| **Normal** | Baseline conditions | Standard allocation |
| **Benign** | Low VIX, tight spreads | Favor momentum/risk-on strategies |

### EventMonitor (`learning/event_monitor.py`)

Polls data sources for actionable events. Each poll returns structured event dicts.

| Method | Source | Used By |
|--------|--------|---------|
| `poll_edgar_filings()` | EDGAR EFTS | filing_analysis, insider_activity |
| `poll_13d_filings()` | EDGAR | (archived: govt_contracts) |
| `poll_form4_filings()` | EDGAR | insider_activity |
| `poll_large_contracts()` | USAspending | (archived: govt_contracts) |
| `poll_proposed_rules()` | Regulations.gov | regulatory_pipeline |
| `poll_court_dockets()` | CourtListener | litigation |
| `poll_congressional_trades()` | Congress (S3) | congressional_trades |
| `poll_all()` | All sources | Full generation |

---

## Data Sources

### Registry (`data_sources/registry.py`)

`DataSource` protocol requires: `name`, `requires_api_key`, `fetch(params)`, `is_available()`.

`build_default_registry(config)` creates a registry with all 12 sources pre-registered (including OpenBB, NOAA, USDA, Drought Monitor).

### Source Reference

| Source | File | API Key | Config Field | Rate Limit |
|--------|------|---------|-------------|------------|
| yfinance | `yfinance_source.py` | No | -- | None (Yahoo) |
| edgar | `edgar_source.py` | No | `edgar_user_agent` | 10 req/sec |
| usaspending | `usaspending_source.py` | No | -- | None |
| congress | `congress_source.py` | No | -- | None (S3) |
| fred | `fred_source.py` | Yes (free) | `fred_api_key` | 120 req/min |
| finnhub | `finnhub_source.py` | Yes (free) | `finnhub_api_key` | 60 calls/min |
| regulations | `regulations_source.py` | Yes (free) | `regulations_api_key` | 1,000 req/hr |
| courtlistener | `courtlistener_source.py` | Yes (free) | `courtlistener_token` | 5,000 req/hr |
| noaa | `noaa_source.py` | Yes (free) | `noaa_cdo_token` | 5 req/sec |
| usda | `usda_source.py` | Yes (free) | `usda_nass_api_key` | 50k records/req |
| drought_monitor | `drought_monitor_source.py` | No | -- | None |
| openbb | `openbb_source.py` | Yes (free) | `fmp_api_key` | 250 calls/day |

API keys are set in `.env` and loaded via `python-dotenv`. `run_generations.py` populates the config from environment variables before building the registry.

---

## Strategy Framework

### StrategyModule Protocol (`modules/base.py`)

```python
class StrategyModule(Protocol):
    name: str           # Unique identifier
    track: str          # "backtest" or "paper_trade"
    data_sources: list[str]  # Registry source names

    def get_param_space(self) -> dict[str, tuple]: ...      # Evolvable ranges
    def get_default_params(self) -> dict[str, Any]: ...     # Defaults
    def screen(self, data, date, params) -> list[Candidate]: ...  # Entry signals
    def check_exit(self, ticker, entry_price, current_price,
                   holding_days, params, data) -> tuple[bool, str]: ...
    def build_propose_prompt(self, context) -> str: ...     # LLM evolution prompt
```

**Data classes:** `Candidate` (ticker, date, direction, score, metadata), `StrategyParams` (id, strategy_name, params, generation, fitness, weight), `BacktestTrade`, `BacktestResult`.

### Active Paper-Trade Strategies (7)

| Strategy | Class | File | Data Sources | Signal |
|----------|-------|------|-------------|--------|
| earnings_call | `EarningsCallStrategy` | `earnings_call.py` | finnhub, yfinance | Earnings transcript analysis |
| insider_activity | `InsiderActivityStrategy` | `insider_activity.py` | edgar, yfinance | Form 4 buy clusters + sell patterns |
| filing_analysis | `FilingAnalysisStrategy` | `filing_analysis.py` | edgar, yfinance | 10-K/10-Q material change detection |
| regulatory_pipeline | `RegulatoryPipelineStrategy` | `regulatory_pipeline.py` | regulations, yfinance | Proposed rules mapped to affected tickers |
| supply_chain | `SupplyChainStrategy` | `supply_chain.py` | finnhub, yfinance | Multi-hop supply chain disruption |
| litigation | `LitigationStrategy` | `litigation.py` | courtlistener, yfinance | Federal court docket monitoring |
| congressional_trades | `CongressionalTradesStrategy` | `congressional_trades.py` | congress, yfinance | Congressional stock transaction tracking |

All 7 strategies are registered in `modules/__init__.py` via `get_paper_trade_strategies()` and `get_all_strategies()`.

See [docs/strategy_research.md](docs/strategy_research.md) for academic backing and detailed signal logic.

### Archived Backtest Strategies (3)

Phase 1 is dormant. Two backtest strategies exist as code in `_dormant/` but are not registered or active:

| Strategy | Class | File | Data Sources | Signal |
|----------|-------|------|-------------|--------|
| govt_contracts | `GovtContractsStrategy` | `_dormant/govt_contracts.py` | yfinance, usaspending | Defense contractor momentum proxy |
| state_economics | `StateEconomicsStrategy` | `_dormant/state_economics.py` | yfinance | Regional ETF rotation |

The remaining 7 originally planned backtest strategies (B1-B4, B6-B8 from the design spec: factor_momentum, cross_asset_momentum, vix_mean_reversion, pead, activist_13d, credit_spread, economic_surprise) were never implemented.

---

## Archived Components

### AutoresearchLoop (`_dormant/autoresearch_loop.py`)

Evolution wrapper using the Atlas-GIC **keep/revert mutation pattern**. This module exists in the `_dormant/` directory and is not used by the active 2-cohort trial. It wraps `MultiStrategyEngine` for single-engine LLM-driven parameter evolution:

1. `identify_weakest()` -- find lowest-weight strategy
2. `_propose_mutation()` -- LLM suggests one targeted parameter change
3. Run N generations with the mutation applied
4. `_evaluate_mutation()` -- compare before/after performance
5. If improved -> keep. If not -> `_revert_mutation()`

Superseded by `CohortOrchestrator` + `PromptOptimizer` for the 2-cohort trial. May be reactivated if Phase 1 backtest evolution is resumed.

### CachedPipelineRunner (`_dormant/cached_pipeline.py`)

Cache-first wrapper around `TradingAgentsGraph.propagate()`. Runs the full core pipeline with a SQLite cache layer. Superseded by the multi-strategy engine for autoresearch use cases.

---

## Configuration Reference

All autoresearch config lives in `default_config.py["autoresearch"]`:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `state_dir` | str | `"data/state"` | JSON state directory |
| `total_capital` | int | `5000` | Portfolio size for allocation |
| `proposals_per_strategy` | int | `3` | Param sets tested per strategy per generation |
| `exploration_budget` | float | `0.15` | Fraction of capital for unproven param sets |
| `adaptive_confidence` | bool | `False` | `False` = fixed 0.5 (Control), `True` = journal-derived (Adaptive) |
| `autoresearch_model` | str | `"claude-haiku-4-5-20251001"` | LLM for all autoresearch calls |
| `fred_api_key` | str | `""` | FRED API key (free from fred.stlouisfed.org) |
| `finnhub_api_key` | str | `""` | Finnhub API key (free from finnhub.io) |
| `regulations_api_key` | str | `""` | Regulations.gov key (free from api.data.gov) |
| `courtlistener_token` | str | `""` | CourtListener token (free from courtlistener.com) |
| `edgar_user_agent` | str | `"TradingAgents ..."` | EDGAR requires User-Agent with name + email |

---

## CLI

### Generation Management (`scripts/run_generations.py`)

Primary entry point for running multiple code versions in parallel. See the Generation Management section in CLAUDE.md for full usage.

```
python scripts/run_generations.py start "Description"       # snapshot current code
python scripts/run_generations.py run-daily [--date DATE]   # run all active generations
python scripts/run_generations.py run-learning              # learning loop for all active gens
python scripts/run_generations.py compare [--gens g1,g2]    # compare across generations
python scripts/run_generations.py list                      # list all generations
```

---

## Legacy Single-Strategy System

The following files implement the **original** single-strategy autoresearch system. `evolution.py`, `screener.py`, and `fitness.py` are still used by `MultiStrategyEngine`. The remaining files are in `_dormant/`:

| File | Status | Purpose |
|------|--------|---------|
| `_dormant/evolution.py` | **Active** (used by MultiStrategyEngine) | Single-strategy evolution loop (screener -> strategist -> backtest -> rank) |
| `_dormant/screener.py` | **Active** (used by MultiStrategyEngine) | Market screening with 30+ technical indicators |
| `_dormant/fitness.py` | **Active** (used by MultiStrategyEngine) | Sharpe-based fitness scoring and ranking |
| `_dormant/strategist.py` | Legacy | LLM-based strategy proposal + CRO adversarial review |
| `_dormant/fast_backtest.py` | Legacy | Single-LLM-call backtest alternative |
| `_dormant/cached_pipeline.py` | **Superseded** | Cache wrapper around TradingAgentsGraph pipeline |
| `_dormant/autoresearch_loop.py` | **Superseded** | Atlas-GIC keep/revert mutation wrapper (see Archived Components) |
| `_dormant/walk_forward.py` | Legacy | Walk-forward window generation |
| `_dormant/ticker_universe.py` | Legacy | Predefined ticker universes |

See `docs/superpowers/specs/2026-03-29-autoresearch-design.md` for the original design spec.
