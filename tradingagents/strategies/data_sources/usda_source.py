"""USDA NASS QuickStats data source.

Provides weekly crop condition ratings (Excellent/Good/Fair/Poor/Very Poor)
for corn, soybeans, and wheat across key agricultural states.

Primary: QuickStats JSON API.
Fallback: ESMIS Crop Progress weekly text reports (used when QuickStats is
unavailable). The publication mirror may lag the API; report observation
dates remain explicit and a single report is not weekly-change history.

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
    "SOYBEANS": ["Soybean Condition"],
    # Wheat reports split winter and spring. Both belong to "WHEAT" semantically.
    "WHEAT": ["Winter Wheat Condition", "Spring Wheat Condition"],
    "COTTON": ["Cotton Condition"],
    "RICE": ["Rice Condition"],
    "SORGHUM": ["Sorghum Condition"],
    "OATS": ["Oats Condition"],
    "BARLEY": ["Barley Condition"],
    "PEANUTS": ["Peanut Condition"],
}


def validated_condition_observations(weeks: Any) -> tuple[Any, dict]:
    """Return newest observed week and region/class G+E values (None if invalid).

    Shared by the screen and LLM prompt so neither can impute zero or substitute
    an older week when the newest observation is invalid.
    """
    from datetime import date

    observations = {}
    if not isinstance(weeks, list):
        return None, observations
    for row in weeks:
        try:
            week = date.fromisoformat(row["week_ending"])
            state = row["state"]
            crop_class = row.get("crop_class", "ALL CLASSES")
            if not all(isinstance(value, str) and value.strip() for value in (state, crop_class)):
                continue
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        key = (week, state, crop_class)
        value = None
        try:
            good = float(row["good_pct"])
            excellent = float(row["excellent_pct"])
            if row.get("condition_valid") is not False and (
                0 <= good <= 100 and 0 <= excellent <= 100
                and good + excellent <= 100
            ):
                value = good + excellent
        except (KeyError, TypeError, ValueError):
            pass
        if key in observations and observations[key] != value:
            value = None
        observations[key] = value
    latest = max((key[0] for key in observations), default=None)
    return latest, observations


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

        requested_states = sorted({
            state.strip().upper()
            for state in (states or AG_STATES).split(",") if state.strip()
        })
        cache_key = f"{commodity.upper()}|{year}|{','.join(requested_states)}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        params = {
            "key": self._api_key,
            "commodity_desc": commodity.upper(),
            "statisticcat_desc": "CONDITION",
            "agg_level_desc": "STATE",
            "freq_desc": "WEEKLY",
            "year": str(year),
            "state_alpha": requested_states,
            "format": "JSON",
        }

        # Bypass the failed primary, while keeping fallback available for each crop.
        if self._unavailable:
            fallback = self._esmis_fallback(commodity, year, states)
            if fallback:
                self._cache[cache_key] = fallback
            return fallback

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

        # Keep wheat classes separate while pivoting condition categories.
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        invalid_groups: set[tuple[str, str, str]] = set()
        for record in data.get("data", []):
            if not isinstance(record, dict):
                continue
            week = record.get("week_ending", "")
            state = record.get("state_alpha", "")
            crop_class = record.get("class_desc", "ALL CLASSES")
            unit = record.get("unit_desc", "")
            if not all(isinstance(value, str) and value.strip() for value in (week, state, crop_class, unit)):
                continue
            field = CONDITION_CATEGORIES.get(unit)
            if not field:
                continue
            key = (week, state, crop_class)
            if key not in grouped:
                grouped[key] = {
                    "week_ending": week,
                    "commodity": commodity.upper(),
                    "crop_class": crop_class,
                    "state": state,
                }
            try:
                value = int(record.get("Value", "").strip())
                if not 0 <= value <= 100:
                    raise ValueError("condition percentage outside range")
            except (ValueError, AttributeError):
                invalid_groups.add(key)
                continue
            if field in grouped[key] and grouped[key][field] != value:
                invalid_groups.add(key)
            grouped[key][field] = value

        # Preserve invalid dates without numerical values: dropping the newest
        # week would let downstream consumers silently reuse an older decline.
        observations = []
        for key, row in grouped.items():
            if (
                key in invalid_groups
                or "good_pct" not in row or "excellent_pct" not in row
                or row["good_pct"] + row["excellent_pct"] > 100
            ):
                row = {
                    field: row[field]
                    for field in ("week_ending", "commodity", "crop_class", "state")
                }
                row["condition_valid"] = False
            observations.append(row)
        weeks = sorted(
            observations,
            key=lambda row: (row["week_ending"], row["state"], row["crop_class"]),
        )
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
        year: int,
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

        wanted_states = {
            state.strip().upper()
            for state in (states or AG_STATES).split(",") if state.strip()
        }

        rows: list[dict[str, Any]] = []
        for section in sections:
            for row in self._parse_esmis_section(text, section):
                if (
                    row["state"] not in wanted_states
                    or not row["week_ending"].startswith(f"{year}-")
                ):
                    continue
                row["commodity"] = commodity.upper()
                row["crop_class"] = (
                    section.split(" Wheat")[0].upper()
                    if commodity.upper() == "WHEAT" else "ALL CLASSES"
                )
                rows.append(row)

        rows.sort(key=lambda r: (r["week_ending"], r["state"]))
        if rows:
            logger.info(
                "ESMIS fallback: parsed %d rows for %s from %d section(s)",
                len(rows), commodity, len(sections),
            )
        return rows
