from datetime import UTC, date, datetime

import pytest

from tradingagents.strategies.metrics.calendar import XNYSCalendar


def test_next_session_skips_mlk_holiday() -> None:
    calendar = XNYSCalendar()
    assert calendar.next_session(date(2026, 1, 16)) == date(2026, 1, 20)
    assert calendar.previous_session(date(2026, 1, 20)) == date(2026, 1, 16)


def test_held_session_counts_entry_as_first_held_session() -> None:
    calendar = XNYSCalendar()
    entry = date(2026, 1, 16)
    assert calendar.held_session(entry, 5) == date(2026, 1, 23)


def test_black_friday_close_is_early() -> None:
    calendar = XNYSCalendar()
    assert calendar.session_close(date(2026, 11, 27)) == datetime(
        2026, 11, 27, 18, 0, tzinfo=UTC
    )


def test_held_session_rejects_nonpositive_window() -> None:
    with pytest.raises(ValueError, match="holding_sessions must be positive"):
        XNYSCalendar().held_session(date(2026, 1, 16), 0)
