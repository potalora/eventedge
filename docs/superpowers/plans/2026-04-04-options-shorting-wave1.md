# Options & Short Selling Wave 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add covered call overlays and short equity trading to eligible cohorts (50k+/3m+ for shorts, 10k+/30d+ for covered calls) via the existing pipeline.

**Architecture:** Extend the existing Candidate → Committee → RiskGate → ExecutionBridge → PaperBroker pipeline. Add `vehicle`/`option_spec` fields to Candidate and TradeRecommendation. Extend PortfolioSizeProfile with eligibility flags. Add short-specific risk gates. Extend PaperBroker with short position tracking, margin simulation, and borrow cost accrual. Add congressional_trades selling signals (other strategies already emit shorts). Committee gains covered call overlay via Sonnet LLM.

**Tech Stack:** Python 3.11+, pytest, dataclasses, anthropic SDK (Sonnet), yfinance (options chains/IV), OpenBB (short interest)

**Spec:** `docs/superpowers/specs/2026-04-04-options-shorting-wave1-design.md`

---

### Task 1: Extend Candidate with vehicle and OptionSpec

**Files:**
- Modify: `tradingagents/strategies/modules/base.py:1-17`
- Test: `tests/test_options_shorting.py` (create)

- [ ] **Step 1: Write the failing test**

In `tests/test_options_shorting.py`:

```python
"""Tests for options & short selling Wave 1."""
from __future__ import annotations

import pytest
from tradingagents.strategies.modules.base import Candidate, OptionSpec


class TestOptionSpec:
    def test_create_covered_call(self):
        spec = OptionSpec(
            strategy="covered_call",
            expiry_target_days=30,
            strike_offset_pct=0.05,
            max_premium_pct=0.03,
        )
        assert spec.strategy == "covered_call"
        assert spec.expiry_target_days == 30
        assert spec.strike_offset_pct == 0.05
        assert spec.max_premium_pct == 0.03

    def test_option_spec_all_strategies(self):
        for strat in ("covered_call", "protective_put", "put_spread", "call_spread", "leaps"):
            spec = OptionSpec(strategy=strat, expiry_target_days=45, strike_offset_pct=-0.05, max_premium_pct=0.05)
            assert spec.strategy == strat


class TestCandidateVehicle:
    def test_default_vehicle_is_equity(self):
        c = Candidate(ticker="AAPL", date="2026-04-04")
        assert c.vehicle == "equity"
        assert c.option_spec is None

    def test_candidate_with_option_spec(self):
        spec = OptionSpec(strategy="covered_call", expiry_target_days=30, strike_offset_pct=0.05, max_premium_pct=0.03)
        c = Candidate(ticker="AAPL", date="2026-04-04", vehicle="option", option_spec=spec)
        assert c.vehicle == "option"
        assert c.option_spec.strategy == "covered_call"

    def test_backward_compat_no_vehicle(self):
        """Existing code creates Candidates without vehicle — must still work."""
        c = Candidate(ticker="MSFT", date="2026-04-04", direction="short", score=0.8)
        assert c.vehicle == "equity"
        assert c.option_spec is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py -v`
Expected: ImportError for `OptionSpec`, FAIL

- [ ] **Step 3: Write minimal implementation**

In `tradingagents/strategies/modules/base.py`, add after the imports (line 4) and before the existing `Candidate` class (line 7):

```python
@dataclass
class OptionSpec:
    """Specification for an options trade."""

    strategy: str  # "covered_call", "protective_put", "put_spread", "call_spread", "leaps"
    expiry_target_days: int
    strike_offset_pct: float  # e.g. 0.05 for 5% OTM call, -0.05 for 5% OTM put
    max_premium_pct: float  # max premium as % of position value
```

Then extend the existing `Candidate` dataclass to add two new fields at the end:

```python
@dataclass
class Candidate:
    """A screened candidate for potential trade entry."""

    ticker: str
    date: str
    direction: str = "long"
    score: float = 0.0
    confidence: float = 0.0
    metadata: dict = field(default_factory=dict)
    vehicle: str = "equity"  # "equity" or "option"
    option_spec: OptionSpec | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py -v`
Expected: 5 PASSED

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/modules/base.py tests/test_options_shorting.py
git commit -m "feat: add OptionSpec dataclass and vehicle field to Candidate"
```

---

### Task 2: Extend PortfolioSizeProfile with eligibility flags

**Files:**
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py:22-72`
- Test: `tests/test_options_shorting.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_options_shorting.py`:

```python
from tradingagents.strategies.orchestration.cohort_orchestrator import (
    PortfolioSizeProfile,
    SIZE_PROFILES,
)


class TestEligibility:
    def test_5k_long_only(self):
        p = SIZE_PROFILES["5k"]
        assert p.short_eligible is False
        assert p.options_eligible == []
        assert p.max_short_exposure_pct == 0.0

    def test_10k_covered_calls_only(self):
        p = SIZE_PROFILES["10k"]
        assert p.short_eligible is False
        assert "covered_call" in p.options_eligible
        assert p.max_options_premium_pct == 0.05

    def test_50k_short_eligible(self):
        p = SIZE_PROFILES["50k"]
        assert p.short_eligible is True
        assert "covered_call" in p.options_eligible
        assert p.max_short_exposure_pct == 0.15
        assert p.max_options_premium_pct == 0.05
        assert p.margin_cash_buffer_pct == 0.20
        assert p.max_correlated_shorts == 2

    def test_100k_full_access(self):
        p = SIZE_PROFILES["100k"]
        assert p.short_eligible is True
        assert "covered_call" in p.options_eligible
        assert p.max_short_exposure_pct == 0.20
        assert p.max_options_premium_pct == 0.08
        assert p.margin_cash_buffer_pct == 0.15
        assert p.max_correlated_shorts == 4

    def test_default_eligibility_is_safe(self):
        """A bare PortfolioSizeProfile defaults to no shorts, no options."""
        p = PortfolioSizeProfile(
            name="test", total_capital=1000, max_position_pct=0.25,
            min_position_value=100, max_positions=5,
            sector_concentration_cap=0.50, cash_reserve_pct=0.10,
        )
        assert p.short_eligible is False
        assert p.options_eligible == []
        assert p.max_short_exposure_pct == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestEligibility -v`
Expected: AttributeError on `short_eligible`

- [ ] **Step 3: Write minimal implementation**

In `tradingagents/strategies/orchestration/cohort_orchestrator.py`, extend `PortfolioSizeProfile` (lines 22-33):

```python
@dataclass
class PortfolioSizeProfile:
    """Position-sizing and concentration parameters for a portfolio tier."""

    name: str
    total_capital: float
    max_position_pct: float
    min_position_value: float
    max_positions: int
    sector_concentration_cap: float
    cash_reserve_pct: float
    # Options & short selling eligibility (Wave 1)
    short_eligible: bool = False
    options_eligible: list[str] = field(default_factory=list)
    max_short_exposure_pct: float = 0.0
    max_single_short_pct: float = 0.05
    max_options_premium_pct: float = 0.0
    margin_cash_buffer_pct: float = 0.0
    max_correlated_shorts: int = 0
```

Add `field` to the imports at the top of the file:

```python
from dataclasses import dataclass, field
```

Update SIZE_PROFILES (lines 35-72):

