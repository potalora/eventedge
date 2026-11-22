"""Lean optional enrichment source without OpenBB SDK dependencies.

Provides:
- ``fetch_profile(ticker)`` / ``fetch_profiles(tickers)`` via direct ``yfinance``
- ``fetch_short_interest(ticker_or_tickers, as_of_date)`` via ``finra_query.py``
- ``fetch_fama_french(model="3")`` via direct Ken French ZIP/CSV (non-blocking)
"""
from __future__ import annotations

import calendar
from datetime import date, datetime
import io
import logging
import zipfile
from typing import Any

from .evidence import acquisition_time
from .fetch_errors import SourceFetchError, source_fetch_error, source_number, source_text
from .finra_schedule import validate_current_settlement
from .request_policy import (
    provider_call,
    provider_clock_time,
    provider_request,
    provider_subbudget,
    provider_timeout,
    read_bounded_response,
)
from .yfinance_source import ensure_yfinance_tz_cache, normalize_ticker

logger = logging.getLogger(__name__)

_FF_URLS = {
    "3": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_CSV.zip",
    "5": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_CSV.zip",
}
_MAX_FF_ZIP_BYTES = 4 * 1024 * 1024
_MAX_FF_CSV_BYTES = 16 * 1024 * 1024


def _normalize_profile(ticker: str, info: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(info, dict) or not info:
        raise SourceFetchError("Profile unavailable", reason_code="invalid_response")
    returned_symbol = info.get("symbol")
    if returned_symbol is not None and returned_symbol != normalize_ticker(ticker) and returned_symbol != ticker:
        raise SourceFetchError("Mismatched profile symbol", reason_code="invalid_response")
    quote_type = str(info.get("quoteType") or "").upper()
    is_etf = quote_type == "ETF"
    name = info.get("shortName") or info.get("longName") or info.get("name")
    sector = info.get("sector") or ""
    market_cap = info.get("marketCap")
    if market_cap is None:
        market_cap = info.get("totalAssets")
    if not source_text(name):
        raise SourceFetchError("Profile name unavailable", reason_code="invalid_response")
    if not is_etf and not source_text(sector):
        raise SourceFetchError("Profile sector unavailable", reason_code="invalid_response")
    if market_cap is not None and not source_number(market_cap, minimum=0):
        raise SourceFetchError("Profile market cap invalid", reason_code="invalid_response")
    result: dict[str, Any] = {
        "market_cap": float(market_cap) if market_cap is not None else None,
        "name": str(name).strip(),
        "description": str(info.get("longBusinessSummary") or info.get("description") or "")[:500],
    }
    if is_etf:
        result.update(
            asset_type="ETF",
            corporate_sector_status="not_applicable",
            lookthrough_sector_status="unknown",
        )
    else:
        result.update(
            sector=str(sector).strip(),
            industry=str(info.get("industry") or info.get("industryDisp") or "").strip(),
        )
    return result


class EnrichmentSource:
    """Direct optional enrichment source (yfinance profile, FINRA short interest, Ken French)."""

    name: str = "enrichment"
    requires_api_key: bool = False

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}

    def is_available(self) -> bool:
        return True

    def clear_cache(self) -> None:
        self._cache.clear()

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "")
        if method == "equity_profile":
            ticker = params.get("ticker") or params.get("symbol") or ""
            if not ticker:
                return {"error": "equity_profile requires 'ticker'", "reason_code": "invalid_response"}
            try:
                return self.fetch_profile(ticker)
            except Exception as exc:
                err = source_fetch_error("Profile enrichment failed", exc)
                return {"error": str(err), "reason_code": err.reason_code}
        if method == "equity_short_interest":
            ticker = params.get("ticker") or params.get("symbol") or ""
            if not ticker:
                return {"error": "equity_short_interest requires 'ticker'", "reason_code": "invalid_response"}
            return self.fetch_short_interest(ticker, as_of_date=params.get("as_of"))
        if method == "factors_fama_french":
            return self.fetch_fama_french(model=str(params.get("model", "3")))
        return {"error": f"Unknown method '{method}'", "reason_code": "invalid_response"}

    def fetch_profile(self, ticker: str) -> dict[str, Any]:
        if not isinstance(ticker, str) or not ticker.strip() or "," in ticker:
            raise SourceFetchError("Invalid profile symbol", reason_code="invalid_response")
        ckey = f"equity_profile|{ticker}"
        if ckey in self._cache:
            return self._cache[ckey]
        import yfinance as yf

        ensure_yfinance_tz_cache()

        def lookup() -> dict[str, Any]:
            tk = yf.Ticker(normalize_ticker(ticker))
            info = tk.get_info() if hasattr(tk, "get_info") else tk.info
            return _normalize_profile(ticker, info)

        result = provider_call("enrichment", "equity_profile", lookup, maximum_seconds=20)
        self._cache[ckey] = result
        return result

    def fetch_profiles(self, tickers: list[str]) -> dict[str, dict]:
        ordered = list(dict.fromkeys(tickers))
        profiles: dict[str, dict] = {}
        errors: dict[str, dict] = {}
        for ticker in ordered:
            try:
                profiles[ticker] = self.fetch_profile(ticker)
            except Exception as exc:
                err = source_fetch_error("Profile enrichment failed", exc)
                errors[ticker] = {"error": str(err), "reason_code": err.reason_code}
        return {"profiles": profiles, "errors": errors}

    def fetch_short_interest(
        self,
        tickers: str | list[str],
        as_of_date: Any = None,
        *,
        as_of: Any = None,
    ) -> dict[str, Any]:
        effective_as_of = as_of if as_of is not None else as_of_date
        if isinstance(tickers, str):
            batch = self._fetch_short_interest_batch([tickers], as_of=effective_as_of)
            if tickers in batch["short_interest"]:
                return batch["short_interest"][tickers]
            return batch["errors"][tickers]
        return self._fetch_short_interest_batch(list(tickers), as_of=effective_as_of)

    def _fetch_short_interest_batch(self, tickers: list[str], *, as_of: Any = None) -> dict[str, Any]:
        from . import finra_query

        acquired_at = datetime.fromisoformat(acquisition_time())
        symbols = list(dict.fromkeys(tickers))
        for symbol in symbols:
            key = f"equity_short_interest|{symbol}"
            if key in self._cache:
                try:
                    current = validate_current_settlement(
                        self._cache[key]["date"], acquired_at=acquired_at, as_of=as_of
                    )["status"] == "current"
                except (SourceFetchError, KeyError):
                    current = False
                if not current:
                    self._cache.pop(key)
        cached = {
            symbol: self._cache[f"equity_short_interest|{symbol}"]
            for symbol in symbols
            if f"equity_short_interest|{symbol}" in self._cache
        }
        pending = [symbol for symbol in symbols if symbol not in cached]
        acquisition = {
            "schema_version": 4,
            "requested_count": len(symbols),
            "cached_count": len(cached),
            "population_sha256": finra_query.population_digest(symbols),
            "attempts": [],
        }
        successes: dict[str, Any] = {}
        errors: dict[str, Any] = {}

        def run():
            attempt = finra_query.new_attempt()
            acquisition["attempts"].append(attempt)
            return finra_query.acquire(pending, attempt, acquired_at=acquired_at, as_of=as_of)

        if pending:
            try:
                with provider_subbudget(
                    "enrichment",
                    maximum_seconds=60,
                    absolute_deadline=provider_clock_time("enrichment") + 60,
                    max_attempts=2,
                ):
                    successes, errors = provider_call("enrichment", "equity_short_interest_batch", run)
                    provider_timeout("enrichment")
                    finra_query.validated_acquisition(acquisition)
                    provider_timeout("enrichment")
            except Exception as exc:
                error = source_fetch_error("FINRA public query acquisition failed", exc)
                if acquisition["attempts"]:
                    finra_query.fail_attempt(acquisition["attempts"][-1], error)
                successes = {}
                failure: dict[str, Any] = {"error": str(error), "reason_code": error.reason_code}
                if error.http_status is not None:
                    failure["http_status"] = error.http_status
                errors = {symbol: dict(failure) for symbol in pending}
            for symbol, value in successes.items():
                self._cache[f"equity_short_interest|{symbol}"] = value
        values = cached | successes
        return {
            "short_interest": {s: values[s] for s in symbols if s in values},
            "errors": {s: errors[s] for s in symbols if s in errors},
            "acquisition": finra_query.validated_acquisition(acquisition),
        }

    def fetch_fama_french(self, model: str = "3") -> dict[str, Any]:
        """Non-blocking direct Ken French monthly factor fetch; returns unavailable status on error."""
        if model not in _FF_URLS:
            return {"status": "unavailable", "error": "Unsupported Fama-French model", "reason_code": "invalid_response"}
        ckey = f"factors_fama_french|{model}"
        if ckey in self._cache:
            return self._cache[ckey]
        try:
            response = provider_request(
                "enrichment",
                "GET",
                _FF_URLS[model],
                operation="factors_fama_french",
                stream=True,
                allow_redirects=False,
                timeout=(5, 15),
            )
            try:
                raw_zip = read_bounded_response(response, provider="enrichment", max_bytes=_MAX_FF_ZIP_BYTES)
            finally:
                response.close()
            result = _parse_ken_french_zip(raw_zip, model=model, acquired_at=acquisition_time())
            self._cache[ckey] = result
            return result
        except Exception as exc:
            err = source_fetch_error("Fama-French enrichment unavailable", exc)
            return {"status": "unavailable", "error": str(err), "reason_code": err.reason_code}


