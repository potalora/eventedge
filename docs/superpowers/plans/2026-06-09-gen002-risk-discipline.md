# gen_002 Risk Discipline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three config-toggleable risk-discipline behaviors (re-entry cooldown, lower short-conviction threshold, VIX-stressed regime trigger) whose defaults reproduce gen_001, then launch gen_002 with them active.

**Architecture:** A new `autoresearch.risk_discipline` config block holds three knobs, each read by the one component it affects: the risk gate (cooldown), the portfolio committee (short threshold), and the engine's regime classifier (VIX cutoff). Defaults equal current behavior, so the frozen gen_001 worktree is unaffected and a later gen can isolate any single knob.

**Tech Stack:** Python 3.11, pytest, existing autoresearch modules (`risk_gate.py`, `portfolio_committee.py`, `multi_strategy_engine.py`, `default_config.py`).

**Spec:** `docs/superpowers/specs/2026-06-09-gen002-risk-discipline-design.md`

**Branch:** `feat/gen002-risk-discipline` (already created; spec already committed there)

**Test command:** `.venv/bin/python -m pytest <path> -v`

---

### Task 1: Add `risk_discipline` config block

**Files:**
- Modify: `tradingagents/default_config.py:153-158` (insert a new block after `short_selling`)
- Test: `tests/test_risk_discipline_config.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_risk_discipline_config.py
"""Defaults for the gen_002 risk_discipline config block must equal gen_001 behavior."""
from tradingagents.default_config import DEFAULT_CONFIG


def test_risk_discipline_defaults_match_baseline():
    rd = DEFAULT_CONFIG["autoresearch"]["risk_discipline"]
    assert rd["reentry_cooldown_days"] == 0          # disabled = gen_001
    assert rd["short_conviction_threshold"] == 0.60  # gen_001 short gate
    assert rd["regime_vix_stressed"] == 25.0         # gen_001 stressed cutoff
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_risk_discipline_config.py -v`
Expected: FAIL with `KeyError: 'risk_discipline'`

- [ ] **Step 3: Add the config block**

In `tradingagents/default_config.py`, immediately after the `short_selling` block (closes at line 158 `},`) and before the closing `},` of `autoresearch` (line 159), insert:

```python
        # gen_002 risk-discipline knobs. Defaults reproduce gen_001 behavior;
        # each is read by exactly one component. Flip per generation.
        "risk_discipline": {
            "reentry_cooldown_days": 0,         # block re-entry of a stopped name for N days (0 = off)
            "short_conviction_threshold": 0.60, # min LLM conviction for a single-strategy short
            "regime_vix_stressed": 25.0,        # VIX above this => "stressed" regime
        },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_risk_discipline_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/default_config.py tests/test_risk_discipline_config.py
git commit -m "feat(config): add risk_discipline block (defaults = gen_001 behavior)"
```

---

### Task 2: `compute_cooling_tickers` pure helper

**Files:**
- Modify: `tradingagents/strategies/trading/risk_gate.py` (add module-level function + `datetime` import)
- Test: `tests/test_reentry_cooldown.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reentry_cooldown.py
"""Re-entry cooldown: block names stopped out within the cooldown window."""
from tradingagents.strategies.trading.risk_gate import compute_cooling_tickers


def _stop(ticker, exit_date, reason="stop_loss"):
    return {"ticker": ticker, "exit_date": exit_date, "exit_reason": reason, "status": "closed"}


class TestComputeCoolingTickers:
    def test_recent_stop_is_cooling(self):
        closed = [_stop("CRWD", "2026-06-09")]
        assert compute_cooling_tickers(closed, "2026-06-11", 7) == {"CRWD"}

    def test_same_day_stop_is_cooling(self):
        closed = [_stop("T", "2026-06-09")]
        assert compute_cooling_tickers(closed, "2026-06-09", 7) == {"T"}

    def test_stop_outside_window_not_cooling(self):
        closed = [_stop("BA", "2026-06-01")]
        assert compute_cooling_tickers(closed, "2026-06-09", 7) == set()  # 8 days >= 7

    def test_take_profit_does_not_cool(self):
        closed = [_stop("IBM", "2026-06-09", reason="take_profit")]
        assert compute_cooling_tickers(closed, "2026-06-10", 7) == set()

    def test_zero_cooldown_disables(self):
        closed = [_stop("CRWD", "2026-06-09")]
        assert compute_cooling_tickers(closed, "2026-06-09", 0) == set()

    def test_malformed_dates_ignored(self):
        closed = [_stop("X", ""), _stop("Y", None), {"ticker": "Z", "exit_reason": "stop_loss"}]
        assert compute_cooling_tickers(closed, "2026-06-09", 7) == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_reentry_cooldown.py -v`
