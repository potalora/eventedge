# Gen 004 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Loosen strategy gates, expand ticker universes, enforce OpenBB availability monitoring, align all strategies to a 30-day investment horizon, and add cycle-based portfolio evaluation.

**Architecture:** Four independent workstreams modifying 10 strategy files, adding one new module (`cycle_tracker.py`), expanding OpenBBSource, and integrating infrastructure health + cycle context into daily reports. Each workstream is independently testable.

**Tech Stack:** Python 3.11, pytest, OpenBB SDK, pandas

**Spec:** `docs/superpowers/specs/2026-04-04-gen004-design.md`

---

## Task 1: WeatherAg — Loosen Gates, Expand Universe, Align Horizon

**Files:**
- Modify: `tradingagents/autoresearch/strategies/weather_ag.py`
- Test: `tests/test_weather_ag.py`

- [ ] **Step 1: Update gate thresholds and defaults in weather_ag.py**

In `weather_ag.py`, update `AG_TICKERS_FULL` to add the curated expansion tickers, update `AG_TICKERS_WINTER` accordingly, lower gate thresholds in `get_default_params()` and `get_param_space()`, and change `hold_days` to 25:

```python
# Replace lines 26-42 with:
# Full agricultural ticker universe (ETFs + stocks)
AG_TICKERS_FULL = {
    # ETFs — direct commodity exposure
    "dba": "DBA",    # Invesco DB Agriculture Fund
    "weat": "WEAT",  # Teucrium Wheat Fund
    "corn": "CORN",  # Teucrium Corn Fund
    "moo": "MOO",    # VanEck Agribusiness ETF
    "soyb": "SOYB",  # Teucrium Soybean Fund
    # Stocks — agribusiness companies
    "adm": "ADM",    # Archer-Daniels-Midland
    "bg": "BG",      # Bunge Global
    "ctva": "CTVA",  # Corteva Agriscience
    "de": "DE",      # Deere & Company
    "fmc": "FMC",    # FMC Corporation
    # Food/beverage — weather-sensitive demand
    "pep": "PEP",    # PepsiCo
    "ko": "KO",      # Coca-Cola
    "gis": "GIS",    # General Mills
    "mdlz": "MDLZ",  # Mondelez International
    # Fertilizer/crop chemicals — input cost sensitivity
    "mos": "MOS",    # Mosaic Company
    "ntr": "NTR",    # Nutrien
}

# Winter subset (Oct-Mar): skip corn/soy-specific + seasonal-only instruments
AG_TICKERS_WINTER = {"weat", "dba", "moo", "adm", "bg", "pep", "ko", "gis", "mdlz"}

# Industries for dynamic OpenBB expansion
AG_EXPANSION_INDUSTRIES = [
    "Agricultural Products",
    "Packaged Foods",
    "Farm & Heavy Construction Machinery",
    "Agricultural Inputs",
]
```

Update `get_param_space()` (lines 52-61):

```python
def get_param_space(self) -> dict[str, tuple]:
    return {
        "lookback_days": (10, 60),
        "hold_days": (20, 45),
        "min_return": (-0.05, 0.05),
        "heat_stress_threshold": (2, 15),
        "precip_deficit_threshold": (-50, -10),
        "drought_min_score": (0.3, 2.0),
        "crop_decline_threshold": (1, 5),
    }
```

Update `get_default_params()` (lines 63-72):

```python
def get_default_params(self) -> dict[str, Any]:
    return {
        "lookback_days": 21,
        "hold_days": 25,
        "min_return": 0.0,
        "heat_stress_threshold": 2,
        "precip_deficit_threshold": -10,
        "drought_min_score": 0.3,
        "crop_decline_threshold": 1,
    }
```

- [ ] **Step 2: Lower the gate check thresholds in screen()**

Update `screen()` gate logic. The momentum gate threshold changes from 5% to 2%. Replace lines 133-147:

```python
        # Momentum gate: any ag ticker trailing return > 2%
        ag_returns: list[tuple[str, str, float]] = []
        for name, ticker in eligible_tickers.items():
            df = prices.get(ticker)
            if df is None or df.empty:
                continue
            df = df.loc[:date]
            if len(df) < lookback:
                continue
            close = df["Close"]
            trailing_return = (close.iloc[-1] / close.iloc[-lookback]) - 1.0
            ag_returns.append((name, ticker, trailing_return))
            if trailing_return > 0.02:
                gate_triggered = True
                gate_reasons.append(f"momentum_{ticker}={trailing_return:.1%}")
```

- [ ] **Step 3: Add dynamic universe expansion method**

Add `get_universe()` method to `WeatherAgStrategy` class (after `_check_crop_decline`):

```python
def get_universe(self, openbb_source=None) -> dict[str, str]:
    """Return eligible ticker universe, optionally expanded via OpenBB."""
    universe = dict(AG_TICKERS_FULL)
    if openbb_source and openbb_source.is_available():
        for industry in AG_EXPANSION_INDUSTRIES:
            result = openbb_source.fetch({
                "method": "sector_tickers",
                "industry": industry,
            })
            tickers = result.get("tickers", [])
            for t in tickers:
                universe[t.lower()] = t
    blocked = set()  # Populated from config at engine level
    return {k: v for k, v in universe.items() if v not in blocked}
```

- [ ] **Step 4: Update screen() to use get_universe() when openbb available**

Replace the ticker selection in `screen()` (lines 88-93) to use the expanded universe when data contains OpenBB:

```python
        # Determine season and eligible tickers
        is_growing_season = 4 <= current_month <= 9
        base_tickers = AG_TICKERS_FULL
        if is_growing_season:
            eligible_tickers = base_tickers
        else:
            eligible_tickers = {k: v for k, v in base_tickers.items() if k in AG_TICKERS_WINTER}
```

Note: The dynamic expansion via `get_universe()` is called at the engine level, not inside `screen()`. The `screen()` method uses the curated tickers for momentum scanning, while the engine can call `get_universe()` to determine the broader screening scope.

- [ ] **Step 5: Update hold_days default in check_exit()**

Replace lines 197 in `check_exit()`:

```python
        hold_days = params.get("hold_days", 25)
```

- [ ] **Step 6: Update build_propose_prompt() for 30-day horizon**

Replace `build_propose_prompt()` (lines 202-240):

```python
def build_propose_prompt(self, context: dict) -> str:
    current = context.get("current_params", self.get_default_params())
    results = context.get("recent_results", [])

    results_text = ""
    if results:
        for r in results[-5:]:
            results_text += (
                f"  params={r.get('params', {})}, "
                f"sharpe={r.get('sharpe', 0):.2f}, "
                f"return={r.get('total_return', 0):.2%}, "
                f"trades={r.get('num_trades', 0)}\n"
            )

    return f"""You are optimizing a Weather/Agriculture strategy that trades ag
ETFs and agribusiness stocks based on NOAA weather anomalies, USDA crop
condition declines, and US Drought Monitor data.

Investment horizon: 30 days. Every signal must answer "why will this move
price within 30 days?" Crop/weather impacts take 3-4 weeks to flow into
commodity prices and earnings expectations.

Current parameters: {current}

Parameter ranges:
- lookback_days: 10-60 (momentum window)
- hold_days: 20-45 (holding period, target ~25-30 days)
- min_return: -0.05 to 0.05 (minimum momentum for fallback)
- heat_stress_threshold: 2-15 (min heat days to trigger)
- precip_deficit_threshold: -50 to -10 (% below normal precipitation)
- drought_min_score: 0.3-2.0 (min composite drought score to trigger)
- crop_decline_threshold: 1-5 (min weekly Good+Excellent decline in pp)

Recent results:
{results_text or '  No results yet.'}

Suggest 3 new parameter combinations. Return JSON array of 3 param dicts."""
```

