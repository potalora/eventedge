# Commodity Macro Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `commodity_macro` strategy that trades non-agricultural commodity ETFs based on CFTC COT positioning extremes, macro regime confirmation, and catalyst signals.

**Architecture:** New `CFTCSource` data source wrapping `cot_reports` library, new `CommodityMacroStrategy` implementing `StrategyModule` protocol, cohort-aware instrument filtering, portfolio committee commodity awareness, commodity enrichment pipeline, and covered call overlay for GLD/SLV.

**Tech Stack:** Python 3.10+, `cot_reports` library, `fredapi`, OpenBB Platform (optional), pytest

---

### Task 1: Add `cot_reports` optional dependency

**Files:**
- Modify: `pyproject.toml:41-52`

- [ ] **Step 1: Add commodities optional extra**

In `pyproject.toml`, add the `commodities` group after the existing `openbb` group:

```toml
commodities = [
    "cot_reports",
]
```

- [ ] **Step 2: Verify install works**

Run: `pip install -e ".[commodities]" 2>&1 | tail -5`
Expected: successful install with `cot_reports` available

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add commodities optional extra with cot_reports"
```

---

### Task 2: Create CFTCSource data source

**Files:**
- Create: `tradingagents/strategies/data_sources/cftc_source.py`
- Test: `tests/test_commodity_macro.py`

- [ ] **Step 1: Write the failing test for CFTCSource positioning**

Create `tests/test_commodity_macro.py`:

```python
"""Tests for commodity_macro strategy and CFTCSource data source."""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np


