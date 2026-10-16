"""Receipt-backed operational notifications; no trading, repair, or transport authority.

The caller supplies canonical expected identity, recipient route, and a synchronous
transport. Only an explicit confirmed receipt acknowledges an event. Uncertain
delivery requires manual reconciliation: this module never automatically resends it.
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

from .operational_report import _publish, render_operational_report
from .runtime_lock import runtime_lock
from .source_incidents import MAX_REPORT_BYTES, POLICY, _PROVIDERS, _SAFE_ID, _identity

STATE_POLICY = 'source_notifications_v1'
PREFLIGHT_STATE_POLICY = 'preflight_runtime_notifications_v1'
MAX_STATE_BYTES = 1024 * 1024
MAX_EVENTS = 2048
RETENTION_DAYS = 90
_DIGEST = re.compile(r'[0-9a-f]{64}')
_KINDS = frozenset({'opened', 'escalated', 'recovered', 'observation_lost'})


@dataclass(frozen=True)
class DeliveryReceipt:
    """A transport-confirmed message ID, proven rejection, or uncertain result.

    ``rejected`` must mean the transport proves no message was accepted. Timeouts,
    connection loss after submission, and absent receipts are ``unknown``.
    """
    status: str
    receipt_id: str | None = None


def _bounded_json(path, limit):
    with Path(path).open('rb') as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError('bounded JSON exceeded')
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = item
        return result
    return json.loads(value, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def _read_report(folder, generation, commit, session):
    """Read an exact bounded pair and return only allowlisted event fields."""
    stem = Path(folder) / f'{session}-{generation}-operational-report'
    try:
        report = _bounded_json(stem.with_suffix('.json'), MAX_REPORT_BYTES)
        _identity(report)
        if (report['generation_id'], report['generation_commit'], report['session']) != (generation, commit, session):
            return 'identity_mismatch', []
        summary = report['source_incidents']
        if (summary['policy'] != POLICY or summary['assessment_status'] != 'available'
                or summary['history_status'] not in {'missing', 'invalid', 'available'}
                or not isinstance(summary['configuration'], str)
                or not _SAFE_ID.fullmatch(summary['configuration'])
                or summary['configuration'] != report['sources']['configuration']):
            return 'policy_unavailable', []
        alerts, opened = summary['alerts'], summary['open_incidents']
        if (not isinstance(alerts, list) or len(alerts) > len(_PROVIDERS)
                or not isinstance(opened, list) or len(opened) > len(_PROVIDERS)):
            raise ValueError('invalid source notification contract')
        seen = set()
        for alert in alerts:
            if (not isinstance(alert, dict) or set(alert) != {'kind', 'provider', 'alert_id'}
                    or alert['kind'] not in _KINDS or alert['provider'] not in _PROVIDERS
                    or not isinstance(alert['alert_id'], str) or not _DIGEST.fullmatch(alert['alert_id'])
                    or alert['alert_id'] in seen):
                raise ValueError('invalid source alert')
            seen.add(alert['alert_id'])
        if any(not isinstance(row, dict) or row.get('provider') not in _PROVIDERS
               or row.get('status') not in {'degraded', 'repair_required'} for row in opened):
            raise ValueError('invalid source incident')
        with stem.with_suffix('.md').open('rb') as stream:
            markdown = stream.read(MAX_REPORT_BYTES + 1)
        if len(markdown) > MAX_REPORT_BYTES or markdown.decode('utf-8') != render_operational_report(report):
            return 'markdown_mismatch', []
        return 'available', alerts
    except FileNotFoundError:
        return 'missing', []
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return 'invalid', []


def _event(scope, session, kind, *, test, provider=None, alert_id=None, gap=None):
    event_id = alert_id or hashlib.sha256(json.dumps(
        [STATE_POLICY, scope, session, kind, gap], separators=(',', ':')).encode()).hexdigest()
    prefix = '[TEST] ' if test else ''
    category = 'preflight runtime' if scope.get('observation') == 'preflight_runtime' else 'source'
    text = (f'{prefix}EventEdge {category} notification: {kind}; generation={scope["generation"]}; '
            f'session={session}; provider={provider or "report"}; event_id={event_id}. '
            'Observation only; no trading or repair action authorized.')
    return {'event_id': event_id, 'alert_id': alert_id, 'kind': kind, 'provider': provider,
            'generation_id': scope['generation'], 'generation_commit': scope['commit'],
            'session': session, 'test': test, 'text': text}


def _valid_state(state, scope):
    preflight = scope.get('observation') == 'preflight_runtime'
    policy = PREFLIGHT_STATE_POLICY if preflight else STATE_POLICY
    if (not isinstance(state, dict) or state.get('policy') != policy
            or not isinstance(state.get('scope'), dict)):
        return 'invalid'
    if state['scope'] != scope:
        return 'identity_mismatch'
    try:
        date.fromisoformat(state['latest_session'])
        entries = state['events']
        if not isinstance(entries, dict) or len(entries) > MAX_EVENTS:
            return 'invalid'
        gap = state['report_gap']
        if gap is not None:
            date.fromisoformat(gap)
        for event_id, entry in entries.items():
            if (not _DIGEST.fullmatch(event_id) or entry['status'] not in {'confirmed', 'rejected', 'unknown'}
                    or entry['event']['event_id'] != event_id):
                return 'invalid'
            event = entry['event']
            day = date.fromisoformat(event['session']).isoformat()
            if preflight and event['kind'] in {'preflight_failed', 'preflight_recovered'}:
                if (event != _preflight_event(scope, day, event['kind'], event['observation'])
                        or entry['status'] == 'confirmed' and
                        (not isinstance(entry['receipt_id'], str) or not _SAFE_ID.fullmatch(entry['receipt_id']))):
                    return 'invalid'
                continue
            kind, provider = event['kind'], event['provider']
            if (set(event) != {'event_id', 'alert_id', 'kind', 'provider', 'generation_id',
                              'generation_commit', 'session', 'test', 'text'}
                    or event['generation_id'] != scope['generation']
                    or event['generation_commit'] != scope['commit'] or event['test'] is not scope['test']
                    or kind not in _KINDS | {'report_unavailable', 'report_recovered'}
                    or preflight and kind not in {'report_unavailable', 'report_recovered'}
                    or kind in _KINDS and (provider not in _PROVIDERS or event['alert_id'] != event_id)
                    or kind not in _KINDS and (provider is not None or event['alert_id'] is not None)
                    or event['text'] != _event(scope, day, kind, test=scope['test'],
                                              provider=provider, alert_id=event_id)['text']
                    or entry['status'] == 'confirmed' and
                    (not isinstance(entry['receipt_id'], str) or not _SAFE_ID.fullmatch(entry['receipt_id']))):
                return 'invalid'
        return 'available'
    except (KeyError, TypeError, ValueError, AttributeError):
        return 'invalid'


def _consume_notifications(report_dir, state_path, *, expected_generation,
        expected_commit, expected_session, recipient_route, send, test=True,
        preflight_scope=None):
    """Consume one expected report and retry proven rejections under a state lock.

    ``state_path`` must be separate from report/production state. The caller owns
    scheduling and target validation. Never reset or edit an unknown entry to retry
    it without first reconciling the destination's message history.
    """
    if (not isinstance(expected_generation, str) or not _SAFE_ID.fullmatch(expected_generation)
            or not isinstance(expected_commit, str) or not _SAFE_ID.fullmatch(expected_commit)
            or not isinstance(recipient_route, str) or not recipient_route or len(recipient_route) > 4096
            or type(test) is not bool):
        raise ValueError('invalid source notification arguments')
    _identity({'generation_id': expected_generation, 'generation_commit': expected_commit, 'session': expected_session})
    scope = {'generation': expected_generation, 'commit': expected_commit, 'test': test,
             'recipient_hash': hashlib.sha256(recipient_route.encode()).hexdigest()}
    if preflight_scope is not None:
        scope.update(preflight_scope)
    path = Path(state_path)
    result = {'state_status': 'available', 'report_status': 'not_read',
              'delivered': [], 'suppressed': [], 'retryable': [], 'unknown': []}
    with runtime_lock(path.with_name(path.name + '.lock'), exclusive=True):
        try:
            state = _bounded_json(path, MAX_STATE_BYTES)
        except FileNotFoundError:
            state = {'policy': PREFLIGHT_STATE_POLICY if preflight_scope else STATE_POLICY,
                     'scope': scope, 'latest_session': expected_session,
                     'report_gap': None, 'events': {}}
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
            result['state_status'] = 'invalid'
            return result
        state_status = _valid_state(state, scope)
        if state_status != 'available':
            result['state_status'] = state_status
            return result
        latest = max(state['latest_session'], expected_session)
        floor = (date.fromisoformat(latest) - timedelta(days=RETENTION_DAYS)).isoformat()
        if expected_session < floor:
            result['state_status'] = 'outside_retention'
            return result
        state['latest_session'] = latest
        state['events'] = {key: value for key, value in state['events'].items()
            if value['event']['session'] >= floor or value['status'] == 'unknown'}
        if preflight_scope:
            status, alerts = _read_preflight_report(report_dir, scope, expected_session)
        else:
            status, alerts = _read_report(report_dir, expected_generation, expected_commit, expected_session)
        result['report_status'] = status
        events = []
        if status != 'available':
            state['report_gap'] = state['report_gap'] or expected_session
            events.append(_event(scope, expected_session, 'report_unavailable', test=test))
        else:
            if state['report_gap'] is not None:
                events.append(_event(scope, expected_session, 'report_recovered', test=test, gap=state['report_gap']))
                state['report_gap'] = None
            if preflight_scope:
                events.extend(_preflight_event(scope, expected_session, alert['kind'], alert['observation'])
                              for alert in alerts)
            else:
                events.extend(_event(scope, expected_session, alert['kind'], test=test,
                                     provider=alert['provider'], alert_id=alert['alert_id']) for alert in alerts)
        if len(state['events']) + sum(event['event_id'] not in state['events'] for event in events) > MAX_EVENTS:
            result['state_status'] = 'capacity_manual_review'
            return result
        for event in events:
            event_id = event['event_id']
            if event_id in state['events']:
                if state['events'][event_id]['status'] == 'confirmed':
                    result['suppressed'].append(event_id)
            else:
                state['events'][event_id] = {'event': event, 'status': 'rejected', 'receipt_id': None}

        def persist():
            content = json.dumps(state, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n'
            if len(content.encode()) > MAX_STATE_BYTES:
                raise ValueError('notification state capacity requires manual review')
            _publish(path, content)

        persist()
        for event_id, entry in state['events'].items():
            if entry['status'] == 'unknown':
                result['unknown'].append(event_id)
                continue
            if entry['status'] == 'confirmed':
                continue
            # Write uncertainty before the side effect. A crash cannot cause an
            # automatic duplicate after a message may have reached its target.
            entry['status'] = 'unknown'
            persist()
            try:
                receipt = send(deepcopy(entry['event']))
            except Exception:
                receipt = DeliveryReceipt('unknown')
            if (isinstance(receipt, DeliveryReceipt) and receipt.status == 'confirmed'
                    and isinstance(receipt.receipt_id, str) and _SAFE_ID.fullmatch(receipt.receipt_id)):
                entry.update(status='confirmed', receipt_id=receipt.receipt_id)
                result['delivered'].append(event_id)
            elif isinstance(receipt, DeliveryReceipt) and receipt.status == 'rejected':
                entry['status'] = 'rejected'
                result['retryable'].append(event_id)
            else:
                result['unknown'].append(event_id)
            persist()
    return result


def consume_source_notifications(report_dir, state_path, *, expected_generation,
        expected_commit, expected_session, recipient_route, send, test=True):
    """Consume bounded source reports with the established source receipt policy."""
    return _consume_notifications(report_dir, state_path,
        expected_generation=expected_generation, expected_commit=expected_commit,
        expected_session=expected_session, recipient_route=recipient_route, send=send, test=test)


def _validate_preflight_observation(row, scope, session):
    from .preflight import canonical_screen_runtime_failures
    fields = {'attempt_id', 'generation_id', 'generation_commit', 'configuration',
              'session', 'preflight_mode', 'phase', 'reason_code', 'status', 'resolved_by'}
    if (not isinstance(row, dict) or set(row) != fields
            or (row['generation_id'], row['generation_commit'], row['configuration'],
                row['session'], row['preflight_mode']) !=
               (scope['generation'], scope['commit'], scope['configuration'], session, scope['mode'])
            or not isinstance(row['attempt_id'], str) or not _SAFE_ID.fullmatch(row['attempt_id'])
            or row['status'] not in {'open', 'resolved'}
            or row['status'] == 'open' and row['resolved_by'] is not None
            or row['status'] == 'resolved' and
               (not isinstance(row['resolved_by'], str) or not _SAFE_ID.fullmatch(row['resolved_by']))
            or canonical_screen_runtime_failures([{'phase': row['phase'],
                'reason_code': row['reason_code']}]) is None):
        raise ValueError('invalid preflight observation')


def _preflight_event(scope, session, kind, observation):
    _validate_preflight_observation(observation, scope, session)
    if kind not in {'preflight_failed', 'preflight_recovered'} or (
            kind == 'preflight_recovered' and observation['status'] != 'resolved'):
        raise ValueError('invalid preflight event')
    # A failure keeps its original identity after recovery; only the recovery
    # event includes the later resolving attempt. Thus rerendering cannot resend it.
    row = dict(observation)
    if kind == 'preflight_failed':
        row.update(status='open', resolved_by=None)
    event_id = hashlib.sha256(json.dumps([PREFLIGHT_STATE_POLICY, kind, row],
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    prefix = '[TEST] ' if scope['test'] else ''
    text = (f'{prefix}EventEdge preflight runtime notification: {kind}; '
        f'generation={scope["generation"]}; session={session}; mode={row["preflight_mode"]}; '
        f'phase={row["phase"]}; reason={row["reason_code"]}; configuration={row["configuration"]}; '
        f'attempt={row["attempt_id"]}; resolved_by={row["resolved_by"] or "none"}; event_id={event_id}. '
        'Inspect the preserved attempt and frozen configuration before rerunning this mode. '
        'Observation only; no trading or repair action authorized.')
    return {'event_id': event_id, 'kind': kind, 'generation_id': scope['generation'],
            'generation_commit': scope['commit'], 'session': session, 'test': scope['test'],
            'observation': row, 'text': text}


def _read_preflight_report(folder, scope, session):
    """Validate projection against preserved attempt facts in the exact JSON/MD pair."""
    from .operational_report import _preflight_runtime_observations
    from .preflight import canonical_screen_runtime_failures
    stem = Path(folder) / f'{session}-{scope["generation"]}-operational-report'
    try:
        report = _bounded_json(stem.with_suffix('.json'), MAX_REPORT_BYTES)
        _identity(report)
        if (report['generation_id'], report['generation_commit'], report['session']) != (
                scope['generation'], scope['commit'], session):
            return 'identity_mismatch', []
        rows, attempts = report['preflight_runtime_observations'], report['attempts']
        if not isinstance(rows, list) or len(rows) > MAX_EVENTS or not isinstance(attempts, list) or len(attempts) > MAX_EVENTS:
            raise ValueError('invalid bounded preflight observation contract')
        for attempt in attempts:
            if attempt['action'] != 'preflight':
                continue
            for key in ('attempt_id', 'generation_commit'):
                if not isinstance(attempt[key], str) or not _SAFE_ID.fullmatch(attempt[key]):
                    raise ValueError('invalid preflight attempt identity')
            config = attempt['configuration']
            if config is not None and (not isinstance(config, str) or not _DIGEST.fullmatch(config)):
                raise ValueError('invalid preflight attempt configuration')
            start, end = datetime.fromisoformat(attempt['started_at']), datetime.fromisoformat(attempt['finished_at'])
            if (start.utcoffset() is None or end.utcoffset() is None or end < start
                    or start.astimezone(timezone.utc).isoformat() != attempt['started_at']
                    or end.astimezone(timezone.utc).isoformat() != attempt['finished_at']
                    or attempt['preflight_mode'] not in {'screen', 'governed', 'all'}
                    or attempt['process_status'] not in {'completed', 'timeout', 'launch_error'}
                    or attempt['process_return_code'] is not None and type(attempt['process_return_code']) is not int
                    or not isinstance(attempt['source_failures'], list)
                    or 'runtime_failures' in attempt and canonical_screen_runtime_failures(attempt['runtime_failures']) is None):
                raise ValueError('invalid preflight attempt facts')
        if rows != _preflight_runtime_observations(attempts, scope['generation'], session):
            raise ValueError('preflight observation differs from attempts')
        selected = []
        for row in rows:
            # Other identities remain visible in reports but cannot enter this receipt scope.
            if (row['generation_commit'], row['configuration'], row['preflight_mode']) != (
                    scope['commit'], scope['configuration'], scope['mode']):
                continue
            _validate_preflight_observation(row, scope, session)
            selected.append({'kind': 'preflight_failed', 'observation': row})
            if row['status'] == 'resolved':
                selected.append({'kind': 'preflight_recovered', 'observation': row})
        with stem.with_suffix('.md').open('rb') as stream:
            markdown = stream.read(MAX_REPORT_BYTES + 1)
        if len(markdown) > MAX_REPORT_BYTES or markdown.decode('utf-8') != render_operational_report(report):
            return 'markdown_mismatch', []
        return 'available', selected
    except FileNotFoundError:
        return 'missing', []
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return 'invalid', []


def consume_preflight_notifications(report_dir, state_path, *, expected_generation,
        expected_commit, expected_session, expected_configuration, expected_mode,
        recipient_route, send, test=True):
    """Local opt-in receipt consumer; separate state, no source-repair authority.

    Only equivalent native screening evidence within the requested session can
    resolve a failure. Unknown receipts retain the established no-resend rule.
    No runtime scheduler or external transport is installed by this function.
    """
    if (not isinstance(expected_configuration, str) or not _DIGEST.fullmatch(expected_configuration)
            or expected_mode not in {'screen', 'all'}):
        raise ValueError('invalid preflight notification scope')
    return _consume_notifications(report_dir, state_path,
        expected_generation=expected_generation, expected_commit=expected_commit,
        expected_session=expected_session, recipient_route=recipient_route, send=send, test=test,
        preflight_scope={'observation': 'preflight_runtime',
                         'configuration': expected_configuration, 'mode': expected_mode})
