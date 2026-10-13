"""Receipt-backed source notifications; no trading, repair, or transport authority.

The caller supplies canonical expected identity, recipient route, and a synchronous
transport. Only an explicit confirmed receipt acknowledges an event. Uncertain
delivery requires manual reconciliation: this module never automatically resends it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re

from .operational_report import _publish, render_operational_report
from .runtime_lock import runtime_lock
from .source_incidents import MAX_REPORT_BYTES, POLICY, _PROVIDERS, _SAFE_ID, _identity

STATE_POLICY = 'source_notifications_v1'
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
    text = (f'{prefix}EventEdge source notification: {kind}; generation={scope["generation"]}; '
            f'session={session}; provider={provider or "report"}; event_id={event_id}. '
            'Observation only; no trading or repair action authorized.')
    return {'event_id': event_id, 'alert_id': alert_id, 'kind': kind, 'provider': provider,
            'generation_id': scope['generation'], 'generation_commit': scope['commit'],
            'session': session, 'test': test, 'text': text}


def _valid_state(state, scope):
    if (not isinstance(state, dict) or state.get('policy') != STATE_POLICY
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
            kind, provider = event['kind'], event['provider']
            if (set(event) != {'event_id', 'alert_id', 'kind', 'provider', 'generation_id',
                              'generation_commit', 'session', 'test', 'text'}
                    or event['generation_id'] != scope['generation']
                    or event['generation_commit'] != scope['commit'] or event['test'] is not scope['test']
                    or kind not in _KINDS | {'report_unavailable', 'report_recovered'}
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


def consume_source_notifications(report_dir, state_path, *, expected_generation,
        expected_commit, expected_session, recipient_route, send, test=True):
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
    path = Path(state_path)
    result = {'state_status': 'available', 'report_status': 'not_read',
              'delivered': [], 'suppressed': [], 'retryable': [], 'unknown': []}
    with runtime_lock(path.with_name(path.name + '.lock'), exclusive=True):
        try:
            state = _bounded_json(path, MAX_STATE_BYTES)
        except FileNotFoundError:
            state = {'policy': STATE_POLICY, 'scope': scope, 'latest_session': expected_session,
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
                receipt = send(dict(entry['event']))
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
