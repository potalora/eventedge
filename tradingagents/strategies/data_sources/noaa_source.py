"""NOAA Climate Data Online (CDO) v2 source.

Provides temperature and precipitation anomaly data for US agricultural
regions. Free token from https://www.ncdc.noaa.gov/cdo-web/token.

Rate limits: 5 requests/second, 10,000 requests/day.
"""
from __future__ import annotations

import logging
import os
import socket
import time
from datetime import datetime, timedelta
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.connection import create_connection as _orig_create_connection

from .request_policy import provider_request, provider_budget, current_provider_deadline
from .fetch_errors import SourceFetchError, source_fetch_error, source_text, source_date, source_number

logger = logging.getLogger(__name__)

BASE_URL = "https://www.ncei.noaa.gov/cdo-web/api/v2"
MAX_STATE_OBSERVATIONS = 100_000

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


def _create_ipv6_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                            source_address=None, socket_options=None):
    """Create a connection preferring IPv6, falling back to IPv4.

    NOAA's IPv4 endpoints hang on some networks while IPv6 works fine.
    """
    host, port = address
    err = None

    # Try IPv6 first
    for af in (socket.AF_INET6, socket.AF_INET):
        try:
            records = socket.getaddrinfo(host, port, af, socket.SOCK_STREAM)
        except socket.gaierror:
            continue
        for res in records:
            af, socktype, proto, canonname, sa = res
            sock = None
            try:
                sock = socket.socket(af, socktype, proto)
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                if socket_options:
                    for opt in socket_options:
                        sock.setsockopt(*opt)
                sock.connect(sa)
                return sock
            except socket.error as e:
                err = e
                if sock is not None:
                    sock.close()

    if err is not None:
        raise err
    raise socket.error("getaddrinfo returned empty list")


class _IPv6PreferAdapter(HTTPAdapter):
    """HTTPAdapter that prefers IPv6 connections."""

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        # Patch the pool manager to use our IPv6-preferring connection func
        self.poolmanager.connection_pool_kw["socket_options"] = []

    def send(self, request, *args, **kwargs):
        import urllib3.util.connection
        _orig = urllib3.util.connection.create_connection
        urllib3.util.connection.create_connection = _create_ipv6_connection
        try:
            return super().send(request, *args, **kwargs)
        finally:
            urllib3.util.connection.create_connection = _orig


