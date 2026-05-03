"""USDA NASS QuickStats data source.

Provides weekly crop condition ratings (Excellent/Good/Fair/Poor/Very Poor)
for corn, soybeans, and wheat across key agricultural states.

Primary: QuickStats JSON API.
Fallback: ESMIS Crop Progress weekly text reports (used when QuickStats is
down — its app servers periodically stall while the publication mirror at
esmis.nal.usda.gov stays up).

API docs: https://quickstats.nass.usda.gov/api/
ESMIS:    https://esmis.nal.usda.gov/concern/publications/8336h188j
Rate limits: 50,000 records per request. No explicit throttle.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://quickstats.nass.usda.gov/api/api_GET/"
ESMIS_LANDING = "https://esmis.nal.usda.gov/concern/publications/8336h188j"

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

# State name → 2-letter code (ESMIS reports use full names; API uses codes)
_STATE_NAMES_TO_CODES = {
    "Alabama": "AL", "Arkansas": "AR", "Arizona": "AZ", "California": "CA",
    "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL",
    "Georgia": "GA", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN",
    "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA",
    "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
    "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC", "North Dakota": "ND",
    "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI",
    "Wyoming": "WY",
}

# Map a requested commodity to the ESMIS section name(s) that carry CONDITION data
_COMMODITY_TO_ESMIS_SECTIONS = {
    "CORN": ["Corn Condition"],
    "SOYBEANS": ["Soybeans Condition"],
    # Wheat reports split winter and spring. Both belong to "WHEAT" semantically.
    "WHEAT": ["Winter Wheat Condition", "Spring Wheat Condition"],
    "COTTON": ["Cotton Condition"],
    "RICE": ["Rice Condition"],
    "SORGHUM": ["Sorghum Condition"],
    "OATS": ["Oats Condition"],
    "BARLEY": ["Barley Condition"],
    "PEANUTS": ["Peanuts Condition"],
}


class USDASource:
    """Data source backed by USDA NASS QuickStats API."""

    name: str = "usda"
    requires_api_key: bool = True

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("USDA_NASS_API_KEY", "")
        self._cache: dict[str, list[dict]] = {}
        # Short-circuit further calls in this run after the first hard failure.
        # USDA NASS is occasionally unresponsive for hours and stalls the pipeline.
        self._unavailable = False

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

        # Short-circuit if we've already seen USDA fail this run.
        if self._unavailable:
            return []

        max_retries = 1
        base_delay = 3.0
        data = None
        for attempt in range(max_retries + 1):
            try:
                resp = requests.get(BASE_URL, params=params, timeout=15)
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
                    logger.warning("USDA NASS returned %d for %s/%d — trying ESMIS fallback", resp.status_code, commodity, year)
                    self._unavailable = True
                    fallback = self._esmis_fallback(commodity, year, states)
                    if fallback:
                        self._cache[cache_key] = fallback
                    return fallback
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
                logger.warning("USDA NASS unreachable after %d retries — trying ESMIS fallback: %s", max_retries, exc)
                self._unavailable = True
                fallback = self._esmis_fallback(commodity, year, states)
                if fallback:
                    self._cache[cache_key] = fallback
                return fallback
            except requests.RequestException:
                logger.warning("USDA NASS request failed — trying ESMIS fallback", exc_info=True)
                self._unavailable = True
                fallback = self._esmis_fallback(commodity, year, states)
                if fallback:
                    self._cache[cache_key] = fallback
                return fallback
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

    # ------------------------------------------------------------------
    # ESMIS fallback
    # ------------------------------------------------------------------

    _esmis_text_cache: str | None = None  # class-level cache: one fetch per process

    def _esmis_fetch_latest_report(self) -> str | None:
        """Fetch the most recent Crop Progress text report from ESMIS.

        Returns the raw report text, or None on failure.
        """
        if USDASource._esmis_text_cache is not None:
            return USDASource._esmis_text_cache

        try:
            resp = requests.get(ESMIS_LANDING, timeout=10)
            if resp.status_code != 200:
                logger.warning("ESMIS landing returned %d", resp.status_code)
                return None
        except requests.RequestException as exc:
            logger.warning("ESMIS landing fetch failed: %s", exc)
            return None

        # Find the first prog<NNYY>.txt link — they are listed newest-first.
        match = re.search(r'href="(/sites/default/release-files/\d+/prog\d+\.txt)"', resp.text)
        if not match:
            logger.warning("No prog*.txt link found on ESMIS landing")
            return None

        report_url = "https://esmis.nal.usda.gov" + match.group(1)
        try:
            r = requests.get(report_url, timeout=15)
            if r.status_code != 200:
                logger.warning("ESMIS report fetch returned %d for %s", r.status_code, report_url)
                return None
            USDASource._esmis_text_cache = r.text
            logger.info("ESMIS fallback: loaded %s (%d bytes)", report_url, len(r.text))
            return r.text
        except requests.RequestException as exc:
            logger.warning("ESMIS report fetch failed: %s", exc)
            return None

    @staticmethod
    def _parse_esmis_section(text: str, section_label: str) -> list[dict[str, Any]]:
        """Parse one '<Crop> Condition' section from an ESMIS Crop Progress report.

        Section header looks like:
            Winter Wheat Condition - Selected States: Week Ending April 26, 2026
            ----------------------------------------------------------------------------
                  State     : Very poor :   Poor    :   Fair    :   Good    : Excellent
            ----------------------------------------------------------------------------
                            :                          percent
                            :
            Arkansas .......:     1           4          34          48          13
            ...
        """
        # Locate the section
        # Example header: "Winter Wheat Condition - Selected States: Week Ending April 26, 2026"
        header_re = re.compile(
            re.escape(section_label) + r"\s*-\s*Selected States(?:: Week Ending ([A-Z][a-z]+\s+\d+,\s+\d{4}))?",
        )
        m = header_re.search(text)
        if not m:
            return []

        week_str = m.group(1)
        week_iso = ""
        if week_str:
            try:
                from datetime import datetime
                week_iso = datetime.strptime(week_str, "%B %d, %Y").strftime("%Y-%m-%d")
            except ValueError:
                pass

        # Walk lines after the header until the section ends (blank line then non-data, or new header)
        start = m.end()
        end_re = re.compile(r"\n[A-Z][a-z A-Z]+ (?:Condition|Planted|Emerged|Headed|Harvested) - Selected States")
        end_match = end_re.search(text, start)
        section_text = text[start:end_match.start() if end_match else None]

        # Data lines look like "Iowa ............:     1           4          34          48          13"
        # State name with dot padding, colon, then 5 ints (or "-").
        data_line_re = re.compile(
            r"^([A-Z][A-Za-z .]+?)\s*\.+\s*:\s*(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$",
        )
        rows: list[dict[str, Any]] = []
        for line in section_text.splitlines():
            line = line.rstrip()
            if not line or ":" not in line:
                continue
            mline = data_line_re.match(line)
            if not mline:
                continue
            name = mline.group(1).strip()
            # Skip aggregate rows like "18 States" or "Previous week" / "Previous year"
            if name in _STATE_NAMES_TO_CODES:
                code = _STATE_NAMES_TO_CODES[name]
                vals = []
                for raw in mline.group(2, 3, 4, 5, 6):
                    if raw == "-":
                        vals.append(0)
                    else:
                        try:
                            vals.append(int(raw))
                        except ValueError:
                            vals.append(0)
                rows.append({
                    "week_ending": week_iso,
                    "state": code,
                    "very_poor_pct": vals[0],
                    "poor_pct": vals[1],
                    "fair_pct": vals[2],
                    "good_pct": vals[3],
                    "excellent_pct": vals[4],
                })
        return rows

    def _esmis_fallback(
        self,
        commodity: str,
        year: int,  # noqa: ARG002 (year is fixed to current week's report)
        states: str | None,
    ) -> list[dict[str, Any]]:
        """Build crop-condition rows from the ESMIS weekly text report.

        Returns the same shape as fetch_crop_progress (commodity-tagged, state-coded rows).
        """
        sections = _COMMODITY_TO_ESMIS_SECTIONS.get(commodity.upper())
        if not sections:
            logger.info("ESMIS fallback: no section mapping for commodity %s", commodity)
            return []

        text = self._esmis_fetch_latest_report()
        if text is None:
            return []

        wanted_states: set[str] | None = None
        if states:
            wanted_states = {s.strip().upper() for s in states.split(",") if s.strip()}

        rows: list[dict[str, Any]] = []
        for section in sections:
            for row in self._parse_esmis_section(text, section):
                if wanted_states and row["state"] not in wanted_states:
                    continue
                row["commodity"] = commodity.upper()
                rows.append(row)

        rows.sort(key=lambda r: (r["week_ending"], r["state"]))
        if rows:
            logger.info(
                "ESMIS fallback: parsed %d rows for %s from %d section(s)",
                len(rows), commodity, len(sections),
            )
        return rows
