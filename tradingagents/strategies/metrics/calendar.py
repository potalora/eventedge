from __future__ import annotations

from datetime import date, datetime

import exchange_calendars
import pandas as pd


class XNYSCalendar:
    def __init__(self) -> None:
        self._calendar = exchange_calendars.get_calendar("XNYS")

    @staticmethod
    def _timestamp(session: date) -> pd.Timestamp:
        return pd.Timestamp(session.isoformat())

    def is_session(self, session: date) -> bool:
        return bool(self._calendar.is_session(self._timestamp(session)))

    def next_session(self, session: date) -> date:
        current = self._calendar.date_to_session(
            self._timestamp(session), direction="previous"
        )
        return self._calendar.next_session(current).date()

    def previous_session(self, session: date) -> date:
        current = self._calendar.date_to_session(
            self._timestamp(session), direction="next"
        )
        return self._calendar.previous_session(current).date()

    def held_session(self, entry_session: date, holding_sessions: int) -> date:
        if holding_sessions <= 0:
            raise ValueError("holding_sessions must be positive")
        if not self.is_session(entry_session):
            raise ValueError(f"{entry_session} is not an XNYS session")
        window = self._calendar.sessions_window(
            self._timestamp(entry_session), holding_sessions
        )
        return window[-1].date()

    def session_close(self, session: date) -> datetime:
        if not self.is_session(session):
            raise ValueError(f"{session} is not an XNYS session")
        return self._calendar.session_close(
            self._timestamp(session)
        ).to_pydatetime()