- [ ] **Step 7: Update tests for new thresholds and expanded universe**

In `tests/test_weather_ag.py`, update gate threshold tests and ticker count assertions:

```python
# Update the test that checks default params
def test_default_params_aligned_to_30d_horizon(self):
    strategy = WeatherAgStrategy()
    params = strategy.get_default_params()
    assert 20 <= params["hold_days"] <= 30, "hold_days should target ~25 days"
    assert params["drought_min_score"] == 0.3, "drought gate should be loose"
    assert params["heat_stress_threshold"] == 2, "heat gate should be loose"
    assert params["precip_deficit_threshold"] == -10, "precip gate should be loose"

def test_expanded_ticker_universe(self):
    """Verify curated expansion adds food/bev and fertilizer names."""
    from tradingagents.autoresearch.strategies.weather_ag import AG_TICKERS_FULL
    tickers = set(AG_TICKERS_FULL.values())
    # Food/beverage
    assert "PEP" in tickers
    assert "KO" in tickers
    assert "GIS" in tickers
    assert "MDLZ" in tickers
    # Fertilizer
    assert "MOS" in tickers
    assert "NTR" in tickers
    assert len(tickers) >= 18, f"Expected >=18 curated tickers, got {len(tickers)}"

def test_winter_subset_expanded(self):
    """Winter subset should include food/bev names."""
    from tradingagents.autoresearch.strategies.weather_ag import AG_TICKERS_WINTER
    assert "pep" in AG_TICKERS_WINTER
    assert "ko" in AG_TICKERS_WINTER

def test_param_space_hold_days_floor(self):
    """Hold days range should have 20-day floor for 30-day horizon."""
    strategy = WeatherAgStrategy()
    space = strategy.get_param_space()
    assert space["hold_days"][0] >= 20, "hold_days floor should be >= 20"
```

- [ ] **Step 8: Run tests to verify**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_weather_ag.py -v`
Expected: All tests pass

- [ ] **Step 9: Commit**

```bash
git add tradingagents/autoresearch/strategies/weather_ag.py tests/test_weather_ag.py
git commit -m "feat(weather_ag): loosen gates, expand universe, align 30-day horizon"
```

---

## Task 2: EarningsCall — Loosen Gates, Align Horizon

**Files:**
- Modify: `tradingagents/autoresearch/strategies/earnings_call.py`
- Test: `tests/test_multi_strategy.py`

- [ ] **Step 1: Update param space and defaults**

In `earnings_call.py`, update `get_param_space()` (lines 30-36):

```python
def get_param_space(self) -> dict[str, tuple]:
    return {
        "hold_days": (20, 45),
        "min_conviction": (0.3, 0.8),
        "max_positions": (2, 6),
        "analyze_qa_only": (True, False),
    }
```

Update `get_default_params()` (lines 38-44):

```python
def get_default_params(self) -> dict[str, Any]:
    return {
        "hold_days": 20,
        "min_conviction": 0.5,
        "max_positions": 4,
        "analyze_qa_only": False,
    }
```

- [ ] **Step 2: Remove the hard EPS threshold — accept any non-zero surprise**

The current `screen()` already accepts any non-zero surprise (line 72: `if eps_actual is not None and eps_estimate is not None and eps_estimate != 0`). No gate change needed here — the current code is already soft. The only gate is "does the data exist?" which is correct.

Verify: no threshold to change in `screen()`.

- [ ] **Step 3: Update check_exit default hold_days**

Replace line 147:

```python
        hold_days = params.get("hold_days", 20)
```

- [ ] **Step 4: Update build_propose_prompt for 30-day horizon**

Replace `build_propose_prompt()` (lines 156-169):

```python
def build_propose_prompt(self, context: dict) -> str:
    current = context.get("current_params", self.get_default_params())
    return f"""You are optimizing an Earnings Call analysis strategy that uses LLM
to detect tone shifts, deception, and guidance revisions in transcripts.

Investment horizon: 30 days. Post-earnings drift is documented over 2-3 weeks.
Exiting after 7 days leaves money on the table. Target 15-20 day holds to
capture the full drift while avoiding noise.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (post-earnings drift window, target ~20 days)
- min_conviction: 0.3-0.8
- max_positions: 2-6
- analyze_qa_only: true/false (Q&A section is more informative per research)

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
```

- [ ] **Step 5: Run tests**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "earnings"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tradingagents/autoresearch/strategies/earnings_call.py
git commit -m "feat(earnings_call): align to 30-day horizon (hold_days 7→20)"
```

---

## Task 3: GovtContracts — Loosen Gates, Expand Universe

**Files:**
- Modify: `tradingagents/autoresearch/strategies/govt_contracts.py`

- [ ] **Step 1: Expand CONTRACTOR_TICKERS and add expansion industries**

Replace `CONTRACTOR_TICKERS` (lines 12-27) and add industries:

```python
CONTRACTOR_TICKERS = {
    "lockheed": "LMT",
    "raytheon": "RTX",
    "northrop": "NOC",
    "general dynamics": "GD",
    "boeing": "BA",
    "l3harris": "LHX",
    "bae systems": "BAESY",
    "leidos": "LDOS",
    "saic": "SAIC",
    "booz allen": "BAH",
    "parsons": "PSN",
    "kratos": "KTOS",
    "palantir": "PLTR",
    "caci": "CACI",
    # Defense tech/cloud
    "snowflake": "SNOW",
    "mongodb": "MDB",
    # Defense components
    "bwx": "BWX",
    "heico": "HEI",
    "transdigm": "TDG",
}

EXPANSION_INDUSTRIES = ["Aerospace & Defense", "Information Technology Services"]
```

- [ ] **Step 2: Lower the contract amount gate from $50M to $10M**

In `screen()`, replace line 94:

```python
                if not ticker or amount < 10_000_000:  # $10M minimum
```

- [ ] **Step 3: Remove the momentum gate from the fallback path**

In the fallback section (lines 111-137), remove the `if momentum > 0.02` gate — pass all contractors with price data:

```python
        else:
            # Fallback: all defense contractors with available price data
            prices = data.get("yfinance", {}).get("prices", {})
            if prices:
                for name, ticker in CONTRACTOR_TICKERS.items():
                    df = prices.get(ticker)
                    if df is None or df.empty:
                        continue
                    df = df.loc[:date]
                    if len(df) < 30:
                        continue
                    close = df["Close"]
                    momentum = (close.iloc[-1] / close.iloc[-30]) - 1.0
                    candidates.append(
                        Candidate(
                            ticker=ticker,
                            date=date,
                            direction="long",
                            score=max(momentum, 0.1),  # Floor score at 0.1
                            metadata={
                                "contractor": name,
                                "momentum_30d": momentum,
                                "source": "momentum_fallback",
                            },
                        )
                    )
```

- [ ] **Step 4: Add get_universe() method**

Add after `build_propose_prompt()`:

```python
def get_universe(self, openbb_source=None) -> dict[str, str]:
    """Return contractor universe, optionally expanded via OpenBB."""
    universe = dict(CONTRACTOR_TICKERS)
    if openbb_source and openbb_source.is_available():
        for industry in EXPANSION_INDUSTRIES:
            result = openbb_source.fetch({
                "method": "sector_tickers",
                "industry": industry,
            })
            tickers = result.get("tickers", [])
            for t in tickers:
                universe[t.lower()] = t
    return universe
```

- [ ] **Step 5: Update build_propose_prompt for 30-day horizon**

Replace `build_propose_prompt()` (lines 178-212):

```python
def build_propose_prompt(self, context: dict) -> str:
    current = context.get("current_params", self.get_default_params())
    results = context.get("recent_results", [])

    results_text = ""
    if results:
        for r in results[-5:]:
            results_text += (
                f"  params={r.get('params', {})}, "
                f"sharpe={r.get('sharpe', 0):.2f}, "
                f"return={r.get('total_return', 0):.2%}, "
                f"trades={r.get('num_trades', 0)}\n"
            )

    return f"""You are optimizing a Government Contract Awards strategy that buys
