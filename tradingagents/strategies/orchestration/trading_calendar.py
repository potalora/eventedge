"""XNYS trading-session utilities used by the authoritative execution clock."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import exchange_calendars
import pandas as pd


_XNYS = exchange_calendars.get_calendar("XNYS")
_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")


def _label(session: date) -> pd.Timestamp:
    """Return a UTC-normalized, timezone-naive exchange session label.

    exchange-calendars 4.x requires a naive midnight label.  Constructing the
    label in UTC first prevents a local timezone from changing its calendar day
    before removing the timezone for that API boundary.
    """
    return pd.Timestamp(session, tz=_UTC).tz_localize(None)


def is_session(session: date) -> bool:
    """Whether *session* is an XNYS trading session."""
    return bool(_XNYS.is_session(_label(session)))


def next_session(session: date) -> date:
    """Return the XNYS session after *session*, or the next session on/after it."""
    label = _label(session)
    if is_session(session):
        return _XNYS.next_session(label).date()
    return _XNYS.date_to_session(label, direction="next").date()


def previous_session(session: date) -> date:
    """Return the XNYS session before *session*, or the prior session on/before it."""
    label = _label(session)
    if is_session(session):
        return _XNYS.previous_session(label).date()
    return _XNYS.date_to_session(label, direction="previous").date()


def session_open(session: date) -> datetime:
    """Return the exact UTC opening timestamp for an XNYS session."""
    if not is_session(session):
        raise ValueError(f"{session} is not an XNYS session")
    return _XNYS.session_open(_label(session)).to_pydatetime()


def session_close(session: date) -> datetime:
    """Return the exact UTC closing timestamp for an XNYS session."""
    if not is_session(session):
        raise ValueError(f"{session} is not an XNYS session")
    return _XNYS.session_close(_label(session)).to_pydatetime()


def resolve_trading_date(date_str: str | None = None) -> str:
    """Resolve a date to the current or prior XNYS session in New York time."""
    local = datetime.now(_ET).date() if date_str is None else date.fromisoformat(date_str)
    if is_session(local):
        return local.isoformat()
    return _XNYS.date_to_session(_label(local), direction="previous").date().isoformat()