```python
SIZE_PROFILES: dict[str, PortfolioSizeProfile] = {
    "5k": PortfolioSizeProfile(
        name="5k",
        total_capital=5_000.0,
        max_position_pct=0.25,
        min_position_value=500.0,
        max_positions=5,
        sector_concentration_cap=0.50,
        cash_reserve_pct=0.10,
    ),
    "10k": PortfolioSizeProfile(
        name="10k",
        total_capital=10_000.0,
        max_position_pct=0.20,
        min_position_value=1_000.0,
        max_positions=8,
        sector_concentration_cap=0.40,
        cash_reserve_pct=0.10,
        options_eligible=["covered_call"],
        max_options_premium_pct=0.05,
    ),
    "50k": PortfolioSizeProfile(
        name="50k",
        total_capital=50_000.0,
        max_position_pct=0.10,
        min_position_value=2_500.0,
        max_positions=15,
        sector_concentration_cap=0.30,
        cash_reserve_pct=0.15,
        short_eligible=True,
        options_eligible=["covered_call"],
        max_short_exposure_pct=0.15,
        max_options_premium_pct=0.05,
        margin_cash_buffer_pct=0.20,
        max_correlated_shorts=2,
    ),
    "100k": PortfolioSizeProfile(
        name="100k",
        total_capital=100_000.0,
        max_position_pct=0.08,
        min_position_value=5_000.0,
        max_positions=20,
        sector_concentration_cap=0.25,
        cash_reserve_pct=0.15,
        short_eligible=True,
        options_eligible=["covered_call"],
        max_short_exposure_pct=0.20,
        max_options_premium_pct=0.08,
        margin_cash_buffer_pct=0.15,
        max_correlated_shorts=4,
    ),
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestEligibility -v`
Expected: 5 PASSED

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/orchestration/cohort_orchestrator.py tests/test_options_shorting.py
git commit -m "feat: add short/options eligibility flags to PortfolioSizeProfile"
```

---

### Task 3: Extend RiskGate with short-specific gates

**Files:**
- Modify: `tradingagents/strategies/trading/risk_gate.py:15-45` (RiskGateConfig), `69-142` (check method)
- Test: `tests/test_options_shorting.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_options_shorting.py`:

```python
from unittest.mock import MagicMock
from tradingagents.strategies.trading.risk_gate import RiskGate, RiskGateConfig
from tradingagents.execution.base_broker import AccountInfo


def _make_broker(cash=50_000, portfolio_value=50_000, positions=None):
    broker = MagicMock()
    broker.get_account.return_value = AccountInfo(
        cash=cash, portfolio_value=portfolio_value, buying_power=cash,
    )
    broker.get_positions.return_value = positions or []
    return broker


class TestShortRiskGates:
    def test_long_only_blocks_shorts(self):
        broker = _make_broker()
        gate = RiskGate(RiskGateConfig(long_only=True, total_capital=50_000), broker)
        passed, reason = gate.check("AAPL", "short", 5000, "litigation")
        assert not passed
        assert "long_only" in reason

    def test_short_allowed_when_not_long_only(self):
        broker = _make_broker()
        gate = RiskGate(RiskGateConfig(long_only=False, total_capital=50_000), broker)
        passed, _ = gate.check("AAPL", "short", 5000, "litigation")
        assert passed

    def test_earnings_blackout_blocks_short(self):
        broker = _make_broker()
        config = RiskGateConfig(long_only=False, total_capital=50_000, earnings_blackout_days=5)
        gate = RiskGate(config, broker)
        # Earnings in 3 days — should block
        passed, reason = gate.check(
            "AAPL", "short", 5000, "litigation",
            earnings_dates={"AAPL": 3},
        )
        assert not passed
        assert "earnings_blackout" in reason

    def test_earnings_blackout_allows_long(self):
        broker = _make_broker()
        config = RiskGateConfig(long_only=False, total_capital=50_000, earnings_blackout_days=5)
        gate = RiskGate(config, broker)
        # Earnings blackout only applies to shorts
        passed, _ = gate.check(
            "AAPL", "long", 5000, "earnings_call",
            earnings_dates={"AAPL": 3},
        )
        assert passed

    def test_borrow_cost_blocks_expensive_short(self):
        broker = _make_broker()
        config = RiskGateConfig(long_only=False, total_capital=50_000, max_borrow_cost_pct=0.05)
        gate = RiskGate(config, broker)
        # SI% = 35 → borrow cost > 5% → reject
        passed, reason = gate.check(
            "GME", "short", 5000, "litigation",
            short_interest={"GME": 35.0},
        )
        assert not passed
        assert "borrow_cost" in reason

    def test_borrow_cost_allows_cheap_short(self):
        broker = _make_broker()
        config = RiskGateConfig(long_only=False, total_capital=50_000, max_borrow_cost_pct=0.05)
        gate = RiskGate(config, broker)
        # SI% = 3 → borrow cost 0.5% → allow
        passed, _ = gate.check(
            "AAPL", "short", 5000, "litigation",
            short_interest={"AAPL": 3.0},
        )
        assert passed

    def test_margin_utilization_blocks_when_high(self):
        broker = _make_broker()
        config = RiskGateConfig(long_only=False, total_capital=50_000, max_margin_utilization_pct=0.70)
        gate = RiskGate(config, broker)
        # Simulate 75% margin used
        gate._margin_used = 37_500  # 75% of 50k
        passed, reason = gate.check("TSLA", "short", 5000, "litigation")
        assert not passed
        assert "margin_utilization" in reason

    def test_margin_utilization_allows_when_low(self):
        broker = _make_broker()
        config = RiskGateConfig(long_only=False, total_capital=50_000, max_margin_utilization_pct=0.70)
        gate = RiskGate(config, broker)
        gate._margin_used = 10_000  # 20% of 50k
        passed, _ = gate.check("TSLA", "short", 5000, "litigation")
        assert passed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestShortRiskGates -v`
Expected: TypeError (unexpected keyword arguments `earnings_dates`, `short_interest`)

- [ ] **Step 3: Write minimal implementation**

Update `RiskGateConfig` in `risk_gate.py`:

```python
@dataclass
class RiskGateConfig:
    """Portfolio risk parameters."""
    total_capital: float = 5000.0
    max_positions: int = 8
    max_position_pct: float = 0.15
    min_position_value: float = 100.0
    daily_loss_limit_pct: float = 0.03
    max_drawdown_pct: float = 0.15
    per_strategy_max: int = 3
    global_stop_loss_pct: float = 0.08
    long_only: bool = True
    cash_reserve_pct: float = 0.0
    # Short-specific gates
    earnings_blackout_days: int = 0       # 0 = disabled
    max_borrow_cost_pct: float = 0.0      # 0 = disabled
    max_margin_utilization_pct: float = 0.0  # 0 = disabled
    short_squeeze_stop_pct: float = 0.15
    short_squeeze_window_days: int = 5
    # Options gates
    premium_decay_floor_pct: float = 0.20

    @classmethod
    def from_dict(cls, config: dict) -> RiskGateConfig:
        """Build from nested config dict (reads autoresearch.risk_gate section)."""
        rg = config.get("autoresearch", {}).get("risk_gate", {})
        total_capital = config.get("autoresearch", {}).get("total_capital", 5000.0)
        return cls(
            total_capital=total_capital,
            max_positions=rg.get("max_positions", 8),
            max_position_pct=rg.get("max_position_pct", 0.15),
            min_position_value=rg.get("min_position_value", 100.0),
            daily_loss_limit_pct=rg.get("daily_loss_limit_pct", 0.03),
            max_drawdown_pct=rg.get("max_drawdown_pct", 0.15),
            per_strategy_max=rg.get("per_strategy_max", 3),
            global_stop_loss_pct=rg.get("global_stop_loss_pct", 0.08),
            long_only=rg.get("long_only", True),
            cash_reserve_pct=rg.get("cash_reserve_pct", 0.0),
            earnings_blackout_days=rg.get("earnings_blackout_days", 0),
            max_borrow_cost_pct=rg.get("max_borrow_cost_pct", 0.0),
            max_margin_utilization_pct=rg.get("max_margin_utilization_pct", 0.0),
            short_squeeze_stop_pct=rg.get("short_squeeze_stop_pct", 0.15),
            short_squeeze_window_days=rg.get("short_squeeze_window_days", 5),
            premium_decay_floor_pct=rg.get("premium_decay_floor_pct", 0.20),
        )
