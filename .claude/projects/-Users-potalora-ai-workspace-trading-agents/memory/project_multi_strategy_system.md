---
name: Multi-strategy autoresearch system design
description: 20 trading strategies (10 backtest, 10 paper-trade) with Darwinian evolution, inspired by atlas-gic architecture
type: project
---

Designing a unified multi-strategy autoresearch system with 20 strategies in two tracks:

**Backtest Track (B1-B10):** Factor momentum, cross-asset momentum, VIX mean reversion, PEAD, govt contracts, 13D activist, credit spread, economic surprise, weather/ag, state economics.

**Paper-Trade Track (P1-P10):** Earnings call tone+revision prediction, earnings cross-referencing, filing changes (Lazy Prices), insider cluster+filing combo, regulatory pipeline, supply chain disruption, 10b5-1 red flags, WARN Act, DEF 14A exec comp, litigation pre-filing.

**Key architecture decisions:**
- Atlas-gic inspired: Darwinian weights [0.3-2.5], JSON state files, prompts-as-weights, Haiku everywhere
- DataSource registry pattern (yfinance, EDGAR, FRED, USAspending, Finnhub, etc.)
- StrategyModule protocol with typed parameters (replaces brittle string entry/exit rules)
- 10 strategies run with NO API keys (yfinance + EDGAR + USAspending + House/Senate Stock Watcher)
- Target: <10 min per generation on 16GB M4, ~$0.03 LLM cost per generation
- Kim et al 2024 (LLM beats analysts) was WITHDRAWN — don't cite
- FINSABER 2025: LLM trading fails on 20-year tests — this is a research project, not a money printer

**Why:** User wants to test ALL evidence-backed strategies that haven't been debunked. Technical rule mining was proven useless. LLM alpha is real but narrow (unstructured text reasoning, not parameter search).

**How to apply:** All implementation should follow the StrategyModule protocol. Each strategy is a self-contained module. Evolution engine orchestrates across all. Detailed strategy research in `docs/strategy_research.md`.