defense/government contractor stocks after large contract announcements.

Investment horizon: 30 days. Government contract repricing takes 30-60 days
for mid/small-caps. The market underreacts to award announcements.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (holding period, target ~30 days)
- stop_loss_pct: 0.05-0.15 (stop loss percentage)
- profit_target_pct: 0.05-0.25 (take profit percentage)
- max_positions: 2-5 (max concurrent positions)

Recent backtest results:
{results_text or '  No results yet.'}

Suggest 3 new parameter combinations. Return JSON array of 3 param dicts."""
```

- [ ] **Step 6: Update param space hold_days floor**

```python
def get_param_space(self) -> dict[str, tuple]:
    return {
        "hold_days": (20, 45),
        "stop_loss_pct": (0.05, 0.15),
        "profit_target_pct": (0.05, 0.25),
        "max_positions": (2, 5),
    }
```

- [ ] **Step 7: Run tests**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "govt"`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add tradingagents/autoresearch/strategies/govt_contracts.py
git commit -m "feat(govt_contracts): lower gate $50M→$10M, expand universe, drop momentum gate"
```

---

## Task 4: InsiderActivity — Loosen Gates, Align Horizon

**Files:**
- Modify: `tradingagents/autoresearch/strategies/insider_activity.py`

- [ ] **Step 1: Update defaults — lower cluster size to 2, hold_days to 25**

Replace `get_default_params()` (lines 38-45):

```python
def get_default_params(self) -> dict[str, Any]:
    return {
        "hold_days": 25,
        "min_cluster_size": 2,
        "min_sell_threshold": 2,
        "min_conviction": 0.5,
        "max_positions": 3,
    }
```

Replace `get_param_space()` (lines 29-36):

```python
def get_param_space(self) -> dict[str, tuple]:
    return {
        "hold_days": (20, 45),
        "min_cluster_size": (2, 5),
        "min_sell_threshold": (2, 5),
        "min_conviction": (0.3, 0.8),
        "max_positions": (2, 5),
    }
```

- [ ] **Step 2: Update check_exit default hold_days**

Replace line 170:

```python
        hold_days = params.get("hold_days", 25)
```

- [ ] **Step 3: Update build_propose_prompt for 30-day horizon**

Replace `build_propose_prompt()` (lines 181-197):

```python
def build_propose_prompt(self, context: dict) -> str:
    current = context.get("current_params", self.get_default_params())
    return f"""You are optimizing an Insider Activity strategy that monitors Form 4
filings for two signal types:
1. Buy clusters: multiple insiders buying the same stock (bullish)
2. Sell patterns / 10b5-1 red flags: suspicious insider selling (bearish)

Investment horizon: 30 days. Insider signal decay is ~30 days in academic
literature (Lakonishok & Lee 2001). Target 25-30 day holds.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (target ~25-30 days)
- min_cluster_size: 2-5 (minimum insiders buying for long signal)
- min_sell_threshold: 2-5 (minimum insider sells for short signal)
- min_conviction: 0.3-0.8
- max_positions: 2-5

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
```

- [ ] **Step 4: Run tests and commit**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "insider"`

```bash
git add tradingagents/autoresearch/strategies/insider_activity.py
git commit -m "feat(insider_activity): lower cluster gate 3→2, align 30-day horizon"
```

---

## Task 5: Litigation — Loosen Gates, Align Horizon

**Files:**
- Modify: `tradingagents/autoresearch/strategies/litigation.py`

- [ ] **Step 1: Change SIGNAL_NATURES from gate to score boost**

In `screen()`, replace the gate logic (lines 79-82):

```python
            # Score boost for high-signal case types (was a hard gate)
            is_high_signal = any(s.lower() in nature.lower() for s in SIGNAL_NATURES)
            is_class_action = self._is_class_action(case_name)

            # Gate: any case with a resolvable company ticker
            ticker = self._extract_ticker(case_name)
            if not ticker and not is_high_signal and not is_class_action:
                continue

            base_score = 0.7 if is_high_signal else 0.5
            if is_class_action:
                base_score = max(base_score, 0.6)
```

And update the Candidate creation (lines 87-103):

```python
            candidates.append(
                Candidate(
                    ticker=ticker,
                    date=date,
                    direction="short",
                    score=base_score,
                    metadata={
                        "docket_id": docket.get("docket_id", ""),
                        "case_name": case_name,
                        "court": docket.get("court", ""),
                        "date_filed": docket.get("date_filed", ""),
                        "nature_of_suit": nature,
                        "cause": docket.get("cause", ""),
                        "is_high_signal_nature": is_high_signal,
                        "is_class_action": is_class_action,
                        "needs_llm_analysis": True,
                        "analysis_type": "litigation",
                    },
                )
            )
```

- [ ] **Step 2: Update defaults and param space for 30-day horizon**

Replace `get_default_params()` (lines 54-60):

```python
def get_default_params(self) -> dict[str, Any]:
    return {
        "hold_days": 25,
        "min_conviction": 0.5,
        "max_positions": 3,
        "lookback_days": 14,
    }
```

Replace `get_param_space()` (lines 46-52):

```python
def get_param_space(self) -> dict[str, tuple]:
    return {
        "hold_days": (20, 45),
        "min_conviction": (0.3, 0.8),
        "max_positions": (2, 5),
        "lookback_days": (7, 30),
    }
```

- [ ] **Step 3: Update check_exit default**

Replace line 185:

```python
        hold_days = params.get("hold_days", 25)
```

- [ ] **Step 4: Update build_propose_prompt**

Replace `build_propose_prompt()` (lines 190-203):

```python
def build_propose_prompt(self, context: dict) -> str:
    current = context.get("current_params", self.get_default_params())
    return f"""You are optimizing a Litigation Detection strategy that monitors
federal court dockets for new lawsuits/investigations against public companies.

Investment horizon: 30 days. Court filing impact + analyst reaction takes
~1 month. Cases don't resolve in 30 days, but sentiment shift does.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (target ~25-30 days)
- min_conviction: 0.3-0.8
- max_positions: 2-5
- lookback_days: 7-30

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
```

- [ ] **Step 5: Run tests and commit**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "litigation"`

```bash
git add tradingagents/autoresearch/strategies/litigation.py
git commit -m "feat(litigation): SIGNAL_NATURES as score boost not gate, align 30-day horizon"
```

---

## Task 6: RegulatoryPipeline — Expand Agencies, Align Horizon

**Files:**
- Modify: `tradingagents/autoresearch/strategies/regulatory_pipeline.py`

- [ ] **Step 1: Remove agency whitelist gate — accept all agencies**

In `screen()`, replace lines 60-66:

```python
        candidates = []

        for rule in rules:
            agency = rule.get("agency_id", "")
            # No agency filter — all agencies can move sectors
