"""Paper-trading-first multi-strategy engine.

Screens event-driven strategies for signals, synthesizes through a
portfolio committee, gates through risk controls, and executes via
PaperBroker or AlpacaBroker.
"""

from __future__ import annotations

import logging
import math
import os
import json
import re
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from collections.abc import Iterable, Mapping
from typing import Any, Callable

import pandas as pd

from tradingagents.strategies.data_sources.registry import (
    DataSourceRegistry,
    build_default_registry,
)
from tradingagents.strategies.state.state import StateManager
from tradingagents.strategies.modules import get_paper_trade_strategies
from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.metrics.identity import signal_id as metric_signal_id
from tradingagents.strategies.metrics.health import classify_strategy_run
from tradingagents.strategies.metrics.models import OutcomeRecord, StrategyHealthRecord
from tradingagents.strategies.metrics.outcomes import directional_accuracy
from tradingagents.strategies.state.portfolio_ledger import (
    LedgerConflictError,
    PortfolioLedger,
)

from tradingagents.strategies.runtime_deadline import bounded_model_phase

logger = logging.getLogger(__name__)

_FINNHUB_FETCH_SAFETY_MARGIN_S = 30.0
_DIAGNOSTIC_HOLDING_SESSIONS = 5


# Shared fetch uses OpenBB only for optional enrichment/expansion, not for
# required screen inputs. Keep this distinction consistent with preflight.
OPTIONAL_ENRICHMENT_SOURCES = frozenset({"openbb"})


def _enrichment_failures(enrichment: Mapping[str, Any]) -> list[dict]:
    """Retain optional failure identities without provider messages or URLs."""
    errors = enrichment.get("errors", {})
    if not isinstance(errors, Mapping):
        return []
    reasons = {"timeout", "transport_error", "http_error", "invalid_response",
               "provider_error", "batch_failure"}
    failures = []
    for operation in ("commodity_futures_curves", "factors", "profiles", "short_interest"):
        entries = errors.get(operation)
        if not isinstance(entries, Mapping):
            continue
        items = [(None, entries)] if operation == "factors" else entries.items()
        for symbol, details in items:
            safe_symbol = (symbol if isinstance(symbol, str) and
                           re.fullmatch(r"[A-Za-z0-9.^=_-]{1,32}", symbol) else "unknown")
            reason = details.get("reason_code") if isinstance(details, Mapping) else None
            failures.append({"operation": operation,
                             "symbol": None if operation == "factors" else safe_symbol,
                             "reason_code": reason if isinstance(reason, str) and reason in reasons else "provider_error"})
    return sorted(failures, key=lambda item: (item["operation"], item["symbol"] or "", item["reason_code"]))


_RETIRED_GEN003_SOURCES = frozenset({"edgar", "congress", "regulations", "courtlistener"})


def _provider_errors(
    data: Mapping[str, Any], sources: Iterable[str], *, include_optional: bool = False
) -> dict[str, str]:
    """Return required-source failures, optionally including enrichment diagnostics."""
    errors: dict[str, str] = {}
    for source in sources:
        if source in OPTIONAL_ENRICHMENT_SOURCES and not include_optional:
            continue
        if source not in data:
            if source in _RETIRED_GEN003_SOURCES:
                continue
            errors[str(source)] = "missing from shared data"
            continue
        payload = data[source]
        if not isinstance(payload, Mapping):
            errors[str(source)] = "invalid shared data payload"
            continue
        error = payload.get("error")
        if error not in (None, ""):
            errors[str(source)] = str(error)
    return errors


def _fetch_timeout_s() -> float:
    """Caller wait ceiling for the parallel API-key-source fetch fan-out
    (finnhub, fred, edgar, congress, regulations, etc.).

    A thread already running when this expires cannot be cancelled safely.
    Cooperative source deadlines therefore prevent follow-on calls in normal
    timeout failure modes. NOTE: the yfinance price fetch runs synchronously
    outside this fan-out and is bounded separately by ``yf.download(timeout=30)``;
    OpenBB enrichment is not bounded here. Overridable via
    AUTORESEARCH_FETCH_TIMEOUT_S.
    """
    try:
        return float(os.environ.get("AUTORESEARCH_FETCH_TIMEOUT_S", "300"))
    except (TypeError, ValueError):
        return 300.0


def _positions_to_price(
    deduped_signals: list[dict],
    open_trades: list[dict],
    price_cache: dict | None,
) -> list[str]:
    """Tickers needing a current price for the daily snapshot.

    Every current signal PLUS every open position. Including open positions is
    what marks held longs and shorts to market even after they stop being
    signaled — without it, a position that drops out of the signal set freezes
    at its entry price in the equity snapshot (the 2026-06 ADMA short whose
    ``short_liability`` never moved). Already-cached tickers are excluded.
    """
    wanted = {s.get("ticker") for s in deduped_signals if s.get("ticker")}
    wanted |= {t.get("ticker") for t in open_trades if t.get("ticker")}
    return sorted(wanted - set(price_cache or {}))


_MUTABLE_EVIDENCE_KEYS = {
    "llm_analysis",
    "document_assessment",
    "filing_assessment",
    "model_provenance",
    "llm_conviction",
    "needs_llm_analysis",
}
_EVENT_DATETIME_KEYS = (
    "event_at",
    "published_at",
    "observed_at",
)
_EVENT_DATE_ONLY_KEYS = (
    "posted_date",
    "date_filed",
    "release_date",
    "file_date",
    "filing_date",
    "observation_date",
    "window_end",
)