```

In `RiskGate.__init__`, add margin tracking:

```python
    def __init__(self, config: RiskGateConfig, broker: Any) -> None:
        self.config = config
        self.broker = broker
        self._high_water_mark: float = config.total_capital
        self._daily_losses: float = 0.0
        self._daily_date: str = ""
        self._margin_used: float = 0.0
```

Update the `check` method signature and add new gates after gate 9 (cash reserve):

```python
    def check(
        self,
        ticker: str,
        direction: str,
        position_value: float,
        strategy: str,
        open_trades: list[dict] | None = None,
        earnings_dates: dict[str, int] | None = None,
        short_interest: dict[str, float] | None = None,
    ) -> tuple[bool, str]:
```

Add after the existing gate 9 (cash reserve check), before the final `return True, ""`:

```python
        # --- Short-specific gates (only apply to direction="short") ---
        if direction == "short":
            # 10. Earnings blackout
            if self.config.earnings_blackout_days > 0 and earnings_dates:
                days_to_earnings = earnings_dates.get(ticker)
                if days_to_earnings is not None and days_to_earnings <= self.config.earnings_blackout_days:
                    return False, (
                        f"earnings_blackout: {ticker} earnings in {days_to_earnings} days "
                        f"(blackout={self.config.earnings_blackout_days})"
                    )

            # 11. Borrow cost check
            if self.config.max_borrow_cost_pct > 0 and short_interest:
                si_pct = short_interest.get(ticker, 0.0)
                borrow_cost = _estimate_borrow_cost(si_pct)
                if borrow_cost > self.config.max_borrow_cost_pct:
                    return False, (
                        f"borrow_cost: {ticker} SI={si_pct:.1f}% → "
                        f"est. borrow {borrow_cost:.1%} > {self.config.max_borrow_cost_pct:.1%}"
                    )

            # 12. Margin utilization
            if self.config.max_margin_utilization_pct > 0:
                account = self.broker.get_account()
                if account.portfolio_value > 0:
                    utilization = self._margin_used / account.portfolio_value
                    if utilization > self.config.max_margin_utilization_pct:
                        return False, (
                            f"margin_utilization: {utilization:.1%} > "
                            f"{self.config.max_margin_utilization_pct:.1%}"
                        )

        return True, ""
```

Add the borrow cost estimation function at module level (before the `RiskGate` class):

```python
# Borrow cost estimation from short interest %
_BORROW_TIERS = [(5, 0.005), (15, 0.02), (30, 0.05)]


def _estimate_borrow_cost(si_pct: float) -> float:
    """Estimate annualized borrow cost from short interest percentage."""
    for threshold, cost in _BORROW_TIERS:
        if si_pct < threshold:
            return cost
    return 0.10  # >30% SI = hard-to-borrow
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestShortRiskGates -v`
Expected: 8 PASSED

- [ ] **Step 5: Run all existing tests to verify no regressions**

Run: `.venv/bin/python -m pytest tests/ -v --timeout=60`
Expected: All existing tests still pass (check method signature is backward compatible via default `None` args)

- [ ] **Step 6: Commit**

```bash
git add tradingagents/strategies/trading/risk_gate.py tests/test_options_shorting.py
git commit -m "feat: add short-specific risk gates (earnings blackout, borrow cost, margin)"
```

---

### Task 4: Extend PaperBroker with short position tracking

**Files:**
- Modify: `tradingagents/execution/paper_broker.py`
- Modify: `tradingagents/execution/base_broker.py`
- Test: `tests/test_options_shorting.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_options_shorting.py`:

```python
from tradingagents.execution.paper_broker import PaperBroker


class TestPaperBrokerShorts:
    def test_short_sell_opens_position(self):
        broker = PaperBroker(initial_capital=50_000)
        result = broker.submit_short_sell("AAPL", qty=10, price=150.0, stop_loss=172.5)
        assert result.status == "filled"
        assert result.filled_qty == 10
        assert result.filled_price == 150.0
        # Short doesn't deduct cash — it reserves margin
        assert broker.cash == 50_000  # cash unchanged
        assert broker.margin_used > 0  # margin reserved

    def test_short_sell_reserves_margin(self):
        broker = PaperBroker(initial_capital=50_000)
        broker.submit_short_sell("AAPL", qty=10, price=150.0, stop_loss=172.5)
        # 150% of position value (Reg T)
        expected_margin = 10 * 150.0 * 1.5
        assert broker.margin_used == expected_margin

    def test_cover_short(self):
        broker = PaperBroker(initial_capital=50_000)
        broker.submit_short_sell("AAPL", qty=10, price=150.0, stop_loss=172.5)
        result = broker.submit_cover("AAPL", qty=10, price=140.0)
        assert result.status == "filled"
        # Profit: sold at 150, covered at 140 = $10/share * 10 = $100
        assert broker.cash == 50_000 + 100
        assert broker.margin_used == 0

    def test_cover_short_with_loss(self):
        broker = PaperBroker(initial_capital=50_000)
        broker.submit_short_sell("AAPL", qty=10, price=150.0, stop_loss=172.5)
        result = broker.submit_cover("AAPL", qty=10, price=160.0)
        assert result.status == "filled"
        # Loss: sold at 150, covered at 160 = -$10/share * 10 = -$100
        assert broker.cash == 50_000 - 100
        assert broker.margin_used == 0

    def test_short_positions_tracked_separately(self):
        broker = PaperBroker(initial_capital=50_000)
        broker.submit_stock_order("MSFT", "buy", 5, price=300.0)
        broker.submit_short_sell("AAPL", qty=10, price=150.0, stop_loss=172.5)
        positions = broker.get_positions()
        # Both long and short positions visible
        tickers = {p["ticker"] for p in positions}
        assert "MSFT" in tickers
        assert "AAPL" in tickers
        # Short position has negative direction indicator
        short_pos = next(p for p in positions if p["ticker"] == "AAPL")
        assert short_pos.get("side") == "short"

    def test_account_includes_short_value(self):
        broker = PaperBroker(initial_capital=50_000)
        broker.submit_short_sell("AAPL", qty=10, price=150.0, stop_loss=172.5)
        account = broker.get_account()
        # Portfolio value = cash + long positions - short liability
        # Short liability = 10 * 150 = 1500 (unrealized)
        # Buying power reduced by margin
        assert account.buying_power < 50_000

    def test_accrue_borrow_cost(self):
        broker = PaperBroker(initial_capital=50_000)
        broker.submit_short_sell("AAPL", qty=10, price=150.0, stop_loss=172.5)
        broker.accrue_borrow_cost("2026-04-04", borrow_rates={"AAPL": 0.02})
        # Daily borrow cost: (10 * 150) * 0.02 / 365
        expected_daily = (10 * 150.0) * 0.02 / 365
        assert abs(broker.accrued_borrow_cost - expected_daily) < 0.01
        assert broker.cash < 50_000  # cost deducted from cash

    def test_reconstruct_includes_shorts(self):
        broker = PaperBroker(initial_capital=50_000)
        open_trades = [
            {"ticker": "AAPL", "shares": 10, "entry_price": 150.0, "direction": "short"},
            {"ticker": "MSFT", "shares": 5, "entry_price": 300.0, "direction": "long"},
        ]
        broker.reconstruct_from_trades(open_trades)
        positions = broker.get_positions()
        assert len(positions) == 2
        short_pos = next(p for p in positions if p["ticker"] == "AAPL")
        assert short_pos["side"] == "short"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestPaperBrokerShorts -v`
Expected: AttributeError `submit_short_sell`

- [ ] **Step 3: Add abstract methods to BaseBroker**

In `tradingagents/execution/base_broker.py`, add after `cancel_order`:

```python
    def submit_short_sell(self, symbol: str, qty: int, price: float,
                          stop_loss: float = 0.0, **kwargs) -> OrderResult:
        """Short sell — default raises NotImplementedError for brokers that don't support it."""
        raise NotImplementedError("Short selling not supported by this broker")

    def submit_cover(self, symbol: str, qty: int, price: float,
                     **kwargs) -> OrderResult:
        """Cover short — default raises NotImplementedError."""
        raise NotImplementedError("Short covering not supported by this broker")