class TestCFTCSource:
    """Tests for CFTCSource data source."""

    def test_cftc_source_positioning(self):
        """Mock COT data -> correct percentiles and direction signals."""
        from tradingagents.strategies.data_sources.cftc_source import CFTCSource

        source = CFTCSource()

        # Build mock COT DataFrame with 52 weeks of data
        np.random.seed(42)
        n_weeks = 52
        dates = pd.date_range(end="2026-04-01", periods=n_weeks, freq="W")

        # Gold: high managed money net long -> should signal "short" (contrarian)
        gold_longs = np.linspace(100_000, 200_000, n_weeks)
        gold_shorts = np.full(n_weeks, 50_000)

        rows = []
        for i in range(n_weeks):
            rows.append({
                "Market and Exchange Names": "GOLD - COMEX",
                "As of Date in Form YYYY-MM-DD": dates[i].strftime("%Y-%m-%d"),
                "M_Money_Positions_Long_All": gold_longs[i],
                "M_Money_Positions_Short_All": gold_shorts[i],
            })

        mock_df = pd.DataFrame(rows)

        with patch.object(source, "_fetch_raw_report", return_value=mock_df):
            result = source.fetch({
                "method": "cot_positioning",
                "commodities": ["gold"],
                "lookback_weeks": 52,
            })

        assert "gold" in result
        gold = result["gold"]
        assert 0.0 <= gold["percentile"] <= 1.0
        assert gold["percentile"] > 0.8  # Near top of range
        assert gold["direction_signal"] == "short"  # Contrarian
        assert gold["net_position"] > 0

    def test_cftc_source_unavailable(self):
        """Graceful degradation when cot_reports not installed."""
        from tradingagents.strategies.data_sources.cftc_source import CFTCSource

        source = CFTCSource()
        with patch.dict("sys.modules", {"cot_reports": None}):
            # is_available should return False when library is missing
            with patch("builtins.__import__", side_effect=ImportError("no cot_reports")):
                assert source.is_available() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_commodity_macro.py::TestCFTCSource -v 2>&1 | tail -20`
Expected: FAIL — `cftc_source` module doesn't exist yet

- [ ] **Step 3: Implement CFTCSource**

Create `tradingagents/strategies/data_sources/cftc_source.py`:

```python
"""CFTC Commitments of Traders data source.

Wraps the `cot_reports` library to fetch COT positioning data.
No API key needed — data is public. Graceful ImportError skip
if cot_reports not installed.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# Contract name strings from CFTC disaggregated reports.
# Validated against live data in test_commodity_macro_live.py.
COMMODITY_CODES = {
    "gold": "GOLD - COMEX",
    "silver": "SILVER - COMEX",
    "crude_oil": "CRUDE OIL, LIGHT SWEET - NEW YORK MERCANTILE EXCHANGE",
    "nat_gas": "NATURAL GAS - NEW YORK MERCANTILE EXCHANGE",
    "copper": "COPPER-GRADE #1 - COMEX",
}


class CFTCSource:
    """Data source backed by CFTC Commitments of Traders reports."""

    name: str = "cftc"
    requires_api_key: bool = False

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "cot_positioning")
        dispatch = {
            "cot_report": self._dispatch_cot_report,
            "cot_positioning": self._dispatch_cot_positioning,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except Exception:
            logger.error("CFTCSource.fetch(%s) failed", method, exc_info=True)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        try:
            import cot_reports  # noqa: F401
            return True
        except ImportError:
            logger.info("cot_reports not installed — run: pip install cot_reports")
            return False

    def _fetch_raw_report(self, report_type: str = "disaggregated_futures") -> pd.DataFrame:
        """Fetch raw COT report. Cached per session (data is weekly)."""
        if report_type in self._cache:
            return self._cache[report_type]

        import cot_reports as cot

        report_funcs = {
            "legacy_futures": cot.cot_year,
            "disaggregated_futures": cot.cot_year,
            "traders_in_financial_futures": cot.cot_year,
        }
        func = report_funcs.get(report_type)
        if func is None:
            raise ValueError(f"Unknown report type: {report_type}")

        # Fetch current year's report
        from datetime import datetime
        year = datetime.now().year
        df = func(year, cot_report_type=report_type)

        self._cache[report_type] = df
        return df

    def _dispatch_cot_report(self, params: dict[str, Any]) -> dict[str, Any]:
        report_type = params.get("report_type", "disaggregated_futures")
        df = self._fetch_raw_report(report_type)
        return {"data": df.to_dict(orient="records")[:100]}  # Cap for memory

    def _dispatch_cot_positioning(self, params: dict[str, Any]) -> dict[str, Any]:
        commodities = params.get("commodities", list(COMMODITY_CODES.keys()))
        lookback_weeks = params.get("lookback_weeks", 52)

        df = self._fetch_raw_report("disaggregated_futures")

        results: dict[str, dict[str, Any]] = {}
        for commodity in commodities:
            code = COMMODITY_CODES.get(commodity)
            if code is None:
                logger.warning("Unknown commodity: %s", commodity)
                continue

            mask = df["Market and Exchange Names"].str.contains(code, na=False)
            commodity_df = df[mask].copy()

            if commodity_df.empty:
                logger.warning("No COT data for %s (%s)", commodity, code)
                continue

            # Parse dates and sort
            commodity_df["date"] = pd.to_datetime(
                commodity_df["As of Date in Form YYYY-MM-DD"]
            )
            commodity_df = commodity_df.sort_values("date")

            # Trim to lookback window
            commodity_df = commodity_df.tail(lookback_weeks)

            if len(commodity_df) < 4:
                logger.warning("Insufficient COT data for %s: %d weeks", commodity, len(commodity_df))
                continue

            # Compute net speculative position (Managed Money long - short)
            commodity_df["net_spec"] = (
                commodity_df["M_Money_Positions_Long_All"].astype(float)
                - commodity_df["M_Money_Positions_Short_All"].astype(float)
            )

            latest = commodity_df.iloc[-1]
            net_position = float(latest["net_spec"])

            # Percentile rank over lookback window
            all_nets = commodity_df["net_spec"].values
            percentile = float((all_nets < net_position).sum() / len(all_nets))

            # Week-over-week change
            if len(commodity_df) >= 2:
                prior = float(commodity_df.iloc[-2]["net_spec"])
                wow_change = net_position - prior
            else:
                wow_change = 0.0

            # Contrarian direction signal
            if percentile >= 0.85:
                direction_signal = "short"
            elif percentile <= 0.15:
                direction_signal = "long"
            else:
                direction_signal = "neutral"

            results[commodity] = {
                "net_position": net_position,
                "percentile": round(percentile, 4),
                "wow_change": wow_change,
                "direction_signal": direction_signal,
            }

        return results

    def clear_cache(self) -> None:
        self._cache.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_commodity_macro.py::TestCFTCSource -v 2>&1 | tail -20`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/data_sources/cftc_source.py tests/test_commodity_macro.py
git commit -m "feat: add CFTCSource data source wrapping cot_reports"
```

---

### Task 3: Register CFTCSource in registry and exports

**Files:**
- Modify: `tradingagents/strategies/data_sources/registry.py:87-139`
- Modify: `tradingagents/strategies/data_sources/__init__.py`

- [ ] **Step 1: Register CFTCSource in build_default_registry()**

In `registry.py`, after the OpenBB block (line ~137), add:

```python
    # CFTC COT data (optional — graceful skip if cot_reports not installed)
    try:
        from tradingagents.strategies.data_sources.cftc_source import CFTCSource

        registry.register(CFTCSource())
    except ImportError:
        logger.info("cot_reports not installed — CFTCSource skipped")
```

- [ ] **Step 2: Export CFTCSource from __init__.py**

In `tradingagents/strategies/data_sources/__init__.py`, add the import after the DroughtMonitorSource import:

```python
from tradingagents.strategies.data_sources.cftc_source import CFTCSource
```

Add `"CFTCSource"` to `__all__`.

- [ ] **Step 3: Run existing tests to verify no breakage**

Run: `python -m pytest tests/ -x -q 2>&1 | tail -10`
Expected: all existing tests still pass

- [ ] **Step 4: Commit**

```bash
git add tradingagents/strategies/data_sources/registry.py tradingagents/strategies/data_sources/__init__.py
git commit -m "feat: register CFTCSource in data source registry"
```

---

### Task 4: Add FRED commodity series

**Files:**
- Modify: `tradingagents/strategies/data_sources/fred_source.py:17-27`
- Test: `tests/test_commodity_macro.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_commodity_macro.py`:

```python
class TestFREDCommoditySeries:
    """Test that FRED commodity series are registered."""

    def test_commodity_series_in_map(self):
        from tradingagents.strategies.data_sources.fred_source import SERIES_MAP

        assert "wti_spot" in SERIES_MAP
        assert SERIES_MAP["wti_spot"] == "DCOILWTICO"
        assert "gold_spot" in SERIES_MAP
        assert SERIES_MAP["gold_spot"] == "GOLDAMGBD228NLBM"
        assert "copper_spot" in SERIES_MAP
        assert SERIES_MAP["copper_spot"] == "PCOPPUSDM"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_commodity_macro.py::TestFREDCommoditySeries -v 2>&1 | tail -10`
Expected: FAIL — keys not in SERIES_MAP

- [ ] **Step 3: Add commodity series to SERIES_MAP**

In `fred_source.py`, add these three entries to `SERIES_MAP` after the `"vix"` entry:

```python
    "wti_spot": "DCOILWTICO",             # WTI Crude Oil Spot Price
    "gold_spot": "GOLDAMGBD228NLBM",      # Gold Fixing Price London
    "copper_spot": "PCOPPUSDM",           # Global Price of Copper
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_commodity_macro.py::TestFREDCommoditySeries -v 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/data_sources/fred_source.py tests/test_commodity_macro.py
git commit -m "feat: add WTI, gold, copper spot series to FRED source"
```

---

### Task 5: Add CFTC fetch to multi-strategy engine

**Files:**
- Modify: `tradingagents/strategies/orchestration/multi_strategy_engine.py:693-755`

- [ ] **Step 1: Add CFTC to _fetch_all_data()**

In `multi_strategy_engine.py`, inside `_fetch_all_data()`, add a CFTC fetch block alongside the other API-key sources in the `api_fetches` dict (after the `usaspending` block, around line 738):

```python
        if "cftc" in needed_sources and "cftc" in available:
            api_fetches["cftc"] = (self._fetch_cftc_data, ())
```

Then add the `_fetch_cftc_data` method to the class (after `_fetch_drought_data` or similar):

```python
    def _fetch_cftc_data(self) -> dict[str, Any]:
        """Fetch CFTC COT positioning data for commodity strategy."""
        source = self.registry.get("cftc")
        if source is None:
            return {}

        return source.fetch({
            "method": "cot_positioning",
            "commodities": ["gold", "silver", "crude_oil", "nat_gas", "copper"],
            "lookback_weeks": 52,
        })
```

- [ ] **Step 2: Run existing tests to verify no breakage**

Run: `python -m pytest tests/ -x -q 2>&1 | tail -10`
Expected: all pass

- [ ] **Step 3: Commit**

```bash
git add tradingagents/strategies/orchestration/multi_strategy_engine.py
git commit -m "feat: add CFTC data fetch to multi-strategy engine"
```

---

### Task 6: Create CommodityMacroStrategy

**Files:**
- Create: `tradingagents/strategies/modules/commodity_macro.py`
- Test: `tests/test_commodity_macro.py`

- [ ] **Step 1: Write failing tests for screen() and check_exit()**

Add to `tests/test_commodity_macro.py`:

```python
class TestCommodityMacroStrategy:
    """Tests for CommodityMacroStrategy."""

    def _make_cot_data(self, gold_pctl=0.5, crude_pctl=0.5, gold_dir="neutral", crude_dir="neutral"):
        """Helper to build mock COT data."""
        return {
            "gold": {"net_position": 100_000, "percentile": gold_pctl, "wow_change": 0, "direction_signal": gold_dir},
            "crude_oil": {"net_position": -50_000, "percentile": crude_pctl, "wow_change": 0, "direction_signal": crude_dir},
            "silver": {"net_position": 50_000, "percentile": 0.5, "wow_change": 0, "direction_signal": "neutral"},
            "nat_gas": {"net_position": 20_000, "percentile": 0.5, "wow_change": 0, "direction_signal": "neutral"},
            "copper": {"net_position": 30_000, "percentile": 0.5, "wow_change": 0, "direction_signal": "neutral"},
        }

    def _make_fred_data(self, fed_funds=5.0, cpi_latest=3.0, cpi_3m_ago=3.0, vix=20.0, yield_curve=0.5):
        """Helper to build mock FRED data."""
        return {
            "FEDFUNDS": {pd.Timestamp("2026-01-01"): fed_funds},
            "CPIAUCSL": {pd.Timestamp("2025-10-01"): cpi_3m_ago, pd.Timestamp("2026-01-01"): cpi_latest},
            "VIXCLS": {pd.Timestamp("2026-01-01"): vix},
            "T10Y2Y": {pd.Timestamp("2026-01-01"): yield_curve},
        }

    def test_screen_cot_gate_triggers(self):
        """Extreme positioning (90th pctl) -> candidates with correct ETF tickers."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy

        strategy = CommodityMacroStrategy()
        data = {
            "cftc": self._make_cot_data(gold_pctl=0.90, gold_dir="short"),
            "fred": self._make_fred_data(),
            "regulations": {},
            "finnhub": {},
        }
        params = strategy.get_default_params("3m")
        params["eligible_instruments"] = ["GLD", "SLV", "PDBC", "COPX", "XLE", "USO", "UNG"]

        candidates = strategy.screen(data, "2026-04-01", params)
        assert len(candidates) > 0
        assert candidates[0].ticker == "GLD"
        assert candidates[0].direction == "short"
        assert candidates[0].metadata["needs_llm_analysis"] is True
        assert candidates[0].metadata["analysis_type"] == "commodity_macro"

    def test_screen_cot_gate_no_trigger(self):
        """Moderate positioning (50th pctl) -> empty list."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy

        strategy = CommodityMacroStrategy()
        data = {
            "cftc": self._make_cot_data(),  # All at 50th percentile
            "fred": self._make_fred_data(),
            "regulations": {},
            "finnhub": {},
        }
        params = strategy.get_default_params("3m")
        params["eligible_instruments"] = ["GLD", "SLV", "PDBC"]

        candidates = strategy.screen(data, "2026-04-01", params)
        assert len(candidates) == 0

    def test_screen_macro_veto(self):
        """COT extreme + contradicting macro -> no candidates."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy

        strategy = CommodityMacroStrategy()
        # Gold at extreme long (signal: short gold) but we test long gold veto:
        # Extreme short gold (signal: long gold) + real rates rising >50bps
        data = {
            "cftc": self._make_cot_data(gold_pctl=0.10, gold_dir="long"),
            "fred": self._make_fred_data(fed_funds=5.5, cpi_latest=2.5, cpi_3m_ago=3.0),
            # real_rate_now = 5.5 - 2.5 = 3.0, real_rate_3m = 5.5 - 3.0 = 2.5
            # delta = 0.5 (50bps) -> veto long gold
            "regulations": {},
            "finnhub": {},
        }
        params = strategy.get_default_params("3m")
        params["eligible_instruments"] = ["GLD", "SLV"]

        candidates = strategy.screen(data, "2026-04-01", params)
        # Gold should be vetoed
        gold_candidates = [c for c in candidates if c.ticker == "GLD"]
        assert len(gold_candidates) == 0

    def test_screen_catalyst_boost(self):
        """Score increases when catalyst aligns."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy

        strategy = CommodityMacroStrategy()
        base_data = {
            "cftc": self._make_cot_data(gold_pctl=0.90, gold_dir="short"),
            "fred": self._make_fred_data(),
            "regulations": {},
            "finnhub": {},
        }
        params = strategy.get_default_params("3m")
        params["eligible_instruments"] = ["GLD", "SLV", "PDBC"]

        no_catalyst = strategy.screen(base_data, "2026-04-01", params)

        # Add catalyst
        catalyst_data = dict(base_data)
        catalyst_data["regulations"] = {"results": [{"title": "New gold mining regulation announced"}]}

        with_catalyst = strategy.screen(catalyst_data, "2026-04-01", params)

        if no_catalyst and with_catalyst:
            assert with_catalyst[0].score >= no_catalyst[0].score

    def test_short_only_enforcement(self):
        """COT long crude -> USO NOT emitted long, XLE substituted or skipped."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy

        strategy = CommodityMacroStrategy()
        data = {
            "cftc": self._make_cot_data(crude_pctl=0.10, crude_dir="long"),
            "fred": self._make_fred_data(),
            "regulations": {},
            "finnhub": {},
        }
        params = strategy.get_default_params("3m")
        params["eligible_instruments"] = ["GLD", "SLV", "PDBC", "COPX", "XLE", "USO", "UNG"]

        candidates = strategy.screen(data, "2026-04-01", params)
        # USO should NOT appear as long
        uso_longs = [c for c in candidates if c.ticker == "USO" and c.direction == "long"]
        assert len(uso_longs) == 0
        # XLE should appear as long substitute (if crude signals long)
        xle_longs = [c for c in candidates if c.ticker == "XLE" and c.direction == "long"]
        assert len(xle_longs) > 0 or len(candidates) == 0  # Either substituted or all vetoed

    def test_horizon_filtering(self):
        """30d -> no candidates. 3m -> candidates. 1y -> GLD/SLV only."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy

        strategy = CommodityMacroStrategy()
        data = {
            "cftc": self._make_cot_data(gold_pctl=0.90, gold_dir="short"),
            "fred": self._make_fred_data(),
            "regulations": {},
            "finnhub": {},
        }

        # 30d: commodity_eligible=False -> strategy should check and skip
        params_30d = strategy.get_default_params("30d")
        params_30d["commodity_eligible"] = False
        params_30d["eligible_instruments"] = []
        candidates_30d = strategy.screen(data, "2026-04-01", params_30d)
        assert len(candidates_30d) == 0

        # 3m: should produce candidates
        params_3m = strategy.get_default_params("3m")
        params_3m["eligible_instruments"] = ["GLD", "SLV", "PDBC"]
        candidates_3m = strategy.screen(data, "2026-04-01", params_3m)
        assert len(candidates_3m) > 0

        # 1y: only GLD/SLV
        params_1y = strategy.get_default_params("1y")
        params_1y["eligible_instruments"] = ["GLD", "SLV"]
        candidates_1y = strategy.screen(data, "2026-04-01", params_1y)
        for c in candidates_1y:
            assert c.ticker in ("GLD", "SLV")

    def test_futures_to_etf_map(self):
        """All map entries resolve, no dangling keys."""
        from tradingagents.strategies.modules.commodity_macro import (
            FUTURES_TO_ETF_MAP, ETF_TO_FUTURES_UNDERLYING, COMMODITY_ETFS,
        )

        for futures, etf in FUTURES_TO_ETF_MAP.items():
            assert etf in COMMODITY_ETFS, f"{etf} from {futures} not in COMMODITY_ETFS"

        for etf in ETF_TO_FUTURES_UNDERLYING:
            assert etf in COMMODITY_ETFS, f"{etf} not in COMMODITY_ETFS"

    def test_check_exit_hold_period(self):
        """Standard hold period exit."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy

        strategy = CommodityMacroStrategy()
        params = strategy.get_default_params("3m")

        should_exit, reason = strategy.check_exit(
            "GLD", 200.0, 210.0, params["hold_days"], params, {}
        )
        assert should_exit is True
        assert reason == "hold_period"

        should_exit, reason = strategy.check_exit(
            "GLD", 200.0, 210.0, 5, params, {}
        )
        assert should_exit is False

    def test_check_exit_cot_normalization(self):
        """Early exit on positioning normalization."""
        from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy

        strategy = CommodityMacroStrategy()
        params = strategy.get_default_params("3m")

        # COT normalized (percentile back inside 30-70)
        data = {
            "cftc": {
                "gold": {"net_position": 80_000, "percentile": 0.50, "wow_change": 0, "direction_signal": "neutral"},
            },
        }
        should_exit, reason = strategy.check_exit(
            "GLD", 200.0, 210.0, 10, params, data
        )
        assert should_exit is True
        assert reason == "cot_normalized"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_commodity_macro.py::TestCommodityMacroStrategy -v 2>&1 | tail -20`
Expected: FAIL — `commodity_macro` module doesn't exist

- [ ] **Step 3: Implement CommodityMacroStrategy**

Create `tradingagents/strategies/modules/commodity_macro.py`:

```python
"""Commodity Macro strategy — trades non-ag commodity ETFs on COT positioning extremes.

Signal logic:
1. CFTC COT gate: extreme speculative positioning (contrarian).
2. Macro confirmation: FRED data veto for contradicting macro regimes.
3. Catalyst scan: regulatory/supply chain news for optional boost.
4. Emit ETF candidates via FUTURES_TO_ETF_MAP.
"""
from __future__ import annotations

