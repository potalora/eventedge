# Agricultural Strategy Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the `weather_ag` strategy with USDA crop conditions, US Drought Monitor data, expanded ticker universe, year-round operation, and LLM-driven signal scoring.

**Architecture:** Two new data source modules (`usda_source.py`, `drought_monitor_source.py`) feed alongside existing `noaa_source.py` into a rewritten `weather_ag` strategy. The strategy uses rule-based gate filtering, then delegates scoring to `LLMAnalyzer` via the `needs_llm_analysis` pattern used by all other strategies.

**Tech Stack:** Python 3.11+, requests, pandas, pytest, Anthropic API (Haiku)

**Spec:** `docs/superpowers/specs/2026-04-03-ag-strategy-enhancement-design.md`

---

### Task 1: USDA NASS Data Source

**Files:**
- Create: `tradingagents/autoresearch/data_sources/usda_source.py`
- Test: `tests/test_usda_source.py`

- [ ] **Step 1: Write failing tests for USDASource**

Create `tests/test_usda_source.py`:

```python
"""Tests for USDA NASS QuickStats data source.

All API calls are mocked — no real requests.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def source():
    from tradingagents.autoresearch.data_sources.usda_source import USDASource
    return USDASource(api_key="test-key-123")


@pytest.fixture()
def source_no_key():
    from tradingagents.autoresearch.data_sources.usda_source import USDASource
    return USDASource(api_key="")


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_name(self, source):
        assert source.name == "usda"

    def test_requires_api_key(self, source):
        assert source.requires_api_key is True

    def test_is_available_with_key(self, source):
        assert source.is_available() is True

    def test_is_available_without_key(self, source_no_key):
        assert source_no_key.is_available() is False

    def test_unknown_method_returns_error(self, source):
        result = source.fetch({"method": "nonexistent"})
        assert "error" in result

    def test_datasource_protocol(self, source):
        from tradingagents.autoresearch.data_sources.registry import DataSource
        assert isinstance(source, DataSource)


# ---------------------------------------------------------------------------
# fetch_crop_progress
# ---------------------------------------------------------------------------

MOCK_NASS_RESPONSE = {
    "data": [
        {
            "commodity_desc": "CORN",
            "state_alpha": "IA",
            "week_ending": "2025-06-15",
            "unit_desc": "PCT EXCELLENT",
            "Value": "21",
        },
        {
            "commodity_desc": "CORN",
            "state_alpha": "IA",
            "week_ending": "2025-06-15",
            "unit_desc": "PCT GOOD",
            "Value": "44",
        },
        {
            "commodity_desc": "CORN",
            "state_alpha": "IA",
            "week_ending": "2025-06-15",
            "unit_desc": "PCT FAIR",
            "Value": "22",
        },
        {
            "commodity_desc": "CORN",
            "state_alpha": "IA",
            "week_ending": "2025-06-15",
            "unit_desc": "PCT POOR",
            "Value": "9",
        },
        {
            "commodity_desc": "CORN",
            "state_alpha": "IA",
            "week_ending": "2025-06-15",
            "unit_desc": "PCT VERY POOR",
            "Value": "4",
        },
    ]
}


class TestFetchCropProgress:
    def test_parses_condition_ratings(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_NASS_RESPONSE

        with patch("requests.get", return_value=mock_resp) as mock_get:
            result = source.fetch_crop_progress("CORN", 2025)

        assert len(result) == 1
        week = result[0]
        assert week["commodity"] == "CORN"
        assert week["state"] == "IA"
        assert week["week_ending"] == "2025-06-15"
        assert week["excellent_pct"] == 21
        assert week["good_pct"] == 44
        assert week["fair_pct"] == 22
        assert week["poor_pct"] == 9
        assert week["very_poor_pct"] == 4

        # Verify API called with correct params
        call_kwargs = mock_get.call_args
        assert call_kwargs[1]["params"]["commodity_desc"] == "CORN"
        assert call_kwargs[1]["params"]["key"] == "test-key-123"

    def test_caches_by_commodity_year(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_NASS_RESPONSE

        with patch("requests.get", return_value=mock_resp) as mock_get:
            result1 = source.fetch_crop_progress("CORN", 2025)
            result2 = source.fetch_crop_progress("CORN", 2025)

        assert mock_get.call_count == 1
        assert result1 == result2

    def test_different_commodity_not_cached(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_NASS_RESPONSE

        with patch("requests.get", return_value=mock_resp) as mock_get:
            source.fetch_crop_progress("CORN", 2025)
            source.fetch_crop_progress("SOYBEANS", 2025)

        assert mock_get.call_count == 2

    def test_graceful_degradation_on_api_failure(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("requests.get", return_value=mock_resp):
            result = source.fetch_crop_progress("CORN", 2025)

        assert result == []

    def test_graceful_degradation_on_network_error(self, source):
        import requests as req
        with patch("requests.get", side_effect=req.RequestException("timeout")):
            result = source.fetch_crop_progress("CORN", 2025)

        assert result == []

    def test_handles_missing_value_field(self, source):
        response = {"data": [{
            "commodity_desc": "CORN",
            "state_alpha": "IA",
            "week_ending": "2025-06-15",
            "unit_desc": "PCT EXCELLENT",
            "Value": " (D)",
        }]}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = response

        with patch("requests.get", return_value=mock_resp):
            result = source.fetch_crop_progress("CORN", 2025)

        # Non-numeric values should be skipped gracefully
        assert isinstance(result, list)

    def test_no_key_returns_empty(self, source_no_key):
        result = source_no_key.fetch_crop_progress("CORN", 2025)
        assert result == []


# ---------------------------------------------------------------------------
# fetch dispatch
# ---------------------------------------------------------------------------

class TestFetchDispatch:
    def test_dispatch_crop_progress(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_NASS_RESPONSE

        with patch("requests.get", return_value=mock_resp):
            result = source.fetch({
                "method": "crop_progress",
                "commodity": "CORN",
                "year": 2025,
            })

        assert "weeks" in result
        assert len(result["weeks"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_usda_source.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradingagents.autoresearch.data_sources.usda_source'`

- [ ] **Step 3: Implement USDASource**

Create `tradingagents/autoresearch/data_sources/usda_source.py`:

```python
"""USDA NASS QuickStats data source.

Provides weekly crop condition ratings (Excellent/Good/Fair/Poor/Very Poor)
for corn, soybeans, and wheat across key agricultural states.

API docs: https://quickstats.nass.usda.gov/api/
Rate limits: 50,000 records per request. No explicit throttle.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://quickstats.nass.usda.gov/api/api_GET/"

# Key agricultural states (Corn Belt + Plains)
AG_STATES = "IA,IL,KS,NE,MN,IN,OH,SD,ND,MO"

# Condition rating categories in NASS data
CONDITION_CATEGORIES = {
    "PCT EXCELLENT": "excellent_pct",
    "PCT GOOD": "good_pct",
    "PCT FAIR": "fair_pct",
    "PCT POOR": "poor_pct",
    "PCT VERY POOR": "very_poor_pct",
}


class USDASource:
    """Data source backed by USDA NASS QuickStats API."""

    name: str = "usda"
    requires_api_key: bool = True

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("USDA_NASS_API_KEY", "")
        self._cache: dict[str, list[dict]] = {}

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "crop_progress")
        dispatch = {
            "crop_progress": self._dispatch_crop_progress,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except Exception:
            logger.error("USDASource.fetch(%s) failed", method, exc_info=True)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        return bool(self._api_key)

    def fetch_crop_progress(
        self,
        commodity: str,
        year: int,
        states: str | None = None,
    ) -> list[dict]:
        """Fetch weekly crop condition ratings from NASS.

        Args:
            commodity: CORN, SOYBEANS, or WHEAT.
            year: Calendar year.
            states: Comma-separated state alpha codes (default: AG_STATES).

        Returns:
            List of weekly snapshots with condition percentages per state.
            Each dict has: week_ending, commodity, state, excellent_pct,
            good_pct, fair_pct, poor_pct, very_poor_pct.
        """
        if not self._api_key:
            return []

        cache_key = f"{commodity}|{year}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        params = {
            "key": self._api_key,
            "commodity_desc": commodity.upper(),
            "statisticcat_desc": "CONDITION",
            "unit_desc": "PCT OF AREA PLANTED",
            "freq_desc": "WEEKLY",
            "year": str(year),
            "state_alpha": states or AG_STATES,
            "format": "JSON",
        }

        try:
            resp = requests.get(BASE_URL, params=params, timeout=30)
            if resp.status_code != 200:
                logger.warning("USDA NASS returned %d for %s/%d", resp.status_code, commodity, year)
                return []
            data = resp.json()
        except requests.RequestException:
            logger.error("USDA NASS request failed", exc_info=True)
            return []

        # Group records by (week_ending, state) and pivot condition categories
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for record in data.get("data", []):
            week = record.get("week_ending", "")
            state = record.get("state_alpha", "")
            unit = record.get("unit_desc", "")
            value_str = record.get("Value", "")

            field = CONDITION_CATEGORIES.get(unit)
            if not field or not week or not state:
                continue

            try:
                value = int(value_str.strip())
            except (ValueError, AttributeError):
                continue

            key = (week, state)
            if key not in grouped:
                grouped[key] = {
                    "week_ending": week,
                    "commodity": commodity.upper(),
                    "state": state,
                    "excellent_pct": 0,
                    "good_pct": 0,
                    "fair_pct": 0,
                    "poor_pct": 0,
                    "very_poor_pct": 0,
                }
            grouped[key][field] = value

        weeks = sorted(grouped.values(), key=lambda w: (w["week_ending"], w["state"]))
        self._cache[cache_key] = weeks
        return weeks

    def clear_cache(self) -> None:
        self._cache.clear()

    def _dispatch_crop_progress(self, params: dict[str, Any]) -> dict[str, Any]:
        commodity = params.get("commodity", "CORN")
        year = params.get("year", 2025)
        states = params.get("states")
        weeks = self.fetch_crop_progress(commodity, year, states)
        return {"weeks": weeks, "count": len(weeks)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_usda_source.py -v`
Expected: All 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/autoresearch/data_sources/usda_source.py tests/test_usda_source.py
git commit -m "feat: add USDA NASS QuickStats data source with crop condition ratings"
```

---

### Task 2: US Drought Monitor Data Source

**Files:**
- Create: `tradingagents/autoresearch/data_sources/drought_monitor_source.py`
- Test: `tests/test_drought_monitor_source.py`

- [ ] **Step 1: Write failing tests for DroughtMonitorSource**

Create `tests/test_drought_monitor_source.py`:

```python
"""Tests for US Drought Monitor data source.

All API calls are mocked — no real requests.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def source():
    from tradingagents.autoresearch.data_sources.drought_monitor_source import DroughtMonitorSource
    return DroughtMonitorSource()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_name(self, source):
        assert source.name == "drought_monitor"

    def test_requires_api_key(self, source):
        assert source.requires_api_key is False

    def test_is_available(self, source):
        assert source.is_available() is True

    def test_unknown_method_returns_error(self, source):
        result = source.fetch({"method": "nonexistent"})
        assert "error" in result

    def test_datasource_protocol(self, source):
        from tradingagents.autoresearch.data_sources.registry import DataSource
        assert isinstance(source, DataSource)


# ---------------------------------------------------------------------------
# fetch_drought_severity
# ---------------------------------------------------------------------------

MOCK_DROUGHT_RESPONSE = [
    {
        "MapDate": "20250610",
        "StatisticFormatID": 1,
        "StateAbbreviation": "IA",
        "None": 45.2,
        "D0": 20.1,
        "D1": 15.3,
        "D2": 10.5,
        "D3": 6.2,
        "D4": 2.7,
    },
    {
        "MapDate": "20250610",
        "StatisticFormatID": 1,
        "StateAbbreviation": "IL",
        "None": 60.0,
        "D0": 18.0,
        "D1": 12.0,
        "D2": 7.0,
        "D3": 2.5,
        "D4": 0.5,
    },
]


class TestFetchDroughtSeverity:
    def test_parses_state_drought_data(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_DROUGHT_RESPONSE

        with patch("requests.get", return_value=mock_resp):
            result = source.fetch_drought_severity(["IA", "IL"], "2025-06-03", "2025-06-10")

        assert "IA" in result
        assert result["IA"]["D0"] == 20.1
        assert result["IA"]["D2"] == 10.5
        assert result["IA"]["D4"] == 2.7
        assert "IL" in result

    def test_graceful_degradation_on_failure(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("requests.get", return_value=mock_resp):
            result = source.fetch_drought_severity(["IA"], "2025-06-03", "2025-06-10")

        assert result == {}

    def test_graceful_degradation_on_network_error(self, source):
        import requests as req
        with patch("requests.get", side_effect=req.RequestException("timeout")):
            result = source.fetch_drought_severity(["IA"], "2025-06-03", "2025-06-10")

        assert result == {}


# ---------------------------------------------------------------------------
# fetch_composite_score
# ---------------------------------------------------------------------------

class TestFetchCompositeScore:
    def test_computes_weighted_score(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_DROUGHT_RESPONSE

        with patch("requests.get", return_value=mock_resp):
            score = source.fetch_composite_score(["IA", "IL"], "2025-06-10")

        # IA: (20.1*0 + 15.3*1 + 10.5*2 + 6.2*3 + 2.7*4) / 100 = 0.653
        # IL: (18.0*0 + 12.0*1 + 7.0*2 + 2.5*3 + 0.5*4) / 100 = 0.355
        # Average: (0.653 + 0.355) / 2 = 0.504
        assert 0.4 < score < 0.6

    def test_returns_zero_on_no_data(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []

        with patch("requests.get", return_value=mock_resp):
            score = source.fetch_composite_score(["IA"], "2025-06-10")

        assert score == 0.0

    def test_returns_zero_on_failure(self, source):
        import requests as req
        with patch("requests.get", side_effect=req.RequestException("fail")):
            score = source.fetch_composite_score(["IA"], "2025-06-10")

        assert score == 0.0


# ---------------------------------------------------------------------------
# fetch dispatch
# ---------------------------------------------------------------------------

class TestFetchDispatch:
    def test_dispatch_drought_severity(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_DROUGHT_RESPONSE

        with patch("requests.get", return_value=mock_resp):
            result = source.fetch({
                "method": "drought_severity",
                "states": ["IA", "IL"],
                "start": "2025-06-03",
                "end": "2025-06-10",
            })

        assert "states" in result
        assert "IA" in result["states"]

    def test_dispatch_composite_score(self, source):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = MOCK_DROUGHT_RESPONSE

        with patch("requests.get", return_value=mock_resp):
            result = source.fetch({
                "method": "composite_score",
                "states": ["IA", "IL"],
                "date": "2025-06-10",
            })

        assert "composite_score" in result
        assert isinstance(result["composite_score"], float)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_drought_monitor_source.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement DroughtMonitorSource**

Create `tradingagents/autoresearch/data_sources/drought_monitor_source.py`:

```python
"""US Drought Monitor data source.

