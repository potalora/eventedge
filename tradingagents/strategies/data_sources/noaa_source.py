"""NOAA Climate Data Online (CDO) v2 source.

Provides temperature and precipitation anomaly data for US agricultural
regions. Free token from https://www.ncdc.noaa.gov/cdo-web/token.

Rate limits: 5 requests/second, 10,000 requests/day.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any

import requests

from .evidence import current_session_date, require_current_as_of, acquisition_time
from .request_policy import provider_request, provider_budget, current_provider_deadline
from .fetch_errors import SourceFetchError, source_fetch_error, source_text, source_date, source_number

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ncei.noaa.gov/cdo-web/api/v2"
MAX_STATE_OBSERVATIONS = 100_000
MAX_OBSERVATION_LAG_DAYS = 7

# Key US agricultural states (Corn Belt + Plains)
AG_STATES = {
    "IL": "FIPS:17",
    "IA": "FIPS:19",
    "KS": "FIPS:20",
    "NE": "FIPS:31",
    "MN": "FIPS:27",
    "IN": "FIPS:18",
    "OH": "FIPS:39",
    "SD": "FIPS:46",
    "ND": "FIPS:38",
    "MO": "FIPS:29",
}

# Growing season: April through September
GROWING_SEASON = (4, 9)

# Crop stress thresholds
HEAT_STRESS_F = 95  # Corn/soy stress threshold
FROST_THRESHOLD_F = 32  # Killing frost


def _build_session() -> requests.Session:
    """Use requests' scoped transport; never mutate process-wide connectors."""
    return requests.Session()


