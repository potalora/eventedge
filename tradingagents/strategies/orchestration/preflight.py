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


def run_preflight(
    config: dict[str, Any],
    trading_date: str,
    *,
    engine: Any | None = None,
    mode: str = "screen",
    price_source: Any | None = None,
    processed_at: datetime | None = None,
    state_inspector: Any | None = None,
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
    if mode in {"governed", "all"}:
        governed_report = _run_governed_preflight(
            config,
            session,
            price_source=price_source,
            processed_at=processed_at,
            state_inspector=state_inspector,
            governed_resolver=governed_resolver,
        )
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
    bounded: list[dict[str, Any]] = []
    fields = (
        "ticker",
        "session",
        "recovery_id",
        "contract_version",
        "evidence_digest",
    )
    for summary in summaries[:_MAX_RECOVERY_SUMMARIES]:
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
    caught_governed_error = False
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
        recoveries = _bounded_recovery_summaries(resolution.recovery_summaries)
        failure_map = dict(sorted(resolution.failure_map.items()))
        governed = set(snapshot.governed_tickers)
        if any(
            row["ticker"] not in governed
            or row["session"] != session.isoformat()
            or tuple(row["affected_cohort_ids"])
            != snapshot.cohort_ids_by_ticker[row["ticker"]]
            for row in recoveries
        ):
            raise ValueError("governed recovery summary scope is invalid")
        if any(
            ticker not in governed
            or not isinstance(reason, str)
            or not reason
            or len(reason) > _MAX_REPORT_TEXT
            for ticker, reason in failure_map.items()
        ):
            raise ValueError("governed failure map is invalid")
    except GovernedMarketDataError as error:
        caught_governed_error = True
        recoveries = []
        failure_map = dict(sorted(error.failure_map.items()))
    except PreflightStateError:
        raise
    except Exception:
        failure_map = {
            ticker: f"invalid {ticker}/{session.isoformat()}"
            for ticker in snapshot.governed_tickers
        }
        recoveries = []
    governed = set(snapshot.governed_tickers)
    if (caught_governed_error and not failure_map) or any(
        ticker not in governed
        or not isinstance(reason, str)
        or not reason
        or len(reason) > _MAX_REPORT_TEXT
        for ticker, reason in failure_map.items()
    ):
        failure_map = {
            ticker: f"invalid {ticker}/{session.isoformat()}"
            for ticker in snapshot.governed_tickers
        }
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
    state_inspector: Any | None,
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
        guard_preflight_state,
        inspect_and_guard_preflight_state,
    )
    from tradingagents.strategies.orchestration.trading_calendar import session_close

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
        if state_inspector is None:
            with inspect_and_guard_preflight_state(
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

        snapshot = state_inspector(
            state_dir=state_dir,
            cohort_ids=cohort_ids,
            session=session,
            benchmark_tickers=benchmarks,
        )
        if snapshot.state_status == "state_already_invalid" or now < session_close(
            session
        ):
            return _governed_snapshot_report(
                base,
                snapshot=snapshot,
                metric_store=None,
                session=session,
                now=now,
                price_source=price_source,
                resolve=resolve,
            )
        with guard_preflight_state(snapshot) as metric_store:
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
