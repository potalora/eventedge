# TradingAgents - Development Guide

## Project Overview

TradingAgents is a multi-agent LLM trading framework built with LangGraph. It has two major systems:

1. **Core Pipeline** — 6-agent LangGraph pipeline (analysts, researchers, trader, risk manager, portfolio manager) that evaluates a single ticker and produces a trade decision.
2. **Autoresearch** — Autonomous multi-strategy evolution engine: Phase 1 infrastructure exists but is dormant (no backtest strategies registered); Phase 2 is a 2-cohort paper trading trial (Control with fixed confidence vs. Adaptive with journal-derived confidence and weekly learning). 10 event-driven strategies across 12 data sources (including OpenBB Platform for sector classification, analyst estimates, short interest, government trades, SEC litigation, and Fama-French factors; NOAA CDO for agricultural weather anomalies; USDA NASS for crop conditions; US Drought Monitor for drought severity).

Additionally, the project includes: options analysis, backtesting engine, Alpaca execution, Streamlit dashboard, APScheduler scheduling, and SQLite persistence.

## Quick Reference

```bash
# Install
pip install .                    # from repo root with .venv active
pip install -e .                 # editable install for development

# Test
.venv/bin/python -m pytest tests/ -v

# Core pipeline CLI
.venv/bin/python -m cli.main

# Generation management (parallel code versions) — primary entry point
python scripts/run_generations.py start "Description"       # snapshot current code as new generation
python scripts/run_generations.py run-daily [--date DATE]   # run all active generations
python scripts/run_generations.py run-learning              # learning loop for all active gens
python scripts/run_generations.py compare [--gens g1,g2]    # compare across generations
python scripts/run_generations.py list                      # list all generations
python scripts/run_generations.py pause gen_001             # pause a generation
python scripts/run_generations.py resume gen_001            # resume a paused generation
python scripts/run_generations.py retire gen_001            # retire (delete worktree, keep state)

# Daily report (auto-runs after daily_trading.sh, or standalone)
python scripts/generate_daily_report.py [--date DATE]      # generate markdown report for all gens

# Dashboard
.venv/bin/python -m streamlit run tradingagents/dashboard/app.py
```

## Architecture Overview

```
CORE PIPELINE (LangGraph)                 AUTORESEARCH
─────────────────────────                 ──────────────────────────────
Fundamental Analyst ──┐                   DataSourceRegistry (9 sources)
Sentiment Analyst  ───┤                            │
News Analyst       ───┤──► Bull/Bear          StrategyModules (9)
Technical Analyst  ───┤    Researchers              │
Options Analyst    ───┘         │       ┌───── Phase 1: Backtest ─────┐
                           Trader       │  (dormant — no strategies   │
                              │         │   currently registered)     │
                        Risk Manager    │  N generations → Playbook   │
                              │         └─────────────┬───────────────┘
                      Portfolio Manager          Playbook (params,
                              │                   regime, scores)
                        Trade Decision              │
                                          ┌── 16 Cohorts (Horizon × Size) ─┐
                                          │  4 horizons: 30d, 3m, 6m, 1y  │
CohortOrchestrator ── Shared Data Fetch ──┤  4 sizes: 5k, 10k, 50k, 100k  │
          │                               │  = 16 independent portfolios   │
     4 horizon screens                    │  Adaptive/learning: dormant    │
                                          │  50k+/3m+: shorts eligible     │
                                          │  10k+: covered calls eligible  │
                                          └────────────────────────────────┘
                                              StateManager (JSON)

GENERATION MANAGEMENT
─────────────────────
GenerationManager ── git worktree per code version
  gen_001/ ── CohortOrchestrator (control + adaptive)
  gen_002/ ── CohortOrchestrator (control + adaptive)
  ...
State: data/generations/gen_NNN/{control,adaptive}/
Code:  .worktrees/gen_NNN/ (frozen git commit)

EXTENSIONS
──────────
storage/     — SQLite persistence (db.py, queries.py)
backtesting/ — Walk-forward engine, metrics, portfolio, reports
execution/   — Broker abstraction (base_broker.py → alpaca_broker.py, paper_broker.py)
dashboard/   — Streamlit UI (app.py, components/, pages/)
scheduler/   — APScheduler jobs + alert channels (apprise)
```

---