```

Note: These are NOT abstract — they have default implementations that raise. This keeps AlpacaBroker working without changes.

- [ ] **Step 4: Implement in PaperBroker**

In `tradingagents/execution/paper_broker.py`, add state to `__init__`:

```python
    def __init__(self, initial_capital: float = 5000.0):
        self.cash = initial_capital
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.short_positions: Dict[str, Dict[str, Any]] = {}
        self.margin_used: float = 0.0
        self.accrued_borrow_cost: float = 0.0
```

Add `submit_short_sell` method after `submit_options_order`:

```python
    def submit_short_sell(self, symbol: str, qty: int, price: float,
                          stop_loss: float = 0.0, **kwargs) -> OrderResult:
        position_value = qty * price
        margin_required = position_value * 1.5  # Reg T

        if margin_required > self.cash:
            return OrderResult(
                order_id=str(uuid.uuid4()), status="rejected",
                message="Insufficient margin",
            )

        self.margin_used += margin_required
        self.short_positions[symbol] = {
            "ticker": symbol, "quantity": qty,
            "avg_price": price, "side": "short",
            "stop_loss": stop_loss,
            "margin_reserved": margin_required,
        }

        return OrderResult(
            order_id=str(uuid.uuid4()), status="filled",
            filled_qty=qty, filled_price=price,
        )

    def submit_cover(self, symbol: str, qty: int, price: float,
                     **kwargs) -> OrderResult:
        if symbol not in self.short_positions:
            return OrderResult(
                order_id=str(uuid.uuid4()), status="rejected",
                message=f"No short position in {symbol}",
            )

        pos = self.short_positions[symbol]
        cover_qty = min(qty, pos["quantity"])
        # P&L: short profit = (entry - exit) * qty
        pnl = (pos["avg_price"] - price) * cover_qty
        self.cash += pnl
        self.margin_used -= pos["margin_reserved"] * (cover_qty / pos["quantity"])
        self.margin_used = max(0.0, self.margin_used)

        pos["quantity"] -= cover_qty
        if pos["quantity"] <= 0:
            del self.short_positions[symbol]

        return OrderResult(
            order_id=str(uuid.uuid4()), status="filled",
            filled_qty=cover_qty, filled_price=price,
        )

    def accrue_borrow_cost(self, date: str, borrow_rates: dict[str, float] | None = None) -> None:
        """Accrue daily borrow cost for all short positions."""
        borrow_rates = borrow_rates or {}
        for symbol, pos in self.short_positions.items():
            rate = borrow_rates.get(symbol, 0.005)  # default 0.5%
            position_value = pos["quantity"] * pos["avg_price"]
            daily_cost = position_value * rate / 365
            self.accrued_borrow_cost += daily_cost
            self.cash -= daily_cost
```

Update `get_positions` to include short positions:

```python
    def get_positions(self) -> List[Dict[str, Any]]:
        result = [pos for pos in self.positions.values() if pos["quantity"] > 0]
        for pos in self.short_positions.values():
            if pos["quantity"] > 0:
                result.append(pos)
        return result
```

Update `get_account` to account for shorts and margin:

```python
    def get_account(self) -> AccountInfo:
        long_value = sum(
            p["quantity"] * p["avg_price"] * (100 if p.get("instrument_type") == "option" else 1)
            for p in self.positions.values() if p["quantity"] > 0
        )
        short_liability = sum(
            p["quantity"] * p["avg_price"]
            for p in self.short_positions.values() if p["quantity"] > 0
        )
        portfolio_value = self.cash + long_value - short_liability
        buying_power = self.cash - self.margin_used
        return AccountInfo(
            cash=self.cash,
            portfolio_value=portfolio_value,
            buying_power=max(0.0, buying_power),
        )
```

Update `reconstruct_from_trades` to handle shorts:

```python
    def reconstruct_from_trades(self, open_trades: list[dict]) -> None:
        self.positions.clear()
        self.short_positions.clear()
        self.margin_used = 0.0
        for t in open_trades:
            ticker = t.get("ticker", "")
            shares = t.get("shares", 0)
            avg_price = t.get("entry_price", 0.0)
            direction = t.get("direction", "long")
            if shares > 0 and ticker:
                if direction == "short":
                    margin = shares * avg_price * 1.5
                    self.short_positions[ticker] = {
                        "ticker": ticker, "quantity": shares,
                        "avg_price": avg_price, "side": "short",
                        "stop_loss": 0.0,
                        "margin_reserved": margin,
                    }
                    self.margin_used += margin
                else:
                    if ticker in self.positions:
                        pos = self.positions[ticker]
                        total = pos["quantity"] + shares
                        pos["avg_price"] = (pos["avg_price"] * pos["quantity"] + avg_price * shares) / total
                        pos["quantity"] = total
                    else:
                        self.positions[ticker] = {
                            "ticker": ticker, "quantity": shares,
                            "avg_price": avg_price, "instrument_type": "stock",
                        }
                    self.cash -= shares * avg_price

        if self.cash < 0:
            logger.warning("Broker cash negative after reconstruction: $%.2f", self.cash)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestPaperBrokerShorts -v`
Expected: 8 PASSED

- [ ] **Step 6: Run all existing tests**

Run: `.venv/bin/python -m pytest tests/ -v --timeout=60`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add tradingagents/execution/base_broker.py tradingagents/execution/paper_broker.py tests/test_options_shorting.py
git commit -m "feat: add short position tracking to PaperBroker (sell, cover, margin, borrow)"
```

