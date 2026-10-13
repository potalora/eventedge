#!/usr/bin/env python3
"""Accelerated synthetic notification drill; local receipt sink by default.

An explicitly supplied transport callback can deliver these TEST payloads to a
validated real recipient. No service, scheduler, production report, or trading
state is read or changed. Every fixture and acknowledgment stays under root_path.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.strategies.orchestration.operational_report import _publish, render_operational_report
from tradingagents.strategies.orchestration.source_incidents import assess_source_incidents
from tradingagents.strategies.orchestration.source_notifications import DeliveryReceipt, consume_source_notifications

GENERATION = 'gen_test_notifications'
COMMIT = 'a' * 40


def publish_test_report(folder, session, *, previous=None, healthy=False):
    """Use the actual incident projector and Markdown renderer for TEST evidence."""
    report = {'generation_id': GENERATION, 'generation_commit': COMMIT, 'session': session,
              'evidence_complete': True, 'outcome': 'degraded', 'accounting_valid': True,
              'input_coverage_valid': healthy, 'staging_complete': False, 'staging_valid': False,
              'attempt_counts': {'daily': 1, 'preflight': 0}, 'cohorts': {}, 'attempts': [],
              'performance_claims_withheld': True, 'candidate_input_issues': [],
              'source_health_failures': [], 'preflight_incidents': [], 'diagnostics': [],
              'sources': {'configuration': 'test_notifications_v1',
                          'acquired_at': session + 'T22:00:00+00:00',
                          'observations': {'fred': 'success' if healthy else 'failed'},
                          'unresolved': [] if healthy else [{'provider': 'fred', 'reason_code': 'timeout'}],
                          'recovered': []}}
    report['source_incidents'] = assess_source_incidents(report, previous)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f'{session}-{GENERATION}-operational-report'
    _publish(stem.with_suffix('.json'), json.dumps(report, indent=2, sort_keys=True) + '\n')
    _publish(stem.with_suffix('.md'), render_operational_report(report))
    return report


def run_drill(root_path, *, send=None, recipient_route='local:test-sink'):
    """Run five TEST notifications and replay each step against durable state.

    root_path must be new or empty. Supply send(event)->DeliveryReceipt explicitly
    to test a real route; the default writes only a local confirmed-receipt sink.
    """
    # macOS tempfile paths often traverse /var -> /private/var. Canonicalize the
    # caller's isolated root before using runtime_lock's no-symlink traversal.
    root = Path(root_path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError('notification drill requires a new or empty directory')
    folder, state_path = root / 'reports', root / 'notification-state.json'
    attempted, steps, receipts = [], [], []

    def transport(event):
        attempted.append(event)
        receipt = send(event) if send is not None else DeliveryReceipt('confirmed', f'local-{len(attempted)}')
        if isinstance(receipt, DeliveryReceipt) and receipt.status == 'confirmed':
            receipts.append({'event_id': event['event_id'], 'kind': event['kind'], 'receipt_id': receipt.receipt_id})
        return receipt

    def step(name, session):
        kwargs = {'expected_generation': GENERATION, 'expected_commit': COMMIT,
                  'expected_session': session, 'recipient_route': recipient_route,
                  'send': transport, 'test': True}
        first = consume_source_notifications(folder, state_path, **kwargs)
        before_replay = len(attempted)
        replay = consume_source_notifications(folder, state_path, **kwargs)
        steps.append({'name': name, 'session': session, 'first': first, 'replay': replay,
                      'replay_transport_calls': len(attempted) - before_replay})

    previous = None
    for session, healthy, name in [('2026-10-06', False, 'opening'),
            ('2026-10-07', False, 'second_failed_session'),
            ('2026-10-08', False, 'escalation'), ('2026-10-09', True, 'source_recovery')]:
        previous = publish_test_report(folder, session, previous=previous, healthy=healthy)
        step(name, session)
    step('missing_report', '2026-10-12')
    publish_test_report(folder, '2026-10-12', previous=previous, healthy=True)
    step('report_recovery', '2026-10-12')
    delivered = [event_id for item in steps for event_id in item['first']['delivered']]
    # The consumer, rather than the transport's claim alone, decides which
    # receipt IDs satisfy the bounded opaque-ID contract.
    receipts = [receipt for receipt in receipts if receipt['event_id'] in delivered]
    kinds = [event['kind'] for event in attempted]
    expected = ['opened', 'escalated', 'recovered', 'report_unavailable', 'report_recovered']
    result = {'test': True, 'transport': 'explicit_callback' if send is not None else 'local_receipt_sink',
              'passed': kinds == expected and len(delivered) == 5 and all(
                  not item['replay_transport_calls'] and not item['first']['unknown']
                  and not item['first']['retryable'] for item in steps),
              'delivered_count': len(delivered), 'kinds': kinds, 'steps': steps,
              'receipts': receipts, 'state_path': str(state_path), 'report_dir': str(folder)}
    _publish(root / 'drill-result.json', json.dumps(result, sort_keys=True, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, help='new or empty local evidence directory')
    args = parser.parse_args()
    root = args.output_dir or Path(tempfile.mkdtemp(prefix='eventedge-source-notification-drill-'))
    result = run_drill(root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
