"""Direct Corn Belt ASOS station GHCNd daily acquisition (~50 KB, growing-season gated).

Replaces the 256 MB global `{year}.csv.gz` download with a targeted NCEI
Access daily-summaries CSV fetch for 22 primary Corn Belt airport/ASOS stations
covering all 10 AG_STATES, with Apr-Sep growing-season gating, ETag/daily local
file caching, and full transport error diagnostics.
"""
from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import date as calendar_date, datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .fetch_errors import SourceFetchError, source_fetch_error
from .request_policy import provider_request, provider_subbudget, provider_timeout

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

# Primary Corn Belt NWS airport/ASOS GHCNd stations (22 stations across all 10 AG_STATES).
CORN_BELT_STATIONS: dict[str, str] = {
    "USW00014933": "IA",  # Des Moines Intl
    "USW00014990": "IA",  # Cedar Rapids
    "USW00014943": "IA",  # Sioux City
    "USW00094846": "IL",  # Chicago O'Hare
    "USW00093822": "IL",  # Springfield
    "USW00014842": "IL",  # Peoria
    "USW00093819": "IN",  # Indianapolis Intl
    "USW00014827": "IN",  # Fort Wayne
    "USW00014942": "NE",  # Omaha Eppley
    "USW00014939": "NE",  # Lincoln
    "USW00014922": "MN",  # Minneapolis-St Paul
    "USW00014925": "MN",  # Rochester
    "USW00003928": "KS",  # Wichita
    "USW00013996": "KS",  # Topeka
    "USW00013994": "MO",  # St. Louis Lambert
    "USW00003947": "MO",  # Kansas City Intl
    "USW00014821": "OH",  # Columbus
    "USW00014820": "OH",  # Cleveland
    "USW00014944": "SD",  # Sioux Falls
    "USW00024025": "SD",  # Pierre
    "USW00014914": "ND",  # Fargo
    "USW00024011": "ND",  # Bismarck
}

# Growing season: April through September
GROWING_SEASON = (4, 9)

# Crop stress thresholds
HEAT_STRESS_F = 95  # Corn/soy stress threshold
FROST_THRESHOLD_F = 32  # Killing frost

NCEI_ACCESS_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
CATALOG_URL = "https://www.ncei.noaa.gov/pub/data/ghcn/daily"
YEARLY_URL = CATALOG_URL + "/by_year"
DELIVERY_POLICY = "ghcnd_yearly_v1"
NUMERIC_POLICY = "ghcnd_metric_exact_to_standard_v1"
REGIONAL_BUDGET_SECONDS = 60
MAX_OBSERVATION_LAG_DAYS = 7
MAX_LOOKBACK_DAYS = 60
MAX_CSV_BYTES = 8 * 1024 * 1024
MAX_SELECTED_RECORDS = 50_000
CHUNK_BYTES = 64 * 1024
ELEMENTS = ("TMAX", "TMIN", "PRCP")
_METRICS = (
    "heat_stress_days",
    "frost_events",
    "precip_deficit_pct",
    "avg_temp_anomaly_f",
    "avg_tmax",
    "avg_daily_prcp",
)
_INTEGER = re.compile(r"-?\d{1,6}\Z")


def current_vintage_date() -> str:
    """Actual New York calendar date; independent from requested observations."""
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _invalid(message="NOAA station response invalid"):
    return SourceFetchError(message, reason_code="invalid_response")


class _LocalHTTPResponse:
    """A completed body with explicit ownership of its private directory."""

    def __init__(self, status_code, headers, directory, body=None):
        self.status_code, self.headers = status_code, headers
        self.directory, self.body = directory, body

    def iter_content(self, chunk_size):
        if self.body is not None:
            while chunk := self.body.read(chunk_size):
                yield chunk

    def close(self):
        try:
            if self.body is not None:
                self.body.close()
        finally:
            self.directory.cleanup()


