"""Cutoff-safe recommendation staging and authoritative paper execution."""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, fields as dataclass_fields, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from tradingagents.strategies.execution import (
    AccountState,
    Fill,
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
from tradingagents.strategies.trading.portfolio_policy import (
    PortfolioPolicy,
    PortfolioRiskContext,
)
from tradingagents.strategies.trading.risk_gate import (
    PendingRiskEntry,
    RiskGate,
    RiskGateConfig,
)


logger = logging.getLogger(__name__)

DIRECT_SUBMISSION_DISABLED = (
    "Direct price submission is disabled; stage and execute a ledger intent"
)

_FILL_RESULT_FIELDS = frozenset(f.name for f in dataclass_fields(FillResult))

_TRIMMABLE_RISK_PREFIXES = (
    "max_position_pct:",
    "buying_power:",
    "cash_reserve:",
    "margin_utilization:",
)
_TRIMMABLE_POLICY_REASONS = frozenset(
    {
        "max_position",
        "max_sector_exposure",
        "cash_reserve",
        "max_single_short",
        "max_short_exposure",
        "margin_cash_buffer",
        "max_risk_contribution",
    }
)


def _is_trimmable_rejection(reason: str) -> bool:
    """Return True only when a rejection is a size/exposure cap, never a hard risk gate."""
    if reason.startswith(_TRIMMABLE_RISK_PREFIXES):
        return True
    if reason.startswith("portfolio_policy:"):
        policy_reason = reason[len("portfolio_policy:") :]
        if PortfolioPolicy.is_hard_rejection(policy_reason):
            return False
        if policy_reason in _TRIMMABLE_POLICY_REASONS:
            return True
        if policy_reason.startswith(("strategy:", "risk_tag:")):
            return True
    return False


def _make_fill_result(
    status: str,
    fill: Fill | None,
    reason: str,
    *,
    trimmed_at_open: bool = False,
    requested_qty: int | None = None,
) -> FillResult:
    """Construct FillResult, populating trim metadata when supported by FillResult."""
    if "trimmed_at_open" in _FILL_RESULT_FIELDS and "requested_qty" in _FILL_RESULT_FIELDS:
        return FillResult(
            status,
            fill,
            reason,
            trimmed_at_open=trimmed_at_open,
            requested_qty=requested_qty,
        )
    return FillResult(status, fill, reason)


def _build_costed_fill(
    cost_model: PaperCostModel,
    intent: OrderIntent,
    reference_price: Decimal,
    effective_at: datetime,
    processing_at: datetime,
    *,
    quantity: int | None = None,
) -> Fill:
    """Build a Fill via PaperCostModel, supporting trimmed quantities cleanly."""
    if quantity is None or quantity == intent.requested_qty:
        return cost_model.fill(intent, reference_price, effective_at, processing_at)
    sig = inspect.signature(cost_model.fill)
    if "quantity" in sig.parameters:
        return cost_model.fill(
            intent,
            reference_price,
            effective_at,
            processing_at,
            quantity=quantity,
        )
    return cost_model.fill(
        replace(intent, requested_qty=quantity),
        reference_price,
        effective_at,
        processing_at,
    )


@dataclass
class OrderResult:
    order_id: str
    status: str
    filled_qty: float = 0
    filled_price: float = 0.0
    message: str = ""


@dataclass
class AccountInfo:
    cash: float
    portfolio_value: float
    buying_power: float


class ZeroShareIntentError(ValueError):
    """A valid approved allocation cannot fund one whole reference-price share."""


class ExecutionBridge:
    """Persist orders before prices exist, then execute through one paper ledger."""

    def __init__(self, config: dict, *, ledger: PortfolioLedger) -> None:
        self.config = config
        if config.get("execution", {}).get("mode", "paper") != "paper":
            raise ValueError("EventEdge cohort execution must remain paper-only")
        self.ledger = ledger
        self.risk_gate = RiskGate(RiskGateConfig.from_dict(config), ledger=ledger)
        self.last_open_trim: dict[str, Any] | None = None
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
        from tradingagents.strategies.orchestration.decision_clock import policy, PROSPECTIVE, resolve_cutoff, require_live_staging
        decision_context = None
        if policy(self.config) == PROSPECTIVE:
            binding = self.ledger.read_policy_session_context(reference_session, binding_kind='staging')
            if binding is None or binding['epoch_id'] != signals[0].epoch_id:
                raise ValueError('matching persisted decision context required')
            decision_context = binding['context'].get('decision_clock')
        cutoff = resolve_cutoff(self.config, reference_session, decision_context)
        if decision_context is not None:
            publication_at = require_live_staging(self.config, reference_session, decision_context)
            if decision_at < cutoff or decision_at > publication_at:
                raise ValueError('intent decision is outside cutoff and publication clocks')
        elif decision_at > cutoff:
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
            raise ZeroShareIntentError("approved recommendation sizes to zero shares")

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
        admission_prices = risk_context.get("opening_prices") if self.ledger.session_execution_context(opening_bar.session) is not None else None
        if marked_account != self.ledger.account_state(admission_prices):
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

        if risk_context.get("intraday"):
            effective_at = session_close(opening_bar.session)
        self.last_open_trim = None
        fill = _build_costed_fill(
            cost_model, intent, reference_price, effective_at, processing_at
        )
        trimmed_at_open = False
        direction = "short" if intent.side == "short" else "long"
        if intent.side in {"buy", "short"}:
            borrow_rate = risk_context.get("borrow_rate")
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

            opening_equity = self.ledger.opening_equity(opening_bar.session, opening_prices)
            cooldown = int(
                self.config.get("autoresearch", {})
                .get("risk_discipline", {})
                .get("reentry_cooldown_days", 0)
            )
            self.risk_gate.set_cooling_tickers(
                self.ledger.cooling_tickers(opening_bar.session, cooldown)
            )
            base_policy_context: PortfolioRiskContext | None = risk_context.get(
                "portfolio_context"
            )
            base_recommendation: TradeRecommendation | None = risk_context.get(
                "recommendation"
            )
            policy_enabled = bool(risk_context.get("policy_enabled", False))
            strategies_tuple = tuple(sorted({signal.strategy for signal in signals}))
            session_realized = float(
                self.ledger.session_realized_net(opening_bar.session)
            )
            pending_entries_tuple = tuple(pending_risk_entries)
            pending_pos_value = sum(
                Decimal(str(entry.position_value)) for entry in pending_entries_tuple
            )
            pending_margin_total = sum(
                Decimal(str(entry.margin_required)) for entry in pending_entries_tuple
            )

            # Determine max_open_chase_pct and overnight gap vs staged reference_close
            if base_policy_context is not None:
                max_open_chase_pct = float(base_policy_context.config.max_open_chase_pct)
            else:
                max_open_chase_pct = float(
                    self.config.get("autoresearch", {})
                    .get("portfolio_policy", {})
                    .get("max_open_chase_pct", 0.05)
                )
            signal_ref_close = signals[0].reference_close
            if signal_ref_close > 0:
                if intent.side == "buy":
                    adverse_chase_pct = float(
                        (reference_price - signal_ref_close) / signal_ref_close
                    )
                else:
                    adverse_chase_pct = float(
                        (signal_ref_close - reference_price) / signal_ref_close
                    )
                abs_gap_pct = float(
                    abs(reference_price - signal_ref_close) / signal_ref_close
                )
            else:
                adverse_chase_pct = 0.0
                abs_gap_pct = 0.0

            if (
                intent.price_rule == "next_session_open"
                and adverse_chase_pct > max_open_chase_pct + 1e-9
            ):
                return self._reject(
                    intent,
                    processing_at,
                    f"max_open_chase_pct: overnight gap {adverse_chase_pct:.2%} > {max_open_chase_pct:.2%}",
                )

            def _evaluate_qty(
                candidate_fill: Fill,
            ) -> tuple[bool, str, AccountState, PortfolioRiskContext | None, TradeRecommendation | None]:
                costs = (
                    candidate_fill.slippage
                    + candidate_fill.commission
                    + candidate_fill.other_fees
                )
                # Exposure is marked at the causal open; costs reduce equity and cash.
                pos_value = reference_price * candidate_fill.quantity
                acct = replace(
                    marked_account,
                    cash=marked_account.cash - costs,
                    buying_power=marked_account.buying_power - costs,
                    net_equity=marked_account.net_equity - costs,
                )
                if acct.buying_power < 0 or acct.net_equity <= 0:
                    return (
                        False,
                        "collateral_deficiency: insufficient costed buying power",
                        acct,
                        base_policy_context,
                        base_recommendation,
                    )
                scaled_ctx = base_policy_context
                scaled_rec = base_recommendation
                if scaled_ctx is not None:
                    equity_scale = scaled_ctx.portfolio_value / float(acct.net_equity)
                    scaled_ctx = replace(
                        scaled_ctx,
                        cash=float(acct.cash),
                        portfolio_value=float(acct.net_equity),
                        positions=tuple(
                            replace(position, weight=position.weight * equity_scale)
                            for position in scaled_ctx.positions
                        ),
                        pending_positions=tuple(
                            replace(position, weight=position.weight * equity_scale)
                            for position in scaled_ctx.pending_positions
                        ),
                    )
                    if scaled_rec is not None:
                        scaled_rec = replace(
                            scaled_rec,
                            position_size_pct=float(pos_value / acct.net_equity),
                        )
                ok, fail_reason = self.risk_gate.check(
                    opening_bar.ticker,
                    direction,
                    float(pos_value),
                    strategies_tuple,
                    risk_context.get("open_trades"),
                    risk_context.get("earnings_dates"),
                    risk_context.get("short_interest"),
                    authoritative_account=acct,
                    daily_loss_equity=float(opening_equity),
                    session_realized_net=session_realized,
                    pending_entries=pending_entries_tuple,
                    proposed_margin=(
                        float(pos_value * cost_model.margin_requirement)
                        if intent.side == "short"
                        else 0.0
                    ),
                    policy_enabled=policy_enabled,
                    recommendation=scaled_rec,
                    portfolio_context=scaled_ctx,
                )
                return ok, fail_reason, acct, scaled_ctx, scaled_rec

            def _max_allowed_notional(
                acct: AccountState,
                pol_ctx: PortfolioRiskContext | None,
                pol_rec: TradeRecommendation | None,
            ) -> Decimal:
                bounds: list[Decimal] = [acct.buying_power - pending_pos_value]
                if policy_enabled and pol_ctx is not None and pol_rec is not None:
                    allowed_w, _ = PortfolioPolicy().max_allowed_weight(pol_rec, pol_ctx)
                    bounds.append(Decimal(str(allowed_w)) * acct.net_equity)
                else:
                    bounds.append(
                        acct.net_equity
                        * Decimal(str(self.risk_gate.config.max_position_pct))
                    )
                    if self.risk_gate.config.cash_reserve_pct > 0:
                        min_cash = acct.net_equity * Decimal(
                            str(self.risk_gate.config.cash_reserve_pct)
                        )
                        bounds.append(acct.buying_power - pending_pos_value - min_cash)
                if (
                    intent.side == "short"
                    and self.risk_gate.config.max_margin_utilization_pct > 0
                    and cost_model.margin_requirement > 0
                ):
                    margin_limit = Decimal(
                        str(self.risk_gate.config.total_capital)
                    ) * Decimal(str(self.risk_gate.config.max_margin_utilization_pct))
                    margin_headroom = (
                        margin_limit - acct.margin_used - pending_margin_total
                    )
                    bounds.append(margin_headroom / cost_model.margin_requirement)
                return max(Decimal("0"), min(bounds))

            passed, reason, risk_account, policy_context, recommendation = _evaluate_qty(
                fill
            )

            # Check if T+1 open fill price (including +10 bps slippage) pushed a
            # staged-valid order above max_allowed_notional.
            unit_exec_price = (
                fill.fill_price
                if intent.side == "buy"
                else reference_price + abs(fill.fill_price - reference_price)
            )
            max_notional_risk = _max_allowed_notional(
                risk_account, policy_context, recommendation
            )
            max_notional_at_marked = _max_allowed_notional(
                marked_account, base_policy_context, base_recommendation
            )
            buy_costed_breach = (
                passed
                and intent.side == "buy"
                and intent.price_rule == "next_session_open"
                and fill.fill_price * intent.requested_qty > max_notional_risk
            )

            if (not passed and _is_trimmable_rejection(reason)) or buy_costed_breach:
                staged_notional = signal_ref_close * intent.requested_qty
                pushed_over_at_open = (
                    intent.price_rule == "next_session_open"
                    and signal_ref_close > 0
                    and staged_notional <= max_notional_at_marked + Decimal("1e-6")
                    and unit_exec_price * intent.requested_qty > max_notional_risk - Decimal("1e-6")
                )
                if pushed_over_at_open:
                    if abs_gap_pct > max_open_chase_pct + 1e-9:
                        return self._reject(
                            intent,
                            processing_at,
                            f"max_open_chase_pct: overnight gap {abs_gap_pct:.2%} > {max_open_chase_pct:.2%}",
                        )
                    fill_qty = min(
                        intent.requested_qty - 1,
                        int(max_notional_risk // unit_exec_price),
                    )
                    if fill_qty < 1:
                        return self._reject(
                            intent,
                            processing_at,
                            reason if not passed else "max_position_pct: trimmed below 1 share",
                        )
                    trimmed_fill = _build_costed_fill(
                        cost_model,
                        intent,
                        reference_price,
                        effective_at,
                        processing_at,
                        quantity=fill_qty,
                    )
                    trim_ok, trim_reason, _, _, _ = _evaluate_qty(trimmed_fill)
                    if not trim_ok and _is_trimmable_rejection(trim_reason) and fill_qty > 1:
                        fill_qty -= 1
                        trimmed_fill = _build_costed_fill(
                            cost_model,
                            intent,
                            reference_price,
                            effective_at,
                            processing_at,
                            quantity=fill_qty,
                        )
                        trim_ok, trim_reason, _, _, _ = _evaluate_qty(trimmed_fill)
                    if not trim_ok:
                        return self._reject(intent, processing_at, trim_reason)
                    fill = trimmed_fill
                    passed = True
                    trimmed_at_open = True
                    self.last_open_trim = {
                        "intent_id": intent.intent_id,
                        "ticker": opening_bar.ticker,
                        "requested_qty": intent.requested_qty,
                        "filled_qty": fill.quantity,
                        "trimmed_at_open": True,
                        "overnight_gap_pct": abs_gap_pct,
                        "original_reason": reason or "costed_notional_cap",
                    }
                    logger.info(
                        "Trimmed T+1 open entry %s %s: requested=%d filled=%d gap=%.2f%%",
                        opening_bar.ticker,
                        intent.side,
                        intent.requested_qty,
                        fill.quantity,
                        abs_gap_pct * 100.0,
                    )

            if not passed:
                return self._reject(intent, processing_at, reason)
            if intent.side == "short":
                try:
                    cost_model.validate_new_short_borrow_rate(
                        borrow_rate, self._borrow_reject_above
                    )
                except (TypeError, ValueError) as error:
                    return self._reject(intent, processing_at, str(error))
        else:
            borrow_rate = None

        self.ledger.apply_fill(intent, fill, borrow_rate=borrow_rate)
        persisted = [
            item
            for item in self.ledger.read_fills(opening_bar.session, opening_bar.session)
            if item.fill_id == fill.fill_id
        ]
        if persisted != [fill]:
            raise RuntimeError("authoritative fill persistence failed")
        if trimmed_at_open:
            return _make_fill_result(
                "filled",
                fill,
                f"trimmed_at_open:requested={intent.requested_qty},filled={fill.quantity}",
                trimmed_at_open=True,
                requested_qty=intent.requested_qty,
            )
        return _make_fill_result("filled", fill, "")

    def _reject(
        self, intent: OrderIntent, occurred_at: datetime, reason: str
    ) -> FillResult:
        self.ledger.reject_intent(intent.intent_id, occurred_at, reason)
        return _make_fill_result("rejected", None, reason)

    def execute_recommendation(self, *args: object, **kwargs: object) -> OrderResult:
        del args, kwargs
        raise RuntimeError(DIRECT_SUBMISSION_DISABLED)

    def close_position(self, *args: object, **kwargs: object) -> OrderResult:
        del args, kwargs
        raise RuntimeError(DIRECT_SUBMISSION_DISABLED)

    def get_account(self) -> AccountInfo:
        state = self.ledger.account_state()
        return AccountInfo(
            cash=float(state.cash),
            portfolio_value=float(state.net_equity),
            buying_power=float(state.buying_power),
        )

    def get_positions(self) -> list[dict]:
        return [
            {**position, "avg_price": float(position["avg_price"])}
            for position in self.ledger.open_positions()
        ]

    @property
    def is_live(self) -> bool:
        return False

    @staticmethod
    def _require_aware(value: datetime, label: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{label} must be timezone-aware")
