# Codebase Simplification Design Spec

**Goal:** Reorganize the codebase for clarity, extensibility, and easy integration of ideas from inspiration repos (TradingAgents, aihedgefund, atlas-gic).

**Scope:** Rename + restructure `autoresearch/` into `strategies/` with logical sub-packages, move dormant code out of the active path, clean up Alpha Vantage dead weight, consolidate redundant scripts, and update all references.

---

## 1. Rename `autoresearch/` → `strategies/`

The `tradingagents/autoresearch/` package is renamed to `tradingagents/strategies/`. The current `strategies/` sub-package (10 strategy module files) is renamed to `modules/` to avoid name collision.

### New structure

```
tradingagents/strategies/
  __init__.py
  orchestration/
    __init__.py
    multi_strategy_engine.py
    cohort_orchestrator.py
    cohort_comparison.py
    generation_manager.py
    generation_comparison.py
  trading/
    __init__.py
    portfolio_committee.py
    risk_gate.py
    paper_trader.py
    execution_bridge.py
  learning/
    __init__.py
    llm_analyzer.py
    prompt_optimizer.py
    signal_journal.py
    event_monitor.py
  state/
    __init__.py
    state.py
    models.py
    cycle_tracker.py
  modules/
    __init__.py
    base.py
    earnings_call.py
    insider_activity.py
    filing_analysis.py
    regulatory_pipeline.py
    supply_chain.py
    litigation.py
    congressional_trades.py
    govt_contracts.py
    state_economics.py
    weather_ag.py
  data_sources/
    __init__.py
    registry.py
    congress_source.py
    courtlistener_source.py
    drought_monitor_source.py
    edgar_source.py
    finnhub_source.py
    fred_source.py
    noaa_source.py
    openbb_source.py
    regulations_source.py
    usaspending_source.py
    usda_source.py
    yfinance_source.py
  _dormant/
    __init__.py
    evolution.py
    walk_forward.py
    fast_backtest.py
    fitness.py
    screener.py
    strategist.py
    cached_pipeline.py
    ticker_universe.py
    autoresearch_loop.py
```

### Rationale for sub-packages

| Sub-package | Responsibility | Files |
|---|---|---|
| `orchestration/` | Running strategies, managing generations, comparing results | 5 files |
| `trading/` | Making trade decisions: sizing, risk, execution | 4 files |
| `learning/` | Post-trade analysis, prompt optimization, journaling | 4 files |
| `state/` | Persistence, data models, cycle tracking | 3 files |
| `modules/` | The 10 strategy implementations | 11 files (base + 10 strategies) |
| `data_sources/` | External API adapters + registry | 13 files |
| `_dormant/` | Phase 1 backtest infrastructure (not active) | 9 files |

---

## 2. Move dormant Phase 1 backtest code

These files currently live at `autoresearch/` root level. They implement Phase 1 backtest evolution infrastructure that has no registered strategies and is not in use. Move to `strategies/_dormant/`:

- `evolution.py` — genetic algorithm for parameter evolution
- `walk_forward.py` — walk-forward backtesting engine
- `fast_backtest.py` — lightweight backtest runner
- `fitness.py` — fitness scoring for evolved parameters
- `screener.py` — ticker screening for backtest phase
- `strategist.py` — strategy orchestration for backtest phase
- `cached_pipeline.py` — cached core pipeline for backtest
- `ticker_universe.py` — ticker list management (superseded by per-strategy universes + `get_universe()`)

Also fold existing `_archive/` contents (`autoresearch/_archive/autoresearch_loop.py`, `strategies/_archive/`) into the single `_dormant/` directory.

---

## 3. Move Alpha Vantage to `dataflows/_dormant/`

These 6 files implement an alternative data vendor that is not in use (all config defaults point to yfinance):

- `alpha_vantage.py`
- `alpha_vantage_common.py`
- `alpha_vantage_fundamentals.py`
- `alpha_vantage_indicator.py`
- `alpha_vantage_news.py`
- `alpha_vantage_stock.py`