## Core Pipeline Patterns

### Analyst Pattern (`tradingagents/agents/analysts/*.py`)

1. Factory function `create_X_analyst(llm)` returns a closure `X_analyst_node(state) -> dict`
2. Extract `trade_date`, `company_of_interest` from state
3. Build instrument context via `build_instrument_context()`
4. Define tools list, create `ChatPromptTemplate`, bind tools to LLM
5. Invoke chain, return `{"messages": [result], "X_report": report}`

Analysts: `fundamental`, `sentiment`, `news`, `technical`, `options`

### Tool Definition Pattern (`tradingagents/agents/utils/*_tools.py`)

- Use `@tool` decorator from `langchain_core.tools`
- Use `Annotated` type hints with descriptions
- Route through `route_to_vendor()` from `tradingagents/dataflows/interface.py`

### Data Vendor Pattern (`tradingagents/dataflows/interface.py`)

- Register in `VENDOR_METHODS` dict: `"method_name": {"vendor": impl_function}`
- Add to `TOOLS_CATEGORIES` dict for category-level routing
- Implement vendor function in `tradingagents/dataflows/` module

### Graph Registration (`tradingagents/graph/setup.py`)

- Analysts are conditionally added based on `selected_analysts` list
- Each analyst gets: node, tool_node, msg_clear node, conditional edges
- Analysts run sequentially, last one connects to Bull Researcher

### State (`tradingagents/agents/utils/agent_states.py`)

- `AgentState(MessagesState)` holds all reports and debate states
- Fields: `market_report`, `sentiment_report`, `news_report`, `fundamentals_report`, `options_report`
- Add new fields as `Annotated[str, "description"]`

### Exports (`tradingagents/agents/__init__.py`)

- All agent factory functions must be exported here

---

## Autoresearch System

Full architecture reference: [AUTORESEARCH_ARCHITECTURE_MAP.md](AUTORESEARCH_ARCHITECTURE_MAP.md)

Strategy research & academic backing: [docs/strategy_research.md](docs/strategy_research.md)

### Architecture

**Phase 1 — Backtest Evolution** (offline, fast): Infrastructure exists but is dormant — no backtest strategies are currently registered. The system runs Phase 2 (paper trading) only.

**Phase 2 — 16-Cohort Paper Trading Matrix** (live, ongoing): Horizon × size matrix with 4 investment horizons (30d, 3m, 6m, 1y) × 4 portfolio sizes ($5k, $10k, $50k, $100k) = 16 independent paper portfolios. All share data fetch; screening runs once per horizon (4 passes). Each cohort has its own `PortfolioSizeProfile` controlling position sizing, concentration limits, and cash reserves. Adaptive confidence and learning infrastructure is dormant but preserved. 50k+/3m+ cohorts support short equity; 10k+ cohorts support covered call overlays.

### Active Strategies (10 event-driven, paper-trade only)

| Strategy | Class | Data Sources |
|----------|-------|-------------|
| `earnings_call` | `EarningsCallStrategy` | Finnhub, yfinance, OpenBB (analyst consensus) |
| `insider_activity` | `InsiderActivityStrategy` | EDGAR Form 4, yfinance, OpenBB (officer titles, sector) |
| `filing_analysis` | `FilingAnalysisStrategy` | EDGAR 10-K/10-Q, yfinance, OpenBB (analyst estimates, sector) |
| `regulatory_pipeline` | `RegulatoryPipelineStrategy` | Regulations.gov, yfinance, OpenBB (sector validation) |
| `supply_chain` | `SupplyChainStrategy` | Finnhub, yfinance, OpenBB (short interest, sector) |
| `litigation` | `LitigationStrategy` | CourtListener, yfinance, OpenBB (SEC litigation) |
| `congressional_trades` | `CongressionalTradesStrategy` | Congress/CapitolTrades, yfinance, OpenBB (govt trades API as primary) |
| `govt_contracts` | `GovtContractsStrategy` | USASpending, yfinance, OpenBB (profile, estimates) |
| `state_economics` | `StateEconomicsStrategy` | FRED, yfinance, OpenBB (Fama-French factors) |
| `weather_ag` | `WeatherAgStrategy` | NOAA CDO, USDA NASS, Drought Monitor, yfinance, OpenBB (weather + crop conditions + drought + ag momentum) |