class NOAASource:
    """Data source backed by NOAA CDO API v2."""

    name: str = "noaa"
    requires_api_key: bool = True

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("NOAA_CDO_TOKEN", "")
        self._cache: dict[str, Any] = {}
        self._last_request_time: float = 0.0
        self._session: requests.Session | None = None

    def _get_session(self) -> requests.Session:
        if self._session is None:
            self._session = _build_session()
        return self._session

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "ag_weather_summary")
        dispatch = {
            "ag_weather_summary": self._dispatch_ag_summary,
            "state_daily": self._dispatch_state_daily,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except SourceFetchError as exc:
            return {**exc.partial_data, "error": str(exc)}
        except Exception:
            logger.error("NOAASource.fetch(%s) failed", method)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        return bool(self._token)

    def fetch_state_daily(
        self,
        state_fips: str,
        start: str,
        end: str,
        datatypes: list[str] | None = None,
        *, as_of: str | None = None,
    ) -> list[dict]:
        """Fetch daily GHCND observations for a US state.

        Args:
            state_fips: FIPS location ID (e.g., "FIPS:19" for Iowa).
            start: Start date YYYY-MM-DD.
            end: End date YYYY-MM-DD.
            datatypes: Data types to fetch (default: TMAX, TMIN, PRCP).

        Returns:
            List of observation dicts with keys: date, datatype, station, value.
        """
        require_current_as_of(as_of or end, current_session_date())
        if datatypes is None:
            datatypes = ["TMAX", "TMIN", "PRCP"]

        cache_key = f"daily|{state_fips}|{start}|{end}|{','.join(datatypes)}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        if current_provider_deadline("noaa") is None:
            with provider_budget("noaa", time.monotonic() + 60):
                return self.fetch_state_daily(state_fips, start, end, datatypes, as_of=as_of)

        all_results: list[dict] = []
        seen_observations: set[tuple[str, str, str]] = set()
        offset = 1
        expected_total = None

        while True:
            try:
                data = self._api_get("/data", {
                    "datasetid": "GHCND",
                    "locationid": state_fips,
                    "datatypeid": ",".join(datatypes),
                    "startdate": start,
                    "enddate": end,
                    "units": "standard",
                    "limit": 1000,
                    "offset": offset,
                })
            except SourceFetchError as exc:
                exc.partial_data = {"observations": all_results + exc.partial_data.get("observations", [])}
                raise
            results = data["results"]
            for row in results:
                identity = (row["date"], row["datatype"], row["station"])
                if identity in seen_observations:
                    raise SourceFetchError("NOAA pagination repeated observations", reason_code="invalid_response",
                                           partial_data={"observations": all_results})
                seen_observations.add(identity)
                all_results.append(row)
            envelope = data.get("metadata")
            metadata = envelope.get("resultset") if isinstance(envelope, dict) else None
            if not isinstance(metadata, dict):
                raise SourceFetchError("NOAA pagination metadata invalid", reason_code="invalid_response",
                                       partial_data={"observations": all_results})
            total = metadata.get("count")
            if type(total) is int and total > MAX_STATE_OBSERVATIONS:
                raise SourceFetchError("NOAA pagination coverage exceeds resource limit", reason_code="invalid_response",
                                       partial_data={"observations": all_results})
            if (type(total) is not int or total < 0
                    or (expected_total is not None and total != expected_total)
                    or ("offset" in metadata and (type(metadata["offset"]) is not int or metadata["offset"] != offset))
                    or ("limit" in metadata and (type(metadata["limit"]) is not int
                        or not 1 <= metadata["limit"] <= 1000 or len(results) > metadata["limit"]))
                    or len(results) > 1000
                    or len(all_results) > MAX_STATE_OBSERVATIONS
                    or len(all_results) > total
                    or (not results and len(all_results) < total)):
                raise SourceFetchError("NOAA pagination inconsistent", reason_code="invalid_response",
                                       partial_data={"observations": all_results})
            expected_total = total
            if len(all_results) == total:
                break
            offset += len(results)
        self._cache[cache_key] = all_results
        return all_results

    def fetch_ag_weather_summary(
        self,
        date: str,
        lookback_days: int = 30,
    ) -> dict[str, Any]:
        """Aggregate station means to state-days, then unique regional days.

        Heat/frost duration counts each calendar day once across all states.
        Every requested state/date/type must have a usable observation; this
        verifies sample coverage, not a census of every station in the state.
        Seasonal reference values remain documented approximations.
        """
        require_current_as_of(date, current_session_date())
        if type(lookback_days) is not int or lookback_days < 1:
            raise SourceFetchError("NOAA lookback invalid", reason_code="invalid_response")
        if current_provider_deadline("noaa") is None:
            with provider_budget("noaa", time.monotonic() + 60):
                return self.fetch_ag_weather_summary(date, lookback_days)
        as_of_date = datetime.strptime(date, "%Y-%m-%d")
        fetch_start = as_of_date - timedelta(days=lookback_days - 1 + MAX_OBSERVATION_LAG_DAYS)
        failures, statuses, state_groups = {}, {}, {}
        for state, fips in AG_STATES.items():
            try:
                obs = self.fetch_state_daily(fips, fetch_start.strftime("%Y-%m-%d"), date)
            except SourceFetchError as exc:
                obs = exc.partial_data.get("observations", [])
                failures[state] = exc.reason_code
                if exc.http_status is not None:
                    statuses[state] = exc.http_status
            grouped = {}
            for row in obs:
                key = (row.get("date", "")[:10], row.get("datatype"))
                if not fetch_start.strftime("%Y-%m-%d") <= key[0] <= date or key[1] not in ("TMAX", "TMIN", "PRCP"):
                    continue
                grouped.setdefault(key, []).append(float(row['value']))
            state_groups[state] = grouped

        # NOAA daily records commonly arrive 1-2 days after observation. Select
        # the newest fully covered contiguous window, never fill missing dates.
        # Seven days is a strategy freshness policy, not a NOAA delivery SLA.
        selected_lag, dates, expected = None, set(), set()
        end_date = as_of_date
        for lag in range(MAX_OBSERVATION_LAG_DAYS + 1):
            candidate_end = as_of_date - timedelta(days=lag)
            candidate_start = candidate_end - timedelta(days=lookback_days - 1)
            candidate_dates = {(candidate_start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(lookback_days)}
            candidate_expected = {(day,dtype) for day in candidate_dates for dtype in ("TMAX","TMIN","PRCP")}
            if all(candidate_expected <= grouped.keys() for grouped in state_groups.values()):
                selected_lag, end_date, dates, expected = lag, candidate_end, candidate_dates, candidate_expected
                break
        if selected_lag is None:
            # Diagnostic missingness is scoped to the current target window;
            # partial older observations do not establish a timely aggregate.
            dates = {(as_of_date - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(lookback_days)}
            expected = {(day,dtype) for day in dates for dtype in ("TMAX","TMIN","PRCP")}
        start_date = end_date - timedelta(days=lookback_days - 1)
        in_season = GROWING_SEASON[0] <= end_date.month <= GROWING_SEASON[1]
        heat_dates, frost_dates, affected = set(), set(), {}
        tmax_values, prcp_values, missing, reporting = [], [], {}, []
        observation_count = 0
        for state, grouped in state_groups.items():
            absent = expected - grouped.keys()
            if absent:
                failures.setdefault(state, "invalid_response")
                missing[state] = sorted(f"{day}:{dtype}" for day, dtype in absent)
            selected = {key:values for key,values in grouped.items() if key in expected}
            if selected:
                reporting.append(state)
            for (day, dtype), values in selected.items():
                value = sum(values) / len(values)
                observation_count += len(values)
                if dtype == "TMAX":
                    tmax_values.append(value)
                    if value > HEAT_STRESS_F:
                        heat_dates.add(day)
                        affected.setdefault(day, {}).setdefault("heat", []).append(state)
                elif dtype == "TMIN":
                    observed = datetime.strptime(day, "%Y-%m-%d")
                    if (value < FROST_THRESHOLD_F and GROWING_SEASON[0] <= observed.month <= GROWING_SEASON[1]
                            and observed > observed.replace(month=4, day=15)):
                        frost_dates.add(day)
                        affected.setdefault(day, {}).setdefault("frost", []).append(state)
                else:
                    prcp_values.append(value)
        avg_tmax = sum(tmax_values) / len(tmax_values) if tmax_values else None
        avg_prcp = sum(prcp_values) / len(prcp_values) if prcp_values else None
        normal_tmax, normal_prcp = (85.0, .12) if in_season else (45.0, .08)
        acquired = acquisition_time()
        summary = {
            "heat_stress_days": len(heat_dates) if tmax_values else None,
            "frost_events": len(frost_dates) if any('frost' in value for value in affected.values()) or reporting else None,
            "precip_deficit_pct": round((avg_prcp - normal_prcp) / normal_prcp * 100, 1) if avg_prcp is not None else None,
            "avg_temp_anomaly_f": round(avg_tmax - normal_tmax, 1) if avg_tmax is not None else None,
            "avg_tmax": round(avg_tmax, 1) if avg_tmax is not None else None,
            "avg_daily_prcp": round(avg_prcp, 3) if avg_prcp is not None else None,
            "states_reporting": len(reporting), "in_growing_season": in_season,
            "lookback_days": lookback_days, "observations": observation_count,
            "day_unit": "regional_days", "affected_states_by_date": affected,
            "acquired_at": acquired, "available_at": acquired,
            "as_of":date, "observation_lag_days":selected_lag,
            "observation_date":end_date.strftime("%Y-%m-%d"),
            "start_date":start_date.strftime("%Y-%m-%d"), "end_date":end_date.strftime("%Y-%m-%d"),
            "coverage": {"mode":"state_date_datatype_sample", "complete":not failures,
                "requested_states":list(AG_STATES), "states_reporting":reporting,
                "window_start":start_date.strftime("%Y-%m-%d"), "window_end":end_date.strftime("%Y-%m-%d"),
                "as_of":date, "observation_lag_days":selected_lag, "max_observation_lag_days":MAX_OBSERVATION_LAG_DAYS,
                "missing":missing, "station_aggregation":"arithmetic_mean_per_state_date_datatype"},
        }
        if failures:
            # Incomplete regional input cannot establish calm/zero conditions.
            for field in ("heat_stress_days", "frost_events", "precip_deficit_pct", "avg_temp_anomaly_f"):
                summary[field] = None
            raise SourceFetchError("NOAA state coverage incomplete", reason_code="batch_failure",
                failed_operations=failures, failed_http_statuses=statuses, partial_data=summary)
        return summary

    def clear_cache(self) -> None:
        self._cache.clear()

    def _api_get(self, endpoint: str, params: dict) -> dict | None:
        """Make a rate-limited GET request to the NOAA CDO API with retry."""
        if not self._token:
            raise SourceFetchError("NOAA access missing", reason_code="provider_error")
        response = provider_request("noaa", "GET", f"{BASE_URL}{endpoint}",
                                    transport=self._get_session().get, operation="daily_observations",
                                    headers={"token": self._token}, params=params, timeout=(5, 10))
        try:
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("results"), list) or not all(isinstance(row, dict) for row in data["results"]):
                raise ValueError("invalid observations schema")
        except Exception:
            raise SourceFetchError("NOAA observations invalid", reason_code="invalid_response") from None
        valid_rows, invalid = [], False
        for row in data["results"]:
            if (not source_date(row.get("date")) or not source_text(row.get("datatype"))
                    or not source_text(row.get("station")) or not source_number(row.get("value"))
                    or (isinstance(row.get("attributes"), str) and len(row["attributes"].split(",")) > 1
                        and row["attributes"].split(",")[1].strip())):
                invalid = True
            else:
                valid_rows.append(row)
        if invalid:
            raise SourceFetchError("NOAA observation records invalid", reason_code="invalid_response",
                                   partial_data={"observations": valid_rows})
        return data

    def _dispatch_ag_summary(self, params: dict[str, Any]) -> dict[str, Any]:
        date = params.get("date", current_session_date())
        lookback = params.get("lookback_days", 30)
        return self.fetch_ag_weather_summary(date, lookback)

    def _dispatch_state_daily(self, params: dict[str, Any]) -> dict[str, Any]:
        state = params.get("state_fips", "")
        start = params.get("start", "")
        end = params.get("end", "")
        datatypes = params.get("datatypes")
        obs = self.fetch_state_daily(state, start, end, datatypes, as_of=params.get("as_of"))
        return {"observations": obs, "count": len(obs)}
