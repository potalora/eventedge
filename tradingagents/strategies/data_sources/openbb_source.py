"""OpenBB Platform data source.

Provides a unified interface to multiple financial data providers via the
OpenBB SDK: equity profiles, estimates, insider trading, short interest,
government trades, options chains, SEC litigation, and Fama-French factors.

OpenBB is lazily imported on first fetch() call. If not installed, the source
reports unavailable and fetch() returns graceful errors.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime
from typing import Any

from .request_policy import provider_call, provider_timeout
from .fetch_errors import SourceFetchError, source_fetch_error, source_number, source_text
from .evidence import acquisition_time

logger = logging.getLogger(__name__)

def _getfield(item: Any, field: str, default: Any = None) -> Any:
    """Safely get a field from an OBBject result item."""
    return getattr(item, field, default)


def _profile_classification_valid(item: Any) -> bool:
    return ((_getfield(item, 'issue_type') == 'ETF' or source_text(_getfield(item, 'sector')))
            and (_getfield(item, 'market_cap') is None
                 or source_number(_getfield(item, 'market_cap'), minimum=0)))


def _profile_result(item: Any, *, observed_short_name: str | None = None) -> dict[str, Any]:
    """Normalize corporate profiles or explicitly identified native ETF rows."""
    is_etf = _getfield(item, 'issue_type') == 'ETF'
    native_name = _getfield(item, 'name')
    name = observed_short_name if native_name is None else native_name
    if not source_text(name) or not _profile_classification_valid(item):
        raise SourceFetchError('OpenBB profile classification unavailable or invalid', reason_code='invalid_response')
    result = {
        "market_cap": _getfield(item, "market_cap"),
        "name": name,
        "description": str(_getfield(item, "long_description", "") or
                           _getfield(item, "long_business_summary", "") or
                           _getfield(item, "description", ""))[:500],
    }
    if is_etf:
        # OpenBB maps Yahoo quoteType to issue_type. This describes the asset,
        # not its holdings: no corporate sector or look-through is inferred.
        # Omitting sector preserves the engine's existing Unknown risk group.
        result.update(asset_type="ETF", corporate_sector_status="not_applicable",
                      lookthrough_sector_status="unknown")
    else:
        result.update(sector=_getfield(item, "sector", ""),
                      industry=_getfield(item, "industry_category", "") or
                               _getfield(item, "industry", ""))
    if native_name is None and observed_short_name is not None:
        result.update(name_source="yfinance.quoteResponse.shortName",
                      name_fallback="native_long_name_missing")
    return result


def _profile_short_name(ticker: str, issue_type: str) -> str:
    """Read an exact Yahoo quote with the native helper and one capped budget.

    Avoid Ticker.get_info: its merger rewrites returned symbol to the requested
    symbol. The raw quote retains the provider's independent identity evidence.
    Native cookie negotiation remains cooperative; late results are rejected.
    """
    def lookup():
        from yfinance.data import YfData
        from yfinance.exceptions import YFRateLimitError
        from curl_cffi.requests.exceptions import HTTPError, Timeout, RequestException
        try:
            raw = YfData().get_raw_json(
                'https://query1.finance.yahoo.com/v7/finance/quote',
                params={'symbols': ticker, 'formatted': 'false'},
                timeout=provider_timeout('openbb', 5),
            )
        except YFRateLimitError:
            raise SourceFetchError('Yahoo profile name lookup failed', reason_code='http_error', http_status=429) from None
        except Timeout:
            raise SourceFetchError('Yahoo profile name lookup failed', reason_code='timeout') from None
        except HTTPError as error:
            response = getattr(error, 'response', None)
            failure = SourceFetchError('Yahoo profile name lookup failed', reason_code='http_error',
                                       http_status=getattr(response, 'status_code', None))
            # The policy consumes Retry-After and closes failed responses. This
            # transient reference never enters the safe diagnostics or cache.
            failure.response = response
            raise failure from None
        except RequestException:
            raise SourceFetchError('Yahoo profile name lookup failed', reason_code='transport_error') from None
        provider_timeout('openbb')
        envelope = raw.get('quoteResponse') if isinstance(raw, dict) else None
        rows = envelope.get('result') if isinstance(envelope, dict) else None
        if (not isinstance(envelope, dict) or envelope.get('error') is not None
                or not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or rows[0].get('symbol') != ticker or rows[0].get('quoteType') != issue_type
                or not source_text(rows[0].get('shortName'))):
            raise SourceFetchError('Yahoo profile name identity unavailable or invalid', reason_code='invalid_response')
        return rows[0]['shortName']
    return provider_call('openbb', 'equity_profile_name', lookup, maximum_seconds=5)


def normalize_short_interest(ticker, items, *, acquired_at=None, as_of=None):
    """Shared scalar/bulk native row validation; preserve unknown float percentage."""
    from .finra_schedule import validate_current_settlement
    acquired_at = acquired_at or datetime.fromisoformat(acquisition_time())
    context = validate_current_settlement(None, acquired_at=acquired_at, as_of=as_of)
    dated = []
    for item in items or []:
        symbol = _getfield(item, "symbol")
        if symbol is not None and symbol != ticker:
            raise SourceFetchError("Mismatched FINRA symbol", reason_code="invalid_response")
        try:
            settlement = date.fromisoformat(str(_getfield(item, "settlement_date", "")))
        except (TypeError, ValueError):
            raise SourceFetchError("Invalid FINRA settlement date", reason_code="invalid_response") from None
        validate_current_settlement(settlement, acquired_at=acquired_at, as_of=as_of)
        dated.append((settlement, item))
    # A provider may expose a future, not-yet-published cycle. It cannot replace
    # the current published row, nor authorize use before its release boundary.
    required = date.fromisoformat(context['required_settlement_date'])
    dated = [(settlement, item) for settlement, item in dated if settlement <= required]
    freshness = validate_current_settlement(max((day for day, _ in dated), default=None),
        acquired_at=acquired_at, as_of=as_of)
    if freshness['status'] != 'current':
        return {'error': f'Current published FINRA short interest unavailable for {ticker}',
                'reason_code': 'provider_error', 'freshness': freshness}
    latest = max(settlement for settlement, _ in dated)
    normalized = []
    for settlement, item in dated:
        if settlement != latest:
            continue
        short_pos = _getfield(item, "current_short_position")
        coverage = _getfield(item, "days_to_cover")
        avg_vol = _getfield(item, "avg_daily_volume", _getfield(item, "average_daily_volume"))
        try:
            if isinstance(short_pos, bool) or not math.isfinite(float(short_pos)) or float(short_pos) < 0:
                raise ValueError
            if coverage is None:
                if isinstance(avg_vol, bool) or not math.isfinite(float(avg_vol)) or float(avg_vol) <= 0:
                    raise ValueError
                coverage = float(short_pos) / float(avg_vol)
            if isinstance(coverage, bool) or not math.isfinite(float(coverage)) or float(coverage) < 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise SourceFetchError("Invalid FINRA coverage data", reason_code="invalid_response") from None
        short_pct = _getfield(item, "short_percent_of_float")
        if short_pct is not None:
            try:
                if isinstance(short_pct, bool) or not math.isfinite(float(short_pct)) or float(short_pct) < 0:
                    raise ValueError
                short_pct = float(short_pct)
            except (TypeError, ValueError, OverflowError):
                raise SourceFetchError("Invalid FINRA float percentage", reason_code="invalid_response") from None
        normalized.append({
            "short_interest": float(short_pos),
            "short_pct_of_float": short_pct,
            "days_to_cover": float(coverage),
            "date": latest.isoformat(),
        })
    result = normalized[0]
    if any(row != result for row in normalized[1:]):
        raise SourceFetchError("Conflicting latest FINRA rows", reason_code="invalid_response")
    return result


class OpenBBSource:
    """Data source backed by the OpenBB Platform SDK.

    Lazily initializes ``from openbb import obb`` on first use.
    Results are cached in-memory per session; call ``clear_cache()`` to reset.
    """

    name: str = "openbb"
    requires_api_key: bool = False

    def __init__(self, fmp_api_key: str | None = None) -> None:
        self._fmp_api_key = fmp_api_key or os.environ.get("FMP_API_KEY", "")
        self._obb: Any | None = None
        self._cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "")
        dispatch = {
            "equity_profile": self._equity_profile,
            "equity_estimates": self._equity_estimates,
            "equity_insider_trading": self._equity_insider_trading,
            "equity_short_interest": self._equity_short_interest,
            "equity_government_trades": self._equity_government_trades,
            "derivatives_options_unusual": self._derivatives_options_unusual,
            "regulators_sec_litigation": self._regulators_sec_litigation,
            "factors_fama_french": self._factors_fama_french,
            "sector_tickers": self._sector_tickers,
            "commodity_futures_curve": self._commodity_futures_curve,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            result = provider_call("openbb", method, lambda: handler(params), maximum_seconds=60)
            if "error" in result:
                result.setdefault("reason_code", "provider_error")
            return result
        except ImportError:
            logger.error("OpenBB SDK not installed")
            return {"error": "OpenBB SDK not installed", "reason_code": "provider_error"}
        except Exception as exc:
            error = source_fetch_error("OpenBB enrichment failed", exc)
            return {**error.partial_data, "error": str(error), "reason_code": error.reason_code}

    def is_available(self) -> bool:
        try:
            import importlib
            mod = importlib.import_module("openbb")
            return mod is not None
        except (ImportError, ModuleNotFoundError):
            return False

    def fetch_profiles(self, tickers: list[str]) -> dict[str, dict]:
        """Fetch every requested profile using sequential native batches of eight.

        Native responses arrive in completion order. Only exact, unique symbol
        attribution is cached; every unsuccessful input retains a safe failure.
        Existing provider scopes, if any, apply to every batch without reset.
        """
        ordered = list(dict.fromkeys(tickers))
        profiles: dict[str, dict] = {}
        errors: dict[str, dict] = {}
        pending = []
        for ticker in ordered:
            if not isinstance(ticker, str) or not ticker or "," in ticker:
                errors[ticker] = {"error": "Invalid profile symbol", "reason_code": "invalid_response"}
            elif f"equity_profile|{ticker}" in self._cache:
                profiles[ticker] = self._cache[f"equity_profile|{ticker}"]
            else:
                pending.append(ticker)
        for offset in range(0, len(pending), 8):
            batch = pending[offset:offset + 8]
            try:
                response = provider_call(
                    "openbb", "equity_profile",
                    lambda: self._get_obb().equity.profile(symbol=",".join(batch), provider="yfinance"),
                    maximum_seconds=60,
                )
                by_symbol: dict[str, list] = {ticker: [] for ticker in batch}
                for item in response.results or []:
                    symbol = _getfield(item, "symbol")
                    if not isinstance(symbol, str) or symbol not in by_symbol:
                        raise SourceFetchError("Unattributed OpenBB profile response", reason_code="invalid_response")
                    by_symbol[symbol].append(item)
                for ticker, items in by_symbol.items():
                    if len(items) != 1:
                        errors[ticker] = {
                            "error": "Missing OpenBB profile" if not items else "Duplicate OpenBB profile",
                            "reason_code": "invalid_response",
                        }
                        continue
                    try:
                        result = self._complete_profile(ticker, items[0])
                    except SourceFetchError as error:
                        errors[ticker] = {'error': str(error), 'reason_code': error.reason_code}
                        continue
                    profiles[ticker] = result
                    self._cache[f"equity_profile|{ticker}"] = result
            except Exception as exc:
                error = source_fetch_error("OpenBB profile batch failed", exc)
                for ticker in batch:
                    errors[ticker] = {"error": str(error), "reason_code": error.reason_code}
        return {
            "profiles": {ticker: profiles[ticker] for ticker in ordered if ticker in profiles},
            "errors": {ticker: errors[ticker] for ticker in ordered if ticker in errors},
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _complete_profile(self, ticker: str, item: Any) -> dict[str, Any]:
        """Repair only a missing native long name on an otherwise valid row."""
        if _getfield(item, 'symbol') != ticker:
            raise SourceFetchError('OpenBB profile symbol unavailable or ambiguous', reason_code='invalid_response')
        short_name = None
        issue_type = _getfield(item, 'issue_type')
        if (_getfield(item, 'name') is None and issue_type in ('EQUITY', 'ETF')
                and _profile_classification_valid(item)):
            short_name = _profile_short_name(ticker, issue_type)
        return _profile_result(item, observed_short_name=short_name)

    def _get_obb(self) -> Any:
        """Lazy-init and return the OpenBB ``obb`` singleton."""
        if self._obb is None:
            from openbb import obb  # noqa: WPS433

            # Configure FMP key if provided
            if self._fmp_api_key:
                try:
                    obb.user.credentials.fmp_api_key = self._fmp_api_key
                except Exception:
                    logger.debug("Could not set FMP API key on obb instance")
            self._obb = obb
        return self._obb

    def _cache_key(self, method: str, params: dict[str, Any]) -> str:
        """Build a stable cache key from method + relevant params."""
        # Include only params that affect the API call (exclude 'method')
        parts = [method]
        for k in sorted(params.keys()):
            if k == "method":
                continue
            parts.append(f"{k}={params[k]}")
        return "|".join(parts)

    def clear_cache(self) -> None:
        """Clear the in-memory cache."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Method handlers
    # ------------------------------------------------------------------

    def _equity_profile(self, params: dict[str, Any]) -> dict[str, Any]:
        ticker = params.get("ticker") or params.get("symbol")
        if not ticker:
            return {"error": "equity_profile requires 'ticker'"}

        ckey = f"equity_profile|{ticker}"
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.equity.profile(symbol=ticker, provider="yfinance")
        if not resp.results:
            return {"error": f"No profile data for {ticker}"}

        if len(resp.results) != 1 or _getfield(resp.results[0], 'symbol') != ticker:
            raise SourceFetchError('OpenBB profile symbol unavailable or ambiguous', reason_code='invalid_response')
        item = resp.results[0]
        try:
            result = self._complete_profile(ticker, item)
        except SourceFetchError as error:
            # The name lookup already owns retries; do not retry the entire
            # native profile call after a failed per-symbol completion.
            return {'error': str(error), 'reason_code': error.reason_code}
        self._cache[ckey] = result
        return result

    def _equity_estimates(self, params: dict[str, Any]) -> dict[str, Any]:
        ticker = params.get("ticker") or params.get("symbol")
        if not ticker:
            return {"error": "equity_estimates requires 'ticker'"}

        ckey = f"equity_estimates|{ticker}"
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.equity.estimates.consensus(symbol=ticker, provider="fmp")
        if not resp.results:
            return {"error": f"No estimates for {ticker}"}

        item = resp.results[0]
        result = {
            "consensus_eps": _getfield(item, "estimated_eps_avg")
                             or _getfield(item, "target_consensus"),
            "consensus_revenue": _getfield(item, "estimated_revenue_avg"),
            "price_target_mean": _getfield(item, "target_consensus")
                                 or _getfield(item, "price_target_average"),
            "price_target_high": _getfield(item, "target_high")
                                 or _getfield(item, "price_target_high"),
            "price_target_low": _getfield(item, "target_low")
                                or _getfield(item, "price_target_low"),
            "num_analysts": _getfield(item, "number_of_analysts", 0)
                            or _getfield(item, "target_median", 0),
        }
        self._cache[ckey] = result
        return result

    def _equity_insider_trading(self, params: dict[str, Any]) -> dict[str, Any]:
        ticker = params.get("ticker") or params.get("symbol")
        if not ticker:
            return {"error": "equity_insider_trading requires 'ticker'"}

        ckey = f"equity_insider_trading|{ticker}"
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.equity.ownership.insider_trading(
            symbol=ticker, limit=100, provider="sec"
        )
        trades = []
        for item in resp.results or []:
            shares = _getfield(item, "securities_transacted", 0)
            price = _getfield(item, "price", 0.0)
            trades.append({
                "owner": _getfield(item, "owner_name", ""),
                "title": _getfield(item, "owner_title", ""),
                "transaction_type": _getfield(item, "transaction_type", ""),
                "shares": shares,
                "price": price,
                "value": (shares or 0) * (price or 0),
                "date": str(_getfield(item, "filing_date", "")),
                "ownership_type": _getfield(item, "owner_type", ""),
            })

        result = {"trades": trades}
        self._cache[ckey] = result
        return result

    def fetch_short_interest(self, tickers: list[str], *, as_of=None) -> dict[str, Any]:
        """Acquire the complete native FINRA history once for uncached exact symbols."""
        from .finra_bulk import acquire, check_deadline, new_attempt, validated_acquisition, population_digest
        from .finra_schedule import validate_current_settlement
        acquired_at = datetime.fromisoformat(acquisition_time())
        symbols = list(dict.fromkeys(tickers))
        for symbol in symbols:
            key = f'equity_short_interest|{symbol}'
            if key in self._cache:
                try:
                    current = validate_current_settlement(self._cache[key]['date'],
                        acquired_at=acquired_at, as_of=as_of)['status'] == 'current'
                except (SourceFetchError, KeyError):
                    current = False
                if not current:
                    self._cache.pop(key)
        cached = {symbol: self._cache[f"equity_short_interest|{symbol}"]
                  for symbol in symbols if f"equity_short_interest|{symbol}" in self._cache}
        pending = [symbol for symbol in symbols if symbol not in cached]
        acquisition = {"schema_version": 3, "requested_count": len(symbols),
                       "cached_count": len(cached), "attempts": [], "population_sha256": population_digest(symbols)}
        successes, errors = {}, {}
        def run():
            attempt = new_attempt()
            acquisition["attempts"].append(attempt)
            published = []
            try:
                check_deadline()
                self._get_obb()
                check_deadline()
                fetcher, histories = acquire(pending, attempt, acquired_at=acquired_at, as_of=as_of)
                values, failures = {}, {}
                for symbol in pending:
                    check_deadline()
                    try:
                        query = fetcher.transform_query({"symbol": symbol})
                        check_deadline()
                        if getattr(query, "symbol", None) != symbol:
                            raise SourceFetchError("Mismatched FINRA query symbol", reason_code="invalid_response")
                        rows = fetcher.transform_data(query, histories[symbol])
                        check_deadline()
                        result = normalize_short_interest(symbol, rows, acquired_at=acquired_at, as_of=as_of)
                        if "error" in result:
                            failures[symbol] = result
                        else:
                            values[symbol] = result
                    except Exception as exc:
                        error = source_fetch_error("Invalid FINRA symbol history", exc)
                        # Native model validation is a per-symbol invalid response.
                        if isinstance(exc, (ValueError, TypeError)):
                            error = SourceFetchError("Invalid FINRA symbol history", reason_code="invalid_response")
                        if error.reason_code == "timeout":
                            raise error
                        failures[symbol] = {"error": str(error), "reason_code": error.reason_code}
                    check_deadline()
                check_deadline()
                for symbol, value in values.items():
                    self._cache[f"equity_short_interest|{symbol}"] = value
                    published.append(symbol)
                check_deadline()
                historical_complete = attempt['archive_coverage']['historical_complete']
                attempt["status"], attempt["reason_code"] = ('success', None) if historical_complete else ('partial', 'provider_error')
                return values, failures
            except Exception as exc:
                for symbol in published:
                    self._cache.pop(f"equity_short_interest|{symbol}", None)
                error = source_fetch_error("FINRA bulk acquisition failed", exc)
                attempt["reason_code"] = error.reason_code
                raise error from None
        if pending:
            try:
                successes, errors = provider_call("openbb", "equity_short_interest_batch", run, maximum_seconds=60)
            except Exception as exc:
                error = source_fetch_error("FINRA bulk acquisition failed", exc)
                errors = {symbol: {"error": str(error), "reason_code": error.reason_code} for symbol in pending}
        values = cached | successes
        return {"short_interest": {symbol: values[symbol] for symbol in symbols if symbol in values},
                "errors": {symbol: errors[symbol] for symbol in symbols if symbol in errors},
                "acquisition": validated_acquisition(acquisition)}

    def _equity_short_interest(self, params: dict[str, Any]) -> dict[str, Any]:
        """Use the same bounded current-cycle acquisition for scalar callers."""
        ticker = params.get("ticker") or params.get("symbol")
        if not ticker:
            return {"error": "equity_short_interest requires 'ticker'"}
        batch = self.fetch_short_interest([ticker], as_of=params.get('as_of'))
        if ticker in batch['short_interest']:
            return batch['short_interest'][ticker]
        return batch['errors'][ticker]

    def _equity_government_trades(self, params: dict[str, Any]) -> dict[str, Any]:
        ckey = "equity_government_trades"
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.equity.ownership.government_trades(provider="fmp")
        trades = []
        for item in resp.results or []:
            trades.append({
                "ticker": _getfield(item, "symbol", "")
                          or _getfield(item, "ticker", ""),
                "representative": _getfield(item, "representative", ""),
                "chamber": _getfield(item, "chamber", ""),
                "transaction_type": _getfield(item, "type", "")
                                    or _getfield(item, "transaction_type", ""),
                "amount": _getfield(item, "amount", ""),
                "transaction_date": str(_getfield(item, "transaction_date", "")
                                        or _getfield(item, "date", "")),
                "district": _getfield(item, "district", ""),
            })

        result = {"trades": trades}
        self._cache[ckey] = result
        return result

    def _derivatives_options_unusual(self, params: dict[str, Any]) -> dict[str, Any]:
        ticker = params.get("ticker") or params.get("symbol")
        if not ticker:
            return {"error": "derivatives_options_unusual requires 'ticker'"}

        ckey = f"derivatives_options_unusual|{ticker}"
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.derivatives.options.chains(symbol=ticker, provider="yfinance")
        unusual = []
        for item in resp.results or []:
            volume = _getfield(item, "volume", 0) or 0
            oi = _getfield(item, "open_interest", 0) or 0
            unusual.append({
                "ticker": _getfield(item, "underlying_symbol", ticker),
                "contract_type": _getfield(item, "option_type", ""),
                "strike": _getfield(item, "strike", 0.0),
                "expiration": str(_getfield(item, "expiration", "")),
                "volume": volume,
                "open_interest": oi,
                "vol_oi_ratio": round(volume / max(oi, 1), 2),
            })

        result = {"unusual": unusual}
        self._cache[ckey] = result
        return result

    def _regulators_sec_litigation(self, params: dict[str, Any]) -> dict[str, Any]:
        ckey = "regulators_sec_litigation"
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.regulators.sec.rss_litigation(provider="sec")
        releases = []
        for item in resp.results or []:
            releases.append({
                "title": _getfield(item, "title", ""),
                "date": str(_getfield(item, "published", "")),
                "url": _getfield(item, "link", ""),
                "category": _getfield(item, "category", ""),
            })

        result = {"releases": releases}
        self._cache[ckey] = result
        return result

    def _factors_fama_french(self, params: dict[str, Any]) -> dict[str, Any]:
        # Preserve the installed SDK's existing default: US monthly 3 factors.
        # The former unused default "5" mislabeled the actual request.
        model = params.get("model", "3")
        if model not in ('3', '5'):
            raise SourceFetchError('Unsupported Fama-French model', reason_code='invalid_response')
        ckey = f"factors_fama_french|{model}"
        if ckey in self._cache:
            return self._cache[ckey]

        obb = self._get_obb()
        resp = obb.famafrench.factors(provider="famafrench", factor=f'{model}_factors', frequency='monthly')
        if not resp.results:
            raise SourceFetchError('Fama-French observations unavailable', reason_code='invalid_response')

        # Extract latest row and normalize to spec keys
        factor_map = {
            "Mkt-RF": "mkt_rf", "SMB": "smb", "HML": "hml",
            "RMW": "rmw", "CMA": "cma", "RF": "rf",
        }
        acquired_at = acquisition_time()
        acquired = datetime.fromisoformat(acquired_at)
        if acquired.utcoffset() is None:
            raise SourceFetchError('Fama-French acquisition clock invalid', reason_code='invalid_response')
        required = {'Mkt-RF', 'SMB', 'HML', 'RF'} | ({'RMW', 'CMA'} if model == '5' else set())
        records, months = {}, set()
        for item in resp.results:
            try:
                observation = date.fromisoformat(str(_getfield(item, 'date', '')))
            except (TypeError, ValueError):
                raise SourceFetchError('Fama-French observation date invalid', reason_code='invalid_response') from None
            month = (observation.year, observation.month)
            if month in months or month >= (acquired.year, acquired.month):
                raise SourceFetchError('Fama-French monthly observations duplicated or not completed', reason_code='invalid_response')
            row = {}
            for spec_key, obb_key in factor_map.items():
                val = _getfield(item, obb_key)
                if val is not None:
                    if not source_number(val):
                        raise SourceFetchError('Fama-French factor invalid', reason_code='invalid_response')
                    row[spec_key] = float(val)
            if not required <= set(row):
                raise SourceFetchError('Fama-French model observations incomplete', reason_code='invalid_response')
            records[observation.isoformat()] = row
            months.add(month)

        # Keep actual observation labels and acquisition availability separate.
        # The publisher supplies no fixed monthly release SLA; age is not proof
        # of an outage and is never replaced with a fabricated daily timestamp.
        ordered = sorted(records)
        history = {day: records[day] for day in ordered[-12:]}
        factors = {**records[ordered[-1]], 'observation_date': ordered[-1],
            'frequency': 'monthly', 'model': model, 'acquired_at': acquired_at,
            'available_at': acquired_at, 'availability_basis': 'forward_acquisition_time'}

        result = {"factors": factors, "history": history}
        self._cache[ckey] = result
        return result

    def _sector_tickers(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return all tickers in a given industry classification.

        Uses OpenBB equity screener. Results cached for 24h (session-level cache).
        """
        industry = params.get("industry", "")
        if not industry:
            return {"tickers": []}

        cache_key = self._cache_key("sector_tickers", params)
        if cache_key in self._cache:
            return self._cache[cache_key]

        obb = self._get_obb()
        try:
            result = obb.equity.screener.screen(industry=industry)
            tickers = [_getfield(item, "symbol", "") for item in (result.results or [])]
            tickers = [t for t in tickers if t]  # Filter empty
        except Exception:
            raise

        out = {"tickers": tickers, "industry": industry}
        self._cache[cache_key] = out
        return out

    def _commodity_futures_curve(self, params: dict[str, Any]) -> dict[str, Any]:
        """Retired: historical prices do not identify contracts or maturities."""
        return {"error": "unsupported futures curve: maturity-identified curve evidence required"}