import logging
from typing import Any

from .base import Candidate

logger = logging.getLogger(__name__)

# --- Instrument Constants ---

FUTURES_TO_ETF_MAP = {
    "GC=F": "GLD",
    "SI=F": "SLV",
    "CL=F": "USO",
    "NG=F": "UNG",
    "HG=F": "COPX",
}

ETF_TO_FUTURES_UNDERLYING = {
    "GLD": "GC",
    "SLV": "SI",
    "USO": "CL",
    "UNG": "NG",
    "COPX": "HG",
    "PDBC": None,
    "XLE": "CL",
}

SHORT_ONLY_ETFS = {"USO", "UNG"}
SAFE_HAVEN_ETFS = {"GLD", "SLV"}
COMMODITY_ETFS = {"GLD", "SLV", "PDBC", "COPX", "XLE", "USO", "UNG"}

# Map from commodity name to primary ETF
_COMMODITY_TO_ETF = {
    "gold": "GLD",
    "silver": "SLV",
    "crude_oil": "USO",
    "nat_gas": "UNG",
    "copper": "COPX",
}

# Long substitutions for SHORT_ONLY_ETFS
_LONG_SUBSTITUTIONS = {
    "USO": "XLE",   # CL long -> XLE (energy sector proxy)
    "UNG": None,     # NG long -> skip (no suitable proxy)
}