```

Remove the `agencies` parameter from `get_param_space()` and `get_default_params()`.

Replace `get_param_space()` (lines 27-37):

```python
def get_param_space(self) -> dict[str, tuple]:
    return {
        "hold_days": (20, 45),
        "min_conviction": (0.3, 0.8),
        "max_positions": (2, 5),
        "days_lookback": (7, 30),
    }
```

Replace `get_default_params()` (lines 39-46):

```python
def get_default_params(self) -> dict[str, Any]:
    return {
        "hold_days": 30,
        "min_conviction": 0.5,
        "max_positions": 3,
        "days_lookback": 14,
    }
```

- [ ] **Step 2: Update check_exit default and build_propose_prompt**

Replace line 107:

```python
        hold_days = params.get("hold_days", 30)
```

Replace `build_propose_prompt()` (lines 112-126):

```python
def build_propose_prompt(self, context: dict) -> str:
    current = context.get("current_params", self.get_default_params())
    return f"""You are optimizing a Regulatory Pipeline strategy that maps
proposed federal regulations to affected publicly traded companies.

Investment horizon: 30 days. Regulatory impact unfolds slowly but
comment period closing creates a catalyst window.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (target ~30 days)
- min_conviction: 0.3-0.8
- max_positions: 2-5
- days_lookback: 7-30

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
```

- [ ] **Step 3: Run tests and commit**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "regulatory"`

```bash
git add tradingagents/autoresearch/strategies/regulatory_pipeline.py
git commit -m "feat(regulatory_pipeline): remove agency whitelist, accept all agencies"
```

---

## Task 7: SupplyChain — Expand Keywords, Align Horizon

**Files:**
- Modify: `tradingagents/autoresearch/strategies/supply_chain.py`

- [ ] **Step 1: Expand DISRUPTION_KEYWORDS**

Replace lines 22-26:

```python
DISRUPTION_KEYWORDS = [
    "supply chain", "shortage", "disruption", "recall", "force majeure",
    "factory shutdown", "port closure", "embargo", "tariff", "sanctions",
    "logistics", "backlog", "inventory shortage", "chip shortage",
    "port congestion", "inventory", "sanction", "trade restriction",
    "export ban", "import duty", "raw material",
]
```

- [ ] **Step 2: Update defaults for 30-day horizon**

Replace `get_param_space()` (lines 36-43):

```python
def get_param_space(self) -> dict[str, tuple]:
    return {
        "hold_days": (20, 45),
        "min_conviction": (0.3, 0.8),
        "max_positions": (2, 6),
        "news_lookback_days": (3, 14),
        "hop_depth": (1, 3),
    }
```

Replace `get_default_params()` (lines 45-52):

```python
def get_default_params(self) -> dict[str, Any]:
    return {
        "hold_days": 22,
        "min_conviction": 0.5,
        "max_positions": 4,
        "news_lookback_days": 7,
        "hop_depth": 2,
    }
```

- [ ] **Step 3: Update check_exit default**

Replace line 126:

```python
        hold_days = params.get("hold_days", 22)
```

- [ ] **Step 4: Update build_propose_prompt**

Replace `build_propose_prompt()` (lines 135-149):

```python
def build_propose_prompt(self, context: dict) -> str:
    current = context.get("current_params", self.get_default_params())
    return f"""You are optimizing a Supply Chain Disruption strategy that detects
disruption events and maps multi-hop impacts to affected companies.

Investment horizon: 30 days. Supply disruptions take weeks to price across
the chain. The initial reaction captures only part of the move.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (target ~22-25 days for disruption propagation)
- min_conviction: 0.3-0.8
- max_positions: 2-6
- news_lookback_days: 3-14
- hop_depth: 1-3 (how many supply chain hops to trace)

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
```

- [ ] **Step 5: Run tests and commit**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "supply"`

```bash
git add tradingagents/autoresearch/strategies/supply_chain.py
git commit -m "feat(supply_chain): expand keywords, align 30-day horizon (10→22 days)"
```

---

## Task 8: FilingAnalysis — Add Form Types, Align Horizon

**Files:**
- Modify: `tradingagents/autoresearch/strategies/filing_analysis.py`

- [ ] **Step 1: Update defaults — add 8-K, SC 13D, SC 13G; align hold_days**

Replace `get_param_space()` (lines 45-51):

```python
def get_param_space(self) -> dict[str, tuple]:
    return {
        "hold_days": (20, 45),
        "min_conviction": (0.3, 0.7),
        "max_positions": (3, 8),
        "forms_to_analyze": (
            ["10-K", "10-Q"],
            ["10-K", "10-Q", "DEF 14A", "8-K", "SC 13D", "SC 13G"],
        ),
    }
```

Replace `get_default_params()` (lines 53-59):

```python
def get_default_params(self) -> dict[str, Any]:
    return {
        "hold_days": 25,
        "min_conviction": 0.5,
        "max_positions": 5,
        "forms_to_analyze": ["10-K", "10-Q", "DEF 14A", "8-K", "SC 13D", "SC 13G"],
    }
```

- [ ] **Step 2: Add 8-K, SC 13D, SC 13G handling in screen()**

After the `DEF 14A` elif block (after line 140), add:

```python
            # 8-K → material event announcement
            elif form_type == "8-K" and form_type in forms_to_analyze:
                event_text = filing.get("current_text", "")
                has_text = bool(event_text.strip())
                candidates.append(
                    Candidate(
                        ticker=ticker,
                        date=date,
                        direction="long",  # LLM will determine
                        score=0.6,  # 8-Ks are time-sensitive
                        metadata={
                            "form_type": form_type,
                            "entity_name": entity_name,
                            "file_date": filing.get("file_date", ""),
                            "file_url": filing.get("file_url", ""),
                            "current_text": event_text[:5000],
                            "needs_llm_analysis": has_text,
                            "analysis_type": "material_event",
                        },
                    )
                )

            # SC 13D/13G → activist or large passive stake
            elif form_type in ("SC 13D", "SC 13G") and form_type in forms_to_analyze:
                stake_text = filing.get("current_text", "")
                has_text = bool(stake_text.strip())
                is_activist = form_type == "SC 13D"
                candidates.append(
                    Candidate(
                        ticker=ticker,
                        date=date,
                        direction="long",  # Activist stakes are typically bullish
                        score=0.7 if is_activist else 0.4,
                        metadata={
                            "form_type": form_type,
                            "entity_name": entity_name,
                            "file_date": filing.get("file_date", ""),
                            "file_url": filing.get("file_url", ""),
                            "current_text": stake_text[:5000],
                            "needs_llm_analysis": has_text,
                            "analysis_type": "activist_stake" if is_activist else "passive_stake",
                        },
                    )
                )
```

- [ ] **Step 3: Update check_exit default and build_propose_prompt**

Replace line 187:

```python
        hold_days = params.get("hold_days", 25)
```

Replace `build_propose_prompt()` (lines 192-208):

```python
def build_propose_prompt(self, context: dict) -> str:
    current = context.get("current_params", self.get_default_params())
    return f"""You are optimizing a unified Filing Analysis strategy that processes
EDGAR filings: 10-K/10-Q (material changes), DEF 14A (exec comp),
8-K (material events), SC 13D (activist stakes), SC 13G (large passive stakes).

Investment horizon: 30 days. Filing implications unfold over weeks as
analysts digest. SC 13D activist stakes are well-documented alpha sources.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (target ~25-30 days)
- min_conviction: 0.3-0.7
- max_positions: 3-8
- forms_to_analyze: subset of ["10-K", "10-Q", "DEF 14A", "8-K", "SC 13D", "SC 13G"]

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
```

