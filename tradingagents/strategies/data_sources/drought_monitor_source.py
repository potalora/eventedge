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

from .evidence import current_session_date, require_current_as_of, acquisition_time
from .request_policy import provider_request
from .fetch_errors import SourceFetchError, source_fetch_error, source_text, source_date, source_number

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
        except SourceFetchError as exc:
            return {**exc.partial_data, "error": str(exc)}
        except Exception:
            logger.error("DroughtMonitorSource.fetch(%s) failed", method)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        return True

    def fetch_drought_severity(
        self,
        states: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Fetch drought category percentages for each state.

        Args:
            states: List of state abbreviations (default: DEFAULT_AG_STATES).
            start: Start date YYYY-MM-DD (default: 7 days ago).
            end: End date YYYY-MM-DD (default: today).

        Returns:
            Dict mapping state abbreviation to drought categories:
            {state: {"None": pct, "D0": pct, ..., "D4": pct,
                     "observation_date": date, "acquired_at": timestamp, "available_at": timestamp}}
        """
        states = states or DEFAULT_AG_STATES
        if end is None:
            end = current_session_date()
        if start is None:
            start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")

        require_current_as_of(end, current_session_date())

        # Convert dates to API format (M/d/yyyy)
        start_fmt = datetime.strptime(start, "%Y-%m-%d").strftime("%-m/%-d/%Y")
        end_fmt = datetime.strptime(end, "%Y-%m-%d").strftime("%-m/%-d/%Y")

        params = {
            "aoi": ",".join(states),
            "startdate": start_fmt,
            "enddate": end_fmt,
            "statisticsType": 2,  # Disjoint categorical areas, not cumulative exceedances
        }

        response = provider_request("drought_monitor", "GET", BASE_URL, operation="severity",
                                    params=params, headers={"Accept": "application/json"}, timeout=60)
        try:
            data = response.json()
            if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
                raise ValueError("invalid drought schema")
        except Exception:
            raise SourceFetchError("Drought Monitor response invalid", reason_code="invalid_response") from None

        acquired_at = acquisition_time()
        result: dict[str, dict[str, Any]] = {}
        latest_dates = {}
        invalid = False
        for record in data:
            state = record.get("StateAbbreviation")
            map_date = record.get("MapDate")
            if isinstance(map_date, str) and len(map_date) == 8 and map_date.isdigit():
                map_date = f"{map_date[:4]}-{map_date[4:6]}-{map_date[6:]}"
            categories = ("None", "D0", "D1", "D2", "D3", "D4")
            if (not source_text(state) or not source_date(map_date)
                    or not all(source_number(record.get(key), minimum=0, maximum=100) for key in categories)
                    or record.get("StatisticFormatID", 2) != 2
                    or abs(sum(float(record[key]) for key in categories) - 100) > 0.2):
                invalid = True
                continue
            if state not in states or not start <= map_date[:10] <= end:
                continue
            if state not in latest_dates or map_date > latest_dates[state]:
                latest_dates[state] = map_date
                result[state] = {key: float(record[key]) for key in categories}
                result[state].update({"observation_date":map_date[:10], "acquired_at":acquired_at,
                                      "available_at":acquired_at, "statistics_type":"categorical"})
        missing = set(states) - result.keys()
        if missing:
            raise SourceFetchError("Drought Monitor requested states unavailable", reason_code="invalid_response",
                                   partial_data={"states":result, "coverage":{"complete":False,"missing_states":sorted(missing)}})
        if invalid:
            raise SourceFetchError("Drought Monitor records invalid", reason_code="invalid_response",
                                   partial_data={"states": result})
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
            date = current_session_date()

        start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        severity = self.fetch_drought_severity(states, start, date)

        if not severity:
            raise SourceFetchError("Drought Monitor observations unavailable", reason_code="invalid_response")

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