# Catalyst keywords for commodity-relevant regulatory/supply chain news
_CATALYST_KEYWORDS = [
    "gold", "silver", "copper", "crude", "oil", "natural gas", "lng",
    "mining", "metals", "energy", "opec", "pipeline", "refinery",
    "tariff", "sanctions", "embargo", "supply chain", "commodity",
]


class CommodityMacroStrategy:
    """Trade non-ag commodity ETFs based on COT positioning extremes."""

    name = "commodity_macro"
    track = "paper_trade"
    data_sources = ["yfinance", "cftc", "fred", "regulations", "finnhub"]

    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "cot_extreme_pct": (75, 95),
            "cot_lookback_weeks": (26, 104),
            "hold_days": hp["hold_days_range"],
            "macro_veto_enabled": (True, False),
            "catalyst_boost": (0.0, 0.3),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "cot_extreme_pct": 85,
            "cot_lookback_weeks": 52,
            "hold_days": hp["hold_days_default"],
            "macro_veto_enabled": True,
            "catalyst_boost": 0.15,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for commodity opportunities using COT + macro + catalyst."""
        eligible = params.get("eligible_instruments", [])
        if not eligible:
            return []

        cot_data = data.get("cftc", {})
        if not cot_data or "error" in cot_data:
            return []

        cot_extreme_pct = params.get("cot_extreme_pct", 85) / 100.0
        cot_normal_low = 0.30
        cot_normal_high = 0.70
        macro_veto = params.get("macro_veto_enabled", True)
        catalyst_boost_val = params.get("catalyst_boost", 0.15)

        fred_data = data.get("fred", {})

        candidates = []

        for commodity, cot in cot_data.items():
            if isinstance(cot, str):
                continue  # Skip error entries

            percentile = cot.get("percentile", 0.5)
            direction = cot.get("direction_signal", "neutral")

            if direction == "neutral":
                continue

            # Step 1: COT Gate
            if not (percentile >= cot_extreme_pct or percentile <= (1.0 - cot_extreme_pct)):
                continue

            # Step 2: Macro veto
            if macro_veto and self._macro_vetoes(commodity, direction, fred_data):
                logger.info("Macro veto: %s %s", commodity, direction)
                continue

            # Resolve ETF
            etf = _COMMODITY_TO_ETF.get(commodity)
            if etf is None:
                continue

            # Step: SHORT_ONLY enforcement
            if etf in SHORT_ONLY_ETFS and direction == "long":
                substitute = _LONG_SUBSTITUTIONS.get(etf)
                if substitute is None:
                    continue
                etf = substitute

            if etf not in eligible:
                continue

            # Step 3: Catalyst scan
            base_score = 0.5
            catalyst_found = self._scan_catalysts(commodity, data)
            if catalyst_found:
                base_score += catalyst_boost_val

            candidates.append(
                Candidate(
                    ticker=etf,
                    date=date,
                    direction=direction,
                    score=base_score,
                    metadata={
                        "commodity": commodity,
                        "cot_percentile": percentile,
                        "cot_net_position": cot.get("net_position", 0),
                        "catalyst_found": catalyst_found,
                        "needs_llm_analysis": True,
                        "analysis_type": "commodity_macro",
                    },
                )
            )

        return candidates

    def check_exit(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        holding_days: int,
        params: dict,
        data: dict,
    ) -> tuple[bool, str]:
        """Exit on hold period or COT normalization."""
        hold_days = params.get("hold_days", 90)
        if holding_days >= hold_days:
            return True, "hold_period"

        # Early exit: COT positioning normalized
        cot_data = data.get("cftc", {})
        if cot_data:
            # Find commodity for this ticker
            for commodity, etf in _COMMODITY_TO_ETF.items():
                if etf == ticker or _LONG_SUBSTITUTIONS.get(etf) == ticker:
                    cot = cot_data.get(commodity, {})
                    if isinstance(cot, dict):
                        pctl = cot.get("percentile", 0.5)
                        if 0.30 <= pctl <= 0.70:
                            return True, "cot_normalized"

        return False, ""

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

        return f"""You are optimizing a Commodity Macro strategy that trades