### Report Generation

Reports are not script-generated. Ask Claude to generate reports by reading state directories and calling `CohortComparison` methods (`compare()`, `compare_by_horizon()`, `compare_by_size()`, `heatmap()`).

### Key Components

- `strategies/orchestration/cohort_orchestrator.py` — `CohortOrchestrator` runs shared data fetch, screens per horizon (4 passes), then dispatches to 16 cohorts with size-appropriate configs. `CohortConfig`, `PortfolioSizeProfile`, `SIZE_PROFILES`, `HORIZON_PARAMS`, and `build_default_cohorts()` define the matrix.
- `strategies/orchestration/cohort_comparison.py` — `CohortComparison` with `compare()`, `compare_by_horizon()`, `compare_by_size()`, and `heatmap()` for matrix analysis.
- `strategies/trading/portfolio_committee.py` — LLM + rule-based signal synthesis (sole sizing authority). Accepts `enrichment` dict with sector profiles, short interest, and factor data. Enforces sector concentration limits (default 30%) when sector data is available. Performs vehicle selection (long equity / short equity / covered call) based on cohort eligibility flags and signal direction. Applies short book limits and covered call overlay for eligible cohorts.
- Vintage tracking (param sets tagged with vintage ID, trade count, creation date), regime model, 15% exploration budget for unproven param sets.
- Future bolt-on: debate validation (not built yet).

### Generation Management

The generation system allows multiple versions of the codebase to run paper trading in parallel, each with isolated state. This enables A/B testing of code changes (e.g., new strategies, enrichment logic, parameter changes) against a baseline.

**How it works:**
1. `start` — Snapshots current HEAD commit into a detached git worktree (`.worktrees/gen_NNN/`). Creates isolated state directory (`data/generations/gen_NNN/{control,adaptive}/`).
2. `run-daily` — Iterates all active generations. Each gen runs its frozen codebase in its worktree, writing results to its own state dir. Both cohorts (control + adaptive) share data fetch but execute independently.
3. `compare` — Reads state dirs across generations and produces a side-by-side comparison of signals, trades, hit rates, Sharpe ratios, and returns.
4. `retire` — Archives a generation (keeps state, optionally deletes worktree).

**Key files:**
- `tradingagents/strategies/orchestration/generation_manager.py` — `GenerationManager` class, manifest persistence
- `tradingagents/strategies/orchestration/generation_comparison.py` — Cross-generation comparison
- `scripts/run_generations.py` — CLI entry point
- `scripts/generate_daily_report.py` — Generates daily markdown reports to `docs/reports/`
- `scripts/daily_trading.sh` — Shell wrapper for cron/launchd (runs daily + generates report)

**Current active generations:**
- `gen_001` — 7-strategy baseline (commit `5f3730d`), started 2026-04-01
- `gen_002` — 9-strategy OpenBB enrichment (commit `a0a4c7a`), started 2026-04-03
- `gen_003` — 10-strategy ag enhancement: USDA + Drought Monitor + expanded tickers (commit `b368114`), started 2026-04-04
- `gen_004` — Gate loosening, universe expansion, 30-day cycle evaluation (commit `3b71fd1`), started 2026-04-04
- `gen_005` — 16-cohort matrix, options & short selling Wave 1, Sonnet LLM (commit `216b990`), started 2026-04-05

**Note:** All strategies now target a 30-day investment horizon. Default `hold_days` / `rebalance_days` are in the 20-30 day range; param space floors are all >= 20 days and ceilings <= 45 days. CycleTracker produces 30-day boundary snapshots aligned to each generation's start date.

### Adding a New Strategy

1. Create `tradingagents/strategies/modules/my_strategy.py`
2. Implement the `StrategyModule` protocol from `modules/base.py`:
   - `name: str` — unique identifier (e.g. `"my_strategy"`)
   - `track: str` — `"backtest"` or `"paper_trade"`
   - `data_sources: list[str]` — registry source names needed (e.g. `["yfinance", "fred"]`)
   - `get_param_space() -> dict[str, tuple]` — evolvable parameter ranges
   - `get_default_params() -> dict[str, Any]` — sensible defaults
   - `screen(data, date, params) -> list[Candidate]` — screen for entry signals
   - `check_exit(ticker, entry_price, current_price, holding_days, params, data) -> (bool, str)` — exit logic
   - `build_propose_prompt(context) -> str` — LLM prompt for parameter evolution
