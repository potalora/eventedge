# gen_002 "Risk Discipline" Bundle — Design

**Date:** 2026-06-09
**Status:** Approved (design); implementation pending
**Branch:** `feat/gen002-risk-discipline`
**Baseline:** gen_001 @ commit `417b5d8` (frozen, unchanged)

## Motivation

During Jun 1–9 2026 the gen_001 paper book drew down to **−3.66%** (aggregate across 16 cohorts). Root-cause analysis (see `docs/reports/2026-06-09-gen001-daily-report.md`) found the loss was driven by a market-wide tech pullback (SPY −2.6%, QQQ −4.1%, XLK −5.4% peak-to-trough −8.8%; VIX 15→21.5) hitting a **long-only, tech-concentrated book**. The −8% stop worked correctly (3–8 day holds, no whipsaw), but three structural behaviors amplified the drawdown:

1. **Re-entry churn** — stopped names were re-bought into the downtrend within 1–3 days (BA stopped Jun 4 → re-bought Jun 5; T stopped Jun 5 → re-bought Jun 8).
2. **Effectively long-only** — 0 shorts cleared the committee in 6 days despite plentiful short signals.
3. **Regime model blind to volatility spikes** — VIX hit 21.5 with zero effect on long sizing (the "stressed" cutoff is 25).

These are **behavioral changes** (they change which trades happen), so unlike the earlier realized-P&L *bug fix* (applied in-place, no measurable delta), they warrant an **A/B generation**: gen_001 stays frozen as the baseline, gen_002 carries the changes, both run daily on shared data, and we compare forward behavior.

## Design Principle: config-toggleable behaviors

All three changes are gated behind config flags in a new `autoresearch.risk_discipline` block in `tradingagents/default_config.py`. **Defaults equal gen_001's current behavior**, so:

- The frozen gen_001 worktree is unaffected even though the code is shared.
- gen_002's worktree commit sets the flags to their active values.
- A future gen_003 can isolate any single variable by flipping one flag — no code change needed. This recovers the per-change attribution that bundling all three into gen_002 otherwise sacrifices.

```yaml
autoresearch:
  risk_discipline:
    reentry_cooldown_days: 0          # 0 = disabled (gen_001).      gen_002 → 7
    short_conviction_threshold: 0.60  # gen_001 short gate.          gen_002 → 0.45
    regime_vix_stressed: 25.0         # gen_001 "stressed" cutoff.   gen_002 → 20.0
```

---

## Behavior 1 — Re-entry cooldown

**Problem:** strategies re-signal a just-stopped name faster than it recovers, so the book re-buys into a downtrend.

**Mechanism:** a new gate in `RiskGate.check()`. The engine computes a set of "cooling" tickers — names whose **most recent `stop_loss` exit** was within `reentry_cooldown_days` of the current trading date — from the persisted closed-trade history, and passes that set into the risk gate. The gate rejects entries for those tickers with reason `cooldown: stopped <N>d ago`.

**Design decisions:**
- **Keyed on ticker** (not ticker+strategy): the name is in a downtrend regardless of which strategy likes it.
- **Triggered only by `stop_loss` exits**: a `take_profit` winner is never blocked from re-entry. Other exit reasons do not trigger cooldown.
- **Enforced in the risk gate, not the committee**: the committee still freely *recommends*; the risk gate is the existing "hard limits" layer (`max_positions`, `duplicate`, `cash_reserve`), so a stopped-name block belongs alongside them. Keeps the committee pure and the gate independently testable.
- **Value: 7 calendar days.** Signals re-fire within 1–3 days and holds ran 3–8 days, so a week outlasts the re-signal window without permanently banning a name.

**Insertion point:** `tradingagents/strategies/trading/risk_gate.py` — `RiskGateConfig` gains `reentry_cooldown_days`; `RiskGate.check()` gains a cooling-set parameter (or a setter populated per run). The engine (`multi_strategy_engine.py`, where `RiskGate` is constructed/used) computes the cooling set from `StateManager` closed trades.

---

## Behavior 2 — Conviction shorts

**Problem:** the committee permits shorts in eligible cohorts (50k+ / 3m+) but 0 cleared in 6 days. The gate requires 2+ strategies shorting one ticker OR a single strategy with **LLM conviction ≥ 0.60**. The LLM emitted short convictions of 0.25–0.55 all week — just under the bar — and rule-based shorts carry conviction 0.0 by deliberate design (prior commit: "gate shorts on real LLM conviction, not raw score").

