"""FRED (Federal Reserve Economic Data) source.

Provides macro indicators: credit spreads, unemployment, CPI, yield curve,
state-level data, etc. Free API key from fred.stlouisfed.org.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

from .fetch_errors import SourceFetchError, source_fetch_error, source_text, source_date, source_number

from .request_policy import provider_request, provider_timeout
from tradingagents.strategies.runtime_deadline import bounded_transport

logger = logging.getLogger(__name__)

# Common FRED series used by strategies
SERIES_MAP = {
    "hy_spread": "BAMLH0A0HYM2",       # ICE BofA US HY OAS
    "ig_spread": "BAMLC0A4CBBB",        # ICE BofA BBB Corp OAS
    "fed_funds": "FEDFUNDS",             # Fed Funds Rate
    "yield_curve": "T10Y2Y",             # 10Y-2Y spread
    "unemployment": "UNRATE",            # Unemployment Rate
    "cpi": "CPIAUCSL",                   # CPI All Urban
    "payrolls": "PAYEMS",                # Total Nonfarm Payrolls
    "initial_claims": "ICSA",            # Initial Jobless Claims
    "vix": "VIXCLS",                     # VIX (FRED version)
    "wti_spot": "DCOILWTICO",             # WTI Crude Oil Spot Price
    "gold_spot": "NASDAQQGLDI",           # Gold price index (London USD fix discontinued by FRED)
    "copper_spot": "PCOPPUSDM",           # Global Price of Copper
}


class FREDSource:
    """Data source backed by the public FRED observations REST API."""

    name: str = "fred"
    requires_api_key: bool = True

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("FRED_API_KEY", "")
        self._cache: dict[str, Any] = {}
        self._base_url = "https://api.stlouisfed.org/fred/series/observations"

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "series")
        dispatch = {
            "series": self._dispatch_series,
            "multi_series": self._dispatch_multi_series,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except SourceFetchError as exc:
            return {"error": str(exc)}
        except Exception:
            logger.error("FREDSource.fetch(%s) failed", method)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        if not self._api_key:
            return False
        return True

    @staticmethod
    def _transport_get(url, **options):
        """Requests inactivity timeout plus a reaped hard wall-clock worker."""
        import requests
        timeout = options["timeout"]
        try:
            result = bounded_transport({"kind": "http_get", "url": url,
                                        "params": options.get("params", {}), "timeout": timeout}, timeout)
        except TimeoutError:
            raise SourceFetchError("FRED transport deadline exhausted", reason_code="timeout") from None
        if result.get("error"):
            raise SourceFetchError("FRED transport failed", reason_code=result["error"])
        response = requests.Response()
        response.status_code = result["status_code"]
        response.headers.update(result["headers"])
        response._content = result["body"].encode()
        return response

    @staticmethod
    def _calendar_date(value):
        """FRED observation and real-time parameters are calendar days."""
        try:
            return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False

    def _get_observation_records(self, series_id, **params):
        """Exhaust declared pages under the existing shared provider deadline.

        A prefix, repeated page or changing total never certifies completeness.
        Both snapshots and history require the complete provider contract.
        """
        records, offset, total, seen = [], 0, None, set()
        for _ in range(1000):
            response = provider_request("fred", "get", self._base_url, operation=series_id,
                transport=self._transport_get,
                params={"api_key": self._api_key, "series_id": series_id, "file_type": "json",
                        **params, "offset": offset, "limit": 100000},
                timeout=provider_timeout("fred"))
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("observations"), list):
                raise SourceFetchError("FRED observations invalid", reason_code="invalid_response")
            observations = payload["observations"]
            count, page_offset, limit = (payload.get(key) for key in ("count", "offset", "limit"))
            if (type(count) is not int or not 0 <= count <= 1_000_000
                    or type(page_offset) is not int or page_offset != offset
                    or type(limit) is not int or not 1 <= limit <= 100000
                    or len(observations) > limit or offset + len(observations) > count
                    or (total is not None and total != count)
                    or (not observations and offset < count)):
                raise SourceFetchError("FRED pagination incomplete", reason_code="invalid_response")
            total = count
            for observation in observations:
                if not isinstance(observation, dict) or not self._calendar_date(observation.get("date")):
                    raise SourceFetchError("FRED observation invalid", reason_code="invalid_response")
                first, last = observation.get("realtime_start"), observation.get("realtime_end")
                if not self._calendar_date(first) or not self._calendar_date(last) or first > last:
                    raise SourceFetchError("FRED real-time bounds invalid", reason_code="invalid_response")
                value = observation.get("value")
                try:
                    number = None if value == "." else float(value)
                except (ValueError, TypeError):
                    raise SourceFetchError("FRED observation value invalid", reason_code="invalid_response") from None
                if number is not None and (isinstance(value, bool) or not source_number(number)):
                    raise SourceFetchError("FRED observation value invalid", reason_code="invalid_response")
                identity = (observation["date"], first, last)
                if identity in seen:
                    raise SourceFetchError("FRED repeated observation invalid", reason_code="invalid_response")
                seen.add(identity)
                records.append({"observation_date": observation["date"], "value": number,
                                "realtime_start": first, "realtime_end": last})
            offset += len(observations)
            if offset == total:
                return records
        raise SourceFetchError("FRED pagination incomplete", reason_code="invalid_response")

    @staticmethod
    def _capture_metadata():
        acquired_at = datetime.now(timezone.utc).isoformat()
        return {"acquired_at": acquired_at, "available_at": acquired_at,
                "availability_basis": "forward_acquisition_time"}

    def _get_series(self, series_id, **params):
        records = self._get_observation_records(series_id, **params)
        start, end, vintage = (params[key] for key in
                              ('observation_start', 'observation_end', 'realtime_start'))
        # Monthly observations are labelled by period start. FRED can include
        # the containing month when observation_start lies inside that month.
        # Use only declared frequencies; do not guess a frequency for arbitrary
        # caller-supplied series IDs (which may be quarterly or annual).
        from .state_economics_policy import SERIES as STATE_SERIES
        monthly = {'UNRATE', 'CPIAUCSL', 'PAYEMS', 'FEDFUNDS', 'PCOPPUSDM'} | set(STATE_SERIES.values())
        daily = {'BAMLH0A0HYM2', 'BAMLC0A4CBBB', 'T10Y2Y', 'VIXCLS', 'DCOILWTICO'}
        lower = date.fromisoformat(start).replace(day=1).isoformat() if series_id in monthly else start if series_id in daily else None
        if any(row['observation_date'] > min(end, vintage)
               or (lower is not None and row['observation_date'] < lower)
               or not row['realtime_start'] <= vintage <= row['realtime_end'] for row in records):
            raise SourceFetchError("FRED snapshot bounds invalid", reason_code="invalid_response")
        dates = [pd.Timestamp(row["observation_date"]) for row in records]
        if len(dates) != len(set(dates)):
            raise SourceFetchError("FRED snapshot dates duplicated", reason_code="invalid_response")
        data = pd.Series([float("nan") if row["value"] is None else row["value"] for row in records],
                         index=pd.DatetimeIndex(dates), dtype=float)
        data.attrs.update(observation_records=records, **self._capture_metadata())
        return data

    def fetch_observation_history(self, series_id: str, start: str, end: str, *, as_of: str) -> dict:
        """Plain initial/revised observations through a bound historical vintage.

        `realtime_start` is the provider's original known-since date, not the
        economic observation month or a fabricated release time. `available_at`
        remains this acquisition's clock: fresh history cannot backdate a trade.
        Callers can retain this envelope directly in the immutable source bundle.
        This endpoint does not cache current
        vintages, since a date-granularity vintage can update within the day.
        """
        if (not all(self._calendar_date(value) for value in (start, end, as_of))
                or start > end or end > as_of):
            raise SourceFetchError("FRED date range invalid", reason_code="invalid_response")
        try:
            records = self._get_observation_records(series_id,
                observation_start=start, observation_end=end,
                realtime_start="1776-07-04", realtime_end=as_of, output_type=1)
            if any(not start <= row["observation_date"] <= end
                   or row["realtime_start"] > as_of or row["realtime_end"] > as_of for row in records):
                raise SourceFetchError("FRED history bounds invalid", reason_code="invalid_response")
            return {"series_id": series_id, "source_url": self._base_url,
                    "observation_start": start, "observation_end": end,
                    "vintage_date": as_of, "realtime_start": "1776-07-04", "realtime_end": as_of,
                    "observations": records, **self._capture_metadata(),
                    "coverage": {"complete": True, "observation_count": len(records)}}
        except Exception as exc:
            classified = source_fetch_error("FRED history fetch failed", exc)
            raise SourceFetchError("FRED history fetch failed", reason_code=classified.reason_code,
                http_status=classified.http_status, failed_operations={series_id: classified.reason_code}) from None

    def fetch_series(
        self, series_id: str, start: str, end: str, *, as_of: str | None = None
    ) -> pd.Series:
        """Fetch a single FRED series."""
        as_of = as_of or end
        if not all(self._calendar_date(value) for value in (as_of,start,end)) or start > end:
            raise SourceFetchError("FRED date range invalid", reason_code="invalid_response")
        cache_key = f"series|{series_id}|{start}|{end}|{as_of}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            data = self._get_series(series_id, observation_start=start, observation_end=end, realtime_start=as_of, realtime_end=as_of)
            if not isinstance(data, pd.Series):
                raise SourceFetchError("FRED series response invalid", reason_code="invalid_response")
            if not data.empty:
                usable = data.map(source_number)
                if not usable.any() or (data.notna() & ~usable).any() or not usable.iloc[-1]:
                    raise SourceFetchError("FRED series observations invalid", reason_code="invalid_response",
                                           partial_data={series_id: data.loc[usable]})
            data.attrs.update({"observation_start": start, "observation_end": end, "vintage_date": as_of})
            self._cache[cache_key] = data
            return data
        except Exception as exc:
            classified = source_fetch_error("FRED series fetch failed", exc)
            safe_error = SourceFetchError(
                "FRED series fetch failed", reason_code=classified.reason_code,
                http_status=classified.http_status,
                failed_operations={series_id: classified.reason_code},
                partial_data=classified.partial_data,
            )
            logger.error("%s", safe_error)
            raise safe_error from None

    def fetch_multi_series(
        self, series_ids: list[str], start: str, end: str, *, as_of: str | None = None
    ) -> dict[str, pd.Series]:
        """Fetch multiple FRED series."""
        results: dict[str, pd.Series] = {}
        failures: dict[str, str] = {}
        http_statuses: dict[str, int] = {}
        for sid in series_ids:
            try:
                results[sid] = self.fetch_series(sid, start, end, as_of=as_of)
            except SourceFetchError as exc:
                if isinstance(exc.partial_data.get(sid), pd.Series):
                    results[sid] = exc.partial_data[sid]
                failures[sid] = exc.reason_code
                if exc.http_status is not None:
                    http_statuses[sid] = exc.http_status
        if failures:
            raise SourceFetchError(
                "FRED series fetch failed", reason_code="batch_failure",
                failed_operations=failures, failed_http_statuses=http_statuses,
                partial_data=results,
            )
        return results

    def fetch_credit_spreads(self, start: str, end: str, *, as_of: str | None = None) -> dict[str, pd.Series]:
        """Fetch HY and IG credit spread data."""
        return self.fetch_multi_series(
            [SERIES_MAP["hy_spread"], SERIES_MAP["ig_spread"]], start, end, as_of=as_of
        )

    def fetch_economic_indicators(self, start: str, end: str, *, as_of: str | None = None) -> dict[str, pd.Series]:
        """Fetch core economic indicators (unemployment, CPI, payrolls, claims)."""
        ids = [
            SERIES_MAP["unemployment"],
            SERIES_MAP["cpi"],
            SERIES_MAP["payrolls"],
            SERIES_MAP["initial_claims"],
            SERIES_MAP["fed_funds"],
            SERIES_MAP["vix"],
            SERIES_MAP["yield_curve"],
        ]
        return self.fetch_multi_series(ids, start, end, as_of=as_of)

    def clear_cache(self) -> None:
        self._cache.clear()

    def _dispatch_series(self, params: dict[str, Any]) -> dict[str, Any]:
        series_id = params.get("series_id", "")
        start = params.get("start", "")
        end = params.get("end", "")
        data = self.fetch_series(series_id, start, end, as_of=params.get("as_of"))
        return {"data": data.to_dict() if not data.empty else {}}

    def _dispatch_multi_series(self, params: dict[str, Any]) -> dict[str, Any]:
        series_ids = params.get("series_ids", [])
        start = params.get("start", "")
        end = params.get("end", "")
        try:
            results = self.fetch_multi_series(series_ids, start, end, as_of=params.get("as_of"))
        except SourceFetchError as exc:
            return {
                "data": {k: v.to_dict() for k, v in exc.partial_data.items()},
                "error": str(exc),
            }
        return {"data": {k: v.to_dict() for k, v in results.items()}}
