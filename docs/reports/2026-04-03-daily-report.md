# Daily Trading Report — Thursday, April 3, 2026

## Market Regime

VIX declined for the third consecutive session, from 25.2 (elevated) on Tuesday to 23.9 today. The regime model downgraded from **stressed** to **normal** after Wednesday's session. Credit spreads remain benign, yield curve flat. No regime-driven position adjustments were triggered.

| Date | VIX | VIX Regime | Overall | Credit | Yield Curve |
|------|-----|------------|---------|--------|-------------|
| Apr 1 | 25.2 | elevated | stressed | normal | flat |
| Apr 2 | 24.5 | normal | normal | normal | flat |
| Apr 3 | 23.9 | normal | normal | normal | flat |

---

## Generation Overview

Three generations are now active, each running dual cohorts (control + adaptive) on frozen code snapshots:

| | gen_001 | gen_002 | gen_003 |
|---|---|---|---|
| **Description** | 7-strategy baseline | 9-strategy + OpenBB enrichment | 10-strategy + USDA/Drought Monitor |
| **Commit** | `5f3730d` | `a0a4c7a` | `b368114` |
| **Started** | Apr 1 | Apr 3 | Apr 4 |
| **Days running** | 4 | 1 | 1 |
| **Strategies active** | 7 | 9 | 10 |
| **New in this gen** | — | OpenBB sector/estimates, supply_chain, govt_contracts | weather_ag (USDA + Drought + NOAA) |

---

## Today's Signals — Cross-Generation Comparison

### Signals that fired across all three generations

These signals are consistent regardless of code version, indicating robust underlying data:

| Ticker | Strategy | Direction | Conviction | Notes |
|--------|----------|-----------|------------|-------|
| **LGIH** | congressional_trades | long | 9.0 | Strongest signal across the board. Multiple congress members buying. |
| **GS** | congressional_trades | long | 4.0 | Not traded — likely priced out by position sizing ($863/share). |
| **CBRL** | congressional_trades | long | 2.0 | Traded in all gens. |
| **PPTA** | filing_analysis | long | 0.5 | Recent SEC filing flagged. No LLM enrichment (conviction=0.0). |
| **CMC** | filing_analysis | long | 0.5 | Same pattern — filing gate triggered, LLM not yet scoring. |
| **META** | insider_activity | neutral | 0.1 | Low conviction, not traded. |
| **GOOGL** | insider_activity | short | 0.35 | Moderate insider selling signal; blocked from trading (compliance). |
| **JPM** | insider_activity | neutral | 0.15 | Low conviction. |
| **BLK** | regulatory_pipeline | neutral | 0.2 | Regulatory noise, no actionable signal. |
| **WEC** | regulatory_pipeline | neutral | 0.1 | Same. |
| **ANF** | filing_analysis | neutral | 0.1-0.2 | Filing detected but LLM scored as uninteresting. |

### Signals unique to gen_001

| Ticker | Strategy | Direction | Score | Notes |
|--------|----------|-----------|-------|-------|
| **NVDA** | insider_activity | short | 64.5 | Anomalous score — likely a raw data artifact (insider sale $ amount leaked into score field). gen_002 scored same event at 0.38, gen_003 at 0.35. This is a bug in gen_001's scoring path. |

### Signals unique to gen_002 and gen_003

| Ticker | Strategy | Direction | Score | Notes |
|--------|----------|-----------|-------|-------|
| **EPAC** | filing_analysis | long | 0.5 | New filing detected. Traded in gen_003, signal-only in gen_002. |
| **MIR** | filing_analysis | long | 0.5 | Same pattern. Small-cap ($19/share), higher share count allocation. |
| **CTVA** | litigation | short | 0.35 | Corteva Agriscience — active litigation. Interesting that this is also an ag ticker in gen_003's weather universe, though weather_ag didn't fire independently. |

### Strategies that produced no signals today

| Strategy | Why |
|----------|-----|
| **earnings_call** | No upcoming earnings events in the screened universe. |
| **supply_chain** | No disruption news matching gates (gen_002+ only). |
| **govt_contracts** | No new contract awards matching gates (gen_002+ only). |
| **state_economics** | No FRED data triggering macro gates (gen_002+ only). |
| **weather_ag** | No drought/NOAA/USDA gates triggered (gen_003 only). Early April — growing season starts in May. Correct behavior. |

---

## Open Positions

### gen_001 (entered Apr 1, held 2 trading days)

| Ticker | Strategy | Dir | Entry | Current | Shares (C/A) | Value (C/A) | Unrealized |
|--------|----------|-----|-------|---------|--------------|-------------|------------|
| LGIH | congressional | long | $39.53 | $38.13 | 10 / 11 | $395 / $435 | -3.5% |
| CBRL | congressional | long | $28.11 | $28.89 | 8 / 8 | $225 / $225 | +2.8% |
| PPTA | filing | long | $28.12 | $29.43 | 7 / 8 | $197 / $225 | +4.7% |
| ACN | filing | long | $198.29 | ~$198 | 1 / 1 | $198 / $198 | ~0% |
| SCHL | filing | long | $39.06 | $39.20 | 3 / 5 | $117 / $195 | +0.4% |