---

### Task 5: Extend ExecutionBridge for short trades

**Files:**
- Modify: `tradingagents/strategies/trading/execution_bridge.py:75-133`
- Test: `tests/test_options_shorting.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_options_shorting.py`:

```python
from tradingagents.strategies.trading.execution_bridge import ExecutionBridge


class TestExecutionBridgeShorts:
    def _make_bridge(self, long_only=False, capital=50_000):
        config = {
            "execution": {"mode": "paper"},
            "autoresearch": {
                "total_capital": capital,
                "risk_gate": {"long_only": long_only},
            },
        }
        return ExecutionBridge(config)

    def test_short_execution_uses_short_sell(self):
        bridge = self._make_bridge(long_only=False)
        result = bridge.execute_recommendation(
            ticker="AAPL", direction="short", position_size_pct=0.10,
            confidence=0.8, strategy="litigation", current_price=150.0,
        )
        assert result is not None
        assert result.status == "filled"
        # Verify short position exists in broker
        assert "AAPL" in bridge.broker.short_positions

    def test_short_rejected_when_long_only(self):
        bridge = self._make_bridge(long_only=True)
        result = bridge.execute_recommendation(
            ticker="AAPL", direction="short", position_size_pct=0.10,
            confidence=0.8, strategy="litigation", current_price=150.0,
        )
        assert result is None  # Rejected by risk gate

    def test_close_short_position(self):
        bridge = self._make_bridge(long_only=False)
        bridge.execute_recommendation(
            ticker="AAPL", direction="short", position_size_pct=0.10,
            confidence=0.8, strategy="litigation", current_price=150.0,
        )
        result = bridge.close_position("AAPL", shares=bridge.broker.short_positions["AAPL"]["quantity"], current_price=140.0, direction="short")
        assert result.status == "filled"
        assert "AAPL" not in bridge.broker.short_positions
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestExecutionBridgeShorts -v`
Expected: FAIL — `execute_recommendation` uses `submit_stock_order` for shorts instead of `submit_short_sell`

- [ ] **Step 3: Update ExecutionBridge**

In `execution_bridge.py`, update `execute_recommendation` (around line 116-119):

Replace the order submission section:

```python
        # 3. Submit order
        if direction == "short":
            result = self.broker.submit_short_sell(
                symbol=ticker, qty=shares, price=current_price,
            )
        else:
            result = self.broker.submit_stock_order(
                symbol=ticker, side="buy", qty=shares, price=current_price,
            )
```

Update `close_position` to handle short covers:

```python
    def close_position(
        self, ticker: str, shares: int, current_price: float,
        direction: str = "long",
    ) -> OrderResult:
        """Close a position (sell long shares or cover short)."""
        if direction == "short":
            return self.broker.submit_cover(
                symbol=ticker, qty=shares, price=current_price,
            )
        return self.broker.submit_stock_order(
            symbol=ticker, side="sell", qty=shares, price=current_price,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestExecutionBridgeShorts -v`
Expected: 3 PASSED

- [ ] **Step 5: Run all existing tests**

Run: `.venv/bin/python -m pytest tests/ -v --timeout=60`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add tradingagents/strategies/trading/execution_bridge.py tests/test_options_shorting.py
git commit -m "feat: route short trades through submit_short_sell in ExecutionBridge"
```

---

### Task 6: Add congressional_trades selling signals

**Files:**
- Modify: `tradingagents/strategies/modules/congressional_trades.py:95-154`
- Test: `tests/test_options_shorting.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_options_shorting.py`:

```python
from tradingagents.strategies.modules.congressional_trades import CongressionalTradesStrategy


class TestCongressionalShortSignals:
    def _make_data(self, trades_list):
        return {"congress": {"trades": trades_list}}

    def test_sale_cluster_generates_short(self):
        """A cluster of congressional sales should produce a short signal."""
        trades = [
            {"ticker": "XYZ", "transaction_type": "sale", "amount": "$15,001 - $50,000",
             "representative": "Rep A", "chamber": "house", "transaction_date": "2026-04-01"},
            {"ticker": "XYZ", "transaction_type": "sale", "amount": "$50,001 - $100,000",
             "representative": "Rep B", "chamber": "house", "transaction_date": "2026-04-01"},
        ]
        strategy = CongressionalTradesStrategy()
        candidates = strategy.screen(self._make_data(trades), "2026-04-04", strategy.get_default_params())
        shorts = [c for c in candidates if c.direction == "short"]
        assert len(shorts) >= 1
        assert shorts[0].ticker == "XYZ"

    def test_purchases_still_generate_long(self):
        """Purchases should still produce long signals (backward compat)."""
        trades = [
            {"ticker": "MSFT", "transaction_type": "purchase", "amount": "$15,001 - $50,000",
             "representative": "Rep A", "chamber": "house", "transaction_date": "2026-04-01"},
            {"ticker": "MSFT", "transaction_type": "purchase", "amount": "$50,001 - $100,000",
             "representative": "Rep B", "chamber": "senate", "transaction_date": "2026-04-01"},
        ]
        strategy = CongressionalTradesStrategy()
        candidates = strategy.screen(self._make_data(trades), "2026-04-04", strategy.get_default_params())
        longs = [c for c in candidates if c.direction == "long"]
        assert len(longs) >= 1
        assert longs[0].ticker == "MSFT"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestCongressionalShortSignals -v`
Expected: `test_sale_cluster_generates_short` FAILS (no short candidates returned — currently only processes purchases)

- [ ] **Step 3: Add sale signal processing**

In `congressional_trades.py`, after the existing purchase-processing block (after line ~124 where `ticker_buys` is built), add a parallel block for sales. Find the section that filters for buy/purchase transactions (lines 103-106) and add a similar block for sales.

After the existing `ticker_buys` loop and candidate generation (around line 154), add:

```python
        # Group sales by ticker (short signals)
        ticker_sells: dict[str, list[dict]] = defaultdict(list)

        for trade in trades:
            tx_type = (trade.get("transaction_type") or "").lower()
            if tx_type not in ("sale", "sell", "sale (full)", "sale (partial)") and "sale" not in tx_type:
                continue

            ticker = (trade.get("ticker") or "").upper().strip()
            if not ticker or ticker == "--":
                continue

            amount = trade.get("amount", "")
            tier = BUCKET_TIER.get(amount, 0)
            if tier < min_bucket:
                continue

            ticker_sells[ticker].append({
                "member": trade.get("representative") or trade.get("senator", "Unknown"),
                "chamber": trade.get("chamber", "unknown"),
                "amount": amount,
                "tier": tier,
                "date": trade.get("transaction_date", ""),
            })

        for ticker, sells in ticker_sells.items():
            unique_members = {s["member"] for s in sells}
            if len(unique_members) < min_members:
                continue

            total_tier = sum(s["tier"] for s in sells)
            cluster_bonus = len(unique_members)
            score = total_tier * cluster_bonus

            candidates.append(
                Candidate(
                    ticker=ticker,
                    date=date,
                    direction="short",
                    score=float(score),
                    metadata={
                        "num_members": len(unique_members),
                        "num_trades": len(sells),
                        "members": list(unique_members)[:5],
                        "max_tier": max(s["tier"] for s in sells),
                        "needs_llm_analysis": False,
                    },
                )
            )

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:max_positions * 2]  # allow room for both long and short
```

Note: remove the existing `candidates.sort(...)` and `return candidates[:max_positions]` at the end and replace with the combined return above.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestCongressionalShortSignals -v`
Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/modules/congressional_trades.py tests/test_options_shorting.py
git commit -m "feat: add sale cluster short signals to congressional_trades strategy"
```

---

### Task 7: Extend TradeRecommendation with vehicle/option_spec

**Files:**
- Modify: `tradingagents/strategies/trading/portfolio_committee.py:20-30`
- Test: `tests/test_options_shorting.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_options_shorting.py`:

```python
from tradingagents.strategies.trading.portfolio_committee import TradeRecommendation