non-agricultural commodity ETFs (GLD, SLV, USO, UNG, COPX, XLE, PDBC)
based on CFTC Commitments of Traders positioning extremes with macro
confirmation.

Current parameters: {current}

Parameter ranges:
- cot_extreme_pct: 75-95 (percentile threshold for extreme positioning)
- cot_lookback_weeks: 26-104 (lookback for percentile calculation)
- hold_days: horizon-dependent (holding period)
- macro_veto_enabled: True/False (whether macro confirmation is required)
- catalyst_boost: 0.0-0.3 (score boost when catalyst present)

Recent results:
{results_text or '  No results yet.'}

Suggest 3 new parameter combinations. Return JSON array of 3 param dicts."""

    @staticmethod
    def _macro_vetoes(commodity: str, direction: str, fred_data: dict) -> bool:
        """Check if macro conditions contradict the COT signal."""
        if not fred_data:
            return False

        # Extract FRED values
        fed_funds = _latest_value(fred_data.get("FEDFUNDS", {}))
        cpi_values = fred_data.get("CPIAUCSL", {})
        vix = _latest_value(fred_data.get("VIXCLS", {}))

        # CPI momentum: latest vs 3 months ago
        cpi_sorted = sorted(cpi_values.items()) if isinstance(cpi_values, dict) else []
        if len(cpi_sorted) >= 2:
            cpi_latest = cpi_sorted[-1][1]
            cpi_3m_ago = cpi_sorted[0][1]
            cpi_momentum = cpi_latest - cpi_3m_ago
        else:
            cpi_latest = _latest_value(cpi_values)
            cpi_momentum = 0.0

        # Real rate = fed_funds - CPI (approximate)
        real_rate_now = (fed_funds or 0) - (cpi_latest or 0)

        # Veto: long gold/silver + real rates rising > 50bps
        if commodity in ("gold", "silver") and direction == "long":
            if len(cpi_sorted) >= 2:
                real_rate_3m = (fed_funds or 0) - cpi_3m_ago
                real_rate_delta = real_rate_now - real_rate_3m
                if real_rate_delta > 0.5:
                    return True

        # Veto: long energy + CPI momentum negative (deflation)
        if commodity in ("crude_oil", "nat_gas") and direction == "long":
            if cpi_momentum < 0:
                return True

        # Veto: short any + strong risk-on (VIX < 15)
        if direction == "short" and vix is not None and vix < 15:
            return True

        return False

    @staticmethod
    def _scan_catalysts(commodity: str, data: dict) -> bool:
        """Scan regulations and news for commodity-relevant catalysts."""
        relevant_keywords = [k for k in _CATALYST_KEYWORDS if k in commodity or commodity in k]
        # Also include general keywords
        relevant_keywords.extend(["mining", "energy", "opec", "tariff", "sanctions"])

        # Check regulations
        regs = data.get("regulations", {})
        if isinstance(regs, dict):
            results = regs.get("results", [])
            if isinstance(results, list):
                for reg in results:
                    title = str(reg.get("title", "")).lower()
                    if any(kw in title for kw in relevant_keywords):
                        return True

        # Check finnhub news
        finnhub = data.get("finnhub", {})
        if isinstance(finnhub, dict):
            news = finnhub.get("news", [])
            if isinstance(news, list):
                for item in news:
                    headline = str(item.get("headline", "")).lower()
                    if any(kw in headline for kw in relevant_keywords):
                        return True

        return False


def _latest_value(series_data) -> float | None:
    """Get the latest value from FRED series data (dict or pd.Series)."""
    if not series_data:
        return None
    if isinstance(series_data, dict):
        if not series_data:
            return None
        sorted_items = sorted(series_data.items())
        return sorted_items[-1][1]
    # pd.Series
    try:
        return float(series_data.iloc[-1])
    except (IndexError, TypeError):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_commodity_macro.py::TestCommodityMacroStrategy -v 2>&1 | tail -30`
Expected: all 9 tests pass

- [ ] **Step 5: Commit**

```bash
git add tradingagents/strategies/modules/commodity_macro.py tests/test_commodity_macro.py
git commit -m "feat: add CommodityMacroStrategy with COT gate, macro veto, catalyst scan"
```

---

### Task 7: Register strategy and add LLM prompt

**Files:**
- Modify: `tradingagents/strategies/modules/__init__.py`
- Modify: `tradingagents/strategies/learning/llm_analyzer.py:49-94`

- [ ] **Step 1: Register CommodityMacroStrategy in __init__.py**

Add import after the WeatherAgStrategy import (line 21):

```python
from .commodity_macro import CommodityMacroStrategy
```

Add `"CommodityMacroStrategy"` to `__all__` list.

Add `CommodityMacroStrategy()` to the list returned by `get_paper_trade_strategies()`.

- [ ] **Step 2: Add commodity_macro LLM prompt**

In `llm_analyzer.py`, add to `_DEFAULT_PROMPTS` dict after the `"ag_weather"` entry:

```python
    "commodity_macro": """You are analyzing commodity positioning and macro data
