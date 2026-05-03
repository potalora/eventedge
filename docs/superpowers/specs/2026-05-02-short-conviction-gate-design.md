# Stricter Short Conviction Gate (gen_007) — Design

**Date:** 2026-05-02
**Status:** Approved (design phase)

## Goal

Reduce the drag from single-strategy short signals in a risk-on regime by requiring **2+ event-driven strategies to converge** before a short trade is taken. Run as a new generation (`gen_007`) in parallel with `gen_005` and `gen_006` for A/B comparison over ≥60 days.

## Motivation

Observed in gen_005 month-to-date (2026-04-05 → 2026-05-01):

- $10K cohorts (long-only by eligibility rules) outperform $50K and $100K cohorts (short-eligible) across all 4 horizons by 5–10 percentage points.
- Specific shorts dragging: TXN (−44.75% as a short — TXN rallied), BRK/B, RH, LNN, MMM.
- Net contribution of the short book is negative even though one short (SNYR, −75% on the underlying) is a big winner.

Standard institutional practice gates shorts more strictly than longs because:

- Shorts have negative expected drift in equity markets (long-term equity premium).
- Loss profile is unbounded.
- Borrow cost is asymmetric drag.

This design tests whether requiring multi-strategy convergence on shorts captures the high-conviction shorts (SNYR-style, where multiple strategies see the same red flags) while filtering the lower-conviction shorts that have been bleeding.

## Non-Goals

- Adding a regime gate (defer — would require its own A/B).
- Changing the threshold for longs (defer — gen_005/gen_006 long behavior is the baseline).
- Changing position sizing for shorts (defer — only entry gating).
- Changing the cohort eligibility matrix (50K+/3m+ for shorts unchanged).

## The Change

**File:** `tradingagents/strategies/trading/portfolio_committee.py`

The active gens use the LLM path (`_llm_synthesize`) as primary; `_rule_based_synthesize` is the fallback. The gate must be enforced in **both** paths plus a **post-filter** as a belt-and-braces guarantee against LLM non-compliance.

### 1. Pre-filter signals before LLM (in `_llm_synthesize`)

Before calling `_build_prompt`, drop short signals that don't have multi-strategy support. Group signals by `(ticker, direction)` and remove any short ticker with fewer than 2 distinct strategies:

```python
# After existing short_eligible pre-filter
shorts_by_ticker: dict[str, set[str]] = {}
for s in filtered_signals:
    if s.get("direction") == "short":
        shorts_by_ticker.setdefault(s.get("ticker", ""), set()).add(s.get("strategy", ""))
single_strategy_short_tickers = {t for t, strats in shorts_by_ticker.items() if len(strats) < 2}
filtered_signals = [
    s for s in filtered_signals
    if not (s.get("direction") == "short" and s.get("ticker") in single_strategy_short_tickers)
]
```

This means the LLM never sees single-strategy short signals, removing temptation.

### 2. Post-filter LLM output (in `_llm_synthesize`)

After `_parse_llm_response`, drop any short recommendation with fewer than 2 `contributing_strategies`:

```python
recs = self._parse_llm_response(text)
if recs:
    recs = [
        r for r in recs
        if not (r.direction == "short" and len(r.contributing_strategies) < 2)
    ]
return recs
```

Defends against the LLM hallucinating a `contributing_strategies: ["earnings_call"]` short despite the pre-filter.

### 3. Rule-based fallback (in `_rule_based_synthesize`, around line 180)

**Before:**
```python
num_strategies = len(strategies)
if num_strategies < 2:
    if consensus_score < 0.5:
        continue
```

**After:**
```python
num_strategies = len(strategies)
if num_strategies < 2:
    if direction == "short":
        continue  # Shorts require 2+ strategy convergence (gen_007 gate)
    if consensus_score < 0.5:
        continue
```

### 4. Update LLM system prompt

Change the existing line:
> "Multi-strategy convergence (2+ strategies on same ticker) is much stronger than single-strategy signals. For single-strategy signals, only recommend if the event is clearly material (score >= 2.0 or strong catalyst)."

to:
> "Multi-strategy convergence (2+ strategies on same ticker) is much stronger than single-strategy signals. **HARD RULE: never recommend a SHORT trade unless 2+ strategies agree on the same ticker.** For single-strategy LONG signals, only recommend if the event is clearly material (score >= 2.0 or strong catalyst)."