Expected: FAIL with `ImportError: cannot import name 'compute_cooling_tickers'`

- [ ] **Step 3: Implement the helper**

In `tradingagents/strategies/trading/risk_gate.py`, ensure `from datetime import datetime` is present near the top imports (add it if missing). Then add this module-level function after the existing `_estimate_borrow_cost` function (around line 27, before `class RiskGateConfig`):

```python
def compute_cooling_tickers(
    closed_trades: list[dict],
    trading_date: str,
    cooldown_days: int,
) -> set[str]:
    """Tickers with a stop_loss exit within ``cooldown_days`` of ``trading_date``.

    Used to block re-entry into names just stopped out (they tend to keep
    falling and get re-bought into the downtrend). Returns an empty set when
    ``cooldown_days <= 0`` so callers get baseline (gen_001) behavior.
    """
    if cooldown_days <= 0:
        return set()
    try:
        td = datetime.strptime(trading_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        return set()
    cooling: set[str] = set()
    for t in closed_trades:
        if t.get("exit_reason") != "stop_loss":
            continue
        exit_date = t.get("exit_date")
        ticker = t.get("ticker", "")
        if not exit_date or not ticker:
            continue
        try:
            xd = datetime.strptime(exit_date, "%Y-%m-%d")
        except (ValueError, TypeError):
            continue
        if 0 <= (td - xd).days < cooldown_days:
            cooling.add(ticker)
    return cooling
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_reentry_cooldown.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/trading/risk_gate.py tests/test_reentry_cooldown.py
git commit -m "feat(risk): compute_cooling_tickers helper for re-entry cooldown"
```

---

### Task 3: `RiskGateConfig.reentry_cooldown_days` + from_dict

**Files:**
- Modify: `tradingagents/strategies/trading/risk_gate.py:44` (dataclass field) and `:68` (from_dict)
- Test: `tests/test_reentry_cooldown.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_reentry_cooldown.py`:

```python
from tradingagents.strategies.trading.risk_gate import RiskGateConfig


class TestRiskGateConfigCooldown:
    def test_default_cooldown_is_zero(self):
        assert RiskGateConfig().reentry_cooldown_days == 0

    def test_from_dict_reads_risk_discipline(self):
        cfg = RiskGateConfig.from_dict(
            {"autoresearch": {"risk_discipline": {"reentry_cooldown_days": 7}}}
        )
        assert cfg.reentry_cooldown_days == 7

    def test_from_dict_defaults_to_zero(self):
        assert RiskGateConfig.from_dict({"autoresearch": {}}).reentry_cooldown_days == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_reentry_cooldown.py::TestRiskGateConfigCooldown -v`
Expected: FAIL with `TypeError` or `AttributeError` on `reentry_cooldown_days`

- [ ] **Step 3: Add the field and from_dict read**

In `RiskGateConfig` (dataclass), add a field after `cash_reserve_pct` (line 44):

```python
    reentry_cooldown_days: int = 0          # Block re-entry of stopped names for N days (0 = off)
```

In `RiskGateConfig.from_dict`, add a read (the method already has `rg = config.get("autoresearch", {}).get("risk_gate", {})`; the cooldown lives under `risk_discipline`, so read it separately). Inside `from_dict`, after the `rg = ...` line (line 56), add:

```python
        rd = config.get("autoresearch", {}).get("risk_discipline", {})
```

and add this kwarg to the `return cls(...)` call (after `cash_reserve_pct=...`, line 68):

```python
            reentry_cooldown_days=rd.get("reentry_cooldown_days", 0),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_reentry_cooldown.py::TestRiskGateConfigCooldown -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/trading/risk_gate.py tests/test_reentry_cooldown.py
git commit -m "feat(risk): RiskGateConfig.reentry_cooldown_days from risk_discipline"
```

---

### Task 4: RiskGate cooling set + `check()` gate

**Files:**
- Modify: `tradingagents/strategies/trading/risk_gate.py` (`__init__` ~line 93-98, new `set_cooling_tickers`, gate in `check()` after line 161)
- Test: `tests/test_reentry_cooldown.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_reentry_cooldown.py`:

```python
from tradingagents.strategies.trading.risk_gate import RiskGate
from tradingagents.execution.paper_broker import PaperBroker


def _gate(cooldown_days=7):
    broker = PaperBroker(initial_capital=50_000)
    cfg = RiskGateConfig(total_capital=50_000, reentry_cooldown_days=cooldown_days)
    return RiskGate(cfg, broker)


class TestRiskGateCooldownGate:
    def test_cooling_ticker_rejected(self):
        gate = _gate()
        gate.set_cooling_tickers({"CRWD"})
        passed, reason = gate.check("CRWD", "long", 1000.0, "quantum_readiness")
        assert passed is False
        assert "cooldown" in reason

    def test_non_cooling_ticker_allowed(self):
        gate = _gate()
        gate.set_cooling_tickers({"CRWD"})
        passed, _ = gate.check("MSFT", "long", 1000.0, "congressional_trades")
        assert passed is True

    def test_no_cooling_set_allows_all(self):
        gate = _gate()
        passed, _ = gate.check("CRWD", "long", 1000.0, "quantum_readiness")
        assert passed is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_reentry_cooldown.py::TestRiskGateCooldownGate -v`
Expected: FAIL with `AttributeError: 'RiskGate' object has no attribute 'set_cooling_tickers'`

- [ ] **Step 3: Implement cooling set + gate**

In `RiskGate.__init__` (after `self._margin_used = 0.0`, line 98), add:

```python
        self._cooling_tickers: set[str] = set()
```

Add a method (after `__init__`, before `check`):

```python
    def set_cooling_tickers(self, tickers: set[str]) -> None:
        """Set tickers in re-entry cooldown (stopped out within the cooldown window)."""
        self._cooling_tickers = set(tickers)
```

In `check()`, immediately after the duplicate check (the `return False, f"duplicate: ..."` block ending at line 161), add:

```python
        # 7b. Re-entry cooldown — skip names stopped out within the cooldown window
        if ticker in self._cooling_tickers:
            return False, f"cooldown: {ticker} stopped out recently"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_reentry_cooldown.py -v`
Expected: PASS (all classes)

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/trading/risk_gate.py tests/test_reentry_cooldown.py
git commit -m "feat(risk): re-entry cooldown gate in RiskGate.check()"
```

---

### Task 5: Wire cooldown into the engine

**Files:**
- Modify: `tradingagents/strategies/orchestration/multi_strategy_engine.py:256-270` (after closed-trades load / reconstruct)
- Test: `tests/test_30day_simulation.py` (existing integration tests verify no regression)

- [ ] **Step 1: Add the wiring**

In `run_paper_trade_phase`, the engine already loads `closed_trades_for_broker = self.state.load_paper_trades(status="closed")` (line 257) and reconstructs the broker. Immediately after the `reconstruct_from_trades` block (after the `logger.info("Reconstructed broker: ...")` call, ~line 270), add:

```python
        # Re-entry cooldown: block names stopped out within the cooldown window
        from tradingagents.strategies.trading.risk_gate import compute_cooling_tickers
        cooling = compute_cooling_tickers(
            closed_trades_for_broker,
            trading_date,
            bridge.risk_gate.config.reentry_cooldown_days,
        )
        bridge.risk_gate.set_cooling_tickers(cooling)
        if cooling:
            logger.info(
                "Re-entry cooldown active for %d tickers: %s",
                len(cooling), sorted(cooling),
            )
```

- [ ] **Step 2: Run the existing engine integration tests**

Run: `.venv/bin/python -m pytest tests/test_30day_simulation.py -v`
Expected: PASS (no regression — cooldown defaults to 0, so the set is empty and behavior is unchanged)

- [ ] **Step 3: Verify the wiring activates with cooldown on**

Run this one-off check (paste into a shell) to confirm a stopped name is excluded when the knob is on:

```bash
.venv/bin/python -c "
from tradingagents.strategies.trading.risk_gate import compute_cooling_tickers
closed=[{'ticker':'CRWD','exit_date':'2026-06-09','exit_reason':'stop_loss'}]
print('on :', compute_cooling_tickers(closed,'2026-06-11',7))
print('off:', compute_cooling_tickers(closed,'2026-06-11',0))
"
```
Expected: `on : {'CRWD'}` and `off: set()`

- [ ] **Step 4: Commit**

```bash
git add tradingagents/strategies/orchestration/multi_strategy_engine.py
git commit -m "feat(engine): wire re-entry cooldown into paper-trade phase"
```

---

### Task 6: Configurable short-conviction threshold

**Files:**
- Modify: `tradingagents/strategies/trading/portfolio_committee.py` (`__init__` ~line 99, `_short_passes_gate` line 70-77, usages line 228, 365, 419)
- Test: `tests/test_short_gate_config.py` (create); existing `tests/test_short_gate.py` must stay green

- [ ] **Step 1: Write the failing test**

```python
# tests/test_short_gate_config.py
"""The short-conviction threshold is config-driven; default stays 0.60 (gen_001)."""
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee


def _short(ticker, strategy, conviction):
    return {
        "ticker": ticker, "strategy": strategy, "direction": "short", "score": conviction,
        "metadata": {"llm_analysis": {"conviction": conviction}},
    }


class TestConfigurableShortThreshold:
    def test_default_threshold_is_baseline(self):
        c = PortfolioCommittee(config={})
        assert c._short_conviction_threshold == 0.60

    def test_config_lowers_threshold(self):
        c = PortfolioCommittee(
            config={"autoresearch": {"risk_discipline": {"short_conviction_threshold": 0.45}}}
        )
        assert c._short_conviction_threshold == 0.45

    def test_045_lets_a_045_short_clear(self):
        c = PortfolioCommittee(
            config={"autoresearch": {"risk_discipline": {"short_conviction_threshold": 0.45}}}
        )
        assert c._short_passes_gate([_short("BTGO", "litigation", 0.45)], c._short_conviction_threshold) is True

    def test_045_still_blocks_a_044_short(self):
        c = PortfolioCommittee(
            config={"autoresearch": {"risk_discipline": {"short_conviction_threshold": 0.45}}}
        )
        assert c._short_passes_gate([_short("BTGO", "litigation", 0.44)], c._short_conviction_threshold) is False

    def test_rule_based_short_still_blocked(self):
        # conviction 0.0 (no llm_analysis) must not clear even at 0.45
        c = PortfolioCommittee(
            config={"autoresearch": {"risk_discipline": {"short_conviction_threshold": 0.45}}}
        )
        raw = {"ticker": "COPX", "strategy": "commodity_macro", "direction": "short", "score": 0.50}
        assert c._short_passes_gate([raw], c._short_conviction_threshold) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_short_gate_config.py -v`
Expected: FAIL with `AttributeError: ... '_short_conviction_threshold'`

- [ ] **Step 3: Implement configurable threshold**

In `PortfolioCommittee.__init__`, after `self._size_profile = size_profile` (line 99), add:

```python
        self._short_conviction_threshold = float(
            self.config.get("autoresearch", {})
            .get("risk_discipline", {})
            .get("short_conviction_threshold", self.SHORT_CONVICTION_THRESHOLD)
        )
```

Change `_short_passes_gate` (line 70-77) to accept an optional threshold (keeps the class constant as default so existing `tests/test_short_gate.py` class-level calls still pass):

```python
    @classmethod
    def _short_passes_gate(
        cls, ticker_short_signals: list[dict], threshold: float | None = None
    ) -> bool:
        """True if a ticker's short signals clear the conviction gate."""
        if threshold is None:
            threshold = cls.SHORT_CONVICTION_THRESHOLD
        strategies = {s.get("strategy", "") for s in ticker_short_signals}
        if len(strategies) >= 2:
            return True
        max_conv = max((cls._signal_conviction(s) for s in ticker_short_signals), default=0.0)
        return max_conv >= threshold
```

Update the three instance usages:
- Line 228: `if max_conv < self.SHORT_CONVICTION_THRESHOLD:` → `if max_conv < self._short_conviction_threshold:`
- Line 365: `... if not self._short_passes_gate(ss)` → `... if not self._short_passes_gate(ss, self._short_conviction_threshold)`
- Line 419: `and r.confidence < self.SHORT_CONVICTION_THRESHOLD` → `and r.confidence < self._short_conviction_threshold`

- [ ] **Step 4: Run new + existing short-gate tests**

Run: `.venv/bin/python -m pytest tests/test_short_gate_config.py tests/test_short_gate.py -v`
Expected: PASS (new config tests + all existing short-gate tests still green)

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/trading/portfolio_committee.py tests/test_short_gate_config.py
git commit -m "feat(committee): config-driven short-conviction threshold (default 0.60)"
```