- [ ] **Step 4: Run tests and commit**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "filing"`

```bash
git add tradingagents/autoresearch/strategies/filing_analysis.py
git commit -m "feat(filing_analysis): add 8-K/SC13D/SC13G, align 30-day horizon"
```

---

## Task 9: CongressionalTrades — Lower Bucket Threshold, Align Horizon

**Files:**
- Modify: `tradingagents/autoresearch/strategies/congressional_trades.py`

- [ ] **Step 1: Update defaults — min_amount_bucket 2→1, hold_days 25→28**

Replace `get_default_params()` (lines 56-62):

```python
def get_default_params(self) -> dict[str, Any]:
    return {
        "hold_days": 28,
        "min_amount_bucket": 1,
        "max_positions": 3,
        "min_members": 1,
    }
```

Replace `get_param_space()` (lines 48-54):

```python
def get_param_space(self) -> dict[str, tuple]:
    return {
        "hold_days": (20, 45),
        "min_amount_bucket": (1, 4),
        "max_positions": (2, 5),
        "min_members": (1, 3),
    }
```

- [ ] **Step 2: Update check_exit default**

Replace line 162:

```python
        hold_days = params.get("hold_days", 28)
```

- [ ] **Step 3: Update build_propose_prompt**

Replace `build_propose_prompt()` (lines 173-191):

```python
def build_propose_prompt(self, context: dict) -> str:
    current = context.get("current_params", self.get_default_params())
    return f"""You are optimizing a Congressional Stock Trades strategy that follows
purchase disclosures from US Congress members.

Investment horizon: 30 days. Congress members often trade ahead of
legislation by 30-60 days. Target ~28-30 day holds.

Current parameters: {current}

Parameter ranges:
- hold_days: 20-45 (target ~28 days)
- min_amount_bucket: 1-4 (1=$1K-$15K, 4=$100K-$250K)
- max_positions: 2-5
- min_members: 1-3 (minimum unique members buying same stock)

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
```

- [ ] **Step 4: Run tests and commit**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "congress"`

```bash
git add tradingagents/autoresearch/strategies/congressional_trades.py
git commit -m "feat(congressional_trades): lower bucket 2→1, align 30-day horizon"
```

---

## Task 10: StateEconomics — Remove Hard Floor, Expand ETFs, Align Rebalance

**Files:**
- Modify: `tradingagents/autoresearch/strategies/state_economics.py`

- [ ] **Step 1: Expand REGIONAL_ETFS**

Replace `REGIONAL_ETFS` (lines 13-19):

```python
REGIONAL_ETFS = {
    "regional_banks": "KRE",      # SPDR S&P Regional Banking ETF
    "small_cap_value": "IWN",     # iShares Russell 2000 Value ETF
    "retail": "XRT",              # SPDR S&P Retail ETF
    "real_estate": "IYR",         # iShares US Real Estate ETF
    "homebuilders": "XHB",        # SPDR S&P Homebuilders ETF
    "homebuilders_focused": "ITB",  # iShares US Home Construction ETF
    "broad_reit": "VNQ",          # Vanguard Real Estate ETF
    "semiconductors": "SOXX",     # iShares Semiconductor ETF
    "industrials": "XLI",         # Industrial Select Sector SPDR
    "real_estate_sector": "XLRE", # Real Estate Select Sector SPDR
}
```

- [ ] **Step 2: Remove min_return hard floor — pass all ETFs to LLM context**

In `screen()`, replace lines 124-126 (the `if combined < min_return: continue` block):

```python
        for name, ticker, combined, momentum, boost in combined_scores[:top_n]:
            # No hard min_return gate — let LLM judge context
            metadata = {
```

Remove `min_return` from `get_default_params()` and `get_param_space()`.

Replace `get_param_space()` (lines 44-50):

```python
def get_param_space(self) -> dict[str, tuple]:
    return {
        "lookback_days": (10, 60),
        "top_n": (1, 4),
        "rebalance_days": (20, 45),
    }
```

Replace `get_default_params()` (lines 52-58):

```python
def get_default_params(self) -> dict[str, Any]:
    return {
        "lookback_days": 21,
        "top_n": 2,
        "rebalance_days": 30,
    }
```

- [ ] **Step 3: Align rebalance window to 30 days in check_exit**

Replace line 162:

```python
        rebalance_days = params.get("rebalance_days", 30)
```

- [ ] **Step 4: Update build_propose_prompt**

Replace `build_propose_prompt()` (lines 167-202):

```python
def build_propose_prompt(self, context: dict) -> str:
    current = context.get("current_params", self.get_default_params())
    results = context.get("recent_results", [])

    results_text = ""
    if results:
        for r in results[-5:]:
            results_text += (
                f"  params={r.get('params', {})}, "
                f"sharpe={r.get('sharpe', 0):.2f}, "
                f"return={r.get('total_return', 0):.2%}, "
                f"trades={r.get('num_trades', 0)}\n"
            )

    return f"""You are optimizing a State Economics strategy that rotates among
regional ETFs (KRE, IWN, XRT, IYR, XHB, ITB, VNQ, SOXX, XLI, XLRE)
based on trailing momentum as a proxy for state-level economic conditions.

Investment horizon: 30 days. Rebalance window aligns with the portfolio
evaluation cycle. Regional economic divergence is persistent over 1-3 months.

Current parameters: {current}

Parameter ranges:
- lookback_days: 10-60 (trailing return window)
- top_n: 1-4 (number of top ETFs to hold)
- rebalance_days: 20-45 (rebalance frequency, target ~30 days)

Recent backtest results:
{results_text or '  No results yet.'}