def _build_session() -> requests.Session:
    """Build a requests session that prefers IPv6 for NOAA endpoints."""
    session = requests.Session()
    adapter = _IPv6PreferAdapter(max_retries=0)
    session.mount("https://www.ncei.noaa.gov", adapter)
    return session


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
        if datatypes is None:
            datatypes = ["TMAX", "TMIN", "PRCP"]

        cache_key = f"daily|{state_fips}|{start}|{end}|{','.join(datatypes)}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        if current_provider_deadline("noaa") is None:
            with provider_budget("noaa", time.monotonic() + 60):
                return self.fetch_state_daily(state_fips, start, end, datatypes)

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
        """Compute agricultural weather anomaly summary across Corn Belt states.

        Returns a dict with:
        - heat_stress_days: count of days with TMAX > 95F across ag states
        - precip_deficit_pct: % below normal precipitation (negative = deficit)
        - frost_events: count of late-season frost events (TMIN < 32F after Apr 15)
        - avg_temp_anomaly_f: average temperature departure from seasonal mean
        - states_reporting: number of states with data
        """
        if current_provider_deadline("noaa") is None:
            with provider_budget("noaa", time.monotonic() + 60):
                return self.fetch_ag_weather_summary(date, lookback_days)
        try:
            end_date = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return {"error": f"Invalid date: {date}"}

        start_date = end_date - timedelta(days=lookback_days)
        start = start_date.strftime("%Y-%m-%d")
        end = end_date.strftime("%Y-%m-%d")

        month = end_date.month
        in_season = GROWING_SEASON[0] <= month <= GROWING_SEASON[1]

        heat_days = 0
        frost_events = 0
        all_tmax: list[float] = []
        all_prcp: list[float] = []
        states_with_data = 0

        failures, statuses = {}, {}
        for state, fips in AG_STATES.items():
            try:
                obs = self.fetch_state_daily(fips, start, end)
            except SourceFetchError as exc:
                obs = exc.partial_data.get("observations", [])
                failures[state] = exc.reason_code
                if exc.http_status is not None:
                    statuses[state] = exc.http_status
            if not obs:
                continue

            states_with_data += 1
            for o in obs:
                dtype = o.get("datatype", "")
                val = o.get("value")
                if val is None:
                    continue

                if dtype == "TMAX":
                    all_tmax.append(val)
                    if val > HEAT_STRESS_F:
                        heat_days += 1
                elif dtype == "TMIN":
                    obs_date = o.get("date", "")[:10]
                    if val < FROST_THRESHOLD_F and in_season:
                        try:
                            obs_dt = datetime.strptime(obs_date, "%Y-%m-%d")
                            if obs_dt.month >= 4 and obs_dt.day >= 15:
                                frost_events += 1
                        except ValueError:
                            pass
                elif dtype == "PRCP":
                    all_prcp.append(val)

        # Seasonal normals (approximate for Corn Belt)
        # Summer avg TMAX ~85F, avg daily precip ~0.12 inches
        seasonal_avg_tmax = 85.0 if in_season else 45.0
        seasonal_avg_daily_prcp = 0.12 if in_season else 0.08

        avg_tmax = sum(all_tmax) / len(all_tmax) if all_tmax else seasonal_avg_tmax
        temp_anomaly = avg_tmax - seasonal_avg_tmax

        avg_daily_prcp = sum(all_prcp) / len(all_prcp) if all_prcp else seasonal_avg_daily_prcp
        if seasonal_avg_daily_prcp > 0:
            precip_deficit_pct = ((avg_daily_prcp - seasonal_avg_daily_prcp) / seasonal_avg_daily_prcp) * 100
        else:
            precip_deficit_pct = 0.0

        summary = {
            "heat_stress_days": heat_days,
            "precip_deficit_pct": round(precip_deficit_pct, 1),
            "frost_events": frost_events,
            "avg_temp_anomaly_f": round(temp_anomaly, 1),
            "avg_tmax": round(avg_tmax, 1) if all_tmax else None,
            "avg_daily_prcp": round(avg_daily_prcp, 3) if all_prcp else None,
            "states_reporting": states_with_data,
            "in_growing_season": in_season,
            "lookback_days": lookback_days,
            "observations": len(all_tmax) + len(all_prcp),
        }

        if failures:
            raise SourceFetchError("NOAA state coverage incomplete", reason_code="batch_failure",
                                   failed_operations=failures, failed_http_statuses=statuses,
                                   partial_data=summary if states_with_data else {"states_reporting": 0, "observations": 0})
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
                    or not source_text(row.get("station")) or not source_number(row.get("value"))):
                invalid = True
            else:
                valid_rows.append(row)
        if invalid:
            raise SourceFetchError("NOAA observation records invalid", reason_code="invalid_response",
                                   partial_data={"observations": valid_rows})
        return data

    def _dispatch_ag_summary(self, params: dict[str, Any]) -> dict[str, Any]:
        date = params.get("date", datetime.now().strftime("%Y-%m-%d"))
        lookback = params.get("lookback_days", 30)
        return self.fetch_ag_weather_summary(date, lookback)

    def _dispatch_state_daily(self, params: dict[str, Any]) -> dict[str, Any]:
        state = params.get("state_fips", "")
        start = params.get("start", "")
        end = params.get("end", "")
        datatypes = params.get("datatypes")
        obs = self.fetch_state_daily(state, start, end, datatypes)
        return {"observations": obs, "count": len(obs)}
