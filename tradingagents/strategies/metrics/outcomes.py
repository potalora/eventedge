from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping

from tradingagents.strategies.execution.models import MarketBar

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
        if not reason:
            raw_return = (exit_price - entry_price) / entry_price
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
