# TradingAgents - Development Guide

## Project Overview

TradingAgents is a multi-agent LLM trading framework built with LangGraph. It has two major systems:

1. **Core Pipeline** — 6-agent LangGraph pipeline (analysts, researchers, trader, risk manager, portfolio manager) that evaluates a single ticker and produces a trade decision.
2. **Autoresearch** — Autonomous multi-strategy evolution engine: Phase 1 infrastructure exists but is dormant (no backtest strategies registered); Phase 2 is a 2-cohort paper trading trial (Control with fixed confidence vs. Adaptive with journal-derived confidence and weekly learning). 7 event-driven strategies across 8 data sources.

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

# Cohort trial (2 parallel paper portfolios) — single-generation use
python scripts/run_cohorts.py --date 2026-03-31          # daily trading (LLM on by default)
python scripts/run_cohorts.py --date 2026-03-31 --no-llm  # without LLM enrichment
python scripts/run_cohorts.py --learning                   # weekly learning (adaptive only)
python scripts/run_cohorts.py --compare                    # compare cohort performance
python scripts/run_cohorts.py --reset                      # reset all cohort state

# Autoresearch — single engine (legacy, single-engine)
python scripts/run_generation.py                                          # paper trading (default)
python scripts/run_generation.py --phase paper                             # daily/weekly trading loop
python scripts/run_generation.py --phase learning                          # monthly/quarterly learning loop
python scripts/run_generation.py --use-llm                                 # with LLM enrichment
python scripts/run_generation.py --reset                                   # clear state and run fresh

# Dashboard
.venv/bin/python -m streamlit run tradingagents/dashboard/app.py
```

## Architecture Overview

```
CORE PIPELINE (LangGraph)                 AUTORESEARCH
─────────────────────────                 ──────────────────────────────
Fundamental Analyst ──┐                   DataSourceRegistry (8 sources)
Sentiment Analyst  ───┤                            │
News Analyst       ───┤──► Bull/Bear          StrategyModules (7)
Technical Analyst  ───┤    Researchers              │
Options Analyst    ───┘         │       ┌───── Phase 1: Backtest ─────┐
                           Trader       │  (dormant — no strategies   │
                              │         │   currently registered)     │
                        Risk Manager    │  N generations → Playbook   │
                              │         └─────────────┬───────────────┘
                      Portfolio Manager          Playbook (params,
                              │                   regime, scores)
                        Trade Decision              │
                                          ┌── Cohort A (Control) ──┐
                                          │  Fixed confidence 0.5  │
CohortOrchestrator ── Shared Data Fetch ──┤  No learning           │
                                          │                        │
                                          ├── Cohort B (Adaptive) ─┤
                                          │  Journal confidence     │
                                          │  Prompt optimization    │
                                          │  Weekly learning loop   │
                                          └────────────────────────┘
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

**Phase 1 — Backtest Evolution** (offline, fast): Infrastructure exists but is dormant — no backtest strategies are currently registered. The system runs Phase 2 (paper trading) only. When activated, Phase 1 would run N generations of backtest strategies with aggressive Darwinian weights (x1.05/x0.95), producing a "playbook" with optimized params, regime model (VIX/credit/yield curve), and strategy reliability scores.

**Phase 2 — 2-Cohort Paper Trading Trial** (live, ongoing): A/B test of two parallel paper portfolios sharing the same data fetch but using separate state directories (`data/state/control/`, `data/state/adaptive/`):

- **Cohort A (Control):** Fixed `strategy_confidence=0.5`, no learning loop. Baseline for comparison.
- **Cohort B (Adaptive):** Journal-derived `strategy_confidence` from hit rates, prompt optimization, weekly learning loop with conservative weight adjustment (x1.02/x0.98, >= 20 completed trades).

The **portfolio committee** is the sole sizing authority. Weight scaling has been removed from `compute_position_size()` and `execute_recommendation()` — the risk gate only enforces hard limits.

### Active Strategies (7 event-driven, paper-trade only)

| Strategy | Class | Data Source |
|----------|-------|-------------|
| `earnings_call` | `EarningsCallStrategy` | Finnhub earnings transcripts |
| `insider_activity` | `InsiderActivityStrategy` | EDGAR Form 4 filings |
| `filing_analysis` | `FilingAnalysisStrategy` | EDGAR 10-K/10-Q changes |
| `regulatory_pipeline` | `RegulatoryPipelineStrategy` | Regulations.gov proposed rules |
| `supply_chain` | `SupplyChainStrategy` | Finnhub supply chain disruption |
| `litigation` | `LitigationStrategy` | CourtListener federal court dockets |
| `congressional_trades` | `CongressionalTradesStrategy` | Congress stock trades |