def _attach_diagnostics(exc: SourceFetchError, diag: dict[str, Any]) -> SourceFetchError:
    if diag:
        exc.partial_data = {**exc.partial_data, "_diagnostics": diag}
    return exc


def _yearly_http_get(url, *, stream, allow_redirects, timeout, headers, max_bytes):
    """Acquire in a killable worker; preserves real transport diagnostics."""
    provider_timeout("noaa")
    directory = tempfile.TemporaryDirectory(prefix="eventedge-noaa-http-")
    child, owned = None, True
    try:
        output = Path(directory.name) / "body"
        command = [
            sys.executable,
            str(Path(__file__).with_name("noaa_yearly_transport.py")),
            "--url", url,
            "--output", str(output),
            "--max-bytes", str(max_bytes),
            "--connect-timeout", str(timeout[0]),
            "--read-timeout", str(timeout[1]),
        ]
        if isinstance(headers, dict) and headers.get("If-None-Match"):
            command.extend(["--if-none-match", str(headers["If-None-Match"])])
        child_env = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            close_fds=True,
        )
        try:
            metadata, stderr_bytes = child.communicate(
                timeout=provider_timeout("noaa", maximum=REGIONAL_BUDGET_SECONDS)
            )
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            raise _attach_diagnostics(
                SourceFetchError("NOAA network acquisition deadline exhausted", reason_code="timeout"),
                {"error_class": "TimeoutExpired", "error_message": "NOAA transport worker timed out"},
            ) from None
        provider_timeout("noaa")
        stderr_excerpt = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()[:256]
        if child.returncode or not metadata or len(metadata) > 4096:
            raise _attach_diagnostics(
                _invalid("NOAA network worker metadata invalid"),
                {
                    "error_class": "WorkerProcessError",
                    "error_message": stderr_excerpt or f"returncode={child.returncode}",
                },
            )
        try:
            result = json.loads(metadata)
            if not isinstance(result, dict):
                raise ValueError
            reason = result.get("reason_code")
            if reason is not None:
                if reason not in ("timeout", "transport_error", "invalid_response", "provider_error"):
                    raise ValueError
                diag = {
                    k: str(result[k])[:200]
                    for k in ("error_class", "error_message")
                    if isinstance(result.get(k), str) and result.get(k)
                }
                if stderr_excerpt and "error_message" not in diag:
                    diag["error_message"] = stderr_excerpt
                raise _attach_diagnostics(
                    SourceFetchError("NOAA network acquisition failed", reason_code=reason),
                    diag,
                )
            status, safe_headers = result["status_code"], result["headers"]
            if type(status) is not int or not 100 <= status <= 599 or not isinstance(safe_headers, dict):
                raise ValueError
            if set(safe_headers) - {"Content-Length", "Content-Encoding", "Retry-After", "ETag"} or any(
                not isinstance(value, str) or len(value) > 128 for value in safe_headers.values()
            ):
                raise ValueError
            body = None
            if 200 <= status < 300:
                count, digest = result["bytes"], result["sha256"]
                if (
                    type(count) is not int
                    or not 0 < count <= max_bytes
                    or not isinstance(digest, str)
                    or not re.fullmatch("[0-9a-f]{64}", digest)
                ):
                    raise ValueError
                if output.stat().st_size != count:
                    raise ValueError
                body = output.open("rb")
            response = _LocalHTTPResponse(status, safe_headers, directory, body)
            owned = False
            return response
        except SourceFetchError:
            raise
        except (ValueError, TypeError, KeyError, OSError) as exc:
            raise _attach_diagnostics(
                _invalid("NOAA network worker metadata invalid"),
                {"error_class": type(exc).__name__, "error_message": stderr_excerpt or str(exc)[:200]},
            ) from None
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate()
        if owned:
            directory.cleanup()