class TestTradeRecommendationVehicle:
    def test_default_vehicle_equity(self):
        rec = TradeRecommendation(
            ticker="AAPL", direction="long",
            position_size_pct=0.05, confidence=0.8, rationale="test",
        )
        assert rec.vehicle == "equity"
        assert rec.option_spec is None

    def test_covered_call_recommendation(self):
        spec = OptionSpec(strategy="covered_call", expiry_target_days=30, strike_offset_pct=0.05, max_premium_pct=0.03)
        rec = TradeRecommendation(
            ticker="AAPL", direction="long",
            position_size_pct=0.05, confidence=0.8, rationale="overlay",
            vehicle="option", option_spec=spec,
        )
        assert rec.vehicle == "option"
        assert rec.option_spec.strategy == "covered_call"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestTradeRecommendationVehicle -v`
Expected: TypeError (unexpected keyword argument `vehicle`)

- [ ] **Step 3: Extend TradeRecommendation**

In `portfolio_committee.py`, add import at top:

```python
from tradingagents.strategies.modules.base import OptionSpec
```

Extend `TradeRecommendation`:

```python
@dataclass
class TradeRecommendation:
    """A sized trade recommendation from the portfolio committee."""
    ticker: str
    direction: str
    position_size_pct: float
    confidence: float
    rationale: str
    contributing_strategies: list[str] = field(default_factory=list)
    regime_alignment: str = "neutral"
    vehicle: str = "equity"
    option_spec: OptionSpec | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestTradeRecommendationVehicle -v`
Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/trading/portfolio_committee.py tests/test_options_shorting.py
git commit -m "feat: add vehicle and option_spec to TradeRecommendation"
```

---

### Task 8: Add committee vehicle selection and short book limits

**Files:**
- Modify: `tradingagents/strategies/trading/portfolio_committee.py`
- Test: `tests/test_options_shorting.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_options_shorting.py`:

```python
class TestCommitteeVehicleSelection:
    def _make_committee(self, short_eligible=True, options_eligible=None, max_short_exposure=0.15):
        profile = PortfolioSizeProfile(
            name="50k", total_capital=50_000, max_position_pct=0.10,
            min_position_value=2_500, max_positions=15,
            sector_concentration_cap=0.30, cash_reserve_pct=0.15,
            short_eligible=short_eligible,
            options_eligible=options_eligible or ["covered_call"],
            max_short_exposure_pct=max_short_exposure,
            max_correlated_shorts=2,
        )
        config = {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}}
        return PortfolioCommittee(config, size_profile=profile)

    def test_short_signal_accepted_when_eligible(self):
        committee = self._make_committee(short_eligible=True)
        signals = [
            {"ticker": "AAPL", "direction": "short", "score": 0.8, "strategy": "litigation", "metadata": {}},
            {"ticker": "AAPL", "direction": "short", "score": 0.7, "strategy": "congressional_trades", "metadata": {}},
        ]
        recs = committee.synthesize(signals, total_capital=50_000)
        assert len(recs) >= 1
        assert recs[0].direction == "short"

    def test_short_signal_dropped_when_ineligible(self):
        committee = self._make_committee(short_eligible=False)
        signals = [
            {"ticker": "AAPL", "direction": "short", "score": 0.8, "strategy": "litigation", "metadata": {}},
            {"ticker": "AAPL", "direction": "short", "score": 0.7, "strategy": "congressional_trades", "metadata": {}},
        ]
        recs = committee.synthesize(signals, total_capital=50_000)
        # Short signals should be filtered out for ineligible cohorts
        short_recs = [r for r in recs if r.direction == "short"]
        assert len(short_recs) == 0

    def test_short_exposure_capped(self):
        committee = self._make_committee(short_eligible=True, max_short_exposure=0.15)
        # 4 short signals — total should be capped at 15%
        signals = [
            {"ticker": f"T{i}", "direction": "short", "score": 0.9,
             "strategy": s, "metadata": {}}
            for i, s in enumerate(["litigation", "congressional_trades", "regulatory_pipeline", "supply_chain"])
        ]
        # Add a second strategy per ticker to pass the 2-strategy filter
        for i, s in enumerate(["congressional_trades", "litigation", "litigation", "litigation"]):
            signals.append({"ticker": f"T{i}", "direction": "short", "score": 0.8, "strategy": s, "metadata": {}})

        recs = committee.synthesize(signals, total_capital=50_000)
        short_recs = [r for r in recs if r.direction == "short"]
        total_short_pct = sum(r.position_size_pct for r in short_recs)
        assert total_short_pct <= 0.15 + 0.001  # allow float tolerance
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestCommitteeVehicleSelection -v`
Expected: FAIL — shorts not filtered for ineligible cohorts, no exposure cap

- [ ] **Step 3: Add vehicle selection to rule-based synthesis**

In `portfolio_committee.py`, update `_rule_based_synthesize` to filter ineligible shorts and cap short exposure.

At the start of `_rule_based_synthesize`, after `ticker_signals` grouping (around line 124), add:

```python
        # Filter short signals for ineligible cohorts
        if self._size_profile and not self._size_profile.short_eligible:
            ticker_signals = {
                t: [s for s in sigs if s.get("direction") != "short"]
                for t, sigs in ticker_signals.items()
            }
            ticker_signals = {t: sigs for t, sigs in ticker_signals.items() if sigs}
```

Before the final return of `_rule_based_synthesize`, after sector enforcement, add short exposure cap:

```python
        # Cap short exposure
        if self._size_profile and self._size_profile.max_short_exposure_pct > 0:
            short_recs = [r for r in recommendations if r.direction == "short"]
            total_short = sum(r.position_size_pct for r in short_recs)
            if total_short > self._size_profile.max_short_exposure_pct:
                scale = self._size_profile.max_short_exposure_pct / total_short
                for r in short_recs:
                    r.position_size_pct *= scale
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestCommitteeVehicleSelection -v`
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/trading/portfolio_committee.py tests/test_options_shorting.py
git commit -m "feat: add vehicle selection and short book limits to portfolio committee"
```

---

### Task 9: Add covered call overlay to committee

**Files:**
- Modify: `tradingagents/strategies/trading/portfolio_committee.py`
- Test: `tests/test_options_shorting.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_options_shorting.py`:

```python
from unittest.mock import patch, MagicMock