3 additional strategies exist as code but are NOT registered/active: `govt_contracts`, `state_economics`, `weather_ag` (in `strategies/` for future use).

### Key Components

- `cohort_orchestrator.py` — `CohortOrchestrator` runs shared data fetch then dispatches to each cohort's engine. `CohortConfig` and `build_default_cohorts()` define the A/B setup.
- `cohort_comparison.py` — `CohortComparison` with `compare()` and `format_report()` for side-by-side cohort analysis.
- `portfolio_committee.py` — LLM + rule-based signal synthesis (sole sizing authority).
- Vintage tracking (param sets tagged with vintage ID, trade count, creation date), regime model, separate weight pools (`backtest_weights.json` / `paper_weights.json`), 15% exploration budget for unproven param sets.
- Future bolt-on: debate validation (not built yet).

Legacy `run_generation.py` still works for single-engine use; for the 2-cohort trial use `scripts/run_cohorts.py`.

### Adding a New Strategy

1. Create `tradingagents/autoresearch/strategies/my_strategy.py`
2. Implement the `StrategyModule` protocol from `strategies/base.py`:
   - `name: str` — unique identifier (e.g. `"my_strategy"`)
   - `track: str` — `"backtest"` or `"paper_trade"`
   - `data_sources: list[str]` — registry source names needed (e.g. `["yfinance", "fred"]`)
   - `get_param_space() -> dict[str, tuple]` — evolvable parameter ranges
   - `get_default_params() -> dict[str, Any]` — sensible defaults
   - `screen(data, date, params) -> list[Candidate]` — screen for entry signals
   - `check_exit(ticker, entry_price, current_price, holding_days, params, data) -> (bool, str)` — exit logic
   - `build_propose_prompt(context) -> str` — LLM prompt for parameter evolution
3. Register in `strategies/__init__.py`:
   - Add import
   - Add to `__all__`
   - Add instance to `get_backtest_strategies()` or `get_paper_trade_strategies()`

### Adding a New Data Source

1. Create `tradingagents/autoresearch/data_sources/my_source.py`
2. Implement the `DataSource` protocol from `data_sources/registry.py`:
   - `name: str` — registry key (e.g. `"my_source"`)
   - `requires_api_key: bool`
   - `fetch(params: dict) -> dict` — generic fetch dispatcher (`params["method"]` selects endpoint)
   - `is_available() -> bool` — check API key and dependencies
3. Register in `data_sources/registry.py` `build_default_registry()`
4. Add to `data_sources/__init__.py` exports
5. Add API key to `default_config.py` autoresearch section and `.env.example`

### Darwinian Weight System

Strategies have weights in `[0.3, 2.5]` (default `1.0`). Dual-mode weight adjustment:
- **Backtest phase** (aggressive): Top quartile `*= 1.05`, bottom quartile `*= 0.95`
- **Paper-trade phase** (conservative): Top quartile `*= 1.02`, bottom quartile `*= 0.98`, requires >= 20 completed trades

Separate weight pools: `backtest_weights.json` and `paper_weights.json`. Weights determine capital allocation. Config keys: `weight_min`, `weight_max`, `weight_up_factor`, `weight_down_factor`, `paper_weight_up_factor`, `paper_weight_down_factor`, `paper_min_trades`.

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
| `autoresearch` | `autoresearch_model` | LLM for all autoresearch calls (Haiku) |
| `autoresearch` | `total_capital` | Portfolio size for allocation |
| `autoresearch` | `adaptive_confidence` | `False` = fixed 0.5 (Cohort A), `True` = journal-derived (Cohort B) |

API keys go in `.env` (git-ignored), loaded via `python-dotenv`. See `.env.example` for required keys.

---

## LLM Provider

- **Primary:** Anthropic Claude
- `deep_think_llm`: `claude-sonnet-4-20250514`
- `quick_think_llm`: `claude-haiku-4-5-20251001`
- **Autoresearch:** Haiku for all LLM calls (~$0.03/generation, ~$0.001/call)
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
  - `tests/test_multi_strategy.py` — 96 tests covering strategies, Darwinian weights, state, engine
  - `tests/test_e2e_pipeline.py` — End-to-end integration tests for full trading pipeline

---

## Key Documentation

| Document | Description |
|----------|-------------|
| [AUTORESEARCH_ARCHITECTURE_MAP.md](AUTORESEARCH_ARCHITECTURE_MAP.md) | Full autoresearch system architecture |
| [docs/strategy_research.md](docs/strategy_research.md) | Strategy research notes and academic backing |
| [docs/archive/](docs/archive/) | Executed design specs for options, backtest, execution, dashboard, autoresearch |
