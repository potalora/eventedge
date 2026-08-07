"""Pre-run pipeline integrity check.

Runs the daily cycle's data path -- shared multi-source fetch followed by
per-horizon strategy screens -- and validates every candidate through the
same event-identity gates that ``screen_and_stage`` applies
(``canonical_event_key`` + ``canonical_observation_time``). No generation
state is written, no LLM is called, and nothing is staged or executed, so
the check can run hours before the scheduled cycle.

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
from datetime import date, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


def run_preflight(
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
