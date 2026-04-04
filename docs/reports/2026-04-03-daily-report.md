# Daily Report: 2026-04-03

**Generated:** 2026-04-04 09:23
**Active generations:** 4

## Cycle Context

**gen_001:** Cycle 1, Day 3 of 30 (28 days remaining)
**gen_002:** Cycle 1, Day 1 of 30 (30 days remaining)
**gen_003:** Cycle 0, Day 30 of 30 (1 days remaining)
**gen_004:** Cycle 0, Day 30 of 30 (1 days remaining)

## Infrastructure Health

| Source | Status | Note |
|--------|--------|------|
| OpenBB | OK | Sector enforcement + enrichment |
| NOAA CDO | OK | Weather anomaly data |
| USDA NASS | OK | Crop condition data |
| FRED | OK | Macro / Fama-French factors |
| Drought Monitor | OK | Drought severity data |

## Summary

| | gen_001 | gen_002 | gen_003 | gen_004 |
|---|---|---|---|---|
| **Description** | Initial 7-strategy baseline v1 | 9-strategy OpenBB enrichment: sector classification, analyst | 10-strategy ag enhancement: USDA + Drought Monitor + expande | Gen 004: gate loosening, universe expansion, 30-day cycle ev |
| **Commit** | 5f3730d28dc8 | a0a4c7aad273 | b36811453675 | 3b71fd18fd96 |
| **Days run** | 4 | 4 | 1 | 1 |
| **Control signals** | 66 | 57 | 14 | 14 |
| **Control trades (open/closed)** | 5/0 | 5/0 | 6/0 | 6/0 |
| **Control capital deployed** | $1,132 | $1,560 | $1,908 | $1,801 |
| **Control hit rate** | N/A | N/A | N/A | N/A |
| **Control Sharpe** | N/A | N/A | N/A | N/A |
| **Control return** | N/A | N/A | N/A | N/A |
| **Adaptive signals** | 66 | 57 | 14 | 14 |
| **Adaptive trades (open/closed)** | 5/0 | 5/0 | 6/0 | 6/0 |
| **Adaptive capital deployed** | $1,278 | $1,658 | $1,981 | $1,801 |
| **Adaptive hit rate** | N/A | N/A | N/A | N/A |
| **Adaptive Sharpe** | N/A | N/A | N/A | N/A |
| **Adaptive return** | N/A | N/A | N/A | N/A |

## gen_001: Initial 7-strategy baseline v1

### Control Cohort

**Open positions** — $1,132 deployed

| Ticker | Strategy | Dir | Entry | Shares | Value | Entry Date |
|--------|----------|-----|-------|--------|-------|------------|
| LGIH | congressional_trades | long | $39.53 | 10 | $395.30 | 2026-04-01 |
| CBRL | congressional_trades | long | $28.11 | 8 | $224.88 | 2026-04-01 |
| PPTA | filing_analysis | long | $28.12 | 7 | $196.84 | 2026-04-01 |
| ACN | filing_analysis | long | $198.29 | 1 | $198.29 | 2026-04-01 |
| SCHL | filing_analysis | long | $39.06 | 3 | $117.18 | 2026-04-01 |

**Today's signals** — 17 signals, 0 traded

| | Ticker | Strategy | Dir | Score |
|---|--------|----------|-----|-------|
|   | META | insider_activity | neutral | 0.10 |
|   | NVDA | insider_activity | short | 64.50 |
|   | GOOGL | insider_activity | short | 0.35 |
|   | BLK | regulatory_pipeline | neutral | 0.20 |
|   | WEC | regulatory_pipeline | neutral | 0.10 |
|   | VSCO | filing_analysis | neutral | 0.10 |
|   | ANF | filing_analysis | neutral | 0.10 |
|   | PPTA | filing_analysis | long | 0.50 |
|   | SCHL | filing_analysis | long | 0.50 |
|   | CMC | filing_analysis | long | 0.50 |
|   | LGIH | congressional_trades | long | 9.00 |
|   | GS | congressional_trades | long | 4.00 |
|   | CBRL | congressional_trades | long | 2.00 |
|   | JPM | insider_activity | neutral | 0.15 |
|   | EPAC | filing_analysis | long | 0.50 |
|   | MIR | filing_analysis | long | 0.50 |
|   | CTVA | litigation | short | 0.35 |