Net effect across all four changes:
- Multi-strategy shorts (≥2) pass through with no change.
- Single-strategy shorts at any consensus score are rejected at three layers (pre-filter, LLM prompt, post-filter, rule-based fallback).
- Long behavior is unchanged.

Sector caps, regime alignment, sizing, sector concentration, short exposure caps, and cohort eligibility are all unchanged.

## Tests

`tests/test_short_gate.py` (new):

**Rule-based path (LLM disabled):**
1. **`test_rule_single_strategy_short_rejected_high_consensus`** — one short signal at `score=0.9, confidence=1.0` → no recommendation for that ticker.
2. **`test_rule_single_strategy_long_accepted_at_threshold`** — one long signal at `score=0.6, confidence=1.0` → recommendation produced (regression baseline).
3. **`test_rule_two_strategy_short_accepted`** — two strategies both shorting same ticker → recommendation with `direction="short"`.
4. **`test_rule_short_blocked_for_long_only_cohort_takes_precedence`** — single-strategy short on a `short_eligible=False` cohort → rejected (verifies cohort gate fires independently of the conviction gate).

**LLM path (mocked LLM client):**
5. **`test_llm_pre_filter_drops_single_strategy_shorts`** — patch `_call_llm` to capture the prompt; pass mixed signals; assert single-strategy shorts do not appear in the prompt's signal block.
6. **`test_llm_post_filter_drops_compliant_short`** — patch `_call_llm` to return a JSON array containing a short with `contributing_strategies=["earnings_call"]`; assert the rec is dropped.
7. **`test_llm_multi_strategy_short_passes`** — patch `_call_llm` to return a short with `contributing_strategies=["earnings_call","insider_activity"]`; assert it survives.
8. **`test_llm_long_unchanged`** — patch `_call_llm` to return a single-strategy long; assert it passes through.

Run via `.venv/bin/python -m pytest tests/test_short_gate.py -v`.

Existing tests that exercise the committee (`test_committee_vehicle.py`, `test_short_risk_gates.py`, `test_integration_shorts.py`) must still pass.

## Generation Setup

After the change is committed to `main`:

```bash
python scripts/run_generations.py start "Shorts require 2+ strategy convergence (gen_007)"
```

This:

- Reads current HEAD commit.
- Creates a detached worktree at `.worktrees/gen_007/`.
- Initializes 16 cohort state directories at `data/generations/gen_007/horizon_*/size_*/`.
- Adds `gen_007` to `manifest.json` with `status: active`.

From the next `run-daily` invocation onward, gen_007 runs alongside gen_005 and gen_006 with the new short gate active. Both control (fixed confidence) and adaptive cohorts inherit the same code change.

## Comparison & Evaluation

The existing dashboard (`scripts/email_dashboard.py`) iterates active generations from the manifest, so gen_007 appears automatically once it has equity data. After ~30 days the equity-curve facet shows whether gen_007's return is converging toward gen_005/gen_006 or diverging.

Quantitative comparison:

```bash
python scripts/run_generations.py compare --gens gen_005,gen_006,gen_007
```

**Evaluation window: 60 days minimum.** Do not declare a winner before 2026-07-01. Until then, treat gen_007 as a research run.

Decision criteria at 60 days:

- **Keep & promote**: gen_007 weighted return ≥ baseline gens AND short book positive contribution AND no obvious "lost a SNYR-class winner" event.
- **Kill**: gen_007 underperforms baselines by ≥3pp AND missed multiple multi-strategy short opportunities (review trade journal).
- **Continue**: anything in between — let it run another 30 days.

## Risks

| Risk | Mitigation |
|------|-----------|
| Lost single-strategy short winners (SNYR-style) | Logged in trade journal; comparable to gen_005's history at 60-day review |
| Short book too small to hedge in a regime flip | If regime flips to `stressed`/`crisis`, re-evaluate immediately rather than waiting 60 days |
| Rule interaction surprise (some short was firing because of stacked single-strategy signals from N tickers, not consensus on one) | Tests cover the gate; review first week's actual rejections via signal_journal entries marked `traded=false` with reason `"single_strategy_short"` |

## Out of Scope (future work)

- Logging rejection reason in `signal_journal` for easier post-mortem (currently the journal records signal but not why a non-trade happened). Worth adding if gen_007 review is hard to interpret.
- Regime gate (option C from brainstorming).
- Asymmetric size haircut for shorts (option D).
- Conviction threshold bump for longs (separate research).
