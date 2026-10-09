"""Cohort-specific diagnostic populations from immutable observation evidence."""
from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Iterable

from .calendar import XNYSCalendar
from .models import OutcomeRecord, SignalMetricRecord

ELIGIBILITY_FIELDS = ('journal_only', 'analysis_status', 'analysis_valid',
                      'analysis_admitted', 'non_actionable_reason', 'discovery_id')


def signal_eligibility(observation: dict | None, journal: dict | None = None) -> dict:
    """Read retained eligibility without retrospectively endorsing legacy rows."""
    signal = next((payload['signal'] for payload in (observation, journal)
                   if isinstance(payload, dict) and isinstance(payload.get('signal'), dict)), None)
    if signal is None:
        return dict(journal_only=False, analysis_status='legacy_unknown', analysis_valid=False,
                    analysis_admitted=False, non_actionable_reason='missing_eligibility_evidence', discovery_id='')
    metadata = signal.get('metadata') or {}
    required = (metadata.get('needs_llm_analysis') is True
                or metadata.get('analysis_status') in {'failed', 'unavailable'}
                or metadata.get('non_actionable_reason') == 'required_analysis_failed')
    optional = (metadata.get('analysis_type') in {'insider_activity', 'commodity_macro', 'ag_weather'}
                and metadata.get('deterministic_evidence_complete') is True)
    status = str(metadata.get('analysis_status') or ('not_required' if not required else 'unavailable'))
    return dict(journal_only=bool(signal.get('journal_only')), analysis_status=status,
                analysis_valid=(not required or optional or status == 'validated'),
                analysis_admitted=metadata.get('analysis_admitted', True) is True,
                non_actionable_reason=str(metadata.get('non_actionable_reason') or ''),
                discovery_id=str(metadata.get('discovery_id') or ''))


def is_actionable(signal: object) -> bool:
    return (getattr(signal, 'analysis_valid', False) is True
            and getattr(signal, 'analysis_admitted', False) is True
            and not getattr(signal, 'journal_only', True)
            and not getattr(signal, 'non_actionable_reason', '')
            and getattr(signal, 'direction', None) in {'long', 'short'})


def population_diagnostics(
    signals: Iterable[SignalMetricRecord], outcomes: Iterable[OutcomeRecord], *,
    selected_ids: set[str] | None = None, executed_ids: set[str] | None = None,
    as_of: date | None = None, holding_sessions: int = 5,
) -> dict:
    """Keep observable missingness and screen-only hypotheses out of valid accuracy."""
    unique = {signal.signal_id: signal for signal in signals}
    rows = {row.signal_id: row for row in outcomes if row.holding_sessions == holding_sessions
            and row.signal_id in unique}
    actionable = {key for key, signal in unique.items() if is_actionable(signal)}
    neutral = {key for key, signal in unique.items() if signal.direction == 'neutral'
               and signal.analysis_valid and signal.analysis_admitted
               and not signal.journal_only and not signal.non_actionable_reason}
    populations = {'all_observation': set(unique), 'validated_actionable': actionable,
                   'validated_neutral': neutral,
                   'provisional': set(unique) - actionable - neutral,
                   'committee_selected': set(selected_ids or ()) & set(unique),
                   'executed': set(executed_ids or ()) & set(unique)}
    calendar = XNYSCalendar()
    result = {'holding_sessions': holding_sessions, 'cohort_aggregation_prohibited': True,
              'analysis_status_counts': dict(sorted(Counter(signal.analysis_status for signal in unique.values()).items())),
              'analysis_admitted_count': sum(signal.analysis_admitted for signal in unique.values()),
              'journal_only_count': sum(signal.journal_only for signal in unique.values())}
    for name, identities in populations.items():
        valid = [rows[key] for key in identities if key in rows and rows[key].status == 'valid'
                 and unique[key].direction in {'long', 'short'} and rows[key].signed_return is not None]
        valid_count = sum(key in rows and rows[key].status == 'valid' for key in identities)
        invalid = sum(key in rows and rows[key].status == 'invalid' for key in identities)
        missing_mature = 0
        pending = 0
        for key in identities:
            if key in rows:
                pending += rows[key].status == 'pending'
                continue
            signal = unique[key]
            maturity = calendar.held_session(calendar.next_session(signal.reference_session), holding_sessions)
            if as_of is not None and maturity <= as_of:
                missing_mature += 1
            else:
                pending += 1
        hits = sum(row.signed_return > 0 for row in valid)
        result[name] = {'count': len(identities), 'signal_ids': sorted(identities),
                        'valid_outcome_count': valid_count,
                        'valid_directional_outcome_count': len(valid), 'hit_count': hits,
                        'invalid_outcome_count': invalid, 'pending_outcome_count': pending,
                        'missing_mature_outcome_count': missing_mature,
                        'directional_accuracy': hits / len(valid) if valid else None,
                        'outcome_coverage': valid_count / len(identities) if identities else None}
    result['provisional']['label'] = 'screen hypotheses only; not validated actionable predictions'
    return result