**Strategy breakdown**

- `filing_analysis`: 24 signals, 3 trades
- `insider_activity`: 14 signals, 0 trades
- `regulatory_pipeline`: 13 signals, 0 trades
- `congressional_trades`: 12 signals, 2 trades
- `litigation`: 3 signals, 0 trades

### Adaptive Cohort

**Open positions** — $1,278 deployed

| Ticker | Strategy | Dir | Entry | Shares | Value | Entry Date |
|--------|----------|-----|-------|--------|-------|------------|
| LGIH | congressional_trades | long | $39.53 | 11 | $434.83 | 2026-04-01 |
| PPTA | filing_analysis | long | $28.12 | 8 | $224.96 | 2026-04-01 |
| ACN | filing_analysis | long | $198.29 | 1 | $198.29 | 2026-04-01 |
| CBRL | congressional_trades | long | $28.11 | 8 | $224.88 | 2026-04-01 |
| SCHL | filing_analysis | long | $39.06 | 5 | $195.30 | 2026-04-01 |

**Today's signals** — 17 signals, 0 traded

| | Ticker | Strategy | Dir | Score |
|---|--------|----------|-----|-------|
|   | META | insider_activity | neutral | 0.10 |
|   | NVDA | insider_activity | short | 64.50 |
|   | GOOGL | insider_activity | short | 0.35 |
|   | BLK | regulatory_pipeline | neutral | 0.20 |
|   | WEC | regulatory_pipeline | neutral | 0.10 |
|   | VSCO | filing_analysis | neutral | 0.10 |
|   | ANF | filing_analysis | neutral | 0.10 |
|   | PPTA | filing_analysis | long | 0.50 |
|   | SCHL | filing_analysis | long | 0.50 |
|   | CMC | filing_analysis | long | 0.50 |
|   | LGIH | congressional_trades | long | 9.00 |
|   | GS | congressional_trades | long | 4.00 |
|   | CBRL | congressional_trades | long | 2.00 |
|   | JPM | insider_activity | neutral | 0.15 |
|   | EPAC | filing_analysis | long | 0.50 |
|   | MIR | filing_analysis | long | 0.50 |
|   | CTVA | litigation | short | 0.35 |

**Strategy breakdown**

- `filing_analysis`: 24 signals, 3 trades
- `insider_activity`: 14 signals, 0 trades
- `regulatory_pipeline`: 13 signals, 0 trades
- `congressional_trades`: 12 signals, 2 trades
- `litigation`: 3 signals, 0 trades

## gen_002: 9-strategy OpenBB enrichment: sector classification, analyst estimates, short in

### Control Cohort

**Open positions** — $1,560 deployed

| Ticker | Strategy | Dir | Entry | Shares | Value | Entry Date |
|--------|----------|-----|-------|--------|-------|------------|
| LGIH | congressional_trades | long | $38.13 | 13 | $495.69 | 2026-04-03 |
| CBRL | congressional_trades | long | $28.89 | 12 | $346.68 | 2026-04-03 |
| CMC | filing_analysis | long | $61.79 | 4 | $247.16 | 2026-04-03 |
| PPTA | filing_analysis | long | $29.43 | 8 | $235.44 | 2026-04-03 |
| SCHL | filing_analysis | long | $39.20 | 6 | $235.20 | 2026-04-03 |

**Today's signals** — 17 signals, 5 traded