Suggest 3 new parameter combinations. Return JSON array of 3 param dicts."""
```

- [ ] **Step 5: Run tests and commit**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "state"`

```bash
git add tradingagents/autoresearch/strategies/state_economics.py
git commit -m "feat(state_economics): expand to 10 ETFs, remove min_return gate, 30-day rebalance"
```

---

## Task 11: OpenBB `sector_tickers` Method

**Files:**
- Modify: `tradingagents/autoresearch/data_sources/openbb_source.py`
- Test: `tests/test_openbb_source.py`

- [ ] **Step 1: Write failing test for sector_tickers**

In `tests/test_openbb_source.py`, add:

```python
class TestSectorTickers:
    """Tests for the sector_tickers method."""

    def test_sector_tickers_returns_list(self, source):
        """sector_tickers should return a dict with 'tickers' key."""
        with patch.object(source, "_get_obb") as mock_obb:
            mock_result = MagicMock()
            mock_result.results = [
                MagicMock(symbol="ADM"),
                MagicMock(symbol="BG"),
                MagicMock(symbol="DE"),
            ]
            mock_obb.return_value.equity.screener.screen.return_value = mock_result

            result = source.fetch({"method": "sector_tickers", "industry": "Agricultural Products"})

            assert "tickers" in result
            assert "ADM" in result["tickers"]
            assert "BG" in result["tickers"]

    def test_sector_tickers_cached(self, source):
        """Second call should use cache."""
        with patch.object(source, "_get_obb") as mock_obb:
            mock_result = MagicMock()
            mock_result.results = [MagicMock(symbol="ADM")]
            mock_obb.return_value.equity.screener.screen.return_value = mock_result

            result1 = source.fetch({"method": "sector_tickers", "industry": "Agricultural Products"})
            result2 = source.fetch({"method": "sector_tickers", "industry": "Agricultural Products"})

            assert result1 == result2
            # Only one API call
            assert mock_obb.return_value.equity.screener.screen.call_count == 1

    def test_sector_tickers_unavailable_returns_empty(self, source):
        """If OpenBB is unavailable, return empty tickers list."""
        with patch.object(source, "_get_obb", side_effect=ImportError):
            result = source.fetch({"method": "sector_tickers", "industry": "Agricultural Products"})
            assert result.get("tickers", []) == [] or "error" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_openbb_source.py::TestSectorTickers -v`
Expected: FAIL with "Unknown method 'sector_tickers'"

- [ ] **Step 3: Add sector_tickers to dispatch and implement handler**

In `openbb_source.py`, add `"sector_tickers"` to the dispatch dict (line 52):

```python
        dispatch = {
            "equity_profile": self._equity_profile,
            "equity_estimates": self._equity_estimates,
            "equity_insider_trading": self._equity_insider_trading,
            "equity_short_interest": self._equity_short_interest,
            "equity_government_trades": self._equity_government_trades,
            "derivatives_options_unusual": self._derivatives_options_unusual,
            "regulators_sec_litigation": self._regulators_sec_litigation,
            "factors_fama_french": self._factors_fama_french,
            "sector_tickers": self._sector_tickers,
        }
```

Add the handler method after `_factors_fama_french()`:

```python
def _sector_tickers(self, params: dict[str, Any]) -> dict[str, Any]:
    """Return all tickers in a given industry classification.

    Uses OpenBB equity screener. Results cached for 24h (session-level cache).
    """
    industry = params.get("industry", "")
    if not industry:
        return {"tickers": []}

    cache_key = self._cache_key("sector_tickers", params)
    if cache_key in self._cache:
        return self._cache[cache_key]

    obb = self._get_obb()
    try:
        result = obb.equity.screener.screen(industry=industry)
        tickers = [_getfield(item, "symbol", "") for item in (result.results or [])]
        tickers = [t for t in tickers if t]  # Filter empty
    except Exception:
        logger.warning("sector_tickers(%s) failed", industry, exc_info=True)
        tickers = []

    out = {"tickers": tickers, "industry": industry}
    self._cache[cache_key] = out
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_openbb_source.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add tradingagents/autoresearch/data_sources/openbb_source.py tests/test_openbb_source.py
git commit -m "feat(openbb): add sector_tickers method for dynamic universe expansion"
```

---

## Task 12: OpenBB Availability Enforcement

**Files:**
- Modify: `tradingagents/autoresearch/cohort_orchestrator.py`
- Modify: `tradingagents/autoresearch/multi_strategy_engine.py` (signal journal metadata)
- Modify: `scripts/generate_daily_report.py`

- [ ] **Step 1: Add startup validation to CohortOrchestrator**

In `cohort_orchestrator.py`, after line 63 (`self._base_config = base_config`), add:

```python
        # Check OpenBB availability and flag degraded mode
        from tradingagents.autoresearch.data_sources.openbb_source import OpenBBSource
        openbb = OpenBBSource(
            fmp_api_key=base_config.get("autoresearch", {}).get("fmp_api_key", "")
        )
        self.openbb_degraded = not openbb.is_available()
        if self.openbb_degraded:
            logger.warning(
                "OpenBB unavailable — running in degraded mode "
                "(curated universes only, no enrichment)"
            )
```

- [ ] **Step 2: Add openbb_available to signal journal entries**

First, add `openbb_available` field to `JournalEntry` in `tradingagents/autoresearch/signal_journal.py` (after line 37):

```python
    prompt_version: str = ""
    openbb_available: bool = True
```

Then in `multi_strategy_engine.py`, in the journal entry creation (lines 316-327), add `openbb_available`:

```python
            journal_entries.append(JournalEntry(
                timestamp=trading_date,
                strategy=signal["strategy"],
                ticker=signal["ticker"],
                direction=signal["direction"],
                score=signal["score"],
                llm_conviction=llm_analysis.get("conviction", llm_analysis.get("score", 0.0)),
                regime=regime_label,
                traded=was_traded,
                entry_price=price,
                prompt_version=prompt_version,
                openbb_available=self._openbb_available,
            ))
```

Add `self._openbb_available` initialization in `__init__()`. After the OpenBB source is created, check availability:

```python
        self._openbb_available = self._openbb_source.is_available() if self._openbb_source else False
```

- [ ] **Step 3: Add infrastructure health section to daily report**

In `scripts/generate_daily_report.py`, add a function and integrate it into the report:

```python
def _check_infrastructure_health() -> list[tuple[str, str, str]]:
    """Check availability of all data sources. Returns [(name, status, notes)]."""
    sources = []

    # OpenBB
    try:
        from tradingagents.autoresearch.data_sources.openbb_source import OpenBBSource
        obb = OpenBBSource()
        sources.append(("OpenBB", "OK" if obb.is_available() else "DEGRADED", "" if obb.is_available() else "SDK not installed"))
    except Exception:
        sources.append(("OpenBB", "DEGRADED", "import failed"))

    # NOAA CDO
    noaa_token = os.environ.get("NOAA_CDO_TOKEN", "")
    sources.append(("NOAA CDO", "OK" if noaa_token else "DEGRADED", "" if noaa_token else "token missing"))

    # USDA NASS
    usda_key = os.environ.get("USDA_NASS_API_KEY", "")
    sources.append(("USDA NASS", "OK" if usda_key else "DEGRADED", "" if usda_key else "API key missing"))

    # FRED
    fred_key = os.environ.get("FRED_API_KEY", "")
    sources.append(("FRED", "OK" if fred_key else "DEGRADED", "" if fred_key else "API key missing"))

    # Drought Monitor (no key required)
    sources.append(("Drought Monitor", "OK", ""))

    return sources
```

In `_generate_report()`, after the header section (line 68), add:

```python
    # --- Infrastructure Health ---
    health = _check_infrastructure_health()
    lines.append("## Infrastructure Health")
    lines.append("")
    lines.append("| Source | Status | Notes |")
    lines.append("|--------|--------|-------|")
    for name, status, notes in health:
        lines.append(f"| {name} | {status} | {notes} |")
    lines.append("")
```

- [ ] **Step 4: Run tests and commit**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/ -v -k "orchestrator or report"`

```bash
git add tradingagents/autoresearch/cohort_orchestrator.py tradingagents/autoresearch/multi_strategy_engine.py tradingagents/autoresearch/signal_journal.py scripts/generate_daily_report.py
git commit -m "feat: OpenBB availability enforcement — startup warning, journal metadata, health report"
```

---

## Task 13: Portfolio Committee — 30-Day Horizon Prompt

**Files:**
- Modify: `tradingagents/autoresearch/portfolio_committee.py`

- [ ] **Step 1: Update the system prompt in _llm_synthesize()**

Replace the system prompt (lines 264-267):

```python
                system="""You are a portfolio manager synthesizing trading signals from multiple strategies.
