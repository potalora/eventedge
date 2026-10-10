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


def _provider_errors(
    data: Mapping[str, Any], sources: Iterable[str], *, include_optional: bool = False
) -> dict[str, str]:
    """Return required-source failures, optionally including enrichment diagnostics."""
    errors: dict[str, str] = {}
    for source in sources:
        if source in OPTIONAL_ENRICHMENT_SOURCES and not include_optional:
            continue
        if source not in data:
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
        adaptive_confidence: bool = False,
        ledger: PortfolioLedger | None = None,
        outcome_reader: Callable[[str], Iterable[OutcomeRecord]] | None = None,
    ):
        if adaptive_confidence:
            raise ValueError(
                "production learning is disabled; adaptive_confidence=True is rejected"
            )
        self.config = config or {}
        self.ar_config = self.config.get("autoresearch", {})

        # Load strategies (paper-trade only)
        self.paper_trade_strategies = strategies or get_paper_trade_strategies()

        # Data source registry
        self.registry = registry or build_default_registry(self.ar_config)

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

        # Price cache: ticker -> DataFrame
        self._price_cache: dict[str, pd.DataFrame] = {}

        # Adaptive confidence: journal-derived (True) or fixed 0.5 (False)
        self._adaptive_confidence = adaptive_confidence
        self.ledger = ledger
        self._outcome_reader = outcome_reader or (lambda _strategy: ())

        # Signal journal (shared across methods)
        from tradingagents.strategies.learning.signal_journal import SignalJournal

        self._journal = SignalJournal(
            self.ar_config.get("state_dir", "data/state"), ledger=self.ledger
        )

        # OpenBB availability flag — checked once at startup
        self._openbb_source = self.registry.get("openbb")
        self._openbb_available = (
            self._openbb_source.is_available() if self._openbb_source else False
        )

        # Cycle tracking (observation-only)
        self._cycle_tracker = None  # Initialized when gen_start_date is known

    def _emit(self, kind: str, **data: Any) -> None:
        self._on_event(kind, **data)

    def set_cycle_tracker(self, gen_start_date: str) -> None:
        """Initialize cycle tracking for this engine's state directory."""
        from tradingagents.strategies.state.cycle_tracker import CycleTracker

        state_dir = self.ar_config.get("state_dir", "data/state")
        self._cycle_tracker = CycleTracker(gen_start_date, state_dir)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_signals(all_signals: list[dict]) -> list[dict]:
        """Keep the single highest-conviction candidate per (strategy, ticker).

        supply_chain emits one candidate per news article, and LLM enrichment can
        tag the same ticker with opposing directions (e.g. 1 short + 3 long for
        AAPL). The previous logic cancelled opposing same-ticker directions, which
        removed BOTH and silenced the strategy entirely. Collapsing to the
        highest-conviction signal per (strategy, ticker) gives one coherent view
        and prevents self-cancellation. Cross-strategy disagreements are left
        intact (they were never resolved by the old key, which included strategy).
        """
        best: dict[tuple[str, str], dict] = {}
        for signal in all_signals:
            st = (signal["strategy"], signal["ticker"])
            if st not in best or signal["score"] > best[st]["score"]:
                best[st] = signal
        return [s for s in best.values() if s.get("ticker", "").strip()]

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
        """Run strategy screening and LLM enrichment (steps 1-2).

        Returns enriched, deduped signals, regime model, and health records. These can be
        shared across cohorts so LLM non-determinism doesn't confound results.
        """
        regime_model = self._build_regime_model(data)
        regime_model.setdefault("timestamp", datetime.now().isoformat())

        universe = None
        universe_failure = False
        if self.ar_config.get("equity_universe_policy"):
            from tradingagents.strategies.data_sources.equity_universe import EquityUniverse, POLICY
            try:
                if self.ar_config["equity_universe_policy"] != POLICY:
                    raise ValueError("unsupported equity universe policy")
                universe_data = data.get("equity_universe", {})
                if universe_data.get("error"):
                    raise ValueError("equity universe acquisition failed")
                universe = EquityUniverse(universe_data.get("snapshot"),
                    company_map=data.get("edgar", {}).get("company_tickers"))
            except (ValueError, TypeError, AttributeError):
                universe_failure = True
        all_signals: list[dict] = []
        health: list[StrategyHealthRecord] = []
        for strategy in self.paper_trade_strategies:
            self._emit("strategy_start", name=strategy.name, track="paper_trade")
            disabled_reason = self.ar_config.get("disabled_strategies", {}).get(strategy.name) or getattr(strategy, "retirement_reason", None)
            if disabled_reason:
                from tradingagents.strategies.metrics.identity import _stable_id
                health.append(StrategyHealthRecord(
                    health_id=_stable_id("health", epoch_id, date.fromisoformat(trading_date), policy_id, strategy.name),
                    epoch_id=epoch_id, session=date.fromisoformat(trading_date), policy_id=policy_id,
                    strategy=strategy.name, status="disabled_by_policy", signal_count=0,
                    evidence={"reason": disabled_reason, "data_sources": sorted(strategy.data_sources), "candidate_count": 0}))
                continue
            try:
                params = strategy.get_default_params(horizon=horizon)
                from tradingagents.strategies.modules.admission import admit_candidates, candidate_universe
                with candidate_universe(universe):
                    candidates = strategy.screen(data, trading_date, params)
                    if not hasattr(candidates, "admission_manifest"):
                        candidates = admit_candidates(strategy.name, candidates, budget=None)
                admission_manifest = candidates.admission_manifest
                error = None
            except Exception as exc:
                candidates = []
                admission_manifest = None
                error = exc
                logger.exception("Strategy %s screen failed", strategy.name)
            if candidates:
                candidates = self._enrich_with_llm(candidates, strategy.name, regime_context=regime_model)
            universe_assessments = []
            if universe is not None:
                for candidate in candidates:
                    decision = universe.decision(candidate.ticker)
                    assessment = {"discovery_id": candidate.metadata.get("discovery_id"),
                        "symbol": candidate.ticker, "decision": decision,
                        "policy": universe.evidence["policy"],
                        "assets_sha256": universe.evidence["assets_sha256"]}
                    universe_assessments.append(assessment)
                    candidate.metadata["equity_universe_post_analysis"] = assessment
                    if decision in {"outside_sip_exchange_universe", "inactive_asset", "absent_from_asset_master"}:
                        candidate.metadata["equity_universe_excluded"] = True
                    elif decision != "eligible":
                        candidate.metadata.setdefault("non_actionable_reason", "equity_universe_unresolved")
                        candidate.journal_only = True
            provider_errors = _provider_errors(data, strategy.data_sources)
            if universe_failure:
                provider_errors = dict(provider_errors, equity_universe="universe_evidence_unavailable")
            non_actionable = sorted({c.metadata["non_actionable_reason"] for c in candidates if c.metadata.get("non_actionable_reason")})
            if non_actionable:
                provider_errors = dict(provider_errors, analysis="required_evidence_unavailable")
            if error is None and any(str(c.metadata.get("analysis_failure_reason", "")).startswith("unsupported_required_analysis:") for c in candidates):
                error = ValueError("unsupported_required_analysis")
            health_record = classify_strategy_run(
                epoch_id=epoch_id, session=date.fromisoformat(trading_date),
                policy_id=policy_id, strategy=strategy.name,
                data_sources=tuple(strategy.data_sources), candidates=candidates,
                provider_errors=provider_errors, exception=error, admission_manifest=admission_manifest)
            if universe is not None:
                health_record.evidence["universe_assessments"] = universe_assessments
                health_record.evidence["universe_policy"] = universe.evidence["policy"]
            if non_actionable:
                health_record.evidence["non_actionable_reasons"] = non_actionable
                health_record.evidence["actionable_candidate_count"] = sum(not c.journal_only for c in candidates)
            source_coverage = {source: data[source]["coverage"] for source in strategy.data_sources if isinstance(data.get(source), dict) and "coverage" in data[source]}
            if source_coverage:
                health_record.evidence["source_coverage"] = source_coverage
            health.append(health_record)
            for c in candidates:
                if c.metadata.get("equity_universe_excluded"):
                    continue
                all_signals.append(
                    {
                        "ticker": c.ticker,
                        "direction": c.direction,
                        "score": c.score,
                        "strategy": strategy.name,
                        "metadata": c.metadata,
                        "event_key": c.event_key,
                        "source_event_keys": c.source_event_keys,
                        "strategy_tags": c.strategy_tags,
                        "risk_tags": c.risk_tags,
                        "journal_only": c.journal_only,
                    }
                )
            self._emit("strategy_done", name=strategy.name, num_signals=len(candidates))

        all_signals, health = hold_incomplete_model_sample(all_signals, health)
        # Preserve every event identity. Committee synthesis may aggregate a
        # decision view, but the authoritative ledger must retain each catalyst.
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

    def pending_late_signals(self, session: date, epoch_id: str) -> list[dict]:
        """Return frozen actionable observations awaiting their first timely offer.

        Callers include these candidates before pricing and quarantine filtering.
        Today's rows cannot consume the queue: replay must reconstruct the same
        initial candidate set, even after staging today's timely observation.
        """
        from tradingagents.strategies.orchestration.trading_calendar import session_close

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
                    or record.observed_at > session_close(session)):
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
        cutoff = session_close(session)
        eligible_session = next_session(session)
        expected_staging_state_digest = self.ledger.verify_session_phase_chain(
            session, PHASES
        )
        replaying = self.ledger.staging_completed(session, epoch_id, policy_id)

        raw_bars = data.get("_execution_reference_bars", {})
        if not isinstance(raw_bars, dict):
            raise ValueError("_execution_reference_bars must be a mapping")

        policy_settings = self.ar_config.get("portfolio_policy")
        policy_enabled = size_profile is not None and isinstance(
            policy_settings, dict
        )
        policy_config = (
            PortfolioPolicyConfig.from_size_profile(size_profile, policy_settings)
            if policy_enabled
            else None
        )
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
                    context=portfolio_risk_context_document(risk_context),
                    bound_at=cutoff,
                )
        if replaying:
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
                or bar.fetched_at < cutoff
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

        strategy_confidence = {
            record.strategy: (
                self._compute_strategy_confidence(record.strategy)
                if self._adaptive_confidence
                else 0.5
            )
            for _, record in timely
        }
        committee_signals = [signal for signal, _ in timely]
        # Policy eligibility may remove every timeout-held candidate. Phase
        # coverage remains independent of the surviving selection input.
        incomplete_model_phase = model_sample_incomplete(shared_signals) or (
            model_coverage is not None and model_coverage.get("complete") is False)
        phase_model_coverage = {"complete": not incomplete_model_phase}
        if incomplete_model_phase:
            phase_model_coverage["reason"] = "model_deadline_exhausted"
        committee = PortfolioCommittee(self.config, size_profile=size_profile)
        frozen_decision = self.ledger.committee_decision(session, epoch_id, policy_id)
        if frozen_decision is None:
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
            failures = _enrichment_failures(enrichment or {})
            if failures:
                decision_status["enrichment_failures"] = failures
            if "short_interest_acquisition" in (enrichment or {}):
                from tradingagents.strategies.data_sources.finra_bulk import validated_acquisition
                decision_status["short_interest_acquisition"] = validated_acquisition(
                    enrichment["short_interest_acquisition"])
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

        exit_specs, cancellations = self._build_exit_specs(
            session, cutoff, eligible_session, raw_bars, data, horizon
        )
        staged_ids: list[str] = []

        def persist_staging() -> None:
            zero_share_candidates: set[tuple[str, str]] = set()
            for intent in cancellations:
                self.ledger.cancel_intent(
                    intent.intent_id, cutoff, "strategy exit superseded resting stop"
                )
            for intent, lot_quantities in exit_specs:
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
                        cutoff,
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

    def run_learning_loop(self) -> dict:
        """Refuse retired production learning before any state mutation."""
        raise RuntimeError("production learning is disabled; no state was changed")

    # ------------------------------------------------------------------
    # Strategy confidence
    # ------------------------------------------------------------------

    def _compute_strategy_confidence(self, strategy_name: str) -> float:
        """Compute confidence from governed v2 directional accuracy.

        Maps hit_rate [0.3, 0.7] → confidence [0.2, 0.9].
        Returns 0.5 (neutral) if fewer than 10 signals with outcomes.
        """
        outcomes = self._read_strategy_outcomes(strategy_name)
        accuracy = directional_accuracy(outcomes)
        if accuracy.actionable_count < 10 or accuracy.rate is None:
            return 0.5  # neutral until proven
        hit_rate = accuracy.rate

        # Linear map: 30% hit rate → 0.2 confidence, 70% → 0.9
        return max(0.2, min(0.9, (hit_rate - 0.3) / 0.4 * 0.7 + 0.2))

    def _read_strategy_outcomes(self, strategy_name: str) -> tuple[OutcomeRecord, ...]:
        rows = tuple(self._outcome_reader(strategy_name))
        if any(row.strategy != strategy_name for row in rows):
            raise ValueError(f"outcome strategy does not match {strategy_name!r}")
        return tuple(
            row for row in rows if row.holding_sessions == _DIAGNOSTIC_HOLDING_SESSIONS
        )

    # ------------------------------------------------------------------
    # Regime model helpers
    # ------------------------------------------------------------------

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

    def _should_trigger_learning_loop(
        self,
        outcomes_by_strategy: Mapping[str, tuple[OutcomeRecord, ...]] | None = None,
    ) -> bool:
        """Check whether governed outcome evidence is ready for diagnostics."""
        pt_config = self.ar_config.get("paper_trade", {})
        ll_state = self.state.load_learning_loop_state()
        outcomes = outcomes_by_strategy or {
            strategy.name: self._read_strategy_outcomes(strategy.name)
            for strategy in self.paper_trade_strategies
        }

        # Calendar check
        last_run = ll_state.get("last_run")
        calendar_days = pt_config.get("learning_loop_calendar_days", 30)
        if last_run:
            last_dt = datetime.fromisoformat(last_run)
            if (datetime.now() - last_dt).days >= calendar_days:
                return True
        else:
            # Never run before — trigger if any governed outcome is actionable.
            if any(
                directional_accuracy(rows).actionable_count
                for rows in outcomes.values()
            ):
                return True

        # Trade count check
        min_strategies = pt_config.get("learning_loop_min_strategies", 5)
        min_trades = pt_config.get("min_trades_for_evaluation", 20)
        qualifying = 0
        for s in self.paper_trade_strategies:
            if (
                directional_accuracy(outcomes.get(s.name, ())).actionable_count
                >= min_trades
            ):
                qualifying += 1

        return qualifying >= min_strategies

    # ------------------------------------------------------------------
    # Price helpers
    # ------------------------------------------------------------------

    def _fetch_missing_prices(
        self,
        tickers: list[str],
        start_date: str,
        end_date: str,
    ) -> None:
        """Fetch prices for tickers not already in cache."""
        from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource

        source = self.registry.get("yfinance")
        if not isinstance(source, YFinanceSource):
            return

        logger.info("Fetching prices for %d signal tickers: %s", len(tickers), tickers)
        extra_df = source.fetch_prices(tickers, start_date, end_date)
        if not extra_df.empty and isinstance(extra_df.columns, pd.MultiIndex):
            for ticker in tickers:
                try:
                    ticker_df = extra_df.xs(ticker, level=1, axis=1)
                    if not ticker_df.empty:
                        self._price_cache[ticker] = ticker_df
                except (KeyError, ValueError):
                    pass
        elif not extra_df.empty and len(tickers) == 1:
            self._price_cache[tickers[0]] = extra_df

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch_all_data(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Fetch all data needed by active strategies.

        Returns nested dict: {source_name: {data_type: data}}.
        """
        import time
        from tradingagents.strategies.data_sources.fetch_errors import source_fetch_error
        from tradingagents.strategies.data_sources.request_policy import provider_budget
        from tradingagents.strategies.orchestration.source_inputs import (
            SourceInputError, SourceInputStore, cache_identity,
            source_configuration_fingerprint, registered_source_fingerprint,
        )

        acquisition_start = time.monotonic()
        acquisition_cutoff = datetime.now(timezone.utc)
        fetch_timeout_s = max(0.0, _fetch_timeout_s())
        acquisition_deadline = acquisition_start + fetch_timeout_s
        cache_dir = self.ar_config.get("source_cache_dir") or os.environ.get("EVENTEDGE_SOURCE_CACHE_DIR")
        cache_store = None
        config_fingerprint = ""
        if cache_dir:
            cache_store = SourceInputStore(
                cache_dir, ttl_s=self.ar_config.get("source_cache_ttl_s", 300)
            )
            config_fingerprint = source_configuration_fingerprint(self.config, exclude_horizon=True)

        def acquire(name, fetcher, args):
            diagnostics = []
            if time.monotonic() >= acquisition_deadline:
                return {"error": "source acquisition deadline exhausted",
                        "_coverage": {"status": "failed", "reason_code": "deadline_exhausted"}}
            try:
                with provider_budget(name, acquisition_deadline, diagnostics=diagnostics):
                    if name == "finnhub":
                        args = (args[0], min(args[1], max(0.0, acquisition_deadline - time.monotonic())))
                    result = fetcher(*args)
                if not isinstance(result, Mapping):
                    raise ValueError("invalid provider payload")
                result = dict(result)
            except Exception as error:
                failure = source_fetch_error("source acquisition failed", error)
                result = dict(failure.partial_data)
                result["error"] = str(failure)
                result["_coverage"] = {
                    "status": "partial" if failure.partial_data else "failed",
                    "reason_code": failure.reason_code, "http_status": failure.http_status,
                    "attempts": failure.attempts,
                }
            if diagnostics:
                result["_request_diagnostics"] = diagnostics
            return result

        self._emit("phase", phase="data_fetch", status="starting")
        data: dict[str, Any] = {}
        universe_policy = self.ar_config.get("equity_universe_policy")
        if universe_policy:
            from tradingagents.strategies.data_sources.equity_universe import fetch_equity_universe, POLICY
            data["equity_universe"] = (acquire("alpaca", fetch_equity_universe, ())
                if universe_policy == POLICY else {"error": "unsupported equity universe policy"})

        # Collect which sources are needed
        needed_sources: set[str] = set()
        for s in self.paper_trade_strategies:
            needed_sources.update(s.data_sources)

        available = set(self.registry.available_sources())
        logger.info("Needed sources: %s, available: %s", needed_sources, available)
        for source in sorted(needed_sources - available):
            data[source] = {"error": "source unavailable or skipped"}
        if "openbb" in needed_sources and "openbb" in available:
            data["openbb"] = {"enrichment_only": True}

        # Fetch yfinance data (VIX + core market data for regime model)
        if "yfinance" in needed_sources and "yfinance" in available:
            data["yfinance"] = acquire("yfinance", self._fetch_yfinance_data, (start_date, end_date))

        # Fetch API-key sources in parallel (I/O bound, no dependency on each other)
        api_fetches: dict[str, tuple] = {}
        if "finnhub" in needed_sources and "finnhub" in available:
            finnhub_budget_cap_s = max(
                fetch_timeout_s - _FINNHUB_FETCH_SAFETY_MARGIN_S,
                0.0,
            )
            api_fetches["finnhub"] = (
                self._fetch_finnhub_data,
                (end_date, finnhub_budget_cap_s),
            )
        if "regulations" in needed_sources and "regulations" in available:
            api_fetches["regulations"] = (self._fetch_regulations_data, (end_date,))
        if "courtlistener" in needed_sources and "courtlistener" in available:
            api_fetches["courtlistener"] = (self._fetch_courtlistener_data, (end_date,))
        if "fred" in needed_sources and "fred" in available:
            api_fetches["fred"] = (self._fetch_fred_data, (start_date, end_date))
        if "congress" in needed_sources and "congress" in available:
            api_fetches["congress"] = (self._fetch_congress_data, (end_date,))
        if "noaa" in needed_sources and "noaa" in available:
            api_fetches["noaa"] = (self._fetch_noaa_data, (end_date,))
        if "usda" in needed_sources and "usda" in available:
            api_fetches["usda"] = (self._fetch_usda_data, (end_date,))
        if "drought_monitor" in needed_sources and "drought_monitor" in available:
            api_fetches["drought_monitor"] = (self._fetch_drought_data, (end_date,))

        # Also fetch EDGAR events for paper-trade strategies
        if "edgar" in needed_sources and "edgar" in available:
            api_fetches["edgar"] = (self._fetch_edgar_events,
                (end_date, data["equity_universe"]) if universe_policy else (end_date,))
        if "usaspending" in needed_sources and "usaspending" in available:
            api_fetches["usaspending"] = (self._fetch_usaspending_data, (end_date,))
        if "cftc" in needed_sources and "cftc" in available:
            api_fetches["cftc"] = (self._fetch_cftc_data, (end_date,))

        pending_fetches = {}
        cache_keys = {}
        for name, (fetcher, args) in api_fetches.items():
            source_fingerprint = config_fingerprint
            if name == "edgar" and universe_policy:
                snapshot = data["equity_universe"].get("snapshot", {})
                # A changed asset master may admit previously excluded filings.
                # Such a source set cannot reuse text selected by the old scope.
                source_fingerprint += ":" + snapshot.get("assets_sha256", "universe_unavailable")
            identity = cache_identity(
                name, start_date, end_date,
                registered_source_fingerprint(source_fingerprint, self.registry.get(name)),
            )
            cache_keys[name] = identity
            cached = cache_store.load_cached(identity, cutoff=acquisition_cutoff) if cache_store else None
            if cached is not None:
                data[name] = cached
            else:
                pending_fetches[name] = (acquire, (name, fetcher, args))
        if pending_fetches:
            fetched = _gather_with_timeout(
                pending_fetches, max(0.0, acquisition_deadline - time.monotonic())
            )
            data.update(fetched)
            if cache_store:
                for name, payload in fetched.items():
                    try:
                        cache_store.save_cached(cache_keys[name], payload)
                    except (OSError, SourceInputError):
                        logger.warning("Successful source %s could not be cached", name)

        self._emit("phase", phase="data_fetch", status="done")
        return data

    def _fetch_finnhub_data(
        self,
        trading_date: str,
        max_workflow_budget_s: float | None = None,
    ) -> dict[str, Any]:
        """Fetch all Finnhub subpaths under one cooperative scheduling deadline."""
        source = self.registry.get("finnhub")
        if source is None:
            return {}

        result: dict[str, Any] = {}
        from tradingagents.strategies.data_sources.fetch_errors import source_fetch_error
        import hashlib
        settings = getattr(self, "ar_config", {}).get("finnhub_acquisition", {})
        if not isinstance(settings, dict):
            raise ValueError("finnhub_acquisition must be a mapping")
        budgets = {key: settings.get(key, default) for key, default in (
            ("earnings_news_budget", 10), ("pqc_symbol_budget", 6),
            ("earnings_article_budget", 5))}
        if any(type(value) is not int or value < 0 for value in budgets.values()):
            raise ValueError("Finnhub acquisition budgets must be nonnegative integers")
        pqc_tickers = settings.get("pqc_universe", [
            "CRWD", "PANW", "ZS", "FTNT", "IBM", "CSCO", "MSFT", "IONQ", "RGTI", "COIN"])
        if (not isinstance(pqc_tickers, list)
                or any(not isinstance(symbol, str) or not symbol.strip() for symbol in pqc_tickers)):
            raise ValueError("Finnhub PQC universe must be a list of nonempty symbols")
        pqc_tickers = sorted({symbol.strip().upper() for symbol in pqc_tickers})

        def admit(records, budget, population, identity_fn, rank_fn):
            """Retain every fetched/query row before a deterministic request cap."""
            entries = []
            for record in records:
                raw = json.dumps(record, sort_keys=True, default=str)
                evidence_hash = hashlib.sha256(raw.encode()).hexdigest()
                identity = identity_fn(record)
                valid = identity is not None
                identity = identity or {"invalid_source_payload_hash": evidence_hash}
                encoded = json.dumps([population, identity], sort_keys=True, default=str)
                discovery_id = "finnhub-discovery:" + hashlib.sha256(encoded.encode()).hexdigest()[:24]
                row = {"discovery_id": discovery_id, "identity": identity, "evidence_hash": evidence_hash}
                entries.append(((0 if valid else 1, *(rank_fn(record) if valid else ()),
                                 discovery_id, evidence_hash), record, row, valid))
            selected, discovered, admitted, excluded, seen = [], [], [], [], set()
            for _, record, row, valid in sorted(entries, key=lambda item: item[0]):
                reason = ("invalid_source_identity" if not valid else
                          "duplicate_source_identity" if row["discovery_id"] in seen else
                          "acquisition_budget" if budget is not None and len(selected) >= budget else "admitted")
                seen.add(row["discovery_id"])
                row = dict(row, reason=reason)
                discovered.append(row)
                if reason == "admitted":
                    selected.append((record, row))
                    admitted.append(row)
                else:
                    excluded.append(row)
            return selected, {"version": 1, "population": population,
                "budget": budget, "discovered_count": len(discovered),
                "admitted_count": len(admitted), "excluded_count": len(excluded),
                "discovered": discovered, "admitted": admitted, "excluded": excluded}

        def earnings_identity(record):
            if not isinstance(record, dict) or not isinstance(record.get("symbol"), str) or not record["symbol"].strip():
                return None
            try:
                report_date = date.fromisoformat(record.get("date", "")).isoformat()
            except (TypeError, ValueError):
                return None
            return {"symbol": record["symbol"].strip().upper(), "date": report_date,
                    "year": record.get("year"), "quarter": record.get("quarter")}

        def article_identity(article):
            if not isinstance(article, dict):
                return None
            locator = article.get("id") or article.get("article_id") or article.get("url")
            return {"locator": str(locator)} if locator else {
                "source": article.get("source", ""), "headline": article.get("headline", ""),
                "published_at": article.get("published_at", article.get("datetime", ""))}

        def article_rank(article):
            stamp = article.get("published_at", article.get("datetime", ""))
            try:
                timestamp = float(stamp) if isinstance(stamp, (int, float)) else datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
                if not math.isfinite(timestamp):
                    timestamp = 0.
            except (TypeError, ValueError):
                timestamp = 0.
            return (-timestamp, json.dumps(article_identity(article), sort_keys=True, default=str))

        manifests: dict[str, Any] = {"earnings_articles": []}
        failures = []
        def acquire(operation, fn, *args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                error = source_fetch_error("Finnhub acquisition incomplete", exc)
                failures.append(f"{operation}: {error}")
                if operation == "peers":
                    return error.partial_data
                return error.partial_data.get("earnings" if operation == "earnings" else "news", [])
        deadline = source.new_workflow_deadline(
            max_budget_s=max_workflow_budget_s,
        )

        # Earnings calendar: who reported recently? (P1/P2)
        date_to = trading_date
        date_from = (
            datetime.strptime(trading_date, "%Y-%m-%d") - timedelta(days=7)
        ).strftime("%Y-%m-%d")
        earnings = acquire("earnings", source.fetch_recent_earnings,
            date_from,
            date_to,
            deadline=deadline,
        )
        earnings_selected, manifests["earnings_news"] = admit(
            earnings, budgets["earnings_news_budget"], "reported_earnings_calendar",
            earnings_identity, lambda record: (-date.fromisoformat(record["date"]).toordinal(),
                record["symbol"].strip().upper(), str(record.get("year", "")), str(record.get("quarter", ""))))
        manifests["earnings_news"]["policy"] = "report_date_desc_symbol_fiscal_identity_v1"
        if earnings_selected:
            # News around explicitly admitted earnings events proxies transcripts.
            transcripts = []
            for e, discovery in earnings_selected:
                symbol = e.get("symbol", "").strip().upper()
                edate = e.get("date", "")
                prior_failures = len(failures)
                news = acquire("earnings_news", source.fetch_earnings_news,
                    symbol,
                    edate,
                    deadline=deadline,
                )
                discovery.update(request_status="failed" if len(failures) > prior_failures else "succeeded", result_count=len(news))
                selected_articles, article_manifest = admit(news, budgets["earnings_article_budget"],
                    "earnings_news_articles", article_identity, article_rank)
                article_manifest.update(policy="publication_desc_source_identity_v1", parent_discovery_id=discovery["discovery_id"])
                manifests["earnings_articles"].append(article_manifest)
                if selected_articles:
                    # Build a pseudo-transcript from earnings news
                    news_text = "\n".join(
                        f"[{n.get('source', 'Unknown')}]: {n.get('headline', '')} — {n.get('summary', '')}"
                        for n, _row in selected_articles
                    )
                    publication_times = [
                        str(article["published_at"])
                        for article, _row in selected_articles
                        if article.get("published_at")
                    ]
                    transcripts.append(
                        {
                            "symbol": symbol,
                            "acquisition_discovery_id": discovery["discovery_id"],
                            "article_discovery_ids": [row["discovery_id"] for _article, row in selected_articles],
                            "year": e.get("year"),
                            "quarter": e.get("quarter"),
                            "transcript_text": news_text,
                            "eps_actual": e.get("epsActual"),
                            "eps_estimate": e.get("epsEstimate"),
                            "revenue_actual": e.get("revenueActual"),
                            "revenue_estimate": e.get("revenueEstimate"),
                            **(
                                {"published_at": max(publication_times)}
                                if publication_times
                                else {}
                            ),
                        }
                    )
            result["transcripts"] = transcripts
        logger.info(
            "Finnhub strategy fetch strategy=earnings_call candidate_count=%d "
            "qualifying_count=%d",
            len(earnings),
            len(result.get("transcripts", [])),
        )

        # Company news for supply chain disruption detection (P6)
        sc_symbols = ["AAPL", "TSLA", "NVDA", "AMZN", "BA", "CAT", "DE"]
        all_news = []
        for symbol in sc_symbols:
            news = acquire("company_news", source.fetch_company_news,
                symbol,
                date_from,
                date_to,
                deadline=deadline,
            )
            for article in news:
                article["symbol"] = symbol
            all_news.extend(news)
        if all_news:
            result["disruption_news"] = all_news

        # Supply chain / peer relationships
        chains: dict[str, list[str]] = {}
        peer_batches = acquire("peers", source.fetch_supply_chains,
            sc_symbols,
            deadline=deadline,
        )
        for symbol, peers in peer_batches.items():
            chains[symbol] = [p["ticker"] for p in peers]
        if chains:
            result["supply_chains"] = chains

        # PQC migration news for quantum_readiness strategy
        pqc_kw = ["quantum", "pqc", "post-quantum", "encryption", "cryptograph", "nist"]
        pqc_news = []
        queries = [{"symbol": symbol, "from": date_from, "to": date_to} for symbol in pqc_tickers]
        pqc_selected, manifests["pqc_news"] = admit(queries, budgets["pqc_symbol_budget"],
            "declared_pqc_query_universe", lambda query: query, lambda query: (query["symbol"],))
        manifests["pqc_news"].update(policy="symbol_ascending_v1", keyword_filter=pqc_kw)
        manifests["pqc_articles"] = []
        for query, discovery in pqc_selected:
            symbol = query["symbol"]
            prior_failures = len(failures)
            news = acquire("company_news", source.fetch_company_news,
                symbol,
                date_from,
                date_to,
                deadline=deadline,
            )
            discovery.update(request_status="failed" if len(failures) > prior_failures else "succeeded", result_count=len(news))
            observed, article_manifest = admit(news, None, "pqc_news_articles", article_identity, article_rank)
            article_manifest.update(policy="all_returned_articles_publication_identity_v1", parent_discovery_id=discovery["discovery_id"])
            manifests["pqc_articles"].append(article_manifest)
            for article, article_discovery in observed:
                text = (
                    article.get("headline", "") + " " + article.get("summary", "")
                ).lower()
                if any(kw in text for kw in pqc_kw):
                    pqc_news.append(dict(article, symbol=symbol,
                        acquisition_discovery_id=article_discovery["discovery_id"],
                        acquisition_query_id=discovery["discovery_id"]))
                else:
                    article_discovery["reason"] = "pqc_keyword_absent"
                    article_manifest["admitted"].remove(article_discovery)
                    article_manifest["excluded"].append(article_discovery)
            article_manifest["admitted_count"] = len(article_manifest["admitted"])
            article_manifest["excluded_count"] = len(article_manifest["excluded"])
            discovery["qualifying_count"] = article_manifest["admitted_count"]
        if pqc_news:
            result["pqc_news"] = pqc_news

        logger.info(
            "Finnhub fetch: %d earnings_candidates, %d earnings_qualifying, "
            "%d news, %d chains, %d pqc_news",
            len(earnings),
            len(result.get("transcripts", [])),
            len(result.get("disruption_news", [])),
            len(result.get("supply_chains", {})),
            len(result.get("pqc_news", [])),
        )
        if failures:
            result["error"] = "; ".join(failures)
        result["coverage"] = {"contract": "finnhub-acquisition-admission-v1",
            "status": "partial" if failures else "complete_within_declared_admission",
            "request_window": {"from": date_from, "to": date_to},
            "supply_chain_query_universe": sc_symbols,
            "acquisition_admission": manifests}
        return result

    def _fetch_regulations_data(self, trading_date: str | None = None) -> dict[str, Any]:
        """Fetch regulations.gov data for regulatory pipeline strategy."""
        from tradingagents.strategies.learning.event_monitor import EventMonitor

        monitor = EventMonitor(self.registry)
        monitor.as_of = trading_date
        result: dict[str, Any] = {}

        rules = monitor.poll_proposed_rules(
            agencies=["SEC", "EPA", "FDA", "FTC", "DOL", "CFPB"],
            days_back=14,
        )
        result["proposed_rules"] = list(rules)
        if hasattr(rules, "coverage"):
            result["coverage"] = rules.coverage

        logger.info("Regulations.gov fetch: %d proposed rules", len(rules))
        return result

    def _fetch_edgar_events(self, trading_date: str | None = None,
                            universe_data: dict | None = None) -> dict[str, Any]:
        """Preserve valid EDGAR categories while exposing every failed operation."""
        from tradingagents.strategies.learning.event_monitor import EventMonitor
        from tradingagents.strategies.data_sources.fetch_errors import source_fetch_error
        monitor = EventMonitor(self.registry)
        monitor.as_of = trading_date
        result, failures = {}, []
        if universe_data is not None:
            from tradingagents.strategies.data_sources.equity_universe import EquityUniverse
            if universe_data.get("error"):
                return {"error": "equity universe evidence unavailable"}
            source = self.registry.get("edgar")
            company_map = source.company_ticker_map()
            monitor.equity_universe = EquityUniverse(universe_data.get("snapshot"), company_map=company_map)
            result["company_tickers"] = company_map
        operations = {
            "filings": lambda: monitor.poll_edgar_filings(["10-K", "10-Q", "DEF 14A", "8-K"], days_back=14),
            "form4": lambda: monitor.poll_form4_filings(["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "JPM"], days_back=14),
            "activist_13d": lambda: monitor.poll_edgar_filings(["SCHEDULE 13D"], days_back=14),
            "passive_13g": lambda: monitor.poll_edgar_filings(["SCHEDULE 13G"], days_back=14),
            "pqc_filings": lambda: monitor.poll_keyword_filings(["8-K", "10-K", "10-Q"],
                ["post-quantum", "quantum-resistant", "quantum-safe", "cryptographic agility"], days_back=30),
        }
        for name, operation in operations.items():
            try:
                result[name] = operation()
                if hasattr(result[name], "coverage"):
                    result.setdefault("coverage", {})[name] = result[name].coverage
                    result[name] = dict(result[name]) if isinstance(result[name], dict) else list(result[name])
            except Exception as exc:
                error = source_fetch_error("EDGAR acquisition incomplete", exc)
                partial = error.partial_data.get(name)
                if partial is None and name in {"activist_13d", "passive_13g"}:
                    partial = error.partial_data.get("filings")
                if partial is not None:
                    result[name] = list(partial) if isinstance(partial, list) else partial
                    if hasattr(partial, "coverage"):
                        result.setdefault("coverage", {})[name] = partial.coverage
                failures.append(f"{name}: {error}")
        if failures:
            result["error"] = "; ".join(failures)
        return result

    def _fetch_courtlistener_data(self, trading_date: str | None = None) -> dict[str, Any]:
        """Fetch CourtListener data for litigation strategy."""
        from tradingagents.strategies.learning.event_monitor import EventMonitor
        from tradingagents.strategies.data_sources.fetch_errors import source_fetch_error

        monitor = EventMonitor(self.registry)
        result: dict[str, Any] = {}
        failures: list[str] = []

        monitor.as_of = trading_date
        # Search for securities-related cases
        for query in ["securities class action", "SEC enforcement", "antitrust"]:
            try:
                dockets = monitor.poll_court_dockets(query=query, days_back=14)
            except Exception as exc:
                safe_error = source_fetch_error("CourtListener docket fetch failed", exc)
                result.setdefault("dockets", []).extend(safe_error.partial_data.get("dockets", []))
                failures.append(f"{query.lower().replace(' ', '_')}: {safe_error}")
                logger.error("%s", safe_error)
                continue
            if hasattr(dockets, "coverage"):
                result.setdefault("coverage", {})[query] = dockets.coverage
            existing = result.get("dockets", [])
            existing.extend(dockets)
            result["dockets"] = existing

        if failures:
            result["error"] = "; ".join(failures)
        logger.info(
            "CourtListener fetch: %d dockets",
            len(result.get("dockets", [])),
        )
        return result

    def _fetch_fred_data(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Fetch FRED credit spreads and economic indicators."""
        from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError

        source = self.registry.get("fred")
        start_date = min(start_date, (pd.Timestamp(end_date) - pd.DateOffset(months=18)).date().isoformat())
        if source is None:
            return {}

        result: dict[str, Any] = {}
        failures: list[str] = []

        # Credit spreads for regime model
        try:
            spreads = source.fetch_credit_spreads(start_date, end_date)
            result.update(
                spreads
            )  # Keys are FRED series IDs (BAMLH0A0HYM2, BAMLC0A4CBBB)
        except SourceFetchError as exc:
            result.update(exc.partial_data)
            failures.append(str(exc))
        except Exception:
            logger.error("Failed to fetch FRED credit spreads")
            failures.append("FRED credit spreads [provider_error]")

        # Economic indicators for regime model
        try:
            indicators = source.fetch_economic_indicators(start_date, end_date)
            result.update(indicators)  # Keys are FRED series IDs (UNRATE, PAYEMS, etc.)
        except SourceFetchError as exc:
            result.update(exc.partial_data)
            failures.append(str(exc))
        except Exception:
            logger.error("Failed to fetch FRED economic indicators")
            failures.append("FRED economic indicators [provider_error]")

        if failures:
            result["error"] = "; ".join(failures)

        # Plain metadata survives the immutable bundle codec (Series.attrs does not).
        result["vintage"] = {"as_of": end_date, "realtime_start": end_date, "realtime_end": end_date}
        # Map friendly names for strategies that use them
        from tradingagents.strategies.data_sources.fred_source import SERIES_MAP

        for friendly_name, series_id in SERIES_MAP.items():
            if series_id in result:
                result[friendly_name] = result[series_id]

        logger.info("FRED fetch: %d series loaded", len(result))
        return result

    def _fetch_congress_data(self, trading_date: str) -> dict[str, Any]:
        """Fetch recent congressional stock trades."""
        source = self.registry.get("congress")
        if source is None:
            return {}

        result: dict[str, Any] = {}
        try:
            trades = source.get_recent_trades(days_back=30, as_of=trading_date)
            result["recent_trades"] = list(trades)
            if hasattr(trades, "coverage"):
                result["coverage"] = trades.coverage
            logger.info("Congress fetch: %d recent trades", len(trades))
        except Exception as exc:
            from tradingagents.strategies.data_sources.fetch_errors import source_fetch_error
            error = source_fetch_error("Source acquisition failed", exc)
            result.update(error.partial_data)
            result["error"] = str(error)

        return result

    def _fetch_usaspending_data(self, trading_date: str) -> dict[str, Any]:
        """Fetch recent large federal contract awards."""
        from tradingagents.strategies.data_sources.fetch_errors import source_fetch_error

        source = self.registry.get("usaspending")
        if source is None:
            return {}

        try:
            contracts = source.get_recent_large_contracts(
                min_amount=50_000_000,
                days_back=30,
                as_of=trading_date,
            )
            result = {"contracts": list(contracts)}
            if hasattr(contracts, "coverage"):
                result["coverage"] = contracts.coverage
            logger.info("USASpending fetch: %d large contracts", len(contracts))
            return {"data": result, **({"coverage": contracts.coverage} if hasattr(contracts, "coverage") else {})}
        except Exception as exc:
            safe_error = source_fetch_error("USASpending contract fetch failed", exc)
            logger.error("%s", safe_error)
            return {**safe_error.partial_data, "error": str(safe_error)}

    def _fetch_noaa_data(self, trading_date: str) -> dict[str, Any]:
        """Fetch NOAA weather anomaly summary for Corn Belt ag regions."""
        source = self.registry.get("noaa")
        if source is None:
            return {}

        try:
            return source.fetch_ag_weather_summary(trading_date, lookback_days=30)
        except Exception as exc:
            from tradingagents.strategies.data_sources.fetch_errors import source_fetch_error
            error = source_fetch_error("Source acquisition failed", exc)
            return {**error.partial_data, "error": str(error)}

    def _fetch_usda_data(self, trading_date: str) -> dict[str, Any]:
        """Preserve crops fetched before any required crop failure."""
        from tradingagents.strategies.data_sources.fetch_errors import source_fetch_error
        from tradingagents.strategies.data_sources.usda_source import condition_scope
        source = self.registry.get("usda")
        if source is None:
            return {}
        crop_progress, failures, crop_coverage = {}, [], {}
        for commodity in ("CORN", "SOYBEANS", "WHEAT"):
            scope = condition_scope(commodity, trading_date)
            year = scope["reporting_year"]
            try:
                observations = source.fetch_crop_progress(commodity, year, as_of=trading_date)
                crop_progress[commodity] = list(observations)
                crop_coverage[commodity] = getattr(observations, "coverage", {**scope, "complete": False, "reason": "missing_survey_coverage"})
                if crop_coverage[commodity].get("complete") is not True:
                    failures.append(f"{commodity}: incomplete declared survey coverage")
            except Exception as exc:
                error = source_fetch_error("USDA crop acquisition incomplete", exc)
                crop_progress.update(error.partial_data.get("crop_progress", {}))
                crop_coverage[commodity] = error.partial_data.get("coverage", {**scope, "complete": False})
                failures.append(f"{commodity}: {error}")
        acquisition_times = [str(row["available_at"]) for weeks in crop_progress.values() for row in weeks if isinstance(row, dict) and row.get("available_at")]
        result = {"crop_progress": crop_progress, "coverage": {
            "complete": not failures and all(row.get("complete") is True for row in crop_coverage.values()),
            "crops": crop_coverage,
        }}
        if acquisition_times:
            acquired = max(acquisition_times)
            result.update(available_at=acquired, acquired_at=acquired)
        if failures:
            result["error"] = "; ".join(failures)
        return result

    def _fetch_drought_data(self, trading_date: str) -> dict[str, Any]:
        """Fetch Drought Monitor severity and composite score."""
        source = self.registry.get("drought_monitor")
        if source is None:
            return {}

        try:
            end = trading_date
            start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=7)).strftime(
                "%Y-%m-%d"
            )
            severity = source.fetch_drought_severity(start=start, end=end)
            composite = source.fetch_composite_score(date=trading_date)
            result = {"composite_score": composite, "states": severity}
            acquisition_times = [str(row["available_at"]) for row in severity.values() if isinstance(row, dict) and row.get("available_at")]
            if acquisition_times:
                acquired = max(acquisition_times)
                result.update(available_at=acquired, acquired_at=acquired)
            return result
        except Exception as exc:
            from tradingagents.strategies.data_sources.fetch_errors import source_fetch_error
            error = source_fetch_error("Source acquisition failed", exc)
            return {**error.partial_data, "error": str(error)}

    def _fetch_cftc_data(self, trading_date: str | None = None) -> dict[str, Any]:
        """Fetch CFTC COT positioning data for commodity strategy."""
        source = self.registry.get("cftc")
        if source is None:
            return {}

        return source.fetch(
            {
                "method": "cot_positioning",
                "commodities": ["gold", "silver", "crude_oil", "nat_gas", "copper"],
                "lookback_weeks": 52,
                "as_of": trading_date,
            }
        )

    def _fetch_yfinance_data(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Fetch all yfinance data needed by strategies."""
        from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource

        source = self.registry.get("yfinance")
        if not isinstance(source, YFinanceSource):
            logger.warning("yfinance source not available")
            return {}

        result: dict[str, Any] = {}
        from tradingagents.strategies.data_sources.fetch_errors import source_fetch_error
        failures = []

        # Core market tickers for regime model and general context
        # Includes ag ETFs for weather_ag strategy
        core_tickers = [
            # Market + regime model
            "SPY",
            "SHY",
            "TLT",
            # Ag ETFs for weather_ag
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
            # Regional ETFs for state_economics
            "KRE",
            "IWN",
            "XRT",
            "IYR",
            "XHB",
            "ITB",
            "VNQ",
            "SOXX",
            "XLI",
            "XLRE",
            # Defense contractors for govt_contracts (momentum fallback)
            "LMT",
            "RTX",
            "NOC",
            "GD",
            "BA",
            "LHX",
            "LDOS",
            "SAIC",
            "BAH",
            "PLTR",
            "KTOS",
            "CACI",
            "HEI",
            "TDG",
        ]

        logger.info("Fetching prices for %d core tickers", len(core_tickers))
        try:
            prices_df = source.fetch_prices(core_tickers, start_date, end_date)
        except Exception as exc:
            error = source_fetch_error("Yahoo research history failed", exc)
            failures.append(str(error))
            prices_df = error.partial_data.get("prices", pd.DataFrame())

        # Split into per-ticker DataFrames
        prices: dict[str, pd.DataFrame] = {}
        if not prices_df.empty and isinstance(prices_df.columns, pd.MultiIndex):
            for ticker in core_tickers:
                try:
                    ticker_df = prices_df.xs(ticker, level=1, axis=1)
                    if not ticker_df.empty:
                        prices[ticker] = ticker_df
                except (KeyError, ValueError):
                    logger.debug("No data for %s in batch download", ticker)
        elif not prices_df.empty and len(core_tickers) == 1:
            prices[core_tickers[0]] = prices_df

        result["prices"] = prices
        self._price_cache.update(prices)

        # Fetch VIX for regime model
        try:
            vix_df = source.fetch_vix(start_date, end_date)
        except Exception as exc:
            error = source_fetch_error("Yahoo VIX history failed", exc)
            failures.append(str(error))
            vix_df = pd.DataFrame()
        if not vix_df.empty:
            result["vix"] = vix_df

        if failures:
            result["error"] = "; ".join(failures)
        return result

    # ------------------------------------------------------------------
    # LLM enrichment
    # ------------------------------------------------------------------

    def _enrich_with_llm(
        self,
        candidates: list[Candidate],
        strategy_name: str,
        regime_context: dict | None = None,
    ) -> list[Candidate]:
        """Run LLM analysis on candidates that have needs_llm_analysis=True."""
        from tradingagents.strategies.runtime_deadline import (
            DEFAULT_MODEL_BUDGET_S, current_model_deadline, model_budget,
        )
        from tradingagents.strategies.candidate_parallel import analyze_candidates
        if current_model_deadline() is None:
            import time
            with model_budget(time.monotonic() + DEFAULT_MODEL_BUDGET_S):
                return self._enrich_with_llm(candidates, strategy_name, regime_context)
        enriched = list(candidates)
        required = [c for c in enriched if c.metadata.get("needs_llm_analysis")]
        try:
            outcomes = analyze_candidates(required, analyzer=self._analyzer,
                analyze_one=lambda c, worker: self._analyze_candidate(c, strategy_name, regime_context, worker),
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

    def _analyze_candidate(self, c: Candidate, strategy_name: str, regime_context: dict | None, analyzer) -> None:
        """Validate one assessment using this worker's independent analyzer state."""
        from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded, model_timeout
        from tradingagents.strategies.candidate_response_reuse import begin_candidate_response, commit_candidate_response, end_candidate_response
        analysis_type = c.metadata.get("analysis_type", "")
        llm_result = {}
        optional = analysis_type in {"insider_activity", "commodity_macro", "ag_weather"} and c.metadata.get("deterministic_evidence_complete") is True

        reuse_token = begin_candidate_response(strategy_name, analysis_type, c.metadata.get("discovery_id", ""), optional)

        try:
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
            if fields and not any(isinstance(c.metadata.get(key), str) and c.metadata[key].strip() for key in fields):
                raise ValueError("missing_source_text")
            if analysis_type == "earnings_call":
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
                if cluster_type == "buy_cluster":
                    llm_result = analyzer.analyze_insider_context(
                        c.metadata.get("filings", []),
                        c.ticker,
                        regime_context=regime_context,
                    )
                elif cluster_type == "sell_pattern":
                    llm_result = analyzer.analyze_10b5_1_plan(
                        c.metadata.get("filings", []),
                        c.ticker,
                        regime_context=regime_context,
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
            provenance = getattr(analyzer, "last_call_provenance", None)
            if isinstance(provenance, dict) and provenance:
                c.metadata["model_provenance"] = dict(provenance)
            if getattr(analyzer, "last_call_failure", "") == "model_deadline_exhausted":
                raise ModelDeadlineExceeded("model_deadline_exhausted")
            if not isinstance(llm_result, dict) or not llm_result:
                raise ValueError("analysis_unavailable")
            llm_result = dict(llm_result)
            if llm_result.get("direction") not in {"long", "short", "neutral"}:
                raise ValueError("invalid_direction")
            score_key = "conviction" if "conviction" in llm_result else "score"
            value = llm_result.get(score_key)
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError("invalid_score")
            score = float(value)
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("invalid_score")
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
            if not ticker:
                ticker = llm_result.get("defendant_ticker") or next(iter(llm_result.get("affected_tickers", [])), "")
                ticker = ticker.strip().upper()
                edgar = self.registry.get("edgar")
                if not ticker or edgar is None or not edgar.validate_ticker(ticker):
                    raise ValueError("unresolved_issuer")
            # Commit fields only after full schema and entity validation.
            reuse = commit_candidate_response()
            if reuse is not None:
                analyzer.last_call_provenance["request_reuse"] = reuse
                c.metadata["model_provenance"] = dict(analyzer.last_call_provenance)
            c.ticker, c.direction, c.score = ticker, llm_result["direction"], score
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
            end_candidate_response(reuse_token)
