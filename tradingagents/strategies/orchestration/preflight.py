"""Pre-run screen integrity and governed market-data checks.

``screen`` runs the daily cycle's shared multi-source fetch followed by
per-horizon strategy screens and validates every candidate through the same
event-identity gates that ``screen_and_stage`` applies
(``canonical_event_key`` + ``canonical_observation_time``). No generation
state is written, no LLM is called, and nothing is staged or executed, so
the check can run hours before the scheduled cycle.

``governed`` derives the exact P0 ticker scope from immutable read-only state
and, after the XNYS close, exercises the shared governed-bar resolver with
``persist=False``. ``all`` composes both checks.

Purpose: catch integration defects (source payloads whose shape no longer
satisfies staging, broken screens) while there is still time to fix and
rerun the same session, instead of discovering them in the scheduled run.
From 2026-08-03 through 2026-08-06 a naive USASpending timestamp raised
during staging and failed 16/16 cohorts in every active generation for
four consecutive sessions; this check exists so that class of failure
surfaces before the run instead of after it.

Notes:
- The check runs on the session date regardless of whether the market has
  closed, so same-day bars may be absent from prices; signal counts can
  differ from the real run. The gates validated here (screen exceptions,
  candidate identity/observation-time conformance) are unaffected.
- Candidates with empty tickers are counted as ``pending_llm`` and are not
  gate-checked: production ``screen_and_enrich`` discards empty-ticker
  signals before staging (LLM enrichment resolves tickers first for
  ``needs_llm_analysis`` candidates from regulatory_pipeline/litigation).
  Staging of LLM-resolved candidates is therefore outside this no-LLM
  check's coverage; everything the deterministic path stages is covered.
- Candidate screening is deliberately re-run per horizon, mirroring the
  four screen passes of the daily cycle.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PREFLIGHT_MODES = frozenset({"screen", "governed", "all"})
_MAX_RECOVERY_SUMMARIES = 64
_MAX_REPORT_TEXT = 4_096
_LOWER_HEX = frozenset("0123456789abcdef")


def run_preflight(
    config: dict[str, Any],
    trading_date: str,
    *,
    engine: Any | None = None,
    mode: str = "screen",
    price_source: Any | None = None,
    processed_at: datetime | None = None,
    state_context_factory: Any | None = None,
    governed_resolver: Any | None = None,
) -> dict[str, Any]:
    """Run the selected screen and/or read-only governed-data checks."""
    from tradingagents.strategies.orchestration.trading_calendar import is_session

    if mode not in _PREFLIGHT_MODES:
        raise ValueError(f"invalid preflight mode {mode!r}")
    session = date.fromisoformat(trading_date)
    if not is_session(session):
        raise ValueError(f"{trading_date} is not an XNYS session")
    report = (
        _run_screen_preflight(config, trading_date, engine=engine)
        if mode in {"screen", "all"}
        else {
            "trading_date": trading_date,
            "fetched_sources": [],
            "horizons": {},
            "failures": [],
            "ok": True,
        }
    )
    if mode in {"screen", "all"}:
        report["screen_ok"] = bool(report["ok"])
        report["screen_failures"] = list(report["failures"])
    if mode in {"governed", "all"}:
        governed_report = _run_governed_preflight(
            config,
            session,
            price_source=price_source,
            processed_at=processed_at,
            state_context_factory=state_context_factory,
            governed_resolver=governed_resolver,
        )
        report["governed_ok"] = bool(governed_report["ok"])
        report["governed_failures"] = list(governed_report["failures"])
        report.update(
            {
                key: value
                for key, value in governed_report.items()
                if key not in {"ok", "failures"}
            }
        )
        report["failures"].extend(governed_report["failures"])
        report["ok"] = bool(report["ok"] and governed_report["ok"])
    return report


def _bounded_recovery_summaries(
    summaries: tuple[Any, ...],
) -> list[dict[str, Any]]:
    if len(summaries) > _MAX_RECOVERY_SUMMARIES:
        raise ValueError("governed recovery summaries are unbounded")
    bounded: list[dict[str, Any]] = []
    fields = (
        "ticker",
        "session",
        "recovery_id",
        "contract_version",
        "evidence_digest",
    )
    for summary in summaries:
        row: dict[str, Any] = {}
        for field in fields:
            value = summary.get(field)
            if not isinstance(value, str) or not value or len(value) > _MAX_REPORT_TEXT:
                raise ValueError("governed recovery summary is invalid")
            row[field] = value
        cohorts = summary.get("affected_cohort_ids")
        if isinstance(cohorts, (str, bytes)):
            raise ValueError("governed recovery summary is invalid")
        normalized = tuple(sorted(str(value) for value in cohorts))
        if (
            not normalized
            or len(normalized) > 64
            or len(set(normalized)) != len(normalized)
            or any(not value or len(value) > _MAX_REPORT_TEXT for value in normalized)
        ):
            raise ValueError("governed recovery summary is invalid")
        row["affected_cohort_ids"] = list(normalized)
        bounded.append(row)
    return bounded


def _canonical_failure_map(tickers: tuple[str, ...], session: date) -> dict[str, str]:
    return {ticker: f"invalid {ticker}/{session.isoformat()}" for ticker in tickers}


def _normalized_failure(ticker: str, session: date, reason: object) -> bool:
    if not isinstance(reason, str) or len(reason) > _MAX_REPORT_TEXT:
        return False
    kind, separator, scope = reason.partition(" ")
    return (
        bool(separator)
        and kind in {"missing", "incoherent", "invalid", "invalid_benchmark"}
        and scope == f"{ticker}/{session.isoformat()}"
    )


def _fixed_digest(value: object, prefix: str) -> bool:
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    suffix = value.removeprefix(prefix)
    return len(suffix) == 64 and all(character in _LOWER_HEX for character in suffix)


def _validate_governed_resolution(
    resolution: Any,
    *,
    snapshot: Any,
    session: date,
    processed_at: datetime,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    from tradingagents.strategies.execution.models import MarketBar
    from tradingagents.strategies.execution.price_source import (
        validate_required_bars,
    )
    from tradingagents.strategies.metrics.models import (
        GOVERNED_BAR_RECOVERY_CONTRACT,
    )
    from tradingagents.strategies.orchestration.governed_market_data import (
        GovernedInputResolution,
        GovernedRecoveryBinding,
    )
    from tradingagents.strategies.orchestration.trading_calendar import session_close

    if type(resolution) is not GovernedInputResolution:
        raise ValueError("governed resolution type is invalid")
    expected = set(snapshot.governed_tickers)
    bars = set(resolution.bars)
    failures = set(resolution.failure_map)
    if (
        not expected
        or bars & failures
        or bars | failures != expected
        or not bars.issubset(expected)
        or not failures.issubset(expected)
    ):
        raise ValueError("governed resolution scope is invalid")
    for ticker in sorted(failures):
        if not _normalized_failure(ticker, session, resolution.failure_map[ticker]):
            raise ValueError("governed failure is invalid")
    raw_bars: dict[tuple[str, date], MarketBar] = {}
    for ticker in sorted(bars):
        bar = resolution.bars[ticker]
        if type(bar) is not MarketBar or bar.ticker != ticker or bar.session != session:
            raise ValueError("governed bar identity is invalid")
        if bar.fetched_at < session_close(session):
            raise ValueError("governed bar predates the session close")
        raw_bars[(ticker, session)] = bar
    validate_required_bars(raw_bars, bars, session, processed_at)
    reconstructed = {
        ticker
        for ticker in bars
        if resolution.bars[ticker].source == "yfinance-60m-reconstruction"
    }
    if set(resolution.recovery_bindings) != reconstructed:
        raise ValueError("governed recovery binding source is invalid")

    recoveries = _bounded_recovery_summaries(resolution.recovery_summaries)
    summary_by_ticker: dict[str, dict[str, Any]] = {}
    for summary in recoveries:
        ticker = summary["ticker"]
        if (
            ticker in summary_by_ticker
            or ticker not in bars
            or summary["session"] != session.isoformat()
            or summary["contract_version"] != GOVERNED_BAR_RECOVERY_CONTRACT
            or not _fixed_digest(
                summary["recovery_id"], "governed_bar_recovery:"
            )
            or not _fixed_digest(summary["evidence_digest"], "sha256:")
            or tuple(summary["affected_cohort_ids"])
            != snapshot.cohort_ids_by_ticker[ticker]
        ):
            raise ValueError("governed recovery summary scope is invalid")
        summary_by_ticker[ticker] = summary
    if set(resolution.recovery_bindings) != set(summary_by_ticker):
        raise ValueError("governed recovery binding scope is invalid")
    for ticker, binding in resolution.recovery_bindings.items():
        summary = summary_by_ticker[ticker]
        if (
            type(binding) is not GovernedRecoveryBinding
            or binding.ticker != ticker
            or binding.recovery_id != summary["recovery_id"]
            or binding.contract_version != summary["contract_version"]
            or binding.evidence_digest != summary["evidence_digest"]
        ):
            raise ValueError("governed recovery binding is invalid")
    return recoveries, {
        ticker: resolution.failure_map[ticker] for ticker in sorted(failures)
    }


def _governed_snapshot_report(
    base: dict[str, Any],
    *,
    snapshot: Any,
    metric_store: Any | None,
    session: date,
    now: datetime,
    price_source: Any | None,
    resolve: Any,
) -> dict[str, Any]:
    from tradingagents.strategies.orchestration.governed_market_data import (
        GovernedMarketDataError,
    )
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
    )
    from tradingagents.strategies.orchestration.trading_calendar import session_close

    base["state_status"] = snapshot.state_status
    base["governed_tickers"] = list(snapshot.governed_tickers)
    if snapshot.state_status == "state_already_invalid":
        base["governed_probe_status"] = "state_already_invalid"
        base["failures"].append(
            {
                "horizon": None,
                "strategy": "governed_market_data",
                "ticker": None,
                "error": "state_already_invalid",
            }
        )
        return base
    if now < session_close(session):
        base["governed_probe_status"] = "not_ready"
        base["ok"] = True
        return base

    if price_source is None:
        from tradingagents.strategies.execution.price_source import YFinancePriceSource

        price_source = YFinancePriceSource()
    try:
        resolution = resolve(
            price_source=price_source,
            metric_store=metric_store,
            epoch_id=snapshot.epoch_id,
            session=session,
            tickers=snapshot.governed_tickers,
            cohort_ids_by_ticker=snapshot.cohort_ids_by_ticker,
            processed_at=now,
            persist=False,
        )
        recoveries, failure_map = _validate_governed_resolution(
            resolution,
            snapshot=snapshot,
            session=session,
            processed_at=now,
        )
    except GovernedMarketDataError as error:
        recoveries = []
        try:
            raw_failures = dict(error.failure_map)
        except Exception:
            raw_failures = {}
        governed = set(snapshot.governed_tickers)
        if (
            not raw_failures
            or not set(raw_failures).issubset(governed)
            or any(
                not _normalized_failure(ticker, session, reason)
                for ticker, reason in raw_failures.items()
            )
        ):
            failure_map = _canonical_failure_map(snapshot.governed_tickers, session)
        else:
            failure_map = {
                ticker: raw_failures.get(
                    ticker, f"invalid {ticker}/{session.isoformat()}"
                )
                for ticker in snapshot.governed_tickers
            }
    except PreflightStateError:
        raise
    except Exception:
        failure_map = _canonical_failure_map(snapshot.governed_tickers, session)
        recoveries = []
    base["governed_bar_recoveries"] = recoveries
    base["governed_failure_map"] = failure_map
    if failure_map:
        base["failures"].extend(
            {
                "horizon": None,
                "strategy": "governed_market_data",
                "ticker": ticker,
                "error": reason,
            }
            for ticker, reason in failure_map.items()
        )
        return base
    base["governed_probe_status"] = "ready"
    base["ok"] = True
    return base


def _run_governed_preflight(
    config: dict[str, Any],
    session: date,
    *,
    price_source: Any | None,
    processed_at: datetime | None,
    state_context_factory: Any | None,
    governed_resolver: Any | None,
) -> dict[str, Any]:
    from tradingagents.strategies.orchestration.cohort_orchestrator import (
        build_default_cohorts,
    )
    from tradingagents.strategies.orchestration.governed_market_data import (
        resolve_governed_bars,
    )
    from tradingagents.strategies.orchestration.preflight_state import (
        PreflightStateError,
        inspect_and_guard_preflight_state,
    )

    now = processed_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("processed_at must be timezone-aware")
    ar_config = config.get("autoresearch", {})
    state_dir = Path(ar_config.get("state_dir", "data/state"))
    cohort_ids = tuple(cfg.name for cfg in build_default_cohorts(config))
    benchmarks = tuple(
        ar_config.get("paper_ledger", {}).get("benchmark_symbols", ("SPY", "BIL"))
    )
    resolve = governed_resolver or resolve_governed_bars
    base: dict[str, Any] = {
        "state_status": "invalid",
        "governed_probe_status": "failed",
        "governed_tickers": [],
        "governed_bar_recoveries": [],
        "governed_failure_map": {},
        "failures": [],
        "ok": False,
    }
    try:
        context_factory = state_context_factory or inspect_and_guard_preflight_state
        with context_factory(
            state_dir=state_dir,
            cohort_ids=cohort_ids,
            session=session,
            benchmark_tickers=benchmarks,
        ) as (snapshot, metric_store):
            result = _governed_snapshot_report(
                base,
                snapshot=snapshot,
                metric_store=metric_store,
                session=session,
                now=now,
                price_source=price_source,
                resolve=resolve,
            )
        return result
    except PreflightStateError as error:
        base["ok"] = False
        base["governed_probe_status"] = "failed"
        base["governed_bar_recoveries"] = []
        base["governed_failure_map"] = {}
        base["failures"] = []
        base["failures"].append(
            {
                "horizon": None,
                "strategy": "governed_market_data",
                "ticker": None,
                "error": error.reason,
            }
        )
        return base


def _run_screen_preflight(
    config: dict[str, Any],
    trading_date: str,
    *,
    engine: Any | None = None,
) -> dict[str, Any]:
    """Run the live fetch -> screen -> staging-gate check for one session.

    Args:
        config: Full DEFAULT_CONFIG-style mapping; the autoresearch section
            is used for registry/strategy construction.
        trading_date: Exact XNYS session (YYYY-MM-DD).
        engine: Optional pre-built MultiStrategyEngine (test seam). When
            omitted, one is constructed against a throwaway state dir.

    Returns:
        JSON-serializable report: per-horizon, per-strategy candidate,
        staged, and pending_llm counts, fetch-source list, and an ``ok``
        flag with a ``failures`` list naming every rejected candidate or
        broken screen.

    Raises:
        ValueError: if trading_date is not an XNYS session.
    """
    from tradingagents.strategies.orchestration.event_identity import (
        canonical_event_key,
        canonical_observation_time,
    )
    from tradingagents.strategies.orchestration.trading_calendar import is_session

    session = date.fromisoformat(trading_date)
    if not is_session(session):
        raise ValueError(f"{trading_date} is not an XNYS session")

    work_dir = tempfile.mkdtemp(prefix="eventedge-preflight-")
    try:
        if engine is None:
            from tradingagents.strategies.orchestration.multi_strategy_engine import (
                MultiStrategyEngine,
            )

            # Isolation: preflight must never touch real generation state.
            isolated = dict(config)
            isolated_ar = dict(isolated.get("autoresearch", {}))
            isolated_ar["state_dir"] = work_dir
            isolated["autoresearch"] = isolated_ar
            engine = MultiStrategyEngine(isolated, use_llm=False)

        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            HORIZON_PARAMS,
        )

        lookback_start = (
            datetime.strptime(trading_date, "%Y-%m-%d") - timedelta(days=90)
        ).strftime("%Y-%m-%d")
        shared_data = engine._fetch_all_data(lookback_start, trading_date)

        report: dict[str, Any] = {
            "trading_date": trading_date,
            "fetched_sources": sorted(
                key for key in shared_data if not key.startswith("_")
            ),
            "horizons": {},
            "failures": [],
        }

        for horizon in sorted(HORIZON_PARAMS):
            horizon_report: dict[str, Any] = {}
            for strategy in engine.paper_trade_strategies:
                entry: dict[str, Any] = {
                    "candidates": 0,
                    "staged": 0,
                    "pending_llm": 0,
                    "errors": [],
                }
                try:
                    params = strategy.get_default_params(horizon=horizon)
                    candidates = strategy.screen(shared_data, trading_date, params)
                except Exception as exc:  # noqa: BLE001 - report every break
                    error = f"screen failed: {exc!r}"
                    entry["errors"].append(error)
                    report["failures"].append(
                        {
                            "horizon": horizon,
                            "strategy": strategy.name,
                            "ticker": None,
                            "error": error,
                        }
                    )
                    horizon_report[strategy.name] = entry
                    continue
                for candidate in candidates:
                    entry["candidates"] += 1
                    ticker = str(candidate.ticker or "").strip()
                    if not ticker:
                        # Mirror production: screen_and_enrich discards
                        # empty-ticker signals before staging (LLM
                        # enrichment resolves tickers first).
                        entry["pending_llm"] += 1
                        continue
                    try:
                        canonical_event_key(
                            strategy.name,
                            candidate.ticker,
                            candidate.metadata,
                            session,
                        )
                        canonical_observation_time(strategy.name, candidate.metadata)
                    except Exception as exc:  # noqa: BLE001 - report every break
                        error = f"staging rejected: {exc}"
                        entry["errors"].append(f"{candidate.ticker}: {error}")
                        report["failures"].append(
                            {
                                "horizon": horizon,
                                "strategy": strategy.name,
                                "ticker": candidate.ticker,
                                "error": error,
                            }
                        )
                    else:
                        entry["staged"] += 1
                horizon_report[strategy.name] = entry
            report["horizons"][horizon] = horizon_report

        report["ok"] = not report["failures"]
        return report
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
