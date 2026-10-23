#!/usr/bin/env python3
"""Bounded public Form 4 capture, consumer preparation and zero-call replay.

Capture never enables a strategy. Replay requires an independently retained
capture hash and reproduces source discovery, extraction and complete requests.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tradingagents.strategies.data_sources.form4_evidence import digest, validate_form4_acquisition
from tradingagents.strategies.modules.insider_activity import InsiderActivityStrategy
from tradingagents.strategies.orchestration.form4_inputs import form4_analysis_inputs, prepare_form4_request
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore

CODE_FILES = (
    'tradingagents/strategies/data_sources/form4_evidence.py',
    'tradingagents/strategies/data_sources/edgar_source.py',
    'tradingagents/strategies/orchestration/form4_inputs.py',
    'tradingagents/strategies/modules/insider_activity.py',
    'tradingagents/strategies/learning/llm_analyzer.py',
    'tradingagents/strategies/orchestration/multi_strategy_engine.py',
    'tradingagents/strategies/orchestration/source_inputs.py',
    'scripts/qualify_sec_form4.py',
)
MAX_RECEIPT_BYTES = 32 * 1024 * 1024


def read_capture(path, expected_sha256):
    with path.open('rb') as source:
        raw = source.read(MAX_RECEIPT_BYTES+1)
    if len(raw) > MAX_RECEIPT_BYTES or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('invalid_form4_capture_hash')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('invalid_form4_duplicate_field')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def write_receipt(path, value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()+b'\n'
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError('invalid_form4_receipt_byte_limit')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as target:
        target.write(raw)
    return hashlib.sha256(raw).hexdigest()


def source_identity():
    return {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in CODE_FILES}


def build_receipt(acquisition):
    acquisition = validate_form4_acquisition(acquisition)
    shared = {'edgar': {'form4_qualification': acquisition['window']}}
    frozen = SourceInputStore.decode(SourceInputStore.encode(shared))
    if frozen != shared:
        raise ValueError('invalid_form4_source_codec')
    actual_date = max(record['observed_at'][:10] for record in acquisition['history_originals'])
    strategy = InsiderActivityStrategy()
    candidates = strategy.screen(frozen, actual_date, {'min_sell_threshold': 1, 'min_cluster_size': 1,
        'max_positions': 100, 'analysis_budget': 100})
    requests = []
    for candidate in candidates:
        context = form4_analysis_inputs(candidate, frozen)
        request = prepare_form4_request(context, ticker=candidate.ticker, cluster_type=candidate.metadata['cluster_type'])
        if not candidate.journal_only or context['trading_authority'] is not False:
            raise ValueError('invalid_form4_authority')
        requests.append({'ticker': candidate.ticker, 'cluster_type': candidate.metadata['cluster_type'],
            'context': context, 'request': request, 'journal_only': True})
    result = {'version': 'sec-form4-qualification-v1', 'source_code_sha256': source_identity(),
        'acquisition': deepcopy(acquisition), 'requests': requests,
        'source_codec_sha256': digest(frozen), 'model_calls': 0, 'source_replay_calls': 0,
        'trading_authority': False, 'production_changed': False,
        'historical_decision_time_availability_proven': False,
        'limits': ['Declared issuer/date/form subset only.',
            'Class registry is an explicit source review, not broker/SIP eligibility proof.',
            'No amendment resolution, joint-reporter allocation, or trading authorization.',
            'Complete request preparation is not model-quality qualification.']}
    result['qualification_receipt_sha256'] = digest(result)
    return result


def replay_receipt(value):
    try:
        if value['source_code_sha256'] != source_identity():
            raise ValueError('code identity differs')
        expected = build_receipt(value['acquisition'])
        if expected != value:
            raise ValueError('recomputed receipt differs')
        return expected
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError('invalid_form4_qualification_replay') from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    capture = commands.add_parser('capture')
    capture.add_argument('--ticker', required=True)
    capture.add_argument('--issuer-cik', required=True)
    capture.add_argument('--as-of', required=True)
    capture.add_argument('--days-back', type=int, default=0)
    capture.add_argument('--bindings', type=Path, required=True)
    capture.add_argument('--deadline-seconds', type=float, default=60)
    capture.add_argument('--request-cap', type=int, default=6)
    capture.add_argument('--user-agent', default=os.environ.get('EDGAR_USER_AGENT'))
    capture.add_argument('--output', type=Path, required=True)
    replay = commands.add_parser('replay')
    replay.add_argument('--capture', type=Path, required=True)
    replay.add_argument('--expected-sha256', required=True)
    replay.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'capture':
        if not 0 < args.deadline_seconds <= 120:
            parser.error('deadline must be positive and at most120 seconds')
        from tradingagents.strategies.data_sources.edgar_source import EDGARSource
        started = time.monotonic()
        source = EDGARSource(args.user_agent) if args.user_agent else EDGARSource()
        acquisition = source.get_form4_window_evidence(args.ticker, issuer_cik=args.issuer_cik,
            days_back=args.days_back, as_of=args.as_of,
            security_bindings=json.loads(args.bindings.read_text()),
            absolute_deadline=started+args.deadline_seconds, request_cap=args.request_cap)
        value = build_receipt(acquisition)
        replay_receipt(value)
    else:
        value = replay_receipt(read_capture(args.capture, args.expected_sha256))
    output_sha256 = write_receipt(args.output, value)
    print(json.dumps({'command': args.command, 'output': str(args.output),
        'sha256': output_sha256,
        'qualification_receipt_sha256': value['qualification_receipt_sha256'],
        'documents': len(value['acquisition']['window']['documents']),
        'admitted_transactions': len(value['acquisition']['window']['admitted_transactions']),
        'consumer_requests': len(value['requests']), 'model_calls': 0, 'production_changed': False}))


if __name__ == '__main__':
    main()
