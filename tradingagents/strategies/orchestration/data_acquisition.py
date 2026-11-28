"""Shared single-instance data acquisition service with Tier-1/Tier-2 fault isolation.

Instantiated ONCE per daily run and shared across horizons and cohorts.
Tier-1 accounting sources (Alpaca SIP bars, corporate actions/benchmarks) are
strictly fail-closed and never handled here. Tier-2 research sources and
optional enrichments are guarded by a per-strategy circuit breaker backed by
``_io.SOURCE_TAXONOMY``: a failure in any Tier-2 source marks *only* the
affected strategy/enrichment ``unavailable`` for the session and never aborts
sibling strategies or invalidates clean Tier-1 accounting.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
import math
import os
import time
from typing import Any

import pandas as pd

from tradingagents.strategies.data_sources.fetch_errors import (
    SourceFetchError,
    source_fetch_error,
)
from tradingagents.strategies.data_sources.registry import (
    DataSourceRegistry,
    build_default_registry,
)
from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.orchestration._io import SOURCE_TAXONOMY

logger = logging.getLogger(__name__)

# Trimmed from 37 -> 13 live tickers (removed 24 dead state_economics &
# legacy govt_contracts momentum tickers).
YAHOO_RESEARCH_CORE_TICKERS: tuple[str, ...] = (
    # Market + regime model
    "SPY",
    "SHY",
    "TLT",
    # Active weather_ag universe
    "DBA",
    "WEAT",
    "CORN",
    "MOO",
    "SOYB",
    "ADM",
    "BG",
    "CTVA",
    "DE",
    "FMC",
)

# Default supply-chain bellwether universe (overridable via strategy/ar params).
DEFAULT_SUPPLY_CHAIN_TICKERS: tuple[str, ...] = (
    "AAPL",
    "TSLA",
    "NVDA",
    "AMZN",
    "BA",
    "CAT",
    "DE",
    "MSFT",
    "AVGO",
    "AMD",
    "QCOM",
    "TXN",
    "HON",
    "GE",
    "UNP",
    "FDX",
    "UPS",
    "GM",
    "F",
    "WMT",
    "COST",
    "TGT",
    "JNJ",
    "NKE",
    "XOM",
)

# Canonical mapping from runtime data-source keys to _io.SOURCE_TAXONOMY names.
_SOURCE_TAXONOMY_ALIASES: dict[str, str] = {
    "yfinance": "yfinance_research",
    "yahoo": "yfinance_research",
    "yahoo_research": "yfinance_research",
    "finnhub": "finnhub",
    "usaspending": "usaspending",
    "noaa": "noaa",
    "usda": "usda",
    "drought_monitor": "drought_monitor",
    "cftc": "cftc",
    "fred": "fred",
    "openbb": "yfinance_profile",
    "enrichment": "yfinance_profile",
    "yfinance_profile": "yfinance_profile",
    "finra_short_interest": "finra_short_interest",
    "ken_french": "ken_french",
    "alpaca_sip": "alpaca_sip",
    "yfinance_actions_benchmarks": "yfinance_actions_benchmarks",
}

# Active strategy -> required Tier-2 strategy sources.
STRATEGY_REQUIRED_SOURCES: dict[str, tuple[str, ...]] = {
    "earnings_call": ("finnhub",),
    "supply_chain": ("finnhub",),
    "govt_contracts": ("usaspending",),
    "weather_ag": ("noaa", "usda", "drought_monitor", "yfinance"),
    "commodity_macro": ("cftc", "fred"),
}


def canonical_taxonomy_source(name: str) -> str:
    """Map an internal source key to its canonical ``SOURCE_TAXONOMY`` identifier."""
    normalized = str(name).strip().lower()
    return _SOURCE_TAXONOMY_ALIASES.get(normalized, normalized)


def source_tier(name: str) -> str:
    """Return ``tier1_accounting``, ``tier2_strategy``, or ``tier2_optional_enrichment``."""
    canonical = canonical_taxonomy_source(name)
    return SOURCE_TAXONOMY.tier_for(canonical)


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _has_valid_numeric_eps_surprise(record: Mapping[str, Any]) -> bool:
    actual = _finite_float(record.get("epsActual"))
    estimate = _finite_float(record.get("epsEstimate"))
    return actual is not None and estimate is not None


def _eps_surprise_magnitude(record: Mapping[str, Any]) -> float:
    actual = _finite_float(record.get("epsActual"))
    estimate = _finite_float(record.get("epsEstimate"))
    if actual is None or estimate is None:
        return -1.0
    diff = abs(actual - estimate)
    denom = max(abs(estimate), 0.10)
    return diff / denom


def _liquidity_score(record: Mapping[str, Any]) -> float:
    for key in (
        "marketCap",
        "market_cap",
        "revenueActual",
        "revenueEstimate",
        "avgVolume",
        "avg_volume",
        "dollar_volume",
    ):
        val = _finite_float(record.get(key))
        if val is not None and val > 0:
            return val
    return 0.0


def earnings_priority_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    """Rank Finnhub earnings candidates by EPS surprise magnitude and liquidity.

    Never ranks alphabetically by symbol before surprise/liquidity so tickers
    starting with 'A' cannot starve higher-impact names.
    """
    try:
        from tradingagents.strategies.modules.earnings_call import (
            earnings_priority_key as strategy_priority_key,
        )

        return strategy_priority_key(record)
    except (ImportError, AttributeError):
        pass

    has_numeric = 0 if _has_valid_numeric_eps_surprise(record) else 1
    surprise = _eps_surprise_magnitude(record)
    liquidity = _liquidity_score(record)
    try:
        date_ord = -date.fromisoformat(str(record.get("date", ""))).toordinal()
    except ValueError:
        date_ord = 0
    return (
        has_numeric,
        -round(surprise, 6),
        -round(liquidity, 2),
        date_ord,
        str(record.get("symbol", "")).strip().upper(),
    )


def _fetch_timeout_s() -> float:
    raw = os.environ.get("AUTORESEARCH_FETCH_TIMEOUT_S", "900")
    try:
        value = float(raw)
        return value if math.isfinite(value) and value >= 0 else 900.0
    except ValueError:
        return 900.0


def _gather_with_timeout(
    tasks: dict[str, tuple[Any, tuple[Any, ...]]],
    timeout_s: float,
    error_factory: Any,
) -> dict[str, Any]:
    if not tasks:
        return {}
    results: dict[str, Any] = {}
    executor = ThreadPoolExecutor(max_workers=min(len(tasks), 8))
    futures = {
        executor.submit(fn, *args): name for name, (fn, args) in tasks.items()
    }
    try:
        done, not_done = wait(futures.keys(), timeout=max(0.0, timeout_s))
        for fut in done:
            name = futures[fut]
            try:
                results[name] = fut.result()
            except Exception as exc:
                results[name] = error_factory(name, exc)
        for fut in not_done:
            name = futures[fut]
            fut.cancel()
            results[name] = error_factory(
                name, TimeoutError(f"source {name} exceeded fetch timeout ({timeout_s:.1f}s)")
            )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return results


def _resolve_supply_chain_universe(ar_config: Mapping[str, Any]) -> list[str]:
    try:
        from tradingagents.strategies.modules.supply_chain import (
            get_supply_chain_universe,
        )

        return list(get_supply_chain_universe(ar_config))
    except (ImportError, AttributeError):
        pass
    configured = ar_config.get("supply_chain_universe")
    if isinstance(configured, (list, tuple)) and configured:
        return [str(s).strip().upper() for s in configured if str(s).strip()]
    return list(DEFAULT_SUPPLY_CHAIN_TICKERS)


def is_source_available(payload: Any) -> bool:
    """Return True when a Tier-2 source payload succeeded without error."""
    if not isinstance(payload, Mapping):
        return False
    if payload.get("error"):
        return False
    cov = payload.get("_coverage")
    if isinstance(cov, Mapping) and cov.get("status") in {"failed", "unavailable"}:
        return False
    return True


def _tier2_failure_rank(reason_code: str, http_status: int | None) -> tuple[int, int]:
    """Explicit severity rank for Tier-2 failure aggregation.

    Higher rank wins:
    invalid_response > 4xx http_error > 5xx http_error > timeout > transport_error > provider_error.
    """
    if reason_code == "invalid_response":
        return (6, http_status or 0)
    if reason_code == "http_error":
        if http_status is not None and 400 <= http_status < 500:
            return (5, http_status)
        if http_status is not None and 500 <= http_status < 600:
            return (4, http_status)
        return (4, http_status or 0)
    if reason_code == "timeout":
        return (3, http_status or 0)
    if reason_code == "transport_error":
        return (2, http_status or 0)
    if reason_code == "provider_error":
        return (1, http_status or 0)
    return (0, http_status or 0)


def _tier2_error_coverage(
    err: SourceFetchError | None = None,
    *,
    status: str = "unavailable",
    reason_code: str | None = None,
    http_status: int | None = None,
    attempts: int | None = None,
) -> dict[str, Any]:
    """Build a uniform Tier-2 ``_coverage`` error block."""
    return {
        "status": status,
        "reason_code": (
            reason_code
            if reason_code is not None
            else (err.reason_code if err is not None else "provider_error")
        ),
        "http_status": (
            http_status
            if http_status is not None
            else (err.http_status if err is not None else None)
        ),
        "attempts": (
            attempts
            if attempts is not None
            else (err.attempts if err is not None else 0)
        ),
    }


def _tier2_aggregated_coverage(
    failure_details: Sequence[tuple[str, int | None, int]],
    *,
    has_partial: bool,
) -> dict[str, Any]:
    """Select the highest-severity Tier-2 failure into a uniform ``_coverage`` dict."""
    reason_code, http_status, _ = max(
        failure_details,
        key=lambda item: _tier2_failure_rank(item[0], item[1]),
        default=("provider_error", None, 0),
    )
    return _tier2_error_coverage(
        status="partial" if has_partial else "unavailable",
        reason_code=reason_code,
        http_status=http_status,
        attempts=max((item[2] for item in failure_details), default=0),
    )


def _tier2_error_payload(
    err: SourceFetchError,
    *,
    partial_data: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    has_partial: bool | None = None,
) -> dict[str, Any]:
    """Build a uniform Tier-2 error payload carrying ``error`` and ``_coverage``."""
    merged: dict[str, Any] = {}
    if extra:
        merged.update(extra)
    if err.partial_data:
        merged.update(err.partial_data)
    if partial_data:
        merged.update(partial_data)
    partial_flag = bool(err.partial_data or partial_data) if has_partial is None else has_partial
    merged["error"] = str(err)
    merged["_coverage"] = _tier2_error_coverage(
        err,
        status="partial" if partial_flag else "unavailable",
    )
    return merged


def evaluate_strategy_circuit_breaker(
    data: Mapping[str, Any],
    strategies: Sequence[Any],
) -> dict[str, dict[str, Any]]:
    """Classify per-strategy availability strictly under Tier-2 fault isolation.

    Returns ``{strategy_name: {"available": bool, "status": "available"|"unavailable",
    "failed_sources": [...], "optional_enrichment_failures": [...]}}``.
    Failures in optional enrichments NEVER mark any strategy unavailable, and a
    failure in one strategy's Tier-2 source NEVER affects sibling strategies.
    """
    enrichment_failures: list[str] = []
    for name in SOURCE_TAXONOMY.optional_enrichment:
        if name in data and not is_source_available(data[name]):
            enrichment_failures.append(name)

    decisions: dict[str, dict[str, Any]] = {}
    for strategy in strategies:
        name = getattr(strategy, "name", str(strategy))
        declared = tuple(getattr(strategy, "data_sources", ()) or STRATEGY_REQUIRED_SOURCES.get(name, ()))
        failed_sources: list[str] = []
        for raw_source in declared:
            canonical = canonical_taxonomy_source(raw_source)
            if canonical in SOURCE_TAXONOMY.optional_enrichment:
                continue
            if canonical in SOURCE_TAXONOMY.accounting_sources:
                # Tier-1 sources are fail-closed elsewhere; never silenced here.
                continue
            payload = data.get(raw_source)
            if payload is None and canonical != raw_source:
                payload = data.get(canonical)
            if payload is not None and not is_source_available(payload):
                failed_sources.append(raw_source)
        available = not failed_sources
        decisions[name] = {
            "available": available,
            "status": "available" if available else "unavailable",
            "failed_sources": sorted(failed_sources),
            "optional_enrichment_failures": sorted(enrichment_failures),
        }
    return decisions


class DataAcquisitionService:
    """Single-instance Tier-2 research data acquisition service."""

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        registry: DataSourceRegistry | None = None,
    ) -> None:
        self.config: dict[str, Any] = dict(config or {})
        self.ar_config: dict[str, Any] = dict(self.config.get("autoresearch", {}))
        self.registry: DataSourceRegistry = registry or build_default_registry(self.ar_config)
        self.price_cache: dict[str, pd.DataFrame] = {}
        self.last_source_status: dict[str, dict[str, Any]] = {}

    def fetch_all_data(
        self,
        start_date: str,
        end_date: str,
        *,
        needed_sources: set[str] | None = None,
        acquisition_deadline: float | None = None,
        acquisition_purpose: str = "decision",
        fetcher_overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fetch all Tier-2 research data once per session with circuit-breaker isolation."""
        from tradingagents.strategies.orchestration.decision_clock import (
            PROSPECTIVE,
            mutable_vintage,
            policy as decision_clock_policy,
        )
        from tradingagents.strategies.orchestration.source_inputs import (
            SourceInputError,
            SourceInputStore,
            cache_identity,
            registered_source_fingerprint,
            source_configuration_fingerprint,
        )
        from tradingagents.strategies.orchestration.trading_calendar import exchange_date

        acquisition_start = time.monotonic()
        acquisition_cutoff = datetime.now(timezone.utc)
        if acquisition_purpose not in {"decision", "screen_diagnostic"}:
            raise ValueError("invalid source acquisition purpose")
        decision_policy = decision_clock_policy(self.config)
        diagnostic = acquisition_purpose == "screen_diagnostic"
        if diagnostic:
            vintage = exchange_date(acquisition_cutoff).isoformat()
        else:
            vintage = mutable_vintage(self.config, date.fromisoformat(end_date), now=acquisition_cutoff)

        fetch_timeout_s = max(0.0, _fetch_timeout_s())
        if acquisition_deadline is not None:
            if (
                isinstance(acquisition_deadline, bool)
                or not isinstance(acquisition_deadline, (int, float))
                or not math.isfinite(acquisition_deadline)
            ):
                raise SourceInputError("invalid source acquisition deadline")
        effective_deadline = min(
            acquisition_start + fetch_timeout_s,
            acquisition_deadline if acquisition_deadline is not None else float("inf"),
        )

        cache_dir = self.ar_config.get("source_cache_dir") or os.environ.get(
            "EVENTEDGE_SOURCE_CACHE_DIR"
        )
        cache_store: SourceInputStore | None = None
        config_fp = ""
        if cache_dir:
            cache_store = SourceInputStore(
                cache_dir, ttl_s=self.ar_config.get("source_cache_ttl_s", 300)
            )
            config_fp = source_configuration_fingerprint(self.config, exclude_horizon=True)
            if diagnostic:
                config_fp += ":screen-diagnostic-v1"

        overrides = dict(fetcher_overrides or {})
        available = set(self.registry.available_sources())
        requested = set(needed_sources or {"yfinance", "fred", "finnhub", "usaspending", "noaa", "usda", "drought_monitor", "cftc"})
        for req_name in requested:
            if canonical_taxonomy_source(req_name) in SOURCE_TAXONOMY.accounting_sources:
                raise ValueError(
                    f"Tier-1 accounting source {req_name!r} cannot be acquired via Tier-2 service"
                )
        requested.update({"yfinance", "fred"})

        def _fn(name: str, default_fn: Any) -> Any:
            return overrides.get(name, default_fn)

        def _acquire(name: str, fetcher: Any, args: tuple[Any, ...]) -> dict[str, Any]:
            # Tier-1 sources must never be silently swallowed here.
            canonical = canonical_taxonomy_source(name)
            if canonical in SOURCE_TAXONOMY.accounting_sources:
                raise ValueError(f"Tier-1 accounting source {name!r} cannot be acquired via Tier-2 service")
            if time.monotonic() >= effective_deadline:
                return {
                    "error": "source acquisition deadline exhausted",
                    "_coverage": {"status": "failed", "reason_code": "deadline_exhausted"},
                }
            diagnostics: list[dict[str, Any]] = []
            try:
                budget_name = "alpaca" if name == "equity_universe" else name
                with provider_budget(budget_name, deadline=effective_deadline, diagnostics=diagnostics):
                    cache_id = None
                    if cache_store is not None:
                        registered = self.registry.get(name)
                        cache_id = cache_identity(
                            name,
                            start_date,
                            end_date,
                            registered_source_fingerprint(config_fp, registered),
                        )
                        cached = cache_store.load_cached(
                            cache_id, now=datetime.now(timezone.utc), cutoff=acquisition_cutoff
                        )
                        if cached is not None:
                            if diagnostics and isinstance(cached, dict):
                                cached = {**cached, "_request_diagnostics": list(diagnostics)}
                            return cached
                    acquired_at = datetime.now(timezone.utc)
                    payload = fetcher(*args)
                    if not isinstance(payload, dict):
                        payload = {"data": payload}
                    if diagnostics:
                        payload["_request_diagnostics"] = list(diagnostics)
                    if cache_store is not None and cache_id is not None:
                        cache_store.save_cached(cache_id, payload, acquired_at=acquired_at)
                    return payload
            except Exception as exc:
                safe = source_fetch_error(f"{name} acquisition failed", exc)
                logger.warning("Tier-2 source %s unavailable: %s", name, safe)
                return _tier2_error_payload(
                    safe,
                    extra={"_request_diagnostics": list(diagnostics)} if diagnostics else None,
                    has_partial=False,
                )

        data: dict[str, Any] = {}
        universe_policy = self.ar_config.get("equity_universe_policy")
        if universe_policy:
            from tradingagents.strategies.data_sources.equity_universe import (
                POLICY as UNIVERSE_POLICY,
                fetch_equity_universe,
            )
            if universe_policy == UNIVERSE_POLICY:
                data["equity_universe"] = _acquire(
                    "equity_universe",
                    _fn("equity_universe", fetch_equity_universe),
                    (),
                )
            else:
                data["equity_universe"] = {"error": "unsupported equity universe policy"}
        # Sequential yfinance batch first (shared regime + weather_ag core prices)
        if "yfinance" in requested and ("yfinance" in available or "yfinance" in overrides):
            data["yfinance"] = _acquire(
                "yfinance", _fn("yfinance", self.fetch_yfinance_data), (start_date, end_date)
            )
            if isinstance(data["yfinance"], dict):
                self.price_cache.update(data["yfinance"].get("prices", {}))

        # Parallel Tier-2 strategy API sources
        api_tasks: dict[str, tuple[Any, tuple[Any, ...]]] = {}
        if "finnhub" in requested and ("finnhub" in available or "finnhub" in overrides):
            api_tasks["finnhub"] = (
                _acquire,
                ("finnhub", _fn("finnhub", self.fetch_finnhub_data), (end_date,)),
            )
        if "fred" in requested and ("fred" in available or "fred" in overrides):
            api_tasks["fred"] = (
                _acquire,
                ("fred", _fn("fred", self.fetch_fred_data), (start_date, end_date)),
            )
        if "usaspending" in requested and ("usaspending" in available or "usaspending" in overrides):
            api_tasks["usaspending"] = (
                _acquire,
                ("usaspending", _fn("usaspending", self.fetch_usaspending_data), (end_date,)),
            )
        if "noaa" in requested and ("noaa" in available or "noaa" in overrides):
            noaa_args = (end_date, vintage) if vintage is not None else (end_date,)
            api_tasks["noaa"] = (
                _acquire,
                ("noaa", _fn("noaa", self.fetch_noaa_data), noaa_args),
            )
        if "usda" in requested and ("usda" in available or "usda" in overrides):
            usda_args = (end_date, vintage) if vintage is not None else (end_date,)
            api_tasks["usda"] = (
                _acquire,
                ("usda", _fn("usda", self.fetch_usda_data), usda_args),
            )
        if "drought_monitor" in requested and ("drought_monitor" in available or "drought_monitor" in overrides):
            drought_args = (end_date, vintage) if vintage is not None else (end_date,)
            api_tasks["drought_monitor"] = (
                _acquire,
                ("drought_monitor", _fn("drought_monitor", self.fetch_drought_data), drought_args),
            )
        if "cftc" in requested and ("cftc" in available or "cftc" in overrides):
            cftc_args = (end_date, vintage) if vintage is not None else (end_date,)
            api_tasks["cftc"] = (
                _acquire,
                ("cftc", _fn("cftc", self.fetch_cftc_data), cftc_args),
            )

        remaining = max(0.0, effective_deadline - time.monotonic())
        if api_tasks:
            def _err(name: str, exc: Exception) -> dict[str, Any]:
                safe = source_fetch_error(f"{name} acquisition failed", exc)
                return _tier2_error_payload(safe, has_partial=False)

            fetched = _gather_with_timeout(api_tasks, remaining, _err)
            data.update(fetched)

        if decision_clock_policy(self.config) == PROSPECTIVE and not diagnostic:
            data["_decision_acquisition"] = {
                "policy": PROSPECTIVE,
                "reference_session": end_date,
                "started_at": acquisition_cutoff.isoformat(),
                "vintage_as_of": vintage,
            }

        # Record Tier-2 circuit-breaker source health snapshot
        self.last_source_status = {}
        for src_name, payload in data.items():
            if src_name.startswith("_"):
                continue
            canonical = canonical_taxonomy_source(src_name)
            tier = (
                SOURCE_TAXONOMY.tier_for(canonical)
                if canonical in SOURCE_TAXONOMY.all_sources
                else "tier2_strategy"
            )
            ok = is_source_available(payload)
            self.last_source_status[src_name] = {
                "canonical_source": canonical,
                "tier": tier,
                "status": "available" if ok else "unavailable",
                "error": payload.get("error") if isinstance(payload, Mapping) else None,
            }
        data["_source_taxonomy_status"] = dict(self.last_source_status)
        return data

    def fetch_missing_prices(
        self, tickers: list[str], start_date: str, end_date: str, *, strict: bool = False
    ) -> dict[str, pd.DataFrame]:
        """Fetch missing research prices; candidate validation opts into explicit failures.

        Legacy research acquisition remains best-effort. Strict callers must classify
        source failures themselves rather than interpreting an empty cache as absence.
        """
        from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource

        missing = [t for t in tickers if t and t not in self.price_cache]
        if not missing:
            return {t: self.price_cache[t] for t in tickers if t in self.price_cache}
        source = self.registry.get("yfinance")
        if not isinstance(source, YFinanceSource):
            if strict:
                raise SourceFetchError("Yahoo research source unavailable")
            return {t: self.price_cache[t] for t in tickers if t in self.price_cache}
        try:
            prices_df = source.fetch_prices(missing, start_date, end_date)
            if strict and (not isinstance(prices_df, pd.DataFrame) or prices_df.empty):
                # An empty download can be a swallowed HTTP/provider failure;
                # do not manufacture per-symbol evidence from request cardinality.
                raise SourceFetchError("Yahoo research history unavailable", reason_code="invalid_response")
        except Exception as exc:
            if strict:
                raise
            logger.warning("Tier-2 missing-price fetch failed for %s: %s", missing, exc)
            return {t: self.price_cache[t] for t in tickers if t in self.price_cache}
        if not prices_df.empty and isinstance(prices_df.columns, pd.MultiIndex):
            for ticker in missing:
                try:
                    tdf = prices_df.xs(ticker, level=1, axis=1)
                    if not tdf.empty:
                        self.price_cache[ticker] = tdf
                except (KeyError, ValueError):
                    pass
        elif not prices_df.empty and len(missing) == 1:
            self.price_cache[missing[0]] = prices_df
        return {t: self.price_cache[t] for t in tickers if t in self.price_cache}

    def fetch_yfinance_data(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Fetch Yahoo research prices for the 13 live core tickers + VIX."""
        from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
        from tradingagents.strategies.orchestration.research_resilience import (
            _project_capabilities,
            acquisition_group,
            acquisition_manifest,
            capability_configured,
        )
        from tradingagents.strategies.orchestration.source_inputs import source_codec_limits

        source = self.registry.get("yfinance")
        if not isinstance(source, YFinanceSource):
            logger.warning("yfinance research source not available")
            return {}

        capability = capability_configured(self.ar_config)
        started_at = datetime.now(timezone.utc).isoformat()
        groups: dict[str, Any] = {}
        core_tickers = list(YAHOO_RESEARCH_CORE_TICKERS)
        result: dict[str, Any] = {}
        failures: list[str] = []
        failure_details: list[tuple[str, int | None, int]] = []

        logger.info("Fetching Yahoo research prices for %d live core tickers", len(core_tickers))
        try:
            prices_df = source.fetch_prices(core_tickers, start_date, end_date)
            if capability:
                groups["research_prices"] = acquisition_group(core_tickers)
        except Exception as exc:
            error = source_fetch_error("Yahoo research history failed", exc)
            failures.append(str(error))
            failure_details.append((error.reason_code, error.http_status, error.attempts))
            if capability:
                groups["research_prices"] = acquisition_group(core_tickers, error=error)
            prices_df = error.partial_data.get("prices", pd.DataFrame())

        prices: dict[str, pd.DataFrame] = {}
        if not prices_df.empty and isinstance(prices_df.columns, pd.MultiIndex):
            for ticker in core_tickers:
                try:
                    ticker_df = prices_df.xs(ticker, level=1, axis=1)
                    if not ticker_df.empty:
                        prices[ticker] = ticker_df
                except (KeyError, ValueError):
                    pass
        elif not prices_df.empty and len(core_tickers) == 1:
            prices[core_tickers[0]] = prices_df

        result["prices"] = prices
        if not capability:
            self.price_cache.update(prices)

        try:
            vix_df = source.fetch_vix(start_date, end_date)
            if capability:
                groups["vix"] = acquisition_group(["^VIX"])
        except Exception as exc:
            error = source_fetch_error("Yahoo VIX history failed", exc)
            failures.append(str(error))
            failure_details.append((error.reason_code, error.http_status, error.attempts))
            if capability:
                groups["vix"] = acquisition_group(["^VIX"], error=error)
            vix_df = error.partial_data.get("vix", pd.DataFrame()) if capability else pd.DataFrame()
        if not vix_df.empty:
            result["vix"] = vix_df

        if failures:
            result["error"] = "; ".join(failures)
            result["_coverage"] = _tier2_aggregated_coverage(
                failure_details,
                has_partial=bool(prices or "vix" in result),
            )
        if capability:
            result["_capability_acquisition"] = acquisition_manifest(
                "yfinance",
                groups,
                start_date,
                end_date,
                started_at,
                datetime.now(timezone.utc).isoformat(),
            )
            accepted, _, _ = _project_capabilities(
                result,
                "yfinance",
                codec_limits=source_codec_limits(self.config or {"autoresearch": self.ar_config}),
            )
            self.price_cache.update(accepted.get("prices", {}))
        return result

    def fetch_finnhub_data(
        self,
        trading_date: str,
        max_workflow_budget_s: float | None = None,
    ) -> dict[str, Any]:
        """Fetch Finnhub earnings + supply-chain news/peers.

        Prioritizes earnings by EPS surprise magnitude and liquidity (never
        alphabetically) and retains valid numeric EPS surprises even when
        Finnhub returns 0 news articles.
        """
        source = self.registry.get("finnhub")
        if source is None:
            return {}

        settings = self.ar_config.get("finnhub_acquisition", {})
        if not isinstance(settings, Mapping):
            raise ValueError("finnhub_acquisition must be a mapping")
        earnings_news_budget = settings.get("earnings_news_budget", 10)
        earnings_article_budget = settings.get("earnings_article_budget", 5)
        if (
            type(earnings_news_budget) is not int
            or type(earnings_article_budget) is not int
            or earnings_news_budget < 0
            or earnings_article_budget < 0
        ):
            raise ValueError("Finnhub acquisition budgets must be nonnegative integers")

        result: dict[str, Any] = {}
        failures: list[str] = []
        failure_details: list[tuple[str, int | None, int]] = []

        def _acquire(operation: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                err = source_fetch_error("Finnhub acquisition incomplete", exc)
                failures.append(f"{operation}: {err}")
                failure_details.append((err.reason_code, err.http_status, err.attempts))
                if operation == "peers":
                    return err.partial_data
                return err.partial_data.get("earnings" if operation == "earnings" else "news", [])

        deadline = source.new_workflow_deadline(max_budget_s=max_workflow_budget_s)
        date_to = trading_date
        date_from = (
            datetime.strptime(trading_date, "%Y-%m-%d") - timedelta(days=7)
        ).strftime("%Y-%m-%d")

        # 1. Earnings calendar — rank by EPS surprise magnitude & liquidity (NOT alphabetical!)
        raw_earnings = _acquire(
            "earnings", source.fetch_recent_earnings, date_from, date_to, deadline=deadline
        )
        valid_earnings: list[dict[str, Any]] = []
        seen_keys: set[tuple[Any, ...]] = set()
        for rec in raw_earnings or []:
            if not isinstance(rec, Mapping):
                continue
            sym = str(rec.get("symbol", "")).strip().upper()
            edate = str(rec.get("date", "")).strip()
            if not sym or not edate:
                continue
            try:
                date.fromisoformat(edate)
            except ValueError:
                continue
            dedup_key = (sym, edate, rec.get("year"), rec.get("quarter"))
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            valid_earnings.append(dict(rec, symbol=sym, date=edate))

        ranked_earnings = sorted(valid_earnings, key=earnings_priority_key)
        news_candidates = ranked_earnings[:earnings_news_budget]

        from tradingagents.strategies.orchestration.event_identity import (
            earnings_calendar_observed_at,
        )

        transcripts: list[dict[str, Any]] = []
        for idx, rec in enumerate(ranked_earnings):
            symbol = rec["symbol"]
            edate = rec["date"]
            hour = str(rec.get("hour") or "").strip().lower()
            cal_observed = earnings_calendar_observed_at(edate, hour).isoformat()
            selected_articles: list[dict[str, Any]] = []
            if idx < len(news_candidates):
                news = _acquire(
                    "earnings_news",
                    source.fetch_earnings_news,
                    symbol,
                    edate,
                    deadline=deadline,
                )
                valid_articles = [a for a in (news or []) if isinstance(a, Mapping)]
                valid_articles.sort(
                    key=lambda a: str(a.get("published_at", a.get("datetime", ""))),
                    reverse=True,
                )
                selected_articles = valid_articles[:earnings_article_budget]

            if selected_articles:
                news_text = "\n".join(
                    f"[{a.get('source', 'Unknown')}]: {a.get('headline', '')} — {a.get('summary', '')}"
                    for a in selected_articles
                )
                pub_times = [
                    str(a["published_at"]) for a in selected_articles if a.get("published_at")
                ]
                transcripts.append(
                    {
                        "symbol": symbol,
                        "date": edate,
                        "report_date": edate,
                        **({"hour": hour} if hour else {}),
                        "year": rec.get("year"),
                        "quarter": rec.get("quarter"),
                        "transcript_text": news_text,
                        "text_source": "earnings_news",
                        "eps_actual": rec.get("epsActual"),
                        "eps_estimate": rec.get("epsEstimate"),
                        "revenue_actual": rec.get("revenueActual"),
                        "revenue_estimate": rec.get("revenueEstimate"),
                        "eps_surprise_pct": round(_eps_surprise_magnitude(rec), 6)
                        if _has_valid_numeric_eps_surprise(rec)
                        else None,
                        **({"published_at": max(pub_times)} if pub_times else {"observed_at": cal_observed}),
                    }
                )
            elif _has_valid_numeric_eps_surprise(rec):
                # Never drop valid numeric EPS surprises when Finnhub news count is 0!
                transcripts.append(
                    {
                        "symbol": symbol,
                        "date": edate,
                        "report_date": edate,
                        **({"hour": hour} if hour else {}),
                        "year": rec.get("year"),
                        "quarter": rec.get("quarter"),
                        "transcript_text": "",
                        "text_source": "numeric_eps_surprise",
                        "eps_actual": rec.get("epsActual"),
                        "eps_estimate": rec.get("epsEstimate"),
                        "revenue_actual": rec.get("revenueActual"),
                        "revenue_estimate": rec.get("revenueEstimate"),
                        "eps_surprise_pct": round(_eps_surprise_magnitude(rec), 6),
                        "observed_at": cal_observed,
                    }
                )

        if transcripts:
            result["transcripts"] = transcripts

        # 2. Supply chain disruption news & peer relationships
        sc_symbols = _resolve_supply_chain_universe(self.ar_config)
        all_news: list[dict[str, Any]] = []
        for symbol in sc_symbols:
            news = _acquire(
                "company_news",
                source.fetch_company_news,
                symbol,
                date_from,
                date_to,
                deadline=deadline,
            )
            for article in news or []:
                if isinstance(article, Mapping):
                    all_news.append(dict(article, symbol=symbol))
        if all_news:
            result["disruption_news"] = all_news

        chains: dict[str, list[str]] = {}
        peer_batches = _acquire(
            "peers", source.fetch_supply_chains, sc_symbols, deadline=deadline
        )
        if isinstance(peer_batches, Mapping):
            for symbol, peers in peer_batches.items():
                chains[symbol] = [
                    p["ticker"] for p in (peers or []) if isinstance(p, Mapping) and p.get("ticker")
                ]
        if chains:
            result["supply_chains"] = chains

        if failures:
            result["error"] = "; ".join(failures)
            result["_coverage"] = _tier2_aggregated_coverage(
                failure_details,
                has_partial=any(
                    key in result for key in ("transcripts", "disruption_news", "supply_chains")
                ),
            )
        result["coverage"] = {
            "contract": "finnhub-acquisition-v2",
            "status": "partial" if failures else "complete",
            "request_window": {"from": date_from, "to": date_to},
            "supply_chain_query_universe": sc_symbols,
            "earnings_discovered": len(valid_earnings),
            "earnings_news_queried": len(news_candidates),
            "earnings_emitted": len(transcripts),
        }
        return result

    def fetch_fred_data(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Fetch FRED credit spreads and economic indicators."""
        from tradingagents.strategies.data_sources.fred_source import SERIES_MAP
        from tradingagents.strategies.orchestration.research_resilience import (
            FRED_GROUPS,
            acquisition_group,
            acquisition_manifest,
            capability_configured,
        )

        source = self.registry.get("fred")
        if source is None:
            return {}
        start_date = min(
            start_date, (pd.Timestamp(end_date) - pd.DateOffset(months=18)).date().isoformat()
        )
        capability = capability_configured(self.ar_config)
        started_at = datetime.now(timezone.utc).isoformat()
        groups: dict[str, Any] = {}
        result: dict[str, Any] = {}
        failures: list[str] = []
        failure_details: list[tuple[str, int | None, int]] = []

        for label, group_key, method_name in (
            ("credit spreads", "credit_spreads", "fetch_credit_spreads"),
            ("economic indicators", "economic_indicators", "fetch_economic_indicators"),
        ):
            try:
                series_map = getattr(source, method_name)(start_date, end_date)
                if capability:
                    groups[group_key] = acquisition_group(FRED_GROUPS[group_key])
                if isinstance(series_map, Mapping):
                    result.update(series_map)
            except SourceFetchError as exc:
                result.update(exc.partial_data)
                failures.append(str(exc))
                failure_details.append((exc.reason_code, exc.http_status, exc.attempts))
                if capability:
                    groups[group_key] = acquisition_group(
                        FRED_GROUPS[group_key], error=exc, independent=True
                    )
            except Exception as exc:
                logger.warning("FRED %s fetch failed: %s", label, exc)
                failures.append(f"FRED {label} [provider_error]")
                failure_details.append(("provider_error", None, 0))
                if capability:
                    groups[group_key] = acquisition_group(
                        FRED_GROUPS[group_key], error_text=failures[-1]
                    )

        if capability:
            result["_capability_acquisition"] = acquisition_manifest(
                "fred",
                groups,
                start_date,
                end_date,
                started_at,
                datetime.now(timezone.utc).isoformat(),
            )
        if failures:
            result["error"] = "; ".join(failures)
            result["_coverage"] = _tier2_aggregated_coverage(
                failure_details,
                has_partial=any(
                    key not in {"_capability_acquisition", "error", "vintage", "_coverage"}
                    for key in result
                ),
            )
        result["vintage"] = {
            "as_of": end_date,
            "realtime_start": end_date,
            "realtime_end": end_date,
        }
        for friendly_name, series_id in SERIES_MAP.items():
            if series_id in result:
                result[friendly_name] = result[series_id]
        return result

    def fetch_usaspending_data(self, trading_date: str) -> dict[str, Any]:
        """Fetch recent large federal contract awards."""
        source = self.registry.get("usaspending")
        if source is None:
            return {}
        payload: dict[str, Any] = {}
        try:
            contracts = source.get_recent_large_contracts(
                min_amount=50_000_000,
                days_back=30,
                as_of=trading_date,
            )
            result = {"contracts": list(contracts)}
            if hasattr(contracts, "coverage"):
                result["coverage"] = contracts.coverage
            payload = {
                "data": result,
                **({"coverage": contracts.coverage} if hasattr(contracts, "coverage") else {}),
            }
            policy = self.ar_config.get("award_attribution_policy")
            if policy is not None:
                from tradingagents.strategies.data_sources.award_attribution_policy import (
                    POLICY,
                    apply_attribution_policy,
                )

                if policy != POLICY:
                    raise ValueError("unsupported award attribution policy")
                scoped = apply_attribution_policy(
                    list(contracts), getattr(contracts, "coverage", None), session=trading_date
                )
                payload.update(scoped)
                result["coverage"] = scoped["coverage"]
            elif (
                hasattr(contracts, "coverage")
                and contracts.coverage.get("issuer_attribution", {}).get("complete") is False
            ):
                payload["error"] = "USASpending issuer attribution incomplete"
                payload["_coverage"] = _tier2_error_coverage(
                    status="partial" if result["contracts"] else "unavailable",
                    reason_code="provider_error",
                    http_status=None,
                    attempts=1,
                )
            return payload
        except Exception as exc:
            safe_error = source_fetch_error("USASpending contract fetch failed", exc)
            logger.warning("Tier-2 USASpending unavailable: %s", safe_error)
            return _tier2_error_payload(
                safe_error,
                extra=payload,
                has_partial=bool(payload or safe_error.partial_data),
            )

    def fetch_noaa_data(
        self, trading_date: str, vintage_as_of: str | None = None
    ) -> dict[str, Any]:
        """Fetch NOAA weather anomaly summary for Corn Belt ag regions."""
        source = self.registry.get("noaa")
        if source is None:
            return {}
        try:
            options = {"vintage_as_of": vintage_as_of} if vintage_as_of is not None else {}
            return source.fetch_ag_weather_summary(trading_date, lookback_days=30, **options)
        except Exception as exc:
            err = source_fetch_error("NOAA acquisition failed", exc)
            return _tier2_error_payload(err)

    def fetch_usda_data(
        self, trading_date: str, vintage_as_of: str | None = None
    ) -> dict[str, Any]:
        """Fetch USDA crop progress observations."""
        from tradingagents.strategies.data_sources import usda_source as usda_module

        source = self.registry.get("usda")
        if source is None:
            return {}
        scopes = {
            commodity: usda_module.condition_scope(commodity, trading_date)
            for commodity in ("CORN", "SOYBEANS", "WHEAT")
        }
        active_commodities = [
            commodity for commodity, scope in scopes.items() if scope["active_classes"]
        ]
        crop_progress: dict[str, Any] = {}
        failures: list[str] = []
        failure_details: list[tuple[str, int | None, int]] = []
        crop_coverage: dict[str, Any] = {}

        if not active_commodities:
            try:
                usda_module.require_current_vintage(
                    trading_date,
                    vintage_as_of,
                    today=usda_module.current_session_date(),
                )
                if callable(getattr(source, "is_available", None)) and not source.is_available():
                    raise SourceFetchError("USDA access missing", reason_code="provider_error")
            except Exception as exc:
                err = source_fetch_error("USDA crop acquisition incomplete", exc)
                return _tier2_error_payload(
                    err,
                    extra={
                        "crop_progress": {},
                        "coverage": {
                            "complete": False,
                            "crops": {
                                commodity: {**scope, "complete": False}
                                for commodity, scope in scopes.items()
                            },
                        },
                    },
                    has_partial=False,
                )

        for commodity, scope in scopes.items():
            year = scope["reporting_year"]
            if not scope["active_classes"]:
                crop_progress[commodity] = []
                crop_coverage[commodity] = {
                    **scope,
                    "classes": {
                        crop_class: {
                            "required_states": list(required),
                            "active": False,
                            "latest_by_state": {state: None for state in required},
                            "missing_states": sorted(required),
                            "stale_states": [],
                            "missing_latest_states": [],
                            "observation_date": None,
                            "complete": True,
                            "inactive_reason": "outside_declared_comparison_season",
                        }
                        for crop_class, required in scope["class_states"].items()
                    },
                    "complete": True,
                    "off_season_skipped": True,
                    "requested_states": sorted(
                        {state for values in scope["class_states"].values() for state in values}
                    ),
                    "scope_mode": "declared_survey",
                    "requested_reporting_year": year,
                }
                continue
            try:
                options = {"vintage_as_of": vintage_as_of} if vintage_as_of is not None else {}
                observations = source.fetch_crop_progress(
                    commodity, year, as_of=trading_date, **options
                )
                crop_progress[commodity] = list(observations)
                crop_coverage[commodity] = getattr(
                    observations,
                    "coverage",
                    {**scope, "complete": False, "reason": "missing_survey_coverage"},
                )
                if crop_coverage[commodity].get("complete") is not True:
                    failures.append(f"{commodity}: incomplete declared survey coverage")
                    failure_details.append(("invalid_response", None, 0))
            except Exception as exc:
                err = source_fetch_error("USDA crop acquisition incomplete", exc)
                crop_progress.update(err.partial_data.get("crop_progress", {}))
                crop_coverage[commodity] = err.partial_data.get(
                    "coverage", {**scope, "complete": False}
                )
                failures.append(f"{commodity}: {err}")
                failure_details.append((err.reason_code, err.http_status, err.attempts))
        acq_times = [
            str(row["available_at"])
            for weeks in crop_progress.values()
            for row in weeks
            if isinstance(row, Mapping) and row.get("available_at")
        ]
        result: dict[str, Any] = {
            "crop_progress": crop_progress,
            "coverage": {
                "complete": not failures
                and all(row.get("complete") is True for row in crop_coverage.values()),
                "crops": crop_coverage,
            },
        }
        if acq_times:
            acquired = max(acq_times)
            result.update(available_at=acquired, acquired_at=acquired)
        if not active_commodities and not failures:
            result["off_season_skipped"] = True
            result["_coverage"] = {
                "status": "ok",
                "reason": "outside_declared_comparison_season",
            }
        if failures:
            result["error"] = "; ".join(failures)
            result["_coverage"] = _tier2_aggregated_coverage(
                failure_details,
                has_partial=any(crop_progress.values()),
            )
        return result

    def fetch_drought_data(
        self, trading_date: str, vintage_as_of: str | None = None
    ) -> dict[str, Any]:
        """Fetch Drought Monitor severity and composite score."""
        source = self.registry.get("drought_monitor")
        if source is None:
            return {}
        try:
            options = {"vintage_as_of": vintage_as_of} if vintage_as_of is not None else {}
            severity = source.fetch_drought_severity(end=trading_date, **options)
            composite = source.fetch_composite_score(date=trading_date, **options)
            result: dict[str, Any] = {"composite_score": composite, "states": severity}
            acq_times = [
                str(row["available_at"])
                for row in severity.values()
                if isinstance(row, Mapping) and row.get("available_at")
            ]
            if acq_times:
                acquired = max(acq_times)
                result.update(available_at=acquired, acquired_at=acquired)
            return result
        except Exception as exc:
            err = source_fetch_error("Drought Monitor acquisition failed", exc)
            return _tier2_error_payload(err)

    def fetch_cftc_data(
        self, trading_date: str | None = None, vintage_as_of: str | None = None
    ) -> dict[str, Any]:
        """Fetch CFTC COT positioning data for commodity macro strategy."""
        source = self.registry.get("cftc")
        if source is None:
            return {}
        try:
            payload = source.fetch(
                {
                    "method": "cot_positioning",
                    "commodities": ["gold", "silver", "crude_oil", "nat_gas", "copper"],
                    "lookback_weeks": 52,
                    "as_of": trading_date,
                    **({"vintage_as_of": vintage_as_of} if vintage_as_of is not None else {}),
                }
            )
        except Exception as exc:
            err = source_fetch_error("CFTC COT acquisition failed", exc)
            return _tier2_error_payload(err)
        if isinstance(payload, dict) and payload.get("error") and "_coverage" not in payload:
            err_text = str(payload["error"])
            reason = (
                err_text
                if err_text in {"invalid_response", "http_error", "timeout", "transport_error", "provider_error"}
                else "provider_error"
            )
            payload = {
                **payload,
                "_coverage": _tier2_error_coverage(
                    status="partial"
                    if any(key not in {"error", "_coverage"} for key in payload)
                    else "unavailable",
                    reason_code=reason,
                    http_status=None,
                    attempts=1,
                ),
            }
        return payload