---

### Task 7: Configurable regime VIX-stressed threshold

**Files:**
- Modify: `tradingagents/strategies/orchestration/multi_strategy_engine.py` (`_build_regime_model` line 635-648, `_classify_regime` line 651-667)
- Test: `tests/test_regime_vix_threshold.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_regime_vix_threshold.py
"""VIX-stressed cutoff is config-driven; default 25 reproduces gen_001."""
import pandas as pd
import pytest

from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.state.state import StateManager
from tradingagents.strategies.modules import get_all_strategies


def _engine(tmp_path, stressed=None):
    state = StateManager(str(tmp_path / "state"))
    ar = {"state_dir": str(tmp_path / "state"), "total_capital": 5000}
    if stressed is not None:
        ar["risk_discipline"] = {"regime_vix_stressed": stressed}
    return MultiStrategyEngine(
        config={"autoresearch": ar}, strategies=get_all_strategies(), state_manager=state,
    )


class TestRegimeVixThreshold:
    def test_default_vix_21_is_normal(self, tmp_path):
        eng = _engine(tmp_path)  # default 25
        assert eng._classify_regime(21.5, 272.0, 0.0) == "normal"

    def test_lowered_threshold_makes_vix_21_stressed(self, tmp_path):
        eng = _engine(tmp_path, stressed=20.0)
        assert eng._classify_regime(21.5, 272.0, 0.0) == "stressed"

    def test_build_regime_model_reflects_threshold(self, tmp_path):
        eng = _engine(tmp_path, stressed=20.0)
        data = {"yfinance": {"vix": pd.DataFrame({"Close": [21.5]})}, "fred": {}}
        regime = eng._build_regime_model(data)
        assert regime["overall_regime"] == "stressed"
        assert regime["vix_regime"] == "elevated"
        assert regime["thresholds"]["vix"]["elevated"] == 20.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_regime_vix_threshold.py -v`
Expected: FAIL (default behavior classifies 21.5 as normal at 25, but the lowered-threshold and build_regime_model assertions fail until implemented)

- [ ] **Step 3: Implement configurable cutoff**

In `_build_regime_model` (line 615), after `overall = self._classify_regime(...)` (line 633), add:

```python
        stressed_vix = self.ar_config.get("risk_discipline", {}).get("regime_vix_stressed", 25.0)
```

Change the `vix_regime` line (637) to use it:

```python
            "vix_regime": "crisis" if vix_level > 35 else "elevated" if vix_level > stressed_vix else "normal" if vix_level > 15 else "low",
```

Change the thresholds dict `vix` entry (line 645) to:

```python
                "vix": {"low": 15, "elevated": stressed_vix, "crisis": 35},
```

In `_classify_regime` (line 651), read the cutoff and use it for the stressed branch (line 663):

```python
    def _classify_regime(self, vix: float, credit_bps: float, yc_slope: float) -> str:
        """Classify overall market regime."""
        stressed_vix = self.ar_config.get("risk_discipline", {}).get("regime_vix_stressed", 25.0)
        crisis_signals = 0
        if vix > 35:
            crisis_signals += 1
        if credit_bps > 600:
            crisis_signals += 1
        if yc_slope < -0.2:
            crisis_signals += 1

        if crisis_signals >= 2:
            return "crisis"
        if vix > stressed_vix or credit_bps > 400:
            return "stressed"
        if vix < 15 and credit_bps < 300:
            return "benign"
        return "normal"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_regime_vix_threshold.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/orchestration/multi_strategy_engine.py tests/test_regime_vix_threshold.py
git commit -m "feat(engine): config-driven VIX-stressed regime cutoff (default 25)"
```

---

### Task 8: Full suite green + merge prep

**Files:** none (verification + integration)

- [ ] **Step 1: Run the full test suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS — prior baseline was 562 passed; expect 562 + new tests (config 1, cooldown ~12, short-config 5, regime 3) all green. Investigate any failure before proceeding.

- [ ] **Step 2: Confirm baseline parity (defaults = gen_001)**

Run: `.venv/bin/python -c "
from tradingagents.default_config import DEFAULT_CONFIG
rd = DEFAULT_CONFIG['autoresearch']['risk_discipline']
assert rd == {'reentry_cooldown_days': 0, 'short_conviction_threshold': 0.60, 'regime_vix_stressed': 25.0}, rd
print('baseline defaults OK — gen_001 behavior preserved')
"`
Expected: `baseline defaults OK — gen_001 behavior preserved`

