"""FRED (Federal Reserve Economic Data) source.

Provides macro indicators: credit spreads, unemployment, CPI, yield curve,
state-level data, etc. Free API key from fred.stlouisfed.org.
"""
from __future__ import annotations

import logging
import os
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

    def _get_series(self, series_id, **params):
        response = provider_request("fred", "get", self._base_url, operation=series_id,
                                    transport=self._transport_get,
                                    params={"api_key": self._api_key, "series_id": series_id,
                                            "file_type": "json", **params},
                                    timeout=provider_timeout("fred"))
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("observations"), list):
            raise SourceFetchError("FRED observations invalid", reason_code="invalid_response")
        dates, values = [], []
        for observation in payload["observations"]:
            if not isinstance(observation, dict) or not source_date(observation.get("date")):
                raise SourceFetchError("FRED observation invalid", reason_code="invalid_response")
            dates.append(pd.Timestamp(observation["date"]))
            value = observation.get("value")
            # FRED's documented missing-observation marker is a period.
            try:
                values.append(float("nan") if value == "." else float(value))
            except (ValueError, TypeError):
                raise SourceFetchError("FRED observation value invalid", reason_code="invalid_response") from None
        return pd.Series(values, index=pd.DatetimeIndex(dates), dtype=float)

    def fetch_series(
        self, series_id: str, start: str, end: str, *, as_of: str | None = None
    ) -> pd.Series:
        """Fetch a single FRED series."""
        as_of = as_of or end
        if not source_date(as_of) or not source_date(start) or not source_date(end) or start > end:
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
