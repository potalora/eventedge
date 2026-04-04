# Codebase Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the codebase by renaming `autoresearch/` to `strategies/` with logical sub-packages, moving dormant code out of the active path, cleaning up Alpha Vantage, and consolidating scripts.

**Architecture:** Pure file-move + import-rename operation. No behavioral changes. The `tradingagents/autoresearch/` package becomes `tradingagents/strategies/` with sub-packages: `orchestration/`, `trading/`, `learning/`, `state/`, `modules/` (the 10 strategies), `data_sources/`, and `_dormant/`. Alpha Vantage files move to `dataflows/_dormant/`. Legacy scripts are deleted.

**Tech Stack:** Python, git, pytest

**Important context for all tasks:**
- The rename is `tradingagents.autoresearch` → `tradingagents.strategies`
- The sub-package rename is `tradingagents.autoresearch.strategies` → `tradingagents.strategies.modules`
- Every `from tradingagents.autoresearch.X` becomes `from tradingagents.strategies.X` (where X may also change based on sub-package restructuring)
- Mock/patch targets like `"tradingagents.autoresearch.foo.bar"` must also be updated
- The config key `"autoresearch"` in `default_config.py` stays as-is (it's a config key, not an import path, and changing it would break existing state files)

---

### Task 1: Create directory structure and move files

**Files:**
- Create: `tradingagents/strategies/orchestration/__init__.py`
- Create: `tradingagents/strategies/trading/__init__.py`
- Create: `tradingagents/strategies/learning/__init__.py`
- Create: `tradingagents/strategies/state/__init__.py`
- Create: `tradingagents/strategies/_dormant/__init__.py`
- Create: `tradingagents/dataflows/_dormant/__init__.py`
- Move: all files from `tradingagents/autoresearch/` to new locations

This task uses `git mv` to preserve history. No import changes yet — that's Task 2+.

- [ ] **Step 1: Create sub-package directories**

```bash
mkdir -p tradingagents/strategies/orchestration
mkdir -p tradingagents/strategies/trading
mkdir -p tradingagents/strategies/learning
mkdir -p tradingagents/strategies/state
mkdir -p tradingagents/strategies/modules
mkdir -p tradingagents/strategies/_dormant
mkdir -p tradingagents/dataflows/_dormant
```

- [ ] **Step 2: Move orchestration files**

```bash
git mv tradingagents/autoresearch/multi_strategy_engine.py tradingagents/strategies/orchestration/
git mv tradingagents/autoresearch/cohort_orchestrator.py tradingagents/strategies/orchestration/
git mv tradingagents/autoresearch/cohort_comparison.py tradingagents/strategies/orchestration/
git mv tradingagents/autoresearch/generation_manager.py tradingagents/strategies/orchestration/
git mv tradingagents/autoresearch/generation_comparison.py tradingagents/strategies/orchestration/
```

- [ ] **Step 3: Move trading files**

```bash
git mv tradingagents/autoresearch/portfolio_committee.py tradingagents/strategies/trading/
git mv tradingagents/autoresearch/risk_gate.py tradingagents/strategies/trading/
git mv tradingagents/autoresearch/paper_trader.py tradingagents/strategies/trading/
git mv tradingagents/autoresearch/execution_bridge.py tradingagents/strategies/trading/
```

- [ ] **Step 4: Move learning files**

```bash
git mv tradingagents/autoresearch/llm_analyzer.py tradingagents/strategies/learning/
git mv tradingagents/autoresearch/prompt_optimizer.py tradingagents/strategies/learning/
git mv tradingagents/autoresearch/signal_journal.py tradingagents/strategies/learning/
git mv tradingagents/autoresearch/event_monitor.py tradingagents/strategies/learning/
```

- [ ] **Step 5: Move state files**

```bash
git mv tradingagents/autoresearch/state.py tradingagents/strategies/state/
git mv tradingagents/autoresearch/models.py tradingagents/strategies/state/
git mv tradingagents/autoresearch/cycle_tracker.py tradingagents/strategies/state/
```

- [ ] **Step 6: Move strategy modules (rename sub-package)**

```bash
git mv tradingagents/autoresearch/strategies/base.py tradingagents/strategies/modules/
git mv tradingagents/autoresearch/strategies/earnings_call.py tradingagents/strategies/modules/
git mv tradingagents/autoresearch/strategies/insider_activity.py tradingagents/strategies/modules/
git mv tradingagents/autoresearch/strategies/filing_analysis.py tradingagents/strategies/modules/
git mv tradingagents/autoresearch/strategies/regulatory_pipeline.py tradingagents/strategies/modules/
git mv tradingagents/autoresearch/strategies/supply_chain.py tradingagents/strategies/modules/
git mv tradingagents/autoresearch/strategies/litigation.py tradingagents/strategies/modules/
git mv tradingagents/autoresearch/strategies/congressional_trades.py tradingagents/strategies/modules/
git mv tradingagents/autoresearch/strategies/govt_contracts.py tradingagents/strategies/modules/
git mv tradingagents/autoresearch/strategies/state_economics.py tradingagents/strategies/modules/
git mv tradingagents/autoresearch/strategies/weather_ag.py tradingagents/strategies/modules/
git mv tradingagents/autoresearch/strategies/__init__.py tradingagents/strategies/modules/
```

- [ ] **Step 7: Move data_sources (keep as sub-package)**

```bash
git mv tradingagents/autoresearch/data_sources tradingagents/strategies/data_sources
```

- [ ] **Step 8: Move dormant Phase 1 backtest files**

```bash
git mv tradingagents/autoresearch/evolution.py tradingagents/strategies/_dormant/
git mv tradingagents/autoresearch/walk_forward.py tradingagents/strategies/_dormant/
git mv tradingagents/autoresearch/fast_backtest.py tradingagents/strategies/_dormant/
git mv tradingagents/autoresearch/fitness.py tradingagents/strategies/_dormant/
git mv tradingagents/autoresearch/screener.py tradingagents/strategies/_dormant/
git mv tradingagents/autoresearch/strategist.py tradingagents/strategies/_dormant/
git mv tradingagents/autoresearch/cached_pipeline.py tradingagents/strategies/_dormant/
git mv tradingagents/autoresearch/ticker_universe.py tradingagents/strategies/_dormant/
```

- [ ] **Step 9: Move archive contents into _dormant**

```bash
# Move the old archive file
git mv tradingagents/autoresearch/_archive/autoresearch_loop.py tradingagents/strategies/_dormant/
# Remove empty archive dirs
rm -rf tradingagents/autoresearch/_archive
rm -rf tradingagents/autoresearch/strategies/_archive 2>/dev/null
```

- [ ] **Step 10: Move Alpha Vantage files to dataflows/_dormant**

```bash
git mv tradingagents/dataflows/alpha_vantage.py tradingagents/dataflows/_dormant/
git mv tradingagents/dataflows/alpha_vantage_common.py tradingagents/dataflows/_dormant/
git mv tradingagents/dataflows/alpha_vantage_fundamentals.py tradingagents/dataflows/_dormant/
git mv tradingagents/dataflows/alpha_vantage_indicator.py tradingagents/dataflows/_dormant/
git mv tradingagents/dataflows/alpha_vantage_news.py tradingagents/dataflows/_dormant/
git mv tradingagents/dataflows/alpha_vantage_stock.py tradingagents/dataflows/_dormant/
```

- [ ] **Step 11: Move remaining autoresearch root files**

```bash
# The __init__.py stays — we'll rewrite it in Task 2
git mv tradingagents/autoresearch/__init__.py tradingagents/strategies/__init__.py
```

- [ ] **Step 12: Clean up empty autoresearch directory**

```bash
# Remove any remaining empty directories
rm -rf tradingagents/autoresearch
```

- [ ] **Step 13: Create __init__.py files for new sub-packages**

Write empty `__init__.py` for each new sub-package:

```python
# tradingagents/strategies/orchestration/__init__.py
# tradingagents/strategies/trading/__init__.py
# tradingagents/strategies/learning/__init__.py
# tradingagents/strategies/state/__init__.py
# tradingagents/strategies/_dormant/__init__.py
# tradingagents/dataflows/_dormant/__init__.py
```

Each file is just an empty file (or a single blank line).

- [ ] **Step 14: Commit the file moves**

```bash
git add -A
git commit -m "refactor: move autoresearch/ to strategies/ with sub-packages

Reorganize into orchestration/, trading/, learning/, state/, modules/,
data_sources/, and _dormant/. Move Alpha Vantage to dataflows/_dormant/.
Imports not yet updated - will break until next commit."
```

---

### Task 2: Update imports in strategies/ package (internal cross-references)

**Files:**
- Modify: `tradingagents/strategies/__init__.py`
- Modify: `tradingagents/strategies/modules/__init__.py`
- Modify: `tradingagents/strategies/data_sources/__init__.py`
- Modify: `tradingagents/strategies/data_sources/registry.py`
- Modify: All files in `orchestration/`, `trading/`, `learning/`, `state/`
- Modify: `tradingagents/strategies/modules/litigation.py`

Every `from tradingagents.autoresearch.X` import within the strategies package must be updated. The mapping is:

| Old import path | New import path |
|---|---|
| `tradingagents.autoresearch.multi_strategy_engine` | `tradingagents.strategies.orchestration.multi_strategy_engine` |
| `tradingagents.autoresearch.cohort_orchestrator` | `tradingagents.strategies.orchestration.cohort_orchestrator` |
| `tradingagents.autoresearch.cohort_comparison` | `tradingagents.strategies.orchestration.cohort_comparison` |
| `tradingagents.autoresearch.generation_manager` | `tradingagents.strategies.orchestration.generation_manager` |
| `tradingagents.autoresearch.generation_comparison` | `tradingagents.strategies.orchestration.generation_comparison` |
| `tradingagents.autoresearch.portfolio_committee` | `tradingagents.strategies.trading.portfolio_committee` |
| `tradingagents.autoresearch.risk_gate` | `tradingagents.strategies.trading.risk_gate` |
| `tradingagents.autoresearch.paper_trader` | `tradingagents.strategies.trading.paper_trader` |
| `tradingagents.autoresearch.execution_bridge` | `tradingagents.strategies.trading.execution_bridge` |
| `tradingagents.autoresearch.llm_analyzer` | `tradingagents.strategies.learning.llm_analyzer` |
| `tradingagents.autoresearch.prompt_optimizer` | `tradingagents.strategies.learning.prompt_optimizer` |
| `tradingagents.autoresearch.signal_journal` | `tradingagents.strategies.learning.signal_journal` |
| `tradingagents.autoresearch.event_monitor` | `tradingagents.strategies.learning.event_monitor` |
| `tradingagents.autoresearch.state` | `tradingagents.strategies.state.state` |
| `tradingagents.autoresearch.models` | `tradingagents.strategies.state.models` |
| `tradingagents.autoresearch.cycle_tracker` | `tradingagents.strategies.state.cycle_tracker` |
| `tradingagents.autoresearch.strategies` | `tradingagents.strategies.modules` |
| `tradingagents.autoresearch.strategies.base` | `tradingagents.strategies.modules.base` |
| `tradingagents.autoresearch.data_sources.X` | `tradingagents.strategies.data_sources.X` |

- [ ] **Step 1: Rewrite `tradingagents/strategies/__init__.py`**

```python
from tradingagents.strategies.state.models import (
    Filter,
    ScreenerCriteria,
    BacktestResults,
    ScreenerResult,
    Strategy,
)
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    CohortOrchestrator,
    CohortConfig,
    build_default_cohorts,
)
from tradingagents.strategies.orchestration.cohort_comparison import CohortComparison
from tradingagents.strategies.learning.signal_journal import SignalJournal

__all__ = [
    "Filter",
    "ScreenerCriteria",
    "BacktestResults",
    "ScreenerResult",
    "Strategy",
    "MultiStrategyEngine",
    "CohortOrchestrator",
    "CohortConfig",
    "build_default_cohorts",
    "CohortComparison",
    "SignalJournal",
]
```

Note: Dormant exports (`CachedPipelineRunner`, `FastBacktestRunner`, `Strategist`, `WalkForwardWindow`, `EvolutionEngine`) are removed from `__init__.py` since those classes moved to `_dormant/`.

- [ ] **Step 2: Update `tradingagents/strategies/modules/__init__.py`**

This file uses relative imports (`.base`, `.insider_activity`, etc.) so it should work as-is after the move. No changes needed — verify the relative imports are intact.

- [ ] **Step 3: Update `tradingagents/strategies/data_sources/__init__.py`**

Replace all `from tradingagents.autoresearch.data_sources.X` with `from tradingagents.strategies.data_sources.X`:

```python
from tradingagents.strategies.data_sources.registry import (
    DataSource,
    DataSourceRegistry,
    build_default_registry,
)
from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
from tradingagents.strategies.data_sources.edgar_source import EDGARSource
from tradingagents.strategies.data_sources.usaspending_source import USASpendingSource
from tradingagents.strategies.data_sources.congress_source import CongressSource
from tradingagents.strategies.data_sources.fred_source import FREDSource
from tradingagents.strategies.data_sources.finnhub_source import FinnhubSource
from tradingagents.strategies.data_sources.regulations_source import RegulationsSource
from tradingagents.strategies.data_sources.courtlistener_source import CourtListenerSource
from tradingagents.strategies.data_sources.openbb_source import OpenBBSource
from tradingagents.strategies.data_sources.usda_source import USDASource
from tradingagents.strategies.data_sources.drought_monitor_source import DroughtMonitorSource
```

- [ ] **Step 4: Update `tradingagents/strategies/data_sources/registry.py`**

Replace all `from tradingagents.autoresearch.data_sources.X` with `from tradingagents.strategies.data_sources.X` (lines 96-133).

- [ ] **Step 5: Update `tradingagents/strategies/orchestration/multi_strategy_engine.py`**

This file has the most imports (~15). Apply the mapping table above to every import. Key replacements:

```python
# Line 16-22: top-level imports
from tradingagents.strategies.data_sources.registry import (...)
from tradingagents.strategies.state.state import StateManager
from tradingagents.strategies.modules import get_paper_trade_strategies
from tradingagents.strategies.modules.base import Candidate

# Lazy imports throughout the file (~12 occurrences):
from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.learning.signal_journal import SignalJournal, JournalEntry
from tradingagents.strategies.state.cycle_tracker import CycleTracker
from tradingagents.strategies.trading.execution_bridge import ExecutionBridge
from tradingagents.strategies.trading.paper_trader import PaperTrader
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
from tradingagents.strategies.learning.prompt_optimizer import PromptOptimizer
from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
from tradingagents.strategies.learning.event_monitor import EventMonitor
from tradingagents.strategies.data_sources.fred_source import SERIES_MAP
```

- [ ] **Step 6: Update `tradingagents/strategies/orchestration/cohort_orchestrator.py`**

```python
# Lines 38-40
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.state.state import StateManager
from tradingagents.strategies.modules import get_paper_trade_strategies
```

- [ ] **Step 7: Update `tradingagents/strategies/orchestration/cohort_comparison.py`**

```python
from tradingagents.strategies.learning.signal_journal import SignalJournal
from tradingagents.strategies.state.state import StateManager
```

- [ ] **Step 8: Update `tradingagents/strategies/orchestration/generation_comparison.py`**

```python
from tradingagents.strategies.learning.signal_journal import SignalJournal
from tradingagents.strategies.state.state import StateManager
```

- [ ] **Step 9: Update `tradingagents/strategies/trading/execution_bridge.py`**

```python
from tradingagents.strategies.trading.risk_gate import RiskGate, RiskGateConfig
```

- [ ] **Step 10: Update `tradingagents/strategies/trading/paper_trader.py`**

```python
from tradingagents.strategies.state.state import StateManager
```

- [ ] **Step 11: Update `tradingagents/strategies/modules/litigation.py`**

```python
# Line 173
from tradingagents.strategies.data_sources.edgar_source import EDGARSource
```

- [ ] **Step 12: Update dormant files (best-effort)**

Update imports in `_dormant/` files. These don't need to work — just update the paths so they're correct if ever revived. Apply the mapping table to:
- `evolution.py`
- `cached_pipeline.py`
- `fast_backtest.py`
- `fitness.py`
- `screener.py`
- `strategist.py`
- `autoresearch_loop.py`

- [ ] **Step 13: Commit**

```bash
git add -A
git commit -m "refactor: update all imports within strategies/ package

Apply autoresearch → strategies import mapping to all internal
cross-references including orchestration, trading, learning, state,
modules, and data_sources sub-packages."
```

---

### Task 3: Update imports in external files (scripts, scheduler, dashboard, config)

**Files:**
- Modify: `scripts/run_generations.py`
- Modify: `scripts/generate_daily_report.py`
- Modify: `tradingagents/scheduler/jobs.py`
- Modify: `tradingagents/dashboard/pages/evolution.py`
- Modify: `tradingagents/dashboard/pages/leaderboard.py`
- Modify: `tradingagents/dataflows/interface.py`
- Delete: `scripts/run_generation.py`
- Delete: `scripts/run_cohorts.py`

- [ ] **Step 1: Update `scripts/run_generations.py`**

```python
# Line 95
from tradingagents.strategies.orchestration.generation_manager import GenerationManager

# Line 128
from tradingagents.strategies.orchestration.generation_comparison import (
    GenerationComparison,
)
```

- [ ] **Step 2: Update `scripts/generate_daily_report.py`**

```python
# Line 71
from tradingagents.strategies.data_sources.openbb_source import OpenBBSource
# Line 84
from tradingagents.strategies.data_sources.noaa_source import NOAASource
# Line 97
from tradingagents.strategies.data_sources.usda_source import USDASource
# Line 110
from tradingagents.strategies.data_sources.fred_source import FREDSource
# Line 123
from tradingagents.strategies.data_sources.drought_monitor_source import DroughtMonitorSource
# Line 151
from tradingagents.strategies.state.cycle_tracker import CycleTracker
# Line 402
from tradingagents.strategies.orchestration.generation_manager import GenerationManager
```

- [ ] **Step 3: Update `tradingagents/scheduler/jobs.py`**

```python
# Lines 50-52 — these reference dormant code, update paths to _dormant:
from tradingagents.strategies._dormant.evolution import EvolutionEngine
from tradingagents.strategies.state.models import Strategy
from tradingagents.strategies._dormant.cached_pipeline import CachedPipelineRunner

# Line 100
from tradingagents.strategies._dormant.evolution import EvolutionEngine
```

- [ ] **Step 4: Update `tradingagents/dashboard/pages/evolution.py`**

Rename string references only (no import changes — this file uses string literals):
- `"autoresearch.pid"` → keep as-is (it's a filename, not an import)
- `"Run autoresearch to start"` → `"Run strategies to start"`

- [ ] **Step 5: Update `tradingagents/dashboard/pages/leaderboard.py`**

- `"Run autoresearch to generate"` → `"Run strategies to generate"`

- [ ] **Step 6: Update `tradingagents/dataflows/interface.py`**

Remove Alpha Vantage imports and entries. Replace lines 14-25:

Remove:
```python
from .alpha_vantage import (
    get_stock as get_alpha_vantage_stock,
    get_indicator as get_alpha_vantage_indicator,
    get_fundamentals as get_alpha_vantage_fundamentals,
    get_balance_sheet as get_alpha_vantage_balance_sheet,
    get_cashflow as get_alpha_vantage_cashflow,
    get_income_statement as get_alpha_vantage_income_statement,
    get_insider_transactions as get_alpha_vantage_insider_transactions,
    get_news as get_alpha_vantage_news,
    get_global_news as get_alpha_vantage_global_news,
)
from .alpha_vantage_common import AlphaVantageRateLimitError
```

Remove `"alpha_vantage"` from `VALID_VENDORS` list (line 78).

Remove all `"alpha_vantage": get_alpha_vantage_*` entries from `VENDOR_METHODS` dict (lines 85-120).

- [ ] **Step 7: Delete legacy scripts**

```bash
git rm scripts/run_generation.py
git rm scripts/run_cohorts.py
```

- [ ] **Step 8: Also delete `scripts/dry_run.py` if it references dormant code**

Check: `scripts/dry_run.py` imports `EvolutionEngine` from autoresearch. It's a dry-run test for the dormant Phase 1 system. Delete it:

```bash
git rm scripts/dry_run.py
```

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor: update external imports, remove legacy scripts

Update scripts, scheduler, dashboard, and dataflows to use new
strategies/ import paths. Delete run_generation.py, run_cohorts.py,
and dry_run.py (all superseded). Remove Alpha Vantage from interface.py."
```

---

### Task 4: Update all test imports

**Files:**
- Modify: All test files that import from `tradingagents.autoresearch`

The test files and their import counts:
- `tests/test_multi_strategy.py` — ~50 imports (largest)
- `tests/test_30day_simulation.py` — ~15 imports
- `tests/test_cohort_lifecycle.py` — ~8 imports
- `tests/test_weather_ag.py` — ~4 imports
- `tests/test_openbb_source.py` — ~5 imports
- `tests/test_generation_manager.py` — ~4 imports
- `tests/test_cycle_tracker.py` — 1 import
- `tests/test_drought_monitor_source.py` — 2 imports
- `tests/test_usda_source.py` — 3 imports
- `tests/test_prompt_optimizer_30day.py` — ~4 imports
- `tests/test_ticker_universe.py` — 1 import (dormant code test)
- `tests/test_evolution.py` — ~6 imports (dormant code test)
- `tests/test_autoresearch_simulation.py` — ~8 imports (dormant code test)
- `tests/test_autoresearch_integration.py` — ~7 imports (dormant code test)
- `tests/test_cached_pipeline.py` — 1 import (dormant code test)
- `tests/test_fast_backtest.py` — 3 imports (dormant code test)
- `tests/test_fitness.py` — 2 imports (dormant code test)
- `tests/test_screener.py` — 2 imports (dormant code test)
- `tests/test_forward_backtest.py` — ~5 imports (dormant code test)
- `tests/test_strategist.py` — 2 imports (dormant code test)
- `tests/test_walk_forward.py` — 1 import (dormant code test)
- `tests/test_scheduler.py` — 2 mock paths
- `tests/test_cli.py` — 1 mock path

Apply the same mapping table from Task 2 to every file. Additionally, update mock/patch targets:
- `"tradingagents.autoresearch.X.Y"` → use the new path from the mapping table

- [ ] **Step 1: Update active test files**

Apply the import mapping to these files (they test active code):
- `tests/test_multi_strategy.py`
- `tests/test_30day_simulation.py`
- `tests/test_cohort_lifecycle.py`
- `tests/test_weather_ag.py`
- `tests/test_openbb_source.py`
- `tests/test_generation_manager.py`
- `tests/test_cycle_tracker.py`
- `tests/test_drought_monitor_source.py`
- `tests/test_usda_source.py`
- `tests/test_prompt_optimizer_30day.py`
- `tests/test_scheduler.py`
- `tests/test_cli.py`

For each file, do a global find-replace:
1. `tradingagents.autoresearch.multi_strategy_engine` → `tradingagents.strategies.orchestration.multi_strategy_engine`
2. `tradingagents.autoresearch.cohort_orchestrator` → `tradingagents.strategies.orchestration.cohort_orchestrator`
3. `tradingagents.autoresearch.cohort_comparison` → `tradingagents.strategies.orchestration.cohort_comparison`
4. `tradingagents.autoresearch.generation_manager` → `tradingagents.strategies.orchestration.generation_manager`
5. `tradingagents.autoresearch.generation_comparison` → `tradingagents.strategies.orchestration.generation_comparison`
6. `tradingagents.autoresearch.portfolio_committee` → `tradingagents.strategies.trading.portfolio_committee`
7. `tradingagents.autoresearch.risk_gate` → `tradingagents.strategies.trading.risk_gate`
8. `tradingagents.autoresearch.paper_trader` → `tradingagents.strategies.trading.paper_trader`
9. `tradingagents.autoresearch.execution_bridge` → `tradingagents.strategies.trading.execution_bridge`
10. `tradingagents.autoresearch.llm_analyzer` → `tradingagents.strategies.learning.llm_analyzer`
11. `tradingagents.autoresearch.prompt_optimizer` → `tradingagents.strategies.learning.prompt_optimizer`
12. `tradingagents.autoresearch.signal_journal` → `tradingagents.strategies.learning.signal_journal`
13. `tradingagents.autoresearch.event_monitor` → `tradingagents.strategies.learning.event_monitor`
14. `tradingagents.autoresearch.state` → `tradingagents.strategies.state.state`
15. `tradingagents.autoresearch.models` → `tradingagents.strategies.state.models`
16. `tradingagents.autoresearch.cycle_tracker` → `tradingagents.strategies.state.cycle_tracker`
17. `tradingagents.autoresearch.strategies.base` → `tradingagents.strategies.modules.base`
18. `tradingagents.autoresearch.strategies.weather_ag` → `tradingagents.strategies.modules.weather_ag`
19. `tradingagents.autoresearch.strategies.govt_contracts` → `tradingagents.strategies.modules.govt_contracts`
20. `tradingagents.autoresearch.strategies.state_economics` → `tradingagents.strategies.modules.state_economics`
21. `tradingagents.autoresearch.strategies` → `tradingagents.strategies.modules` (catch-all for remaining)
22. `tradingagents.autoresearch.data_sources` → `tradingagents.strategies.data_sources`

**Important ordering:** Apply specific replacements (e.g., `strategies.base`) before the catch-all (`strategies` alone) to avoid double-replacement.

- [ ] **Step 2: Update dormant test files**

Apply the same mapping to these files (they test dormant code — update paths to `_dormant/`):
- `tests/test_ticker_universe.py` — `tradingagents.autoresearch.ticker_universe` → `tradingagents.strategies._dormant.ticker_universe`
- `tests/test_evolution.py` — update all to `_dormant.` paths
- `tests/test_autoresearch_simulation.py` — update all to `_dormant.` paths
- `tests/test_autoresearch_integration.py` — update all to `_dormant.` paths
- `tests/test_cached_pipeline.py` — update to `_dormant.` path
- `tests/test_fast_backtest.py` — update to `_dormant.` paths
- `tests/test_fitness.py` — update to `_dormant.` paths
- `tests/test_screener.py` — update to `_dormant.` paths
- `tests/test_forward_backtest.py` — update to `_dormant.` paths
- `tests/test_strategist.py` — update to `_dormant.` paths
- `tests/test_walk_forward.py` — update to `_dormant.` path

For dormant tests, the mapping is:
- `tradingagents.autoresearch.evolution` → `tradingagents.strategies._dormant.evolution`
- `tradingagents.autoresearch.cached_pipeline` → `tradingagents.strategies._dormant.cached_pipeline`
- `tradingagents.autoresearch.fast_backtest` → `tradingagents.strategies._dormant.fast_backtest`
- `tradingagents.autoresearch.fitness` → `tradingagents.strategies._dormant.fitness`
- `tradingagents.autoresearch.screener` → `tradingagents.strategies._dormant.screener`
- `tradingagents.autoresearch.strategist` → `tradingagents.strategies._dormant.strategist`
- `tradingagents.autoresearch.walk_forward` → `tradingagents.strategies._dormant.walk_forward`
- `tradingagents.autoresearch.ticker_universe` → `tradingagents.strategies._dormant.ticker_universe`

Plus the same active-code mappings for `models`, `state`, etc.

- [ ] **Step 3: Run all tests**

```bash
.venv/bin/python -m pytest tests/ -v 2>&1 | tail -30
```

Expected: All 641+ tests pass.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: update all test imports to strategies/ paths"
```

---

### Task 5: Update documentation and CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`
- Modify: `AUTORESEARCH_ARCHITECTURE_MAP.md`
- Modify: `docs/next-gen-improvements.md`
- Modify: `docs/strategy_research.md` (if it references autoresearch paths)

- [ ] **Step 1: Update CLAUDE.md**

Global replacements throughout the file:
1. `autoresearch/` → `strategies/` (directory references)
2. `autoresearch.` → `strategies.` (import-style references)
3. `tradingagents/autoresearch/strategies/` → `tradingagents/strategies/modules/`
4. Update the Architecture diagram to show `strategies/` instead of `autoresearch/`
5. Update the "Adding a New Strategy" section paths
6. Update the "Adding a New Data Source" section paths
7. Update the config section — note the config key stays `"autoresearch"` but the package is `strategies/`
8. Remove references to `run_generation.py` and `run_cohorts.py` from Quick Reference
9. Remove references to `dry_run.py` if present
10. Update test file list paths
11. Update "Current active generations" section if needed
12. Update Key Documentation table

- [ ] **Step 2: Update AUTORESEARCH_ARCHITECTURE_MAP.md**

This is a documentation-only file. Do a global find-replace of `autoresearch` → `strategies` in all code paths, and `strategies/` (the old sub-package) → `modules/`. Update the title if appropriate.

- [ ] **Step 3: Update docs/next-gen-improvements.md**

Replace any `autoresearch/` path references.

- [ ] **Step 4: Update docs/strategy_research.md**

Replace any `autoresearch/` path references if they exist.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "docs: update all documentation for strategies/ restructure"
```

---

### Task 6: Final verification and cleanup

**Files:**
- Verify: entire codebase

- [ ] **Step 1: Search for any remaining `autoresearch` references**

```bash
grep -r "autoresearch" tradingagents/ scripts/ tests/ --include="*.py" -l
```

Expected: No results (or only `_dormant/autoresearch_loop.py` filename matches, and the config key in `default_config.py`).

- [ ] **Step 2: Search docs for remaining references**

```bash
grep -r "autoresearch" *.md docs/ --include="*.md" -l
```

Expected: No results except possibly `AUTORESEARCH_ARCHITECTURE_MAP.md` filename itself (which is fine to keep as-is or rename).

- [ ] **Step 3: Run full test suite**

```bash
.venv/bin/python -m pytest tests/ -v
```

Expected: All 641+ tests pass.

- [ ] **Step 4: Verify package installs correctly**

```bash
pip install -e . 2>&1 | tail -5
```

Expected: Successfully installed.

- [ ] **Step 5: Verify generation runner still works**

```bash
python scripts/run_generations.py list
```

Expected: Lists gen_001 through gen_004 as before.

- [ ] **Step 6: Commit any final fixes**

```bash
git add -A
git commit -m "refactor: final cleanup for codebase simplification"
```
