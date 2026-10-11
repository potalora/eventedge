"""NOAA public regional weather and optional CDO v2 source.

Regional temperature and precipitation use public NCEI Access summaries.
Direct CDO state queries require a token from https://www.ncdc.noaa.gov/cdo-web/token.

Rate limits: 5 requests/second, 10,000 requests/day.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from .evidence import current_session_date, require_current_as_of, require_current_vintage, acquisition_time
from .request_policy import provider_request, provider_budget, current_provider_deadline
from .fetch_errors import SourceFetchError, source_fetch_error, source_text, source_date, source_number

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ncei.noaa.gov/cdo-web/api/v2"
MAX_STATE_OBSERVATIONS = 100_000
MAX_OBSERVATION_LAG_DAYS = 7
BULK_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
CATALOG_URL = "https://www.ncei.noaa.gov/pub/data/ghcn/daily"
REGIONAL_BUDGET_SECONDS = 90
MAX_REGIONAL_STATIONS = 12_000

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
    """Public NCEI regional summaries with optional authenticated CDO queries."""

    name: str = "noaa"
    requires_api_key: bool = False

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("NOAA_CDO_TOKEN", "")
        self._cache: dict[str, Any] = {}
        self._last_request_time: float = 0.0
        self._session: requests.Session | None = None
        self.observation_exclusions = {"quality_flag": 0}
        self.regional_coverage: dict[str, Any] = {}

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
        # The default regional acquisition path is public. Direct CDO requests
        # still check their token at _api_get before sending any request.
        return True

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
        raw_received = 0

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
            raw_count = data.get("raw_count", len(results))
            raw_received += raw_count
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
                        or not 1 <= metadata["limit"] <= 1000 or raw_count > metadata["limit"]))
                    or raw_count > 1000
                    or len(all_results) > MAX_STATE_OBSERVATIONS
                    or raw_received > total
                    or (not raw_count and raw_received < total)):
                raise SourceFetchError("NOAA pagination inconsistent", reason_code="invalid_response",
                                       partial_data={"observations": all_results})
            expected_total = total
            if raw_received == total:
                break
            offset += raw_count
        self._cache[cache_key] = all_results
        return all_results

    def fetch_ag_weather_summary(
        self,
        date: str,
        lookback_days: int = 30,
        *, vintage_as_of: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate station means to state-days, then unique regional days.

        Heat/frost duration counts each calendar day once across all states.
        Every requested state/date/type must have a usable observation; this
        verifies sample coverage, not a census of every station in the state.
        Seasonal reference values remain documented approximations.
        """
        require_current_vintage(date, vintage_as_of, today=current_session_date())
        if type(lookback_days) is not int or lookback_days < 1:
            raise SourceFetchError("NOAA lookback invalid", reason_code="invalid_response")
        deadline = current_provider_deadline("noaa")
        native_deadline = time.monotonic() + REGIONAL_BUDGET_SECONDS
        if deadline is None or deadline > native_deadline:
            with provider_budget("noaa", native_deadline):
                return self.fetch_ag_weather_summary(date, lookback_days, vintage_as_of=vintage_as_of)
        as_of_date = datetime.strptime(date, "%Y-%m-%d")
        fetch_start = as_of_date - timedelta(days=lookback_days - 1 + MAX_OBSERVATION_LAG_DAYS)
        failures, statuses, state_groups = {}, {}, {}
        try:
            regional = self.fetch_region_daily(fetch_start.strftime("%Y-%m-%d"), date)
        except SourceFetchError as exc:
            regional = exc.partial_data.get("states", {})
            failures = {state: exc.reason_code for state in AG_STATES}
            if exc.http_status is not None:
                statuses = {state: exc.http_status for state in AG_STATES}
        for state in AG_STATES:
            obs = regional.get(state, [])
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
                **self.regional_coverage, "exclusions":dict(self.observation_exclusions),
                "missing":missing, "station_aggregation":"arithmetic_mean_per_state_date_datatype"},
        }
        if failures:
            # Incomplete regional input cannot establish calm/zero conditions.
            for field in ("heat_stress_days", "frost_events", "precip_deficit_pct", "avg_temp_anomaly_f"):
                summary[field] = None
            raise SourceFetchError("NOAA state coverage incomplete", reason_code="batch_failure",
                failed_operations=failures, failed_http_statuses=statuses, partial_data=summary)
        return summary

    @staticmethod
    def _quality_flagged(attributes):
        return isinstance(attributes, str) and len(attributes.split(",")) > 1 and bool(attributes.split(",")[1].strip())

    def fetch_region_daily(self, start: str, end: str) -> dict[str, list[dict]]:
        """Bulk GHCN observations for all state-catalog stations active in the window's years.

        The official inventory selects all stations with any requested element
        spanning an observation year, never a conveniently reporting panel.
        Completeness still means usable state/day/type samples, not spatial census.
        """
        deadline = current_provider_deadline("noaa")
        native_deadline = time.monotonic() + REGIONAL_BUDGET_SECONDS
        if deadline is None or deadline > native_deadline:
            with provider_budget("noaa", native_deadline):
                return self.fetch_region_daily(start, end)
        station_text = provider_request("noaa", "GET", f"{CATALOG_URL}/ghcnd-stations.txt",
            operation="station_catalog", timeout=(5,15)).text
        inventory = provider_request("noaa", "GET", f"{CATALOG_URL}/ghcnd-inventory.txt",
            operation="element_inventory", timeout=(5,15)).text
        states = {line[:11]:line[38:40] for line in station_text.splitlines() if line[38:40] in AG_STATES}
        selected = set()
        try:
            for line in inventory.splitlines():
                if line[:11] in states and line[31:35] in {"TMAX","TMIN","PRCP"}:
                    if int(line[36:40]) <= int(end[:4]) and int(line[41:45]) >= int(start[:4]):
                        selected.add(line[:11])
        except (ValueError, TypeError):
            raise SourceFetchError("NOAA station inventory invalid", reason_code="invalid_response") from None
        if not selected or len(selected) > MAX_REGIONAL_STATIONS or set(AG_STATES) - {states[s] for s in selected}:
            raise SourceFetchError("NOAA regional station catalog incomplete or exceeds bound", reason_code="invalid_response")
        self.observation_exclusions = {"quality_flag":0}
        self.regional_coverage = {"acquisition_path":"ncei_daily_summaries_bulk",
            "catalog_station_count":len(selected), "catalog_station_ids":sorted(selected),
            "catalog_selection":"all_requested_state_stations_with_element_inventory_overlapping_observation_years",
            "catalog_stations_by_state":{state:sum(states[s]==state for s in selected) for state in AG_STATES},
            "acquisition_budget_seconds":REGIONAL_BUDGET_SECONDS}
        batches = [sorted(selected)[i:i+100] for i in range(0,len(selected),100)]
        def acquire(batch):
            with provider_budget("noaa", deadline):
                data = provider_request("noaa", "GET", BULK_URL, operation="bulk_daily_observations",
                    params={"dataset":"daily-summaries", "stations":",".join(batch),
                        "startDate":start, "endDate":end, "dataTypes":"TMAX,TMIN,PRCP",
                        "units":"standard", "includeAttributes":"true", "format":"json"}, timeout=(5,15)).json()
            if not isinstance(data,list) or not all(isinstance(row,dict) for row in data):
                raise SourceFetchError("NOAA bulk schema invalid", reason_code="invalid_response")
            rows, excluded, seen = [], 0, set()
            for row in data:
                station, day = row.get("STATION"), row.get("DATE")
                if station not in batch or not source_date(day) or not start <= day[:10] <= end or (station,day[:10]) in seen:
                    raise SourceFetchError("NOAA bulk observation identity invalid", reason_code="invalid_response")
                seen.add((station,day[:10]))
                for dtype in ("TMAX","TMIN","PRCP"):
                    value = row.get(dtype)
                    if value is None or value == "":
                        continue
                    attributes = row.get(dtype+"_ATTRIBUTES")
                    if not isinstance(attributes,str) or len(attributes.split(",")) < 3:
                        raise SourceFetchError("NOAA bulk quality attributes missing", reason_code="invalid_response")
                    if self._quality_flagged(attributes):
                        excluded += 1
                        continue
                    try:
                        value = float(value)
                    except (ValueError,TypeError):
                        raise SourceFetchError("NOAA bulk value invalid", reason_code="invalid_response") from None
                    if not source_number(value):
                        raise SourceFetchError("NOAA bulk value invalid", reason_code="invalid_response")
                    rows.append({"date":day[:10], "station":station, "datatype":dtype, "value":value})
            return rows, excluded
        regional = {state:[] for state in AG_STATES}
        errors = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(acquire,batch) for batch in batches]
            for future in futures:
                try:
                    rows, excluded = future.result()
                    self.observation_exclusions["quality_flag"] += excluded
                    for row in rows:
                        regional[states[row["station"]]].append(row)
                except SourceFetchError as exc:
                    errors.append(exc)
        if errors:
            error = errors[0]
            error.partial_data = {"states":regional}
            raise error
        return regional

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
        raw_count = len(data["results"])
        for row in data["results"]:
            if (not source_date(row.get("date")) or not source_text(row.get("datatype"))
                    or not source_text(row.get("station")) or not source_number(row.get("value"))
                    ):
                invalid = True
            elif self._quality_flagged(row.get("attributes")):
                self.observation_exclusions["quality_flag"] += 1
            else:
                valid_rows.append(row)
        if invalid:
            raise SourceFetchError("NOAA observation records invalid", reason_code="invalid_response",
                                   partial_data={"observations": valid_rows})
        return {**data, "results":valid_rows, "raw_count":raw_count}

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
