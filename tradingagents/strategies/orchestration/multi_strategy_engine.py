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
import statistics
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from collections.abc import Iterable
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
from tradingagents.strategies.metrics.models import OutcomeRecord
from tradingagents.strategies.metrics.outcomes import directional_accuracy
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

logger = logging.getLogger(__name__)

_FINNHUB_FETCH_SAFETY_MARGIN_S = 30.0


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

    Every source defaults to ``{}`` so any that error or do not return within
    ``timeout_s`` are recorded as empty rather than delaying the caller. Python
    cannot safely stop a running thread, so this is not request cancellation.
    On timeout the pool is shut down with ``wait=False`` so its teardown does
    not re-block the caller; sources need cooperative scheduling deadlines to
    avoid issuing follow-on requests after the caller has moved on.
    """
    from concurrent.futures import (
        ThreadPoolExecutor,
        TimeoutError as FuturesTimeout,
        as_completed,
    )

    results: dict[str, Any] = {name: {} for name in api_fetches}
    if not api_fetches:
        return results

    pool = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {
            pool.submit(fn, *args): name for name, (fn, args) in api_fetches.items()
        }
        try:
            for future in as_completed(futures, timeout=timeout_s):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception:
                    logger.error("Failed to fetch %s", name, exc_info=True)
                    results[name] = {}
        except FuturesTimeout:
            stuck = sorted(futures[f] for f in futures if not f.done())
            logger.error(
                "Data fetch exceeded %.0fs; abandoning slow sources: %s",
                timeout_s,
                stuck,
            )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    return results


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

    def screen_and_enrich(
        self,
        trading_date: str,
        data: dict,
        horizon: str = "30d",
    ) -> tuple[list[dict], dict]:
        """Run strategy screening and LLM enrichment (steps 1-2).

        Returns enriched, deduped signals and regime model. These can be
        shared across cohorts so LLM non-determinism doesn't confound results.
        """
        regime_model = self._build_regime_model(data)
        self.state.save_regime_snapshot(regime_model)

        all_signals: list[dict] = []
        for strategy in self.paper_trade_strategies:
            self._emit("strategy_start", name=strategy.name, track="paper_trade")
            params = strategy.get_default_params(horizon=horizon)
            candidates = strategy.screen(data, trading_date, params)
            if candidates:
                candidates = self._enrich_with_llm(
                    candidates, strategy.name, regime_context=regime_model
                )
            for c in candidates:
                all_signals.append(
                    {
                        "ticker": c.ticker,
                        "direction": c.direction,
                        "score": c.score,
                        "strategy": strategy.name,
                        "metadata": c.metadata,
                    }
                )
            self._emit("strategy_done", name=strategy.name, num_signals=len(candidates))

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

        return deduped_signals, regime_model

    def screen_and_stage(
        self,
        trading_date: str,
        data: dict,
        shared_signals: list[dict],
        shared_regime: dict,
        enrichment: dict,
        size_profile: Any,
        marked_account: Any,
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
        from tradingagents.strategies.trading.execution_bridge import ExecutionBridge
        from tradingagents.strategies.trading.portfolio_committee import (
            PortfolioCommittee,
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
        if self.ledger.staging_completed(session, epoch_id, policy_id):
            records = self.ledger.read_signals(
                session, session, epoch_id=epoch_id, policy_id=policy_id
            )
            return {
                "signals": [record.__dict__ for record in records],
                "recommendations": [],
                "intents_staged": [],
                "cutoff_late": [],
                "regime": shared_regime,
                "account": marked_account.__dict__,
                "replayed": True,
            }

        raw_bars = data.get("_execution_reference_bars", {})
        if not isinstance(raw_bars, dict):
            raise ValueError("_execution_reference_bars must be a mapping")

        records: list[SignalRecord] = []
        timely: list[tuple[dict, SignalRecord]] = []
        late_ids: list[str] = []
        seen_signal_ids: set[str] = set()
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
            if signal_id in seen_signal_ids:
                continue
            seen_signal_ids.add(signal_id)
            existing_observation = self.ledger.signal_observation(signal_id)
            if existing_observation is not None:
                record, candidate_context, journal_payload = existing_observation
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
                    timely.append((enriched, record))
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
                timely.append((enriched, record))
                status = "timely"
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
        committee = PortfolioCommittee(self.config, size_profile=size_profile)
        recommendations = committee.synthesize(
            signals=committee_signals,
            regime_context=shared_regime or {},
            strategy_confidence=strategy_confidence,
            current_positions=self.ledger.open_positions(),
            total_capital=float(marked_account.net_equity),
            enrichment=enrichment or {},
        )
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
                intent = bridge.stage_intent(
                    recommendation,
                    contributor_records,
                    self.ledger.account_state(),
                    cutoff,
                    eligible_session,
                )
                staged_ids.append(intent.intent_id)

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
        """Phase 2 learning loop: Evaluate strategy performance and optimize prompts."""
        if not self._should_trigger_learning_loop():
            return {"triggered": False, "strategies_evaluated": 0}

        scores: dict[str, float] = {}
        trade_counts: dict[str, int] = {}

        for strategy in self.paper_trade_strategies:
            trades = self.state.load_paper_trades(
                strategy=strategy.name, status="closed"
            )
            trade_counts[strategy.name] = len(trades)

            if not trades:
                scores[strategy.name] = 0.0
                continue

            # Compute Sharpe from PnL (with fallback for trades closed before pnl field existed)
            pnls = []
            for t in trades:
                p = t.get("pnl")
                if p is not None:
                    pnls.append(p)
                else:
                    entry = t.get("entry_price", 0)
                    exit_ = t.get("exit_price", 0)
                    if entry > 0 and exit_ > 0:
                        raw = (exit_ - entry) / entry
                        if t.get("direction") == "short":
                            raw = -raw
                        pnls.append(raw * entry * t.get("shares", 1))
            if len(pnls) > 1:
                mean_pnl = statistics.mean(pnls)
                std_pnl = statistics.stdev(pnls)
                scores[strategy.name] = mean_pnl / std_pnl if std_pnl > 0 else 0.0
            elif pnls:
                scores[strategy.name] = pnls[0]
            else:
                scores[strategy.name] = 0.0

        # ------------------------------------------------------------------
        # Prompt optimization (Atlas-GIC inspired)
        # ------------------------------------------------------------------
        prompt_optimization_result: dict[str, Any] = {}
        if self._analyzer:
            from tradingagents.strategies.learning.prompt_optimizer import (
                PromptOptimizer,
            )

            state_dir = self.ar_config.get("state_dir", "data/state")
            optimizer = PromptOptimizer(state_dir, self._analyzer)
            outcomes_by_strategy = {
                strategy.name: tuple(self._outcome_reader(strategy.name))
                for strategy in self.paper_trade_strategies
            }
            all_outcomes = tuple(
                outcome for rows in outcomes_by_strategy.values() for outcome in rows
            )

            # Check active trial first
            trial_id, trial = optimizer.get_active_trial()
            if trial_id:
                decision = optimizer.check_trial(trial_id, all_outcomes)
                if decision in ("keep", "revert"):
                    optimizer.commit_or_revert(trial_id, decision)
                    prompt_optimization_result["trial_completed"] = {
                        "trial_id": trial_id,
                        "decision": decision,
                    }
                else:
                    prompt_optimization_result["trial_ongoing"] = trial_id
            else:
                # No active trial — evaluate and potentially start one
                prompt_scores = optimizer.evaluate_prompts(outcomes_by_strategy)
                worst = optimizer.identify_worst_prompt(prompt_scores)
                if worst:
                    current_prompt = self._analyzer.get_prompt(worst)
                    failures = self._journal.get_high_conviction_failures(
                        worst, limit=10
                    )
                    if failures:
                        new_prompt = optimizer.propose_modification(
                            worst, current_prompt, failures
                        )
                        if new_prompt != current_prompt:
                            new_trial_id = optimizer.start_trial(worst, new_prompt)
                            prompt_optimization_result["trial_started"] = {
                                "trial_id": new_trial_id,
                                "strategy": worst,
                            }
                prompt_optimization_result["prompt_scores"] = {
                    k: {"hit_rate": v["hit_rate"], "n_signals": v["n_signals"]}
                    for k, v in prompt_scores.items()
                }

        # Update learning loop state
        ll_state = self.state.load_learning_loop_state()
        ll_state["last_run"] = datetime.now().isoformat()
        ll_state["strategies_evaluated"] = list(scores.keys())
        self.state.save_learning_loop_state(ll_state)

        return {
            "triggered": True,
            "strategies_evaluated": len(scores),
            "scores": scores,
            "trade_counts": trade_counts,
            "prompt_optimization": prompt_optimization_result,
        }

    # ------------------------------------------------------------------
    # Strategy confidence
    # ------------------------------------------------------------------

    def _compute_strategy_confidence(self, strategy_name: str) -> float:
        """Compute confidence from signal journal hit rates.

        Maps hit_rate [0.3, 0.7] → confidence [0.2, 0.9].
        Returns 0.5 (neutral) if fewer than 10 signals with outcomes.
        """
        outcomes = tuple(self._outcome_reader(strategy_name))
        accuracy = directional_accuracy(outcomes)
        if accuracy.actionable_count < 10 or accuracy.rate is None:
            return 0.5  # neutral until proven
        hit_rate = accuracy.rate

        # Linear map: 30% hit rate → 0.2 confidence, 70% → 0.9
        return max(0.2, min(0.9, (hit_rate - 0.3) / 0.4 * 0.7 + 0.2))

    # ------------------------------------------------------------------
    # Regime model helpers
    # ------------------------------------------------------------------

    def _build_regime_model(self, data: dict) -> dict:
        """Build regime model from available data (VIX, credit spreads, yield curve)."""
        vix_data = data.get("yfinance", {}).get("vix")
        vix_level = 0.0
        if vix_data is not None and not vix_data.empty:
            vix_level = float(vix_data["Close"].iloc[-1])

        fred = data.get("fred", {})
        hy_spread = fred.get("hy_spread")
        credit_bps = 0.0
        if hy_spread is not None and hasattr(hy_spread, "iloc") and len(hy_spread) > 0:
            credit_bps = (
                float(hy_spread.iloc[-1]) * 100
                if not pd.isna(hy_spread.iloc[-1])
                else 0.0
            )

        yield_curve = fred.get("yield_curve")
        yc_slope = 0.0
        if (
            yield_curve is not None
            and hasattr(yield_curve, "iloc")
            and len(yield_curve) > 0
        ):
            yc_slope = (
                float(yield_curve.iloc[-1])
                if not pd.isna(yield_curve.iloc[-1])
                else 0.0
            )

        overall = self._classify_regime(vix_level, credit_bps, yc_slope)
        stressed_vix = self.ar_config.get("risk_discipline", {}).get(
            "regime_vix_stressed", 25.0
        )

        return {
            "vix_level": vix_level,
            "vix_regime": "crisis"
            if vix_level > 35
            else "elevated"
            if vix_level > stressed_vix
            else "normal"
            if vix_level > 15
            else "low",
            "credit_spread_bps": credit_bps,
            "credit_regime": "crisis"
            if credit_bps > 600
            else "stressed"
            if credit_bps > 400
            else "normal",
            "yield_curve_slope": yc_slope,
            "yield_regime": "inverted"
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

    def _should_trigger_learning_loop(self) -> bool:
        """Check if learning loop should fire."""
        pt_config = self.ar_config.get("paper_trade", {})
        ll_state = self.state.load_learning_loop_state()

        # Calendar check
        last_run = ll_state.get("last_run")
        calendar_days = pt_config.get("learning_loop_calendar_days", 30)
        if last_run:
            last_dt = datetime.fromisoformat(last_run)
            if (datetime.now() - last_dt).days >= calendar_days:
                return True
        else:
            # Never run before — trigger if we have any completed trades
            trades = self.state.load_paper_trades(status="closed")
            if trades:
                return True

        # Trade count check
        min_strategies = pt_config.get("learning_loop_min_strategies", 5)
        min_trades = pt_config.get("min_trades_for_evaluation", 20)
        qualifying = 0
        for s in self.paper_trade_strategies:
            trades = self.state.load_paper_trades(strategy=s.name, status="closed")
            if len(trades) >= min_trades:
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
        self._emit("phase", phase="data_fetch", status="starting")
        data: dict[str, Any] = {}

        # Collect which sources are needed
        needed_sources: set[str] = set()
        for s in self.paper_trade_strategies:
            needed_sources.update(s.data_sources)

        available = set(self.registry.available_sources())
        logger.info("Needed sources: %s, available: %s", needed_sources, available)

        # Fetch yfinance data (VIX + core market data for regime model)
        if "yfinance" in needed_sources and "yfinance" in available:
            data["yfinance"] = self._fetch_yfinance_data(start_date, end_date)

        # Fetch API-key sources in parallel (I/O bound, no dependency on each other)
        fetch_timeout_s = _fetch_timeout_s()
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
            api_fetches["regulations"] = (self._fetch_regulations_data, ())
        if "courtlistener" in needed_sources and "courtlistener" in available:
            api_fetches["courtlistener"] = (self._fetch_courtlistener_data, ())
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
            api_fetches["edgar"] = (self._fetch_edgar_events, ())
        if "usaspending" in needed_sources and "usaspending" in available:
            api_fetches["usaspending"] = (self._fetch_usaspending_data, (end_date,))
        if "cftc" in needed_sources and "cftc" in available:
            api_fetches["cftc"] = (self._fetch_cftc_data, ())

        if api_fetches:
            data.update(_gather_with_timeout(api_fetches, fetch_timeout_s))

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
        deadline = source.new_workflow_deadline(
            max_budget_s=max_workflow_budget_s,
        )

        # Earnings calendar: who reported recently? (P1/P2)
        date_to = trading_date
        date_from = (
            datetime.strptime(trading_date, "%Y-%m-%d") - timedelta(days=7)
        ).strftime("%Y-%m-%d")
        earnings = source.fetch_recent_earnings(
            date_from,
            date_to,
            deadline=deadline,
        )
        if earnings:
            # Collect news around earnings dates for top reporters (proxy for transcripts)
            transcripts = []
            for e in earnings[:10]:  # Top 10 to limit API calls
                symbol = e.get("symbol", "")
                edate = e.get("date", "")
                if not symbol or not edate:
                    continue
                news = source.fetch_earnings_news(
                    symbol,
                    edate,
                    deadline=deadline,
                )
                if news:
                    # Build a pseudo-transcript from earnings news
                    news_text = "\n".join(
                        f"[{n.get('source', 'Unknown')}]: {n.get('headline', '')} — {n.get('summary', '')}"
                        for n in news[:5]
                    )
                    publication_times = [
                        str(article["published_at"])
                        for article in news[:5]
                        if article.get("published_at")
                    ]
                    transcripts.append(
                        {
                            "symbol": symbol,
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
            news = source.fetch_company_news(
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
        peer_batches = source.fetch_supply_chains(
            sc_symbols,
            deadline=deadline,
        )
        for symbol, peers in peer_batches.items():
            chains[symbol] = [p["ticker"] for p in peers]
        if chains:
            result["supply_chains"] = chains

        # PQC migration news for quantum_readiness strategy
        pqc_tickers = [
            "CRWD",
            "PANW",
            "ZS",
            "FTNT",
            "IBM",
            "CSCO",
            "MSFT",
            "IONQ",
            "RGTI",
            "COIN",
        ]
        pqc_kw = ["quantum", "pqc", "post-quantum", "encryption", "cryptograph", "nist"]
        pqc_news = []
        for symbol in pqc_tickers[:6]:  # Rate limit: 6 tickers max
            news = source.fetch_company_news(
                symbol,
                date_from,
                date_to,
                deadline=deadline,
            )
            for article in news:
                text = (
                    article.get("headline", "") + " " + article.get("summary", "")
                ).lower()
                if any(kw in text for kw in pqc_kw):
                    article["symbol"] = symbol
                    pqc_news.append(article)
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
        return result

    def _fetch_regulations_data(self) -> dict[str, Any]:
        """Fetch regulations.gov data for regulatory pipeline strategy."""
        from tradingagents.strategies.learning.event_monitor import EventMonitor

        monitor = EventMonitor(self.registry)
        result: dict[str, Any] = {}

        rules = monitor.poll_proposed_rules(
            agencies=["SEC", "EPA", "FDA", "FTC", "DOL", "CFPB"],
            days_back=14,
        )
        if rules:
            result["proposed_rules"] = rules

        logger.info("Regulations.gov fetch: %d proposed rules", len(rules))
        return result

    def _fetch_edgar_events(self) -> dict[str, Any]:
        """Fetch EDGAR events for paper-trade strategies (P3, P4, P7, P8, P9)."""
        from tradingagents.strategies.learning.event_monitor import EventMonitor

        monitor = EventMonitor(self.registry)
        result: dict[str, Any] = {}

        # Filings for P3 (filing changes), P9 (exec comp)
        filings = monitor.poll_edgar_filings(
            form_types=["10-K", "10-Q", "DEF 14A", "8-K"],
            days_back=14,
        )
        if filings:
            result["filings"] = filings

        # Form 4 for P4 (insider combo), P7 (10b5-1)
        # Poll for major tickers
        tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "JPM"]
        form4 = monitor.poll_form4_filings(tickers, days_back=14)
        if form4:
            result["form4"] = form4

        # 13D for B6 (activist)
        filings_13d = monitor.poll_13d_filings(days_back=14)
        if filings_13d:
            result["activist_13d"] = filings_13d

        # PQC keyword filings for quantum_readiness strategy
        pqc_filings = monitor.poll_keyword_filings(
            form_types=["8-K", "10-K", "10-Q"],
            keywords=[
                "post-quantum",
                "quantum-resistant",
                "quantum-safe",
                "cryptographic agility",
            ],
            days_back=30,
        )
        if pqc_filings:
            result["pqc_filings"] = pqc_filings

        logger.info(
            "EDGAR fetch: %d filings, %d form4 tickers, %d 13D, %d PQC",
            len(result.get("filings", [])),
            len(result.get("form4", {})),
            len(result.get("activist_13d", [])),
            len(result.get("pqc_filings", [])),
        )
        return result

    def _fetch_courtlistener_data(self) -> dict[str, Any]:
        """Fetch CourtListener data for litigation strategy."""
        from tradingagents.strategies.learning.event_monitor import EventMonitor

        monitor = EventMonitor(self.registry)
        result: dict[str, Any] = {}

        # Search for securities-related cases
        for query in ["securities class action", "SEC enforcement", "antitrust"]:
            dockets = monitor.poll_court_dockets(query=query, days_back=14)
            existing = result.get("dockets", [])
            existing.extend(dockets)
            result["dockets"] = existing

        logger.info(
            "CourtListener fetch: %d dockets",
            len(result.get("dockets", [])),
        )
        return result

    def _fetch_fred_data(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Fetch FRED credit spreads and economic indicators."""
        source = self.registry.get("fred")
        if source is None:
            return {}

        result: dict[str, Any] = {}

        # Credit spreads for regime model
        try:
            spreads = source.fetch_credit_spreads(start_date, end_date)
            result.update(
                spreads
            )  # Keys are FRED series IDs (BAMLH0A0HYM2, BAMLC0A4CBBB)
        except Exception:
            logger.error("Failed to fetch FRED credit spreads", exc_info=True)

        # Economic indicators for regime model
        try:
            indicators = source.fetch_economic_indicators(start_date, end_date)
            result.update(indicators)  # Keys are FRED series IDs (UNRATE, PAYEMS, etc.)
        except Exception:
            logger.error("Failed to fetch FRED economic indicators", exc_info=True)

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
            result["recent_trades"] = trades
            logger.info("Congress fetch: %d recent trades", len(trades))
        except Exception:
            logger.error("Failed to fetch congressional trades", exc_info=True)

        return result

    def _fetch_usaspending_data(self, trading_date: str) -> dict[str, Any]:
        """Fetch recent large federal contract awards."""
        source = self.registry.get("usaspending")
        if source is None:
            return {}

        try:
            contracts = source.get_recent_large_contracts(
                min_amount=50_000_000,
                days_back=30,
                as_of=trading_date,
            )
            result = {"contracts": contracts}
            logger.info("USASpending fetch: %d large contracts", len(contracts))
            return {"data": result}
        except Exception:
            logger.error("Failed to fetch USASpending data", exc_info=True)
            return {}

    def _fetch_cftc_data(self) -> dict[str, Any]:
        """Fetch CFTC COT positioning data for commodity strategy."""
        source = self.registry.get("cftc")
        if source is None:
            return {}

        return source.fetch(
            {
                "method": "cot_positioning",
                "commodities": ["gold", "silver", "crude_oil", "nat_gas", "copper"],
                "lookback_weeks": 52,
            }
        )

    def _fetch_noaa_data(self, trading_date: str) -> dict[str, Any]:
        """Fetch NOAA weather anomaly summary for Corn Belt ag regions."""
        source = self.registry.get("noaa")
        if source is None:
            return {}

        try:
            return source.fetch_ag_weather_summary(trading_date, lookback_days=30)
        except Exception:
            logger.error("Failed to fetch NOAA weather data", exc_info=True)
            return {}

    def _fetch_usda_data(self, trading_date: str) -> dict[str, Any]:
        """Fetch USDA crop condition data for corn, soybeans, and wheat."""
        source = self.registry.get("usda")
        if source is None:
            return {}

        try:
            from datetime import datetime

            year = datetime.strptime(trading_date, "%Y-%m-%d").year
            crop_progress = {}
            for commodity in ("CORN", "SOYBEANS", "WHEAT"):
                weeks = source.fetch_crop_progress(commodity, year)
                if weeks:
                    crop_progress[commodity] = weeks
            return {"crop_progress": crop_progress}
        except Exception:
            logger.error("Failed to fetch USDA data", exc_info=True)
            return {}

    def _fetch_drought_data(self, trading_date: str) -> dict[str, Any]:
        """Fetch Drought Monitor severity and composite score."""
        source = self.registry.get("drought_monitor")
        if source is None:
            return {}

        try:
            from datetime import datetime, timedelta

            end = trading_date
            start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=7)).strftime(
                "%Y-%m-%d"
            )
            severity = source.fetch_drought_severity(start=start, end=end)
            composite = source.fetch_composite_score(date=trading_date)
            return {"composite_score": composite, "states": severity}
        except Exception:
            logger.error("Failed to fetch Drought Monitor data", exc_info=True)
            return {}

    def _fetch_cftc_data(self) -> dict[str, Any]:
        """Fetch CFTC COT positioning data for commodity strategy."""
        source = self.registry.get("cftc")
        if source is None:
            return {}

        return source.fetch(
            {
                "method": "cot_positioning",
                "commodities": ["gold", "silver", "crude_oil", "nat_gas", "copper"],
                "lookback_weeks": 52,
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
        prices_df = source.fetch_prices(core_tickers, start_date, end_date)

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
        vix_df = source.fetch_vix(start_date, end_date)
        if not vix_df.empty:
            result["vix"] = vix_df

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
        enriched = []
        for c in candidates:
            if not c.metadata.get("needs_llm_analysis"):
                enriched.append(c)
                continue

            analysis_type = c.metadata.get("analysis_type", "")
            llm_result = {}

            try:
                if analysis_type == "earnings_call":
                    llm_result = self._analyzer.analyze_earnings_call(
                        c.metadata.get(
                            "analysis_text", c.metadata.get("transcript_text", "")
                        ),
                        c.ticker,
                        regime_context=regime_context,
                        text_source=c.metadata.get("text_source", "earnings_news"),
                    )
                elif analysis_type == "regulation":
                    llm_result = self._analyzer.analyze_regulation(
                        c.metadata.get("title", ""),
                        c.metadata.get("summary", ""),
                        c.metadata.get("agency_id", ""),
                        regime_context=regime_context,
                    )
                elif analysis_type == "supply_chain":
                    llm_result = self._analyzer.analyze_supply_chain(
                        c.metadata.get("headline", ""),
                        c.metadata.get("summary", ""),
                        c.ticker,
                        c.metadata.get("affected_peers", []),
                        regime_context=regime_context,
                    )
                elif analysis_type == "litigation":
                    llm_result = self._analyzer.analyze_litigation(
                        c.metadata.get("case_name", ""),
                        c.metadata.get("nature_of_suit", ""),
                        c.metadata.get("cause", ""),
                        c.metadata.get("court", ""),
                        regime_context=regime_context,
                    )
                elif analysis_type == "insider_activity":
                    cluster_type = c.metadata.get("cluster_type", "")
                    if cluster_type == "buy_cluster":
                        llm_result = self._analyzer.analyze_insider_context(
                            c.metadata.get("filings", []),
                            c.ticker,
                            regime_context=regime_context,
                        )
                    elif cluster_type == "sell_pattern":
                        llm_result = self._analyzer.analyze_10b5_1_plan(
                            c.metadata.get("filings", []),
                            c.ticker,
                            regime_context=regime_context,
                        )
                elif analysis_type == "filing_change":
                    llm_result = self._analyzer.analyze_filing_change(
                        c.metadata.get("current_text", ""),
                        c.metadata.get("prior_text", ""),
                        c.ticker,
                        regime_context=regime_context,
                    )
                elif analysis_type == "exec_comp":
                    llm_result = self._analyzer.analyze_exec_comp(
                        c.metadata.get("proxy_text", ""),
                        c.ticker,
                        regime_context=regime_context,
                    )
                elif analysis_type == "ag_weather":
                    llm_result = self._analyzer.analyze_ag_weather(
                        ticker=c.ticker,
                        commodity_name=c.metadata.get("commodity", c.ticker),
                        ag_context={
                            "drought_score": c.metadata.get("drought_score", 0),
                            "drought_states": c.metadata.get("drought_states", {}),
                            "noaa_data": c.metadata.get("noaa_data", {}),
                            "usda_data": c.metadata.get("usda_data", {}),
                        },
                        trailing_return=c.metadata.get("trailing_return", 0),
                        hold_days=21,
                        regime_context=regime_context,
                    )
            except Exception:
                logger.error(
                    "LLM analysis failed for %s/%s",
                    strategy_name,
                    c.ticker,
                    exc_info=True,
                )

            if llm_result:
                # Update candidate with LLM results
                c.direction = llm_result.get("direction", c.direction)
                c.score = llm_result.get("conviction", llm_result.get("score", c.score))
                c.metadata["llm_analysis"] = llm_result
                # Resolve ticker if LLM provided one
                if not c.ticker and llm_result.get("defendant_ticker"):
                    c.ticker = llm_result["defendant_ticker"]
                if not c.ticker and llm_result.get("affected_tickers"):
                    c.ticker = llm_result["affected_tickers"][0]

                # Validate LLM-resolved ticker against SEC data
                if c.ticker:
                    edgar = self.registry.get("edgar")
                    if edgar and hasattr(edgar, "validate_ticker"):
                        if not edgar.validate_ticker(c.ticker):
                            logger.warning(
                                "LLM returned invalid ticker %s for %s, dropping",
                                c.ticker,
                                strategy_name,
                            )
                            c.ticker = ""

            enriched.append(c)

        return enriched