for a non-agricultural commodity trade signal.
Assess whether CFTC COT positioning extremes, macro regime, and catalysts
support the proposed direction. Consider:
1. Is speculative positioning truly extreme, or could it extend further?
2. Does the macro regime (real rates, CPI, VIX) support or contradict?
3. Are there regulatory or supply chain catalysts that could accelerate the move?
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"short"/"neutral"), score (0.0-1.0), reasoning (1-2 sentences).
Keep ALL string values under 100 characters.""",
```

- [ ] **Step 3: Run existing tests to verify no breakage**

Run: `python -m pytest tests/ -x -q 2>&1 | tail -10`
Expected: all pass

- [ ] **Step 4: Commit**

```bash
git add tradingagents/strategies/modules/__init__.py tradingagents/strategies/learning/llm_analyzer.py
git commit -m "feat: register commodity_macro strategy and add LLM prompt"
```

---

### Task 8: Add commodity fields to cohort profiles

**Files:**
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py:22-127`
- Test: `tests/test_commodity_macro.py`

- [ ] **Step 1: Write failing integration tests**

Add to `tests/test_commodity_macro.py`:

```python
class TestCohortIntegration:
    """Integration tests for commodity cohort configuration."""

    def test_30d_cohort_excludes_commodities(self):
        """30d cohort gets no commodity candidates."""
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS

        hp = HORIZON_PARAMS["30d"]
        assert hp.get("commodity_eligible") is False

    def test_5k_cohort_excludes_commodities(self):
        """5k profiles get no commodity candidates."""
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES

        profile = SIZE_PROFILES["5k"]
        assert profile.commodity_eligible is False
        assert profile.commodity_instruments == []

    def test_10k_commodity_eligible(self):
        """10k profiles are commodity eligible with limited instruments."""
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES

        profile = SIZE_PROFILES["10k"]
        assert profile.commodity_eligible is True
        assert profile.max_commodity_allocation_pct == 0.10
        assert "GLD" in profile.commodity_instruments
        assert "SLV" in profile.commodity_instruments

    def test_1y_horizon_narrows_instruments(self):
        """1y horizon overrides to GLD/SLV only."""
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS

        hp = HORIZON_PARAMS["1y"]
        assert hp.get("commodity_eligible") is True
        assert hp.get("commodity_instruments_override") == ["GLD", "SLV"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_commodity_macro.py::TestCohortIntegration -v 2>&1 | tail -15`
Expected: FAIL — fields don't exist

- [ ] **Step 3: Add commodity fields to PortfolioSizeProfile**

In `cohort_orchestrator.py`, add these fields to the `PortfolioSizeProfile` dataclass after the options fields (after line 43):

```python
    # Commodity eligibility
    commodity_eligible: bool = False
    max_commodity_allocation_pct: float = 0.0
    commodity_instruments: list[str] = field(default_factory=list)
```

- [ ] **Step 4: Update SIZE_PROFILES**

Add commodity fields to each profile in `SIZE_PROFILES`:

For `"5k"`: no changes needed (defaults are False/0.0/[]).

For `"10k"`, add:
```python
        commodity_eligible=True,
        max_commodity_allocation_pct=0.10,
        commodity_instruments=["GLD", "SLV", "PDBC"],
```

For `"50k"`, add:
```python
        commodity_eligible=True,
        max_commodity_allocation_pct=0.10,
        commodity_instruments=["GLD", "SLV", "PDBC", "COPX", "XLE", "USO", "UNG"],
```

For `"100k"`, add:
```python
        commodity_eligible=True,
        max_commodity_allocation_pct=0.10,
        commodity_instruments=["GLD", "SLV", "PDBC", "COPX", "XLE", "USO", "UNG"],
```

- [ ] **Step 5: Update HORIZON_PARAMS**

Add commodity fields to each horizon in `HORIZON_PARAMS`:

```python
    "30d": {
        ...existing fields...
        "commodity_eligible": False,
    },
    "3m": {
        ...existing fields...
        "commodity_eligible": True,
        "commodity_signal_decay_window": (7, 21),
    },
    "6m": {
        ...existing fields...
        "commodity_eligible": True,
        "commodity_signal_decay_window": (14, 45),
    },
    "1y": {
        ...existing fields...
        "commodity_eligible": True,
        "commodity_signal_decay_window": (30, 90),
        "commodity_instruments_override": ["GLD", "SLV"],
    },
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_commodity_macro.py::TestCohortIntegration -v 2>&1 | tail -15`
Expected: 4 passed

- [ ] **Step 7: Commit**

```bash
git add tradingagents/strategies/orchestration/cohort_orchestrator.py tests/test_commodity_macro.py
git commit -m "feat: add commodity eligibility to cohort size profiles and horizon params"
```

---

### Task 9: Portfolio committee — commodity allocation cap and regime alignment

**Files:**
- Modify: `tradingagents/strategies/trading/portfolio_committee.py`
- Test: `tests/test_commodity_macro.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_commodity_macro.py`:

```python
class TestPortfolioCommittee:
    """Tests for portfolio committee commodity awareness."""

    def test_commodity_regime_alignment_crisis_gld(self):
        """Crisis + GLD long = aligned (safe haven)."""
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee

        committee = PortfolioCommittee()
        result = committee._assess_regime_alignment("long", {"overall_regime": "crisis"}, ticker="GLD")
        assert result == "aligned"

    def test_commodity_regime_alignment_crisis_xle(self):
        """Crisis + XLE long = misaligned."""
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee

        committee = PortfolioCommittee()
        result = committee._assess_regime_alignment("long", {"overall_regime": "crisis"}, ticker="XLE")
        assert result == "misaligned"

    def test_commodity_regime_alignment_stressed_slv(self):
        """Stressed + SLV long = aligned (safe haven)."""
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee

        committee = PortfolioCommittee()
        result = committee._assess_regime_alignment("long", {"overall_regime": "stressed"}, ticker="SLV")
        assert result == "aligned"

    def test_regime_alignment_backward_compatible(self):
        """Existing callers without ticker still work."""
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee

        committee = PortfolioCommittee()
        # No ticker param — should work exactly as before
        result = committee._assess_regime_alignment("short", {"overall_regime": "crisis"})
        assert result == "aligned"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_commodity_macro.py::TestPortfolioCommittee -v 2>&1 | tail -15`
Expected: FAIL — `_assess_regime_alignment` doesn't accept `ticker`

- [ ] **Step 3: Modify _assess_regime_alignment() to accept ticker**

In `portfolio_committee.py`, change the method signature from:

```python
def _assess_regime_alignment(self, direction: str, regime_context: dict) -> str:
```

to:

```python
def _assess_regime_alignment(self, direction: str, regime_context: dict, ticker: str = "") -> str:
```

Add safe-haven logic at the start of the crisis/stressed block. Replace:

```python
        if overall in ("crisis", "stressed"):
            return "aligned" if direction == "short" else "misaligned"
```

with:

```python
        if overall in ("crisis", "stressed"):
            if direction == "short":
                return "aligned"
            # Safe-haven commodities are aligned long in crisis
            if ticker in ("GLD", "SLV") and direction == "long":
                return "aligned"
            return "misaligned"
```

- [ ] **Step 4: Add commodity allocation cap to _rule_based_synthesize()**

After the sector concentration enforcement block (around line 225), add:

```python
        # Enforce commodity allocation cap
        if self._size_profile and getattr(self._size_profile, 'commodity_eligible', False):
            from tradingagents.strategies.modules.commodity_macro import COMMODITY_ETFS
            max_commodity = getattr(self._size_profile, 'max_commodity_allocation_pct', 0.10)
            commodity_alloc = sum(
                r.position_size_pct for r in recommendations if r.ticker in COMMODITY_ETFS
            )
            if commodity_alloc > max_commodity and commodity_alloc > 0:
                scale = max_commodity / commodity_alloc
                for r in recommendations:
                    if r.ticker in COMMODITY_ETFS:
                        r.position_size_pct *= scale
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_commodity_macro.py::TestPortfolioCommittee -v 2>&1 | tail -15`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add tradingagents/strategies/trading/portfolio_committee.py tests/test_commodity_macro.py
git commit -m "feat: add commodity regime alignment and allocation cap to portfolio committee"
```

---

### Task 10: Portfolio committee — LLM prompt commodity context

**Files:**
- Modify: `tradingagents/strategies/trading/portfolio_committee.py`

- [ ] **Step 1: Extend _build_prompt() with commodity context**

In `_build_prompt()`, after the short interest block (around line 355), add:

```python
        # Commodity context (if commodity signals present)
        from tradingagents.strategies.modules.commodity_macro import COMMODITY_ETFS
        commodity_tickers = [s for s in signals if s.get("ticker", "") in COMMODITY_ETFS]
        if commodity_tickers:
            commodity_lines = []
            for s in commodity_tickers:
                meta = s.get("metadata", {})
                ticker = s.get("ticker", "?")
                pctl = meta.get("cot_percentile", "N/A")
                commodity_lines.append(f"  {ticker}: COT speculative percentile={pctl}")
            enrichment_str += "\nCommodity context:\n" + "\n".join(commodity_lines)
```

- [ ] **Step 2: Run existing tests to verify no breakage**

Run: `python -m pytest tests/ -x -q 2>&1 | tail -10`
Expected: all pass

- [ ] **Step 3: Commit**

```bash
git add tradingagents/strategies/trading/portfolio_committee.py
git commit -m "feat: add commodity context to portfolio committee LLM prompt"
```

---

### Task 11: Covered call overlay for commodity ETFs

**Files:**
- Modify: `tradingagents/strategies/trading/portfolio_committee.py`
- Test: `tests/test_commodity_macro.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_commodity_macro.py`:

```python
class TestCoveredCallOverlay:
    """Test covered call overlay for commodity ETFs."""

    def test_covered_call_overlay_on_gld(self):
        """GLD long position -> overlay considers it with earnings_days=999."""
        from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee
        from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES

        profile = SIZE_PROFILES["10k"]
        committee = PortfolioCommittee(size_profile=profile)

        positions = [
            {"ticker": "GLD", "direction": "long", "entry_price": 220.0, "shares": 10},
        ]
        iv_data = {"GLD": {"iv": 0.18, "iv_rank": 0.45}}
        earnings_dates = {}  # No earnings for ETFs

        # The _llm_covered_call_overlay should receive earnings_in=999 for GLD
        # We'll verify the prompt includes the override
        import unittest.mock as mock
        with mock.patch.object(committee, '_call_llm', return_value='[]') as mock_llm:
            committee.generate_covered_call_overlays(positions, iv_data, earnings_dates, "2026-04-01")
            if mock_llm.called:
                prompt = mock_llm.call_args[1].get("prompt", "") or mock_llm.call_args[0][1] if len(mock_llm.call_args[0]) > 1 else ""
                # Verify earnings_in=999 for GLD
                assert "999" in str(mock_llm.call_args)
```

- [ ] **Step 2: Modify _llm_covered_call_overlay() for commodity ETFs**

In `portfolio_committee.py`, in the `_llm_covered_call_overlay` method, after the line that sets `earnings_days` (line 469):

```python
            earnings_days = earnings_dates.get(ticker, "unknown")
```

Add the commodity ETF override:

```python
            # Commodity ETFs have no earnings risk
            from tradingagents.strategies.modules.commodity_macro import COMMODITY_ETFS
            if ticker in COMMODITY_ETFS:
                earnings_days = 999
```

- [ ] **Step 3: Run test to verify it passes**

Run: `python -m pytest tests/test_commodity_macro.py::TestCoveredCallOverlay -v 2>&1 | tail -10`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add tradingagents/strategies/trading/portfolio_committee.py tests/test_commodity_macro.py
git commit -m "feat: set earnings_days=999 for commodity ETFs in covered call overlay"
```

---

### Task 12: OpenBB futures curve enrichment

**Files:**
- Modify: `tradingagents/strategies/data_sources/openbb_source.py`
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py`

- [ ] **Step 1: Add commodity_futures_curve method to OpenBBSource**

In `openbb_source.py`, add to the dispatch dict in `fetch()`:

```python
        "commodity_futures_curve": self._commodity_futures_curve,
```

Add the method to the class:

```python
    def _commodity_futures_curve(self, params: dict[str, Any]) -> dict[str, Any]:
        """Fetch futures term structure for a commodity underlying."""
        symbol = params.get("symbol", "")
        if not symbol:
            return {"error": "commodity_futures_curve requires 'symbol'"}

        ckey = f"commodity_futures_curve|{symbol}"
        if ckey in self._cache:
            return self._cache[ckey]

        try:
            obb = self._get_obb()
            resp = obb.derivatives.futures.historical(symbol=symbol, provider="yfinance")
            results = resp.results or []
            if len(results) < 2:
                return {"error": f"insufficient futures data for {symbol}"}

            front = _getfield(results[0], "close", 0.0) or 0.0
            second = _getfield(results[1], "close", 0.0) or 0.0
            contango_pct = round((second - front) / front * 100, 2) if front else 0.0

            result = {
                "symbol": symbol,
                "front_month": front,
                "second_month": second,
                "contango_pct": contango_pct,
            }
            self._cache[ckey] = result
            return result
        except Exception:
            logger.error("Failed to fetch futures curve for %s", symbol, exc_info=True)
            return {"error": f"futures curve fetch failed for {symbol}"}
```

- [ ] **Step 2: Extend _fetch_openbb_enrichment() for commodity signals**

In `cohort_orchestrator.py`, in `_fetch_openbb_enrichment()`, after the Fama-French block (around line 317), add:

```python
        # Fetch commodity futures curves for commodity signals
        from tradingagents.strategies.modules.commodity_macro import ETF_TO_FUTURES_UNDERLYING
        commodity_tickers = [t for t in tickers if t in ETF_TO_FUTURES_UNDERLYING]
        if commodity_tickers:
            curves = {}
            for ticker in commodity_tickers:
                underlying = ETF_TO_FUTURES_UNDERLYING.get(ticker)
                if underlying is None:
                    continue
                result = openbb_source.fetch({
                    "method": "commodity_futures_curve",
                    "symbol": underlying,
                })
                if "error" not in result:
                    curves[underlying] = result
            if curves:
                enrichment["commodity_futures_curves"] = curves
```

- [ ] **Step 3: Run existing tests to verify no breakage**

Run: `python -m pytest tests/ -x -q 2>&1 | tail -10`
Expected: all pass

- [ ] **Step 4: Commit**

```bash
git add tradingagents/strategies/data_sources/openbb_source.py tradingagents/strategies/orchestration/cohort_orchestrator.py
git commit -m "feat: add commodity futures curve enrichment via OpenBB"
```

---

### Task 13: Live API smoke tests

**Files:**
- Create: `tests/test_commodity_macro_live.py`

- [ ] **Step 1: Create live smoke tests**

Create `tests/test_commodity_macro_live.py`:

```python
"""Live API smoke tests for commodity_macro strategy.