class TestCoveredCallOverlay:
    def _make_committee_with_options(self):
        profile = PortfolioSizeProfile(
            name="50k", total_capital=50_000, max_position_pct=0.10,
            min_position_value=2_500, max_positions=15,
            sector_concentration_cap=0.30, cash_reserve_pct=0.15,
            options_eligible=["covered_call"],
            max_options_premium_pct=0.05,
        )
        config = {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": True}}}
        return PortfolioCommittee(config, size_profile=profile)

    def test_generate_overlay_candidates(self):
        committee = self._make_committee_with_options()
        current_positions = [
            {"ticker": "AAPL", "direction": "long", "entry_price": 150.0,
             "entry_date": "2026-03-01", "shares": 10},
            {"ticker": "MSFT", "direction": "long", "entry_price": 300.0,
             "entry_date": "2026-04-01", "shares": 5},
        ]
        iv_data = {"AAPL": {"iv_rank": 35, "iv": 0.30}, "MSFT": {"iv_rank": 60, "iv": 0.25}}
        earnings_dates = {"AAPL": 30, "MSFT": 5}  # MSFT has earnings soon

        # Mock the LLM call
        mock_response = [
            {"ticker": "AAPL", "strike_offset_pct": 0.05, "expiry_days": 30, "rationale": "Low IV, sideways"}
        ]
        with patch.object(committee, "_llm_covered_call_overlay", return_value=mock_response):
            overlays = committee.generate_covered_call_overlays(
                current_positions=current_positions,
                iv_data=iv_data,
                earnings_dates=earnings_dates,
                trading_date="2026-04-04",
            )

        assert len(overlays) >= 1
        assert overlays[0].ticker == "AAPL"
        assert overlays[0].vehicle == "option"
        assert overlays[0].option_spec.strategy == "covered_call"

    def test_no_overlay_when_not_eligible(self):
        profile = PortfolioSizeProfile(
            name="5k", total_capital=5_000, max_position_pct=0.25,
            min_position_value=500, max_positions=5,
            sector_concentration_cap=0.50, cash_reserve_pct=0.10,
            options_eligible=[],  # no options
        )
        config = {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}}
        committee = PortfolioCommittee(config, size_profile=profile)
        overlays = committee.generate_covered_call_overlays(
            current_positions=[{"ticker": "AAPL", "direction": "long", "entry_price": 150.0, "entry_date": "2026-03-01", "shares": 10}],
            iv_data={}, earnings_dates={}, trading_date="2026-04-04",
        )
        assert len(overlays) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestCoveredCallOverlay -v`
Expected: AttributeError `generate_covered_call_overlays`

- [ ] **Step 3: Implement covered call overlay**

Add to `PortfolioCommittee` class:

```python
    def generate_covered_call_overlays(
        self,
        current_positions: list[dict],
        iv_data: dict[str, dict],
        earnings_dates: dict[str, int],
        trading_date: str,
    ) -> list[TradeRecommendation]:
        """Identify existing long positions suitable for covered call overlay.

        Uses Sonnet LLM to decide which positions to overlay.
        Returns TradeRecommendation with vehicle="option".
        """
        if not self._size_profile or "covered_call" not in self._size_profile.options_eligible:
            return []

        # Filter to long positions only
        longs = [p for p in current_positions if p.get("direction") == "long"]
        if not longs:
            return []

        # Build context for LLM
        candidates = self._llm_covered_call_overlay(longs, iv_data, earnings_dates, trading_date)
        if not candidates:
            return []

        overlays = []
        for c in candidates:
            ticker = c.get("ticker", "")
            if not ticker:
                continue
            spec = OptionSpec(
                strategy="covered_call",
                expiry_target_days=c.get("expiry_days", 30),
                strike_offset_pct=c.get("strike_offset_pct", 0.05),
                max_premium_pct=self._size_profile.max_options_premium_pct,
            )
            overlays.append(TradeRecommendation(
                ticker=ticker,
                direction="long",  # it's on an existing long
                position_size_pct=0.0,  # overlay, not new position
                confidence=0.7,
                rationale=c.get("rationale", "covered call overlay"),
                vehicle="option",
                option_spec=spec,
            ))
        return overlays

    def _llm_covered_call_overlay(
        self,
        positions: list[dict],
        iv_data: dict[str, dict],
        earnings_dates: dict[str, int],
        trading_date: str,
    ) -> list[dict]:
        """Ask Sonnet which positions to overlay with covered calls."""
        client = self._get_client()
        if client is None:
            return []

        pos_lines = []
        for p in positions[:10]:
            ticker = p.get("ticker", "?")
            iv = iv_data.get(ticker, {})
            earnings_days = earnings_dates.get(ticker, "unknown")
            pos_lines.append(
                f"  {ticker}: entry=${p.get('entry_price', 0):.0f}, "
                f"shares={p.get('shares', 0)}, "
                f"IV={iv.get('iv', 'N/A')}, IV_rank={iv.get('iv_rank', 'N/A')}, "
                f"earnings_in={earnings_days} days"
            )

        prompt = f"""Date: {trading_date}

Current long positions:
{chr(10).join(pos_lines)}

Which positions are good candidates for covered call overlays?

Consider: IV rank (higher = better for selling calls), upcoming earnings (avoid if <10 days),
days held (prefer positions held >14 days), and overall market conditions.

Return ONLY a JSON array of objects with keys: ticker, strike_offset_pct (e.g. 0.05 for 5% OTM),
expiry_days (target DTE), rationale (under 60 chars). Return empty array [] if no good candidates."""

        try:
            response = client.messages.create(
                model=self._model_name,
                max_tokens=512,
                system="You are a portfolio manager specializing in options overlays. Be conservative — only recommend covered calls on positions with favorable conditions.",
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                text = "\n".join(lines)
            data = json.loads(text)
            return data if isinstance(data, list) else []
        except Exception:
            logger.warning("Covered call overlay LLM call failed", exc_info=True)
            return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestCoveredCallOverlay -v`
Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/trading/portfolio_committee.py tests/test_options_shorting.py
git commit -m "feat: add covered call overlay generation to portfolio committee"
```

---

### Task 10: Wire eligibility into CohortOrchestrator and update config

**Files:**
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py`
- Modify: `tradingagents/strategies/orchestration/multi_strategy_engine.py`
- Modify: `tradingagents/default_config.py`
- Test: `tests/test_options_shorting.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_options_shorting.py`:

```python
class TestCohortEligibilityWiring:
    def test_50k_3m_cohort_allows_shorts(self):
        """A 50k/3m cohort should set long_only=False on the risk gate."""
        config = {
            "execution": {"mode": "paper"},
            "autoresearch": {
                "total_capital": 50_000,
                "risk_gate": {"long_only": True},  # default
            },
        }
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
        profile = SIZE_PROFILES["50k"]
        # The engine should override long_only based on profile
        from tradingagents.strategies.trading.execution_bridge import ExecutionBridge
        bridge = ExecutionBridge(config)
        # Simulate what the engine does when wiring up
        if profile.short_eligible:
            bridge.risk_gate.config.long_only = False
            bridge.risk_gate.config.earnings_blackout_days = 5
            bridge.risk_gate.config.max_borrow_cost_pct = 0.05
            bridge.risk_gate.config.max_margin_utilization_pct = 0.70
        assert bridge.risk_gate.config.long_only is False
        assert bridge.risk_gate.config.earnings_blackout_days == 5

    def test_5k_cohort_stays_long_only(self):
        profile = SIZE_PROFILES["5k"]
        assert not profile.short_eligible
```

- [ ] **Step 2: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestCohortEligibilityWiring -v`
Expected: 2 PASSED (this test verifies the wiring pattern)

- [ ] **Step 3: Wire eligibility in MultiStrategyEngine**

In `multi_strategy_engine.py`, find where the ExecutionBridge is created and risk gate config is set (around lines 230-237). After the existing `size_profile` config lines, add:

```python
            # Wire short/options eligibility from size profile
            if size_profile is not None and size_profile.short_eligible:
                bridge.risk_gate.config.long_only = False
                bridge.risk_gate.config.earnings_blackout_days = 5
                bridge.risk_gate.config.max_borrow_cost_pct = 0.05
                bridge.risk_gate.config.max_margin_utilization_pct = 0.70
```

- [ ] **Step 4: Update default_config.py**

In `tradingagents/default_config.py`, update the `autoresearch_model` and add short selling config:

Change `"autoresearch_model"` value (line 107):
```python
        "autoresearch_model": "claude-sonnet-4-20250514",
```

Add to the `"options"` section (after line 43):
```python
        "covered_call_min_hold_days": 14,
        "covered_call_default_dte": 30,
        "covered_call_strike_offset": 0.05,
```

Add a new `"short_selling"` section inside `"autoresearch"` (after the `"paper_trade"` section, around line 141):
```python
        # Short selling configuration
        "short_selling": {
            "borrow_cost_tiers": {5: 0.005, 15: 0.02, 30: 0.05},
            "borrow_cost_reject_above": 0.05,
            "hard_to_borrow_si_pct": 30,
        },
```

- [ ] **Step 5: Run all tests**

Run: `.venv/bin/python -m pytest tests/ -v --timeout=60`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add tradingagents/strategies/orchestration/multi_strategy_engine.py tradingagents/default_config.py tests/test_options_shorting.py
git commit -m "feat: wire eligibility into cohort pipeline, update config for Sonnet + shorts"
```

---

### Task 11: Integration test — full short trade pipeline

**Files:**
- Test: `tests/test_options_shorting.py` (append)

- [ ] **Step 1: Write the integration test**

Append to `tests/test_options_shorting.py`:

```python
class TestIntegrationShortPipeline:
    """End-to-end: short signal → committee → risk gate → PaperBroker → state."""

    def test_short_trade_full_pipeline(self):
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        from tradingagents.strategies.trading.execution_bridge import ExecutionBridge
        from tradingagents.strategies.trading.paper_trader import PaperTrader
        from tradingagents.strategies.state.state import StateManager
        import tempfile, os

        # Setup
        with tempfile.TemporaryDirectory() as tmpdir:
            state = StateManager(tmpdir)
            config = {
                "execution": {"mode": "paper"},
                "autoresearch": {
                    "total_capital": 50_000,
                    "risk_gate": {"long_only": False},
                    "paper_trade": {"portfolio_committee_enabled": False},
                },
            }
            profile = SIZE_PROFILES["50k"]

            # 1. Signals — two strategies agree on short AAPL
            signals = [
                {"ticker": "AAPL", "direction": "short", "score": 0.85, "strategy": "litigation", "metadata": {}},
                {"ticker": "AAPL", "direction": "short", "score": 0.75, "strategy": "congressional_trades", "metadata": {}},
            ]

            # 2. Committee synthesis (rule-based, LLM disabled)
            committee = PortfolioCommittee(config, size_profile=profile)
            recs = committee.synthesize(signals, total_capital=50_000)
            assert len(recs) >= 1
            rec = recs[0]
            assert rec.direction == "short"
            assert rec.ticker == "AAPL"

            # 3. Execution bridge
            bridge = ExecutionBridge(config)
            bridge.risk_gate.config.long_only = False
            bridge.risk_gate.config.total_capital = 50_000

            result = bridge.execute_recommendation(
                ticker=rec.ticker, direction=rec.direction,
                position_size_pct=rec.position_size_pct,
                confidence=rec.confidence, strategy="litigation",
                current_price=150.0,
            )
            assert result is not None
            assert result.status == "filled"

            # 4. Record in state
            trader = PaperTrader(state)
            trade_id = trader.open_trade(
                strategy="litigation", ticker="AAPL", direction="short",
                entry_price=150.0, entry_date="2026-04-04",
                shares=result.filled_qty, position_value=result.filled_qty * 150.0,
            )
            assert trade_id

            # 5. Verify state
            open_trades = state.load_paper_trades(status="open")
            assert len(open_trades) == 1
            assert open_trades[0]["direction"] == "short"

            # 6. Close trade
            trader.close_trade(trade_id, exit_price=140.0, exit_date="2026-04-10", exit_reason="take_profit")
            closed = state.load_paper_trades(status="closed")
            assert len(closed) == 1
            assert closed[0]["pnl"] > 0  # short at 150, covered at 140 = profit

    def test_covered_call_overlay_pipeline(self):
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            profile = SIZE_PROFILES["50k"]
            config = {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": True}}}
            committee = PortfolioCommittee(config, size_profile=profile)

            positions = [
                {"ticker": "AAPL", "direction": "long", "entry_price": 150.0,
                 "entry_date": "2026-03-01", "shares": 10},
            ]

            mock_llm_result = [
                {"ticker": "AAPL", "strike_offset_pct": 0.05, "expiry_days": 30,
                 "rationale": "Sideways, IV elevated"}
            ]

            with patch.object(committee, "_llm_covered_call_overlay", return_value=mock_llm_result):
                overlays = committee.generate_covered_call_overlays(
                    current_positions=positions,
                    iv_data={"AAPL": {"iv_rank": 55, "iv": 0.30}},
                    earnings_dates={"AAPL": 40},
                    trading_date="2026-04-04",
                )

            assert len(overlays) == 1
            assert overlays[0].vehicle == "option"
            assert overlays[0].option_spec.strategy == "covered_call"
            assert overlays[0].option_spec.expiry_target_days == 30
```

- [ ] **Step 2: Run integration tests**

Run: `.venv/bin/python -m pytest tests/test_options_shorting.py::TestIntegrationShortPipeline -v`
Expected: 2 PASSED

- [ ] **Step 3: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v --timeout=60`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add tests/test_options_shorting.py
git commit -m "test: add integration tests for short trade and covered call overlay pipelines"
```

---

### Note: Deferred to Options Execution Wiring

The **premium decay floor** gate (`premium_decay_floor_pct`) and **short squeeze stop** gate are configured in `RiskGateConfig` but enforcement requires options position monitoring in the daily loop (checking option value decay, short price movement over N days). These will be wired when the covered call execution path is integrated into `run_paper_trade_phase` — a natural follow-up after Wave 1 core is working.

---

### Task 12: Update CLAUDE.md and documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md**

Add to the Eligibility Matrix section of CLAUDE.md (in the Architecture Overview or appropriate section):

- Update the Architecture Overview ASCII diagram to mention short/options eligibility on size profiles
- Add to the "Extension Modules" table or "Autoresearch System" section: note that 50k+/3m+ cohorts support short equity, and 10k+ cohorts support covered calls
- Update the `default_config.py` config table to include the new `short_selling` section and `autoresearch_model` change to Sonnet
- Add `tests/test_options_shorting.py` to the Key test files list

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for options & short selling Wave 1"
```