def _canonical_signal_evidence(value: object) -> object:
    """Normalize heterogeneous metadata without mutable LLM annotations."""
    if isinstance(value, dict):
        return {
            str(key): _canonical_signal_evidence(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _MUTABLE_EVIDENCE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_signal_evidence(item) for item in value]
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("signal evidence contains a non-finite float")
        return format(Decimal(str(value)), "f")
    if value is None or isinstance(value, (str, int, bool, Decimal)):
        return value
    return str(value)


def _metadata_timestamp(
    metadata: dict, key: str, *, allow_date_only: bool
) -> datetime | None:
    """Parse supplied provenance strictly; date-only evidence resolves to day end."""
    if key not in metadata:
        return None
    value = metadata[key]
    if value in (None, ""):
        raise ValueError(f"invalid candidate timestamp {key}")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        if not allow_date_only:
            raise ValueError(f"candidate timestamp {key} requires an aware datetime")
        return datetime.combine(value, datetime.max.time(), tzinfo=timezone.utc)
    elif isinstance(value, str):
        if allow_date_only:
            try:
                date_value = date.fromisoformat(value)
            except ValueError:
                date_value = None
            if date_value is not None and value == date_value.isoformat():
                return datetime.combine(
                    date_value, datetime.max.time(), tzinfo=timezone.utc
                )
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"invalid candidate timestamp {key}") from None
    else:
        raise ValueError(f"invalid candidate timestamp {key}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"candidate timestamp {key} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _gather_with_timeout(
    api_fetches: dict[str, tuple],
    timeout_s: float,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Run each ``name -> (fn, args)`` fetch in a thread pool, returning
    ``{name: result}``.

    Each source is classified from one completed/pending wait partition;
    failures and timeouts retain an explicit error payload rather than being
    mistaken for healthy empty results. A pending source remains timed out even
    if it finishes while the result is assembled. Python
    cannot safely stop a running thread, so this is not request cancellation.
    On timeout the pool is shut down with ``wait=False`` so its teardown does
    not re-block the caller; sources need cooperative scheduling deadlines to
    avoid issuing follow-on requests after the caller has moved on.
    """
    from concurrent.futures import ThreadPoolExecutor, wait

    results: dict[str, Any] = {name: {} for name in api_fetches}
    if not api_fetches:
        return results

    pool = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {
            pool.submit(fn, *args): name for name, (fn, args) in api_fetches.items()
        }
        done, pending = wait(futures, timeout=timeout_s)
        for future, name in futures.items():
            if future not in done:
                results[name] = {"error": f"timeout after {timeout_s:g}s"}
                continue
            try:
                results[name] = future.result()
            except Exception as error:
                logger.error("Failed to fetch %s", name, exc_info=True)
                results[name] = {"error": f"{type(error).__name__}: {error}"}
        if pending:
            stuck = sorted(futures[future] for future in pending)
            logger.error(
                "Data fetch exceeded %.0fs; abandoning slow sources: %s",
                timeout_s,
                stuck,
            )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    return results


def model_sample_incomplete(signals, health=()):
    """Coverage survives removal of unresolved/blocked candidates from signals."""
    return any((s.get("metadata") or {}).get("analysis_failure_reason") == "model_deadline_exhausted" for s in signals) or any(
        isinstance(record.evidence.get("model_coverage"), dict)
        and record.evidence["model_coverage"].get("complete") is False for record in health)


def hold_incomplete_model_sample(signals, health, *, incomplete=False):
    """Keep admitted evidence but prohibit selection of a timeout-biased sample."""
    if not incomplete and not model_sample_incomplete(signals, health):
        return signals, health
    for signal in signals:
        signal["journal_only"] = True
        signal.setdefault("metadata", {}).update(analysis_status="failed",
            analysis_failure_reason="model_deadline_exhausted", non_actionable_reason="model_sample_incomplete")
    from .research_resilience import hold_signals
    hold_signals(signals, 'model_sample_incomplete')
    for record in health:
        for event in record.evidence.get('state_economic_events', []):
            event.update(journal_only=True, analysis_status='failed',
                analysis_failure_reason='model_deadline_exhausted', non_actionable_reason='model_sample_incomplete')
    health = [replace(record, status="data_failure", evidence={**record.evidence,
        "provider_errors": {**record.evidence.get("provider_errors", {}), "analysis": "model_deadline_exhausted"},
        "model_coverage": {"complete": False, "reason": "model_deadline_exhausted"},
        "actionable_candidate_count": 0}) if record.status != "disabled_by_policy" else record for record in health]
    return signals, health


class MultiStrategyEngine:
    """Paper-trading-first strategy engine.

    Screens event-driven strategies for signals, synthesizes through
    a portfolio committee, gates through risk controls, and executes
    via PaperBroker or AlpacaBroker. Weights evolve through a
    conservative learning loop based on realized trade outcomes.
    """

    def __init__(
        self,
        config: dict | None = None,
        strategies: list | None = None,
        registry: DataSourceRegistry | None = None,
        state_manager: StateManager | None = None,
        on_event: Callable | None = None,
        use_llm: bool = False,
        ledger: PortfolioLedger | None = None,
        data_acquisition: Any | None = None,
    ):
        self.config = config or {}
        self.ar_config = self.config.get("autoresearch", {})
        from .data_acquisition import DataAcquisitionService
        from .research_resilience import configured as research_resilience_configured
        research_resilience_configured(self.ar_config)
        from .research_resilience import capability_configured
        capability_configured(self.ar_config)

        # Load strategies (active paper-trade strategies only)
        self.paper_trade_strategies = list(strategies or get_paper_trade_strategies())

        from .operating_contract import configured as profile_configured, configure_strategies
        if profile_configured(self.config) or self.ar_config.get("enabled_strategies") or self.ar_config.get("disabled_strategies"):
            self.paper_trade_strategies = configure_strategies(
                self.paper_trade_strategies, self.ar_config, full_config=self.config
            )

        # Shared single-instance Tier-2 DataAcquisitionService
        if data_acquisition is not None:
            self.data_acquisition = data_acquisition
            self._registry = registry or data_acquisition.registry
        else:
            self._registry = registry or build_default_registry(self.ar_config)
            self.data_acquisition = DataAcquisitionService(self.config, registry=self._registry)

        # State
        self.state = state_manager or StateManager(
            self.ar_config.get("state_dir", "data/state")
        )

        # Event callback
        self._on_event = on_event or (lambda kind, **kw: None)

        # LLM analyzer for paper-trade signal enrichment
        self._analyzer = None
        if use_llm:
            from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer

            self._analyzer = LLMAnalyzer(self.config)

        # Shared price cache owned by DataAcquisitionService
        self._price_cache: dict[str, pd.DataFrame] = self.data_acquisition.price_cache
        self.ledger = ledger

        # Signal journal (shared across methods)
        from tradingagents.strategies.learning.signal_journal import SignalJournal

        self._journal = SignalJournal(
            self.ar_config.get("state_dir", "data/state"), ledger=self.ledger
        )

        # Optional enrichment availability flag — checked once at startup
        self._openbb_source = self.registry.get("enrichment") or self.registry.get("openbb")
        self._openbb_available = (
            self._openbb_source.is_available() if self._openbb_source else False
        )

    def _emit(self, kind: str, **data: Any) -> None:
        self._on_event(kind, **data)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_signals(all_signals: list[dict]) -> list[dict]:
        """Aggregate signals per (strategy, ticker) while preserving disagreement.

        Preserves ``long_count``, ``short_count``, ``net_conviction``,
        ``disagreement``, and top ``bull_evidence`` / ``bear_evidence`` instead
        of collapsing to winner-take-all ``max(score)``.
        """
        grouped: dict[tuple[str, str], list[dict]] = {}
        for signal in all_signals:
            ticker = str(signal.get("ticker", "")).strip()
            if not ticker:
                continue
            key = (str(signal["strategy"]), ticker)
            grouped.setdefault(key, []).append(signal)

        def _evidence_snippet(sig: dict) -> str:
            meta = sig.get("metadata") if isinstance(sig.get("metadata"), Mapping) else {}
            llm = meta.get("llm_analysis") if isinstance(meta.get("llm_analysis"), Mapping) else {}
            for candidate in (
                llm.get("thesis"),
                llm.get("rationale"),
                llm.get("summary"),
                meta.get("thesis"),
                meta.get("catalyst"),
                meta.get("headline"),
                meta.get("summary"),
            ):
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()[:240]
            return f"{sig.get('strategy')}:{sig.get('direction')}@{float(sig.get('score', 0.0)):.2f}"

        resolved: list[dict] = []
        for (_strategy, _ticker), items in grouped.items():
            longs = [s for s in items if s.get("direction") == "long"]
            shorts = [s for s in items if s.get("direction") == "short"]
            neutrals = [s for s in items if s.get("direction") not in {"long", "short"}]
            long_count = len(longs)
            short_count = len(shorts)

            long_sum = sum(float(s.get("score", 0.0) or 0.0) for s in longs)
            short_sum = sum(float(s.get("score", 0.0) or 0.0) for s in shorts)
            directional_count = long_count + short_count
            net_conviction = (
                round((long_sum - short_sum) / directional_count, 6)
                if directional_count > 0
                else 0.0
            )

            if directional_count == 0:
                direction = "neutral"
                pool = neutrals
            elif net_conviction >= 0:
                direction = "long"
                pool = longs or items
            else:
                direction = "short"
                pool = shorts or items

            lead = max(pool, key=lambda s: float(s.get("score", 0.0) or 0.0))
            bull_evidence = [
                _evidence_snippet(s)
                for s in sorted(longs, key=lambda s: float(s.get("score", 0.0) or 0.0), reverse=True)[:3]
            ]
            bear_evidence = [
                _evidence_snippet(s)
                for s in sorted(shorts, key=lambda s: float(s.get("score", 0.0) or 0.0), reverse=True)[:3]
            ]
            merged = dict(lead)
            merged["direction"] = direction
            merged["score"] = round(abs(net_conviction), 6) if directional_count > 0 else float(lead.get("score", 0.0) or 0.0)
            merged["long_count"] = long_count
            merged["short_count"] = short_count
            merged["net_conviction"] = net_conviction
            merged["disagreement"] = long_count > 0 and short_count > 0
            merged["bull_evidence"] = bull_evidence
            merged["bear_evidence"] = bear_evidence
            merged.setdefault("raw_signal_score", lead.get("raw_signal_score", lead.get("score")))
            if "llm_conviction" in lead:
                merged["llm_conviction"] = lead["llm_conviction"]
            resolved.append(merged)
        return resolved

    @bounded_model_phase
    def screen_and_enrich(
        self,
        trading_date: str,
        data: dict,
        horizon: str = "30d",
        *,
        epoch_id: str,
        policy_id: str,
    ) -> tuple[list[dict], dict, list[StrategyHealthRecord]]:
        """Run strategy screening and LLM enrichment with Tier-2 circuit-breaker isolation.

        A failure in any Tier-2 strategy source marks *only* the affected
        strategy ``unavailable`` for the session and never aborts sibling
        strategies or invalidates clean Tier-1 accounting.
        """
        from tradingagents.strategies.metrics.identity import _stable_id
        from tradingagents.strategies.modules.admission import (
            admit_candidates,
            candidate_universe,
        )
        from .data_acquisition import evaluate_strategy_circuit_breaker
        from .research_resilience import (
            configured as resilience_configured,
            digest as research_digest,
            project_sources,
            strategy_capability_holds,
        )
        from .source_inputs import source_codec_limits

        resilience = resilience_configured(self.ar_config)
        research_scope = None
        research_hold_reasons: list[str] = []
        signal_data = dict(data)
        codec_limits = source_codec_limits(self.config)

        if resilience:
            names = set(data) | {"fred", "yfinance"}
            if self.ar_config.get("equity_universe_policy"):
                names.add("equity_universe")
            names.update(
                source for strategy in self.paper_trade_strategies for source in strategy.data_sources
            )
            projected_sources, research_scope = project_sources(
                data,
                names,
                codec_limits=codec_limits,
                capability_policy=self.ar_config.get("source_capability_policy"),
            )
            signal_data = {
                key: projected_sources[key] for key in signal_data if key in projected_sources
            }

        regime_model = self._build_regime_model(signal_data)
        if resilience:
            if any(
                regime_model[key] is None
                for key in ("vix_level", "credit_spread_bps", "yield_curve_slope")
            ):
                research_hold_reasons.append("shared_regime_unavailable")
            regime_model["research_source_scope"] = research_scope
            regime_model["research_hold_reasons"] = research_hold_reasons
            regime_model["research_projection_sha256"] = research_digest(
                signal_data, codec_limits=codec_limits
            )
        regime_model.setdefault("timestamp", datetime.now().isoformat())

        universe = None
        universe_failure = False
        if self.ar_config.get("equity_universe_policy"):
            from tradingagents.strategies.data_sources.equity_universe import (
                POLICY as UNIVERSE_POLICY,
                EquityUniverse,
            )

            if self.ar_config["equity_universe_policy"] != UNIVERSE_POLICY:
                raise ValueError("unsupported equity universe policy")
            selected = signal_data if resilience else data
            universe_data = selected.get("equity_universe", {})
            if isinstance(universe_data, Mapping) and not universe_data.get("error") and universe_data.get("snapshot") is not None:
                try:
                    universe = EquityUniverse(universe_data.get("snapshot"))
                except (ValueError, TypeError, AttributeError):
                    universe_failure = True
            else:
                universe_failure = True
        if resilience and universe_failure:
            research_hold_reasons.append("required_universe_unavailable")

        # Strict Tier-2 per-strategy circuit breaker
        circuit_breaker = evaluate_strategy_circuit_breaker(data, self.paper_trade_strategies)
        regime_model["strategy_circuit_breaker"] = circuit_breaker

        analysis_inputs: dict[int, str] = {}
        all_signals: list[dict] = []
        health: list[StrategyHealthRecord] = []
        session_date = date.fromisoformat(trading_date)

        for strategy in self.paper_trade_strategies:
            self._emit("strategy_start", name=strategy.name, track="paper_trade")
            disabled_reason = self.ar_config.get("disabled_strategies", {}).get(
                strategy.name
            ) or getattr(strategy, "retirement_reason", None)
            if disabled_reason:
                health.append(
                    StrategyHealthRecord(
                        health_id=_stable_id(
                            "health", epoch_id, session_date, policy_id, strategy.name
                        ),
                        epoch_id=epoch_id,
                        session=session_date,
                        policy_id=policy_id,
                        strategy=strategy.name,
                        status="disabled_by_policy",
                        signal_count=0,
                        evidence={
                            "reason": disabled_reason,
                            "data_sources": sorted(strategy.data_sources),
                            "candidate_count": 0,
                        },
                    )
                )
                continue

            breaker_state = circuit_breaker.get(strategy.name, {"available": True, "failed_sources": []})
            if not breaker_state["available"]:
                failed_sources = list(breaker_state.get("failed_sources", []))
                provider_errors = _provider_errors(data, strategy.data_sources)
                logger.warning(
                    "Tier-2 circuit breaker: strategy %s marked unavailable due to failed sources %s "
                    "(sibling strategies unaffected)",
                    strategy.name,
                    failed_sources,
                )
                health.append(
                    StrategyHealthRecord(
                        health_id=_stable_id(
                            "health", epoch_id, session_date, policy_id, strategy.name
                        ),
                        epoch_id=epoch_id,
                        session=session_date,
                        policy_id=policy_id,
                        strategy=strategy.name,
                        status="unavailable",
                        signal_count=0,
                        evidence={
                            "tier": "tier2_strategy",
                            "failed_sources": failed_sources,
                            "provider_errors": provider_errors,
                            "data_sources": sorted(strategy.data_sources),
                            "candidate_count": 0,
                        },
                    )
                )
                self._emit("strategy_done", name=strategy.name, num_signals=0)
                continue

            try:
                params = strategy.get_default_params(horizon=horizon)
                capability_holds = (
                    strategy_capability_holds(research_scope, strategy.name, strategy.data_sources)
                    if resilience
                    else []
                )
                with candidate_universe(universe):
                    candidates = (
                        admit_candidates(strategy.name, [], budget=None)
                        if capability_holds
                        else strategy.screen(signal_data, trading_date, params)
                    )
                    if not hasattr(candidates, "admission_manifest"):
                        candidates = admit_candidates(strategy.name, candidates, budget=None)
                admission_manifest = candidates.admission_manifest
                error = None
            except Exception as exc:
                candidates = []
                admission_manifest = None
                error = exc
                logger.exception("Strategy %s screen failed", strategy.name)

            if resilience:
                for candidate in candidates:
                    analysis_inputs[id(candidate)] = research_digest(
                        {
                            "candidate": asdict(candidate),
                            "strategy": strategy.name,
                            "regime": regime_model,
                            "projection_sha256": regime_model["research_projection_sha256"],
                        },
                        codec_limits=codec_limits,
                    )
            if research_hold_reasons:
                for candidate in candidates:
                    candidate.journal_only = True
                    candidate.metadata["non_actionable_reason"] = research_hold_reasons[0]
            if candidates and not research_hold_reasons:
                candidates = self._enrich_with_llm(
                    candidates, strategy.name, regime_context=regime_model
                )

            universe_assessments = []
            if universe is not None:
                for candidate in candidates:
                    decision = universe.decision(candidate.ticker)
                    assessment = {
                        "discovery_id": candidate.metadata.get("discovery_id"),
                        "symbol": candidate.ticker,
                        "decision": decision,
                        "policy": universe.evidence["policy"],
                        "assets_sha256": universe.evidence["assets_sha256"],
                    }
                    universe_assessments.append(assessment)
                    candidate.metadata["equity_universe_post_analysis"] = assessment
                    if decision in {
                        "outside_sip_exchange_universe",
                        "inactive_asset",
                        "absent_from_asset_master",
                    }:
                        candidate.metadata["equity_universe_excluded"] = True
                    elif decision != "eligible":
                        candidate.metadata.setdefault(
                            "non_actionable_reason", "equity_universe_unresolved"
                        )
                        candidate.journal_only = True

            provider_errors = _provider_errors(data, strategy.data_sources)
            if universe_failure:
                provider_errors = dict(
                    provider_errors, equity_universe="universe_evidence_unavailable"
                )
            health_record = classify_strategy_run(
                epoch_id=epoch_id,
                session=session_date,
                policy_id=policy_id,
                strategy=strategy.name,
                data_sources=tuple(strategy.data_sources),
                candidates=candidates,
                provider_errors=provider_errors,
                exception=error,
                admission_manifest=admission_manifest,
            )
            if universe is not None:
                health_record.evidence["universe_assessments"] = universe_assessments
                health_record.evidence["universe_policy"] = universe.evidence["policy"]
            source_coverage = {
                source: data[source]["coverage"]
                for source in strategy.data_sources
                if isinstance(data.get(source), Mapping) and "coverage" in data[source]
            }
            if source_coverage:
                health_record.evidence["source_coverage"] = source_coverage
            if resilience:
                health_record.evidence["research_source_scope"] = research_scope
                health_record.evidence["research_hold_reasons"] = list(research_hold_reasons)
            health.append(health_record)

            for c in candidates:
                if c.metadata.get("equity_universe_excluded"):
                    continue
                sig_dict: dict[str, Any] = {
                    "ticker": c.ticker,
                    "direction": c.direction,
                    "score": c.score,
                    "raw_signal_score": getattr(c, "raw_signal_score", c.score),
                    "llm_conviction": getattr(c, "llm_conviction", None),
                    "strategy": strategy.name,
                    "metadata": c.metadata,
                    "event_key": c.event_key,
                    "source_event_keys": c.source_event_keys,
                    "strategy_tags": c.strategy_tags,
                    "risk_tags": c.risk_tags,
                    "journal_only": c.journal_only,
                }
                all_signals.append(sig_dict)
            if resilience:
                from .research_resilience import bind_signal

                for candidate in candidates:
                    if candidate.metadata.get("equity_universe_excluded"):
                        continue
                    signal = next(
                        row for row in reversed(all_signals) if row["metadata"] is candidate.metadata
                    )
                    bind_signal(
                        signal,
                        regime_model,
                        strategy.data_sources,
                        analysis_inputs[id(candidate)],
                        codec_limits=codec_limits,
                    )
            self._emit("strategy_done", name=strategy.name, num_signals=len(candidates))

        all_signals, health = hold_incomplete_model_sample(all_signals, health)
        deduped_signals = [
            signal for signal in all_signals if signal.get("ticker", "").strip()
        ]

        # Filter blocked tickers
        blocked = set(t.upper() for t in self.ar_config.get("blocked_tickers", []))
        if blocked:
            before = len(deduped_signals)
            deduped_signals = [
                signal
                for signal in deduped_signals
                if signal["ticker"].upper() not in blocked
            ]
            removed = before - len(deduped_signals)
            if removed:
                logger.info("Blocked %d signals for tickers: %s", removed, blocked)

        return deduped_signals, regime_model, health

    def pending_late_signals(self, session: date, epoch_id: str, *, information_cutoff: datetime | None = None) -> list[dict]:
        """Return frozen actionable observations awaiting their first timely offer.

        Callers include these candidates before pricing and quarantine filtering.
        Today's rows cannot consume the queue: replay must reconstruct the same
        initial candidate set, even after staging today's timely observation.
        """
        from tradingagents.strategies.orchestration.trading_calendar import session_close
        from tradingagents.strategies.orchestration.decision_clock import policy, PROSPECTIVE, require_reference

        if policy(self.config) == PROSPECTIVE:
            if information_cutoff is None:
                raise ValueError('prospective retained-signal cutoff required')
            require_reference(session, information_cutoff)
        else:
            information_cutoff = session_close(session)

        if self.ledger is None:
            raise ValueError("pending_late_signals requires an authoritative ledger")
        horizon = str(self.ar_config.get("horizon", "30d"))
        policy_id = str(self.ar_config.get("paper_ledger", {}).get("policy_id", f"foundation-{horizon}"))
        disabled = set(self.ar_config.get("disabled_strategies", {}))
        disabled.update(strategy.name for strategy in self.paper_trade_strategies
                        if getattr(strategy, "retirement_reason", None))
        consumed = set()
        pending = {}
        for record in self.ledger.read_signals(end_session=session, epoch_id=epoch_id, policy_id=policy_id):
            if record.reference_session >= session:
                continue
            identity = (record.event_key, record.strategy)
            observation = self.ledger.signal_observation(record.signal_id)
            if observation is None:
                # Lower-level accounting/outcome callers may record a bare
                # signal. It cannot prove a deferred actionable thesis.
                consumed.add(identity)
                continue
            _, context, journal = observation
            if journal.get("status", "timely") != "cutoff-late":
                consumed.add(identity)
                continue
            signal = context.get("signal")
            if not isinstance(signal, dict):
                raise ValueError(f"signal {record.signal_id} lacks canonical committee context")
            metadata = signal.get("metadata", {})
            if (record.strategy in disabled or signal.get("journal_only")
                    or record.direction not in {"long", "short"}
                    or not isinstance(metadata, dict) or metadata.get("non_actionable_reason")
                    or (metadata.get("needs_llm_analysis")
                        and metadata.get("analysis_status") != "validated"
                        and not metadata.get("deterministic_evidence_complete"))
                    or record.observed_at > information_cutoff):
                continue
            # read_signals is session ordered; retain the first accepted thesis,
            # rather than substituting a newly acquired source or model analysis.
            if identity not in pending:
                retained = dict(signal)
                retained["metadata"] = {**metadata, "retained_from_signal_id": record.signal_id}
                pending[identity] = retained
        return [pending[key] for key in sorted(pending) if key not in consumed]

    @bounded_model_phase
    def screen_and_stage(
        self,
        trading_date: str,
        data: dict,
        shared_signals: list[dict],
        shared_regime: dict,
        enrichment: dict,
        size_profile: Any,
        marked_account: Any,
        annualized_volatility_evidence: Mapping[str, float] | None = None,
        model_coverage: Mapping[str, Any] | None = None,
        *,
        horizon_recommendations: list[Any] | None = None,
        horizon_decision_status: Mapping[str, Any] | None = None,
    ) -> dict:
        """Persist cutoff-safe signals and next-session intents without economics."""
        from tradingagents.strategies.execution import (
            AccountSnapshot,
            SignalRecord,
            stable_id,
        )
        from tradingagents.strategies.learning.signal_journal import JournalEntry
        from tradingagents.strategies.orchestration.trading_calendar import (
            is_session,
            next_session,
            session_close,
        )
        from tradingagents.strategies.orchestration.session_executor import PHASES
        from tradingagents.strategies.trading.execution_bridge import (
            ExecutionBridge,
            ZeroShareIntentError,
        )
        from tradingagents.strategies.trading.portfolio_committee import (
            PortfolioCommittee,
        )
        from tradingagents.strategies.trading.portfolio_policy import (
            PortfolioPolicyConfig,
            build_portfolio_risk_context,
            portfolio_policy_config_document,
            portfolio_risk_context_document,
            portfolio_risk_context_from_document,
        )

        if self.ledger is None:
            raise ValueError("screen_and_stage requires an authoritative ledger")
        session = date.fromisoformat(trading_date)
        if not is_session(session):
            raise ValueError(f"{session} is not an XNYS session")
        if not isinstance(marked_account, AccountSnapshot):
            raise TypeError("marked_account must be AccountSnapshot")
        if (
            not marked_account.valid
            or marked_account.session != session
            or marked_account.cohort_id != self.ledger.cohort_id
        ):
            raise ValueError("marked_account is not the valid current cohort snapshot")
        account_state = self.ledger.account_state()
        if (
            account_state.cash != marked_account.cash
            or account_state.net_equity != marked_account.net_equity
            or account_state.buying_power != marked_account.buying_power
            or account_state.high_water_mark != marked_account.high_water_mark
        ):
            raise ValueError("marked_account does not match authoritative ledger")

        horizon = str(self.ar_config.get("horizon", "30d"))
        policy_id = str(
            self.ar_config.get("paper_ledger", {}).get(
                "policy_id", f"foundation-{horizon}"
            )
        )
        epoch_id = marked_account.epoch_id
        from .research_resilience import configured as resilience_configured
        resilience = resilience_configured(self.ar_config)
        exit_data = data
        original_enrichment = enrichment
        enrichment_scope = None
        if resilience:
            from .research_resilience import validate_projection, validate_signal, validate_retained_signal, project_enrichment
            from .source_inputs import source_codec_limits
            exit_data = validate_projection(self, data, shared_regime)
            enrichment, enrichment_scope = project_enrichment(enrichment, codec_limits=source_codec_limits(self.config),
                capability_config=self.ar_config)
            strategy_sources = {strategy.name: strategy.data_sources for strategy in self.paper_trade_strategies}
            for signal in shared_signals:
                if signal.get('journal_only') or signal.get('metadata', {}).get('non_actionable_reason'):
                    continue
                if shared_regime['research_hold_reasons']:
                    raise ValueError('invalid_research_candidate_during_hold')
                if signal.get('metadata', {}).get('retained_from_signal_id'):
                    validate_retained_signal(signal, self.ledger, session, epoch_id, policy_id)
                else:
                    validate_signal(signal, shared_regime, strategy_sources.get(signal.get('strategy'), ()),
                                    codec_limits=source_codec_limits(self.config))
        from tradingagents.strategies.orchestration.decision_clock import resolve_cutoff, policy, PROSPECTIVE, require_live_staging
        decision_context = data.get('_decision_context')
        cutoff = resolve_cutoff(self.config, session, decision_context)
        eligible_session = next_session(session)
        expected_staging_state_digest = self.ledger.verify_session_phase_chain(
            session, PHASES
        )
        replaying = self.ledger.staging_completed(session, epoch_id, policy_id)
        if decision_context is not None and not replaying:
            require_live_staging(self.config, session, decision_context)

        raw_bars = data.get("_execution_reference_bars", {})
        if not isinstance(raw_bars, dict):
            raise ValueError("_execution_reference_bars must be a mapping")

        policy_settings = self.ar_config.get("portfolio_policy")
        policy_enabled = size_profile is not None and isinstance(
            policy_settings, dict
        )
        effective_policy_settings = policy_settings
        if (
            policy_enabled
            and "congressional_exposure_pct"
            in getattr(PortfolioPolicyConfig, "__dataclass_fields__", {})
            and "congressional_exposure_by_size" not in policy_settings
            and hasattr(size_profile, "name")
        ):
            effective_policy_settings = {
                **policy_settings,
                "congressional_exposure_by_size": {size_profile.name: 0.0},
            }
        policy_config = (
            PortfolioPolicyConfig.from_size_profile(
                size_profile, effective_policy_settings
            )
            if policy_enabled
            else None
        )
        if policy(self.config) == PROSPECTIVE and policy_config is None:
            raise ValueError('prospective decision clock requires governed staging policy')
        if replaying and policy_config is None:
            persisted_binding = self.ledger.read_policy_session_context(
                session, binding_kind="staging"
            )
            persisted_manifests = self.ledger.read_policy_staging_audit_manifests(
                epoch_id=epoch_id
            )
            if persisted_binding is not None or any(
                manifest["session"] == session
                and manifest["policy_id"] == policy_id
                for manifest in persisted_manifests
            ):
                raise LedgerConflictError(
                    "policy artifacts exist on replay while portfolio policy is disabled"
                )
        risk_context = None
        policy_binding = None
        if policy_config is not None:
            baseline_current = self.ledger.policy_open_lot_projection(session)
            baseline_pending = tuple(
                row
                for row in self.ledger.policy_pending_entry_projection()
                if not (replaying and row["eligible_session"] == eligible_session)
            )
            if replaying:
                policy_binding = self.ledger.read_policy_session_context(
                    session, binding_kind="staging"
                )
                if (
                    policy_binding is None
                    or policy_binding["epoch_id"] != epoch_id
                    or policy_binding["policy_version"] != policy_config.version
                    or policy_binding["policy_config"]
                    != portfolio_policy_config_document(policy_config)
                ):
                    raise LedgerConflictError(
                        "staging policy binding mismatch on replay"
                    )
                if policy_binding['context'].get('decision_clock') != decision_context:
                    raise LedgerConflictError('staging decision clock binding mismatch on replay')
                risk_context = portfolio_risk_context_from_document(
                    policy_binding["context"], policy_config
                )
                authoritative = build_portfolio_risk_context(
                    portfolio_value=float(marked_account.net_equity),
                    cash=float(marked_account.cash),
                    current_positions=baseline_current,
                    pending_positions=baseline_pending,
                    annualized_volatility_evidence=(risk_context.annualized_volatility),
                    earnings_dates=risk_context.earnings_dates,
                    short_interest=risk_context.short_interest,
                    borrow_available=risk_context.borrow_available,
                    margin_used=float(marked_account.margin_used),
                    consumed_event_keys=self.ledger.consumed_event_keys(),
                    config=policy_config,
                    sectors=risk_context.sectors,
                    require_borrow=risk_context.require_borrow,
                )

                def economic_positions(items):
                    return tuple(
                        (
                            item.ticker,
                            item.direction,
                            item.weight,
                            item.sector,
                            item.strategy_tags,
                            item.risk_tags,
                            item.annualized_volatility,
                        )
                        for item in items
                    )

                if (
                    authoritative.portfolio_value != risk_context.portfolio_value
                    or authoritative.cash != risk_context.cash
                    or authoritative.margin_used != risk_context.margin_used
                    or authoritative.consumed_event_keys
                    != risk_context.consumed_event_keys
                    or economic_positions(authoritative.positions)
                    != economic_positions(risk_context.positions)
                    or economic_positions(authoritative.pending_positions)
                    != economic_positions(risk_context.pending_positions)
                    or authoritative.annualized_volatility
                    != risk_context.annualized_volatility
                ):
                    raise LedgerConflictError(
                        "authoritative staging context changed on replay"
                    )
            else:
                profiles = (enrichment or {}).get("profiles", {})
                sectors = {
                    str(signal.get("ticker", "")).strip().upper(): str(
                        profiles.get(
                            str(signal.get("ticker", "")).strip().upper(), {}
                        ).get("sector", "Unknown")
                    )
                    for signal in shared_signals
                    if signal.get("ticker")
                }
                if annualized_volatility_evidence is None:
                    raise ValueError(
                        "fresh policy staging requires explicit annualized "
                        "volatility evidence"
                    )
                staging_volatility_evidence = annualized_volatility_evidence
                risk_context = build_portfolio_risk_context(
                    portfolio_value=float(marked_account.net_equity),
                    cash=float(marked_account.cash),
                    current_positions=baseline_current,
                    pending_positions=baseline_pending,
                    price_cache=None,
                    annualized_volatility_evidence=staging_volatility_evidence,
                    earnings_dates={},
                    short_interest={},
                    borrow_available={},
                    margin_used=float(marked_account.margin_used),
                    consumed_event_keys=self.ledger.consumed_event_keys(),
                    config=policy_config,
                    sectors=sectors,
                    require_borrow=False,
                )
                candidate_tickers = {
                    str(signal.get("ticker", "")).strip().upper()
                    for signal in shared_signals
                    if str(signal.get("ticker", "")).strip()
                }
                missing_candidates = sorted(
                    candidate_tickers - set(risk_context.annualized_volatility)
                )
                if missing_candidates:
                    raise ValueError(
                        "missing annualized volatility evidence for governed "
                        "ticker(s): " + ", ".join(missing_candidates)
                    )
                policy_binding = self.ledger.bind_policy_session_context(
                    session,
                    binding_kind="staging",
                    epoch_id=epoch_id,
                    policy_version=policy_config.version,
                    policy_config=portfolio_policy_config_document(policy_config),
                    context={**portfolio_risk_context_document(risk_context),
                             **({'decision_clock': decision_context} if decision_context is not None else {})},
                    bound_at=cutoff,
                )
        if replaying:
            if resilience:
                from .research_resilience import validate_book_source_scope
                accepted_decision = self.ledger.committee_decision(session, epoch_id, policy_id)
                status = accepted_decision.get('status', {}) if isinstance(accepted_decision, dict) else {}
                validate_book_source_scope(status, shared_regime, codec_limits=source_codec_limits(self.config))
                if status.get('research_enrichment_scope') != enrichment_scope:
                    raise ValueError('research_enrichment_scope_conflict')
            records = self.ledger.read_signals(
                session, session, epoch_id=epoch_id, policy_id=policy_id
            )
            if policy_config is not None:
                manifests = self.ledger.read_policy_staging_audit_manifests(
                    epoch_id=epoch_id
                )
                matching = [
                    item
                    for item in manifests
                    if item["session"] == session and item["policy_id"] == policy_id
                ]
                if len(matching) != 1:
                    raise LedgerConflictError(
                        "missing policy staging audit manifest on replay"
                    )
                for record in records:
                    if self.ledger.read_signal_policy_provenance(record.signal_id) is None:
                        raise LedgerConflictError(
                            f"missing signal policy provenance {record.signal_id}"
                        )
                intent_rows = self.ledger.connection.execute(
                    """SELECT DISTINCT i.intent_id FROM order_intents i
                       JOIN intent_signals x ON x.intent_id = i.intent_id
                       JOIN signals s ON s.signal_id = x.signal_id
                       WHERE i.cohort_id = ? AND s.reference_session = ?
                         AND s.epoch_id = ? AND s.policy_id = ?""",
                    (self.ledger.cohort_id, session.isoformat(), epoch_id, policy_id),
                ).fetchall()
                for row in intent_rows:
                    intent = self.ledger.intent(str(row["intent_id"]))
                    if intent is not None and intent.side in {"buy", "short"}:
                        if self.ledger.read_intent_policy_provenance(
                            intent.intent_id
                        ) is None:
                            raise LedgerConflictError(
                                f"missing intent policy provenance {intent.intent_id}"
                            )
            return {
                "signals": [record.__dict__ for record in records],
                "recommendations": [],
                "intents_staged": [],
                "cutoff_late": [],
                "regime": shared_regime,
                "account": marked_account.__dict__,
                "replayed": True,
                "committee_decision_status": (self.ledger.committee_decision(session, epoch_id, policy_id) or {}).get("status", {"status": "legacy_unknown", "degraded": True}),
            }

        records: list[SignalRecord] = []
        timely: list[tuple[dict, SignalRecord]] = []
        policy_inputs: list[tuple[dict, SignalRecord, str]] = []
        policy_provenance_specs: dict[str, dict[str, object]] = {}
        late_ids: list[str] = []
        seen_signal_ids: set[str] = set()
        prior_by_event = {}
        for row in self.ledger.read_signals(end_session=session, epoch_id=epoch_id, policy_id=policy_id):
            if row.reference_session < session:
                prior_by_event.setdefault((row.event_key, row.strategy), []).append(row)
        for signal in shared_signals:
            ticker = str(signal.get("ticker", "")).strip().upper()
            strategy = str(signal.get("strategy", "")).strip()
            direction = str(signal.get("direction", "")).strip()
            if (
                not ticker
                or not strategy
                or direction not in {"long", "short", "neutral"}
            ):
                raise ValueError("candidate identity is incomplete")
            bar = raw_bars.get(ticker)
            if (
                bar is None
                or bar.ticker != ticker
                or bar.session != session
                or bar.adjusted
                or bar.fetched_at < session_close(session)
                or (decision_context is not None and bar.fetched_at > cutoff)
            ):
                raise ValueError(
                    f"missing exact raw reference bar for {ticker}/{session}"
                )
            metadata = (
                signal.get("metadata")
                if isinstance(signal.get("metadata"), dict)
                else {}
            )
            from tradingagents.strategies.orchestration.event_identity import (
                ACTIVE_STRATEGY_NAMES,
                canonical_event_key,
                canonical_observation_time,
            )

            explicit_event_key = metadata.get("event_key")
            if explicit_event_key and strategy not in ACTIVE_STRATEGY_NAMES:
                event_key = str(explicit_event_key)
            else:
                event_key = canonical_event_key(strategy, ticker, metadata, session)
            signal_id = metric_signal_id(
                epoch_id, strategy, policy_id, direction, event_key
            )
            # A late observation is immutable. Offer the same event once at its
            # first eligible session, irrespective of a later direction flip.
            prior_events = prior_by_event.get((event_key, strategy), [])
            prior_timely = [row for row in prior_events
                            if (prior_observation := self.ledger.signal_observation(row.signal_id)) is None
                            or prior_observation[2].get("status", "timely") != "cutoff-late"]
            if prior_timely:
                continue
            if prior_events:
                signal_id = metric_signal_id(epoch_id, strategy, policy_id, direction,
                                             stable_id("eligible_event", event_key, session))
            if signal_id in seen_signal_ids:
                continue
            seen_signal_ids.add(signal_id)
            existing_observation = self.ledger.signal_observation(signal_id)
            if existing_observation is not None:
                record, candidate_context, journal_payload = existing_observation
                if record.reference_session != session:
                    continue
                records.append(record)
                status = str(journal_payload.get("status", "timely"))
                if status == "cutoff-late":
                    late_ids.append(signal_id)
                elif record.reference_session == session:
                    stored_signal = candidate_context.get("signal")
                    if not isinstance(stored_signal, dict):
                        raise ValueError(
                            f"signal {signal_id} lacks canonical committee context"
                        )
                    enriched = dict(stored_signal)
                    enriched["_signal_id"] = signal_id
                    enriched["event_key"] = record.event_key
                    timely.append((enriched, record))
                policy_inputs.append((dict(candidate_context["signal"]), record, status))
                continue

            evidence = _canonical_signal_evidence(
                {
                    "metadata": metadata,
                    "score": signal.get("score", 0),
                    "ticker": ticker,
                    "strategy": strategy,
                    "direction": direction,
                }
            )
            if strategy in ACTIVE_STRATEGY_NAMES:
                observed_at = canonical_observation_time(strategy, metadata)
                event_at = observed_at
            else:
                event_times = [
                    parsed
                    for key in _EVENT_DATETIME_KEYS
                    if (
                        parsed := _metadata_timestamp(
                            metadata, key, allow_date_only=False
                        )
                    )
                    is not None
                ]
                event_times.extend(
                    parsed
                    for key in _EVENT_DATE_ONLY_KEYS
                    if (
                        parsed := _metadata_timestamp(
                            metadata, key, allow_date_only=True
                        )
                    )
                    is not None
                )
                event_at = max(event_times) if event_times else None
                observed_at = (
                    _metadata_timestamp(metadata, "observed_at", allow_date_only=False)
                    if "observed_at" in metadata
                    else event_at
                )
            if observed_at is None:  # pragma: no cover - strict parser invariant.
                raise ValueError(f"{strategy} candidate lacks observation time")
            decision_at = max(
                [cutoff, observed_at] + ([event_at] if event_at is not None else [])
            )
            record = SignalRecord(
                signal_id,
                epoch_id,
                policy_id,
                event_key,
                strategy,
                ticker,
                direction,
                event_at,
                observed_at,
                session,
                bar.close,
                decision_at,
                stable_id("evidence", evidence),
            )
            records.append(record)
            is_late = observed_at > cutoff or (
                event_at is not None and event_at > cutoff
            )
            if is_late:
                late_ids.append(signal_id)
                status = "cutoff-late"
            else:
                enriched = dict(signal)
                enriched["ticker"] = ticker
                enriched["_signal_id"] = signal_id
                enriched["event_key"] = event_key
                timely.append((enriched, record))
                status = "timely"
            policy_inputs.append((dict(signal), record, status))
            llm = metadata.get("llm_analysis")
            journal_payload = asdict(
                JournalEntry(
                    timestamp=record.reference_session.isoformat(),
                    strategy=record.strategy,
                    ticker=record.ticker,
                    direction=record.direction,
                    score=float(signal.get("score", 0) or 0),
                    signal_id=record.signal_id,
                    llm_conviction=(
                        float(llm.get("conviction", llm.get("score", 0)) or 0)
                        if isinstance(llm, dict)
                        else 0.0
                    ),
                    regime=(shared_regime or {}).get("overall_regime", ""),
                    traded=False,
                    entry_price=None,
                    metadata={},
                    status=status,
                )
            )
            self.ledger.record_signal_with_journal(
                record,
                journal_payload,
                record.decision_at,
                {
                    "signal": json.loads(
                        json.dumps(
                            {
                                **signal,
                                "ticker": ticker,
                                "strategy": strategy,
                                "direction": direction,
                            },
                            sort_keys=True,
                            default=str,
                        )
                    )
                },
            )

        if policy_config is not None:
            assert policy_binding is not None
            consumed = risk_context.consumed_event_keys
            profiles = (enrichment or {}).get("profiles", {})
            eligible_signal_ids: set[str] = set()
            def policy_tags(value: object) -> tuple[str, ...]:
                if isinstance(value, str):
                    return (value,) if value else ()
                if not isinstance(value, (list, tuple, set, frozenset)):
                    return ()
                return tuple(str(item) for item in value if str(item))

            for signal, record, status in policy_inputs:
                journal_only = bool(signal.get("journal_only", False))
                reasons: list[str] = []
                if status == "cutoff-late":
                    reasons.append("cutoff_late")
                if journal_only:
                    reasons.append("journal_only")
                if record.direction == "neutral":
                    reasons.append("neutral")
                if record.event_key in consumed:
                    reasons.append("consumed_event")
                if record.direction == "short" and not size_profile.short_eligible:
                    reasons.append("short_ineligible")
                order_eligible = not reasons
                if order_eligible:
                    eligible_signal_ids.add(record.signal_id)
                source_event_keys = policy_tags(signal.get("source_event_keys", ()))
                strategy_tags = tuple(
                    sorted(
                        {record.strategy}
                        | {
                            str(value)
                            for value in policy_tags(signal.get("strategy_tags", ()))
                        }
                    )
                )
                risk_tags = policy_tags(signal.get("risk_tags", ()))
                profile = profiles.get(record.ticker, {})
                sector = (
                    str(profile.get("sector", "Unknown"))
                    if isinstance(profile, Mapping)
                    else "Unknown"
                )
                policy_provenance_specs[record.signal_id] = {
                    "record": record,
                    "source_event_keys": source_event_keys,
                    "strategy_tags": strategy_tags,
                    "risk_tags": risk_tags,
                    "sector": sector,
                    "journal_only": journal_only,
                    "preliminary_reasons": tuple(reasons),
                }
            timely = [
                (signal, record)
                for signal, record in timely
                if record.signal_id in eligible_signal_ids
            ]

        self._journal.mirror_signals(records, {})

        strategy_confidence = {record.strategy: 0.5 for _, record in timely}
        committee_signals = [signal for signal, _ in timely]
        # Policy eligibility may remove every timeout-held candidate. Phase
        # coverage remains independent of the surviving selection input.
        incomplete_model_phase = model_sample_incomplete(shared_signals) or (
            model_coverage is not None and model_coverage.get("complete") is False)
        if resilience and 'model_deadline_exhausted' in enrichment_scope['hold_reasons']:
            incomplete_model_phase = True
        phase_model_coverage = {"complete": not incomplete_model_phase}
        if incomplete_model_phase:
            phase_model_coverage["reason"] = "model_deadline_exhausted"
        research_authority = None
        if resilience:
            from .research_resilience import digest as research_digest, POLICY as RESEARCH_POLICY
            research_authority = {'version': 'research-book-authority-v1', 'policy': RESEARCH_POLICY,
                'source_bundle_sha256': shared_regime['research_source_scope']['source_bundle_sha256'],
                'source_scope_sha256': research_digest(shared_regime['research_source_scope'], codec_limits=source_codec_limits(self.config)),
                'projection_sha256': shared_regime['research_projection_sha256'],
                'shared_context_sha256': research_digest(shared_regime, codec_limits=source_codec_limits(self.config)),
                'entry_hold': bool(incomplete_model_phase or shared_regime['research_hold_reasons']),
                'hold_reasons': (['model_deadline_exhausted'] if incomplete_model_phase else list(shared_regime['research_hold_reasons'])),
                'eligible_signal_ids': sorted(record.signal_id for _, record in timely),
                'candidate_receipts': {record.signal_id: signal['metadata']['research_admission'] for signal, record in timely},
                'retained_origins': {record.signal_id: signal['metadata']['retained_from_signal_id']
                    for signal, record in timely if signal['metadata'].get('retained_from_signal_id')}}
        committee_config = self.config
        if effective_policy_settings is not policy_settings:
            committee_config = {
                **self.config,
                "autoresearch": {
                    **self.ar_config,
                    "portfolio_policy": effective_policy_settings,
                },
            }
        committee = PortfolioCommittee(committee_config, size_profile=size_profile)
        frozen_decision = self.ledger.committee_decision(session, epoch_id, policy_id)
        if frozen_decision is None:
            from .runtime_recovery import require_acquisition_allowed
            require_acquisition_allowed('committee_decision')
            if horizon_recommendations is not None:
                # 1-committee-call-per-horizon path: reuse the horizon-level
                # ranked recommendations and size deterministically per tier.
                from copy import deepcopy
                eligible_recs = [
                    deepcopy(rec)
                    for rec in horizon_recommendations
                    if (rec.direction != "short" or getattr(size_profile, "short_eligible", True))
                ]
                if policy_config is not None and risk_context is not None:
                    from tradingagents.strategies.trading.portfolio_policy import PortfolioPolicy
                    policy_engine = PortfolioPolicy(policy_config)
                    if hasattr(policy_engine, "apply"):
                        applied = policy_engine.apply(eligible_recs, risk_context)
                        if isinstance(applied, tuple) and len(applied) == 2:
                            recommendations, policy_decisions_out = applied
                        else:
                            recommendations = list(applied)
                            policy_decisions_out = tuple(getattr(policy_engine, "last_decisions", ()))
                    else:
                        recommendations, policy_decisions_out = policy_engine.evaluate(
                            eligible_recs, risk_context
                        )
                    committee.last_policy_decisions = tuple(policy_decisions_out)
                else:
                    recommendations = eligible_recs
                    committee.last_policy_decisions = ()
                decision_status = dict(
                    horizon_decision_status
                    or {
                        "status": "selected" if recommendations else "no_candidates",
                        "model_coverage": phase_model_coverage,
                        "shared_horizon_synthesis": True,
                    }
                )
            else:
                positions = []
                for position in self.ledger.open_positions():
                    item = dict(position)
                    ticker = str(item["ticker"])
                    mark = raw_bars[ticker].close
                    value = Decimal(str(item["quantity"])) * mark
                    direction = str(item.get("side", "long"))
                    signed_value = -value if direction == "short" else value
                    item.update(direction=direction, mark_price=str(mark), market_value=str(value),
                                signed_market_value=str(signed_value),
                                weight=float(value / marked_account.net_equity) if marked_account.net_equity else 0.0)
                    positions.append(item)
                recommendations = committee.synthesize(
                    signals=committee_signals, regime_context=shared_regime or {},
                    strategy_confidence=strategy_confidence, current_positions=positions,
                    total_capital=float(marked_account.net_equity), enrichment=enrichment or {}, risk_context=risk_context,
                    model_coverage=phase_model_coverage,
                )
                decision_status = dict(committee.last_decision_status)
            if research_authority is not None:
                decision_status['research_authority'] = research_authority
                decision_status['research_enrichment_scope'] = enrichment_scope
            failures = _enrichment_failures(original_enrichment or {})
            if failures:
                decision_status["enrichment_failures"] = failures
            if "short_interest_acquisition" in (original_enrichment or {}):
                from tradingagents.strategies.data_sources.finra_query import validated_acquisition
                decision_status["short_interest_acquisition"] = validated_acquisition(
                    original_enrichment["short_interest_acquisition"])
            decision_status["selected_signal_ids"] = sorted({
                record.signal_id for _, record in timely for rec in recommendations
                if record.ticker == rec.ticker and record.direction == rec.direction
                and record.strategy in rec.contributing_strategies
            })
            frozen_decision = {"status": decision_status,
                "recommendations": [asdict(rec) for rec in recommendations],
                "policy_decisions": [asdict(row) for row in committee.last_policy_decisions]}
            self.ledger.record_committee_decision(session, epoch_id, policy_id, frozen_decision)
        else:
            if resilience and frozen_decision['status'].get('research_authority') != research_authority:
                raise ValueError('research_book_authority_conflict')
            if resilience and frozen_decision['status'].get('research_enrichment_scope') != enrichment_scope:
                raise ValueError('research_enrichment_scope_conflict')
            from tradingagents.strategies.modules.base import OptionSpec
            from tradingagents.strategies.trading.portfolio_committee import TradeRecommendation
            from tradingagents.strategies.trading.portfolio_policy import PortfolioPolicyDecision
            recommendations = []
            for row in frozen_decision["recommendations"]:
                values = dict(row)
                if values.get("option_spec"):
                    values["option_spec"] = OptionSpec(**values["option_spec"])
                recommendations.append(TradeRecommendation(**values))
            committee.last_policy_decisions = tuple(PortfolioPolicyDecision(**row) for row in frozen_decision["policy_decisions"])
        decision_status = frozen_decision["status"]
        if policy_config is not None:
            policy_decisions = tuple(
                getattr(committee, "last_policy_decisions", ())
            )
            for signal_id, spec in policy_provenance_specs.items():
                record = spec["record"]
                preliminary = tuple(spec["preliminary_reasons"])
                if preliminary:
                    decision = "rejected"
                    reason_codes = preliminary
                    order_eligible = False
                else:
                    # Signal companions describe ingress eligibility only.  A
                    # multi-signal recommendation is audited once in the
                    # candidate-decision table below, avoiding double counts.
                    decision = "accepted"
                    reason_codes = ("ingress_eligible",)
                    order_eligible = True
                self.ledger.record_signal_policy_provenance(
                    signal_id,
                    policy_version=policy_config.version,
                    event_key=record.event_key,
                    source_event_keys=tuple(spec["source_event_keys"]),
                    strategy_tags=tuple(spec["strategy_tags"]),
                    risk_tags=tuple(spec["risk_tags"]),
                    sector=str(spec["sector"]),
                    journal_only=bool(spec["journal_only"]),
                    order_eligible=order_eligible,
                    decision=decision,
                    reason_codes=reason_codes,
                    bound_context_digest=str(policy_binding["context_digest"]),
                    captured_at=cutoff,
                )
            candidate_decision_specs: list[tuple[Any, tuple[str, ...]]] = []
            covered_signal_ids: set[str] = set()
            for outcome in policy_decisions:
                contributors = tuple(
                    sorted(
                        record.signal_id
                        for signal, record in timely
                        if record.ticker == outcome.ticker
                        and record.direction == outcome.direction
                    )
                )
                if not contributors:
                    raise LedgerConflictError(
                        "policy candidate decision lacks signal contributors"
                    )
                candidate_decision_specs.append((outcome, contributors))
                covered_signal_ids.update(contributors)
            candidate_decisions = {
                (item.ticker, item.direction): item for item in policy_decisions
            }
        else:
            candidate_decisions = {}
            candidate_decision_specs = []
            covered_signal_ids = set()
        bridge = ExecutionBridge(self.config, ledger=self.ledger)
        rec_specs: list[tuple[Any, tuple[SignalRecord, ...]]] = []
        for recommendation in recommendations:
            contributors = set(recommendation.contributing_strategies)
            contributor_records = tuple(
                sorted(
                    (
                        record
                        for signal, record in timely
                        if record.ticker == recommendation.ticker
                        and record.direction == recommendation.direction
                        and record.strategy in contributors
                    ),
                    key=lambda record: record.signal_id,
                )
            )
            if not contributor_records:
                continue
            recommendation.contributing_signal_ids = tuple(
                record.signal_id for record in contributor_records
            )
            rec_specs.append((recommendation, contributor_records))

        exit_specs, cancellations = (self._build_exit_specs(
            session, cutoff, eligible_session, raw_bars, exit_data, horizon
        ) if decision_context is None else ([], []))
        staged_ids: list[str] = []

        def persist_staging() -> None:
            publication_at = cutoff
            current_exit_specs, current_cancellations = exit_specs, cancellations
            if decision_context is not None:
                publication_at = require_live_staging(self.config, session, decision_context)
                current_exit_specs, current_cancellations = self._build_exit_specs(
                    session, publication_at, eligible_session, raw_bars, exit_data, horizon)
            zero_share_candidates: set[tuple[str, str]] = set()
            for intent in current_cancellations:
                self.ledger.cancel_intent(
                    intent.intent_id, publication_at, "strategy exit superseded resting stop"
                )
            for intent, lot_quantities in current_exit_specs:
                self.ledger.stage_exit_intent(intent, lot_quantities)
                staged_ids.append(intent.intent_id)
            held = {
                str(position["ticker"]) for position in self.ledger.open_positions()
            }
            for recommendation, contributor_records in rec_specs:
                if recommendation.ticker in held or self.ledger.pending_exit_intents(
                    recommendation.ticker
                ):
                    continue
                try:
                    intent = bridge.stage_intent(
                        recommendation,
                        contributor_records,
                        self.ledger.account_state(),
                        publication_at,
                        eligible_session,
                    )
                except ZeroShareIntentError:
                    zero_share_candidates.add(
                        (recommendation.ticker, recommendation.direction)
                    )
                    logger.info(
                        "Skipping %s %s: approved allocation sizes to zero shares",
                        recommendation.ticker,
                        recommendation.direction,
                    )
                    continue
                if policy_config is not None:
                    assert policy_binding is not None
                    outcome = candidate_decisions.get(
                        (recommendation.ticker, recommendation.direction)
                    )
                    intent_decision = (
                        outcome.decision if outcome is not None else "accepted"
                    )
                    intent_reasons = (
                        (outcome.reason,) if outcome is not None else ("accepted",)
                    )
                    self.ledger.record_intent_policy_provenance(
                        intent.intent_id,
                        signal_ids=intent.signal_ids,
                        policy_version=policy_config.version,
                        event_key=recommendation.event_key,
                        source_event_keys=recommendation.source_event_keys,
                        strategy_tags=recommendation.strategy_tags,
                        risk_tags=recommendation.risk_tags,
                        sector=str(risk_context.sectors.get(
                            recommendation.ticker, "Unknown"
                        )),
                        journal_only=recommendation.journal_only,
                        order_eligible=True,
                        decision=intent_decision,
                        reason_codes=intent_reasons,
                        bound_context_digest=str(policy_binding["context_digest"]),
                        captured_at=cutoff,
                    )
                staged_ids.append(intent.intent_id)
            # Persist final candidate outcomes after whole-share sizing.  One
            # unrepresentable entry must not roll back other entries or exits.
            candidate_decision_ids: list[str] = []
            if policy_config is not None:
                assert policy_binding is not None
                for outcome, contributors in candidate_decision_specs:
                    zero_shares = (
                        outcome.ticker, outcome.direction
                    ) in zero_share_candidates
                    persisted = self.ledger.record_policy_candidate_decision(
                        session,
                        epoch_id=epoch_id,
                        policy_version=policy_config.version,
                        ticker=outcome.ticker,
                        direction=outcome.direction,
                        event_key=outcome.event_key,
                        signal_ids=contributors,
                        requested_weight=outcome.requested_weight,
                        approved_weight=(
                            0.0 if zero_shares else outcome.approved_weight
                        ),
                        decision="rejected" if zero_shares else outcome.decision,
                        reason_codes=(
                            ("zero_shares",) if zero_shares else (outcome.reason,)
                        ),
                        bound_context_digest=str(policy_binding["context_digest"]),
                        captured_at=cutoff,
                    )
                    candidate_decision_ids.append(str(persisted["decision_id"]))
            if policy_config is not None:
                assert policy_binding is not None
                ingress_ids = tuple(sorted(eligible_signal_ids))
                nonselected_ids = tuple(
                    sorted(set(ingress_ids) - covered_signal_ids)
                )
                self.ledger.record_policy_staging_audit_manifest(
                    session,
                    epoch_id=epoch_id,
                    policy_id=policy_id,
                    policy_version=policy_config.version,
                    bound_context_digest=str(policy_binding["context_digest"]),
                    ingress_signal_ids=ingress_ids,
                    candidate_decision_ids=tuple(candidate_decision_ids),
                    committee_not_selected_ids=nonselected_ids,
                    recorded_at=cutoff,
                )
            if decision_context is not None:
                require_live_staging(self.config, session, decision_context)

        executed, _ = self.ledger.complete_staging(
            session,
            epoch_id,
            policy_id,
            cutoff,
            persist_staging,
            expected_staging_state_digest,
        )
        if not executed:
            staged_ids = []
        return {
            "signals": [record.__dict__ for record in records],
            "recommendations": [
                recommendation.__dict__ for recommendation, _ in rec_specs
            ],
            "intents_staged": staged_ids,
            "cutoff_late": late_ids,
            "regime": shared_regime,
            "account": marked_account.__dict__,
            "replayed": not executed,
            "committee_decision_status": decision_status,
        }

    def _build_exit_specs(
        self,
        session: date,
        cutoff: datetime,
        eligible_session: date,
        raw_bars: dict[str, Any],
        data: dict,
        horizon: str,
    ) -> tuple[list[tuple[Any, tuple[tuple[str, int], ...]]], list[Any]]:
        """Build deterministic next-open exits or one persistent stop per lot."""
        from tradingagents.strategies.execution import OrderIntent, stable_id

        exit_specs: list[tuple[OrderIntent, tuple[tuple[str, int], ...]]] = []
        cancellations: list[OrderIntent] = []
        risk = self.config.get("autoresearch", {}).get("risk_gate", {})
        long_stop = Decimal(str(risk.get("global_stop_loss_pct", "0.08")))
        short_stop = Decimal(str(risk.get("short_squeeze_stop_pct", "0.15")))
        for position in self.ledger.open_exit_positions():
            ticker = str(position["ticker"])
            bar = raw_bars.get(ticker)
            if bar is None or bar.session != session or bar.adjusted:
                raise ValueError(
                    f"missing exact exit reference bar for {ticker}/{session}"
                )
            pending = self.ledger.pending_exit_intents(ticker, str(position["lot_id"]))
            should_exit = False
            for strategy_name in position["strategies"]:
                strategy = next(
                    (
                        item
                        for item in self.paper_trade_strategies
                        if item.name == strategy_name
                    ),
                    None,
                )
                if strategy is None:
                    continue
                should_exit, _ = strategy.check_exit(
                    ticker=ticker,
                    entry_price=float(position["entry_price"]),
                    current_price=float(bar.close),
                    holding_days=(session - position["opened_session"]).days,
                    params=strategy.get_default_params(horizon=horizon),
                    data=data,
                    direction=position["direction"],
                )
                if should_exit:
                    break
            if should_exit:
                cancellations.extend(
                    intent for intent in pending if intent.price_rule == "resting_stop"
                )
                pending = [
                    intent for intent in pending if intent.price_rule != "resting_stop"
                ]
                price_rule = "next_session_open"
                stop_price = None
            elif pending:
                continue
            else:
                price_rule = "resting_stop"
                if position["direction"] == "short":
                    stop_price = position["entry_price"] * (Decimal("1") + short_stop)
                else:
                    stop_price = position["entry_price"] * (Decimal("1") - long_stop)
            if pending:
                continue
            side = "cover" if position["direction"] == "short" else "sell"
            signal_ids = tuple(sorted(position["signal_ids"]))
            intent = OrderIntent(
                stable_id(
                    "intent",
                    self.ledger.cohort_id,
                    signal_ids,
                    side,
                    position["quantity"],
                    cutoff,
                    eligible_session,
                    price_rule,
                    stop_price,
                    position["lot_id"],
                ),
                signal_ids,
                self.ledger.cohort_id,
                side,
                int(position["quantity"]),
                cutoff,
                eligible_session,
                price_rule,
                "pending",
                stop_price,
                None,
            )
            exit_specs.append(
                (
                    intent,
                    ((str(position["lot_id"]), int(position["quantity"])),),
                )
            )
        return exit_specs, cancellations

    def _build_regime_model(self, data: dict) -> dict:
        """Build regime model from available data (VIX, credit spreads, yield curve)."""
        vix_data = data.get("yfinance", {}).get("vix")
        vix_level = None
        if vix_data is not None and not vix_data.empty:
            vix_level = float(vix_data["Close"].iloc[-1])

        fred = data.get("fred", {})
        from tradingagents.strategies.modules.commodity_macro import _latest_value
        hy_level = _latest_value(fred.get("hy_spread", fred.get("BAMLH0A0HYM2")))
        credit_bps = None if hy_level is None else hy_level * 100
        yc_slope = _latest_value(fred.get("yield_curve", fred.get("T10Y2Y")))

        if vix_level is None:
            vix_level = _latest_value(fred.get("VIXCLS"))
        if vix_level is not None and not math.isfinite(vix_level):
            vix_level = None
        overall = self._classify_regime(vix_level, credit_bps, yc_slope)
        stressed_vix = self.ar_config.get("risk_discipline", {}).get(
            "regime_vix_stressed", 25.0
        )

        return {
            "vix_level": vix_level,
            "vix_regime": "unknown" if vix_level is None else "crisis"
            if vix_level > 35
            else "elevated"
            if vix_level > stressed_vix
            else "normal"
            if vix_level > 15
            else "low",
            "credit_spread_bps": credit_bps,
            "credit_regime": "unknown" if credit_bps is None else "crisis"
            if credit_bps > 600
            else "stressed"
            if credit_bps > 400
            else "normal",
            "yield_curve_slope": yc_slope,
            "yield_regime": "unknown" if yc_slope is None else "inverted"
            if yc_slope < -0.2
            else "flat"
            if yc_slope < 0.5
            else "normal"
            if yc_slope < 1.5
            else "steep",
            "overall_regime": overall,
            "timestamp": datetime.now().isoformat(),
            "thresholds": {
                "vix": {"low": 15, "elevated": stressed_vix, "crisis": 35},
                "credit_bps": {"stressed": 400, "crisis": 600},
                "yield_curve": {"inverted": -0.2, "flat": 0.5, "steep": 1.5},
            },
        }

    def _classify_regime(self, vix: float, credit_bps: float, yc_slope: float) -> str:
        """Classify overall market regime."""
        stressed_vix = self.ar_config.get("risk_discipline", {}).get(
            "regime_vix_stressed", 25.0
        )
        if any(value is None for value in (vix, credit_bps, yc_slope)):
            return "unknown"
        crisis_signals = 0
        if vix > 35:
            crisis_signals += 1
        if credit_bps > 600:
            crisis_signals += 1
        if yc_slope < -0.2:
            crisis_signals += 1

        if crisis_signals >= 2:
            return "crisis"
        if vix > stressed_vix or credit_bps > 400:
            return "stressed"
        if vix < 15 and credit_bps < 300:
            return "benign"
        return "normal"

    # ------------------------------------------------------------------
    # Data acquisition delegation (owned by shared DataAcquisitionService)
    # ------------------------------------------------------------------

    @property
    def registry(self) -> DataSourceRegistry:
        return self._registry

    @registry.setter
    def registry(self, value: DataSourceRegistry) -> None:
        self._registry = value
        if hasattr(self, "data_acquisition") and self.data_acquisition is not None:
            self.data_acquisition.registry = value

    def _fetch_missing_prices(self, tickers: list[str], start_date: str, end_date: str) -> None:
        self.data_acquisition.fetch_missing_prices(tickers, start_date, end_date)

    def _fetch_all_data(
        self,
        start_date: str,
        end_date: str,
        *,
        acquisition_deadline: float | None = None,
        acquisition_purpose: str = "decision",
    ) -> dict[str, Any]:
        needed = {"yfinance", "fred"}
        for strategy in self.paper_trade_strategies:
            if not self.ar_config.get("disabled_strategies", {}).get(getattr(strategy, "name", "")):
                needed.update(getattr(strategy, "data_sources", ()))
        overrides = {}
        for src_name, method_name in (
            ("yfinance", "_fetch_yfinance_data"),
            ("finnhub", "_fetch_finnhub_data"),
            ("fred", "_fetch_fred_data"),
            ("usaspending", "_fetch_usaspending_data"),
            ("noaa", "_fetch_noaa_data"),
            ("usda", "_fetch_usda_data"),
            ("drought_monitor", "_fetch_drought_data"),
            ("cftc", "_fetch_cftc_data"),
        ):
            if method_name in self.__dict__:
                overrides[src_name] = self.__dict__[method_name]
        return self.data_acquisition.fetch_all_data(
            start_date,
            end_date,
            needed_sources=needed,
            acquisition_deadline=acquisition_deadline,
            acquisition_purpose=acquisition_purpose,
            fetcher_overrides=overrides,
        )

    def _fetch_finnhub_data(self, trading_date: str, max_workflow_budget_s: float | None = None) -> dict[str, Any]:
        return self.data_acquisition.fetch_finnhub_data(trading_date, max_workflow_budget_s=max_workflow_budget_s)

    def _fetch_fred_data(self, start_date: str, end_date: str) -> dict[str, Any]:
        return self.data_acquisition.fetch_fred_data(start_date, end_date)

    def _fetch_usaspending_data(self, trading_date: str) -> dict[str, Any]:
        return self.data_acquisition.fetch_usaspending_data(trading_date)

    def _fetch_noaa_data(self, trading_date: str, vintage_as_of: str | None = None) -> dict[str, Any]:
        return self.data_acquisition.fetch_noaa_data(trading_date, vintage_as_of=vintage_as_of)

    def _fetch_usda_data(self, trading_date: str, vintage_as_of: str | None = None) -> dict[str, Any]:
        return self.data_acquisition.fetch_usda_data(trading_date, vintage_as_of=vintage_as_of)

    def _fetch_drought_data(self, trading_date: str, vintage_as_of: str | None = None) -> dict[str, Any]:
        return self.data_acquisition.fetch_drought_data(trading_date, vintage_as_of=vintage_as_of)

    def _fetch_cftc_data(self, trading_date: str | None = None, vintage_as_of: str | None = None) -> dict[str, Any]:
        return self.data_acquisition.fetch_cftc_data(trading_date, vintage_as_of=vintage_as_of)

    def _fetch_yfinance_data(self, start_date: str, end_date: str) -> dict[str, Any]:
        return self.data_acquisition.fetch_yfinance_data(start_date, end_date)

    # ------------------------------------------------------------------
    # LLM enrichment
    # ------------------------------------------------------------------

    def _enrich_with_llm(
        self,
        candidates: list[Candidate],
        strategy_name: str,
        regime_context: dict | None = None,
        *, filing_data: dict | None = None, universe=None,
    ) -> list[Candidate]:
        """Run LLM analysis on candidates that have needs_llm_analysis=True."""
        from tradingagents.strategies.runtime_deadline import (
            model_phase_budget_seconds, current_model_deadline, model_budget,
        )
        from tradingagents.strategies.candidate_parallel import analyze_candidates
        allowance = model_phase_budget_seconds(self.config)
        if current_model_deadline() is None:
            import time
            with model_budget(time.monotonic() + allowance):
                return self._enrich_with_llm(candidates, strategy_name, regime_context,
                                             filing_data=filing_data, universe=universe)
        enriched = list(candidates)
        required = [c for c in enriched if c.metadata.get("needs_llm_analysis")]
        try:
            outcomes = analyze_candidates(required, analyzer=self._analyzer,
                analyze_one=lambda c, worker: self._analyze_candidate(c, strategy_name, regime_context, worker,
                    filing_data=filing_data, universe=universe),
                max_workers=self.ar_config.get("candidate_analysis_workers", 1))
        except ValueError:
            # A configuration/client refusal retains every discovered input and
            # fails required coverage. Never silently change the transport.
            from tradingagents.strategies.candidate_parallel import CandidateAnalysisOutcome
            outcomes = [CandidateAnalysisOutcome(c, "analysis_unavailable") for c in required]
        for outcome in outcomes:
            if not outcome.failure:
                continue
            c = outcome.candidate
            c.metadata.update(analysis_status="failed", analysis_failure_reason=outcome.failure)
            optional = c.metadata.get("analysis_type") in {"insider_activity", "commodity_macro", "ag_weather"} and c.metadata.get("deterministic_evidence_complete") is True
            if not optional or outcome.failure == "model_deadline_exhausted":
                c.journal_only = True
                c.metadata.setdefault("non_actionable_reason", "required_analysis_failed")

        if any(c.metadata.get("analysis_failure_reason") == "model_deadline_exhausted" for c in enriched):
            # A timeout must not select a prefix of the admitted sample or route
            # optional assessments back into deterministic selection.
            for c in enriched:
                c.journal_only = True
                c.metadata.update(analysis_status="failed", analysis_failure_reason="model_deadline_exhausted",
                                  non_actionable_reason="model_sample_incomplete")
        return enriched

    def _analyze_candidate(self, c: Candidate, strategy_name: str, regime_context: dict | None, analyzer, *,
                           filing_data: dict | None = None, universe=None) -> None:
        """Validate one assessment using this worker's independent analyzer state."""
        from copy import deepcopy
        import hashlib
        from .research_resilience import configured as resilience_configured
        from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded, model_timeout
        from tradingagents.strategies.candidate_response_reuse import begin_candidate_response, commit_candidate_response, end_candidate_response
        resilience = resilience_configured(self.ar_config)
        if resilience:
            # A refusal before the call boundary must not inherit the preceding
            # candidate's request, returned text, or transport provenance.
            analyzer.last_research_request = None
            analyzer.last_call_response = {'received': False, 'text': None}
            analyzer.last_call_provenance = {}
            analyzer.last_call_failure = ''
        analysis_type = c.metadata.get("analysis_type", "")
        if analysis_type == 'bounded_document_research':
            analyzer.last_bounded_document_attempt = None
        llm_result = {}
        full_filing = c.metadata.get('full_filing_evidence_policy') == 'complete_submission_v1'
        full_regulation = analysis_type == 'regulation' and (
            self.ar_config.get('regulation_evidence_policy') is not None
            or c.metadata.get('regulation_evidence_policy') is not None)
        regulation_call_started = False
        optional = analysis_type in {"insider_activity", "commodity_macro", "ag_weather"} and c.metadata.get("deterministic_evidence_complete") is True

        reuse_token = begin_candidate_response(strategy_name, analysis_type, c.metadata.get("discovery_id", ""), optional)

        try:
            if not c.ticker and 'edgar' in self.ar_config.get('retired_sources', {}):
                raise ValueError('invalid_retired_source_identity_dependency')
            model_timeout()
            required_text_fields = {
                "earnings_call": ("analysis_text", "transcript_text"),
                "filing_change": ("current_text",), "exec_comp": ("proxy_text",),
                "material_event": ("current_text",), "activist_stake": ("current_text",),
                "passive_stake": ("current_text",), "quantum_readiness": ("analysis_text",),
                "supply_chain": ("headline", "summary"), "regulation": ("title", "summary"),
                "litigation": ("case_name", "nature_of_suit", "cause"),
            }
            fields = required_text_fields.get(analysis_type)
            if not full_filing and not full_regulation and fields and not any(isinstance(c.metadata.get(key), str) and c.metadata[key].strip() for key in fields):
                raise ValueError("missing_source_text")
            if analysis_type == 'bounded_document_research':
                # Held until independent claim/context verification and scoped
                # thesis validation. Ordinary committee/risk still own orders.
                c.journal_only = True
                decision_policy = self.ar_config.get('bounded_document_decision_policy')
                decision_options = ({'decision_scope':c.metadata.get('bounded_document_decision_scope'),
                                     'ticker':c.ticker} if decision_policy is not None else {})
                scope = decision_options.get('decision_scope')
                if isinstance(scope,dict) and 'independent_review' not in scope:
                    decision_options['verification_clock'] = lambda:datetime.now(timezone.utc).isoformat()
                llm_result = analyzer.analyze_bounded_document_evidence(
                    c.metadata['bounded_document_evidence'], session_as_of=c.date,
                    regime_context=regime_context, **decision_options)
                if decision_policy is not None:
                    eligibility = llm_result.get('decision_eligibility', {})
                    from tradingagents.strategies.document_review_clock import POLICY as NEXT_OPEN_POLICY
                    expected_authority = ('requires_final_decision_clock' if decision_policy == NEXT_OPEN_POLICY
                        else 'ordinary_committee_and_risk_gates')
                    if (decision_policy not in ('verified_scoped_claims_v1', NEXT_OPEN_POLICY)
                            or eligibility.get('eligible') is not True
                            or eligibility.get('policy') != decision_policy
                            or eligibility.get('execution_authority') != expected_authority
                            or llm_result.get('assessment_scope') != 'selected_pages'
                            or llm_result.get('journal_only') is not (llm_result.get('direction') == 'neutral')):
                        raise ValueError('invalid_bounded_document_eligibility')
                    c.journal_only = llm_result['journal_only']
                elif (llm_result.get('journal_only') is not True
                        or llm_result.get('trading_authority') is not False
                        or llm_result.get('assessment_scope') != 'selected_pages'):
                    raise ValueError('invalid_bounded_document_authority')
            elif full_filing:
                from tradingagents.strategies.orchestration.filing_inputs import filing_analysis_inputs
                filing_arguments = filing_analysis_inputs(c, filing_data, universe)
                llm_result = analyzer.analyze_filing_evidence(
                    analysis_type, regime_context=regime_context, **filing_arguments)
                c.metadata['document_assessment'] = llm_result['document_assessment']
                if llm_result['filing_evidence_status'] != 'sufficient':
                    c.metadata['filing_assessment'] = llm_result
                    raise ValueError('missing_required_filing_analysis')
            elif full_regulation:
                from tradingagents.strategies.orchestration.regulation_inputs import (
                    regulation_analysis_inputs, validate_regulation_policy,
                )
                if self.ar_config.get('regulation_evidence_policy') not in (None, 'complete_document_text_v1'):
                    raise ValueError('invalid_regulation_evidence_policy')
                source_graph = (filing_data or {}).get('regulations', {}).get('regulation_evidence', {})
                if (self.ar_config.get('regulation_retrieval_policy') is not None
                        or self.ar_config.get('regulation_assessment_policy') is not None
                        or source_graph.get('retrieval_policy') is not None):
                    validate_regulation_policy(filing_data, self.ar_config)
                regulation_arguments = regulation_analysis_inputs(c, filing_data, universe)
                regulation_call_started = True
                llm_result = analyzer.analyze_regulation_evidence(
                    regime_context=regime_context, **regulation_arguments,
                    **({'assessment_policy': self.ar_config['regulation_assessment_policy']}
                       if self.ar_config.get('regulation_assessment_policy') is not None else {}))
                c.metadata['document_assessment'] = llm_result['document_assessment']
                c.metadata['regulation_evidence_status'] = llm_result['regulation_evidence_status']
                abstained_reg = (
                    isinstance(llm_result, dict)
                    and llm_result.get('document_assessment') == 'assessed_no_directional_thesis'
                )
                if llm_result['regulation_evidence_status'] != 'sufficient' and not (
                        self.ar_config.get('regulation_assessment_policy') == 'recorded_abstention_v1'
                        and abstained_reg):
                    c.metadata['regulation_assessment'] = llm_result
                    provenance = getattr(analyzer, 'last_call_provenance', None)
                    if isinstance(provenance, dict) and provenance:
                        c.metadata['model_provenance'] = dict(provenance)
                    raise ValueError('missing_required_regulation_analysis')
            elif analysis_type == 'state_economics':
                from tradingagents.strategies.data_sources.state_economics_policy import validate_assessment
                event, exposure = c.metadata['state_event'], c.metadata['state_exposure']
                llm_result = analyzer.analyze_state_economics(ticker=c.ticker, state_event=event,
                    state_exposure=exposure, regime_context=regime_context)
                llm_result = validate_assessment(llm_result, ticker=c.ticker, event=event, exposure=exposure)
            elif analysis_type == "earnings_call":
                llm_result = analyzer.analyze_earnings_call(
                    c.metadata.get(
                        "analysis_text", c.metadata.get("transcript_text", "")
                    ),
                    c.ticker,
                    regime_context=regime_context,
                    text_source=c.metadata.get("text_source", "earnings_news"),
                )
            elif analysis_type == "regulation":
                llm_result = analyzer.analyze_regulation(
                    c.metadata.get("title", ""),
                    c.metadata.get("summary", ""),
                    c.metadata.get("agency_id", ""),
                    regime_context=regime_context,
                )
            elif analysis_type == "supply_chain":
                llm_result = analyzer.analyze_supply_chain(
                    c.metadata.get("headline", ""),
                    c.metadata.get("summary", ""),
                    c.ticker,
                    c.metadata.get("affected_peers", []),
                    regime_context=regime_context,
                )
            elif analysis_type == "litigation":
                llm_result = analyzer.analyze_litigation(
                    c.metadata.get("case_name", ""),
                    c.metadata.get("nature_of_suit", ""),
                    c.metadata.get("cause", ""),
                    c.metadata.get("court", ""),
                    regime_context=regime_context,
                )
            elif analysis_type == "insider_activity":
                cluster_type = c.metadata.get("cluster_type", "")
                form4_options = {}
                if 'form4_context' in c.metadata or 'form4_evidence_policy' in c.metadata:
                    from .form4_inputs import form4_analysis_inputs
                    form4_options['form4_context'] = form4_analysis_inputs(c, filing_data, universe)
                if cluster_type == "buy_cluster":
                    llm_result = analyzer.analyze_insider_context(
                        c.metadata.get("filings", []),
                        c.ticker,
                        regime_context=regime_context,
                        **form4_options,
                    )
                elif cluster_type == "sell_pattern":
                    llm_result = analyzer.analyze_10b5_1_plan(
                        c.metadata.get("filings", []),
                        c.ticker,
                        regime_context=regime_context,
                        **form4_options,
                    )
            elif analysis_type == "filing_change":
                llm_result = analyzer.analyze_filing_change(
                    c.metadata.get("current_text", ""),
                    c.metadata.get("prior_text", ""),
                    c.ticker,
                    regime_context=regime_context,
                )
            elif analysis_type in {"material_event", "activist_stake", "passive_stake"}:
                text = c.metadata.get("current_text", "")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("missing_source_text")
                llm_result = analyzer.analyze_filing_change(text, "", c.ticker, regime_context=regime_context)
            elif analysis_type == "quantum_readiness":
                text = c.metadata.get("analysis_text", "")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("missing_source_text")
                llm_result = analyzer.analyze_quantum_readiness(text, c.ticker, text_source="news", regime_context=regime_context)
            elif analysis_type == "commodity_macro":
                llm_result = analyzer.analyze_commodity_macro(
                    ticker=c.ticker, commodity_name=c.metadata.get("commodity", ""),
                    cot_context=c.metadata.get("cot_evidence", {}),
                    macro_context=c.metadata.get("macro_evidence", {}),
                    regime_context=regime_context)
            elif analysis_type == "exec_comp":
                llm_result = analyzer.analyze_exec_comp(
                    c.metadata.get("proxy_text", ""),
                    c.ticker,
                    regime_context=regime_context,
                )
            elif analysis_type == "ag_weather":
                llm_result = analyzer.analyze_ag_weather(
                    ticker=c.ticker,
                    commodity_name=c.metadata.get("commodity", c.ticker),
                    ag_context={
                        "drought_score": c.metadata.get("drought_score", 0),
                        "drought_states": c.metadata.get("drought_states", {}),
                        "noaa_data": c.metadata.get("noaa_data", {}),
                        "usda_data": c.metadata.get("usda_data", {}),
                    },
                    trailing_return=c.metadata.get("trailing_return", 0),
                    hold_days=c.metadata.get("hold_days", 21),
                    regime_context=regime_context,
                )
            else:
                raise ValueError(f"unsupported_required_analysis:{analysis_type}")
            model_timeout()
            if resilience:
                observed = getattr(analyzer, 'last_call_response', None)
                if isinstance(observed, dict) and observed.get('received') is True and type(observed.get('text')) is str:
                    try:
                        observed['text'].encode('utf-8')
                    except UnicodeEncodeError as error:
                        raise ValueError('invalid_response_encoding') from error
            provenance = getattr(analyzer, "last_call_provenance", None)
            if isinstance(provenance, dict) and provenance:
                c.metadata["model_provenance"] = dict(provenance)
            if getattr(analyzer, "last_call_failure", "") == "model_deadline_exhausted":
                raise ModelDeadlineExceeded("model_deadline_exhausted")
            if not isinstance(llm_result, dict) or not llm_result:
                raise ValueError("analysis_unavailable")
            llm_result = dict(llm_result)
            abstained = full_regulation and (
                llm_result.get("document_assessment") == "assessed_no_directional_thesis"
            )
            if not abstained and llm_result.get("direction") not in {"long", "short", "neutral"}:
                raise ValueError("invalid_direction")
            score_key = "conviction" if "conviction" in llm_result else "score"
            value = 0.0 if abstained else llm_result.get(score_key)
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError("invalid_score")
            score = float(value)
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("invalid_score")
            if not abstained:
                llm_result[score_key] = score
            for key in ("defendant_ticker", "primary_ticker"):
                if key in llm_result and (not isinstance(llm_result[key], str) or len(llm_result[key]) > 16):
                    raise ValueError(f"invalid_{key}")
            for key in ("affected_tickers", "secondary_tickers"):
                if key in llm_result and (not isinstance(llm_result[key], list) or len(llm_result[key]) > 50 or any(not isinstance(t, str) or not t.strip() or len(t) > 16 for t in llm_result[key])):
                    raise ValueError(f"invalid_{key}")
            if not optional and not any(isinstance(llm_result.get(key), str) and llm_result[key].strip() for key in ("rationale", "reasoning", "evidence_claim")):
                raise ValueError("missing_analysis_explanation")
            for key in ("rationale", "reasoning", "evidence_claim"):
                if key in llm_result and not isinstance(llm_result[key], str):
                    raise ValueError(f"invalid_{key}")
            for key in ("tone_assessment", "primary_impact", "duration_estimate", "impact_assessment", "case_type", "pqc_readiness", "crypto_dependency"):
                if key in llm_result and not isinstance(llm_result[key], str):
                    raise ValueError(f"invalid_{key}")
            for key in ("changes", "red_flags", "comp_changes", "guidance_changes", "notable_insiders", "affected_sectors"):
                if key in llm_result and (not isinstance(llm_result[key], list) or len(llm_result[key]) > 50 or any(not isinstance(item, str) for item in llm_result[key])):
                    raise ValueError(f"invalid_{key}")
            if "severity" in llm_result and llm_result["severity"] not in {"low", "medium", "high", "critical"}:
                raise ValueError("invalid_severity")
            if "regime_signal" in llm_result and llm_result["regime_signal"] not in {"bull", "bear", "neutral", "accelerating", "stalling"}:
                raise ValueError("invalid_regime_signal")
            if "regime_confidence" in llm_result:
                value = llm_result["regime_confidence"]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError("invalid_regime_confidence")
            if "cluster_size" in llm_result and (type(llm_result["cluster_size"]) is not int or llm_result["cluster_size"] < 0):
                raise ValueError("invalid_cluster_size")
            if "secondary_impacts" in llm_result:
                impacts = llm_result["secondary_impacts"]
                if not isinstance(impacts, list) or len(impacts) > 50 or any(not isinstance(item, dict) or not isinstance(item.get("ticker"), str) or not isinstance(item.get("relationship"), str) or not isinstance(item.get("estimated_impact", item.get("impact")), str) for item in impacts):
                    raise ValueError("invalid_secondary_impacts")
            ticker = c.ticker
            if full_regulation:
                if llm_result.get('document_assessment') == 'assessed_no_directional_thesis' or abstained:
                    if llm_result.get('target_ticker') is not None:
                        raise ValueError('invalid_regulation_target')
                    ticker = ''
                    c.journal_only = True
                elif llm_result.get('document_assessment') == 'assessed_directional':
                    ticker = llm_result.get('target_ticker')
                    if not ticker or ticker not in {item['ticker'] for item in regulation_arguments['target_bindings']}:
                        raise ValueError('invalid_regulation_target')
                else:
                    raise ValueError('missing_required_regulation_analysis')
            elif full_filing and not ticker:
                if llm_result.get('document_assessment') != 'assessed_no_directional_thesis':
                    raise ValueError('unresolved_issuer')
                c.journal_only = True
                c.metadata['non_actionable_reason'] = 'equity_universe_unresolved'
            elif not ticker:
                ticker = llm_result.get("defendant_ticker") or next(iter(llm_result.get("affected_tickers", [])), "")
                ticker = ticker.strip().upper()
                if 'edgar' in self.ar_config.get('retired_sources', {}):
                    raise ValueError('invalid_retired_source_identity_dependency')
                edgar = self.registry.get("edgar")
                if not ticker or edgar is None or not edgar.validate_ticker(ticker):
                    raise ValueError("unresolved_issuer")
            # Commit fields only after full schema and entity validation.
            reuse = commit_candidate_response()
            if reuse is not None:
                analyzer.last_call_provenance["request_reuse"] = reuse
                c.metadata["model_provenance"] = dict(analyzer.last_call_provenance)
            c.ticker, c.direction, c.score = ticker, 'neutral' if abstained else llm_result["direction"], score
            c.metadata["llm_analysis"] = llm_result
            c.metadata["analysis_status"] = "validated"
            if c.metadata.get("non_actionable_reason") == "missing_source_text":
                c.metadata.pop("non_actionable_reason", None)
                c.journal_only = False
        except Exception as exc:
            c.metadata["analysis_status"] = "failed"
            reason = str(exc)
            if isinstance(exc, ModelDeadlineExceeded):
                reason = "model_deadline_exhausted"
            elif not isinstance(exc, ValueError) or not reason.startswith(("invalid_", "missing_", "unsupported_required_analysis:", "analysis_unavailable", "unresolved_issuer")):
                reason = "analysis_unavailable"
            c.metadata["analysis_failure_reason"] = reason[:160]
            if not optional or isinstance(exc, ModelDeadlineExceeded):
                c.journal_only = True
                c.metadata.setdefault("non_actionable_reason", "required_analysis_failed")
            logger.warning("LLM analysis failed for %s/%s: %s", strategy_name, c.ticker, reason)
        finally:
            if resilience:
                observed = getattr(analyzer, 'last_call_response', None)
                text = observed.get('text') if isinstance(observed, dict) and observed.get('received') is True else None
                raw, encoding = None, None
                if type(text) is str:
                    encoding = 'utf-8'
                    try:
                        raw = text.encode('utf-8')
                    except UnicodeEncodeError:
                        # Keep invalid scalars losslessly and explicitly. They
                        # cannot become a validated assessment or memo entry.
                        raw = text.encode('utf-8', errors='surrogatepass')
                        encoding = 'utf-8-surrogatepass'
                status = c.metadata.get('analysis_status', 'failed')
                request = getattr(analyzer, 'last_research_request', None)
                provenance = getattr(analyzer, 'last_call_provenance', None)
                c.metadata['research_model_outcome'] = {
                    'version': 'research-model-outcome-v1',
                    'model_request': deepcopy(request) if isinstance(request, dict) else None,
                    'response_received': raw is not None,
                    'response_sha256': hashlib.sha256(raw).hexdigest() if raw is not None else None,
                    'response_bytes': len(raw) if raw is not None else None, 'response_encoding': encoding,
                    # This records this assessment, before any later timeout
                    # holds the complete sample through final candidate flags.
                    'analysis_status': status,
                    'analysis_failure_reason': c.metadata.get('analysis_failure_reason') if status == 'failed' else None,
                    'model_provenance': deepcopy(provenance) if isinstance(provenance, dict) else {},
                }
            if regulation_call_started:
                attempt = getattr(analyzer, 'last_regulation_attempt', None)
                provenance = getattr(analyzer, 'last_call_provenance', None)
                if isinstance(attempt, dict):
                    c.metadata['regulation_model_attempt'] = deepcopy(attempt)
                    # A successful candidate commit can append its reuse
                    # receipt after analyzer validation. Keep both retained
                    # copies bound to the same final transport provenance.
                    if isinstance(provenance, dict):
                        c.metadata['regulation_model_attempt']['model_provenance'] = deepcopy(provenance)
                if isinstance(provenance, dict):
                    c.metadata['model_provenance'] = deepcopy(provenance)
            end_candidate_response(reuse_token)
