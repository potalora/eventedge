"""Finnhub data source for earnings transcripts and company news.

Free tier: 60 calls/min. Used by P1/P2 (earnings call analysis)
and P6 (supply chain disruption news).
"""
from __future__ import annotations

import logging
import math
import os
import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import requests
from urllib3.exceptions import ProtocolError as Urllib3ProtocolError
from urllib3.exceptions import TimeoutError as Urllib3TimeoutError

logger = logging.getLogger(__name__)

_RATE_DELAY = 1.1  # ~60 calls/min → 1 call/sec with margin
_MIN_REQUEST_BUDGET_S = 0.05
_MAX_ATTEMPTS_CAP = 4
_MAX_REQUEST_TIMEOUT_S = 60.0
_MAX_BACKOFF_S = 30.0
_FINNHUB_API_BASE_URL = "https://api.finnhub.io/api/v1"

# The engine normally stops waiting for all API-key sources after 300 seconds.
# This source's one scheduling budget defaults to 240 seconds and is capped at
# 270 seconds, preserving at least a 30-second margin. Requests connect/read
# values are inactivity timeouts, not total wall-clock cancellation: a current
# request can overrun this deadline, but no later request or retry may start.
_DEFAULT_WORKFLOW_BUDGET_S = 240.0
_MAX_WORKFLOW_BUDGET_S = 270.0
_RETRYABLE_SERVER_STATUS_CODES = frozenset({500, 502, 503, 504})

_DEFAULT_RELIABILITY: dict[str, Any] = {
    "rate_delay_s": _RATE_DELAY,
    "workflow_budget_s": _DEFAULT_WORKFLOW_BUDGET_S,
    "earnings_calendar": {
        "connect_timeout_s": 5.0,
        "read_timeout_s": 30.0,
        "max_attempts": 3,
        "base_backoff_s": 2.0,
        "max_backoff_s": 6.0,
        "jitter_s": 0.5,
    },
    "company_peers": {
        "connect_timeout_s": 5.0,
        "read_timeout_s": 15.0,
        "max_attempts": 2,
        "base_backoff_s": 2.0,
        "max_backoff_s": 4.0,
        "jitter_s": 0.5,
    },
    "company_news": {
        "connect_timeout_s": 5.0,
        "read_timeout_s": 15.0,
        "max_attempts": 3,
        "base_backoff_s": 2.0,
        "max_backoff_s": 6.0,
        "jitter_s": 0.5,
    },
}


@dataclass(frozen=True)
class _RetryPolicy:
    connect_timeout_s: float
    read_timeout_s: float
    max_attempts: int
    base_backoff_s: float
    max_backoff_s: float
    jitter_s: float
    retry_transport: bool = True
    retry_server_errors: bool = True
    pass_timeout: bool = True


_RATE_LIMIT_POLICY = _RetryPolicy(
    connect_timeout_s=0.0,
    read_timeout_s=0.0,
    max_attempts=4,
    base_backoff_s=2.0,
    max_backoff_s=8.0,
    jitter_s=0.0,
    retry_transport=True,
    retry_server_errors=True,
    pass_timeout=False,
)