Provides drought severity statistics by state from the
US Drought Monitor (USDM). No authentication required.

API docs: https://droughtmonitor.unl.edu/DmData/DataDownload/WebServiceInfo.aspx
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://usdmdataservices.unl.edu/api/StateStatistics/GetDroughtSeverityStatisticsByAreaPercent"

# Default agricultural states (same as NOAA/USDA sources)
DEFAULT_AG_STATES = ["IA", "IL", "KS", "NE", "MN", "IN", "OH", "SD", "ND", "MO"]


class DroughtMonitorSource:
    """Data source backed by US Drought Monitor API."""

    name: str = "drought_monitor"
    requires_api_key: bool = False

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "drought_severity")
        dispatch = {
            "drought_severity": self._dispatch_severity,
            "composite_score": self._dispatch_composite,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except Exception:
            logger.error("DroughtMonitorSource.fetch(%s) failed", method, exc_info=True)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        return True

    def fetch_drought_severity(
        self,
        states: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, dict[str, float]]:
        """Fetch drought category percentages for each state.

        Args:
            states: List of state abbreviations (default: DEFAULT_AG_STATES).
            start: Start date YYYY-MM-DD (default: 7 days ago).
            end: End date YYYY-MM-DD (default: today).

        Returns:
            Dict mapping state abbreviation to drought categories:
            {state: {"None": pct, "D0": pct, "D1": pct, "D2": pct, "D3": pct, "D4": pct}}
        """
        states = states or DEFAULT_AG_STATES
        if end is None:
            end = datetime.now().strftime("%Y-%m-%d")
        if start is None:
            start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")

        # Convert dates to API format (M/d/yyyy)
        start_fmt = datetime.strptime(start, "%Y-%m-%d").strftime("%-m/%-d/%Y")
        end_fmt = datetime.strptime(end, "%Y-%m-%d").strftime("%-m/%-d/%Y")

        params = {
            "aoi": ",".join(states),
            "startdate": start_fmt,
            "enddate": end_fmt,
            "statisticsType": 1,  # State-level
        }

        try:
            resp = requests.get(
                BASE_URL,
                params=params,
                headers={"Accept": "application/json"},
                timeout=30,
            )
            if resp.status_code != 200:
                logger.warning("Drought Monitor returned %d", resp.status_code)
                return {}
            data = resp.json()
        except requests.RequestException:
            logger.error("Drought Monitor request failed", exc_info=True)
            return {}

        result: dict[str, dict[str, float]] = {}
        for record in data:
            state = record.get("StateAbbreviation", "")
            if not state or state not in states:
                continue
            # Keep the latest record per state
            result[state] = {
                "None": record.get("None", 0.0),
                "D0": record.get("D0", 0.0),
                "D1": record.get("D1", 0.0),
                "D2": record.get("D2", 0.0),
                "D3": record.get("D3", 0.0),
                "D4": record.get("D4", 0.0),
            }

        return result

    def fetch_composite_score(
        self,
        states: list[str] | None = None,
        date: str | None = None,
    ) -> float:
        """Compute a single 0-4 weighted drought score across ag states.

        Score = average across states of:
            (D0*0 + D1*1 + D2*2 + D3*3 + D4*4) / 100

        0 = no drought, 4 = entire region in exceptional drought.

        Args:
            states: State abbreviations (default: DEFAULT_AG_STATES).
            date: Target date YYYY-MM-DD (default: today).

        Returns:
            Composite drought score (float, 0.0-4.0).
        """
        states = states or DEFAULT_AG_STATES
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        severity = self.fetch_drought_severity(states, start, date)

        if not severity:
            return 0.0

        scores = []
        for state_data in severity.values():
            state_score = (
                state_data.get("D0", 0) * 0
                + state_data.get("D1", 0) * 1
                + state_data.get("D2", 0) * 2
                + state_data.get("D3", 0) * 3
                + state_data.get("D4", 0) * 4
            ) / 100.0
            scores.append(state_score)

        return round(sum(scores) / len(scores), 3)

    def clear_cache(self) -> None:
        self._cache.clear()

    def _dispatch_severity(self, params: dict[str, Any]) -> dict[str, Any]:
        states = params.get("states", DEFAULT_AG_STATES)
        start = params.get("start")
        end = params.get("end")
        severity = self.fetch_drought_severity(states, start, end)
        return {"states": severity}

    def _dispatch_composite(self, params: dict[str, Any]) -> dict[str, Any]:
        states = params.get("states", DEFAULT_AG_STATES)
        date = params.get("date")
        score = self.fetch_composite_score(states, date)
        return {"composite_score": score}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_drought_monitor_source.py -v`
Expected: All 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/autoresearch/data_sources/drought_monitor_source.py tests/test_drought_monitor_source.py
git commit -m "feat: add US Drought Monitor data source with composite scoring"
```

---

### Task 3: Register New Data Sources

**Files:**
- Modify: `tradingagents/autoresearch/data_sources/registry.py:122-123` (add registrations)
- Modify: `tradingagents/autoresearch/data_sources/__init__.py` (add exports)

- [ ] **Step 1: Add USDA and DroughtMonitor to registry**

In `tradingagents/autoresearch/data_sources/registry.py`, add after the NOAA registration (line 123) and before the OpenBB block:

```python
    from tradingagents.autoresearch.data_sources.usda_source import USDASource
    registry.register(USDASource(api_key=config.get("usda_nass_api_key")))

    from tradingagents.autoresearch.data_sources.drought_monitor_source import DroughtMonitorSource
    registry.register(DroughtMonitorSource())
```

- [ ] **Step 2: Add exports to `__init__.py`**

In `tradingagents/autoresearch/data_sources/__init__.py`, add the imports:

```python
from tradingagents.autoresearch.data_sources.usda_source import USDASource
from tradingagents.autoresearch.data_sources.drought_monitor_source import DroughtMonitorSource
```

And add to `__all__`:

```python
    "USDASource",
    "DroughtMonitorSource",
```

- [ ] **Step 3: Run existing tests to verify no regressions**

Run: `.venv/bin/python -m pytest tests/test_multi_strategy.py -v`
Expected: All existing tests PASS

- [ ] **Step 4: Commit**

```bash
git add tradingagents/autoresearch/data_sources/registry.py tradingagents/autoresearch/data_sources/__init__.py
git commit -m "feat: register USDA and Drought Monitor in data source registry"
```

---

### Task 4: Rewrite WeatherAg Strategy

**Files:**
- Modify: `tradingagents/autoresearch/strategies/weather_ag.py`
- Test: `tests/test_weather_ag.py` (create)

- [ ] **Step 1: Write failing tests for enhanced WeatherAgStrategy**

Create `tests/test_weather_ag.py`:

```python
"""Tests for the enhanced WeatherAg strategy.

Covers: expanded tickers, year-round operation, gate logic,
LLM metadata bundling, seasonal ticker filtering, graceful degradation.
"""
from __future__ import annotations

import pandas as pd
import pytest

from tradingagents.autoresearch.strategies.weather_ag import (
    WeatherAgStrategy,
    AG_TICKERS_FULL,
    AG_TICKERS_WINTER,
)
from tradingagents.autoresearch.strategies.base import Candidate


@pytest.fixture()
def strategy():
    return WeatherAgStrategy()


@pytest.fixture()
def price_data():
    """Build mock price DataFrames for ag tickers."""
    dates = pd.bdate_range("2025-05-01", periods=30)
    prices = {}
    for ticker in AG_TICKERS_FULL.values():
        df = pd.DataFrame(
            {"Close": [100 + i * 0.5 for i in range(30)]},
            index=dates,
        )
        prices[ticker] = df
    return prices


def _make_data(
    prices: dict,
    noaa: dict | None = None,
    drought: dict | None = None,
    usda: dict | None = None,
) -> dict:
    """Build a data dict matching engine output."""
    data: dict = {"yfinance": {"prices": prices}}
    if noaa is not None:
        data["noaa"] = noaa
    if drought is not None:
        data["drought_monitor"] = drought
    if usda is not None:
        data["usda"] = usda
    return data


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_name(self, strategy):
        assert strategy.name == "weather_ag"

    def test_track(self, strategy):
        assert strategy.track == "paper_trade"

    def test_data_sources(self, strategy):
        assert "yfinance" in strategy.data_sources
        assert "noaa" in strategy.data_sources
        assert "usda" in strategy.data_sources
        assert "drought_monitor" in strategy.data_sources
        assert "openbb" in strategy.data_sources

    def test_param_space_has_new_params(self, strategy):
        space = strategy.get_param_space()
        assert "drought_min_score" in space
        assert "crop_decline_threshold" in space

    def test_default_params_has_new_params(self, strategy):
        defaults = strategy.get_default_params()
        assert "drought_min_score" in defaults
        assert "crop_decline_threshold" in defaults
        # Season params removed
        assert "season_start_month" not in defaults
        assert "season_end_month" not in defaults


# ---------------------------------------------------------------------------
# Expanded tickers
# ---------------------------------------------------------------------------

class TestTickerUniverse:
    def test_full_universe_has_10_tickers(self):
        assert len(AG_TICKERS_FULL) == 10

    def test_full_includes_etfs_and_stocks(self):
        assert "DBA" in AG_TICKERS_FULL.values()
        assert "ADM" in AG_TICKERS_FULL.values()
        assert "DE" in AG_TICKERS_FULL.values()
        assert "SOYB" in AG_TICKERS_FULL.values()

    def test_winter_subset_excludes_corn_soy(self):
        assert "corn" not in AG_TICKERS_WINTER
        assert "soyb" not in AG_TICKERS_WINTER
        assert "weat" in AG_TICKERS_WINTER
        assert "dba" in AG_TICKERS_WINTER


# ---------------------------------------------------------------------------
# Year-round operation
# ---------------------------------------------------------------------------

class TestYearRound:
    def test_growing_season_returns_candidates(self, strategy, price_data):
        data = _make_data(
            price_data,
            drought={"composite_score": 1.5, "states": {}},
        )
        result = strategy.screen(data, "2025-05-20", strategy.get_default_params())
        assert isinstance(result, list)
        assert len(result) > 0

    def test_winter_returns_candidates_with_drought(self, strategy, price_data):
        data = _make_data(
            price_data,
            drought={"composite_score": 1.5, "states": {}},
        )
        result = strategy.screen(data, "2025-01-15", strategy.get_default_params())
        assert isinstance(result, list)
        # Should only have winter-eligible tickers
        for c in result:
            assert c.ticker in {AG_TICKERS_FULL[k] for k in AG_TICKERS_WINTER}

    def test_winter_excludes_corn_soy_tickers(self, strategy, price_data):
        data = _make_data(
            price_data,
            drought={"composite_score": 2.0, "states": {}},
        )
        result = strategy.screen(data, "2025-12-15", strategy.get_default_params())
        summer_only = {"CORN", "SOYB", "CTVA", "FMC", "DE"}
        for c in result:
            assert c.ticker not in summer_only


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------

class TestGateLogic:
    def test_no_signals_when_nothing_interesting(self, strategy, price_data):
        """All data present but below thresholds → empty."""
        data = _make_data(
            price_data,
            noaa={"heat_stress_days": 0, "precip_deficit_pct": 0, "frost_events": 0},
            drought={"composite_score": 0.3, "states": {}},
            usda={"crop_progress": {"CORN": []}},
        )
        # Also need returns below 5%
        flat_prices = {}
        dates = pd.bdate_range("2025-05-01", periods=30)
        for ticker in AG_TICKERS_FULL.values():
            flat_prices[ticker] = pd.DataFrame({"Close": [100.0] * 30}, index=dates)
        data["yfinance"]["prices"] = flat_prices

        result = strategy.screen(data, "2025-05-20", strategy.get_default_params())
        assert result == []

    def test_drought_gate_triggers(self, strategy, price_data):
        data = _make_data(
            price_data,
            drought={"composite_score": 1.5, "states": {}},
        )
        result = strategy.screen(data, "2025-06-15", strategy.get_default_params())
        assert len(result) > 0

    def test_momentum_gate_triggers(self, strategy):
        """High momentum alone should trigger gate."""
        dates = pd.bdate_range("2025-06-01", periods=30)
        prices = {}
        for ticker in AG_TICKERS_FULL.values():
            # 10% return over period
            prices[ticker] = pd.DataFrame(
                {"Close": [100 + i * 1.0 for i in range(30)]},
                index=dates,
            )
        data = _make_data(prices)
        result = strategy.screen(data, "2025-07-10", strategy.get_default_params())
        assert len(result) > 0


# ---------------------------------------------------------------------------
# LLM metadata bundling
# ---------------------------------------------------------------------------

class TestLLMMetadata:
    def test_candidates_have_llm_flags(self, strategy, price_data):
        data = _make_data(
            price_data,
            drought={"composite_score": 1.5, "states": {"IA": {"D2": 30}}},
            noaa={"heat_stress_days": 8, "precip_deficit_pct": -30, "frost_events": 0},
            usda={"crop_progress": {"CORN": [{"week_ending": "2025-06-15", "good_pct": 40, "excellent_pct": 15}]}},
        )
        result = strategy.screen(data, "2025-06-15", strategy.get_default_params())
        assert len(result) > 0
        for c in result:
            assert c.metadata.get("needs_llm_analysis") is True
            assert c.metadata.get("analysis_type") == "ag_weather"

    def test_candidates_bundle_raw_data(self, strategy, price_data):
        drought_data = {"composite_score": 1.5, "states": {"IA": {"D2": 30}}}
        noaa_data = {"heat_stress_days": 8, "precip_deficit_pct": -30, "frost_events": 0, "avg_temp_anomaly_f": 3.5}
        data = _make_data(price_data, drought=drought_data, noaa=noaa_data)
        result = strategy.screen(data, "2025-06-15", strategy.get_default_params())
        assert len(result) > 0
        meta = result[0].metadata
        assert "drought_score" in meta
        assert "noaa_data" in meta or "heat_stress_days" in meta


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    def test_works_with_only_momentum(self, strategy):
        """No NOAA, no USDA, no drought — momentum alone."""
        dates = pd.bdate_range("2025-06-01", periods=30)
        prices = {}
        for ticker in AG_TICKERS_FULL.values():
            prices[ticker] = pd.DataFrame(
                {"Close": [100 + i * 1.0 for i in range(30)]},
                index=dates,
            )
        data = _make_data(prices)
        result = strategy.screen(data, "2025-07-10", strategy.get_default_params())
        assert isinstance(result, list)

    def test_empty_prices_returns_empty(self, strategy):
        data = _make_data({}, drought={"composite_score": 2.0, "states": {}})
        result = strategy.screen(data, "2025-06-15", strategy.get_default_params())
        assert result == []

    def test_screen_with_no_data_returns_empty(self, strategy):
        result = strategy.screen({}, "2025-06-15", strategy.get_default_params())
        assert result == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_weather_ag.py -v`
Expected: FAIL — `ImportError` for `AG_TICKERS_FULL`, `AG_TICKERS_WINTER`

- [ ] **Step 3: Rewrite WeatherAgStrategy**

Replace the entire contents of `tradingagents/autoresearch/strategies/weather_ag.py` with:

```python
"""Weather/Agriculture strategy — trades ag instruments on supply disruption signals.

Academic basis: "USDA Reports Affect the Stock Market, Too" (2024,
J. Commodity Markets) and Energy Economics (2021) showing temperature
anomalies significantly affect ag futures returns. Weather explains
~33% of crop yield variability.

Signal logic:
1. Gather data from NOAA (weather), USDA (crop conditions), Drought Monitor.
2. Rule-based gate: is anything interesting happening?
3. If yes, bundle all raw ag data into metadata and delegate to LLM for scoring.
4. Year-round operation with seasonal ticker filtering.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from .base import Candidate

logger = logging.getLogger(__name__)

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
}

# Winter subset (Oct-Mar): skip corn/soy-specific instruments
AG_TICKERS_WINTER = {"weat", "dba", "moo", "adm", "bg"}


class WeatherAgStrategy:
    """Trade agricultural instruments based on multi-source supply disruption signals."""

    name = "weather_ag"
    track = "paper_trade"
    data_sources = ["yfinance", "noaa", "usda", "drought_monitor", "openbb"]

    def get_param_space(self) -> dict[str, tuple]:
        return {
            "lookback_days": (10, 60),
            "hold_days": (10, 45),
            "min_return": (-0.05, 0.05),
            "heat_stress_threshold": (3, 15),
            "precip_deficit_threshold": (-50, -15),
            "drought_min_score": (0.5, 2.0),
            "crop_decline_threshold": (1, 5),
        }

    def get_default_params(self) -> dict[str, Any]:
        return {
            "lookback_days": 21,
            "hold_days": 21,
            "min_return": 0.0,
            "heat_stress_threshold": 5,
            "precip_deficit_threshold": -25,
            "drought_min_score": 1.0,
            "crop_decline_threshold": 2,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Screen for agricultural opportunities using multi-source disruption signals."""
        prices = data.get("yfinance", {}).get("prices", {})
        lookback = params.get("lookback_days", 21)
        heat_threshold = params.get("heat_stress_threshold", 5)
        precip_threshold = params.get("precip_deficit_threshold", -25)
        drought_min = params.get("drought_min_score", 1.0)
        crop_decline_min = params.get("crop_decline_threshold", 2)

        try:
            current_month = pd.Timestamp(date).month
        except ValueError:
            return []

        # Determine season and eligible tickers
        is_growing_season = 4 <= current_month <= 9
        if is_growing_season:
            eligible_tickers = AG_TICKERS_FULL
        else:
            eligible_tickers = {k: v for k, v in AG_TICKERS_FULL.items() if k in AG_TICKERS_WINTER}

        # --- Gather ag context from all sources ---
        noaa_data = data.get("noaa", {})
        drought_data = data.get("drought_monitor", {})
        usda_data = data.get("usda", {})

        # --- Gate check: is anything interesting happening? ---
        gate_triggered = False
        gate_reasons = []

        # Drought gate
        drought_score = 0.0
        if isinstance(drought_data, dict):
            drought_score = drought_data.get("composite_score", 0.0)
        if drought_score >= drought_min:
            gate_triggered = True
            gate_reasons.append(f"drought={drought_score:.1f}")

        # USDA crop condition gate (week-over-week decline in Good+Excellent)
        crop_decline = self._check_crop_decline(usda_data)
        if crop_decline > crop_decline_min:
            gate_triggered = True
            gate_reasons.append(f"crop_decline={crop_decline}pp")

        # NOAA weather gate (growing season only)
        if is_growing_season and noaa_data and "error" not in noaa_data:
            heat_days = noaa_data.get("heat_stress_days", 0)
            precip_deficit = noaa_data.get("precip_deficit_pct", 0)
            frost_events = noaa_data.get("frost_events", 0)
            if heat_days >= heat_threshold:
                gate_triggered = True
                gate_reasons.append(f"heat={heat_days}d")
            if precip_deficit < precip_threshold:
                gate_triggered = True
                gate_reasons.append(f"precip={precip_deficit:.0f}%")
            if frost_events > 0:
                gate_triggered = True
                gate_reasons.append(f"frost={frost_events}")

        # Momentum gate: any ag ticker trailing return > 5%
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
            if trailing_return > 0.05:
                gate_triggered = True
                gate_reasons.append(f"momentum_{ticker}={trailing_return:.1%}")

        if not gate_triggered or not ag_returns:
            return []

        logger.info("Ag gate triggered: %s", ", ".join(gate_reasons))

        # --- Build candidates with LLM metadata ---
        ag_returns.sort(key=lambda x: x[2], reverse=True)

        # Bundle all raw ag data for LLM analysis
        ag_context = {
            "drought_score": drought_score,
            "drought_states": drought_data.get("states", {}) if isinstance(drought_data, dict) else {},
            "noaa_data": {k: v for k, v in noaa_data.items() if k != "error"} if noaa_data else {},
            "usda_data": usda_data if usda_data else {},
            "is_growing_season": is_growing_season,
            "gate_reasons": gate_reasons,
        }

        candidates = []
        for name, ticker, ret in ag_returns[:3]:
            candidates.append(
                Candidate(
                    ticker=ticker,
                    date=date,
                    direction="long",
                    score=0.5,  # LLM will adjust
                    metadata={
                        "commodity": name,
                        "trailing_return": round(ret, 4),
                        "needs_llm_analysis": True,
                        "analysis_type": "ag_weather",
                        **ag_context,
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
        """Exit on hold period."""
        hold_days = params.get("hold_days", 21)
        if holding_days >= hold_days:
            return True, "hold_period"
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

        return f"""You are optimizing a Weather/Agriculture strategy that trades ag
ETFs and agribusiness stocks based on NOAA weather anomalies, USDA crop
condition declines, and US Drought Monitor data.

Current parameters: {current}

Parameter ranges:
- lookback_days: 10-60 (momentum window)
- hold_days: 10-45 (holding period)
- min_return: -0.05 to 0.05 (minimum momentum for fallback)
- heat_stress_threshold: 3-15 (min heat days to trigger)
- precip_deficit_threshold: -50 to -15 (% below normal precipitation)
- drought_min_score: 0.5-2.0 (min composite drought score to trigger)
- crop_decline_threshold: 1-5 (min weekly Good+Excellent decline in pp)

Recent results:
{results_text or '  No results yet.'}

Suggest 3 new parameter combinations. Consider:
- Drought Monitor composite score >= 1.0 means moderate drought across region
- USDA crop condition declines > 2pp week-over-week signal real damage
- Heat stress above 95F during June-August is the strongest weather signal
- Precipitation deficit > 25% drives sustained price moves

Return JSON array of 3 param dicts."""

    @staticmethod
    def _check_crop_decline(usda_data: dict) -> float:
        """Check for week-over-week Good+Excellent decline across commodities.

        Returns the maximum decline in percentage points across all commodities.
        """
        if not usda_data:
            return 0.0

        crop_progress = usda_data.get("crop_progress", {})
        if not crop_progress:
            return 0.0

        max_decline = 0.0
        for commodity, weeks in crop_progress.items():
            if not isinstance(weeks, list) or len(weeks) < 2:
                continue
            # Compare last two weeks (most recent data)
            latest = weeks[-1]
            prior = weeks[-2]
            latest_ge = latest.get("good_pct", 0) + latest.get("excellent_pct", 0)
            prior_ge = prior.get("good_pct", 0) + prior.get("excellent_pct", 0)
            decline = prior_ge - latest_ge
            if decline > max_decline:
                max_decline = decline

        return max_decline
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_weather_ag.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run existing strategy tests to verify no regressions**

Run: `.venv/bin/python -m pytest tests/test_multi_strategy.py::TestStrategyModules -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add tradingagents/autoresearch/strategies/weather_ag.py tests/test_weather_ag.py
git commit -m "feat: rewrite weather_ag with expanded tickers, year-round operation, LLM scoring"
```

---

### Task 5: Add ag_weather LLM Analysis Type

**Files:**
- Modify: `tradingagents/autoresearch/llm_analyzer.py:49-85` (add prompt to `_DEFAULT_PROMPTS`)
- Modify: `tradingagents/autoresearch/llm_analyzer.py` (add `analyze_ag_weather` method)
- Modify: `tradingagents/autoresearch/multi_strategy_engine.py:993-1021` (add dispatch case in `_enrich_with_llm`)

- [ ] **Step 1: Write failing test for ag_weather analysis**

Add to `tests/test_weather_ag.py` (append at the end):

```python
# ---------------------------------------------------------------------------
# LLM Analyzer integration
# ---------------------------------------------------------------------------

class TestLLMAnalyzerIntegration:
    def test_ag_weather_prompt_exists(self):
        from tradingagents.autoresearch.llm_analyzer import _DEFAULT_PROMPTS
        assert "ag_weather" in _DEFAULT_PROMPTS

    def test_analyze_ag_weather_returns_dict(self):
        from tradingagents.autoresearch.llm_analyzer import LLMAnalyzer
        analyzer = LLMAnalyzer()
        # Mock the LLM call
        from unittest.mock import patch
        mock_response = '{"direction": "long", "score": 0.7, "reasoning": "Drought conditions severe"}'
        with patch.object(analyzer, "_call_llm", return_value=mock_response):
            result = analyzer.analyze_ag_weather(
                ticker="DBA",
                commodity_name="Invesco DB Agriculture Fund",
                ag_context={
                    "drought_score": 1.5,
                    "noaa_data": {"heat_stress_days": 8},
                    "usda_data": {},
                },
                trailing_return=0.03,
                hold_days=21,
            )
        assert result["direction"] == "long"
        assert result["score"] == 0.7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_weather_ag.py::TestLLMAnalyzerIntegration -v`
Expected: FAIL — `"ag_weather" not in _DEFAULT_PROMPTS`

- [ ] **Step 3: Add ag_weather prompt to _DEFAULT_PROMPTS**

In `tradingagents/autoresearch/llm_analyzer.py`, add to the `_DEFAULT_PROMPTS` dict after the `"litigation"` entry (line 85):

```python
    "ag_weather": """You are analyzing agricultural supply disruption risk.
Assess whether weather, drought, and crop condition data support a long position
in agricultural instruments. Consider:
1. Are conditions actually damaging crops, or just concerning?
2. Has the market already priced in the disruption (check momentum)?
3. Is this ticker directly exposed to the affected commodities?
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"neutral"), score (0.0-1.0), reasoning (1-2 sentences).
Keep ALL string values under 100 characters.""",
```

- [ ] **Step 4: Add analyze_ag_weather method to LLMAnalyzer**

In `tradingagents/autoresearch/llm_analyzer.py`, add after the `analyze_litigation` method (after line 477):

```python
    # ------------------------------------------------------------------
    # Agricultural weather analysis (ag_weather)
    # ------------------------------------------------------------------

    def analyze_ag_weather(
        self,
        ticker: str,
        commodity_name: str,
        ag_context: dict,
        trailing_return: float = 0.0,
        hold_days: int = 21,
        regime_context: dict | None = None,
    ) -> dict[str, Any]:
        """Analyze agricultural supply disruption risk for a ticker.

        Returns dict with: direction ("long"/"neutral"), score (0-1), reasoning.
        """
        system = self.get_prompt("ag_weather")

        noaa = ag_context.get("noaa_data", {})
        drought_score = ag_context.get("drought_score", 0.0)
        drought_states = ag_context.get("drought_states", {})
        usda = ag_context.get("usda_data", {})

        # Format USDA crop progress for prompt
        crop_lines = []
        crop_progress = usda.get("crop_progress", {}) if isinstance(usda, dict) else {}
        for commodity, weeks in crop_progress.items():
            if not isinstance(weeks, list) or not weeks:
                continue
            latest = weeks[-1]
            ge = latest.get("good_pct", 0) + latest.get("excellent_pct", 0)
            change = ""
            if len(weeks) >= 2:
                prior = weeks[-2]
                prior_ge = prior.get("good_pct", 0) + prior.get("excellent_pct", 0)
                change = f" (change: {ge - prior_ge:+d}pp)"
            crop_lines.append(f"- {commodity}: {ge}% Good/Excellent{change}")

        # Count states in severe+ drought
        severe_states = [s for s, d in drought_states.items()
                        if isinstance(d, dict) and d.get("D2", 0) + d.get("D3", 0) + d.get("D4", 0) > 20]

        user = f"""Analyzing {ticker} ({commodity_name}).

WEATHER (NOAA, last 30 days):
- Heat stress days (>95F): {noaa.get('heat_stress_days', 'N/A')}
- Precipitation deficit: {noaa.get('precip_deficit_pct', 'N/A')}%
- Frost events: {noaa.get('frost_events', 'N/A')}
- Temp anomaly: {noaa.get('avg_temp_anomaly_f', 'N/A')}F

DROUGHT (US Drought Monitor):
- Composite score: {drought_score}/4.0
- States in severe+ drought: {', '.join(severe_states) if severe_states else 'none'}

CROP CONDITIONS (USDA):
{chr(10).join(crop_lines) if crop_lines else '- No data available'}

PRICE ACTION:
- Trailing return: {trailing_return:.1%}

Assess probability that ag supply disruption drives {ticker} higher over {hold_days} days.
Return JSON.""" + self._regime_suffix(regime_context)

        result = self._call_llm(system, user)
        return _parse_json_response(result) if result else {}
```

- [ ] **Step 5: Add ag_weather dispatch in _enrich_with_llm**

In `tradingagents/autoresearch/multi_strategy_engine.py`, add a new `elif` case after the `"exec_comp"` block (after line 1020):

```python
                elif analysis_type == "ag_weather":
                    llm_result = self._analyzer.analyze_ag_weather(
                        ticker=c.ticker,
                        commodity_name=c.metadata.get("commodity", c.ticker),
                        ag_context={
                            "drought_score": c.metadata.get("drought_score", 0),
                            "drought_states": c.metadata.get("drought_states", {}),
                            "noaa_data": c.metadata.get("noaa_data", {}),
                            "usda_data": c.metadata.get("usda_data", {}),
                        },
                        trailing_return=c.metadata.get("trailing_return", 0),
                        hold_days=21,
                        regime_context=regime_context,
                    )
```

Also: the `ag_weather` LLM returns `score` instead of `conviction`. After the dispatch block, the existing code uses `llm_result.get("conviction", c.score)` to update `c.score`. Add a normalizer — find line 1027 (`c.score = llm_result.get("conviction", c.score)`) and change it to:

```python
                c.score = llm_result.get("conviction", llm_result.get("score", c.score))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_weather_ag.py::TestLLMAnalyzerIntegration -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add tradingagents/autoresearch/llm_analyzer.py tradingagents/autoresearch/multi_strategy_engine.py
git commit -m "feat: add ag_weather LLM analysis type and engine dispatch"
```

---

### Task 6: Engine Integration — Fetch Methods and Core Tickers

**Files:**
- Modify: `tradingagents/autoresearch/multi_strategy_engine.py:648-704` (add fetch methods to `_fetch_all_data`)
- Modify: `tradingagents/autoresearch/multi_strategy_engine.py:917` (expand `core_tickers`)

- [ ] **Step 1: Add _fetch_usda_data and _fetch_drought_data methods**

In `tradingagents/autoresearch/multi_strategy_engine.py`, add two new methods after `_fetch_noaa_data` (after line 902):

```python
    def _fetch_usda_data(self, trading_date: str) -> dict[str, Any]:
        """Fetch USDA crop condition data for corn, soybeans, and wheat."""
        source = self.registry.get("usda")
        if source is None:
            return {}

        try:
            from datetime import datetime
            year = datetime.strptime(trading_date, "%Y-%m-%d").year
            crop_progress = {}
            for commodity in ("CORN", "SOYBEANS", "WHEAT"):
                weeks = source.fetch_crop_progress(commodity, year)
                if weeks:
                    crop_progress[commodity] = weeks
            return {"crop_progress": crop_progress}
        except Exception:
            logger.error("Failed to fetch USDA data", exc_info=True)
            return {}

    def _fetch_drought_data(self, trading_date: str) -> dict[str, Any]:
        """Fetch Drought Monitor severity and composite score."""
        source = self.registry.get("drought_monitor")
        if source is None:
            return {}

        try:
            from datetime import datetime, timedelta
            end = trading_date
            start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
            severity = source.fetch_drought_severity(start=start, end=end)
            composite = source.fetch_composite_score(date=trading_date)
            return {"composite_score": composite, "states": severity}
        except Exception:
            logger.error("Failed to fetch Drought Monitor data", exc_info=True)
            return {}
```

- [ ] **Step 2: Add USDA and Drought Monitor to api_fetches in _fetch_all_data**

In `_fetch_all_data`, add these two entries after the `noaa` entry (after line 683):

```python
        if "usda" in needed_sources and "usda" in available:
            api_fetches["usda"] = (self._fetch_usda_data, (end_date,))
        if "drought_monitor" in needed_sources and "drought_monitor" in available:
            api_fetches["drought_monitor"] = (self._fetch_drought_data, (end_date,))
```

- [ ] **Step 3: Expand core_tickers to include ag stocks**

In `_fetch_yfinance_data`, change line 917 from:

```python
        core_tickers = ["SPY", "SHY", "TLT", "DBA", "WEAT", "CORN", "MOO"]
```

to:

```python
        core_tickers = ["SPY", "SHY", "TLT", "DBA", "WEAT", "CORN", "MOO", "SOYB", "ADM", "BG", "CTVA", "DE", "FMC"]
```

- [ ] **Step 4: Run full test suite to verify no regressions**

Run: `.venv/bin/python -m pytest tests/test_multi_strategy.py tests/test_weather_ag.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add tradingagents/autoresearch/multi_strategy_engine.py
git commit -m "feat: add USDA/drought fetch methods and expand core tickers for ag stocks"
```

---

### Task 7: Config, .env.example, and Documentation Updates

**Files:**
- Modify: `tradingagents/default_config.py:115` (add `usda_nass_api_key` — already present from spec work)
- Modify: `.env.example` (add `USDA_NASS_API_KEY=` — already present from spec work)
- Modify: `CLAUDE.md` (update strategy table, data source count, config entries)
- Modify: `AUTORESEARCH_ARCHITECTURE_MAP.md` (add USDA and Drought Monitor)

- [ ] **Step 1: Verify default_config.py has usda_nass_api_key**

Read `tradingagents/default_config.py` and verify `"usda_nass_api_key": ""` is present in the autoresearch section. If not, add it after the `noaa_cdo_token` entry.

- [ ] **Step 2: Verify .env.example has USDA_NASS_API_KEY**

Read `.env.example` and verify `USDA_NASS_API_KEY=` is present. If not, add it after `NOAA_CDO_TOKEN=`.

- [ ] **Step 3: Update CLAUDE.md strategy table**

In `CLAUDE.md`, update the `weather_ag` row in the Active Strategies table. Change:

```
| `weather_ag` | `WeatherAgStrategy` | NOAA CDO, yfinance, OpenBB (weather anomalies + ag ETF momentum) |
```

to:

```
| `weather_ag` | `WeatherAgStrategy` | NOAA CDO, USDA NASS, Drought Monitor, yfinance, OpenBB (weather + crop conditions + drought + ag momentum) |
```

Update the data source count from 10 to 12 in the Project Overview section:

```
10 event-driven strategies across 12 data sources
```

Add `usda_nass_api_key` to the Config table:

```
| `autoresearch` | `usda_nass_api_key` | USDA NASS API key for crop condition data (free from quickstats.nass.usda.gov) |
```

- [ ] **Step 4: Update AUTORESEARCH_ARCHITECTURE_MAP.md**

Add USDA NASS and Drought Monitor to the data source table. Add the entries following the existing format for NOAA.

- [ ] **Step 5: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v --timeout=30`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add tradingagents/default_config.py .env.example CLAUDE.md AUTORESEARCH_ARCHITECTURE_MAP.md
git commit -m "docs: update config, docs for USDA/Drought Monitor integration"
```

---

## File Structure Summary

| File | Action | Responsibility |
|------|--------|---------------|
| `tradingagents/autoresearch/data_sources/usda_source.py` | Create | USDA NASS crop condition API |
| `tradingagents/autoresearch/data_sources/drought_monitor_source.py` | Create | US Drought Monitor API |
| `tradingagents/autoresearch/data_sources/registry.py` | Modify | Register both new sources |
| `tradingagents/autoresearch/data_sources/__init__.py` | Modify | Export both new sources |
| `tradingagents/autoresearch/strategies/weather_ag.py` | Rewrite | Expanded tickers, year-round, LLM scoring |
| `tradingagents/autoresearch/llm_analyzer.py` | Modify | Add ag_weather prompt + analyze method |
| `tradingagents/autoresearch/multi_strategy_engine.py` | Modify | Add fetch methods, dispatch, expand tickers |
| `tradingagents/default_config.py` | Verify | usda_nass_api_key present |
| `.env.example` | Verify | USDA_NASS_API_KEY present |
| `CLAUDE.md` | Modify | Strategy table, source count, config |
| `AUTORESEARCH_ARCHITECTURE_MAP.md` | Modify | Add data source entries |
| `tests/test_usda_source.py` | Create | 12 tests for USDA source |
| `tests/test_drought_monitor_source.py` | Create | 12 tests for Drought Monitor |
| `tests/test_weather_ag.py` | Create | 18+ tests for enhanced strategy |
