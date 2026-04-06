"""CFTC Commitments of Traders data source.

Wraps the `cot_reports` library to fetch COT positioning data.
No API key needed — data is public. Graceful ImportError skip
if cot_reports not installed.
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

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
        except Exception:
            logger.error("CFTCSource.fetch(%s) failed", method, exc_info=True)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        try:
            import cot_reports  # noqa: F401
            return True
        except ImportError:
            logger.info("cot_reports not installed — run: pip install cot_reports")
            return False

    def _fetch_raw_report(self, report_type: str = "disaggregated_futures") -> pd.DataFrame:
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
        df = cot.cot_year(year, cot_report_type=cot_type)

        self._cache[report_type] = df
        return df

    def _dispatch_cot_report(self, params: dict[str, Any]) -> dict[str, Any]:
        report_type = params.get("report_type", "disaggregated_futures")
        df = self._fetch_raw_report(report_type)
        return {"data": df.to_dict(orient="records")[:100]}

    def _dispatch_cot_positioning(self, params: dict[str, Any]) -> dict[str, Any]:
        commodities = params.get("commodities", list(COMMODITY_CODES.keys()))
        lookback_weeks = params.get("lookback_weeks", 52)

        df = self._fetch_raw_report("disaggregated_futures")

        results: dict[str, dict[str, Any]] = {}
        for commodity in commodities:
            code = COMMODITY_CODES.get(commodity)
            if code is None:
                logger.warning("Unknown commodity: %s", commodity)
                continue

            mask = df[COL_MARKET].str.contains(code, na=False)
            commodity_df = df[mask].copy()

            if commodity_df.empty:
                logger.warning("No COT data for %s (%s)", commodity, code)
                continue

            commodity_df["date"] = pd.to_datetime(commodity_df[COL_DATE])
            commodity_df = commodity_df.sort_values("date")
            commodity_df = commodity_df.tail(lookback_weeks)

            if len(commodity_df) < 4:
                logger.warning("Insufficient COT data for %s: %d weeks", commodity, len(commodity_df))
                continue

            commodity_df["net_spec"] = (
                commodity_df[COL_MM_LONG].astype(float)
                - commodity_df[COL_MM_SHORT].astype(float)
            )

            latest = commodity_df.iloc[-1]
            net_position = float(latest["net_spec"])

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
            }

        return results

    def clear_cache(self) -> None:
        self._cache.clear()