Investment horizon: 30 days. Every position should have a thesis that plays out within 30 days.
Given signals, regime context, and strategy confidence scores, output a ranked list of trades.
Return ONLY a JSON array of objects with keys: ticker, direction, position_size_pct, confidence, rationale, contributing_strategies, regime_alignment.
Keep position_size_pct between 0.01 and 0.10. Keep rationale under 80 chars. Prefer signals with catalysts that resolve within 30 days.""",
```

- [ ] **Step 2: Commit**

```bash
git add tradingagents/autoresearch/portfolio_committee.py
git commit -m "feat(portfolio_committee): add 30-day horizon framing to system prompt"
```

---

## Task 14: CycleTracker — New Module

**Files:**
- Create: `tradingagents/autoresearch/cycle_tracker.py`
- Create: `tests/test_cycle_tracker.py`

- [ ] **Step 1: Write failing tests for CycleTracker**

Create `tests/test_cycle_tracker.py`:

```python
"""Tests for CycleTracker — 30-day portfolio evaluation cycles."""
from __future__ import annotations

import json
import os
import tempfile

import pytest


class TestCycleTracker:
    """Test cycle math, boundary detection, and snapshots."""

    @pytest.fixture
    def tracker(self):
        from tradingagents.autoresearch.cycle_tracker import CycleTracker
        tmpdir = tempfile.mkdtemp()
        return CycleTracker(gen_start_date="2026-04-01", state_dir=tmpdir)

    def test_current_cycle_day_one(self, tracker):
        assert tracker.current_cycle("2026-04-01") == 1

    def test_current_cycle_day_fifteen(self, tracker):
        assert tracker.current_cycle("2026-04-15") == 1

    def test_current_cycle_day_thirty(self, tracker):
        assert tracker.current_cycle("2026-04-30") == 1

    def test_current_cycle_day_thirty_one(self, tracker):
        assert tracker.current_cycle("2026-05-01") == 2

    def test_current_cycle_day_sixty(self, tracker):
        assert tracker.current_cycle("2026-05-30") == 2

    def test_current_cycle_day_sixty_one(self, tracker):
        assert tracker.current_cycle("2026-05-31") == 3

    def test_days_remaining_day_one(self, tracker):
        assert tracker.days_remaining("2026-04-01") == 30

    def test_days_remaining_day_fifteen(self, tracker):
        assert tracker.days_remaining("2026-04-15") == 16

    def test_days_remaining_day_thirty(self, tracker):
        assert tracker.days_remaining("2026-04-30") == 1

    def test_days_remaining_day_thirty_one(self, tracker):
        assert tracker.days_remaining("2026-05-01") == 30

    def test_is_boundary_day_twenty_nine(self, tracker):
        assert not tracker.is_boundary("2026-04-29")

    def test_is_boundary_day_thirty(self, tracker):
        assert tracker.is_boundary("2026-04-30")

    def test_is_boundary_day_sixty(self, tracker):
        assert tracker.is_boundary("2026-05-30")

    def test_update_daily(self, tracker):
        tracker.update_daily("2026-04-01", positions=[], portfolio_value=5000.0)
        tracker.update_daily("2026-04-02", positions=[], portfolio_value=5010.0)
        # Should not raise

    def test_snapshot_cycle(self, tracker):
        tracker.update_daily("2026-04-01", positions=[], portfolio_value=5000.0)
        snap = tracker.snapshot_cycle(
            cycle_number=1,
            positions=[],
            closed_trades=[{"pnl": 50.0, "strategy": "earnings_call"}],
            portfolio_value=5050.0,
        )
        assert snap["cycle"] == 1
        assert snap["portfolio_value_end"] == 5050.0
        assert snap["realized_pnl"] == 50.0

    def test_snapshot_persisted_to_disk(self, tracker):
        tracker.update_daily("2026-04-01", positions=[], portfolio_value=5000.0)
        tracker.snapshot_cycle(1, [], [], 5000.0)
        cycle_path = os.path.join(tracker._state_dir, "cycles", "cycle_001.json")
        assert os.path.exists(cycle_path)
        data = json.loads(open(cycle_path).read())
        assert data["cycle"] == 1

    def test_strategy_breakdown_in_snapshot(self, tracker):
        tracker.update_daily("2026-04-01", positions=[], portfolio_value=5000.0)
        closed = [
            {"pnl": 50.0, "strategy": "earnings_call", "holding_days": 18},
            {"pnl": -20.0, "strategy": "earnings_call", "holding_days": 15},
            {"pnl": 30.0, "strategy": "weather_ag", "holding_days": 25},
        ]
        snap = tracker.snapshot_cycle(1, [], closed, 5060.0)
        breakdown = snap["strategy_breakdown"]
        assert "earnings_call" in breakdown
        assert breakdown["earnings_call"]["traded"] == 2
        assert breakdown["earnings_call"]["pnl"] == 30.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cycle_tracker.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement CycleTracker**

Create `tradingagents/autoresearch/cycle_tracker.py`:

```python
"""30-day cycle evaluation tracker for portfolio performance.

Observation-only component that produces snapshots at 30-day boundaries
aligned to generation start date. Does not force exits or modify strategy
behavior.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

CYCLE_LENGTH = 30