**Total deployed:** $1,132 (control) / $1,278 (adaptive)

Early read: PPTA (+4.7%) and CBRL (+2.8%) are working. LGIH is underwater (-3.5%). ACN and SCHL are flat. The adaptive cohort allocated more shares to the winners (PPTA: 8 vs 7, LGIH: 11 vs 10, SCHL: 5 vs 3) — sizing differences come from non-deterministic portfolio committee LLM calls, since both cohorts still use fixed confidence=0.5.

### gen_002 (entered Apr 3, Day 1)

| Ticker | Strategy | Dir | Entry | Shares (C/A) | Value (C/A) |
|--------|----------|-----|-------|--------------|-------------|
| LGIH | congressional | long | $38.13 | 13 / 13 | $496 / $496 |
| CBRL | congressional | long | $28.89 | 12 / 12 | $347 / $347 |
| PPTA | filing | long | $29.43 | 8 / 10 | $235 / $294 |
| SCHL | filing | long | $39.20 | 6 / 7 | $235 / $274 |
| CMC | filing | long | $61.79 | 4 / 4 | $247 / $247 |

**Total deployed:** $1,560 (C) / $1,658 (A)

gen_002 is more aggressive on Day 1 — higher share counts across the board vs gen_001's first day. This likely reflects OpenBB enrichment data influencing the portfolio committee's sizing decisions.

### gen_003 (entered Apr 3, Day 1)

| Ticker | Strategy | Dir | Entry | Shares (C/A) | Value (C/A) |
|--------|----------|-----|-------|--------------|-------------|
| LGIH | congressional | long | $38.13 | 13 / 13 | $496 / $496 |
| CBRL | congressional | long | $28.89 | 13 / 13 | $376 / $376 |
| PPTA | filing | long | $29.43 | 10 / 10 | $294 / $294 |
| CMC | filing | long | $61.79 | 4 / 4 | $247 / $247 |
| EPAC | filing | long | $35.44 | 7 / 8 | $248 / $284 |
| MIR | filing | long | $19.00 | 13 / 15 | $247 / $285 |

**Total deployed:** $1,908 (C) / $1,981 (A)

gen_003 opened **6 positions** vs gen_002's 5 — picking up EPAC and MIR (both filing_analysis). It deployed the most capital on Day 1 ($1,908-$1,981 of $10,000). The two new positions are small-caps which allows higher share counts.

---

## Key Observations

**1. Filing analysis is the most active strategy.** It generated the most signals and trades across all generations. However, all filing_analysis trades show `llm_conviction: 0.0` — meaning the LLM enrichment step isn't scoring these candidates. They're being traded on the raw gate signal (score=0.5) alone. This is a known gap: the filing_analysis LLM prompt may not be returning structured conviction scores correctly.

**2. Congressional trades are the highest-conviction signals.** LGIH (score=9.0) and GS (score=4.0) dominate. These scores come from the number of congress members trading, not LLM conviction. GS is consistently filtered out by position sizing (a single share costs $863, exceeding per-position allocation limits).

**3. The NVDA score=64.5 in gen_001 is a bug.** gen_002's OpenBB enrichment normalized this to 0.38, and gen_003 to 0.35. The raw insider transaction dollar amount appears to be leaking into the score field in the pre-OpenBB code path.

**4. weather_ag correctly produced no signals.** April 3 is before the growing season (May-Sep). No drought, NOAA heat, or USDA crop decline gates triggered. The strategy is wired up and will activate when conditions warrant — likely mid-summer.

**5. Adaptive vs Control hasn't diverged yet.** Both cohorts use fixed confidence=0.5 until the adaptive cohort accumulates enough journal data for its weekly learning loop. The only sizing differences come from non-deterministic portfolio committee LLM calls. Meaningful divergence expected after ~2 weeks.

**6. No closed trades yet.** All positions are within their holding periods (default 21 days). First exits expected around April 22 for gen_001's positions.

---

## Strategy Hit Rates

Too early to measure — no trades have been closed. First meaningful data expected mid-to-late April when gen_001's 21-day holding periods expire.

---

## What to Watch Next Week

- **LGIH** — Largest position across all gens, currently -3.5% in gen_001. Congressional buy signal was strong (9.0). Worth monitoring whether the thesis plays out.
- **PPTA** — Best performer so far (+4.7%). Filing-driven. Watch for follow-through.
- **CTVA litigation** — Short signal at 0.35 conviction. Also in gen_003's weather_ag ticker universe. If weather conditions deteriorate while litigation continues, could see converging signals.
- **weather_ag activation** — Growing season starts next month. First signals expected if drought develops in the corn belt or USDA crop conditions decline.
- **Filing analysis LLM conviction** — All filing trades have conviction=0.0. Investigate whether the LLM prompt is returning scores in the expected format.