Move to `tradingagents/dataflows/_dormant/`.

Remove Alpha Vantage imports from `interface.py`. Keep the vendor routing dict architecture so it remains pluggable — just remove the AV entries and imports.

---

## 4. Consolidate scripts

### Delete

- `scripts/run_generation.py` — Legacy single-engine runner, fully superseded by `run_generations.py`
- `scripts/run_cohorts.py` — Single-gen cohort runner, superseded by generation system

### Keep

- `scripts/run_generations.py` — Primary entry point for generation management
- `scripts/generate_daily_report.py` — Daily report generator
- `scripts/daily_trading.sh` — Shell wrapper for cron/launchd

---

## 5. Update all references

Every import path referencing `autoresearch` must be updated to `strategies`. Every reference to the old `strategies` sub-package must be updated to `modules`.

### Files requiring import updates

**Internal cross-references within the package:**
- All files in `strategies/orchestration/`, `strategies/trading/`, `strategies/learning/`, `strategies/state/` that import from each other
- `strategies/modules/__init__.py` — exports `get_paper_trade_strategies()`, `get_backtest_strategies()`
- `strategies/data_sources/__init__.py` — exports `build_default_registry()`

**External references:**
- `tradingagents/default_config.py` — `autoresearch` config section key
- `scripts/run_generations.py` — imports from `tradingagents.autoresearch`
- `scripts/generate_daily_report.py` — imports from `tradingagents.autoresearch`
- `scripts/run_generations.py` — references to `autoresearch` in CLI help text

**Tests:**
- All test files under `tests/` that import from `tradingagents.autoresearch`

**Documentation:**
- `CLAUDE.md` — all references to `autoresearch/`
- `AUTORESEARCH_ARCHITECTURE_MAP.md` — full document update
- `docs/next-gen-improvements.md`
- `docs/strategy_research.md`
- Any other docs referencing old paths

---

## 6. Unchanged modules

These modules are not modified by this work:

- `tradingagents/agents/` — upstream core pipeline agents
- `tradingagents/graph/` — LangGraph setup and orchestration
- `tradingagents/llm_clients/` — LLM provider abstraction
- `tradingagents/execution/` — broker implementations
- `tradingagents/backtesting/` — walk-forward engine (separate from dormant Phase 1 code)
- `tradingagents/dashboard/` — Streamlit UI
- `tradingagents/scheduler/` — APScheduler jobs
- `tradingagents/storage/` — SQLite persistence

---

## 7. Inspiration repo integration map

After this restructure, features from inspiration repos map cleanly:

| Feature from inspiration repo | Maps to |
|---|---|
| New analyst agent (TradingAgents, aihedgefund) | `tradingagents/agents/analysts/` |
| New data vendor (TradingAgents) | `tradingagents/dataflows/` |
| New event-driven strategy idea (atlas-gic) | `tradingagents/strategies/modules/` |
| New external data source API | `tradingagents/strategies/data_sources/` |
| Graph/pipeline improvement (TradingAgents) | `tradingagents/graph/` |
| Risk/portfolio logic (aihedgefund, atlas-gic) | `tradingagents/strategies/trading/` |
| LLM provider support (TradingAgents) | `tradingagents/llm_clients/` |
| Famous investor personas (aihedgefund) | `tradingagents/agents/` (new sub-package) |
| Macro/sector desk agents (atlas-gic) | `tradingagents/agents/` (new sub-package) |
| Autoresearch/self-improvement (atlas-gic) | `tradingagents/strategies/learning/` |

---

## 8. Constraints

- All 641+ tests must pass after restructuring
- No behavioral changes — this is a pure reorganization
- Active generations (gen_001 through gen_004) run from frozen worktrees with their own code copies, so they are unaffected
- Config key `autoresearch` in `default_config.py` can be renamed to `strategies` but the `.env` keys and state directory paths remain unchanged
- Dashboard pages that reference autoresearch imports must be updated