class CycleTracker:
    """Track portfolio performance in 30-day cycles aligned to generation start."""

    def __init__(self, gen_start_date: str, state_dir: str) -> None:
        self._gen_start = pd.Timestamp(gen_start_date)
        self._state_dir = state_dir
        self._cycles_dir = os.path.join(state_dir, "cycles")
        os.makedirs(self._cycles_dir, exist_ok=True)

        # Running metrics for the current cycle
        self._daily_values: list[tuple[str, float]] = []
        self._cycle_start_value: float | None = None

    def current_cycle(self, trading_date: str) -> int:
        """Return the current cycle number (1-indexed)."""
        days = (pd.Timestamp(trading_date) - self._gen_start).days
        return days // CYCLE_LENGTH + 1

    def days_remaining(self, trading_date: str) -> int:
        """Days remaining in the current cycle."""
        days = (pd.Timestamp(trading_date) - self._gen_start).days
        days_into_cycle = days % CYCLE_LENGTH
        return CYCLE_LENGTH - days_into_cycle

    def is_boundary(self, trading_date: str) -> bool:
        """True if trading_date is the last day of a cycle."""
        return self.days_remaining(trading_date) == 1

    def update_daily(
        self, trading_date: str, positions: list, portfolio_value: float
    ) -> None:
        """Called after each trading day. Updates running metrics."""
        if self._cycle_start_value is None:
            self._cycle_start_value = portfolio_value
        self._daily_values.append((trading_date, portfolio_value))

    def snapshot_cycle(
        self,
        cycle_number: int,
        positions: list,
        closed_trades: list,
        portfolio_value: float,
    ) -> dict[str, Any]:
        """Generate full cycle evaluation and persist to disk."""
        start_value = self._cycle_start_value or portfolio_value
        cycle_start_date = self._gen_start + pd.Timedelta(days=(cycle_number - 1) * CYCLE_LENGTH)
        cycle_end_date = self._gen_start + pd.Timedelta(days=cycle_number * CYCLE_LENGTH - 1)

        realized_pnl = sum(t.get("pnl", 0.0) for t in closed_trades)
        unrealized_pnl = portfolio_value - start_value - realized_pnl

        # Capital utilization from daily values
        daily_vals = [v for _, v in self._daily_values]
        avg_util = 0.0
        peak_util = 0.0
        if daily_vals and start_value > 0:
            deployed = [start_value - v for v in daily_vals]
            # Rough proxy: deviation from starting capital
            avg_util = sum(abs(d) for d in deployed) / len(deployed) / start_value * 100
            peak_util = max(abs(d) for d in deployed) / start_value * 100

        # Strategy breakdown
        strategy_stats: dict[str, dict] = defaultdict(
            lambda: {"signals": 0, "traded": 0, "pnl": 0.0, "hold_days": []}
        )
        for t in closed_trades:
            s = t.get("strategy", "unknown")
            strategy_stats[s]["traded"] += 1
            strategy_stats[s]["pnl"] += t.get("pnl", 0.0)
            if "holding_days" in t:
                strategy_stats[s]["hold_days"].append(t["holding_days"])

        breakdown = {}
        for strat, stats in strategy_stats.items():
            hold_days_list = stats["hold_days"]
            avg_hold = sum(hold_days_list) / len(hold_days_list) if hold_days_list else 0
            hit_rate = (
                sum(1 for t in closed_trades if t.get("strategy") == strat and t.get("pnl", 0) > 0)
                / stats["traded"]
                if stats["traded"] > 0
                else 0.0
            )
            breakdown[strat] = {
                "signals": stats["signals"],
                "traded": stats["traded"],
                "pnl": round(stats["pnl"], 2),
                "hit_rate": round(hit_rate, 2),
                "avg_hold_days": round(avg_hold, 1),
            }

        snapshot = {
            "cycle": cycle_number,
            "start_date": cycle_start_date.strftime("%Y-%m-%d"),
            "end_date": cycle_end_date.strftime("%Y-%m-%d"),
            "portfolio_value_start": round(start_value, 2),
            "portfolio_value_end": round(portfolio_value, 2),
            "cycle_return_pct": round(
                (portfolio_value - start_value) / start_value * 100
                if start_value > 0
                else 0.0,
                2,
            ),
            "realized_pnl": round(realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "positions_opened": len(closed_trades) + len(positions),
            "positions_closed": len(closed_trades),
            "positions_open_at_end": len(positions),
            "capital_utilization_avg_pct": round(avg_util, 1),
            "capital_utilization_peak_pct": round(peak_util, 1),
            "strategy_breakdown": breakdown,
        }

        # Persist
        cycle_path = os.path.join(self._cycles_dir, f"cycle_{cycle_number:03d}.json")
        with open(cycle_path, "w") as f:
            json.dump(snapshot, f, indent=2)
        logger.info("Cycle %d snapshot saved to %s", cycle_number, cycle_path)

        # Reset running metrics for next cycle
        self._daily_values = []
        self._cycle_start_value = portfolio_value

        return snapshot
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/test_cycle_tracker.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add tradingagents/autoresearch/cycle_tracker.py tests/test_cycle_tracker.py
git commit -m "feat: add CycleTracker for 30-day portfolio evaluation cycles"
```

---

## Task 15: Integrate CycleTracker into Engine and Daily Report

**Files:**
- Modify: `tradingagents/autoresearch/multi_strategy_engine.py`
- Modify: `scripts/generate_daily_report.py`

- [ ] **Step 1: Add CycleTracker to MultiStrategyEngine**

In `multi_strategy_engine.py`, add to `__init__()` (after existing state setup):

```python
        # Cycle tracking (observation-only)
        self._cycle_tracker = None  # Initialized when gen_start_date is known
```

Add a method to set up cycle tracking:

```python
def set_cycle_tracker(self, gen_start_date: str) -> None:
    """Initialize cycle tracking for this engine's state directory."""
    from tradingagents.autoresearch.cycle_tracker import CycleTracker
    state_dir = self.ar_config.get("state_dir", "data/state")
    self._cycle_tracker = CycleTracker(gen_start_date, state_dir)
```

- [ ] **Step 2: Call CycleTracker at end of run_paper_trade_phase()**

At the end of `run_paper_trade_phase()`, before the return statement, add:

```python
        # Update cycle tracker
        if self._cycle_tracker:
            portfolio_value = trader.get_portfolio_value() if hasattr(trader, 'get_portfolio_value') else total_capital
            self._cycle_tracker.update_daily(trading_date, list(trader.open_positions()), portfolio_value)
            if self._cycle_tracker.is_boundary(trading_date):
                cycle_num = self._cycle_tracker.current_cycle(trading_date)
                closed = [t for t in trader.get_all_trades() if t.get("status") == "closed"]
                self._cycle_tracker.snapshot_cycle(cycle_num, list(trader.open_positions()), closed, portfolio_value)
                logger.info("Cycle %d boundary — snapshot generated", cycle_num)
```

- [ ] **Step 3: Add cycle context to daily report header**

In `scripts/generate_daily_report.py`, in `_generate_report()`, after the header (line 68), add cycle context if available:

```python
    # --- Cycle context (if generation has start date) ---
    for gen in gens:
        start_date = gen.get("start_date")
        if start_date:
            try:
                from tradingagents.autoresearch.cycle_tracker import CycleTracker
                ct = CycleTracker(gen_start_date=start_date, state_dir=gen.get("state_dir", ""))
                cycle = ct.current_cycle(date)
                remaining = ct.days_remaining(date)
                lines.append(f"**{gen.get('gen_id', '?')}:** Cycle {cycle}, Day {30 - remaining + 1} of 30 ({remaining} days remaining)")
            except Exception:
                pass
    lines.append("")
```

- [ ] **Step 4: Run full test suite**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add tradingagents/autoresearch/multi_strategy_engine.py scripts/generate_daily_report.py
git commit -m "feat: integrate CycleTracker into engine and daily report"
```

---

## Task 16: Update Existing Tests for New Defaults

**Files:**
- Modify: `tests/test_multi_strategy.py`
- Modify: `tests/test_30day_simulation.py`

- [ ] **Step 1: Update holding period assertions in test_multi_strategy.py**

In `tests/test_multi_strategy.py`, update the holding period test (around lines 58-69) to reflect the new 20-45 day ranges:

```python
def test_all_strategies_30_day_horizon(self):
    """All strategies should have hold_days defaults in 20-30 range."""
    for strategy in strategies:
        params = strategy.get_default_params()
        hold_key = "hold_days" if "hold_days" in params else "rebalance_days"
        hold = params.get(hold_key, 30)
        assert 20 <= hold <= 30, (
            f"{strategy.name}: {hold_key}={hold} outside 20-30 day range"
        )

def test_all_strategies_param_space_floor(self):
    """All strategies should have hold_days floor >= 20."""
    for strategy in strategies:
        space = strategy.get_param_space()
        hold_key = "hold_days" if "hold_days" in space else "rebalance_days"
        if hold_key in space:
            low, high = space[hold_key]
            assert low >= 20, f"{strategy.name}: {hold_key} floor {low} < 20"
            assert high <= 45, f"{strategy.name}: {hold_key} ceiling {high} > 45"
```

- [ ] **Step 2: Update test_30day_simulation.py for expanded universes**

Verify that the 30-day simulation tests still work with the new defaults. Update any hardcoded ticker counts or threshold values that changed.

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/potalora/ai_workspace/trading_agents && .venv/bin/python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add tests/test_multi_strategy.py tests/test_30day_simulation.py
git commit -m "test: update assertions for 30-day horizon alignment and expanded universes"
```

---

## Task 17: Update CLAUDE.md and next-gen-improvements.md

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/next-gen-improvements.md`

- [ ] **Step 1: Update CLAUDE.md — gen_004 entry, strategy changes**

Add gen_004 to the "Current active generations" section:

```markdown
- `gen_004` — Gate loosening, universe expansion, 30-day cycle evaluation (commit `<SHA>`), started 2026-04-04
```

Update the strategy table to reflect new defaults (hold_days column).

Update the `get_param_space` descriptions where ranges changed (hold_days floors).

- [ ] **Step 2: Update next-gen-improvements.md to mark items as done**

Add "Status: Implemented in gen_004" headers to sections 1 and 2, and add section 3 for the 30-day cycle work.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/next-gen-improvements.md
git commit -m "docs: update CLAUDE.md and next-gen-improvements.md for gen_004"
```