3. Register in `modules/__init__.py`:
   - Add import
   - Add to `__all__`
   - Add instance to `get_backtest_strategies()` or `get_paper_trade_strategies()`

### Adding a New Data Source

1. Create `tradingagents/strategies/data_sources/my_source.py`
2. Implement the `DataSource` protocol from `data_sources/registry.py`:
   - `name: str` — registry key (e.g. `"my_source"`)
   - `requires_api_key: bool`
   - `fetch(params: dict) -> dict` — generic fetch dispatcher (`params["method"]` selects endpoint)
   - `is_available() -> bool` — check API key and dependencies
3. Register in `data_sources/registry.py` `build_default_registry()`
4. Add to `data_sources/__init__.py` exports
5. Add API key to `default_config.py` autoresearch section and `.env.example`

---

## Extension Modules

| Module | Path | Config Key | Description |
|--------|------|-----------|-------------|
| Storage | `tradingagents/storage/` | — | SQLite persistence for results and trades |
| Backtesting | `tradingagents/backtesting/` | `backtest` | Walk-forward engine, metrics, portfolio simulation |
| Execution | `tradingagents/execution/` | `execution` | Broker abstraction: `paper_broker.py`, `alpaca_broker.py` |
| Dashboard | `tradingagents/dashboard/` | — | Streamlit UI for monitoring positions and signals |
| Scheduler | `tradingagents/scheduler/` | `scheduler` | APScheduler jobs with alert channels via apprise |

---

## Config

All configuration lives in `tradingagents/default_config.py`.

| Section | Key | Description |
|---------|-----|-------------|
| (root) | `llm_provider` | LLM backend: `"openai"`, `"anthropic"`, `"google"`, `"xai"` |
| (root) | `deep_think_llm` | Model for complex reasoning |
| (root) | `quick_think_llm` | Model for fast tasks |
| `data_vendors` | `core_stock_apis` | Data backend: `"yfinance"` or `"alpha_vantage"` |
| `options` | `allowed_strategies` | Permitted options strategies |
| `backtest` | `initial_capital` | Starting capital for backtests |
| `execution` | `mode` | `"paper"` or `"live"` |
| `execution` | `broker` | `"alpaca"` |
| `scheduler` | `scan_time` | Daily scan time (US/Eastern) |
| `autoresearch` | `state_dir` | JSON state directory (default: `data/state`) |
| `autoresearch` | `autoresearch_model` | LLM for all autoresearch calls (Sonnet) |
| `autoresearch` | `total_capital` | Portfolio size for allocation |
| `autoresearch` | `adaptive_confidence` | `False` = fixed 0.5 (Cohort A), `True` = journal-derived (Cohort B) |
| `autoresearch` | `fmp_api_key` | FMP API key for OpenBB equity estimates (optional, free tier: 250 calls/day) |
| `autoresearch` | `noaa_cdo_token` | NOAA Climate Data Online token for weather_ag strategy (free from ncdc.noaa.gov) |
| `autoresearch` | `usda_nass_api_key` | USDA NASS API key for crop condition data (free from quickstats.nass.usda.gov) |
| `autoresearch.short_selling` | `borrow_cost_tiers` | Borrow cost tiers (cheap/normal/expensive/HTB) used for risk gate rejection |
| `autoresearch.short_selling` | `rejection_threshold` | Max allowed borrow cost tier before short is rejected |
| `options` | `covered_call_delta` | Target delta for covered call strike selection (default: 0.30) |
| `options` | `covered_call_min_premium_pct` | Minimum premium as % of stock price to place covered call (default: 0.005) |

API keys go in `.env` (git-ignored), loaded via `python-dotenv`. See `.env.example` for required keys.

OpenBB is an optional dependency: `pip install -e ".[openbb]"`. All strategies gracefully degrade when OpenBB is unavailable. NOAA weather data gracefully degrades to momentum-only when the token is unavailable.

---

## LLM Provider

- **Primary:** Anthropic Claude
- `deep_think_llm`: `claude-sonnet-4-20250514`
- `quick_think_llm`: `claude-haiku-4-5-20251001`
- **Autoresearch:** Sonnet for all LLM calls (~$0.03/generation, ~$0.001/call)
- Multi-provider support: OpenAI, Google, Anthropic, xAI, Ollama

