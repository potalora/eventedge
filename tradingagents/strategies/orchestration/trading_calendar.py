"""Trading calendar utility for resolving dates to trading days."""
from __future__ import annotations

from datetime import datetime

import numpy as np


# US market holidays (NYSE/NASDAQ) for 2025-2027.
_US_MARKET_HOLIDAYS = [
    # 2025
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01",
    "2025-11-27", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
    "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06",
    "2027-11-25", "2027-12-24",
]

_HOLIDAYS = np.busdaycalendar(
    weekmask="1111100",
    holidays=[np.datetime64(d) for d in _US_MARKET_HOLIDAYS],
)


def resolve_trading_date(date_str: str | None = None) -> str:
    """Resolve a date to the most recent US market trading day.

    - Weekdays that are not holidays pass through unchanged.
    - Weekends roll back to the preceding Friday (or Thursday if Friday is a holiday).
    - Holidays roll back to the previous trading day.
    - If date_str is None, uses today.

    Returns:
        YYYY-MM-DD string for the resolved trading day.
    """
    if date_str is None:
        dt = datetime.now()
    else:
        dt = datetime.strptime(date_str, "%Y-%m-%d")

    d = np.datetime64(dt.strftime("%Y-%m-%d"))
    resolved = np.busday_offset(d, 0, roll="preceding", busdaycal=_HOLIDAYS)
    return str(resolved)
