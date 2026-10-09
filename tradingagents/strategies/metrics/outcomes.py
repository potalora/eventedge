from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping

from tradingagents.strategies.execution.models import CorporateAction, MarketBar

from .calendar import XNYSCalendar
from .identity import _stable_id
from .models import OutcomeRecord, SignalMetricRecord


@dataclass(frozen=True)
class DirectionalAccuracy:
    actionable_count: int
    hit_count: int
    neutral_count: int
    invalid_count: int
    rate: float | None


class OutcomeCalculator:
    def __init__(self, calendar: XNYSCalendar | None = None) -> None:
        self.calendar = calendar or XNYSCalendar()

    def build(
        self,
        signal: SignalMetricRecord,
        holding_sessions: int,
        bars: Mapping[tuple[str, object], MarketBar],
        *,
        corporate_actions: Iterable[CorporateAction] = (),
    ) -> OutcomeRecord:
        entry_session = self.calendar.next_session(signal.reference_session)
        exit_session = self.calendar.held_session(entry_session, holding_sessions)
        entry_bar = bars.get((signal.ticker, entry_session))
        exit_bar = bars.get((signal.ticker, exit_session))
        reason = ""
        entry_price = entry_bar.open if entry_bar else None
        exit_price = exit_bar.close if exit_bar else None
        if entry_bar is None:
            reason = "missing_entry_bar"
        elif not self._is_exact_raw_bar(
            entry_bar, signal.ticker, entry_session
        ):
            reason = "invalid_entry_bar"
        elif entry_price is not None and (
            not entry_price.is_finite() or entry_price <= 0
        ):
            reason = "invalid_entry_price"
        elif exit_bar is None:
            reason = "missing_exit_bar"
        elif not self._is_exact_raw_bar(
            exit_bar, signal.ticker, exit_session
        ):
            reason = "invalid_exit_bar"
        elif exit_price is not None and (not exit_price.is_finite() or exit_price <= 0):
            reason = "invalid_exit_price"
        raw_return: Decimal | None = None
        signed_return: Decimal | None = None
        shares = Decimal(1)
        distributions = Decimal(0)
        seen: dict[str, CorporateAction] = {}
        dividend_sessions: set[object] = set()
        for action in sorted(corporate_actions, key=lambda a: (a.session, a.action_type != "split", a.action_id)):
            if action.ticker != signal.ticker or not (entry_session < action.session <= exit_session):
                continue
            if action.action_id in seen:
                if seen[action.action_id] != action:
                    reason = reason or "conflicting_corporate_action"
                continue
            seen[action.action_id] = action
            if not action.verified or not action.source or not self.calendar.is_session(action.session):
                reason = reason or "unverified_corporate_action"
            elif action.action_type == "split":
                if action.ratio is None or not action.ratio.is_finite() or action.ratio <= 0 or action.cash_per_share is not None:
                    reason = reason or "invalid_split_terms"
                else:
                    shares *= action.ratio
            elif action.action_type == "cash_dividend":
                if action.session in dividend_sessions or action.cash_per_share is None or not action.cash_per_share.is_finite() or action.cash_per_share < 0 or action.ratio is not None:
                    reason = reason or "invalid_dividend_terms"
                else:
                    distributions += shares * action.cash_per_share
                    dividend_sessions.add(action.session)
            else:
                reason = reason or "unsupported_corporate_action"
        if not reason:
            raw_return = (exit_price * shares + distributions - entry_price) / entry_price
            if signal.direction == "long":
                signed_return = raw_return
            elif signal.direction == "short":
                signed_return = -raw_return
        return OutcomeRecord(
            outcome_id=self.outcome_id(signal, holding_sessions),
            signal_id=signal.signal_id,
            event_key=signal.event_key,
            epoch_id=signal.epoch_id,
            strategy=signal.strategy,
            policy_id=signal.policy_id,
            ticker=signal.ticker,
            direction=signal.direction,
            holding_sessions=holding_sessions,
            entry_session=entry_session,
            exit_session=exit_session,
            entry_price=entry_price,
            exit_price=exit_price,
            raw_return=raw_return,
            signed_return=signed_return,
            status="invalid" if reason else "valid",
            invalid_reason=reason,
            return_basis="next_open_total_shareholder_return_gross_v2",
        )

    @staticmethod
    def outcome_id(signal: SignalMetricRecord, holding_sessions: int) -> str:
        return _stable_id("outcome", signal.signal_id, holding_sessions)

    @staticmethod
    def _is_exact_raw_bar(bar: MarketBar, ticker: str, session: object) -> bool:
        return bar.ticker == ticker and bar.session == session and not bar.adjusted


def directional_accuracy(
    outcomes: Iterable[OutcomeRecord],
) -> DirectionalAccuracy:
    rows = list(outcomes)
    valid = [row for row in rows if row.status == "valid"]
    actionable = [row for row in valid if row.direction in {"long", "short"}]
    hits = sum(row.signed_return > 0 for row in actionable)
    return DirectionalAccuracy(
        actionable_count=len(actionable),
        hit_count=hits,
        neutral_count=sum(row.direction == "neutral" for row in valid),
        invalid_count=sum(row.status == "invalid" for row in rows),
        rate=hits / len(actionable) if actionable else None,
    )