**Mechanism:** make `PortfolioCommittee.SHORT_CONVICTION_THRESHOLD` config-driven (read at construction; default 0.60). gen_002 sets it to **0.45**.

**Explicitly unchanged (preserves prior principles):**
- Rule-based shorts still carry conviction 0.0 and cannot clear on score alone (the "raw score ≠ conviction" fix stays intact).
- Mixed long+short tickers still resolve by the weighted direction vote (a strong long still beats a weak short on the same ticker).
- Shorts still fire only in short-eligible cohorts and only when short wins direction consensus.

**Expected effect:** a *trickle* of shorts (e.g., litigation BTGO at 0.45 on a short-only-consensus ticker), not a flood. Intentionally conservative — tests whether real conviction shorts help without opening the floodgates.

**Insertion point:** `tradingagents/strategies/trading/portfolio_committee.py` — replace the `SHORT_CONVICTION_THRESHOLD` class constant with an instance value read from config at construction.

---

## Behavior 3 — Regime / VIX trigger

**Problem:** VIX spiked to 21.5 on Jun 8 (a clear risk-off session) but `overall_regime` stayed "normal" — the `_classify_regime` "stressed" cutoff is `VIX > 25` — so new longs got no caution overlay.

**Mechanism:** lower the `stressed` VIX cutoff in `_classify_regime` from the hard-coded `25` to config `regime_vix_stressed` (gen_002 = **20**). **No new alignment code:** a spike to ~20+ now flips `overall_regime` → `stressed`, and the **existing** `_assess_regime_alignment` in the committee then sizes new longs ×0.6 (misaligned) and marks shorts "aligned" (×1.0).

**Design decisions:**
- **Sizing, not blocking:** existing holds are untouched; only *new* longs during a spike are throttled. The committee's confidence→size multiplier already implements this.
- **Composes with Behavior 2:** a spike makes shorts "aligned", amplifying the newly-enabled conviction shorts exactly when they're most wanted.
- **Threshold, not velocity:** a one-line config change reusing tested machinery. VIX-velocity ("VIX +30% in 3 days") is a cleaner signal but needs VIX-history plumbing — deferred to a later gen if the threshold version proves the concept.

**Insertion point:** `tradingagents/strategies/orchestration/multi_strategy_engine.py` — `_classify_regime` reads the stressed cutoff from config instead of the literal `25`. The `thresholds` dict in `_build_regime_model` should reflect the configured value for snapshot transparency.

---

## Testing (TDD, all offline / mocked — no API calls)

- **Cooldown gate:** a ticker stopped within N days is rejected; a `take_profit` exit does not block; cooldown expires after N days; `reentry_cooldown_days=0` disables the gate entirely (baseline parity).
- **Short threshold:** at 0.45, a 0.45-conviction short clears and a 0.44 does not; a rule-based short with conviction 0.0 is still blocked; at default 0.60, behavior is unchanged from gen_001.
- **Regime classification:** VIX 21.5 → "stressed" when `regime_vix_stressed=20`, → "normal" when 25; alignment then sizes a new long down and marks a short aligned; snapshot `thresholds` reflects the configured value.
- **Config plumbing:** defaults reproduce gen_001 behavior across all three.

Test files mirror source: `tests/test_reentry_cooldown.py`, additions to existing committee/regime tests as appropriate. Mock LLM and external services per project testing rules.

## Rollout

1. Implement on `feat/gen002-risk-discipline` (TDD).
2. Full suite green; PR to `main` (push to `private` remote only, per project convention — never to upstream `origin`).
3. `python scripts/run_generations.py start "gen_002 risk-discipline: re-entry cooldown + conviction shorts + VIX regime trigger"` snapshots the merged commit as **gen_002** with the flags active.
4. **gen_001 stays frozen on `417b5d8`** as the baseline (defaults already match its behavior).
5. Both gens run daily on shared data fetch. Compare **forward** behavior (not cumulative-from-day-1: gen_002 starts flat while gen_001 carries the drawdown book).

## Out of scope (deferred)

- filing_analysis micro-cap price/liquidity floor (GWLL −42%, GURE −13.5%).
- VIX-velocity regime trigger.
- Short-gate direction-consensus changes (letting a high-conviction short override a competing long on the same ticker).
- Per-name cross-cohort concentration cap.

These remain candidates for gen_003+ once gen_002's effect is measurable.
