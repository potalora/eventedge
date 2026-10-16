"""Acquisition clocks and explicit collection scopes shared by source adapters."""
from datetime import date, datetime, timezone
from typing import Any

from .fetch_errors import SourceFetchError


def current_session_date() -> str:
    from tradingagents.strategies.orchestration.trading_calendar import exchange_date
    return exchange_date().isoformat()


def acquisition_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_current_as_of(as_of: str, today: str) -> None:
    """Latest mutable provider data cannot reconstruct past information sets."""
    if as_of[:10] != today:
        raise SourceFetchError('historical_vintage_unavailable: fresh acquisition requires current as-of date',
                               reason_code='provider_error')


def require_current_vintage(observation_end: str, vintage_as_of: str | None = None,
                            *, today: str | None = None) -> str:
    """Keep observation dates separate from the real mutable dataset vintage."""
    vintage = observation_end if vintage_as_of is None else vintage_as_of
    require_current_as_of(vintage, today or current_session_date())
    try:
        end, version = date.fromisoformat(observation_end), date.fromisoformat(vintage)
    except (TypeError, ValueError):
        raise SourceFetchError('invalid observation window or vintage', reason_code='invalid_response') from None
    if end > version:
        raise SourceFetchError('observation window is after current vintage', reason_code='invalid_response')
    return vintage


class CoverageRecords(list):
    """List-compatible records; callers MUST persist coverage in the envelope."""
    def __init__(self, records=(), *, coverage: dict[str, Any]):
        super().__init__(records)
        self.coverage = coverage


class CoverageMapping(dict):
    """Mapping-compatible collection; persist coverage separately before freezing."""
    def __init__(self, records=(), *, coverage: dict[str, Any]):
        super().__init__(records)
        self.coverage = coverage


def collection_envelope(records, key='data') -> dict[str, Any]:
    out = {key: records}
    if hasattr(records, 'coverage'):
        out['coverage'] = records.coverage
    return out


def bounded_coverage(*, returned: int, limit: int, total=None, has_next=None, **scope) -> dict:
    # A first page is never evidence of exhaustive event-window coverage.
    return {'mode':'bounded_sample', 'complete':False, 'returned':returned, 'limit':limit,
            'provider_total':total, 'has_next':has_next, **scope}