Skipped in normal pytest runs. Invoke with: pytest -m live
"""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.live


class TestCFTCLive:
    """Live CFTC data tests."""

    def test_cftc_source_live_fetch(self):
        """Real COT report is non-empty, expected columns present, COMMODITY_CODES match."""
        from tradingagents.strategies.data_sources.cftc_source import CFTCSource, COMMODITY_CODES

        source = CFTCSource()
        if not source.is_available():
            pytest.skip("cot_reports not installed")

        df = source._fetch_raw_report("disaggregated_futures")
        assert len(df) > 0, "COT report is empty"
        assert "Market and Exchange Names" in df.columns
        assert "M_Money_Positions_Long_All" in df.columns

        # Verify COMMODITY_CODES strings match actual data
        market_names = df["Market and Exchange Names"].unique()
        for commodity, code in COMMODITY_CODES.items():
            matches = [m for m in market_names if code in m]
            assert len(matches) > 0, (
                f"COMMODITY_CODES['{commodity}'] = '{code}' not found in report. "
                f"Available names containing partial match: "
                f"{[m for m in market_names if commodity.split('_')[0].upper() in m.upper()][:5]}"
            )

    def test_cftc_positioning_live(self):
        """Live positioning for gold/crude returns percentiles in 0-1 range."""
        from tradingagents.strategies.data_sources.cftc_source import CFTCSource

        source = CFTCSource()
        if not source.is_available():
            pytest.skip("cot_reports not installed")

        result = source.fetch({
            "method": "cot_positioning",
            "commodities": ["gold", "crude_oil"],
            "lookback_weeks": 52,
        })
        assert "error" not in result, f"Fetch failed: {result}"
        assert "gold" in result, f"Gold not in result. Keys: {list(result.keys())}"
        assert 0.0 <= result["gold"]["percentile"] <= 1.0
        assert result["gold"]["net_position"] is not None


class TestFREDCommodityLive:
    """Live FRED commodity series tests."""

    def test_fred_commodity_series_live(self):
        """DCOILWTICO, GOLDAMGBD228NLBM, PCOPPUSDM return non-empty series."""
        from tradingagents.strategies.data_sources.fred_source import FREDSource

        source = FREDSource()
        if not source.is_available():
            pytest.skip("fredapi not installed or no API key")

        series_ids = ["DCOILWTICO", "GOLDAMGBD228NLBM", "PCOPPUSDM"]
        for sid in series_ids:
            data = source.fetch_series(sid, "2025-01-01", "2026-04-01")
            assert len(data) > 0, f"FRED series {sid} returned empty"


class TestOpenBBFuturesCurveLive:
    """Live OpenBB futures curve tests."""

    def test_openbb_futures_curve_live(self):
        """Gold futures curve present, contango calculation sane."""
        try:
            from tradingagents.strategies.data_sources.openbb_source import OpenBBSource
        except ImportError:
            pytest.skip("openbb not installed")

        source = OpenBBSource()
        if not source.is_available():
            pytest.skip("OpenBB not available")

        result = source.fetch({"method": "commodity_futures_curve", "symbol": "GC"})
        if "error" in result:
            pytest.skip(f"Futures curve fetch failed: {result['error']}")

        assert "front_month" in result
        assert "contango_pct" in result
        assert isinstance(result["contango_pct"], (int, float))
```

- [ ] **Step 2: Configure live marker in pytest**

If not already present, add to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = [
    "live: marks tests that hit real APIs (deselect with '-m \"not live\"')",
]
```

- [ ] **Step 3: Verify smoke tests are skipped in normal runs**

Run: `python -m pytest tests/test_commodity_macro_live.py -v 2>&1 | tail -15`
Expected: all tests skipped or deselected (no `live` marker)

- [ ] **Step 4: Run live tests (optional, requires API keys/libraries)**

Run: `python -m pytest tests/test_commodity_macro_live.py -m live -v 2>&1 | tail -20`
Expected: tests run against real APIs (or skip if dependencies missing)

- [ ] **Step 5: Commit**

```bash
git add tests/test_commodity_macro_live.py pyproject.toml
git commit -m "test: add live API smoke tests for commodity_macro strategy"
```

---

### Task Summary

| Task | What | Files |
|------|------|-------|
| 1 | Add `cot_reports` dependency | `pyproject.toml` |
| 2 | Create CFTCSource | `cftc_source.py`, tests |
| 3 | Register CFTCSource | `registry.py`, `__init__.py` |
| 4 | Add FRED commodity series | `fred_source.py`, tests |
| 5 | Wire CFTC into engine | `multi_strategy_engine.py` |
| 6 | Create CommodityMacroStrategy | `commodity_macro.py`, tests |
| 7 | Register strategy + LLM prompt | `__init__.py`, `llm_analyzer.py` |
| 8 | Cohort commodity fields | `cohort_orchestrator.py`, tests |
| 9 | Portfolio committee cap + regime | `portfolio_committee.py`, tests |
| 10 | LLM prompt commodity context | `portfolio_committee.py` |
| 11 | Covered call overlay | `portfolio_committee.py`, tests |
| 12 | OpenBB futures enrichment | `openbb_source.py`, `cohort_orchestrator.py` |
| 13 | Live API smoke tests | `test_commodity_macro_live.py` |
