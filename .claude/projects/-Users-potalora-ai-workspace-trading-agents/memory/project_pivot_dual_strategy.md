---
name: Strategic pivot to filing analysis + VRP
description: Major pivot from technical rule mining to two evidence-backed strategies - small-cap filing analysis and regime-aware VRP
type: project
---

Pivoted away from technical rule mining (RSI/EMA/MACD on S&P 500) after research showed it's thoroughly debunked (Bajgrowicz & Scaillet 2012, Sullivan et al 1999, Zakamulin 2023).

**Pivot #1: Small-cap filing analysis** — Use multi-agent LLM pipeline to read 10-K/10-Q filings on neglected small-caps, detect material changes before the market prices them in. Based on "Lazy Prices" (Cohen et al, JoF 2020) showing 188bps/month from 10-K language changes. LLMs beat analysts at financial statement analysis (Kim et al 2024: 60% vs 53%).

**Pivot #2: Regime-aware VRP** — Harvest volatility risk premium (sell SPX puts) with LLM-based regime detection to avoid tail events. Simple VRP = Sharpe 0.5-0.7, regime-managed = Sharpe 1.0-1.4. LLMs decide *when not to trade*, not what to trade.

**Why:** LLMs add alpha where the task is reading unstructured text faster than humans, not searching numeric parameter space. Multi-agent debate genuinely helps for interpreting filing changes and regime assessment.

**How to apply:** All new feature work should align with one of these two strategies. Keep existing autoresearch infrastructure (screener, walk-forward, fitness scoring, evolution loop) but replace technical rule signals with filing-based signals and VRP timing rules.
