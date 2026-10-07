"""CFTC Commitments of Traders data source.

Wraps the `cot_reports` library to fetch COT positioning data.
No API key needed — data is public. Graceful ImportError skip
if cot_reports not installed.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from .request_policy import provider_call
from .fetch_errors import SourceFetchError, source_date, source_number, source_text

logger = logging.getLogger(__name__)

# Column names in the cot_reports library output (disaggregated report)
COL_MARKET = "Market_and_Exchange_Names"
COL_DATE = "Report_Date_as_YYYY-MM-DD"
COL_MM_LONG = "M_Money_Positions_Long_All"
COL_MM_SHORT = "M_Money_Positions_Short_All"

# Contract name strings from CFTC disaggregated reports.
# Validated against live data in test_commodity_macro_live.py.
COMMODITY_CODES = {
    "gold": "GOLD - COMMODITY EXCHANGE INC.",
    "silver": "SILVER - COMMODITY EXCHANGE INC.",
    "crude_oil": "WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE",
    "nat_gas": "HENRY HUB - NEW YORK MERCANTILE EXCHANGE",
    "copper": "COPPER- #1 - COMMODITY EXCHANGE INC.",
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
        except SourceFetchError as exc:
            return {**exc.partial_data, "error": str(exc)}
        except Exception:
            logger.error("CFTCSource.fetch(%s) failed", method)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        try:
            import cot_reports  # noqa: F401

            return True
        except ImportError:
            logger.info("cot_reports not installed — run: pip install cot_reports")
            return False

    def _fetch_raw_report(
        self, report_type: str = "disaggregated_futures"
    ) -> pd.DataFrame:
        """Fetch raw COT report. Cached per session (data is weekly)."""
        if report_type in self._cache:
            return self._cache[report_type]

        import cot_reports as cot

        # Map our friendly names to cot_reports library's expected strings
        cot_type_map = {
            "legacy_futures": "legacy_fut",
            "disaggregated_futures": "disaggregated_fut",
            "traders_in_financial_futures": "traders_in_financial_futures_fut",
        }
        cot_type = cot_type_map.get(report_type)
        if cot_type is None:
            raise ValueError(f"Unknown report type: {report_type}")

        from datetime import datetime

        year = datetime.now().year
        df = provider_call("cftc", "cot_report", lambda: cot.cot_year(year, cot_report_type=cot_type))
        if not isinstance(df, pd.DataFrame) or not {COL_MARKET, COL_DATE, COL_MM_LONG, COL_MM_SHORT}.issubset(df.columns):
            raise SourceFetchError("CFTC report invalid", reason_code="invalid_response")
        valid = df[COL_MARKET].map(source_text) & df[COL_DATE].map(lambda value: source_date(str(value)))
        for column in (COL_MM_LONG, COL_MM_SHORT):
            values = pd.to_numeric(df[column], errors="coerce")
            valid &= values.map(lambda value: source_number(value, minimum=0))
        if not valid.all():
            raise SourceFetchError("CFTC report records invalid", reason_code="invalid_response",
                                   partial_data={"raw_report": df[valid].copy()})

        self._cache[report_type] = df
        return df

    def _dispatch_cot_report(self, params: dict[str, Any]) -> dict[str, Any]:
        report_type = params.get("report_type", "disaggregated_futures")
        df = self._fetch_raw_report(report_type)
        return {"data": df.to_dict(orient="records")[:100]}

    def _dispatch_cot_positioning(self, params: dict[str, Any]) -> dict[str, Any]:
        commodities = params.get("commodities", list(COMMODITY_CODES.keys()))
        lookback_weeks = params.get("lookback_weeks", 52)

        results: dict[str, dict[str, Any]] = {}
        failures = {}
        try:
            df = self._fetch_raw_report("disaggregated_futures")
        except SourceFetchError as exc:
            df = exc.partial_data.get("raw_report")
            if not isinstance(df, pd.DataFrame):
                raise
            failures["cot_report"] = exc.reason_code
        for commodity in commodities:
            code = COMMODITY_CODES.get(commodity)
            if code is None:
                failures[commodity] = "invalid_response"
                continue

            mask = df[COL_MARKET].str.contains(code, na=False)
            commodity_df = df[mask].copy()

            if commodity_df.empty:
                failures[commodity] = "invalid_response"
                continue

            commodity_df["date"] = pd.to_datetime(commodity_df[COL_DATE])
            commodity_df = commodity_df.sort_values("date")
            commodity_df = commodity_df.tail(lookback_weeks)

            if len(commodity_df) < 4:
                failures[commodity] = "invalid_response"
                continue

            commodity_df["net_spec"] = commodity_df[COL_MM_LONG].astype(
                float
            ) - commodity_df[COL_MM_SHORT].astype(float)

            latest = commodity_df.iloc[-1]
            net_position = float(latest["net_spec"])
            report_date = latest["date"].date().isoformat()

            all_nets = commodity_df["net_spec"].values
            percentile = float((all_nets < net_position).sum() / len(all_nets))

            if len(commodity_df) >= 2:
                prior = float(commodity_df.iloc[-2]["net_spec"])
                wow_change = net_position - prior
            else:
                wow_change = 0.0

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
                "report_id": f"CFTC:{code}:{report_date}",
                "window_end": report_date,
            }

        if failures:
            raise SourceFetchError("CFTC commodity coverage incomplete", reason_code="batch_failure",
                                   failed_operations=failures, partial_data=results)
        return results

    def clear_cache(self) -> None:
        self._cache.clear()
