# TradingAgents - Development Guide

## Project Overview
TradingAgents is a multi-agent LLM trading framework built with LangGraph. We are extending it with: options analysis, backtesting, execution (Alpaca), dashboard (Streamlit), scheduling, and alerts.

**Design spec:** `docs/superpowers/specs/2026-03-29-trading-extensions-design.md`
**Implementation plan:** `docs/superpowers/plans/2026-03-29-trading-extensions.md`

## Architecture

### Existing Patterns (FOLLOW THESE)

**Analyst pattern** (`tradingagents/agents/analysts/*.py`):
1. Factory function `create_X_analyst(llm)` returns a closure `X_analyst_node(state) -> dict`
2. Extract `trade_date`, `company_of_interest` from state
3. Build instrument context via `build_instrument_context()`
4. Define tools list, create ChatPromptTemplate, bind tools to LLM
5. Invoke chain, return `{"messages": [result], "X_report": report}`

**Tool definition pattern** (`tradingagents/agents/utils/*_tools.py`):
- Use `@tool` decorator from `langchain_core.tools`
- Use `Annotated` type hints with descriptions
- Route through `route_to_vendor()` from `tradingagents/dataflows/interface.py`

**Data vendor pattern** (`tradingagents/dataflows/interface.py`):
- Register in `VENDOR_METHODS` dict: `"method_name": {"vendor": impl_function}`
- Add to `TOOLS_CATEGORIES` dict for category-level routing
- Implement vendor function in `tradingagents/dataflows/` module

**Graph registration** (`tradingagents/graph/setup.py`):
- Analysts are conditionally added based on `selected_analysts` list
- Each analyst gets: node, tool_node, msg_clear node, conditional edges
- Analysts run sequentially, last one connects to Bull Researcher

**State** (`tradingagents/agents/utils/agent_states.py`):
- `AgentState(MessagesState)` holds all reports and debate states
- Add new fields as `Annotated[str, "description"]`

**Exports** (`tradingagents/agents/__init__.py`):
- All new agent factory functions must be added here

### New Modules Being Added

```
tradingagents/storage/          # SQLite persistence (Phase 1)
tradingagents/dataflows/options_data.py  # Options chain data (Phase 2)
tradingagents/agents/analysts/options_analyst.py  # Options analyst (Phase 2)
tradingagents/agents/utils/options_tools.py       # Options tools (Phase 2)
tradingagents/backtesting/      # Backtest engine (Phase 3)
tradingagents/execution/        # Broker abstraction + Alpaca (Phase 4)
tradingagents/dashboard/        # Streamlit UI (Phase 5)
tradingagents/scheduler/        # APScheduler + alerts (Phase 6)
```

## Testing

- Framework: `pytest` (project uses unittest but we standardize on pytest for new code)
- Test location: `tests/` mirroring source structure
- Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/ -v`
- Mock LLM calls in unit tests — never call real APIs in tests
- Use `unittest.mock.patch` for external services (yfinance, Alpaca)

## Config

- All new config goes into `tradingagents/default_config.py` under new keys
- Config keys: `options`, `backtest`, `execution`, `scheduler`, `alerts`
- API keys in `.env` file (git-ignored), loaded via `python-dotenv`

## LLM Provider
- Using Anthropic (Claude) as primary provider
- `deep_think_llm`: `claude-sonnet-4-20250514`
- `quick_think_llm`: `claude-haiku-4-5-20251001`

## Commands
- Install: `pip install .` (from repo root with .venv active)
- Test: `.venv/bin/python -m pytest tests/ -v`
- CLI: `.venv/bin/python -m cli.main`
- Dashboard (once built): `.venv/bin/python -m streamlit run tradingagents/dashboard/app.py`