| | Ticker | Strategy | Dir | Score |
|---|--------|----------|-----|-------|
|   | META | insider_activity | neutral | 0.10 |
|   | NVDA | insider_activity | short | 0.38 |
|   | GOOGL | insider_activity | short | 0.35 |
|   | BLK | regulatory_pipeline | neutral | 0.20 |
|   | WEC | regulatory_pipeline | neutral | 0.10 |
|   | VSCO | filing_analysis | neutral | 0.20 |
|   | ANF | filing_analysis | neutral | 0.10 |
| \* | PPTA | filing_analysis | long | 0.50 |
| \* | SCHL | filing_analysis | long | 0.50 |
| \* | CMC | filing_analysis | long | 0.50 |
| \* | LGIH | congressional_trades | long | 9.00 |
|   | GS | congressional_trades | long | 4.00 |
| \* | CBRL | congressional_trades | long | 2.00 |
|   | JPM | insider_activity | neutral | 0.15 |
|   | EPAC | filing_analysis | long | 0.50 |
|   | MIR | filing_analysis | long | 0.50 |
|   | CTVA | litigation | short | 0.35 |

**Strategy breakdown**

- `filing_analysis`: 22 signals, 3 trades
- `insider_activity`: 13 signals, 0 trades
- `congressional_trades`: 12 signals, 2 trades
- `regulatory_pipeline`: 8 signals, 0 trades
- `litigation`: 2 signals, 0 trades

### Adaptive Cohort

**Open positions** — $1,658 deployed

| Ticker | Strategy | Dir | Entry | Shares | Value | Entry Date |
|--------|----------|-----|-------|--------|-------|------------|
| LGIH | congressional_trades | long | $38.13 | 13 | $495.69 | 2026-04-03 |
| CBRL | congressional_trades | long | $28.89 | 12 | $346.68 | 2026-04-03 |
| PPTA | filing_analysis | long | $29.43 | 10 | $294.30 | 2026-04-03 |
| SCHL | filing_analysis | long | $39.20 | 7 | $274.40 | 2026-04-03 |
| CMC | filing_analysis | long | $61.79 | 4 | $247.16 | 2026-04-03 |

**Today's signals** — 17 signals, 5 traded

| | Ticker | Strategy | Dir | Score |
|---|--------|----------|-----|-------|
|   | META | insider_activity | neutral | 0.10 |
|   | NVDA | insider_activity | short | 0.38 |
|   | GOOGL | insider_activity | short | 0.35 |
|   | BLK | regulatory_pipeline | neutral | 0.20 |
|   | WEC | regulatory_pipeline | neutral | 0.10 |
|   | VSCO | filing_analysis | neutral | 0.20 |
|   | ANF | filing_analysis | neutral | 0.10 |
| \* | PPTA | filing_analysis | long | 0.50 |
| \* | SCHL | filing_analysis | long | 0.50 |
| \* | CMC | filing_analysis | long | 0.50 |
| \* | LGIH | congressional_trades | long | 9.00 |
|   | GS | congressional_trades | long | 4.00 |
| \* | CBRL | congressional_trades | long | 2.00 |
|   | JPM | insider_activity | neutral | 0.15 |
|   | EPAC | filing_analysis | long | 0.50 |
|   | MIR | filing_analysis | long | 0.50 |
|   | CTVA | litigation | short | 0.35 |

**Strategy breakdown**

- `filing_analysis`: 22 signals, 3 trades
- `insider_activity`: 13 signals, 0 trades
- `congressional_trades`: 12 signals, 2 trades
- `regulatory_pipeline`: 8 signals, 0 trades
- `litigation`: 2 signals, 0 trades

## gen_003: 10-strategy ag enhancement: USDA + Drought Monitor + expanded tickers

### Control Cohort

**Open positions** — $1,908 deployed

| Ticker | Strategy | Dir | Entry | Shares | Value | Entry Date |
|--------|----------|-----|-------|--------|-------|------------|
| LGIH | congressional_trades | long | $38.13 | 13 | $495.69 | 2026-04-03 |
| CBRL | congressional_trades | long | $28.89 | 13 | $375.57 | 2026-04-03 |
| PPTA | filing_analysis | long | $29.43 | 10 | $294.30 | 2026-04-03 |
| CMC | filing_analysis | long | $61.79 | 4 | $247.16 | 2026-04-03 |
| EPAC | filing_analysis | long | $35.44 | 7 | $248.08 | 2026-04-03 |
| MIR | filing_analysis | long | $19.00 | 13 | $247.00 | 2026-04-03 |