def _bounded_float(
    value: Any,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed < minimum:
        return default
    return min(parsed, maximum)


def _build_policy(config: Mapping[str, Any], endpoint: str) -> _RetryPolicy:
    defaults = _DEFAULT_RELIABILITY[endpoint]
    supplied = config.get(endpoint, {})
    if not isinstance(supplied, Mapping):
        supplied = {}

    try:
        max_attempts = int(supplied.get("max_attempts", defaults["max_attempts"]))
    except (OverflowError, TypeError, ValueError):
        max_attempts = defaults["max_attempts"]

    return _RetryPolicy(
        connect_timeout_s=_bounded_float(
            supplied.get("connect_timeout_s"),
            defaults["connect_timeout_s"],
            minimum=_MIN_REQUEST_BUDGET_S,
            maximum=_MAX_REQUEST_TIMEOUT_S,
        ),
        read_timeout_s=_bounded_float(
            supplied.get("read_timeout_s"),
            defaults["read_timeout_s"],
            minimum=_MIN_REQUEST_BUDGET_S,
            maximum=_MAX_REQUEST_TIMEOUT_S,
        ),
        max_attempts=min(max(max_attempts, 1), _MAX_ATTEMPTS_CAP),
        base_backoff_s=_bounded_float(
            supplied.get("base_backoff_s"),
            defaults["base_backoff_s"],
            maximum=_MAX_BACKOFF_S,
        ),
        max_backoff_s=_bounded_float(
            supplied.get("max_backoff_s"),
            defaults["max_backoff_s"],
            maximum=_MAX_BACKOFF_S,
        ),
        jitter_s=_bounded_float(
            supplied.get("jitter_s"),
            defaults["jitter_s"],
            maximum=_MAX_BACKOFF_S,
        ),
    )


class _FinnhubHTTPAdapter:
    """Small project-owned adapter for Finnhub's documented HTTP endpoints."""

    def __init__(
        self,
        api_key: str,
        session: requests.Session,
        base_url: str = _FINNHUB_API_BASE_URL,
    ) -> None:
        self._api_key = api_key
        self._session = session
        self._base_url = base_url.rstrip("/")

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        timeout: tuple[float, float],
    ) -> Any:
        query = dict(params or {})
        query["token"] = self._api_key
        response = self._session.get(
            f"{self._base_url}/{path.lstrip('/')}",
            params=query,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()


class FinnhubSource:
    """Data source backed by the Finnhub API."""

    name: str = "finnhub"
    requires_api_key: bool = True

    def __init__(
        self,
        api_key: str | None = None,
        reliability_config: Mapping[str, Any] | None = None,
        *,
        sleep_fn: Callable[[float], None] | None = None,
        monotonic_fn: Callable[[], float] | None = None,
        jitter_fn: Callable[[float], float] | None = None,
        http_session: requests.Session | None = None,
        http_base_url: str = _FINNHUB_API_BASE_URL,
    ) -> None:
        self._api_key = api_key or os.environ.get("FINNHUB_API_KEY", "")
        self._cache: dict[str, Any] = {}
        self._client = None
        reliability = (
            reliability_config
            if isinstance(reliability_config, Mapping)
            else {}
        )
        self._policies = {
            endpoint: _build_policy(reliability, endpoint)
            for endpoint in ("earnings_calendar", "company_peers", "company_news")
        }
        self._rate_delay_s = _bounded_float(
            reliability.get("rate_delay_s"),
            _DEFAULT_RELIABILITY["rate_delay_s"],
            maximum=10.0,
        )
        self._workflow_budget_s = _bounded_float(
            reliability.get("workflow_budget_s"),
            _DEFAULT_RELIABILITY["workflow_budget_s"],
            minimum=_MIN_REQUEST_BUDGET_S,
            maximum=_MAX_WORKFLOW_BUDGET_S,
        )
        self._sleep_fn = sleep_fn
        self._monotonic_fn = monotonic_fn
        self._jitter_fn = jitter_fn
        session = http_session or requests.Session()
        self._http = _FinnhubHTTPAdapter(
            self._api_key,
            session,
            base_url=http_base_url,
        )

    def _get_client(self):
        if self._client is None:
            import finnhub
            self._client = finnhub.Client(api_key=self._api_key)
        return self._client

    def _sleep(self, delay: float) -> None:
        if delay > 0:
            (self._sleep_fn or time.sleep)(delay)

    def _monotonic(self) -> float:
        return (self._monotonic_fn or time.monotonic)()

    def new_workflow_deadline(self, max_budget_s: float | None = None) -> float:
        """Return the one cooperative scheduling deadline for a Finnhub fetch.

        The deadline prevents subsequent HTTP calls and retries from starting.
        It cannot interrupt a request already blocked inside requests; requests'
        connect and read values bound connection attempts and socket inactivity,
        respectively, rather than total end-to-end wall-clock time.
        """
        budget_s = self._workflow_budget_s
        if max_budget_s is not None:
            budget_s = min(
                budget_s,
                _bounded_float(
                    max_budget_s,
                    0.0,
                    maximum=_MAX_WORKFLOW_BUDGET_S,
                ),
            )
        return self._monotonic() + budget_s

    def _resolve_deadline(self, deadline: float | None) -> float:
        return deadline if deadline is not None else self.new_workflow_deadline()

    def _has_budget(self, deadline: float) -> bool:
        return deadline - self._monotonic() > _MIN_REQUEST_BUDGET_S

    def _jitter(self, maximum: float) -> float:
        if maximum <= 0:
            return 0.0
        if self._jitter_fn is not None:
            return min(max(self._jitter_fn(maximum), 0.0), maximum)
        return random.uniform(0.0, maximum)

    def _safe_error_message(self, exc: BaseException) -> str:
        """Return a bounded exception message with API credentials redacted."""
        message = str(exc) or "<empty>"
        if self._api_key:
            message = message.replace(self._api_key, "<redacted>")
        message = re.sub(
            r"([?&]token=)[^&\s]+",
            r"\1<redacted>",
            message,
            flags=re.IGNORECASE,
        )
        return message[:500]

    def _wait_for_request_slot(
        self,
        deadline: float,
        *,
        strategy: str,
        endpoint: str,
        symbol: str = "",
    ) -> bool:
        """Apply the rate delay only when enough shared scheduling budget remains."""
        remaining = deadline - self._monotonic()
        if remaining <= _MIN_REQUEST_BUDGET_S:
            logger.warning(
                "Finnhub request skipped strategy=%s endpoint=%s symbol=%s "
                "reason=workflow_deadline",
                strategy,
                endpoint,
                symbol or "-",
            )
            return False
        if self._rate_delay_s > 0:
            if remaining <= self._rate_delay_s + _MIN_REQUEST_BUDGET_S:
                logger.warning(
                    "Finnhub request skipped strategy=%s endpoint=%s symbol=%s "
                    "reason=workflow_deadline",
                    strategy,
                    endpoint,
                    symbol or "-",
                )
                return False
            self._sleep(self._rate_delay_s)
        return self._has_budget(deadline)

    @staticmethod
    def _status_code(exc: BaseException) -> int | None:
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            status = getattr(current, "status_code", None)
            if status is None:
                status = getattr(getattr(current, "response", None), "status_code", None)
            try:
                if status is not None:
                    return int(status)
            except (TypeError, ValueError):
                pass
            current = current.__cause__ or current.__context__
        return None

    @staticmethod
    def _is_transport_error(exc: BaseException) -> bool:
        current: BaseException | None = exc
        seen: set[int] = set()
        transport_types = (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            Urllib3TimeoutError,
            Urllib3ProtocolError,
        )
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, transport_types):
                return True
            current = current.__cause__ or current.__context__
        return False

    @classmethod
    def _is_retryable(cls, exc: BaseException, policy: _RetryPolicy) -> bool:
        status = cls._status_code(exc)
        message = str(exc).lower()
        if status == 429 or (status is None and ("429" in message or "limit reached" in message)):
            return True
        if (
            policy.retry_server_errors
            and status in _RETRYABLE_SERVER_STATUS_CODES
        ):
            return True
        return policy.retry_transport and cls._is_transport_error(exc)

    @staticmethod
    def _request_timeout(policy: _RetryPolicy, remaining_s: float) -> tuple[float, float]:
        """Cap inactivity timeouts to the remaining cooperative budget.

        These values do not form a total request deadline. In particular,
        requests may apply connect timeout per resolved address and read timeout
        between received bytes.
        """
        inactivity_cap = max(remaining_s, _MIN_REQUEST_BUDGET_S)
        return (
            min(policy.connect_timeout_s, inactivity_cap),
            min(policy.read_timeout_s, inactivity_cap),
        )

    def _call_with_retry(
        self,
        fn,
        *args,
        policy: _RetryPolicy | None = None,
        strategy: str = "shared",
        endpoint: str | None = None,
        symbol: str = "",
        deadline: float | None = None,
        **kwargs,
    ):
        """Call Finnhub with an endpoint-aware transient-error policy.

        Timeouts, connection errors, 429s, and retryable 5xx responses may be
        retried. Permanent 4xx responses are re-raised immediately. The shared
        workflow deadline is checked before every attempt and after every
        backoff, so no follow-on request starts once it has expired.
        """
        selected = policy or _RATE_LIMIT_POLICY
        endpoint_name = endpoint or getattr(fn, "__name__", "unknown")
        started = self._monotonic()
        effective_deadline = self._resolve_deadline(deadline)
        last_exc: BaseException | None = None
        attempts_made = 0

        for attempt in range(1, selected.max_attempts + 1):
            remaining = effective_deadline - self._monotonic()
            if remaining <= 0:
                break
            call_kwargs = dict(kwargs)
            if selected.pass_timeout:
                call_kwargs["timeout"] = self._request_timeout(selected, remaining)
            attempts_made = attempt
            try:
                result = fn(*args, **call_kwargs)
                if attempt > 1:
                    logger.info(
                        "Finnhub request recovered strategy=%s endpoint=%s symbol=%s "
                        "attempt=%d/%d elapsed_s=%.3f",
                        strategy,
                        endpoint_name,
                        symbol or "-",
                        attempt,
                        selected.max_attempts,
                        self._monotonic() - started,
                    )
                return result
            except Exception as exc:
                last_exc = exc
                status = self._status_code(exc)
                elapsed = self._monotonic() - started
                if not self._is_retryable(exc, selected):
                    logger.error(
                        "Finnhub request non_retryable strategy=%s endpoint=%s symbol=%s "
                        "attempt=%d/%d status=%s error_type=%s error_message=%s "
                        "elapsed_s=%.3f",
                        strategy,
                        endpoint_name,
                        symbol or "-",
                        attempt,
                        selected.max_attempts,
                        status if status is not None else "-",
                        type(exc).__name__,
                        self._safe_error_message(exc),
                        elapsed,
                    )
                    raise
                if attempt >= selected.max_attempts:
                    break

                remaining = effective_deadline - self._monotonic()
                delay = min(
                    selected.max_backoff_s,
                    (
                        selected.base_backoff_s * (2 ** (attempt - 1))
                        + self._jitter(selected.jitter_s)
                    ),
                )
                if remaining <= delay + _MIN_REQUEST_BUDGET_S:
                    break
                logger.warning(
                    "Finnhub request retry strategy=%s endpoint=%s symbol=%s "
                    "attempt=%d/%d status=%s error_type=%s error_message=%s "
                    "delay_s=%.3f elapsed_s=%.3f",
                    strategy,
                    endpoint_name,
                    symbol or "-",
                    attempt,
                    selected.max_attempts,
                    status if status is not None else "-",
                    type(exc).__name__,
                    self._safe_error_message(exc),
                    delay,
                    elapsed,
                )
                self._sleep(delay)

        if last_exc is None:
            last_exc = TimeoutError("Finnhub request deadline elapsed before an attempt")
        logger.error(
            "Finnhub request exhausted strategy=%s endpoint=%s symbol=%s "
            "attempt=%d/%d status=%s error_type=%s error_message=%s elapsed_s=%.3f",
            strategy,
            endpoint_name,
            symbol or "-",
            attempts_made,
            selected.max_attempts,
            self._status_code(last_exc) or "-",
            type(last_exc).__name__,
            self._safe_error_message(last_exc),
            self._monotonic() - started,
        )
        raise last_exc

    def fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        method = params.get("method", "earnings_transcripts")
        dispatch = {
            "earnings_transcripts": self._dispatch_transcripts,
            "company_news": self._dispatch_news,
            "supply_chain": self._dispatch_supply_chain,
        }
        handler = dispatch.get(method)
        if handler is None:
            return {"error": f"Unknown method '{method}'"}
        try:
            return handler(params)
        except Exception:
            logger.error("FinnhubSource.fetch(%s) failed", method, exc_info=True)
            return {"error": f"{method} fetch failed"}

    def is_available(self) -> bool:
        return bool(self._api_key)

    def fetch_recent_earnings(
        self,
        date_from: str,
        date_to: str,
        *,
        deadline: float | None = None,
    ) -> list[dict]:
        """Fetch earnings calendar — which companies just reported.

        Returns list of dicts with symbol, date, epsActual, epsEstimate, etc.
        Free tier endpoint (transcripts require paid plan).
        """
        cache_key = f"earnings_cal|{date_from}|{date_to}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        workflow_deadline = self._resolve_deadline(deadline)
        if not self._wait_for_request_slot(
            workflow_deadline,
            strategy="earnings_call",
            endpoint="earnings_calendar",
        ):
            return []
        try:
            result = self._call_with_retry(
                self._http.get,
                "/calendar/earnings",
                params={
                    "from": date_from,
                    "to": date_to,
                    "symbol": "",
                    "international": "false",
                },
                policy=self._policies["earnings_calendar"],
                strategy="earnings_call",
                endpoint="earnings_calendar",
                deadline=workflow_deadline,
            )
            events = result.get("earningsCalendar", [])
            # Filter to those with actual results (already reported)
            reported = [
                e for e in events
                if e.get("epsActual") is not None
            ]
            self._cache[cache_key] = reported
            logger.info(
                "Finnhub fetch complete strategy=earnings_call endpoint=earnings_calendar "
                "candidate_count=%d qualifying_count=%d",
                len(events),
                len(reported),
            )
            return reported
        except Exception as exc:  # noqa: BLE001 - graceful source degradation boundary
            logger.error(
                "Finnhub fetch degraded strategy=earnings_call endpoint=earnings_calendar "
                "candidate_count=0 qualifying_count=0 error_type=%s error_message=%s",
                type(exc).__name__,
                self._safe_error_message(exc),
            )
            return []

    def fetch_earnings_news(
        self,
        symbol: str,
        earnings_date: str,
        *,
        deadline: float | None = None,
    ) -> list[dict]:
        """Fetch news around an earnings date as a proxy for transcript analysis.

        Gets news from 1 day before to 2 days after earnings to capture
        call commentary, analyst reactions, and guidance discussion.
        """
        from datetime import date, timedelta

        try:
            dt = date.fromisoformat(earnings_date)
        except ValueError as exc:
            logger.error(
                "Finnhub fetch degraded strategy=earnings_call endpoint=company_news "
                "symbol=%s error_type=%s error_message=%s",
                symbol,
                type(exc).__name__,
                self._safe_error_message(exc),
            )
            return []
        date_from = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        date_to = (dt + timedelta(days=2)).strftime("%Y-%m-%d")

        return self.fetch_company_news(
            symbol,
            date_from,
            date_to,
            deadline=deadline,
        )

    def fetch_company_news(
        self,
        symbol: str,
        date_from: str,
        date_to: str,
        *,
        deadline: float | None = None,
    ) -> list[dict]:
        """Fetch company news articles."""
        cache_key = f"news|{symbol}|{date_from}|{date_to}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        workflow_deadline = self._resolve_deadline(deadline)
        if not self._wait_for_request_slot(
            workflow_deadline,
            strategy="company_news",
            endpoint="company_news",
            symbol=symbol,
        ):
            return []
        try:
            news = self._call_with_retry(
                self._http.get,
                "/company-news",
                params={"symbol": symbol, "from": date_from, "to": date_to},
                policy=self._policies["company_news"],
                strategy="company_news",
                endpoint="company_news",
                symbol=symbol,
                deadline=workflow_deadline,
            )
            result = [
                {
                    "headline": n.get("headline", ""),
                    "summary": n.get("summary", ""),
                    "source": n.get("source", ""),
                    "datetime": n.get("datetime", 0),
                    "url": n.get("url", ""),
                    "category": n.get("category", ""),
                }
                for n in (news or [])
            ]
            self._cache[cache_key] = result
            return result
        except Exception as exc:  # noqa: BLE001 - graceful source degradation boundary
            logger.error(
                "Finnhub fetch degraded strategy=company_news endpoint=company_news "
                "symbol=%s error_type=%s error_message=%s",
                symbol,
                type(exc).__name__,
                self._safe_error_message(exc),
            )
            return []

    def fetch_supply_chain(
        self,
        symbol: str,
        *,
        deadline: float | None = None,
    ) -> list[dict]:
        """Fetch supply chain relationships (peers/suppliers/customers)."""
        cache_key = f"supply|{symbol}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        workflow_deadline = self._resolve_deadline(deadline)
        if not self._wait_for_request_slot(
            workflow_deadline,
            strategy="supply_chain",
            endpoint="company_peers",
            symbol=symbol,
        ):
            return []
        try:
            peers = self._call_with_retry(
                self._http.get,
                "/stock/peers",
                params={"symbol": symbol},
                policy=self._policies["company_peers"],
                strategy="supply_chain",
                endpoint="company_peers",
                symbol=symbol,
                deadline=workflow_deadline,
            )
            result = [{"ticker": p, "relationship": "peer"} for p in (peers or [])]
            self._cache[cache_key] = result
            logger.info(
                "Finnhub fetch complete strategy=supply_chain endpoint=company_peers "
                "symbol=%s candidate_count=%d qualifying_count=%d",
                symbol,
                len(peers or []),
                len(result),
            )
            return result
        except Exception as exc:  # noqa: BLE001 - graceful source degradation boundary
            logger.error(
                "Finnhub fetch degraded strategy=supply_chain endpoint=company_peers "
                "symbol=%s candidate_count=0 qualifying_count=0 "
                "error_type=%s error_message=%s",
                symbol,
                type(exc).__name__,
                self._safe_error_message(exc),
            )
            return []

    def fetch_supply_chains(
        self,
        symbols: list[str],
        *,
        deadline: float | None = None,
    ) -> dict[str, list[dict]]:
        """Fetch a deduplicated peer batch serially under one shared deadline.

        Serial concurrency is intentional: the source uses a shared requests
        session and Finnhub's free tier permits roughly one call per second.
        """
        unique_symbols = list(dict.fromkeys(symbol for symbol in symbols if symbol))
        started = self._monotonic()
        workflow_deadline = self._resolve_deadline(deadline)
        chains: dict[str, list[dict]] = {}
        attempted = 0

        for symbol in unique_symbols:
            if not self._has_budget(workflow_deadline):
                break
            attempted += 1
            peers = self.fetch_supply_chain(symbol, deadline=workflow_deadline)
            if peers:
                chains[symbol] = peers

        deadline_exhausted = (
            attempted < len(unique_symbols) or not self._has_budget(workflow_deadline)
        )
        logger.info(
            "Finnhub batch complete strategy=supply_chain endpoint=company_peers "
            "concurrency=1 candidate_count=%d attempted_count=%d qualifying_count=%d "
            "deadline_exhausted=%s elapsed_s=%.3f",
            len(unique_symbols),
            attempted,
            len(chains),
            str(deadline_exhausted).lower(),
            self._monotonic() - started,
        )
        return chains

    def fetch_earnings_transcript(
        self, symbol: str, year: int, quarter: int,
    ) -> str | None:
        """Fetch real earnings call transcript from Finnhub.

        Returns full transcript text, or None if unavailable (paid-tier, not found, etc).
        """
        cache_key = f"transcript|{symbol}|{year}|{quarter}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        client = self._get_client()
        self._sleep(self._rate_delay_s)
        try:
            result = client.earnings_call_transcripts(symbol, year, quarter)
        except Exception as exc:
            # 403 / payment-required / network errors
            logger.debug(
                "Transcript unavailable for %s %dQ%d: %s", symbol, year, quarter, exc,
            )
            return None

        if not result:
            return None

        # Response is a list of segments: [{name, speech}, ...]
        transcript = result if isinstance(result, list) else result.get("transcript", [])
        if not transcript:
            return None

        text = "\n".join(
            f"{seg.get('name', 'Speaker')}: {seg.get('speech', '')}"
            for seg in transcript
            if seg.get("speech")
        )
        if not text:
            return None

        self._cache[cache_key] = text
        return text

    def clear_cache(self) -> None:
        self._cache.clear()

    def _dispatch_transcripts(self, params: dict[str, Any]) -> dict[str, Any]:
        date_from = params.get("date_from", "")
        date_to = params.get("date_to", "")
        return {"data": self.fetch_recent_earnings(date_from, date_to)}

    def _dispatch_news(self, params: dict[str, Any]) -> dict[str, Any]:
        symbol = params.get("symbol", "")
        date_from = params.get("date_from", "")
        date_to = params.get("date_to", "")
        return {"data": self.fetch_company_news(symbol, date_from, date_to)}

    def _dispatch_supply_chain(self, params: dict[str, Any]) -> dict[str, Any]:
        symbol = params.get("symbol", "")
        return {"data": self.fetch_supply_chain(symbol)}
