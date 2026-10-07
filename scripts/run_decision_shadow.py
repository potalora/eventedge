#!/usr/bin/env python3
"""Retry saved unattempted Clef shadow inputs without running trading or providers."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.strategies.orchestration.decision_shadow import evaluate_shadow, read_shadow_summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--generation', required=True)
    parser.add_argument('--date', dest='session', required=True)
    parser.add_argument('--epoch-id', required=True)
    parser.add_argument('--commit', dest='generation_commit')
    args = parser.parse_args(argv)
    scope = {'generation': args.generation, 'session': args.session,
             'epoch_id': args.epoch_id, 'generation_commit': args.generation_commit}
    saved = read_shadow_summary(args.state_dir / 'decision_shadow' / f'{args.session}.json', **scope)
    if saved['status'] not in {'missing', 'unavailable'}:
        saved = evaluate_shadow(state_dir=args.state_dir, config=saved['config'], **scope)
    print(json.dumps({'status': saved['status'], 'event_counts': dict(Counter(
        event['status'] for event in saved['events']))}, sort_keys=True))
    return 0 if saved['status'] == 'complete' else 2


if __name__ == '__main__':
    raise SystemExit(main())
