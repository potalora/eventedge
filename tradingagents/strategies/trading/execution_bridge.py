"""Cutoff-safe recommendation staging and authoritative paper execution."""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from tradingagents.execution.base_broker import AccountInfo, OrderResult
from tradingagents.execution.paper_broker import (
    DIRECT_SUBMISSION_DISABLED,
    PaperBroker,
)
from tradingagents.strategies.execution import (
    AccountState,
    FillResult,
    MarketBar,
    OrderIntent,
    SignalRecord,
    stable_id,
)
from tradingagents.strategies.execution.cost_model import PaperCostModel
from tradingagents.strategies.execution.price_source import validate_required_bars
from tradingagents.strategies.execution.stop_execution import stop_reference
from tradingagents.strategies.orchestration.trading_calendar import (
    next_session,
    session_close,
    session_open,
)
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
from tradingagents.strategies.trading.portfolio_committee import TradeRecommendation
from tradingagents.strategies.trading.risk_gate import (
    PendingRiskEntry,
    RiskGate,
    RiskGateConfig,
)


logger = logging.getLogger(__name__)


class ExecutionBridge:
    """Persist orders before prices exist, then execute through one paper ledger."""

    def __init__(self, config: dict, *, ledger: PortfolioLedger) -> None:
        self.config = config
        if config.get("execution", {}).get("mode", "paper") != "paper":
            raise ValueError("EventEdge cohort execution must remain paper-only")
        self.ledger = ledger
        self.broker = PaperBroker(ledger)
        self.risk_gate = RiskGate(RiskGateConfig.from_dict(config), self.broker)
        short_config = config.get("autoresearch", {}).get("short_selling", {})
        try:
            self._borrow_reject_above = Decimal(
                str(short_config.get("borrow_cost_reject_above", "0.05"))
            )
        except (InvalidOperation, ValueError) as error:
            raise ValueError("invalid borrow_cost_reject_above") from error
        if not self._borrow_reject_above.is_finite() or self._borrow_reject_above < 0:
            raise ValueError("invalid borrow_cost_reject_above")

    def stage_intent(
        self,
        recommendation: TradeRecommendation,
        signal_records: tuple[SignalRecord, ...],
        marked_account: AccountState,
        decision_at: datetime,
        eligible_session: date,
    ) -> OrderIntent:
        """Persist a deterministic next-open intent without mutating accounting."""
        self._require_aware(decision_at, "decision_at")
        if recommendation.vehicle != "equity":
            raise ValueError("only equity recommendations can become stock intents")
        if not signal_records:
            raise ValueError("at least one persisted signal is required")
        if marked_account != self.ledger.account_state():
            raise ValueError("marked_account does not match authoritative ledger")
        if marked_account.cohort_id != self.ledger.cohort_id:
            raise ValueError("marked_account cohort does not match ledger")

        signals = tuple(sorted(signal_records, key=lambda signal: signal.signal_id))
        signal_ids = tuple(signal.signal_id for signal in signals)
        if self.ledger.signals_by_ids(signal_ids) != signals:
            raise ValueError("all intent signals must already be persisted exactly")
        if len({signal.epoch_id for signal in signals}) != 1:
            raise ValueError("intent signals must share one epoch")
        if len({signal.policy_id for signal in signals}) != 1:
            raise ValueError("intent signals must share one policy")
        if len({signal.ticker for signal in signals}) != 1:
            raise ValueError("intent signals must share one ticker")
        if len({signal.reference_session for signal in signals}) != 1:
            raise ValueError("intent signals must share one reference session")
        if len({signal.reference_close for signal in signals}) != 1:
            raise ValueError("intent signals must share one reference close")
        if recommendation.ticker != signals[0].ticker:
            raise ValueError("recommendation ticker does not match signals")
        if recommendation.direction not in {"long", "short"}:
            raise ValueError("recommendation direction must be long or short")
        contributors = recommendation.contributing_strategies
        if len(contributors) != len(set(contributors)) or set(contributors) != {
            signal.strategy for signal in signals
        }:
            raise ValueError(
                "recommendation contributing strategies do not match signals"
            )

        reference_session = signals[0].reference_session
        cutoff = session_close(reference_session)
        if decision_at > cutoff:
            raise ValueError("intent decision is after the session cutoff")
        for signal in signals:
            self._require_aware(signal.observed_at, "signal observed_at")
            self._require_aware(signal.decision_at, "signal decision_at")
            if signal.event_at is not None:
                self._require_aware(signal.event_at, "signal event_at")
            timestamps = [signal.observed_at, signal.decision_at]
            if signal.event_at is not None:
                timestamps.append(signal.event_at)
            if any(timestamp > cutoff for timestamp in timestamps):
                raise ValueError("signal information is after the session cutoff")
            if signal.observed_at > signal.decision_at:
                raise ValueError("signal observed_at is after signal decision_at")
            if signal.decision_at > decision_at:
                raise ValueError("signal decision_at is after intent decision_at")
        if decision_at < max(signal.decision_at for signal in signals):
            raise ValueError("decision_at precedes signal decision")
        if eligible_session != next_session(reference_session):
            raise ValueError("eligible_session is not the next XNYS session")

        try:
            approved_pct = Decimal(str(recommendation.position_size_pct))
        except (InvalidOperation, ValueError) as error:
            raise ValueError("invalid approved position percentage") from error
        reference_price = signals[0].reference_close
        if (
            not approved_pct.is_finite()
            or approved_pct <= 0
            or approved_pct > 1
            or not reference_price.is_finite()
            or reference_price <= 0
        ):
            raise ValueError("invalid intent sizing inputs")
        allocation = min(
            marked_account.net_equity * approved_pct,
            marked_account.buying_power,
        )
        requested_qty = int(allocation / reference_price)
        if requested_qty <= 0:
            raise ValueError("approved recommendation sizes to zero shares")

        side = "short" if recommendation.direction == "short" else "buy"
        intent = OrderIntent(
            stable_id(
                "intent",
                self.ledger.cohort_id,
                signal_ids,
                side,
                requested_qty,
                decision_at,
                eligible_session,
                "next_session_open",
            ),
            signal_ids,
            self.ledger.cohort_id,
            side,
            requested_qty,
            decision_at,
            eligible_session,
            "next_session_open",
            "pending",
            None,
            None,
        )
        self.ledger.stage_intent(intent)
        return intent

    def execute_due_intent(
        self,
        intent: OrderIntent,
        opening_bar: MarketBar,
        marked_account: AccountState,
        risk_context: dict,
        cost_model: PaperCostModel,
    ) -> FillResult:
        """Risk-check and atomically fill one due intent from an exact raw bar."""
        stored = self.ledger.intent(intent.intent_id)
        if stored is None:
            raise ValueError(f"unknown order intent {intent.intent_id}")
        if stored.status != "pending":
            raise ValueError(f"intent is already terminal: {stored.status}")
        if stored != intent:
            raise ValueError("supplied intent does not match persisted intent")
        if marked_account != self.ledger.account_state():
            raise ValueError("marked_account does not match authoritative ledger")

        signals = self.ledger.signals_for_intent(intent.intent_id)
        tickers = {signal.ticker for signal in signals}
        if len(tickers) != 1 or opening_bar.ticker not in tickers:
            raise ValueError("opening bar ticker does not match intent provenance")
        if intent.price_rule == "next_session_open":
            if opening_bar.session != intent.eligible_session:
                raise ValueError("opening bar does not match eligible session")
        elif (
            intent.price_rule == "resting_stop"
            and opening_bar.session < intent.eligible_session
        ):
            raise ValueError("opening bar precedes resting-stop eligible session")
        if opening_bar.adjusted:
            raise ValueError("execution requires a raw unadjusted bar")
        self._require_aware(opening_bar.fetched_at, "opening_bar.fetched_at")
        processing_at = risk_context.get("processing_at")
        if not isinstance(processing_at, datetime):
            raise ValueError("risk_context processing_at is required")
        self._require_aware(processing_at, "risk_context processing_at")
        bar_validation_at = risk_context.get("bar_validation_at", processing_at)
        if not isinstance(bar_validation_at, datetime):
            raise ValueError("risk_context bar_validation_at must be a datetime")
        self._require_aware(bar_validation_at, "risk_context bar_validation_at")
        validate_required_bars(
            {(opening_bar.ticker, opening_bar.session): opening_bar},
            {opening_bar.ticker},
            opening_bar.session,
            bar_validation_at,
        )
        if opening_bar.fetched_at < session_close(opening_bar.session):
            raise ValueError("execution bar was fetched before session close")
        effective_at = session_open(opening_bar.session)
        if intent.created_at >= effective_at:
            raise ValueError("intent did not exist before the execution price")

        reference_price = stop_reference(intent, opening_bar)
        if reference_price is None:
            return FillResult("pending", None, "resting stop not triggered")

        direction = "short" if intent.side == "short" else "long"
        if intent.side in {"buy", "short"}:
            borrow_rate = risk_context.get("borrow_rate")
            if intent.side == "short":
                try:
                    cost_model.validate_new_short_borrow_rate(
                        borrow_rate, self._borrow_reject_above
                    )
                except (TypeError, ValueError) as error:
                    return self._reject(intent, processing_at, str(error))
            due_entries = [
                pending
                for pending in self.ledger.pending_intents(opening_bar.session)
                if pending.side in {"buy", "short"}
            ]
            prior_entries = []
            for pending in due_entries:
                if pending.intent_id == intent.intent_id:
                    break
                prior_entries.append(pending)
            opening_prices = risk_context.get("opening_prices", {})
            if not isinstance(opening_prices, dict):
                raise ValueError("risk_context opening_prices must be a mapping")
            pending_risk_entries: list[PendingRiskEntry] = []
            for pending in prior_entries:
                pending_signals = self.ledger.signals_for_intent(pending.intent_id)
                pending_tickers = {signal.ticker for signal in pending_signals}
                if len(pending_tickers) != 1:
                    raise ValueError("pending intent has ambiguous ticker provenance")
                pending_ticker = next(iter(pending_tickers))
                pending_price = opening_prices.get(pending_ticker)
                if not isinstance(pending_price, Decimal):
                    raise ValueError(
                        f"missing Decimal opening price for pending {pending_ticker}"
                    )
                if not pending_price.is_finite() or pending_price <= 0:
                    raise ValueError(
                        f"invalid opening price for pending {pending_ticker}"
                    )
                pending_value = pending.requested_qty * pending_price
                pending_risk_entries.append(
                    PendingRiskEntry(
                        pending_ticker,
                        tuple(sorted({signal.strategy for signal in pending_signals})),
                        float(pending_value),
                        float(pending_value * cost_model.margin_requirement)
                        if pending.side == "short"
                        else 0.0,
                    )
                )
            position_value = reference_price * intent.requested_qty
            passed, reason = self.risk_gate.check(
                opening_bar.ticker,
                direction,
                float(position_value),
                tuple(sorted({signal.strategy for signal in signals})),
                risk_context.get("open_trades"),
                risk_context.get("earnings_dates"),
                risk_context.get("short_interest"),
                authoritative_account=marked_account,
                pending_entries=tuple(pending_risk_entries),
                proposed_margin=(
                    float(position_value * cost_model.margin_requirement)
                    if intent.side == "short"
                    else 0.0
                ),
            )
            if not passed:
                return self._reject(intent, processing_at, reason)
        else:
            borrow_rate = None

        fill = cost_model.fill(
            intent,
            reference_price,
            effective_at,
            processing_at,
        )
        self.ledger.apply_fill(intent, fill, borrow_rate=borrow_rate)
        persisted = [
            item
            for item in self.ledger.read_fills(opening_bar.session, opening_bar.session)
            if item.fill_id == fill.fill_id
        ]
        if persisted != [fill]:
            raise RuntimeError("authoritative fill persistence failed")
        return FillResult("filled", fill, "")

    def _reject(
        self, intent: OrderIntent, occurred_at: datetime, reason: str
    ) -> FillResult:
        self.ledger.reject_intent(intent.intent_id, occurred_at, reason)
        return FillResult("rejected", None, reason)

    def execute_recommendation(self, *args: object, **kwargs: object) -> OrderResult:
        del args, kwargs
        raise RuntimeError(DIRECT_SUBMISSION_DISABLED)

    def close_position(self, *args: object, **kwargs: object) -> OrderResult:
        del args, kwargs
        raise RuntimeError(DIRECT_SUBMISSION_DISABLED)

    def get_account(self) -> AccountInfo:
        return self.broker.get_account()

    def get_positions(self) -> list[dict]:
        return self.broker.get_positions()

    @property
    def is_live(self) -> bool:
        return False

    @staticmethod
    def _require_aware(value: datetime, label: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{label} must be timezone-aware")
