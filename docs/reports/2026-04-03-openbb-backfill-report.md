# 3-Day Backfill Report: gen_001 vs gen_002

**Date:** 2026-04-03
**Period:** April 1–3, 2026
**Branch:** feature/trading-extensions

## Portfolio Summary

| | gen_001 (7-strategy baseline) | gen_002 (9-strategy OpenBB) |
|---|---|---|
| **Days run** | 3 (Apr 1–3) | 3 (Apr 1–3) |
| **Total signals** | 48 per cohort | 39 per cohort |
| **Traded signals** | 5 (all on day 1) | 5 (all on day 3) |
| **Strategies firing** | 5 (filing_analysis, congressional_trades, insider_activity, regulatory_pipeline, litigation) | 4 (filing_analysis, congressional_trades, insider_activity, regulatory_pipeline) |
| **All trades** | Open (no exits yet) | Open (no exits yet) |

## Open Positions

### gen_001 (entered Apr 1)

**Control cohort** — capital deployed: $1,132 of $5,000 (23%)

| Ticker | Strategy | Direction | Entry | Shares | Value |
|--------|----------|-----------|-------|--------|-------|
| LGIH | congressional_trades | Long | $39.53 | 10 | $395.30 |
| CBRL | congressional_trades | Long | $28.11 | 8 | $224.88 |
| PPTA | filing_analysis | Long | $28.12 | 7 | $196.84 |
| ACN | filing_analysis | Long | $198.29 | 1 | $198.29 |
| SCHL | filing_analysis | Long | $39.06 | 3 | $117.18 |

**Adaptive cohort** — capital deployed: $1,278 of $5,000 (26%)

| Ticker | Strategy | Direction | Entry | Shares | Value |
|--------|----------|-----------|-------|--------|-------|
| LGIH | congressional_trades | Long | $39.53 | 11 | $434.83 |
| CBRL | congressional_trades | Long | $28.11 | 8 | $224.88 |
| PPTA | filing_analysis | Long | $28.12 | 8 | $224.96 |
| ACN | filing_analysis | Long | $198.29 | 1 | $198.29 |
| SCHL | filing_analysis | Long | $39.06 | 5 | $195.30 |

> Adaptive sized LGIH, PPTA, and SCHL larger than control — journal-derived confidence for `congressional_trades` and `filing_analysis` is above the fixed 0.5 baseline, producing slightly larger positions.

### gen_002 (entered Apr 3)

**Control cohort** — capital deployed: $1,560 of $5,000 (31%)

| Ticker | Strategy | Direction | Entry | Shares | Value |
|--------|----------|-----------|-------|--------|-------|
| LGIH | congressional_trades | Long | $38.13 | 13 | $495.69 |
| CBRL | congressional_trades | Long | $28.89 | 12 | $346.68 |
| CMC | filing_analysis | Long | $61.79 | 4 | $247.16 |
| PPTA | filing_analysis | Long | $29.43 | 8 | $235.44 |
| SCHL | filing_analysis | Long | $39.20 | 6 | $235.20 |

**Adaptive cohort** — capital deployed: $1,658 of $5,000 (33%)

| Ticker | Strategy | Direction | Entry | Shares | Value |
|--------|----------|-----------|-------|--------|-------|
| LGIH | congressional_trades | Long | $38.13 | 13 | $495.69 |
| CBRL | congressional_trades | Long | $28.89 | 12 | $346.68 |
| PPTA | filing_analysis | Long | $29.43 | 10 | $294.30 |
| SCHL | filing_analysis | Long | $39.20 | 7 | $274.40 |
| CMC | filing_analysis | Long | $61.79 | 4 | $247.16 |

> gen_002 deployed more capital overall (31–33% vs 23–26%) and picked CMC (Commercial Metals) instead of ACN (Accenture). The OpenBB analyst consensus enrichment in `filing_analysis` shifted which filings scored highest.

## Signal Analysis

### Highest-Conviction Signals

| Ticker | Strategy | Score | Notes |
|--------|----------|-------|-------|
| LGIH (LGI Homes) | congressional_trades | 9.0 | Member of Congress traded this stock; strongest signal in both generations |
| GS (Goldman Sachs) | congressional_trades | 4.0–8.0 | Congressional trade, but not enough cross-strategy confirmation to execute |
| NVDA | insider_activity | 0.35–64.5 | Spiked to 64.5 on Apr 3 in gen_001 — likely a large insider sale filing. Short signal. |
| CBRL (Cracker Barrel) | congressional_trades | 2.0 | Congressional trade, executed in both generations |

### Strategies That Did NOT Fire

| Strategy | Reason |
|----------|--------|
| `earnings_call` | No earnings events in the Apr 1–3 window |
| `supply_chain` | No supply chain disruption signals from Finnhub |
| `govt_contracts` (gen_002 only) | No USASpending contract data and no strong defense contractor momentum |
| `state_economics` (gen_002 only) | Regional ETFs didn't meet the minimum return threshold |

### gen_001 vs gen_002 Signal Differences

- gen_001 produced 48 signals vs gen_002's 39 — gen_002 has fewer signals because the backfill ordering meant Apr 1–2 signals were recorded but not traded (gen_002's worktree was created on Apr 3)
- gen_002's `insider_activity` scores are slightly different (NVDA: 0.52 vs 0.45 on Apr 1) — the OpenBB officer title enrichment boosts C-suite insider signals by 1.3x
- gen_002 picked up **CMC** instead of **ACN** — the OpenBB analyst consensus enrichment in `filing_analysis` shifted scoring
- `litigation` fired once in gen_001 (AAPL, score=0.10, neutral, Apr 2) but not in gen_002 — same underlying data, so this may reflect LLM nondeterminism in signal classification

## Regime Context

| Date | Overall | VIX | Credit | Yield Curve |
|------|---------|-----|--------|-------------|
| Apr 1 | **Stressed** | Elevated | Normal | Unknown |
| Apr 2 | Normal | Normal | Normal | Unknown |
| Apr 3 | Normal | Normal | Normal | Unknown |

The stressed regime on Apr 1 would have biased the portfolio committee against long positions (regime misalignment), but the strong congressional trade scores (LGIH=9.0) overrode the regime signal.

## What to Watch

- **First exits expected:** Apr 6–8 (5-day hold strategies) or Apr 15 (14-day rebalance for `state_economics`)
- **NVDA insider signal:** Score spiked to 64.5 on Apr 3 in gen_001 — a massive insider sale worth monitoring even though it wasn't traded (short + single strategy = filtered by committee)
- **govt_contracts and state_economics:** Haven't fired yet in gen_002. Need specific triggers (large federal contract announcements or strong regional ETF momentum). These strategies will differentiate gen_002 from gen_001 once market conditions activate them.
- **Cohort divergence:** Control and adaptive are sizing differently (adaptive is larger) but picking the same tickers. Divergence will grow as the adaptive cohort accumulates journal history and adjusts confidence scores.

## Next Steps

Run daily:
```bash
python scripts/run_generations.py run-daily
```

Check progress:
```bash
python scripts/run_generations.py compare
```

Day 15 writeup target: **2026-04-16**
