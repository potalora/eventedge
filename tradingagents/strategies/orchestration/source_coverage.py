"""Exact-session strategy coverage, independent of portfolio accounting."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import date
import hashlib
import json
import re
from typing import Any

_HEALTHY = frozenset({'signals', 'legitimate_no_event'})
_DISABLED = 'disabled_by_policy'
_FAILURES = frozenset({'data_failure', 'strategy_defect', 'missing_health'})
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}')
_KEYS = frozenset({'health_id', 'epoch_id', 'session', 'policy_id', 'strategy', 'status', 'sources', 'affected_cohorts'})


def _safe_id(value: object) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def canonical_source_scope_limits(value: object) -> dict[str, dict]:
    """Allowlisted public scope facts, never provider prose or raw payloads."""
    if not isinstance(value, Mapping) or not set(value) <= {'usaspending', 'courtlistener'}:
        raise ValueError('source scope limits are invalid')
    output = {}
    for provider, row in value.items():
        fields = ({'policy', 'scope_sha256', 'counts', 'attribution_complete', 'coverage_basis'}
                  if provider == 'usaspending' else
                  {'policy', 'scope_sha256', 'target_manifest_sha256', 'coverage_basis', 'content_kind',
                   'issuer_count', 'case_count', 'docket_count', 'omitted_issuer_count',
                   'target_search_complete', 'marketwide_coverage'})
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError('source scope fields are invalid')
        if any(not isinstance(row[key], str) or re.fullmatch('[a-f0-9]{64}', row[key]) is None
               for key in fields if key.endswith('sha256')):
            raise ValueError('source scope digest is invalid')
        if provider == 'usaspending':
            counts = row['counts']
            if (row['policy'] != 'verified_listed_targets_v1'
                    or row['coverage_basis'] != 'verified_listed_targets'
                    or not isinstance(counts, dict)
                    or set(counts) != {'verified_listed_target', 'verified_no_listed_target', 'unresolved'}
                    or any(type(n) is not int or not 0 <= n <= 2**63-1 for n in counts.values())
                    or type(row['attribution_complete']) is not bool
                    or row['attribution_complete'] is not (counts['unresolved'] == 0)):
                raise ValueError('source scope attribution is contradictory')
        elif (row['policy'] != 'focused_litigation_v1'
                or row['coverage_basis'] != 'declared_issuer_and_case_queries'
                or row['content_kind'] != 'docket_metadata_only'
                or row['target_search_complete'] is not (row['omitted_issuer_count'] == 0)
                or row['marketwide_coverage'] is not False
                or any(type(row[key]) is not int or not 0 <= row[key] <= 2**63-1
                       for key in ('issuer_count', 'case_count', 'docket_count', 'omitted_issuer_count'))):
            raise ValueError('source scope litigation is contradictory')
        output[provider] = deepcopy(row)
    return dict(sorted(output.items()))


def source_scope_limits_from_health(records) -> dict[str, dict]:
    combined = {}
    for record in records:
        evidence = record.get('evidence', {}) if isinstance(record, Mapping) else record.evidence
        if 'source_scope_limits' not in evidence:
            continue
        scoped = canonical_source_scope_limits(evidence['source_scope_limits'])
        sources = evidence.get('data_sources')
        errors = evidence.get('provider_errors', {})
        status = record.get('status') if isinstance(record, Mapping) else record.status
        if (not isinstance(sources, (list, tuple)) or any(not isinstance(source, str) for source in sources)
                or not isinstance(errors, Mapping) or status == _DISABLED
                or any(provider not in sources or provider in errors for provider in scoped)):
            raise ValueError('source scope limits lack matching source provenance')
        for provider, summary in scoped.items():
            if provider in combined and combined[provider] != summary:
                raise ValueError('source scope limits conflict across durable health')
            combined[provider] = summary
    return dict(sorted(combined.items()))


def aggregate_source_scope_limits(results: dict) -> dict[str, dict]:
    combined = {}
    for result in results.values():
        if not isinstance(result, dict):
            continue
        for provider, summary in canonical_source_scope_limits(result.get('source_scope_limits', {})).items():
            if provider in combined and combined[provider] != summary:
                raise ValueError('source scope limits conflict across cohort results')
            combined[provider] = summary
    if combined and any(isinstance(result, dict) and not result.get('error')
                        and canonical_source_scope_limits(result.get('source_scope_limits', {})) != combined
                        for result in results.values()):
        raise ValueError('source scope limits missing from completed cohort')
    return dict(sorted(combined.items()))


def apply_source_coverage(state: Any, results: dict[str, Any]) -> None:
    """Read durable health, including on a fully completed same-session resume."""
    owner = state.owner
    strategies = getattr(owner, '_active_strategy_names', None)
    if strategies is None:
        return  # The pure result helper may be used without an orchestration context.
    records = () if state.epoch_id is None else owner._metric_store.read_strategy_health(
        state.epoch_id, session=state.session, limit=1000
    )
    disabled = getattr(owner, '_disabled_strategies', {})
    by_scope: dict[tuple[str, str], Any] = {}
    for record in records:
        key = (record.policy_id, record.strategy)
        if (record.epoch_id != state.epoch_id or record.session != state.session
                or key in by_scope or record.status not in _HEALTHY | (_FAILURES - {'missing_health'}) | {_DISABLED}):
            raise ValueError('source coverage durable health is invalid')
        if record.status == _DISABLED and (
            disabled.get(record.strategy) != record.evidence.get('reason')
            or record.strategy not in disabled or record.signal_count != 0
        ):
            raise ValueError('disabled strategy health does not match configured policy')
        if record.strategy in disabled and record.status != _DISABLED:
            raise ValueError('disabled strategy health is missing its policy exclusion')
        by_scope[key] = record
    cohorts_by_policy: dict[str, list[str]] = {}
    for cohort in owner.cohorts:
        cfg = cohort['config']
        if cfg.name in results:
            cohorts_by_policy.setdefault(owner._policy_id_for_horizon(cfg.horizon), []).append(cfg.name)
    for policy, names in sorted(cohorts_by_policy.items()):
        scope_limits = source_scope_limits_from_health(
            record for (bound_policy, _), record in by_scope.items() if bound_policy == policy)
        references = []
        for strategy in sorted(strategies):
            record = by_scope.get((policy, strategy))
            if record is not None and record.status in _HEALTHY | {_DISABLED}:
                continue
            if state.epoch_id is None:
                continue
            missing_id = hashlib.sha256(json.dumps([state.epoch_id, str(state.session), policy, strategy]).encode()).hexdigest()[:32]
            errors = record.evidence.get('provider_errors', {}) if record else {}
            references.append({
                'health_id': record.health_id if record else 'missing_health_' + missing_id,
                'epoch_id': state.epoch_id, 'session': state.session.isoformat(),
                'policy_id': policy, 'strategy': strategy,
                'status': record.status if record else 'missing_health',
                # Never copy provider exception text into the worker/manifest wire.
                'sources': sorted(source for source in errors if _safe_id(source)) if isinstance(errors, Mapping) else [],
                'affected_cohorts': sorted(names),
            })
        for name in names:
            result = results[name]
            if not isinstance(result, dict):
                continue
            if 'source_scope_limits' in result and canonical_source_scope_limits(result['source_scope_limits']) != scope_limits:
                raise ValueError('source scope limits differ from durable health')
            if scope_limits:
                result['source_scope_limits'] = deepcopy(scope_limits)
            result['input_coverage_valid'] = state.epoch_id is not None and not references
            result['source_health_failures'] = references
            if disabled:
                result['disabled_strategies'] = dict(sorted(disabled.items()))
            if references and result.get('execution_valid') is True:
                result['degraded'] = True
    for result in results.values():
        if isinstance(result, dict):
            result.setdefault('input_coverage_valid', False)
            result.setdefault('source_health_failures', [])


def canonical_source_health_failures(value: object, session: str | None = None) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)) or len(value) > 256:
        raise ValueError('source coverage references are invalid')
    by_id = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != _KEYS:
            raise ValueError('source coverage reference fields are invalid')
        if any(not _safe_id(item[key]) for key in ('health_id', 'epoch_id', 'policy_id', 'strategy')):
            raise ValueError('source coverage reference identity is invalid')
        try:
            exact_session = date.fromisoformat(item['session']).isoformat()
        except (TypeError, ValueError):
            raise ValueError('source coverage reference session is invalid') from None
        if exact_session != item['session'] or session and exact_session != session:
            raise ValueError('source coverage reference session mismatch')
        if item['status'] not in _FAILURES:
            raise ValueError('source coverage reference status is invalid')
        for key, maximum in (('sources', 32), ('affected_cohorts', 64)):
            values = item[key]
            if (not isinstance(values, list) or len(values) > maximum
                    or any(not _safe_id(text) for text in values)
                    or values != sorted(set(values))):
                raise ValueError('source coverage reference scope is invalid')
        if not item['affected_cohorts']:
            raise ValueError('source coverage reference has empty scope')
        identity = item['health_id']
        if identity in by_id and by_id[identity] != item:
            raise ValueError('source coverage references conflict')
        by_id[identity] = dict(item)
    return sorted(by_id.values(), key=lambda item: (item['policy_id'], item['strategy'], item['health_id']))


def aggregate_source_health_failures(results: dict, session: str | None = None, *, require_coverage: bool = False) -> list[dict[str, object]]:
    references = []
    for name, result in results.items():
        if not isinstance(result, dict):
            continue
        rows = canonical_source_health_failures(result.get('source_health_failures', []), session)
        if (require_coverage and not result.get('error') or 'input_coverage_valid' in result) and type(result.get('input_coverage_valid')) is not bool:
            raise ValueError('source coverage validity is missing')
        if rows:
            if result.get('input_coverage_valid') is not False or result.get('degraded') is not True and not result.get('error'):
                raise ValueError('source coverage failure carrier is contradictory')
            if any(name not in row['affected_cohorts'] or not set(row['affected_cohorts']) <= results.keys() for row in rows):
                raise ValueError('source coverage failure scope is contradictory')
        if result.get('input_coverage_valid') is False and not result.get('error') and (not rows or result.get('degraded') is not True):
            raise ValueError('source coverage incomplete without failure evidence')
        references.extend(rows)
    combined = canonical_source_health_failures(references, session)
    for row in combined:
        for name in row['affected_cohorts']:
            if row not in results[name].get('source_health_failures', []):
                raise ValueError('source coverage failure is missing from affected cohort')
    return combined
