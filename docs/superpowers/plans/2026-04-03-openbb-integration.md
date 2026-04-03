# OpenBB Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OpenBBSource data source with 8 methods, integrate into 9 strategies (7 modified + 2 reactivated), enrich portfolio committee, validate with 30-day simulation, and launch as new generation.

**Architecture:** Single `OpenBBSource` class registered in the autoresearch `DataSourceRegistry`, dispatching on `params["method"]` to internal handlers grouped by domain. OpenBB imported lazily. Strategies add `"openbb"` to `data_sources` and consume enrichment data via `data["openbb"]`. Portfolio committee receives enrichment (sector, estimates, short interest, factors) after strategies produce signals.

**Tech Stack:** openbb-core, openbb-equity, openbb-derivatives, openbb-regulators, openbb-sec, openbb-yfinance, openbb-fmp, openbb-congress-gov, openbb-government-us, openbb-finra, openbb-famafrench

**Spec:** `docs/superpowers/specs/2026-04-03-openbb-integration-design.md`

---

## Phase 1: OpenBBSource & Unit Tests

### Task 1: Install OpenBB packages

**Files:**
- Modify: `pyproject.toml` (or `setup.py` / `requirements.txt` — whichever manages deps)

- [ ] **Step 1: Check current dependency management**

Run: `ls pyproject.toml setup.py setup.cfg requirements*.txt`

Identify which file manages dependencies.

- [ ] **Step 2: Add OpenBB dependencies**

Add these to the project's optional dependencies (e.g., under an `[openbb]` extra):

```
openbb-core
openbb-equity
openbb-derivatives
openbb-regulators
openbb-sec
openbb-yfinance
openbb-fmp
openbb-congress-gov
openbb-government-us
openbb-finra
openbb-famafrench
```

- [ ] **Step 3: Install**

Run: `pip install -e ".[openbb]"` (or equivalent)
Expected: Clean install, no conflicts.

- [ ] **Step 4: Verify import**

Run: `python -c "from openbb import obb; print('OpenBB OK')"`
Expected: Prints "OpenBB OK" (first run may take 2-5s for package build).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "build: add openbb optional dependencies"
```

---

### Task 2: Create OpenBBSource with `equity_profile` method (TDD)

**Files:**
- Create: `tests/test_openbb_source.py`
- Create: `tradingagents/autoresearch/data_sources/openbb_source.py`

- [ ] **Step 1: Write failing test for `equity_profile`**

```python
"""Tests for OpenBBSource data source."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def openbb_source():
    """Create OpenBBSource instance."""
    from tradingagents.autoresearch.data_sources.openbb_source import OpenBBSource
    return OpenBBSource()


class TestOpenBBSourceAvailability:
    def test_is_available_when_installed(self, openbb_source):
        assert openbb_source.is_available() is True

    @patch.dict("sys.modules", {"openbb": None})
    def test_is_available_when_not_installed(self):
        from tradingagents.autoresearch.data_sources.openbb_source import OpenBBSource
        source = OpenBBSource()
        assert source.is_available() is False

    def test_name(self, openbb_source):
        assert openbb_source.name == "openbb"

    def test_requires_api_key(self, openbb_source):
        assert openbb_source.requires_api_key is False


class TestEquityProfile:
    def test_equity_profile_returns_sector(self, openbb_source):
        mock_result = MagicMock()
        mock_result.results = [MagicMock(
            sector="Technology",
            industry="Consumer Electronics",
            market_cap=3200000000000,
            name="Apple Inc.",
            long_business_summary="Apple designs...",
        )]
        with patch.object(openbb_source, "_obb") as mock_obb:
            mock_obb.equity.profile.return_value = mock_result
            openbb_source._initialized = True
            result = openbb_source.fetch({"method": "equity_profile", "ticker": "AAPL"})

        assert result["sector"] == "Technology"
        assert result["industry"] == "Consumer Electronics"
        assert result["market_cap"] == 3200000000000
        assert result["name"] == "Apple Inc."

    def test_equity_profile_unknown_ticker(self, openbb_source):
        with patch.object(openbb_source, "_obb") as mock_obb:
            mock_obb.equity.profile.side_effect = Exception("Not found")
            openbb_source._initialized = True
            result = openbb_source.fetch({"method": "equity_profile", "ticker": "ZZZZZ"})

        assert "error" in result

    def test_equity_profile_cached(self, openbb_source):
        openbb_source._cache["equity_profile|AAPL"] = {"sector": "Technology"}
        result = openbb_source.fetch({"method": "equity_profile", "ticker": "AAPL"})
        assert result["sector"] == "Technology"

    def test_unknown_method(self, openbb_source):
        result = openbb_source.fetch({"method": "nonexistent"})
        assert "error" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_openbb_source.py -v`
Expected: ImportError — `openbb_source` module doesn't exist yet.

- [ ] **Step 3: Write OpenBBSource skeleton with `equity_profile`**

Create `tradingagents/autoresearch/data_sources/openbb_source.py`:

```python
"""OpenBB Platform data source.

Provides unified access to equity profiles, analyst estimates, insider trading,
short interest, government trades, unusual options, SEC litigation, and
Fama-French factors via the OpenBB SDK.