---

## Development Constraints

Every change — new features, refactors, data pipeline additions — must go through an optimization cycle:

1. **Profile for bottlenecks** — measure wall time, memory, and API cost before and after.
2. **Optimize for cost** — minimize LLM token usage and external API calls. Shared data fetch, prompt truncation, caching.
3. **Optimize for performance on 16GB M4 MacBook Air** — no unbounded in-memory data structures, stream/truncate large responses, keep peak RSS well under 8GB.

This applies to all components: data sources, strategies, LLM prompts, and orchestration.

---

## Upstream Sync

This fork extends [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents). Periodically scan upstream merges for:

- New analyst agents or tool improvements in `tradingagents/agents/`
- Data vendor updates in `tradingagents/dataflows/`
- LLM client improvements in `tradingagents/llm_clients/`
- Graph pipeline optimizations in `tradingagents/graph/`
- Bug fixes in core infrastructure

Check upstream with: `git fetch upstream && git log upstream/main --oneline -20`

---

## Testing

- **Framework:** pytest
- **Location:** `tests/` mirroring source structure
- **Run:** `.venv/bin/python -m pytest tests/ -v`
- **Rules:** Mock LLM calls in unit tests. Never call real APIs in tests. Use `unittest.mock.patch` for external services.
- **Key test files:**
  - `tests/test_multi_strategy.py` — tests covering 10 strategies, state, engine
  - `tests/test_openbb_source.py` — 37 tests for OpenBB data source (all 8 methods, cache, graceful degradation)
  - `tests/test_weather_ag.py` — 36 tests for weather_ag strategy (gates, tickers, seasons, LLM metadata, crop decline)
  - `tests/test_usda_source.py` — 14 tests for USDA NASS data source
  - `tests/test_drought_monitor_source.py` — 13 tests for US Drought Monitor data source
  - `tests/test_30day_simulation.py` — 30-day simulation tests including OpenBB enrichment and reactivated strategies
  - `tests/test_e2e_pipeline.py` — End-to-end integration tests for full trading pipeline
  - `tests/test_option_spec.py` — OptionSpec dataclass and Candidate vehicle field
  - `tests/test_eligibility.py` — PortfolioSizeProfile eligibility flags (long-only, covered calls, shorts)
  - `tests/test_short_risk_gates.py` — Short-specific risk gates (earnings blackout, borrow cost, margin utilization)
  - `tests/test_paper_broker_shorts.py` — PaperBroker short position lifecycle (submit_short_sell, submit_cover, margin, borrow cost)
  - `tests/test_congressional_shorts.py` — Congressional trades sale cluster short signals
  - `tests/test_execution_bridge_shorts.py` — ExecutionBridge short routing
  - `tests/test_trade_rec_vehicle.py` — TradeRecommendation vehicle field
  - `tests/test_committee_vehicle.py` — Committee vehicle selection logic
  - `tests/test_covered_call_overlay.py` — Covered call overlay generation
  - `tests/test_eligibility_wiring.py` — Eligibility wiring and config integration
  - `tests/test_integration_shorts.py` — Integration tests for short/options pipelines

---

## Key Documentation

| Document | Description |
|----------|-------------|
| [AUTORESEARCH_ARCHITECTURE_MAP.md](AUTORESEARCH_ARCHITECTURE_MAP.md) | Full autoresearch system architecture |
| [docs/strategy_research.md](docs/strategy_research.md) | Strategy research notes and academic backing |
| [docs/superpowers/specs/2026-04-03-openbb-integration-design.md](docs/superpowers/specs/2026-04-03-openbb-integration-design.md) | OpenBB integration design spec |
| [docs/next-gen-improvements.md](docs/next-gen-improvements.md) | Follow-on improvements for gen_004 (loosen gates, expand universes) |
| [docs/reports/](docs/reports/) | Daily trading reports |
| [docs/archive/](docs/archive/) | Executed design specs for options, backtest, execution, dashboard, autoresearch |
| [docs/superpowers/specs/2026-04-04-options-shorting-wave1-design.md](docs/superpowers/specs/2026-04-04-options-shorting-wave1-design.md) | Options & shorting Wave 1 design spec |