**Today's signals** — 14 signals, 6 traded

| | Ticker | Strategy | Dir | Score |
|---|--------|----------|-----|-------|
|   | META | insider_activity | neutral | 0.10 |
|   | GOOGL | insider_activity | short | 0.35 |
|   | JPM | insider_activity | neutral | 0.15 |
|   | BLK | regulatory_pipeline | neutral | 0.20 |
|   | WEC | regulatory_pipeline | neutral | 0.10 |
|   | ANF | filing_analysis | neutral | 0.20 |
| \* | PPTA | filing_analysis | long | 0.50 |
| \* | CMC | filing_analysis | long | 0.50 |
| \* | EPAC | filing_analysis | long | 0.50 |
| \* | MIR | filing_analysis | long | 0.50 |
|   | CTVA | litigation | short | 0.35 |
| \* | LGIH | congressional_trades | long | 9.00 |
|   | GS | congressional_trades | long | 4.00 |
| \* | CBRL | congressional_trades | long | 2.00 |

**Strategy breakdown**

- `filing_analysis`: 5 signals, 4 trades
- `insider_activity`: 3 signals, 0 trades
- `congressional_trades`: 3 signals, 2 trades
- `regulatory_pipeline`: 2 signals, 0 trades
- `litigation`: 1 signals, 0 trades

### Adaptive Cohort

**Open positions** — $1,981 deployed

| Ticker | Strategy | Dir | Entry | Shares | Value | Entry Date |
|--------|----------|-----|-------|--------|-------|------------|
| LGIH | congressional_trades | long | $38.13 | 13 | $495.69 | 2026-04-03 |
| CBRL | congressional_trades | long | $28.89 | 13 | $375.57 | 2026-04-03 |
| PPTA | filing_analysis | long | $29.43 | 10 | $294.30 | 2026-04-03 |
| CMC | filing_analysis | long | $61.79 | 4 | $247.16 | 2026-04-03 |
| EPAC | filing_analysis | long | $35.44 | 8 | $283.52 | 2026-04-03 |
| MIR | filing_analysis | long | $19.00 | 15 | $285.00 | 2026-04-03 |

**Today's signals** — 14 signals, 6 traded

| | Ticker | Strategy | Dir | Score |
|---|--------|----------|-----|-------|
|   | META | insider_activity | neutral | 0.10 |
|   | GOOGL | insider_activity | short | 0.35 |
|   | JPM | insider_activity | neutral | 0.15 |
|   | BLK | regulatory_pipeline | neutral | 0.20 |
|   | WEC | regulatory_pipeline | neutral | 0.10 |
|   | ANF | filing_analysis | neutral | 0.20 |
| \* | PPTA | filing_analysis | long | 0.50 |
| \* | CMC | filing_analysis | long | 0.50 |
| \* | EPAC | filing_analysis | long | 0.50 |
| \* | MIR | filing_analysis | long | 0.50 |
|   | CTVA | litigation | short | 0.35 |
| \* | LGIH | congressional_trades | long | 9.00 |
|   | GS | congressional_trades | long | 4.00 |
| \* | CBRL | congressional_trades | long | 2.00 |

**Strategy breakdown**

- `filing_analysis`: 5 signals, 4 trades
- `insider_activity`: 3 signals, 0 trades
- `congressional_trades`: 3 signals, 2 trades
- `regulatory_pipeline`: 2 signals, 0 trades
- `litigation`: 1 signals, 0 trades

## gen_004: Gen 004: gate loosening, universe expansion, 30-day cycle evaluation

### Control Cohort

**Open positions** — $1,801 deployed

| Ticker | Strategy | Dir | Entry | Shares | Value | Entry Date |
|--------|----------|-----|-------|--------|-------|------------|
| LGIH | congressional_trades | long | $38.13 | 13 | $495.69 | 2026-04-03 |
| IBP | congressional_trades | long | $268.71 | 1 | $268.71 | 2026-04-03 |
| PPTA | filing_analysis | long | $29.43 | 10 | $294.30 | 2026-04-03 |
| CMC | filing_analysis | long | $61.79 | 4 | $247.16 | 2026-04-03 |
| EPAC | filing_analysis | long | $35.44 | 7 | $248.08 | 2026-04-03 |
| MIR | filing_analysis | long | $19.00 | 13 | $247.00 | 2026-04-03 |

