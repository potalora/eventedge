from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tradingagents.strategies.execution import (
    Fill,
    MarketBar,
    OrderIntent,
    SignalRecord,
)
from tradingagents.strategies.execution.cost_model import (
    PaperCostModel,
    validate_new_short_borrow_rate,
)
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


UTC = timezone.utc
SESSION = date(2026, 8, 3)
COHORT = "horizon_30d_size_5k"


def _signal(signal_id: str = "signal", ticker: str = "AAPL") -> SignalRecord:
    now = datetime(2026, 8, 1, 22, tzinfo=UTC)
    return SignalRecord(
        signal_id,
        "epoch",
        "policy",
        signal_id,
        "test",
        ticker,
        "short",
        now,
        now,
        date(2026, 8, 1),
        Decimal("100"),
        now,
        f"evidence-{signal_id}",
    )


def _intent(side: str, intent_id: str | None = None) -> OrderIntent:
    return OrderIntent(
        intent_id or f"{side}-intent",
        ("signal",),
        COHORT,
        side,
        10,
        datetime(2026, 8, 1, 22, tzinfo=UTC),
        SESSION,
        "next_session_open",
        "pending",
        None,
        None,
    )


def _bar(ticker: str = "AAPL", close: str = "100") -> MarketBar:
    value = Decimal(close)
    return MarketBar(
        ticker,
        SESSION,
        value,
        value,
        value,
        value,
        "fixture",
        datetime(2026, 8, 3, 22, tzinfo=UTC),
        False,
    )


def _stage_short(ledger: PortfolioLedger) -> OrderIntent:
    intent = _intent("short")
    ledger.record_signal(_signal())
    ledger.stage_intent(intent)
    ledger.apply_fill(
        intent,
        Fill(
            "short-fill",
            intent.intent_id,
            "short",
            SESSION,
            datetime(2026, 8, 3, 13, 30, tzinfo=UTC),
            datetime(2026, 8, 3, 22, tzinfo=UTC),
            Decimal("100"),
            Decimal("99.900"),
            10,
            Decimal("1.0000"),
            Decimal("0"),
            Decimal("0"),
        ),
    )
    return intent


def test_cost_model_applies_exact_ten_bps_adversely_and_persists_zero_fees():
    model = PaperCostModel()
    open_at = datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
    run_at = datetime(2026, 8, 3, 22, tzinfo=UTC)

    buy = model.fill(_intent("buy"), Decimal("100"), open_at, run_at)
    cover = model.fill(_intent("cover"), Decimal("100"), open_at, run_at)
    sell = model.fill(_intent("sell"), Decimal("100"), open_at, run_at)
    short = model.fill(_intent("short"), Decimal("100"), open_at, run_at)

    assert buy.fill_price == Decimal("100.100")
    assert cover.fill_price == Decimal("100.100")
    assert sell.fill_price == Decimal("99.900")
    assert short.fill_price == Decimal("99.900")
    assert buy.slippage == Decimal("1.0000")
    assert buy.commission == buy.other_fees == Decimal("0.0000")
    assert (
        buy.fill_id
        == model.fill(_intent("buy"), Decimal("100"), open_at, run_at).fill_id
    )


def test_daily_borrow_and_financing_are_exactly_once_across_restart(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(path, COHORT, Decimal("5000"))
    try:
        _stage_short(ledger)
        borrow = ledger.accrue_borrow(
            SESSION, {"AAPL": _bar()}, {"AAPL": Decimal("0.365")}
        )
        assert borrow.amount == Decimal("1.0000")
        assert (
            ledger.accrue_borrow(SESSION, {"AAPL": _bar()}, {"AAPL": Decimal("0.365")})
            == borrow
        )
        financing = ledger.accrue_financing(SESSION, Decimal("0.10"))
        assert financing.amount == Decimal("0.0000")
    finally:
        ledger.close()

    reopened = PortfolioLedger(path, COHORT, Decimal("5000"))
    try:
        assert (
            reopened.accrue_borrow(
                SESSION, {"AAPL": _bar()}, {"AAPL": Decimal("0.365")}
            )
            == borrow
        )
        assert reopened.accrue_financing(SESSION, Decimal("0.10")) == financing
        assert reopened.account_state().cash == Decimal("5998.0000")
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM borrow_accruals"
            ).fetchone()[0]
            == 1
        )
        assert (
            reopened.connection.execute(
                "SELECT COUNT(*) FROM financing_accruals"
            ).fetchone()[0]
            == 1
        )
    finally:
        reopened.close()


def test_existing_short_missing_rate_uses_flagged_fallback_and_new_short_is_rejected(
    tmp_path,
):
    ledger = PortfolioLedger(tmp_path / "ledger.db", COHORT, Decimal("5000"))
    try:
        _stage_short(ledger)
        event = ledger.accrue_borrow(SESSION, {"AAPL": _bar()}, {"AAPL": None})
        assert event.amount == Decimal("0.8219")
        assert event.flagged is True
        assert tuple(
            ledger.connection.execute(
                "SELECT flagged, annual_rate FROM borrow_accruals"
            ).fetchone()
        ) == (1, "0.30")
    finally:
        ledger.close()

    with pytest.raises(ValueError, match="missing borrow rate"):
        validate_new_short_borrow_rate(None, Decimal("0.05"))
    with pytest.raises(ValueError, match="exceeds"):
        validate_new_short_borrow_rate(Decimal("0.051"), Decimal("0.05"))
    assert validate_new_short_borrow_rate(Decimal("0.05"), Decimal("0.05")) == Decimal(
        "0.05"
    )
