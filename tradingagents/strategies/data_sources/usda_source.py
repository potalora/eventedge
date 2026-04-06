"""USDA NASS QuickStats data source.

Provides weekly crop condition ratings (Excellent/Good/Fair/Poor/Very Poor)
for corn, soybeans, and wheat across key agricultural states.

API docs: https://quickstats.nass.usda.gov/api/
Rate limits: 50,000 records per request. No explicit throttle.
"""
from __future__ import annotations

import logging
import os
import time
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

        max_retries = 3
        base_delay = 5.0
        data = None
        for attempt in range(max_retries + 1):
            try:
                resp = requests.get(BASE_URL, params=params, timeout=60)
                if resp.status_code == 200:
                    data = resp.json()
                    break
                elif resp.status_code >= 500:
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(
                            "USDA NASS returned %d (attempt %d/%d), retrying in %.0fs",
                            resp.status_code, attempt + 1, max_retries, delay,
                        )
                        time.sleep(delay)
                        continue
                    logger.warning("USDA NASS returned %d for %s/%d after %d retries", resp.status_code, commodity, year, max_retries)
                    return []
                else:
                    logger.warning("USDA NASS returned %d for %s/%d", resp.status_code, commodity, year)
                    return []
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "USDA NASS request failed (attempt %d/%d): %s, retrying in %.0fs",
                        attempt + 1, max_retries, exc, delay,
                    )
                    time.sleep(delay)
                    continue
                logger.error("USDA NASS request failed after %d retries", max_retries, exc_info=True)
                return []
            except requests.RequestException:
                logger.error("USDA NASS request failed", exc_info=True)
                return []
        if data is None:
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