def _parse_ken_french_zip(raw_zip: bytes, *, model: str, acquired_at: str) -> dict[str, Any]:
    acquired = datetime.fromisoformat(acquired_at)
    if acquired.utcoffset() is None:
        raise SourceFetchError("Fama-French acquisition clock invalid", reason_code="invalid_response")
    required = {"Mkt-RF", "SMB", "HML", "RF"} | ({"RMW", "CMA"} if model == "5" else set())
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
        csv_members = [m for m in zf.infolist() if m.filename.lower().endswith(".csv")]
        if len(csv_members) != 1 or csv_members[0].file_size > _MAX_FF_CSV_BYTES:
            raise SourceFetchError("Fama-French archive invalid", reason_code="invalid_response")
        text = zf.read(csv_members[0]).decode("utf-8", errors="strict")

    lines = text.splitlines()
    header_cols: list[str] | None = None
    records: dict[str, dict[str, float]] = {}
    months: set[tuple[int, int]] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if header_cols is not None and records:
                break  # Annual section follows monthly section after blank line.
            continue
        parts = [p.strip() for p in stripped.split(",")]
        if header_cols is None:
            if len(parts) >= 5 and "Mkt-RF" in parts and "RF" in parts:
                header_cols = parts[1:]
            continue
        ym = parts[0]
        if len(ym) != 6 or not ym.isdigit():
            if records:
                break
            continue
        year, month_num = int(ym[:4]), int(ym[4:])
        if not 1 <= month_num <= 12:
            raise SourceFetchError("Fama-French observation date invalid", reason_code="invalid_response")
        month_key = (year, month_num)
        if month_key in months or month_key >= (acquired.year, acquired.month):
            raise SourceFetchError("Fama-French monthly observations duplicated or incomplete", reason_code="invalid_response")
        if len(parts) - 1 != len(header_cols):
            raise SourceFetchError("Fama-French row schema mismatch", reason_code="invalid_response")
        row: dict[str, float] = {}
        for col_name, raw_val in zip(header_cols, parts[1:]):
            if col_name not in required:
                continue
            val = float(raw_val) / 100.0
            if not source_number(val):
                raise SourceFetchError("Fama-French factor invalid", reason_code="invalid_response")
            row[col_name] = val
        if not required <= set(row):
            raise SourceFetchError("Fama-French observations incomplete", reason_code="invalid_response")
        last_day = calendar.monthrange(year, month_num)[1]
        records[date(year, month_num, last_day).isoformat()] = row
        months.add(month_key)

    if not records:
        raise SourceFetchError("Fama-French observations unavailable", reason_code="invalid_response")
    ordered = sorted(records)
    history = {day: records[day] for day in ordered[-12:]}
    factors = {
        **records[ordered[-1]],
        "observation_date": ordered[-1],
        "frequency": "monthly",
        "model": model,
        "acquired_at": acquired_at,
        "available_at": acquired_at,
        "availability_basis": "forward_acquisition_time",
    }
    return {"factors": factors, "history": history}
