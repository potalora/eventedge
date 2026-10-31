"""Version-guarded FINRA cache extraction using native preparation and models.

The database is read in one snapshot, never modified here. This adapter is tied
explicitly to the inspected native implementation; incompatible upgrades fail.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import importlib
from importlib import metadata
import json
import math
from pathlib import Path
import re
import sqlite3

import requests

from .fetch_errors import SourceFetchError, source_fetch_error
from .request_policy import provider_timeout, provider_clock_time
from .finra_schedule import correct_archive_dates, assess_archive_inventory

VERSIONS = {"openbb-finra": "1.6.1", "openbb-core": "1.6.13"}
ALIASES = {
    "symbol": "symbolCode", "issue_name": "issueName", "market_class": "marketClassCode",
    "current_short_position": "currentShortPositionQuantity",
    "previous_short_position": "previousShortPositionQuantity",
    "avg_daily_volume": "averageDailyVolumeQuantity", "days_to_cover": "daysToCoverQuantity",
    "change_pct": "changePercent", "change": "changePreviousNumber", "settlement_date": "settlementDate",
}
COLUMNS = tuple(ALIASES.values())
SCHEMA = (("index", "INTEGER"), ("symbolCode", "TEXT"), ("issueName", "TEXT"),
          ("marketClassCode", "TEXT"), ("currentShortPositionQuantity", "INTEGER"),
          ("previousShortPositionQuantity", "INTEGER"), ("averageDailyVolumeQuantity", "INTEGER"),
          ("daysToCoverQuantity", "REAL"), ("changePercent", "REAL"),
          ("changePreviousNumber", "INTEGER"), ("settlementDate", "TEXT"))
MAX_ROWS = 1_000_000
MAX_BYTES = 128 * 1024 * 1024
MAX_DATES = 1024
MAX_SYMBOLS = 100_000
ARCHIVE_URL = 'https://cdn.finra.org/equity/otcmarket/biweekly/shrt{}.csv'
PROBE_OUTCOMES = {'http_response', 'timeout', 'transport_error', 'probe_error', 'budget_exhausted'}
REASONS = {"timeout", "transport_error", "http_error", "invalid_response", "provider_error", "batch_failure"}


def invalid():
    return SourceFetchError("Unsupported or invalid FINRA bulk acquisition", reason_code="invalid_response")


def check_deadline():
    provider_timeout("openbb")


def validated_acquisition(value):
    """Validate the precise credential-free durable schema and return an isolated copy."""
    if type(value) is not dict or set(value) != {"schema_version", "requested_count", "cached_count", "attempts", "population_sha256"}:
        raise ValueError("Invalid FINRA acquisition metadata")
    if value["schema_version"] not in (1, 2, 3) or type(value["schema_version"]) is not int:
        raise ValueError("Invalid FINRA acquisition metadata")
    if type(value["population_sha256"]) is not str or not re.fullmatch("[a-f0-9]{64}", value["population_sha256"]):
        raise ValueError("Invalid FINRA acquisition metadata")
    for key in ("requested_count", "cached_count"):
        if type(value[key]) is not int or not 0 <= value[key] <= MAX_SYMBOLS:
            raise ValueError("Invalid FINRA acquisition metadata")
    if value["cached_count"] > value["requested_count"] or type(value["attempts"]) is not list or len(value["attempts"]) > 5:
        raise ValueError("Invalid FINRA acquisition metadata")
    keys = {"status", "reason_code", "versions", "cache_path", "row_count", "rowset_sha256",
            "cached_archive_dates", "missing_archive_dates", "cached_archive_dates_before",
            "missing_archive_dates_before", "required_archive_dates", "query_population_sha256",
            "prepare_started_at", "prepare_finished_at", "prepare_elapsed_seconds",
            "inventory_before_observed", "inventory_after_observed"}
    if value['schema_version'] >= 2:
        keys |= {'acquired_at', 'archive_coverage'}
    if value['schema_version'] == 3:
        keys |= {'current_archive_probe'}
    for attempt in value["attempts"]:
        if type(attempt) is not dict or set(attempt) != keys:
            raise ValueError("Invalid FINRA acquisition metadata")
        statuses = ('success', 'partial', 'error') if value['schema_version'] >= 2 else ('success', 'error')
        if type(attempt["status"]) is not str or (attempt["reason_code"] is not None and type(attempt["reason_code"]) is not str) or attempt["status"] not in statuses or attempt["reason_code"] not in REASONS | {None}:
            raise ValueError("Invalid FINRA acquisition metadata")
        if (attempt["status"] == "success") != (attempt["reason_code"] is None):
            raise ValueError("Invalid FINRA acquisition metadata")
        if attempt["versions"] not in ({}, VERSIONS):
            raise ValueError("Invalid FINRA acquisition metadata")
        path = attempt["cache_path"]
        if path is not None and (type(path) is not str or len(path) > 4096 or not Path(path).is_absolute() or any(ord(c) < 32 for c in path)):
            raise ValueError("Invalid FINRA acquisition metadata")
        if type(attempt["row_count"]) is not int or not 0 <= attempt["row_count"] <= MAX_ROWS:
            raise ValueError("Invalid FINRA acquisition metadata")
        for key in ("rowset_sha256", "query_population_sha256"):
            digest = attempt[key]
            if digest is not None and (type(digest) is not str or not re.fullmatch("[a-f0-9]{64}", digest)):
                raise ValueError("Invalid FINRA acquisition metadata")
        times = [attempt[key] for key in ("prepare_started_at", "prepare_finished_at", "prepare_elapsed_seconds")]
        if any(t is not None and (type(t) not in (int, float) or not math.isfinite(t) or t < 0) for t in times):
            raise ValueError("Invalid FINRA acquisition metadata")
        if any(t is not None for t in times) and (any(t is None for t in times) or times[1] < times[0] or not math.isclose(times[1]-times[0], times[2], abs_tol=1e-9)):
            raise ValueError("Invalid FINRA acquisition metadata")
        for key in ("cached_archive_dates", "missing_archive_dates", "cached_archive_dates_before", "missing_archive_dates_before", "required_archive_dates"):
            dates = attempt[key]
            if type(dates) is not list or len(dates) > MAX_DATES:
                raise ValueError("Invalid FINRA acquisition metadata")
            try:
                if any(type(day) is not str or date.fromisoformat(day).isoformat() != day for day in dates) or dates != sorted(set(dates)):
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError("Invalid FINRA acquisition metadata") from None
        if attempt["status"] in ("success", "partial") and (
                attempt["versions"] != VERSIONS or path is None or
                attempt["rowset_sha256"] is None or attempt["query_population_sha256"] is None or
                any(t is None for t in times) or not attempt["inventory_before_observed"] or
                not attempt["inventory_after_observed"]):
            raise ValueError("Invalid FINRA acquisition metadata")
        for suffix, cached_key, missing_key in (
                ("before", "cached_archive_dates_before", "missing_archive_dates_before"),
                ("after", "cached_archive_dates", "missing_archive_dates")):
            observed = attempt[f"inventory_{suffix}_observed"]
            if type(observed) is not bool:
                raise ValueError("Invalid FINRA acquisition metadata")
            if not observed and (attempt[cached_key] or attempt[missing_key]):
                raise ValueError("Invalid FINRA acquisition metadata")
            if observed and attempt[missing_key] != sorted(set(attempt["required_archive_dates"]) - set(attempt[cached_key])):
                raise ValueError("Invalid FINRA acquisition metadata")
        if value['schema_version'] >= 2:
            acquired = attempt['acquired_at']
            try:
                acquired_time = datetime.fromisoformat(acquired) if acquired is not None else None
                if acquired_time is not None and (acquired_time.tzinfo is None or acquired_time.utcoffset() is None
                                                   or acquired_time.isoformat() != acquired):
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError('Invalid FINRA acquisition metadata') from None
            coverage = attempt['archive_coverage']
            if coverage is None:
                if attempt['inventory_after_observed'] or attempt['status'] in ('success', 'partial'):
                    raise ValueError('Invalid FINRA acquisition metadata')
            else:
                try:
                    if type(coverage) is not dict or acquired_time is None or not attempt['inventory_after_observed']:
                        raise ValueError
                    expected = assess_archive_inventory(
                        attempt['required_archive_dates'], attempt['cached_archive_dates'],
                        acquired_at=acquired_time, as_of=date.fromisoformat(coverage['effective_as_of']))
                    if coverage != expected:
                        raise ValueError
                    if attempt['status'] in ('success', 'partial'):
                        if not coverage['current_complete']:
                            raise ValueError
                        if (attempt['status'] == 'success') != coverage['historical_complete']:
                            raise ValueError
                except (ValueError, TypeError, KeyError, SourceFetchError):
                    raise ValueError('Invalid FINRA acquisition metadata') from None
        if value['schema_version'] == 3 and attempt['current_archive_probe'] is not None:
            probe = attempt['current_archive_probe']
            try:
                if (type(probe) is not dict or set(probe) != {
                        'kind', 'settlement_date', 'url', 'observed_at', 'http_status', 'outcome'}
                        or probe['kind'] != 'post_download_http_probe'
                        or probe['outcome'] not in PROBE_OUTCOMES
                        or attempt['status'] != 'error'
                        or attempt['archive_coverage']['current_complete'] is not False
                        or probe['settlement_date'] != attempt['archive_coverage']['required_settlement_date']
                        or probe['url'] != ARCHIVE_URL.format(probe['settlement_date'].replace('-', ''))):
                    raise ValueError
                observed = datetime.fromisoformat(probe['observed_at'])
                settlement = date.fromisoformat(probe['settlement_date'])
                if (observed.utcoffset() is None or observed.isoformat() != probe['observed_at']
                        or observed < acquired_time or settlement.isoformat() != probe['settlement_date']):
                    raise ValueError
                status = probe['http_status']
                if ((probe['outcome'] == 'http_response') != (status is not None)
                        or status is not None and (type(status) is not int or not 100 <= status <= 599)):
                    raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError):
                raise ValueError('Invalid FINRA acquisition metadata') from None
    return json.loads(json.dumps(value, allow_nan=False))


def new_attempt():
    return {"status": "error", "reason_code": "provider_error", "versions": {}, "cache_path": None,
            "row_count": 0, "rowset_sha256": None, "cached_archive_dates": [], "missing_archive_dates": [],
            "cached_archive_dates_before": [], "missing_archive_dates_before": [], "required_archive_dates": [],
            "query_population_sha256": None, "prepare_started_at": None, "prepare_finished_at": None,
            "prepare_elapsed_seconds": None, "inventory_before_observed": False,
            "inventory_after_observed": False, "acquired_at": None, "archive_coverage": None,
            "current_archive_probe": None}


def probe_missing_current_archive(day):
    """Observe a later HTTP status, never the SDK result or archive validity.

    One header-only observation uses the remaining acquisition budget, with no
    redirects, retries, ambient authentication, body reads or cache writes.
    Failure to diagnose must not replace the original missing-archive failure.
    """
    probe = {'kind': 'post_download_http_probe', 'settlement_date': day,
             'url': ARCHIVE_URL.format(day.replace('-', '')),
             'observed_at': datetime.now(timezone.utc).isoformat(),
             'http_status': None, 'outcome': 'probe_error'}
    session = response = None
    try:
        timeout = min(3, provider_timeout('openbb'))
        session = requests.Session()
        session.trust_env = False
        response = session.get(probe['url'], headers={
            'Range': 'bytes=0-0', 'User-Agent': 'EventEdge-FINRA-diagnostic/1'},
            timeout=(timeout / 2, timeout / 2), allow_redirects=False, stream=True)
        status = response.status_code
        if type(status) is int and 100 <= status <= 599:
            probe.update(http_status=status, outcome='http_response')
    except SourceFetchError:
        probe['outcome'] = 'budget_exhausted'
    except requests.Timeout:
        probe['outcome'] = 'timeout'
    except requests.RequestException:
        probe['outcome'] = 'transport_error'
    except Exception:
        pass
    finally:
        for resource in (response, session):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
    return probe


def population_digest(symbols):
    return hashlib.sha256(json.dumps(sorted(symbols), ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def cached_dates_before(path):
    """Separate pre-preparation read; never claim this is the subsequent row snapshot."""
    check_deadline()
    if not path.exists():
        return []
    conn = None
    cursor = None
    interrupted = []
    def progress():
        try:
            check_deadline()
            return 0
        except SourceFetchError as exc:
            interrupted.append(exc)
            return 1
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=provider_timeout("openbb"))
        conn.set_progress_handler(progress, 1000)
        conn.execute("PRAGMA query_only=ON")
        schema = tuple((row[1], row[2]) for row in conn.execute("PRAGMA table_info(short_interest)"))
        if not schema:
            # Native preparation bootstraps an absent table in an existing empty DB.
            check_deadline()
            return []
        if schema != SCHEMA:
            raise invalid()
        cursor = conn.execute("SELECT DISTINCT settlementDate FROM short_interest")
        dates = set()
        for ordinal, row in enumerate(cursor):
            check_deadline()
            if ordinal >= MAX_DATES:
                raise invalid()
            try:
                dates.add(date.fromisoformat(str(row[0])).isoformat())
            except ValueError:
                continue
        check_deadline()
        return sorted(dates)
    except sqlite3.OperationalError:
        if interrupted:
            raise interrupted[0] from None
        raise invalid() from None
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.set_progress_handler(None, 0)
            conn.close()


def acquire(symbols, attempt, *, acquired_at=None, as_of=None):
    """Prepare once and completely read a bounded snapshot before transforming any symbol."""
    check_deadline()
    acquired_at = acquired_at if acquired_at is not None else datetime.now(timezone.utc)
    # The same captured wall clock governs archive requirements and row eligibility.
    attempt['acquired_at'] = acquired_at.isoformat()
    try:
        versions = {name: metadata.version(name) for name in VERSIONS}
    except metadata.PackageNotFoundError:
        raise invalid() from None
    if versions != VERSIONS:
        raise invalid()
    attempt["versions"] = versions
    storage = importlib.import_module("openbb_finra.utils.data_storage")
    model = importlib.import_module("openbb_finra.models.equity_short_interest")
    fetcher = model.FinraShortInterestFetcher
    if (model.FinraShortInterestData.__alias_dict__ != ALIASES or
            not all(callable(getattr(storage, name, None)) for name in ("get_data_from_date_and_store", "get_db_path", "get_short_interest_dates")) or
            not all(callable(getattr(fetcher, name, None)) for name in ("transform_query", "transform_data"))):
        raise invalid()
    check_deadline()
    path = Path(storage.get_db_path()).resolve()
    attempt["cache_path"] = str(path)
    expected = correct_archive_dates(storage.get_short_interest_dates(), acquired_at=acquired_at, as_of=as_of)
    attempt["required_archive_dates"] = expected
    attempt["query_population_sha256"] = population_digest(symbols)
    before = cached_dates_before(path)
    attempt["cached_archive_dates_before"] = before
    attempt["missing_archive_dates_before"] = sorted(set(expected) - set(before))
    attempt["inventory_before_observed"] = True
    check_deadline()
    attempt["prepare_started_at"] = provider_clock_time("openbb")
    try:
        # Native prepare_data computes weekend-only dates, excludes the current
        # month, and silently skips failed downloads. Reuse its native downloader
        # for the required current cycle without mutating SDK globals. The
        # native downloader populates its own cache. Historical backfill is not
        # needed for current rows and cannot consume the query's deadline.
        missing = set(expected) - set(before)
        current = assess_archive_inventory(expected, before, acquired_at=acquired_at, as_of=as_of)['required_settlement_date']
        for day in (current,) if current in missing else ():
            check_deadline()
            storage.get_data_from_date_and_store(day.replace('-', ''))
            check_deadline()
            if day == current:
                # The native downloader silently returns on a missing archive.
                # Do not spend the remaining deadline backfilling history when
                # the required current cycle is already known to be unavailable.
                refreshed = cached_dates_before(path)
                if current not in refreshed:
                    attempt['cached_archive_dates'] = refreshed
                    attempt['missing_archive_dates'] = sorted(set(expected) - set(refreshed))
                    attempt['inventory_after_observed'] = True
                    attempt['archive_coverage'] = assess_archive_inventory(
                        expected, refreshed, acquired_at=acquired_at, as_of=as_of)
                    attempt['current_archive_probe'] = probe_missing_current_archive(current)
                    raise SourceFetchError('Required published FINRA archive unavailable', reason_code='provider_error')
    finally:
        attempt["prepare_finished_at"] = provider_clock_time("openbb")
        attempt["prepare_elapsed_seconds"] = attempt["prepare_finished_at"] - attempt["prepare_started_at"]
    check_deadline()
    conn = None
    cursor = None
    interrupted = []
    def progress():
        try:
            check_deadline()
            return 0
        except SourceFetchError as exc:
            interrupted.append(exc)
            return 1
    grouped = {symbol: [] for symbol in symbols}
    encoded_rows = []
    size = 0
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=provider_timeout("openbb"))
        conn.set_progress_handler(progress, 1000)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        schema = tuple((row[1], row[2]) for row in conn.execute("PRAGMA table_info(short_interest)"))
        if schema != SCHEMA or conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) < len(symbols):
            raise invalid()
        cursor = conn.execute("SELECT DISTINCT settlementDate FROM short_interest")
        dates = set()
        for ordinal, row in enumerate(cursor):
            check_deadline()
            if ordinal >= MAX_DATES:
                raise invalid()
            try:
                dates.add(date.fromisoformat(str(row[0])).isoformat())
            except ValueError:
                # Bad dates remain in the queried history and fail their individual symbol.
                continue
            if len(dates) > MAX_DATES:
                raise invalid()
        cursor.close(); cursor = None
        attempt["cached_archive_dates"] = sorted(dates)
        attempt["missing_archive_dates"] = sorted(set(expected) - dates)
        attempt["inventory_after_observed"] = True
        attempt['archive_coverage'] = assess_archive_inventory(
            expected, attempt['cached_archive_dates'], acquired_at=acquired_at, as_of=as_of)
        if not attempt['archive_coverage']['current_complete']:
            raise SourceFetchError('Required published FINRA archive unavailable', reason_code='provider_error')
        check_deadline()
        query = "SELECT " + ",".join(COLUMNS) + " FROM short_interest WHERE symbolCode IN (" + ",".join("?" for _ in symbols) + ")"
        cursor = conn.execute(query, symbols)
        while True:
            check_deadline()
            rows = cursor.fetchmany(256)
            check_deadline()
            if not rows:
                break
            for row in rows:
                if row[0] not in grouped or len(encoded_rows) >= MAX_ROWS:
                    raise invalid()
                encoded = json.dumps([{"bytes": item.hex()} if isinstance(item, bytes) else item for item in row], ensure_ascii=True, separators=(",", ":")).encode()
                size += len(encoded)
                if size > MAX_BYTES:
                    raise invalid()
                encoded_rows.append(encoded)
                grouped[row[0]].append(dict(zip(COLUMNS, row)))
        check_deadline()
        attempt["row_count"] = len(encoded_rows)
        digest = hashlib.sha256()
        for encoded in sorted(encoded_rows):
            digest.update(len(encoded).to_bytes(8, "big"));digest.update(encoded)
        attempt["rowset_sha256"] = digest.hexdigest()
        check_deadline()
    except sqlite3.OperationalError:
        if interrupted:
            raise interrupted[0] from None
        raise invalid() from None
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.set_progress_handler(None, 0)
            conn.close()
    check_deadline()
    return fetcher, grouped