- [ ] **Step 3: Push branch and open PR (private remote only)**

```bash
git push -u private feat/gen002-risk-discipline
gh pr create --repo potalora/eventedge --base main --head feat/gen002-risk-discipline \
  --title "feat: gen_002 risk discipline (cooldown + conviction shorts + VIX regime trigger)" \
  --body "Implements docs/superpowers/specs/2026-06-09-gen002-risk-discipline-design.md. Three config-toggleable behaviors; defaults reproduce gen_001. Full suite green."
```

- [ ] **Step 4: Merge after review, sync main**

```bash
gh pr merge <number> --repo potalora/eventedge --merge --delete-branch
git checkout main && git pull private main
```

---

### Task 9: Launch gen_002 with the knobs active

> This task runs AFTER merge. gen_002 must run the **merged** code with the three flags ON. Because `default_config.py` defaults are baseline (off), the active values are set via gen_002's config — see Step 1.

**Files:**
- The launch reads `default_config.py`; to turn the knobs ON for gen_002 only (without disturbing gen_001), set them in the gen_002 environment/config. Confirm the mechanism before launching (Step 1).

- [ ] **Step 1: Decide how gen_002 turns the knobs on**

The generation runner executes each gen's frozen worktree with `AUTORESEARCH_STATE_DIR` set. Two options — confirm with the user / inspect `scripts/run_cohorts.py` and `default_config.py` loading:
  - **(a)** Set the active values directly in `default_config.py` on the merged commit (gen_001 is frozen on `417b5d8`, so it keeps the old defaults; gen_002 snapshots the merged commit with active values). This is the simplest and matches "defaults flip per generation via the snapshot."
  - **(b)** Override via env/config file read by `run_cohorts.py`.

Inspect: `Read scripts/run_cohorts.py` and confirm how config is assembled, then choose. If (a): change the three defaults to `7 / 0.45 / 20.0` in a follow-up commit on `main` BEFORE snapshotting — but that would also change gen_001 if it re-points. Since gen_001 is frozen on `417b5d8` (which has no `risk_discipline` block at all → components fall back to baseline), changing main's defaults is safe for gen_001. Verify gen_001's commit predates this work before relying on that.

- [ ] **Step 2: Start gen_002**

```bash
.venv/bin/python scripts/run_generations.py start "gen_002 risk-discipline: re-entry cooldown (7d) + conviction shorts (0.45) + VIX regime trigger (20)"
```

- [ ] **Step 3: Verify both gens are active and on the right commits**

```bash
.venv/bin/python scripts/run_generations.py list
```
Expected: gen_001 on `417b5d8` (baseline), gen_002 on the merged commit.

- [ ] **Step 4: Confirm gen_002 knobs are active**

Run a single dated daily run and grep the run log for cooldown/regime evidence; confirm gen_001's log shows none. (First real evidence will appear once gen_002 has a stop-loss to cool from.)

---

## Self-Review

**Spec coverage:**
- Config block → Task 1. ✓
- Re-entry cooldown (helper, config, gate, wiring) → Tasks 2–5. ✓
- Conviction shorts (config-driven threshold, preserves raw-score exclusion) → Task 6. ✓
- Regime VIX trigger (reuses existing alignment machinery) → Task 7. ✓
- Defaults = gen_001 → asserted in Tasks 1, 3, 6, 7 and Task 8 Step 2. ✓
- Rollout (branch → tests → PR → `run_generations.py start`, gen_001 frozen) → Tasks 8–9. ✓
- Out-of-scope items → not implemented (correct). ✓

**Placeholder scan:** Task 9 Step 1 intentionally leaves a decision (how gen_002 flips the knobs) to confirm against `run_cohorts.py` at execution time — flagged explicitly, not a hidden TODO. All code steps contain complete code.

**Type/name consistency:** `compute_cooling_tickers(closed_trades, trading_date, cooldown_days)` defined in Task 2, used identically in Tasks 4 (test) and 5 (engine). `reentry_cooldown_days` consistent across config (Task 1), dataclass (Task 3), wiring (Task 5). `_short_conviction_threshold` consistent across Task 6. `regime_vix_stressed` consistent across Tasks 1 and 7. `set_cooling_tickers(set[str])` consistent Task 4 ↔ Task 5.
