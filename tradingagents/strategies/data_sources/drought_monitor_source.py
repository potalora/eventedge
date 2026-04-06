"""US Drought Monitor data source.

Provides drought severity statistics by state from the
US Drought Monitor (USDM). No authentication required.

API docs: https://droughtmonitor.unl.edu/DmData/DataDownload/WebServiceInfo.aspx
"""
from __future__ import annotations

import logging
import time
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

        max_retries = 3
        base_delay = 5.0
        data = None
        for attempt in range(max_retries + 1):
            try:
                resp = requests.get(
                    BASE_URL,
                    params=params,
                    headers={"Accept": "application/json"},
                    timeout=60,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    break
                elif resp.status_code >= 500:
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(
                            "Drought Monitor returned %d (attempt %d/%d), retrying in %.0fs",
                            resp.status_code, attempt + 1, max_retries, delay,
                        )
                        time.sleep(delay)
                        continue
                    logger.warning("Drought Monitor returned %d after %d retries", resp.status_code, max_retries)
                    return {}
                else:
                    logger.warning("Drought Monitor returned %d", resp.status_code)
                    return {}
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "Drought Monitor request failed (attempt %d/%d): %s, retrying in %.0fs",
                        attempt + 1, max_retries, exc, delay,
                    )
                    time.sleep(delay)
                    continue
                logger.error("Drought Monitor request failed after %d retries", max_retries, exc_info=True)
                return {}
            except requests.RequestException:
                logger.error("Drought Monitor request failed", exc_info=True)
                return {}
        if data is None:
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
