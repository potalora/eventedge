"""Explicit verified-target scope over a complete, unchanged award population."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json
from typing import Any

from .award_identity import CROSSWALK_VERSION, native_uei, resolve_award_issuer
from .fetch_errors import SourceFetchError, source_date, source_number, source_text

POLICY = 'verified_listed_targets_v1'
STATUSES = ('verified_listed_target', 'verified_no_listed_target', 'unresolved')


def _require(condition: bool) -> None:
    if not condition:
        raise SourceFetchError('USASpending verified-target scope evidence invalid', reason_code='invalid_response')


def _digest(value: Any) -> str:
    try:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                        allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError):
        raise SourceFetchError('USASpending award evidence invalid', reason_code='invalid_response') from None


def _acquisition_failed(value: Any) -> bool:
    if isinstance(value, dict):
        return (('complete' in value and value['complete'] is not True)
                or value.get('status') in {'failed', 'partial', 'incomplete', 'unavailable'}
                or bool(value.get('error'))
                or any(_acquisition_failed(item) for item in value.values()))
    return isinstance(value, (list, tuple)) and any(_acquisition_failed(item) for item in value)


def build_attribution_scope(records: list[dict], coverage: dict, *, session: str) -> dict:
    """Recompute every disposition; acquisition completeness is a separate fact.

    No unknown becomes a proved universe exclusion. Recipient-detail failures
    remain metadata failures, rather than successful-but-unmapped identities.
    """
    _require(isinstance(records, list) and isinstance(coverage, dict) and source_date(session))
    _require(not _acquisition_failed({key: value for key, value in coverage.items()
                                      if key not in {'issuer_attribution', 'attribution_scope'}}))
    start = (date.fromisoformat(session) - timedelta(days=30)).isoformat()
    _require(coverage.get('mode') == 'exhaustive_window' and coverage.get('complete') is True
             and type(coverage.get('returned')) is int and coverage['returned'] == len(records)
             and type(coverage.get('pages')) is int and coverage['pages'] > 0
             and coverage.get('date_from') == start and coverage.get('date_to') == session
             and coverage.get('date_type') == 'new_awards_only'
             and coverage.get('amount_basis') == 'cumulative_award_obligations')
    awards, seen = [], set()
    counts = dict.fromkeys(STATUSES, 0)
    for row in records:
        _require(isinstance(row, dict))
        key = row.get('award_key')
        _require(source_text(key) and key not in seen and source_text(row.get('award_id'))
                 and source_text(row.get('recipient_name'))
                 and row.get('award_scope') == 'new_awards_only'
                 and row.get('amount_basis') == 'cumulative_award_obligations'
                 and source_number(row.get('amount'), minimum=0)
                 and source_date(row.get('base_obligation_date'))
                 and start <= row['base_obligation_date'] <= session)
        try:
            observed = datetime.fromisoformat(row.get('observed_at', ''))
            _require(observed.tzinfo is not None and observed.utcoffset() is not None)
        except (TypeError, ValueError):
            _require(False)
        status = row.get('recipient_identity_status')
        _require(status in {'search_recipient', 'native_award_verified', 'missing_native_recipient'})
        _require(all(not row.get(field) or native_uei(row[field]) == row[field]
                     for field in ('recipient_uei', 'parent_recipient_uei')))
        if status == 'native_award_verified':
            _require(bool(native_uei(row.get('recipient_uei'))) and source_text(row.get('recipient_identity_source')))
        attribution = resolve_award_issuer(row)
        disposition = attribution['status']
        counts[disposition] += 1
        seen.add(key)
        awards.append({'award_key': key, 'award_id': row['award_id'],
            'recipient_uei': row.get('recipient_uei', ''), 'parent_recipient_uei': row.get('parent_recipient_uei', ''),
            'recipient_identity_status': status, 'status': disposition,
            'attribution': attribution})
    evidence = {'policy': POLICY, 'crosswalk_version': CROSSWALK_VERSION,
        'session': session, 'acquisition_complete': True,
        'attribution_complete': counts['unresolved'] == 0,
        'verified_listed_scope_complete': True,
        'counts': counts, 'source_rows_sha256': _digest(records),
        'awards': sorted(awards, key=lambda row: row['award_key'])}
    return {**evidence, 'scope_sha256': _digest(evidence)}


def apply_attribution_policy(records: list[dict], coverage: dict, *, session: str) -> dict:
    """Describe acquisition success without overwriting original identity evidence."""
    scope = build_attribution_scope(records, coverage, session=session)
    acquisition = deepcopy(coverage)
    original_attribution = acquisition.pop('issuer_attribution', None)
    acquisition['attribution_scope'] = scope
    return {'award_attribution_policy': POLICY, 'coverage': acquisition,
            'issuer_attribution_evidence': deepcopy(original_attribution)}


def validate_attribution_scope(payload: dict, *, session: str) -> dict:
    """Independently validate frozen/cached policy evidence against all raw rows."""
    _require(isinstance(payload, dict) and payload.get('award_attribution_policy') == POLICY
             and payload.get('error') in (None, '') and isinstance(payload.get('data'), dict))
    coverage = payload.get('coverage')
    _require(isinstance(coverage, dict))
    expected = build_attribution_scope(payload['data'].get('contracts'), coverage, session=session)
    _require(coverage.get('attribution_scope') == expected
             and payload['data'].get('coverage') == coverage)
    return expected
