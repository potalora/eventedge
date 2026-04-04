# Daily Report: 2026-04-03

**Generated:** 2026-04-03 23:31
**Active generations:** 2

## Summary

| | gen_001 | gen_002 |
|---|---|---|
| **Description** | Initial 7-strategy baseline v1 | 9-strategy OpenBB enrichment: sector classification, analyst |
| **Commit** | 5f3730d28dc8 | a0a4c7aad273 |
| **Days run** | 3 | 3 |
| **Control signals** | 48 | 39 |
| **Control trades (open/closed)** | 5/0 | 5/0 |
| **Control capital deployed** | $1,132 | $1,560 |
| **Control hit rate** | N/A | N/A |
| **Control Sharpe** | N/A | N/A |
| **Control return** | N/A | N/A |
| **Adaptive signals** | 48 | 39 |
| **Adaptive trades (open/closed)** | 5/0 | 5/0 |
| **Adaptive capital deployed** | $1,278 | $1,658 |
| **Adaptive hit rate** | N/A | N/A |
| **Adaptive Sharpe** | N/A | N/A |
| **Adaptive return** | N/A | N/A |

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

**Today's signals** — 13 signals, 0 traded

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

**Strategy breakdown**

- `filing_analysis`: 17 signals, 3 trades
- `regulatory_pipeline`: 11 signals, 0 trades
- `insider_activity`: 10 signals, 0 trades
- `congressional_trades`: 9 signals, 2 trades
- `litigation`: 1 signals, 0 trades

### Adaptive Cohort

**Open positions** — $1,278 deployed

| Ticker | Strategy | Dir | Entry | Shares | Value | Entry Date |
|--------|----------|-----|-------|--------|-------|------------|
| LGIH | congressional_trades | long | $39.53 | 11 | $434.83 | 2026-04-01 |
| PPTA | filing_analysis | long | $28.12 | 8 | $224.96 | 2026-04-01 |
| ACN | filing_analysis | long | $198.29 | 1 | $198.29 | 2026-04-01 |
| CBRL | congressional_trades | long | $28.11 | 8 | $224.88 | 2026-04-01 |
| SCHL | filing_analysis | long | $39.06 | 5 | $195.30 | 2026-04-01 |

**Today's signals** — 13 signals, 0 traded

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

**Strategy breakdown**

- `filing_analysis`: 17 signals, 3 trades
- `regulatory_pipeline`: 11 signals, 0 trades
- `insider_activity`: 10 signals, 0 trades
- `congressional_trades`: 9 signals, 2 trades
- `litigation`: 1 signals, 0 trades

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

**Today's signals** — 13 signals, 5 traded

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

**Strategy breakdown**

- `filing_analysis`: 15 signals, 3 trades
- `insider_activity`: 9 signals, 0 trades
- `congressional_trades`: 9 signals, 2 trades
- `regulatory_pipeline`: 6 signals, 0 trades

### Adaptive Cohort

**Open positions** — $1,658 deployed

| Ticker | Strategy | Dir | Entry | Shares | Value | Entry Date |
|--------|----------|-----|-------|--------|-------|------------|
| LGIH | congressional_trades | long | $38.13 | 13 | $495.69 | 2026-04-03 |
| CBRL | congressional_trades | long | $28.89 | 12 | $346.68 | 2026-04-03 |
| PPTA | filing_analysis | long | $29.43 | 10 | $294.30 | 2026-04-03 |
| SCHL | filing_analysis | long | $39.20 | 7 | $274.40 | 2026-04-03 |
| CMC | filing_analysis | long | $61.79 | 4 | $247.16 | 2026-04-03 |

**Today's signals** — 13 signals, 5 traded

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

**Strategy breakdown**

- `filing_analysis`: 15 signals, 3 trades
- `insider_activity`: 9 signals, 0 trades
- `congressional_trades`: 9 signals, 2 trades
- `regulatory_pipeline`: 6 signals, 0 trades

## Regime Context

| Date | Overall | VIX | Credit | Yield Curve |
|------|---------|-----|--------|-------------|
| ? | normal | normal | normal | ? |
| ? | stressed | elevated | normal | ? |
| ? | stressed | elevated | normal | ? |
| ? | normal | normal | normal | ? |
| ? | normal | normal | normal | ? |