**Today's signals** — 14 signals, 6 traded

| | Ticker | Strategy | Dir | Score |
|---|--------|----------|-----|-------|
|   | META | insider_activity | neutral | 0.10 |
|   | GOOGL | insider_activity | short | 0.35 |
|   | JPM | insider_activity | neutral | 0.15 |
|   | BLK | regulatory_pipeline | neutral | 0.20 |
|   | WEC | regulatory_pipeline | neutral | 0.10 |
|   | ANF | filing_analysis | neutral | 0.20 |
| \* | PPTA | filing_analysis | long | 0.50 |
| \* | CMC | filing_analysis | long | 0.50 |
| \* | EPAC | filing_analysis | long | 0.50 |
| \* | MIR | filing_analysis | long | 0.50 |
|   | CTVA | litigation | short | 0.35 |
| \* | LGIH | congressional_trades | long | 9.00 |
|   | GS | congressional_trades | long | 4.00 |
| \* | IBP | congressional_trades | long | 2.00 |

**Strategy breakdown**

- `filing_analysis`: 5 signals, 4 trades
- `insider_activity`: 3 signals, 0 trades
- `congressional_trades`: 3 signals, 2 trades
- `regulatory_pipeline`: 2 signals, 0 trades
- `litigation`: 1 signals, 0 trades

### Adaptive Cohort

**Open positions** — $1,801 deployed

| Ticker | Strategy | Dir | Entry | Shares | Value | Entry Date |
|--------|----------|-----|-------|--------|-------|------------|
| LGIH | congressional_trades | long | $38.13 | 13 | $495.69 | 2026-04-03 |
| IBP | congressional_trades | long | $268.71 | 1 | $268.71 | 2026-04-03 |
| PPTA | filing_analysis | long | $29.43 | 10 | $294.30 | 2026-04-03 |
| CMC | filing_analysis | long | $61.79 | 4 | $247.16 | 2026-04-03 |
| EPAC | filing_analysis | long | $35.44 | 7 | $248.08 | 2026-04-03 |
| MIR | filing_analysis | long | $19.00 | 13 | $247.00 | 2026-04-03 |

**Today's signals** — 14 signals, 6 traded

| | Ticker | Strategy | Dir | Score |
|---|--------|----------|-----|-------|
|   | META | insider_activity | neutral | 0.10 |
|   | GOOGL | insider_activity | short | 0.35 |
|   | JPM | insider_activity | neutral | 0.15 |
|   | BLK | regulatory_pipeline | neutral | 0.20 |
|   | WEC | regulatory_pipeline | neutral | 0.10 |
|   | ANF | filing_analysis | neutral | 0.20 |
| \* | PPTA | filing_analysis | long | 0.50 |
| \* | CMC | filing_analysis | long | 0.50 |
| \* | EPAC | filing_analysis | long | 0.50 |
| \* | MIR | filing_analysis | long | 0.50 |
|   | CTVA | litigation | short | 0.35 |
| \* | LGIH | congressional_trades | long | 9.00 |
|   | GS | congressional_trades | long | 4.00 |
| \* | IBP | congressional_trades | long | 2.00 |

**Strategy breakdown**

- `filing_analysis`: 5 signals, 4 trades
- `insider_activity`: 3 signals, 0 trades
- `congressional_trades`: 3 signals, 2 trades
- `regulatory_pipeline`: 2 signals, 0 trades
- `litigation`: 1 signals, 0 trades

## Regime Context

| Date | Overall | VIX | Credit | Yield Curve |
|------|---------|-----|--------|-------------|
| 2026-04-01 | stressed | elevated | normal | flat |
| 2026-04-02 | normal | normal | normal | flat |
| 2026-04-03 | normal | normal | normal | flat |
| 2026-04-04 | normal | normal | normal | flat |
