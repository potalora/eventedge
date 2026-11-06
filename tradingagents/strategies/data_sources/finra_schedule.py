"""Reviewed FINRA publication dates, separate from cache and symbol coverage.

The source provides dates, not release timestamps. A cycle is eligible only on
the following New York calendar day. This intentionally conservative boundary
cannot turn a midnight or ambiguous same-day observation into publication proof.
Refresh this reviewed calendar before using it outside calendar year 2026.
"""
from __future__ import annotations

from datetime import date, datetime
import re
from zoneinfo import ZoneInfo

from .fetch_errors import SourceFetchError

SCHEDULE_SOURCE = 'https://www.finra.org/filing-reporting/regulatory-filing-systems/short-interest'
SCHEDULE_VERSION = 'finra_publication_dates_reviewed_2026-10-22'
PUBLICATION_POLICY = 'date_only_following_new_york_day_v1'
_NEW_YORK = ZoneInfo('America/New_York')
# Late 2025 carryover plus the published 2026 table. No extrapolated holidays.
_PUBLICATIONS = dict(pair.split() for pair in '''
2025-11-14 2025-11-25
2025-11-28 2025-12-09
2025-12-15 2025-12-24
2025-12-31 2026-01-12
2026-01-15 2026-01-27
2026-01-30 2026-02-10
2026-02-13 2026-02-25
2026-02-27 2026-03-10
2026-03-13 2026-03-24
2026-03-31 2026-04-10
2026-04-15 2026-04-24
2026-04-30 2026-05-11
2026-05-15 2026-05-27
2026-05-29 2026-06-09
2026-06-15 2026-06-25
2026-06-30 2026-07-10
2026-07-15 2026-07-24
2026-07-31 2026-08-11
2026-08-14 2026-08-25
2026-08-31 2026-09-10
2026-09-15 2026-09-24
2026-09-30 2026-10-09
2026-10-15 2026-10-26
2026-10-30 2026-11-10
2026-11-13 2026-11-24
2026-11-30 2026-12-09
2026-12-15 2026-12-24
2026-12-31 2027-01-12
'''.strip().splitlines())
# SDK 1.6.1 adjusts weekends but misses these holidays. The corrected dates are
# independently corroborated by Nasdaq's official 2024 settlement announcements.
_SDK_CORRECTIONS = {'2024-01-15': '2024-01-12', '2024-03-29': '2024-03-28'}


def _invalid():
    return SourceFetchError('Unsupported or invalid FINRA publication evidence', reason_code='invalid_response')


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _invalid()
    return value


def _context(*, acquired_at, as_of=None):
    acquired_at = _aware(acquired_at)
    observed_day = acquired_at.astimezone(_NEW_YORK).date()
    if as_of is None:
        reference_day = observed_day
    elif isinstance(as_of, datetime):
        reference_day = _aware(as_of).astimezone(_NEW_YORK).date()
    elif type(as_of) is date:
        reference_day = as_of
    else:
        raise _invalid()
    effective = min(observed_day, reference_day)
    if observed_day.year != 2026 or effective.year != 2026:
        raise _invalid()
    eligible = sorted(day for day, published in _PUBLICATIONS.items() if published < effective.isoformat())
    required = eligible[-1]
    return {
        'required_settlement_date': required,
        'publication_date': _PUBLICATIONS[required],
        'effective_as_of': effective.isoformat(),
        'acquired_at': acquired_at.isoformat(),
        'publication_day_ambiguous': effective.isoformat() in _PUBLICATIONS.values(),
        'publication_policy': PUBLICATION_POLICY,
        'schedule_version': SCHEDULE_VERSION,
        'schedule_source': SCHEDULE_SOURCE,
    }


def eligible_settlement_dates(*, acquired_at: datetime, as_of: date | datetime | None = None) -> list[str]:
    """Return reviewed cycles published strictly before both reference clocks."""
    context = _context(acquired_at=acquired_at, as_of=as_of)
    return sorted(day for day, published in _PUBLICATIONS.items() if published < context['effective_as_of'])


def validate_current_settlement(settlement_date: str | date | None, *, acquired_at: datetime,
                                as_of: date | datetime | None = None) -> dict:
    """Classify missing/stale/current rows without treating old rows as zero."""
    context = _context(acquired_at=acquired_at, as_of=as_of)
    if settlement_date is None:
        return {**context, 'status': 'missing'}
    try:
        day = date.fromisoformat(str(settlement_date)).isoformat()
    except (ValueError, TypeError):
        raise _invalid() from None
    if day >= min(_PUBLICATIONS) and day not in _PUBLICATIONS:
        raise _invalid()
    required = context['required_settlement_date']
    status = 'current' if day == required else 'stale' if day < required else 'not_yet_available'
    return {**context, 'status': status}


def correct_archive_dates(sdk_dates: list[str], *, acquired_at: datetime,
                          as_of: date | datetime | None = None) -> list[str]:
    """Correct inspected SDK holidays and include the published current cycle.

    Historical inventory is the SDK's requested history, not an invented proof
    that all historical exchange coverage is complete. Current-cycle freshness
    is independently required even when the SDK omits the entire current month.
    """
    context = _context(acquired_at=acquired_at, as_of=as_of)
    if type(sdk_dates) is not list or len(sdk_dates) > 1024:
        raise _invalid()
    corrected = set()
    for compact in sdk_dates:
        if type(compact) is not str or not re.fullmatch(r'[0-9]{8}', compact):
            raise _invalid()
        try:
            day = date.fromisoformat(f'{compact[:4]}-{compact[4:6]}-{compact[6:]}').isoformat()
        except ValueError:
            raise _invalid() from None
        day = _SDK_CORRECTIONS.get(day, day)
        if day >= min(_PUBLICATIONS):
            if day not in _PUBLICATIONS:
                raise _invalid()
            if _PUBLICATIONS[day] >= context['effective_as_of']:
                continue
        corrected.add(day)
    corrected.add(context['required_settlement_date'])
    return sorted(corrected)


def assess_archive_inventory(required: list[str], cached: list[str], *, acquired_at: datetime,
                             as_of: date | datetime | None = None) -> dict:
    """Keep a present latest archive distinct from gaps in requested history."""
    context = _context(acquired_at=acquired_at, as_of=as_of)
    missing = set(required) - set(cached)
    latest = context['required_settlement_date']
    historical = sorted(missing - {latest})
    return {**context, 'current_complete': latest in cached,
            'historical_complete': not historical,
            'history_scope': 'sdk_requested_archives_with_reviewed_holiday_corrections',
            'missing_historical_archive_dates': historical}