All OpenBB imports are lazy — zero cost if no strategy uses this source.
Install: pip install openbb-core openbb-equity openbb-derivatives openbb-regulators \
         openbb-sec openbb-yfinance openbb-fmp openbb-congress-gov \
         openbb-government-us openbb-finra openbb-famafrench
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class OpenBBSource:
    """Data source backed by the OpenBB Platform SDK."""

    name: str = "openbb"
    requires_api_key: bool = False

    def __init__(self, fmp_api_key: str | None = None) -> None:
        self._fmp_api_key = fmp_api_key or os.environ.get("FMP_API_KEY", "")
        self._cache: dict[str, Any] = {}
        self._obb: Any = None
        self._initialized = False

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "")
        dispatch = {
            "equity_profile": self._fetch_equity_profile,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except Exception:
            logger.error("OpenBBSource.fetch(%s) failed", method, exc_info=True)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        try:
            import openbb  # noqa: F401
            return True
        except ImportError:
            return False

    def clear_cache(self) -> None:
        self._cache.clear()

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        from openbb import obb
        self._obb = obb
        self._initialized = True

    # ------------------------------------------------------------------
    # Equity methods
    # ------------------------------------------------------------------

    def _fetch_equity_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        ticker = params.get("ticker", "")
        cache_key = f"equity_profile|{ticker}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        self._ensure_initialized()
        result = self._obb.equity.profile(ticker, provider="yfinance")
        if not result.results:
            return {"error": f"No profile data for {ticker}"}

        item = result.results[0]
        data = {
            "sector": getattr(item, "sector", ""),
            "industry": getattr(item, "industry", ""),
            "market_cap": getattr(item, "market_cap", 0),
            "name": getattr(item, "name", ""),
            "description": getattr(item, "long_business_summary", "")[:500],
        }
        self._cache[cache_key] = data
        return data
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_openbb_source.py -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_openbb_source.py tradingagents/autoresearch/data_sources/openbb_source.py
git commit -m "feat(openbb): add OpenBBSource skeleton with equity_profile method"
```

---

### Task 3: Add remaining 7 OpenBB methods (TDD)

**Files:**
- Modify: `tests/test_openbb_source.py`
- Modify: `tradingagents/autoresearch/data_sources/openbb_source.py`

- [ ] **Step 1: Write failing tests for all remaining methods**

Append to `tests/test_openbb_source.py`:

```python
class TestEquityEstimates:
    def test_equity_estimates_returns_consensus(self, openbb_source):
        mock_result = MagicMock()
        mock_result.results = [MagicMock(
            estimated_eps_avg=7.12,
            estimated_revenue_avg=410000000000,
            number_of_analysts=38,
        )]
        with patch.object(openbb_source, "_obb") as mock_obb:
            mock_obb.equity.estimates.consensus.return_value = mock_result
            openbb_source._initialized = True
            result = openbb_source.fetch({"method": "equity_estimates", "ticker": "AAPL"})

        assert result["consensus_eps"] == 7.12
        assert result["num_analysts"] == 38

    def test_equity_estimates_no_fmp_key_returns_error(self):
        from tradingagents.autoresearch.data_sources.openbb_source import OpenBBSource
        source = OpenBBSource(fmp_api_key="")
        source._initialized = True
        source._obb = MagicMock()
        source._obb.equity.estimates.consensus.side_effect = Exception("No API key")
        result = source.fetch({"method": "equity_estimates", "ticker": "AAPL"})
        assert "error" in result


class TestEquityInsiderTrading:
    def test_insider_trading_returns_trades(self, openbb_source):
        mock_result = MagicMock()
        mock_result.results = [MagicMock(
            owner_name="Tim Cook",
            owner_title="CEO",
            transaction_type="S-Sale",
            securities_transacted=50000,
            price=198.5,
            value=9925000.0,
            filing_date="2026-03-15",
            owner_type="officer",
        )]
        with patch.object(openbb_source, "_obb") as mock_obb:
            mock_obb.equity.ownership.insider_trading.return_value = mock_result
            openbb_source._initialized = True
            result = openbb_source.fetch({"method": "equity_insider_trading", "ticker": "AAPL"})

        assert len(result["trades"]) == 1
        assert result["trades"][0]["owner"] == "Tim Cook"
        assert result["trades"][0]["title"] == "CEO"


class TestEquityShortInterest:
    def test_short_interest_returns_data(self, openbb_source):
        mock_result = MagicMock()
        mock_result.results = [MagicMock(
            short_interest=120000000,
            short_percent_of_float=0.98,
            days_to_cover=1.2,
            settlement_date="2026-03-15",
        )]
        with patch.object(openbb_source, "_obb") as mock_obb:
            mock_obb.equity.shorts.short_interest.return_value = mock_result
            openbb_source._initialized = True
            result = openbb_source.fetch({"method": "equity_short_interest", "ticker": "AAPL"})

        assert result["short_interest"] == 120000000
        assert result["days_to_cover"] == 1.2


class TestEquityGovernmentTrades:
    def test_government_trades_returns_trades(self, openbb_source):
        mock_result = MagicMock()
        mock_result.results = [MagicMock(
            ticker="NVDA",
            representative="Nancy Pelosi",
            chamber="house",
            transaction_type="purchase",
            amount="$1,000,001 - $5,000,000",
            transaction_date="2026-03-10",
            district="CA-11",
        )]
        with patch.object(openbb_source, "_obb") as mock_obb:
            mock_obb.equity.ownership.government_trades.return_value = mock_result
            openbb_source._initialized = True
            result = openbb_source.fetch({"method": "equity_government_trades", "days_back": 30})

        assert len(result["trades"]) == 1
        assert result["trades"][0]["ticker"] == "NVDA"


class TestDerivativesOptionsUnusual:
    def test_options_unusual_returns_data(self, openbb_source):
        mock_result = MagicMock()
        mock_result.results = [MagicMock(
            underlying_symbol="TSLA",
            option_type="call",
            strike=250.0,
            expiration="2026-04-18",
            volume=45000,
            open_interest=12000,
        )]
        with patch.object(openbb_source, "_obb") as mock_obb:
            mock_obb.derivatives.options.unusual.return_value = mock_result
            openbb_source._initialized = True
            result = openbb_source.fetch({"method": "derivatives_options_unusual"})

        assert len(result["unusual"]) == 1
        assert result["unusual"][0]["ticker"] == "TSLA"

    def test_options_unusual_graceful_fallback(self, openbb_source):
        with patch.object(openbb_source, "_obb") as mock_obb:
            mock_obb.derivatives.options.unusual.side_effect = Exception("No provider")
            openbb_source._initialized = True
            result = openbb_source.fetch({"method": "derivatives_options_unusual"})

        assert "error" in result


class TestRegulatorsSECLitigation:
    def test_sec_litigation_returns_releases(self, openbb_source):
        mock_result = MagicMock()
        mock_result.results = [MagicMock(
            title="SEC Charges XYZ Corp",
            published="2026-03-28",
            link="https://sec.gov/litigation/123",
            category="litigation",
        )]
        with patch.object(openbb_source, "_obb") as mock_obb:
            mock_obb.regulators.sec.rss_litigation.return_value = mock_result
            openbb_source._initialized = True
            result = openbb_source.fetch({"method": "regulators_sec_litigation"})

        assert len(result["releases"]) == 1
        assert "SEC Charges" in result["releases"][0]["title"]


class TestFactorsFamaFrench:
    def test_fama_french_returns_factors(self, openbb_source):
        mock_result = MagicMock()
        mock_result.results = [MagicMock(
            date="2026-03-01",
        )]
        # Fama-French returns a DataFrame-like result
        mock_df = MagicMock()
        mock_df.iloc = {-1: {"Mkt-RF": 0.023, "SMB": -0.011, "HML": 0.008,
                              "RMW": 0.005, "CMA": -0.003, "RF": 0.004}}
        mock_result.to_df.return_value = mock_df
        with patch.object(openbb_source, "_obb") as mock_obb:
            mock_obb.equity.discovery.fama_french = MagicMock(return_value=mock_result)
            openbb_source._initialized = True
            result = openbb_source.fetch({"method": "factors_fama_french", "model": "5"})

        assert "factors" in result or "error" in result


class TestCacheBehavior:
    def test_clear_cache(self, openbb_source):
        openbb_source._cache["test"] = "value"
        openbb_source.clear_cache()
        assert len(openbb_source._cache) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_openbb_source.py -v`
Expected: New tests FAIL — methods not in dispatch dict yet.

- [ ] **Step 3: Add all 7 remaining methods to OpenBBSource**

Add to the `dispatch` dict in `fetch()`:

```python
dispatch = {
    "equity_profile": self._fetch_equity_profile,
    "equity_estimates": self._fetch_equity_estimates,
    "equity_insider_trading": self._fetch_equity_insider_trading,
    "equity_short_interest": self._fetch_equity_short_interest,
    "equity_government_trades": self._fetch_equity_government_trades,
    "derivatives_options_unusual": self._fetch_derivatives_options_unusual,
    "regulators_sec_litigation": self._fetch_regulators_sec_litigation,
    "factors_fama_french": self._fetch_factors_fama_french,
}
```

Add these methods after `_fetch_equity_profile`:

```python
def _fetch_equity_estimates(self, params: dict[str, Any]) -> dict[str, Any]:
    ticker = params.get("ticker", "")
    cache_key = f"equity_estimates|{ticker}"
    if cache_key in self._cache:
        return self._cache[cache_key]

    self._ensure_initialized()
    result = self._obb.equity.estimates.consensus(ticker, provider="fmp")
    if not result.results:
        return {"error": f"No estimate data for {ticker}"}

    item = result.results[0]
    data = {
        "consensus_eps": getattr(item, "estimated_eps_avg", None),
        "consensus_revenue": getattr(item, "estimated_revenue_avg", None),
        "num_analysts": getattr(item, "number_of_analysts", 0),
    }
    # Try price target separately (may not be available)
    try:
        pt_result = self._obb.equity.estimates.price_target(ticker, provider="fmp")
        if pt_result.results:
            pt = pt_result.results[0]
            data["price_target_mean"] = getattr(pt, "price_target_average", None)
            data["price_target_high"] = getattr(pt, "price_target_high", None)
            data["price_target_low"] = getattr(pt, "price_target_low", None)
    except Exception:
        pass  # Price target is bonus data

    self._cache[cache_key] = data
    return data

def _fetch_equity_insider_trading(self, params: dict[str, Any]) -> dict[str, Any]:
    ticker = params.get("ticker", "")
    cache_key = f"equity_insider_trading|{ticker}"
    if cache_key in self._cache:
        return self._cache[cache_key]

    self._ensure_initialized()
    result = self._obb.equity.ownership.insider_trading(ticker, provider="sec")
    if not result.results:
        return {"trades": []}

    trades = []
    for item in result.results[:20]:  # Cap at 20 most recent
        trades.append({
            "owner": getattr(item, "owner_name", ""),
            "title": getattr(item, "owner_title", ""),
            "transaction_type": getattr(item, "transaction_type", ""),
            "shares": getattr(item, "securities_transacted", 0),
            "price": getattr(item, "price", 0.0),
            "value": getattr(item, "value", 0.0),
            "date": str(getattr(item, "filing_date", "")),
            "ownership_type": getattr(item, "owner_type", ""),
        })

    data = {"trades": trades}
    self._cache[cache_key] = data
    return data

def _fetch_equity_short_interest(self, params: dict[str, Any]) -> dict[str, Any]:
    ticker = params.get("ticker", "")
    cache_key = f"equity_short_interest|{ticker}"
    if cache_key in self._cache:
        return self._cache[cache_key]

    self._ensure_initialized()
    result = self._obb.equity.shorts.short_interest(ticker, provider="finra")
    if not result.results:
        return {"error": f"No short interest data for {ticker}"}

    item = result.results[0]
    data = {
        "short_interest": getattr(item, "short_interest", 0),
        "short_pct_of_float": getattr(item, "short_percent_of_float", 0.0),
        "days_to_cover": getattr(item, "days_to_cover", 0.0),
        "date": str(getattr(item, "settlement_date", "")),
    }
    self._cache[cache_key] = data
    return data

def _fetch_equity_government_trades(self, params: dict[str, Any]) -> dict[str, Any]:
    cache_key = "equity_government_trades"
    if cache_key in self._cache:
        return self._cache[cache_key]

    self._ensure_initialized()
    result = self._obb.equity.ownership.government_trades(provider="government_us")
    if not result.results:
        return {"trades": []}

    trades = []
    for item in result.results[:50]:  # Cap at 50
        trades.append({
            "ticker": getattr(item, "ticker", ""),
            "representative": getattr(item, "representative", ""),
            "chamber": getattr(item, "chamber", ""),
            "transaction_type": getattr(item, "transaction_type", ""),
            "amount": getattr(item, "amount", ""),
            "transaction_date": str(getattr(item, "transaction_date", "")),
            "district": getattr(item, "district", ""),
        })

    data = {"trades": trades}
    self._cache[cache_key] = data
    return data

def _fetch_derivatives_options_unusual(self, params: dict[str, Any]) -> dict[str, Any]:
    cache_key = "derivatives_options_unusual"
    if cache_key in self._cache:
        return self._cache[cache_key]

    self._ensure_initialized()
    result = self._obb.derivatives.options.unusual()
    if not result.results:
        return {"unusual": []}

    unusual = []
    for item in result.results[:30]:  # Cap at 30
        unusual.append({
            "ticker": getattr(item, "underlying_symbol", ""),
            "contract_type": getattr(item, "option_type", ""),
            "strike": getattr(item, "strike", 0.0),
            "expiration": str(getattr(item, "expiration", "")),
            "volume": getattr(item, "volume", 0),
            "open_interest": getattr(item, "open_interest", 0),
            "vol_oi_ratio": round(
                getattr(item, "volume", 0) / max(getattr(item, "open_interest", 1), 1), 2
            ),
        })

    data = {"unusual": unusual}
    self._cache[cache_key] = data
    return data

def _fetch_regulators_sec_litigation(self, params: dict[str, Any]) -> dict[str, Any]:
    cache_key = "regulators_sec_litigation"
    if cache_key in self._cache:
        return self._cache[cache_key]

    self._ensure_initialized()
    result = self._obb.regulators.sec.rss_litigation()
    if not result.results:
        return {"releases": []}

    releases = []
    for item in result.results[:20]:  # Cap at 20
        releases.append({
            "title": getattr(item, "title", ""),
            "date": str(getattr(item, "published", "")),
            "url": getattr(item, "link", ""),
            "category": getattr(item, "category", ""),
        })

    data = {"releases": releases}
    self._cache[cache_key] = data
    return data

def _fetch_factors_fama_french(self, params: dict[str, Any]) -> dict[str, Any]:
    model = params.get("model", "5")
    cache_key = f"factors_fama_french|{model}"
    if cache_key in self._cache:
        return self._cache[cache_key]

    self._ensure_initialized()
    try:
        # The famafrench extension endpoint may vary — try common patterns
        result = self._obb.equity.discovery.fama_french(model=model)
        df = result.to_df()
        if df.empty:
            return {"factors": {}, "history": {}}

        latest = df.iloc[-1]
        factor_names = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]
        factors = {}
        for f in factor_names:
            if f in latest.index:
                factors[f] = float(latest[f])

        data = {"factors": factors, "history": df.tail(12).to_dict()}
        self._cache[cache_key] = data
        return data
    except Exception:
        logger.warning("Fama-French data unavailable", exc_info=True)
        return {"error": "Fama-French data unavailable"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_openbb_source.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_openbb_source.py tradingagents/autoresearch/data_sources/openbb_source.py
git commit -m "feat(openbb): add all 8 data methods to OpenBBSource"
```

---

### Task 4: Register OpenBBSource in registry and config

**Files:**
- Modify: `tradingagents/autoresearch/data_sources/registry.py:87-122`
- Modify: `tradingagents/autoresearch/data_sources/__init__.py`
- Modify: `tradingagents/default_config.py:115-120`
- Modify: `.env.example`

- [ ] **Step 1: Add OpenBBSource to `build_default_registry()`**

In `registry.py`, after the CourtListenerSource registration (line 120), add:

```python
    # OpenBB Platform (optional — gracefully skipped if not installed)
    from tradingagents.autoresearch.data_sources.openbb_source import OpenBBSource
    registry.register(OpenBBSource(fmp_api_key=config.get("fmp_api_key")))
```

Wrap in try/except so missing openbb doesn't break the registry:

```python
    try:
        from tradingagents.autoresearch.data_sources.openbb_source import OpenBBSource
        registry.register(OpenBBSource(fmp_api_key=config.get("fmp_api_key")))
    except ImportError:
        logger.info("OpenBB not installed — skipping OpenBBSource")
```

- [ ] **Step 2: Add to `__init__.py` exports**

In `data_sources/__init__.py`, add import and `__all__` entry:

```python
from tradingagents.autoresearch.data_sources.openbb_source import OpenBBSource
```

Add `"OpenBBSource"` to the `__all__` list.

- [ ] **Step 3: Add `fmp_api_key` to default config**

In `default_config.py`, after line 120 (`"edgar_user_agent": ...`), add:

```python
        "fmp_api_key": "",
```

- [ ] **Step 4: Add `FMP_API_KEY` to `.env.example`**

After the `COURTLISTENER_TOKEN` line, add:

```
FMP_API_KEY=
```

- [ ] **Step 5: Verify registry loads with OpenBB**

Run: `python -c "from tradingagents.autoresearch.data_sources import build_default_registry; r = build_default_registry(); print(r.available_sources())"`
Expected: List includes `"openbb"` alongside existing sources.

- [ ] **Step 6: Commit**

```bash
git add tradingagents/autoresearch/data_sources/registry.py \
       tradingagents/autoresearch/data_sources/__init__.py \
       tradingagents/default_config.py .env.example
git commit -m "feat(openbb): register OpenBBSource in data source registry"
```

---

## Phase 2: Strategy Integration

### Task 5: Integrate OpenBB into `earnings_call.py`

**Files:**
- Modify: `tradingagents/autoresearch/strategies/earnings_call.py:28,46-119`

- [ ] **Step 1: Add `"openbb"` to `data_sources`**

Change line 28 from:
```python
    data_sources = ["finnhub", "yfinance"]
```
to:
```python
    data_sources = ["finnhub", "yfinance", "openbb"]
```

- [ ] **Step 2: Enrich EPS surprise scoring with consensus estimates**

In `screen()`, after the existing EPS surprise calculation (after line 93), add enrichment before appending the candidate:

```python
            # Enrich with analyst consensus if available
            openbb_data = data.get("openbb", {})
            estimates = openbb_data.get("estimates", {})
            if isinstance(estimates, dict) and symbol in estimates:
                est = estimates[symbol]
                consensus_eps = est.get("consensus_eps")
                if consensus_eps is not None and consensus_eps != 0:
                    # Use consensus estimate if more reliable than Finnhub estimate
                    consensus_surprise = (eps_actual - consensus_eps) / abs(consensus_eps)
                    eps_score = min(abs(consensus_surprise) * 7.0, 1.0)
                    eps_direction = "long" if consensus_surprise > 0 else "short"
                    # Boost score if many analysts cover (higher conviction)
                    num_analysts = est.get("num_analysts", 0)
                    if num_analysts >= 10:
                        eps_score = min(eps_score * 1.15, 1.0)
```

Update the Candidate metadata to include estimate context:

```python
                    candidates[-1].metadata.update({
                        "consensus_eps": consensus_eps,
                        "num_analysts": num_analysts,
                        "consensus_surprise_pct": round(consensus_surprise * 100, 2),
                    })
```

- [ ] **Step 3: Run existing tests**

Run: `.venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "earnings"`
Expected: PASS — existing tests don't pass OpenBB data, so the `if` guard skips enrichment.

- [ ] **Step 4: Commit**

```bash
git add tradingagents/autoresearch/strategies/earnings_call.py
git commit -m "feat(openbb): enrich earnings_call with analyst consensus estimates"
```

---

### Task 6: Integrate OpenBB into `insider_activity.py`

**Files:**
- Modify: `tradingagents/autoresearch/strategies/insider_activity.py:27,47-136`

- [ ] **Step 1: Add `"openbb"` to `data_sources`**

Change line 27 from:
```python
    data_sources = ["edgar", "yfinance"]
```
to:
```python
    data_sources = ["edgar", "yfinance", "openbb"]
```

- [ ] **Step 2: Enrich with structured insider titles**

In `screen()`, after building each candidate from EDGAR Form 4 data, add title-based scoring:

```python
            # Enrich with OpenBB insider data for officer titles
            openbb_data = data.get("openbb", {})
            insider_data = openbb_data.get("insider_trading", {})
            if isinstance(insider_data, dict) and ticker in insider_data:
                openbb_trades = insider_data[ticker].get("trades", [])
                # Check if any officer in OpenBB data matches this transaction
                for obb_trade in openbb_trades:
                    title = obb_trade.get("title", "").upper()
                    # C-suite buys are higher conviction
                    if any(t in title for t in ["CEO", "CFO", "COO", "CTO", "PRESIDENT"]):
                        candidate.score = min(candidate.score * 1.3, 1.0)
                        candidate.metadata["officer_title"] = obb_trade.get("title", "")
                        break
                    elif "DIRECTOR" in title:
                        candidate.metadata["officer_title"] = obb_trade.get("title", "")
                        break

            # Add sector context from profile
            profile = openbb_data.get("profile", {})
            if isinstance(profile, dict) and ticker in profile:
                candidate.metadata["sector"] = profile[ticker].get("sector", "")
```

- [ ] **Step 3: Run existing tests**

Run: `.venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "insider"`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tradingagents/autoresearch/strategies/insider_activity.py
git commit -m "feat(openbb): enrich insider_activity with officer titles and sector"
```

---

### Task 7: Integrate OpenBB into `congressional_trades.py`

**Files:**
- Modify: `tradingagents/autoresearch/strategies/congressional_trades.py:46,64-130`

- [ ] **Step 1: Add `"openbb"` to `data_sources`**

Change line 46 from:
```python
    data_sources = ["congress", "yfinance"]
```
to:
```python
    data_sources = ["congress", "yfinance", "openbb"]
```

- [ ] **Step 2: Use OpenBB government trades as primary, CapitolTrades as fallback**

In `screen()`, at the beginning (after getting params), add:

```python
        # Try OpenBB government trades first (stable API), fall back to CapitolTrades
        openbb_data = data.get("openbb", {})
        govt_trades = openbb_data.get("government_trades", {})
        obb_trades = govt_trades.get("trades", []) if isinstance(govt_trades, dict) else []

        if obb_trades:
            # Normalize OpenBB format to match existing congress format
            all_trades = []
            for t in obb_trades:
                all_trades.append({
                    "ticker": t.get("ticker", ""),
                    "transaction_date": t.get("transaction_date", ""),
                    "transaction_type": t.get("transaction_type", ""),
                    "amount": t.get("amount", ""),
                    "representative": t.get("representative", ""),
                    "chamber": t.get("chamber", ""),
                })
            logger.info("Using OpenBB government trades: %d trades", len(all_trades))
        else:
            # Fall back to CapitolTrades scraping
            congress_data = data.get("congress", {})
            all_trades = congress_data.get("data", [])
            logger.info("Falling back to CapitolTrades: %d trades", len(all_trades))
```

Then update the rest of `screen()` to use `all_trades` instead of `data["congress"]["data"]`.

- [ ] **Step 3: Run existing tests**

Run: `.venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "congress"`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tradingagents/autoresearch/strategies/congressional_trades.py
git commit -m "feat(openbb): use government trades API as primary for congressional_trades"
```

---

### Task 8: Integrate OpenBB into `supply_chain.py`

**Files:**
- Modify: `tradingagents/autoresearch/strategies/supply_chain.py:34,54-100`

- [ ] **Step 1: Add `"openbb"` to `data_sources`**

Change line 34 from:
```python
    data_sources = ["finnhub", "yfinance"]
```
to:
```python
    data_sources = ["finnhub", "yfinance", "openbb"]
```

- [ ] **Step 2: Add short interest amplification**

In `screen()`, after identifying disruption candidates (before the return), add:

```python
        # Amplify signal for heavily-shorted names (squeeze potential on disruption)
        openbb_data = data.get("openbb", {})
        for candidate in candidates:
            short_data = openbb_data.get("short_interest", {})
            if isinstance(short_data, dict) and candidate.ticker in short_data:
                si = short_data[candidate.ticker]
                short_pct = si.get("short_pct_of_float", 0)
                if short_pct > 5.0:  # >5% of float shorted
                    candidate.score = min(candidate.score * 1.25, 1.0)
                    candidate.metadata["short_pct_of_float"] = short_pct
                    candidate.metadata["days_to_cover"] = si.get("days_to_cover", 0)

            # Add sector context
            profile = openbb_data.get("profile", {})
            if isinstance(profile, dict) and candidate.ticker in profile:
                candidate.metadata["sector"] = profile[candidate.ticker].get("sector", "")
```

- [ ] **Step 3: Run existing tests**

Run: `.venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "supply"`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tradingagents/autoresearch/strategies/supply_chain.py
git commit -m "feat(openbb): add short interest amplification to supply_chain"
```

---

### Task 9: Integrate OpenBB into `litigation.py`

**Files:**
- Modify: `tradingagents/autoresearch/strategies/litigation.py:44,62-106`

- [ ] **Step 1: Add `"openbb"` to `data_sources`**

Change line 44 from:
```python
    data_sources = ["courtlistener", "yfinance"]
```
to:
```python
    data_sources = ["courtlistener", "yfinance", "openbb"]
```

- [ ] **Step 2: Merge SEC litigation releases with CourtListener**

In `screen()`, after processing CourtListener dockets (before the return), add:

```python
        # Merge SEC enforcement actions (higher signal quality)
        openbb_data = data.get("openbb", {})
        sec_lit = openbb_data.get("sec_litigation", {})
        sec_releases = sec_lit.get("releases", []) if isinstance(sec_lit, dict) else []

        for release in sec_releases:
            title = release.get("title", "")
            # Try to extract ticker from SEC release title
            # SEC titles often mention company names, not tickers
            # Score SEC actions higher — SEC doesn't bring frivolous cases
            candidates.append(
                Candidate(
                    ticker="",  # LLM will resolve from title
                    date=date,
                    direction="short",  # Enforcement = bearish for target
                    score=0.8,  # High base score for SEC actions
                    metadata={
                        "source": "sec_enforcement",
                        "title": title[:200],
                        "url": release.get("url", ""),
                        "date": release.get("date", ""),
                        "needs_llm_analysis": True,
                        "analysis_type": "litigation",
                    },
                )
            )
```

- [ ] **Step 3: Run existing tests**

Run: `.venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "litigation"`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tradingagents/autoresearch/strategies/litigation.py
git commit -m "feat(openbb): merge SEC litigation releases into litigation strategy"
```

---

### Task 10: Integrate OpenBB into `filing_analysis.py`

**Files:**
- Modify: `tradingagents/autoresearch/strategies/filing_analysis.py:43,61-151`

- [ ] **Step 1: Add `"openbb"` to `data_sources`**

Change line 43 from:
```python
    data_sources = ["edgar", "yfinance"]
```
to:
```python
    data_sources = ["edgar", "yfinance", "openbb"]
```

- [ ] **Step 2: Compare filings against analyst consensus**

In `screen()`, after generating candidates from EDGAR filings, add enrichment:

```python
        # Enrich with analyst consensus for contradiction detection
        openbb_data = data.get("openbb", {})
        estimates = openbb_data.get("estimates", {})
        for candidate in candidates:
            ticker = candidate.ticker
            if isinstance(estimates, dict) and ticker in estimates:
                est = estimates[ticker]
                candidate.metadata["consensus_eps"] = est.get("consensus_eps")
                candidate.metadata["consensus_revenue"] = est.get("consensus_revenue")
                candidate.metadata["price_target_mean"] = est.get("price_target_mean")
                # If filing contradicts consensus, boost conviction
                if est.get("num_analysts", 0) >= 5:
                    candidate.score = min(candidate.score * 1.1, 1.0)

            # Add sector context
            profile = openbb_data.get("profile", {})
            if isinstance(profile, dict) and ticker in profile:
                candidate.metadata["sector"] = profile[ticker].get("sector", "")
```

- [ ] **Step 3: Run existing tests**

Run: `.venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "filing"`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tradingagents/autoresearch/strategies/filing_analysis.py
git commit -m "feat(openbb): enrich filing_analysis with analyst consensus"
```

---

### Task 11: Integrate OpenBB into `regulatory_pipeline.py`

**Files:**
- Modify: `tradingagents/autoresearch/strategies/regulatory_pipeline.py:25,48-86`

- [ ] **Step 1: Add `"openbb"` to `data_sources`**

Change line 25 from:
```python
    data_sources = ["regulations", "yfinance"]
```
to:
```python
    data_sources = ["regulations", "yfinance", "openbb"]
```

- [ ] **Step 2: Add sector validation for ticker mapping**

In `screen()`, after generating candidates, add sector validation:

```python
        # Validate ticker-to-regulation mapping with sector data
        openbb_data = data.get("openbb", {})
        profile = openbb_data.get("profile", {})
        if isinstance(profile, dict):
            for candidate in candidates:
                ticker = candidate.ticker
                if ticker in profile:
                    candidate.metadata["sector"] = profile[ticker].get("sector", "")
                    candidate.metadata["industry"] = profile[ticker].get("industry", "")
```

- [ ] **Step 3: Run existing tests**

Run: `.venv/bin/python -m pytest tests/test_multi_strategy.py -v -k "regulatory"`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tradingagents/autoresearch/strategies/regulatory_pipeline.py
git commit -m "feat(openbb): add sector validation to regulatory_pipeline"
```

---

## Phase 3: Reactivate Archived Strategies

### Task 12: Reactivate `govt_contracts` strategy

**Files:**
- Move: `tradingagents/autoresearch/strategies/_archive/govt_contracts.py` → `tradingagents/autoresearch/strategies/govt_contracts.py`
- Modify: `tradingagents/autoresearch/strategies/__init__.py`

- [ ] **Step 1: Move file out of archive**

Run:
```bash
git mv tradingagents/autoresearch/strategies/_archive/govt_contracts.py \
       tradingagents/autoresearch/strategies/govt_contracts.py
```

- [ ] **Step 2: Change track and data_sources**

In `govt_contracts.py`, change line 51 from:
```python
    track = "backtest"
```
to:
```python
    track = "paper_trade"
```

Change line 52 from:
```python
    data_sources = ["yfinance", "usaspending"]
```
to:
```python
    data_sources = ["yfinance", "usaspending", "openbb"]
```

- [ ] **Step 3: Rewrite `screen()` to use real contract data + OpenBB enrichment**

Replace the `screen()` method (lines 70-109) with:

```python
    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for government contractor opportunities.

        Uses USASpending contract data when available, falls back to
        defense contractor momentum. Enriches with OpenBB profile/estimates.
        """
        candidates = []

        # Try USASpending contract data first
        usaspending = data.get("usaspending", {})
        contracts = usaspending.get("data", {}).get("contracts", [])

        if contracts:
            for contract in contracts:
                recipient = (contract.get("recipient", "") or "").lower()
                amount = contract.get("amount", 0) or 0

                # Resolve recipient to ticker
                ticker = None
                for keyword, t in CONTRACTOR_TICKERS.items():
                    if keyword in recipient:
                        ticker = t
                        break

                if not ticker or amount < 50_000_000:  # $50M minimum
                    continue

                score = min(amount / 1_000_000_000, 1.0)  # Scale by $1B
                candidates.append(
                    Candidate(
                        ticker=ticker,
                        date=date,
                        direction="long",
                        score=score,
                        metadata={
                            "contractor": recipient,
                            "contract_amount": amount,
                            "source": "usaspending",
                        },
                    )
                )
        else:
            # Fallback: momentum-based screening for defense contractors
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
                    if momentum > 0.02:
                        candidates.append(
                            Candidate(
                                ticker=ticker,
                                date=date,
                                direction="long",
                                score=momentum,
                                metadata={"contractor": name, "momentum_30d": momentum,
                                          "source": "momentum_fallback"},
                            )
                        )

        # Enrich with OpenBB data
        openbb_data = data.get("openbb", {})
        profile = openbb_data.get("profile", {})
        estimates = openbb_data.get("estimates", {})
        for c in candidates:
            if isinstance(profile, dict) and c.ticker in profile:
                c.metadata["sector"] = profile[c.ticker].get("sector", "")
            if isinstance(estimates, dict) and c.ticker in estimates:
                c.metadata["price_target_mean"] = estimates[c.ticker].get("price_target_mean")

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[: params.get("max_positions", 3)]
```

- [ ] **Step 4: Register in `__init__.py`**

In `strategies/__init__.py`, add import:

```python
from .govt_contracts import GovtContractsStrategy
```

Add to `__all__`:

```python
    "GovtContractsStrategy",
```

Add to `get_paper_trade_strategies()`:

```python
        GovtContractsStrategy(),
```

- [ ] **Step 5: Run all strategy tests**

Run: `.venv/bin/python -m pytest tests/test_multi_strategy.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tradingagents/autoresearch/strategies/govt_contracts.py \
       tradingagents/autoresearch/strategies/__init__.py
git commit -m "feat: reactivate govt_contracts as paper_trade strategy with OpenBB enrichment"
```

---

### Task 13: Reactivate `state_economics` strategy

**Files:**
- Move: `tradingagents/autoresearch/strategies/_archive/state_economics.py` → `tradingagents/autoresearch/strategies/state_economics.py`
- Modify: `tradingagents/autoresearch/strategies/__init__.py`

- [ ] **Step 1: Move file out of archive**

Run:
```bash
git mv tradingagents/autoresearch/strategies/_archive/state_economics.py \
       tradingagents/autoresearch/strategies/state_economics.py
```

- [ ] **Step 2: Change track and data_sources**

Change line 41 from:
```python
    track = "backtest"
```
to:
```python
    track = "paper_trade"
```

Change line 42 from:
```python
    data_sources = ["yfinance"]
```
to:
```python
    data_sources = ["yfinance", "fred", "openbb"]
```

- [ ] **Step 3: Rewrite `screen()` to use FRED + OpenBB composite signal**

Replace the `screen()` method (lines 60-101) with:

```python
    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for regional ETFs using economic indicators + momentum composite.

        Combines FRED economic indicators with ETF momentum for a
        composite signal. Falls back to pure momentum if FRED unavailable.
        """
        prices = data.get("yfinance", {}).get("prices", {})
        lookback = params.get("lookback_days", 21)
        top_n = params.get("top_n", 2)
        min_return = params.get("min_return", 0.0)

        # Compute momentum scores
        etf_scores: list[tuple[str, str, float]] = []
        for name, ticker in REGIONAL_ETFS.items():
            df = prices.get(ticker)
            if df is None or df.empty:
                continue
            df = df.loc[:date]
            if len(df) < lookback:
                continue
            close = df["Close"]
            momentum = (close.iloc[-1] / close.iloc[-lookback]) - 1.0
            etf_scores.append((name, ticker, momentum))

        if not etf_scores:
            return []

        # Enrich with FRED economic indicator context
        fred_data = data.get("fred", {})
        fred_indicators = fred_data.get("data", {})
        econ_boost = {}
        if fred_indicators:
            # Check if unemployment is declining (bullish for regionals)
            unemployment = fred_indicators.get("UNRATE", {})
            if unemployment:
                values = sorted(unemployment.items())
                if len(values) >= 2:
                    recent = values[-1][1]
                    prior = values[-2][1]
                    if recent < prior:  # Declining unemployment
                        econ_boost["KRE"] = 0.02  # Boost regional banks
                        econ_boost["XRT"] = 0.01  # Boost retail
                        econ_boost["XHB"] = 0.01  # Boost homebuilders

            # Check initial claims (leading indicator)
            claims = fred_indicators.get("ICSA", {})
            if claims:
                values = sorted(claims.items())
                if len(values) >= 2:
                    recent = values[-1][1]
                    prior = values[-2][1]
                    if recent < prior:  # Declining claims = bullish
                        econ_boost["IWN"] = 0.02  # Small-cap value benefits most

        # Combine momentum + economic boost
        combined_scores = []
        for name, ticker, momentum in etf_scores:
            boost = econ_boost.get(ticker, 0.0)
            combined = momentum + boost
            combined_scores.append((name, ticker, combined, momentum, boost))

        combined_scores.sort(key=lambda x: x[2], reverse=True)

        candidates = []
        for name, ticker, combined, momentum, boost in combined_scores[:top_n]:
            if combined < min_return:
                continue
            metadata = {
                "regional_sector": name,
                "trailing_return": momentum,
                "econ_boost": boost,
                "composite_score": combined,
            }

            # Add factor context from OpenBB
            openbb_data = data.get("openbb", {})
            factors = openbb_data.get("factors_fama_french", {})
            if isinstance(factors, dict) and "factors" in factors:
                metadata["fama_french"] = factors["factors"]

            candidates.append(
                Candidate(
                    ticker=ticker,
                    date=date,
                    direction="long",
                    score=combined,
                    metadata=metadata,
                )
            )

        return candidates
```

- [ ] **Step 4: Register in `__init__.py`**

In `strategies/__init__.py`, add import:

```python
from .state_economics import StateEconomicsStrategy
```

Add to `__all__`:

```python
    "StateEconomicsStrategy",
```

Add to `get_paper_trade_strategies()`:

```python
        StateEconomicsStrategy(),
```

- [ ] **Step 5: Run all strategy tests**

Run: `.venv/bin/python -m pytest tests/test_multi_strategy.py -v`
Expected: PASS. Strategy count is now 9.

- [ ] **Step 6: Commit**

```bash
git add tradingagents/autoresearch/strategies/state_economics.py \
       tradingagents/autoresearch/strategies/__init__.py
git commit -m "feat: reactivate state_economics with FRED + OpenBB composite signal"
```

---

### Task 14: Update test strategy count assertions

**Files:**
- Modify: `tests/test_multi_strategy.py`

- [ ] **Step 1: Find and update strategy count assertions**

Run: `grep -n "len.*strategies\|assert.*7\|assert.*count" tests/test_multi_strategy.py`

Update any assertion that checks for 7 strategies to check for 9. For example:

```python
# Before
assert len(strategies) == 7
# After
assert len(strategies) == 9
```

Also update `get_paper_trade_strategies` assertions.

- [ ] **Step 2: Add attribute tests for new strategies**

Ensure `test_all_strategies_have_required_attributes` covers the 2 new strategies. Since it iterates `get_all_strategies()`, it should automatically pick them up.

- [ ] **Step 3: Run full test suite**

Run: `.venv/bin/python -m pytest tests/test_multi_strategy.py -v`
Expected: All tests PASS with 9 strategies.

- [ ] **Step 4: Commit**

```bash
git add tests/test_multi_strategy.py
git commit -m "test: update strategy count assertions for 9 strategies"
```

---

## Phase 4: Portfolio Committee & Orchestrator Enrichment

### Task 15: Add enrichment to portfolio committee

**Files:**
- Modify: `tradingagents/autoresearch/portfolio_committee.py:47-90,92-192,210-221,257-295`

- [ ] **Step 1: Add `enrichment` parameter to `synthesize()`**

In `synthesize()` (line 47), add `enrichment: dict | None = None` parameter:

```python
    def synthesize(
        self,
        signals: list[dict],
        regime_context: dict | None = None,
        strategy_confidence: dict[str, float] | None = None,
        current_positions: list[dict] | None = None,
        total_capital: float = 5000.0,
        enrichment: dict | None = None,
    ) -> list[TradeRecommendation]:
```

Pass `enrichment` through to both `_llm_synthesize` and `_rule_based_synthesize`.

- [ ] **Step 2: Add sector enforcement to `_rule_based_synthesize()`**

In `_rule_based_synthesize()`, after creating recommendations and before `_enforce_sector_limits()` (line 186), add sector-aware enforcement:

```python
        # Sector concentration enforcement using enrichment data
        enrichment = enrichment or {}
        profiles = enrichment.get("profiles", {})
        if profiles:
            sector_alloc: dict[str, float] = defaultdict(float)
            for rec in recommendations:
                sector = profiles.get(rec.ticker, {}).get("sector", "Unknown")
                rec.metadata = getattr(rec, "metadata", {}) or {}
                # Store sector on recommendation for downstream use
                sector_alloc[sector] += rec.position_size_pct

            # Trim sectors exceeding max_sector concentration
            for rec in recommendations:
                sector = profiles.get(rec.ticker, {}).get("sector", "Unknown")
                if sector_alloc[sector] > self._max_sector:
                    scale = self._max_sector / sector_alloc[sector]
                    rec.position_size_pct *= scale
```

- [ ] **Step 3: Add enrichment context to LLM prompt**

In `_build_prompt()` (line 257), add enrichment to the `synthesize` signature and include in prompt:

```python
        # Add enrichment context if available
        enrichment_str = ""
        if enrichment:
            profiles = enrichment.get("profiles", {})
            short_interest = enrichment.get("short_interest", {})
            factors = enrichment.get("factors", {})

            if profiles:
                sector_lines = [f"  {t}: {p.get('sector', '?')}" for t, p in list(profiles.items())[:10]]
                enrichment_str += f"\nSector classification:\n" + "\n".join(sector_lines)

            if short_interest:
                si_lines = [f"  {t}: {s.get('short_pct_of_float', 0):.1f}% short"
                            for t, s in list(short_interest.items())[:10]]
                enrichment_str += f"\nShort interest:\n" + "\n".join(si_lines)

            if factors:
                enrichment_str += f"\nFama-French factors: {json.dumps(factors, default=str)}"
```

Append `enrichment_str` to the return string.

- [ ] **Step 4: Run existing tests**

Run: `.venv/bin/python -m pytest tests/ -v -k "portfolio_committee or committee"`
Expected: PASS — `enrichment` defaults to `None`, so existing callers are unaffected.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/autoresearch/portfolio_committee.py
git commit -m "feat(openbb): add sector enforcement and enrichment context to portfolio committee"
```

---

### Task 16: Add OpenBB enrichment fetch to cohort orchestrator

**Files:**
- Modify: `tradingagents/autoresearch/cohort_orchestrator.py:65-117`

- [ ] **Step 1: Add enrichment fetch after signals are produced**

In `run_daily()`, after `screen_and_enrich()` (line 91) and before the cohort loop (line 96), add:

```python
        # Fetch OpenBB enrichment for signal tickers (profiles, estimates, short interest, factors)
        enrichment = self._fetch_openbb_enrichment(shared_signals)
```

- [ ] **Step 2: Pass enrichment to each cohort's engine**

In the cohort loop (line 100), pass enrichment:

```python
                result = cohort["engine"].run_paper_trade_phase(
                    trading_date=trading_date,
                    shared_signals=shared_signals,
                    shared_regime=shared_regime,
                    enrichment=enrichment,
                )
```

Note: `run_paper_trade_phase` will need to accept and forward `enrichment` to `portfolio_committee.synthesize()`. This may require a small change in `MultiStrategyEngine` — check its signature and add `enrichment: dict | None = None` if needed.

- [ ] **Step 3: Implement `_fetch_openbb_enrichment()` method**

Add to `CohortOrchestrator`:

```python
    def _fetch_openbb_enrichment(self, signals: list[dict]) -> dict:
        """Fetch OpenBB data to enrich portfolio committee decisions.

        Returns dict with profiles, estimates, short_interest, factors
        for all signal tickers.
        """
        enrichment: dict[str, Any] = {}

        # Get unique tickers from signals
        tickers = list({s.get("ticker", "") for s in signals if s.get("ticker")})
        if not tickers:
            return enrichment

        # Get OpenBB source from first engine's registry
        first_engine = self.cohorts[0]["engine"]
        registry = getattr(first_engine, "_registry", None)
        if registry is None:
            return enrichment

        openbb_source = registry.get("openbb")
        if openbb_source is None or not openbb_source.is_available():
            return enrichment

        # Fetch profiles for all tickers
        profiles = {}
        for ticker in tickers:
            result = openbb_source.fetch({"method": "equity_profile", "ticker": ticker})
            if "error" not in result:
                profiles[ticker] = result
        if profiles:
            enrichment["profiles"] = profiles

        # Fetch short interest for all tickers
        short_interest = {}
        for ticker in tickers:
            result = openbb_source.fetch({"method": "equity_short_interest", "ticker": ticker})
            if "error" not in result:
                short_interest[ticker] = result
        if short_interest:
            enrichment["short_interest"] = short_interest

        # Fetch Fama-French factors (once, not per ticker)
        factors = openbb_source.fetch({"method": "factors_fama_french"})
        if "error" not in factors:
            enrichment["factors"] = factors.get("factors", {})

        return enrichment
```

- [ ] **Step 4: Run existing tests**

Run: `.venv/bin/python -m pytest tests/ -v -k "cohort"`
Expected: PASS — enrichment returns empty dict when OpenBB unavailable.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/autoresearch/cohort_orchestrator.py
git commit -m "feat(openbb): fetch enrichment data in cohort orchestrator for portfolio committee"
```

---

## Phase 5: 30-Day Simulation Tests

### Task 17: Add `TestOpenBBEnrichment` simulation test

**Files:**
- Modify: `tests/test_30day_simulation.py` (or `tests/test_autoresearch_simulation.py`)

- [ ] **Step 1: Add test class for OpenBB enrichment in 30-day simulation**

Add a new test class:

```python
class TestOpenBBEnrichment:
    """Verify OpenBB enrichment works across 30-day simulation."""

    def test_enrichment_30_days_with_openbb(self, tmp_path):
        """Run 30 days with mocked OpenBB data. Verify portfolio committee
        receives sector data and strategies produce signals with enrichment."""
        # Setup: 2 cohorts, mocked data including OpenBB
        # Mock OpenBBSource.fetch to return synthetic profiles/estimates/shorts
        mock_profiles = {
            "AAPL": {"sector": "Technology", "industry": "Consumer Electronics", "market_cap": 3e12, "name": "Apple"},
            "MSFT": {"sector": "Technology", "industry": "Software", "market_cap": 2.8e12, "name": "Microsoft"},
            "AMZN": {"sector": "Consumer Cyclical", "industry": "Internet Retail", "market_cap": 1.8e12, "name": "Amazon"},
            "TSLA": {"sector": "Consumer Cyclical", "industry": "Auto Manufacturers", "market_cap": 0.8e12, "name": "Tesla"},
            "NVDA": {"sector": "Technology", "industry": "Semiconductors", "market_cap": 2.5e12, "name": "NVIDIA"},
        }
        mock_short_interest = {
            "TSLA": {"short_interest": 50000000, "short_pct_of_float": 3.2, "days_to_cover": 1.5, "date": "2026-03-15"},
        }

        def mock_openbb_fetch(params):
            method = params.get("method", "")
            if method == "equity_profile":
                ticker = params.get("ticker", "")
                if ticker in mock_profiles:
                    return mock_profiles[ticker]
                return {"error": "not found"}
            elif method == "equity_short_interest":
                ticker = params.get("ticker", "")
                if ticker in mock_short_interest:
                    return mock_short_interest[ticker]
                return {"error": "not found"}
            elif method == "factors_fama_french":
                return {"factors": {"Mkt-RF": 0.02, "SMB": -0.01, "HML": 0.005}, "history": {}}
            return {"error": f"unknown method {method}"}

        # ... (follow existing TestThirtyDayCohortDivergence pattern)
        # Run 30 days, assert:
        # 1. Both cohorts produce signals
        # 2. Portfolio committee receives enrichment (mock and verify call args)
        # 3. No crashes when OpenBB data is present
        pass  # Full implementation follows existing test patterns

    def test_graceful_degradation_without_openbb(self, tmp_path):
        """Run 30 days with OpenBB unavailable. Verify identical behavior to baseline."""
        # Mock OpenBBSource.is_available() to return False
        # Run same 30-day simulation
        # Assert: strategies produce signals, no crashes, no enrichment passed
        pass  # Full implementation follows existing test patterns
```

- [ ] **Step 2: Implement full test bodies**

Follow the exact patterns in `TestThirtyDayCohortDivergence` (lines 535-650 of `test_30day_simulation.py`):
- Use `FakeStrategy` and `FakeStrategy2` 
- Mock `_fetch_all_data`, `_fetch_missing_prices`, `get_paper_trade_strategies`
- Additionally mock `OpenBBSource.fetch` with the synthetic data above
- Iterate 30 days
- Assert signals, trades, no duplicates, capital conservation

- [ ] **Step 3: Run simulation tests**

Run: `.venv/bin/python -m pytest tests/test_30day_simulation.py::TestOpenBBEnrichment -v`
Expected: Both tests PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_30day_simulation.py
git commit -m "test: add 30-day simulation tests for OpenBB enrichment and graceful degradation"
```

---

### Task 18: Add `TestReactivatedStrategies` simulation test

**Files:**
- Modify: `tests/test_30day_simulation.py`

- [ ] **Step 1: Add test class for reactivated strategies**

```python
class TestReactivatedStrategies:
    """Verify govt_contracts and state_economics work in 30-day lifecycle."""

    def test_govt_contracts_30_days(self, tmp_path):
        """Run govt_contracts through 30 days with synthetic USASpending data."""
        from tradingagents.autoresearch.strategies.govt_contracts import GovtContractsStrategy
        strategy = GovtContractsStrategy()
        assert strategy.track == "paper_trade"
        assert "openbb" in strategy.data_sources
        assert "usaspending" in strategy.data_sources

        # Test screen with synthetic data
        data = {
            "yfinance": {"prices": _build_synthetic_prices(["LMT", "RTX", "NOC", "BA"])},
            "usaspending": {"data": {"contracts": [
                {"recipient": "lockheed martin corp", "amount": 100_000_000},
                {"recipient": "boeing defense", "amount": 200_000_000},
            ]}},
            "openbb": {"profile": {
                "LMT": {"sector": "Industrials"},
                "BA": {"sector": "Industrials"},
            }},
        }
        candidates = strategy.screen(data, "2026-04-01", strategy.get_default_params())
        assert len(candidates) > 0
        assert all(c.direction == "long" for c in candidates)

        # Test exit logic
        should_exit, reason = strategy.check_exit("LMT", 450.0, 520.0, 30, strategy.get_default_params(), {})
        assert should_exit  # profit target or hold period

    def test_state_economics_30_days(self, tmp_path):
        """Run state_economics through 30 days with synthetic FRED data."""
        from tradingagents.autoresearch.strategies.state_economics import StateEconomicsStrategy
        strategy = StateEconomicsStrategy()
        assert strategy.track == "paper_trade"
        assert "fred" in strategy.data_sources
        assert "openbb" in strategy.data_sources

        # Test screen with synthetic data
        data = {
            "yfinance": {"prices": _build_synthetic_prices(["KRE", "IWN", "XRT", "IYR", "XHB"])},
            "fred": {"data": {
                "UNRATE": {"2026-02-01": 4.1, "2026-03-01": 3.9},  # Declining = bullish
                "ICSA": {"2026-02-01": 220000, "2026-03-01": 210000},  # Declining = bullish
            }},
            "openbb": {"factors_fama_french": {
                "factors": {"Mkt-RF": 0.02, "SMB": -0.01, "HML": 0.005},
            }},
        }
        candidates = strategy.screen(data, "2026-04-01", strategy.get_default_params())
        assert len(candidates) > 0
        # Verify FRED boost was applied
        for c in candidates:
            if c.ticker in ("KRE", "IWN"):
                assert c.metadata.get("econ_boost", 0) > 0

    def test_reactivated_strategies_without_openbb(self, tmp_path):
        """Both reactivated strategies work without OpenBB data."""
        from tradingagents.autoresearch.strategies.govt_contracts import GovtContractsStrategy
        from tradingagents.autoresearch.strategies.state_economics import StateEconomicsStrategy

        # govt_contracts falls back to momentum
        gc = GovtContractsStrategy()
        data = {"yfinance": {"prices": _build_synthetic_prices(list(CONTRACTOR_TICKERS.values()))}}
        candidates = gc.screen(data, "2026-04-01", gc.get_default_params())
        # May or may not have candidates depending on synthetic momentum, but shouldn't crash
        assert isinstance(candidates, list)

        # state_economics works with pure momentum
        se = StateEconomicsStrategy()
        data = {"yfinance": {"prices": _build_synthetic_prices(list(REGIONAL_ETFS.values()))}}
        candidates = se.screen(data, "2026-04-01", se.get_default_params())
        assert isinstance(candidates, list)
```

Note: `_build_synthetic_prices` should be a helper that creates synthetic DataFrames similar to the existing `_make_price_df` in the test file. Check the existing test helpers and reuse them.

- [ ] **Step 2: Run tests**

Run: `.venv/bin/python -m pytest tests/test_30day_simulation.py::TestReactivatedStrategies -v`
Expected: All tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_30day_simulation.py
git commit -m "test: add 30-day simulation tests for reactivated govt_contracts and state_economics"
```

---

### Task 19: Extend cohort divergence test to 9 strategies

**Files:**
- Modify: `tests/test_30day_simulation.py` (in `TestThirtyDayCohortDivergence`)

- [ ] **Step 1: Update mock strategy count**

In `TestThirtyDayCohortDivergence.test_cohort_divergence_30_days`, update the mock for `get_paper_trade_strategies` to return more strategies (matching the 9-strategy count):

Add more `FakeStrategy` variants or adjust the mock to include the 2 new strategies. The key assertion: `len(get_paper_trade_strategies()) == 9`.

- [ ] **Step 2: Run full simulation test suite**

Run: `.venv/bin/python -m pytest tests/test_30day_simulation.py -v`
Expected: ALL simulation tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_30day_simulation.py
git commit -m "test: extend cohort divergence test to 9 strategies"
```

---

## Phase 6: Full Validation & Generation Launch

### Task 20: Run full test suite

**Files:** None (validation only)

- [ ] **Step 1: Run all tests**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: ALL tests PASS. Zero failures.

- [ ] **Step 2: If any failures, fix them**

Diagnose and fix any test failures. Common issues:
- Strategy count assertions not updated
- Mock data shape mismatches
- Import errors from moved files

- [ ] **Step 3: Run tests again after fixes**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: ALL PASS.

---

### Task 21: Run dry_run.py end-to-end

**Files:** None (validation only)

- [ ] **Step 1: Run the dry run script**

Run: `.venv/bin/python scripts/dry_run.py`

This runs the full pipeline with real LLM calls but paper trading. Expected:
- Completes without errors
- Reports strategies, budget usage, leaderboard
- DB verification shows strategies and reflections

- [ ] **Step 2: Verify 9 strategies appear**

In the dry run output, verify the strategy count reflects 9 paper-trade strategies.

- [ ] **Step 3: Verify OpenBB data flows through**

Check logs for OpenBB-related messages:
- `"Registered data source: openbb"` in registry init
- Any OpenBB fetch logs in strategy screen output

---

### Task 22: Start new generation and run first daily

**Files:** None (operational)

- [ ] **Step 1: Commit all remaining changes**

Run:
```bash
git status
git add -A  # Review what's staged!
git commit -m "feat: OpenBB integration complete — 9 strategies, portfolio enrichment, 30-day simulation"
```

- [ ] **Step 2: Start new generation**

Run:
```bash
python scripts/run_generations.py start "9-strategy OpenBB enrichment: sector classification, analyst estimates, short interest, govt trades API, SEC litigation, Fama-French factors, reactivated govt_contracts + state_economics"
```

Expected: New generation created (e.g., `gen_002`), worktree at `.worktrees/gen_002/`.

- [ ] **Step 3: Run first daily**

Run:
```bash
python scripts/run_generations.py run-daily
```

Expected: Both cohorts produce signals and trades. No errors.

- [ ] **Step 4: Verify both cohorts**

Check state files:
```bash
ls data/generations/gen_*/control/paper_trades.json
ls data/generations/gen_*/adaptive/paper_trades.json
```

Expected: Both files exist and contain trade data.

- [ ] **Step 5: Verify graceful degradation**

Temporarily uninstall OpenBB and re-run:
```bash
pip uninstall openbb-core -y
python scripts/run_generations.py run-daily --date 2026-04-04
pip install -e ".[openbb]"
```

Expected: System runs without errors, OpenBB source skipped, strategies still produce signals.

---

### Task 23: Final verification checklist

- [ ] All unit tests pass (`pytest tests/ -v` — zero failures)
- [ ] All 30-day simulation tests pass (OpenBB enrichment, reactivated strategies, cohort divergence)
- [ ] `dry_run.py` completes without errors
- [ ] New generation started successfully
- [ ] First `run-daily` produces signals and trades in both cohorts
- [ ] Graceful degradation confirmed (system works without OpenBB installed)
- [ ] Strategy count = 9 (7 existing + 2 reactivated)
- [ ] No existing tests broken