class NOAAYearlySource:
    """Corn Belt station-roster GHCNd daily source with growing-season gating & daily/ETag caching."""

    name = "noaa"
    requires_api_key = False

    def __init__(self, *, cache_dir: str | Path | None = None):
        self.regional_coverage: dict[str, Any] = {}
        self.observation_exclusions: dict[str, int] = {}
        env_dir = os.environ.get("EVENTEDGE_NOAA_CACHE_DIR") or os.environ.get("EVENTEDGE_SOURCE_CACHE_DIR")
        if cache_dir is not None:
            self._cache_dir: Path | None = Path(cache_dir)
        elif env_dir:
            self._cache_dir = Path(env_dir) / "noaa"
        else:
            self._cache_dir = None

    def is_available(self) -> bool:
        return True

    def clear_cache(self) -> None:
        self.regional_coverage = {}
        self.observation_exclusions = {}

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if params.get("method", "ag_weather_summary") != "ag_weather_summary":
                raise _invalid("NOAA yearly method unsupported")
            return self.fetch_ag_weather_summary(
                params.get("date", current_vintage_date()),
                params.get("lookback_days", 30),
                vintage_as_of=params.get("vintage_as_of"),
            )
        except SourceFetchError as exc:
            return {**self._failure(exc).partial_data, "error": str(exc)}

    def _failure(self, exc: SourceFetchError) -> SourceFetchError:
        partial = dict(exc.partial_data)
        diag = partial.pop("_diagnostics", {})
        coverage = {**self.regional_coverage, **partial.get("coverage", {}), "complete": False}
        partial.update({field: None for field in _METRICS})
        cov_meta: dict[str, Any] = {
            "status": "failed",
            "reason_code": exc.reason_code,
            "http_status": exc.http_status,
            "attempts": exc.attempts,
            "failed_operations": exc.failed_operations,
            "failed_http_statuses": exc.failed_http_statuses,
        }
        if isinstance(diag, dict):
            for key in ("error_class", "error_message"):
                if diag.get(key):
                    cov_meta[key] = diag[key]
        partial.update(
            {
                "coverage": coverage,
                "delivery_policy": DELIVERY_POLICY,
                "numeric_policy": NUMERIC_POLICY,
                "_coverage": cov_meta,
            }
        )
        exc.partial_data = partial
        return exc

    @contextmanager
    def _budget(self):
        provider_timeout("noaa")
        with provider_subbudget(
            "noaa",
            maximum_seconds=REGIONAL_BUDGET_SECONDS,
            absolute_deadline=time.monotonic() + REGIONAL_BUDGET_SECONDS,
        ):
            provider_timeout("noaa")
            yield
            provider_timeout("noaa")

    def _station_csv_url(self, start: str, end: str) -> str:
        query = urlencode(
            {
                "dataset": "daily-summaries",
                "stations": ",".join(sorted(CORN_BELT_STATIONS)),
                "startDate": start,
                "endDate": end,
                "dataTypes": ",".join(ELEMENTS),
                "includeAttributes": "true",
                "format": "csv",
            }
        )
        return f"{NCEI_ACCESS_URL}?{query}"

    def _read_cache(self, cache_key: str, vintage: str) -> tuple[bytes | None, str | None]:
        if self._cache_dir is None:
            return None, None
        csv_path = self._cache_dir / f"{cache_key}.csv"
        meta_path = self._cache_dir / f"{cache_key}.meta.json"
        if not csv_path.is_file() or not meta_path.is_file():
            return None, None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            raw = csv_path.read_bytes()
            if meta.get("sha256") != hashlib.sha256(raw).hexdigest():
                return None, None
            etag = meta.get("etag") if isinstance(meta.get("etag"), str) else None
            if meta.get("vintage") == vintage:
                return raw, etag
            return None, etag
        except (OSError, ValueError):
            return None, None

    def _write_cache(self, cache_key: str, vintage: str, raw: bytes, etag: str | None) -> None:
        if self._cache_dir is None:
            return
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            csv_path = self._cache_dir / f"{cache_key}.csv"
            meta_path = self._cache_dir / f"{cache_key}.meta.json"
            csv_path.write_bytes(raw)
            meta_path.write_text(
                json.dumps(
                    {
                        "vintage": vintage,
                        "etag": etag,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "cached_at": _now(),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _download_station_csv(self, start: str, end: str, vintage: str) -> tuple[bytes, dict[str, Any]]:
        roster_hash = hashlib.sha256(",".join(sorted(CORN_BELT_STATIONS)).encode("ascii")).hexdigest()[:12]
        cache_key = f"ghcnd_cornbelt_{start}_{end}_{roster_hash}"
        cached_raw, cached_etag = self._read_cache(cache_key, vintage)
        url = self._station_csv_url(start, end)
        if cached_raw is not None:
            evidence = {
                "kind": "station_daily_csv",
                "bytes": len(cached_raw),
                "sha256": hashlib.sha256(cached_raw).hexdigest(),
                "acquired_at": _now(),
                "integrity_complete": True,
                "official_url": url,
                "cache_hit": "daily",
            }
            self.regional_coverage.setdefault("files", []).append(evidence)
            return cached_raw, evidence

        provider_timeout("noaa")
        started = _now()
        req_headers: dict[str, str] = {"Accept-Encoding": "identity"}
        if cached_etag:
            req_headers["If-None-Match"] = cached_etag
        response = provider_request(
            "noaa",
            "GET",
            url,
            operation="station_daily_csv",
            transport=_yearly_http_get,
            stream=True,
            allow_redirects=False,
            return_statuses=(304,) if cached_etag else (),
            timeout=(5, 15),
            headers=req_headers,
            max_bytes=MAX_CSV_BYTES,
        )
        try:
            resp_headers = getattr(response, "headers", {})
            etag = resp_headers.get("ETag")
            if getattr(response, "status_code", None) == 304 and self._cache_dir is not None:
                csv_path = self._cache_dir / f"{cache_key}.csv"
                if csv_path.is_file():
                    raw = csv_path.read_bytes()
                    self._write_cache(cache_key, vintage, raw, etag or cached_etag)
                    evidence = {
                        "kind": "station_daily_csv",
                        "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "acquired_at": _now(),
                        "integrity_complete": True,
                        "official_url": url,
                        "cache_hit": "etag_304",
                        "acquisition_started_at": started,
                        "acquisition_completed_at": _now(),
                    }
                    self.regional_coverage.setdefault("files", []).append(evidence)
                    return raw, evidence
                raise _invalid("NOAA 304 cache miss")

            length = resp_headers.get("Content-Length")
            if length is not None:
                if not isinstance(length, str) or not length.isdigit() or len(length) > 12:
                    raise _invalid("NOAA response length invalid")
                if int(length) > MAX_CSV_BYTES:
                    raise _invalid("NOAA response exceeds byte limit")
            if resp_headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise _invalid("NOAA response encoding unsupported")
            buf = bytearray()
            for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                provider_timeout("noaa")
                if not isinstance(chunk, bytes):
                    raise _invalid("NOAA response bytes invalid")
                if len(buf) + len(chunk) > MAX_CSV_BYTES:
                    raise _invalid("NOAA response exceeds byte limit")
                buf.extend(chunk)
            provider_timeout("noaa")
            raw = bytes(buf)
            if not raw or (length is not None and len(raw) != int(length)):
                raise _invalid("NOAA response incomplete")
            self._write_cache(cache_key, vintage, raw, etag)
            evidence = {
                "kind": "station_daily_csv",
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "acquired_at": _now(),
                "integrity_complete": False,
                "official_url": url,
                "cache_hit": "miss",
                "acquisition_started_at": started,
                "acquisition_completed_at": _now(),
            }
            self.regional_coverage.setdefault("files", []).append(evidence)
            return raw, evidence
        except SourceFetchError:
            raise
        except Exception as exc:
            raise _attach_diagnostics(
                source_fetch_error("NOAA station acquisition failed", exc),
                {"error_class": type(exc).__name__, "error_message": str(exc)[:200]},
            ) from None
        finally:
            response.close()

    def fetch_region_daily(self, start: str, end: str, *, vintage_as_of: str | None = None) -> dict[str, list[dict]]:
        """Acquire Corn Belt primary-station daily CSV observations for [start, end]."""
        vintage = current_vintage_date() if vintage_as_of is None else vintage_as_of
        self.regional_coverage = {
            "acquisition_path": "ncei_ghcnd_station_csv",
            "files": [],
            "acquisition_budget_seconds": REGIONAL_BUDGET_SECONDS,
            "delivery_policy": DELIVERY_POLICY,
            "numeric_policy": NUMERIC_POLICY,
            "catalog_station_count": len(CORN_BELT_STATIONS),
            "catalog_station_ids": sorted(CORN_BELT_STATIONS),
            "catalog_stations_by_state": {
                state: sum(st == state for st in CORN_BELT_STATIONS.values())
                for state in AG_STATES
            },
        }
        self.observation_exclusions = {"quality_flag": 0, "missing_value": 0}
        try:
            first, last = calendar_date.fromisoformat(start), calendar_date.fromisoformat(end)
            if first > last or (last - first).days >= MAX_LOOKBACK_DAYS + MAX_OBSERVATION_LAG_DAYS:
                raise _invalid("NOAA regional window invalid")
            with self._budget():
                raw, evidence = self._download_station_csv(start, end, vintage)
                try:
                    decoded_text = raw.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    raise _invalid("NOAA station CSV text invalid") from None
                reader = csv.DictReader(io.StringIO(decoded_text))
                if not reader.fieldnames or not {"STATION", "DATE", *ELEMENTS}.issubset(set(reader.fieldnames)):
                    raise _invalid("NOAA station CSV header invalid")
                regional: dict[str, list[dict]] = {state: [] for state in AG_STATES}
                seen: set[tuple[str, str, str]] = set()
                flag_counts: dict[str, dict[str, int]] = {"measurement": {}, "source": {}}
                trace_retained = 0
                raw_digest = hashlib.sha256(raw)
                row_count = 0
                for row in reader:
                    row_count += 1
                    if row_count % 512 == 0:
                        provider_timeout("noaa")
                    station = (row.get("STATION") or "").strip()
                    day_str = (row.get("DATE") or "").strip()
                    if station not in CORN_BELT_STATIONS:
                        raise _invalid("NOAA unexpected station in response")
                    try:
                        day = calendar_date.fromisoformat(day_str)
                    except ValueError:
                        raise _invalid("NOAA observation date invalid") from None
                    if not first <= day <= last:
                        raise _invalid("NOAA observation date outside window")
                    state = CORN_BELT_STATIONS[station]
                    for dtype in ELEMENTS:
                        raw_val = (row.get(dtype) or "").strip()
                        key = (station, day_str, dtype)
                        if key in seen:
                            raise _invalid("NOAA duplicate station observation")
                        seen.add(key)
                        if len(seen) > MAX_SELECTED_RECORDS:
                            raise _invalid("NOAA selected record limit exceeded")
                        attr_raw = row.get(f"{dtype}_ATTRIBUTES") or ""
                        attr_parts = attr_raw.split(",") if attr_raw else []
                        mflag = attr_parts[0].strip() if len(attr_parts) > 0 else ""
                        qflag = attr_parts[1].strip() if len(attr_parts) > 1 else ""
                        sflag = attr_parts[2].strip() if len(attr_parts) > 2 else ""
                        obs_time = attr_parts[3].strip() if len(attr_parts) > 3 else ""
                        flag_counts["measurement"][mflag] = flag_counts["measurement"].get(mflag, 0) + 1
                        flag_counts["source"][sflag] = flag_counts["source"].get(sflag, 0) + 1
                        if qflag:
                            self.observation_exclusions["quality_flag"] += 1
                            continue
                        if not raw_val or raw_val == "-9999":
                            self.observation_exclusions["missing_value"] += 1
                            continue
                        if not _INTEGER.fullmatch(raw_val):
                            raise _invalid("NOAA observation value invalid")
                        int_val = int(raw_val)
                        if dtype == "PRCP" and int_val < 0:
                            raise _invalid("NOAA precipitation invalid")
                        if mflag == "T":
                            trace_retained += 1
                        converted = int_val / 254 if dtype == "PRCP" else (int_val * 9 + 1600) / 50
                        regional[state].append(
                            {
                                "date": day.isoformat(),
                                "station": station,
                                "datatype": dtype,
                                "value": converted,
                                "measurement_flag": mflag,
                                "quality_flag": qflag,
                                "source_flag": sflag,
                                "observation_time": obs_time,
                            }
                        )
                evidence.update({"integrity_complete": True, "rows": row_count})
                self.regional_coverage.update(
                    {
                        "exclusions": dict(self.observation_exclusions),
                        "exclusion_scope": {
                            "start_date": start,
                            "end_date": end,
                            "selected_stations_only": True,
                            "datatypes": list(ELEMENTS),
                        },
                        "selected_records": len(seen),
                        "selected_raw_sha256": raw_digest.hexdigest(),
                        "flag_counts": flag_counts,
                        "trace_retained": trace_retained,
                    }
                )
                return regional
        except (TypeError, ValueError):
            raise self._failure(_invalid("NOAA regional window invalid")) from None
        except SourceFetchError as exc:
            raise self._failure(exc) from None

    def fetch_ag_weather_summary(
        self,
        date: str,
        lookback_days: int = 30,
        *,
        vintage_as_of: str | None = None,
    ) -> dict[str, Any]:
        self.clear_cache()
        try:
            end = calendar_date.fromisoformat(date)
            vintage = current_vintage_date() if vintage_as_of is None else vintage_as_of
            version = calendar_date.fromisoformat(vintage)
            if type(lookback_days) is not int or not 1 <= lookback_days <= MAX_LOOKBACK_DAYS or end > version:
                raise _invalid("NOAA observation window invalid")
            if vintage != current_vintage_date():
                raise SourceFetchError("NOAA historical vintage unavailable", reason_code="provider_error")
            in_season = GROWING_SEASON[0] <= end.month <= GROWING_SEASON[1]
            if not in_season:
                return {
                    "off_season_skipped": True,
                    "in_growing_season": False,
                    "states_reporting": 0,
                    "as_of": end.isoformat(),
                    "vintage_as_of": vintage,
                    "delivery_policy": DELIVERY_POLICY,
                    "numeric_policy": NUMERIC_POLICY,
                    "_coverage": {"status": "ok", "reason": "outside_growing_season"},
                    "coverage": {
                        "complete": True,
                        "off_season_skipped": True,
                        "reason": "outside_growing_season",
                        "as_of": end.isoformat(),
                    },
                }
            with self._budget():
                fetch_start = end - timedelta(days=lookback_days - 1 + MAX_OBSERVATION_LAG_DAYS)
                regional = self.fetch_region_daily(fetch_start.isoformat(), date, vintage_as_of=vintage)
                return self._summary(regional, end, lookback_days, vintage)
        except (TypeError, ValueError):
            raise self._failure(_invalid("NOAA observation window invalid")) from None
        except SourceFetchError as exc:
            raise self._failure(exc) from None

    def _summary(self, regional, reference, lookback, vintage):
        groups = {}
        for state in AG_STATES:
            grouped: dict[tuple[str, str], list[float]] = {}
            for index, row in enumerate(regional[state]):
                if index % 512 == 0:
                    provider_timeout("noaa")
                grouped.setdefault((row["date"], row["datatype"]), []).append(row["value"])
            groups[state] = grouped
        selected_lag, selected_end = None, reference
        for lag in range(MAX_OBSERVATION_LAG_DAYS + 1):
            candidate_end = reference - timedelta(days=lag)
            candidate_start = candidate_end - timedelta(days=lookback - 1)
            expected = {
                ((candidate_start + timedelta(days=i)).isoformat(), dtype)
                for i in range(lookback)
                for dtype in ELEMENTS
            }
            if all(expected <= grouped.keys() for grouped in groups.values()):
                selected_lag, selected_end = lag, candidate_end
                break
        if selected_lag is None:
            expected = {
                ((reference - timedelta(days=i)).isoformat(), dtype)
                for i in range(lookback)
                for dtype in ELEMENTS
            }
        start = selected_end - timedelta(days=lookback - 1)
        in_season = GROWING_SEASON[0] <= selected_end.month <= GROWING_SEASON[1]
        heat_dates, frost_dates, affected, tmax, prcp, missing, reporting = set(), set(), {}, [], [], {}, []
        observations = 0
        for state, grouped in groups.items():
            provider_timeout("noaa")
            absent = expected - grouped.keys()
            if absent:
                missing[state] = sorted(f"{day}:{dtype}" for day, dtype in absent)
            selected = {key: values for key, values in grouped.items() if key in expected}
            if selected:
                reporting.append(state)
            for (day, dtype), values in selected.items():
                value = sum(values) / len(values)
                observations += len(values)
                if dtype == "TMAX":
                    tmax.append(value)
                    if value > HEAT_STRESS_F:
                        heat_dates.add(day)
                        affected.setdefault(day, {}).setdefault("heat", []).append(state)
                elif dtype == "TMIN":
                    observed = calendar_date.fromisoformat(day)
                    if (
                        value < FROST_THRESHOLD_F
                        and GROWING_SEASON[0] <= observed.month <= GROWING_SEASON[1]
                        and observed > observed.replace(month=4, day=15)
                    ):
                        frost_dates.add(day)
                        affected.setdefault(day, {}).setdefault("frost", []).append(state)
                else:
                    prcp.append(value)
        avg_tmax = sum(tmax) / len(tmax) if tmax else None
        avg_prcp = sum(prcp) / len(prcp) if prcp else None
        normal_tmax, normal_prcp = (85.0, 0.12) if in_season else (45.0, 0.08)
        acquired = _now()
        summary = {
            "heat_stress_days": len(heat_dates),
            "frost_events": len(frost_dates),
            "precip_deficit_pct": round((avg_prcp - normal_prcp) / normal_prcp * 100, 1)
            if avg_prcp is not None
            else None,
            "avg_temp_anomaly_f": round(avg_tmax - normal_tmax, 1) if avg_tmax is not None else None,
            "avg_tmax": round(avg_tmax, 1) if avg_tmax is not None else None,
            "avg_daily_prcp": round(avg_prcp, 3) if avg_prcp is not None else None,
            "states_reporting": len(reporting),
            "in_growing_season": in_season,
            "lookback_days": lookback,
            "observations": observations,
            "day_unit": "regional_days",
            "affected_states_by_date": affected,
            "acquired_at": acquired,
            "available_at": acquired,
            "as_of": reference.isoformat(),
            "vintage_as_of": vintage,
            "observation_lag_days": selected_lag,
            "observation_date": selected_end.isoformat(),
            "start_date": start.isoformat(),
            "end_date": selected_end.isoformat(),
            "delivery_policy": DELIVERY_POLICY,
            "numeric_policy": NUMERIC_POLICY,
            "coverage": {
                **self.regional_coverage,
                "mode": "state_date_datatype_sample",
                "complete": not missing,
                "requested_states": list(AG_STATES),
                "states_reporting": reporting,
                "window_start": start.isoformat(),
                "window_end": selected_end.isoformat(),
                "as_of": reference.isoformat(),
                "observation_lag_days": selected_lag,
                "max_observation_lag_days": MAX_OBSERVATION_LAG_DAYS,
                "missing": missing,
                "station_aggregation": "arithmetic_mean_per_state_date_datatype",
            },
        }
        if missing:
            raise SourceFetchError(
                "NOAA state coverage incomplete",
                reason_code="batch_failure",
                failed_operations={state: "invalid_response" for state in missing},
                partial_data=summary,
            )
        provider_timeout("noaa")
        return summary
